from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pm_trader.engine import Engine

from papertrader.config import Settings
from papertrader.signals import Signal
from papertrader.trade_log import append_copy_event, append_skipped
from papertrader.weather import WeatherHttp

log = logging.getLogger("papertrader")

DATA_API = "https://data-api.polymarket.com"
PROFILE_RE = re.compile(r"proxyWallet[^0]*?(0x[a-fA-F0-9]{40})", re.I)


@dataclass(frozen=True)
class CopiedTrade:
    tx_id: str
    side: str
    slug: str
    title: str
    outcome: str
    condition_id: str
    price: float
    size: float
    timestamp: int
    event_slug: str | None = None

    @property
    def notional(self) -> float:
        return self.size * self.price


def trade_key(row: dict[str, Any]) -> str:
    return (
        f"{row.get('transactionHash')}:{row.get('side')}:{row.get('slug')}:"
        f"{row.get('timestamp')}:{row.get('size')}:{row.get('price')}"
    )


def parse_trade(row: dict[str, Any]) -> CopiedTrade:
    side = str(row.get("side") or "BUY").upper()
    return CopiedTrade(
        tx_id=trade_key(row),
        side="SELL" if side == "SELL" else "BUY",
        slug=str(row.get("slug") or ""),
        title=str(row.get("title") or row.get("slug") or ""),
        outcome=str(row.get("outcome") or "Yes").lower(),
        condition_id=str(row.get("conditionId") or ""),
        price=float(row.get("price") or 0),
        size=float(row.get("size") or 0),
        timestamp=int(row.get("timestamp") or 0),
        event_slug=row.get("eventSlug"),
    )


def peak_capital(trades: list[CopiedTrade]) -> float:
    cash = 0.0
    floor = 0.0
    for t in trades:
        cash -= t.notional if t.side == "BUY" else -t.notional
        floor = min(floor, cash)
    return max(-floor, 0.0)


def copy_scale(trades: list[CopiedTrade], budget: float) -> float:
    needed = peak_capital(trades)
    if needed <= 0 or budget <= 0:
        return 1.0
    return min(1.0, budget / needed)


def state_path(engine: Engine) -> Path:
    return Path(engine.db.data_dir) / "copied_trades.json"


def load_state(engine: Engine) -> dict[str, Any]:
    path = state_path(engine)
    if not path.exists():
        return {"seen": [], "scale": None}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"seen": [], "scale": None}


def save_state(engine: Engine, state: dict[str, Any]) -> None:
    path = state_path(engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def resolve_wallet(http: WeatherHttp, settings: Settings) -> str | None:
    if settings.copy.wallet:
        return settings.copy.wallet.lower()
    username = settings.copy.username.lstrip("@")
    try:
        resp = http.client.get(f"https://polymarket.com/@{username}")
        resp.raise_for_status()
    except Exception as e:
        log.warning("copy: profile fetch failed: %s", e)
        return None
    m = PROFILE_RE.search(resp.text)
    return m.group(1).lower() if m else None


def fetch_recent_trades(
    http: WeatherHttp,
    wallet: str,
    *,
    limit: int = 50,
) -> list[CopiedTrade]:
    """Latest trades only — data-api returns newest first."""
    resp = http.client.get(
        f"{DATA_API}/trades",
        params={
            "user": wallet,
            "limit": limit,
            "offset": 0,
            "takerOnly": "false",
        },
        timeout=3.0,
    )
    resp.raise_for_status()
    chunk = resp.json()
    if not isinstance(chunk, list):
        return []
    trades = [parse_trade(r) for r in chunk if r.get("slug")]
    trades.sort(key=lambda t: (t.timestamp, 0 if t.side == "BUY" else 1, t.tx_id))
    return trades


def fetch_trades(http: WeatherHttp, wallet: str) -> list[CopiedTrade]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = http.client.get(
            f"{DATA_API}/trades",
            params={
                "user": wallet,
                "limit": 500,
                "offset": offset,
                "takerOnly": "false",
            },
        )
        resp.raise_for_status()
        chunk = resp.json()
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 500:
            break
        offset += 500
    trades = [parse_trade(r) for r in rows if r.get("slug")]
    trades.sort(key=lambda t: (t.timestamp, 0 if t.side == "BUY" else 1, t.tx_id))
    return trades


def copied_signal(trade: CopiedTrade, scale: float) -> Signal | None:
    shares = trade.size * scale
    usd = shares * trade.price
    if shares <= 0 or usd < 1e-6 or not trade.slug:
        return None
    if trade.side == "BUY":
        return Signal(
            action="buy",
            slug=trade.slug,
            outcome=trade.outcome,
            amount_usd=usd,
            shares=shares,
            event_slug=trade.event_slug,
            reason=f"copy @{trade.tx_id.split(':')[0][:10]} {trade.title}",
        )
    return Signal(
        action="sell",
        slug=trade.slug,
        outcome=trade.outcome,
        shares=shares,
        event_slug=trade.event_slug,
        reason=f"copy @{trade.tx_id.split(':')[0][:10]} {trade.title}",
    )


def apply_copied_trade(engine: Engine, trade: CopiedTrade, scale: float) -> Signal | None:
    fill_started = time.perf_counter()
    detected_at = time.time()
    shares = trade.size * scale
    usd = shares * trade.price
    if shares <= 0 or usd < 1e-6 or not trade.slug or not trade.condition_id:
        append_copy_event(
            engine.db.data_dir,
            tx_id=trade.tx_id,
            leader_ts=trade.timestamp,
            side=trade.side,
            slug=trade.slug,
            title=trade.title,
            status="skipped",
            error="invalid trade size or missing slug/condition",
            detected_at=detected_at,
        )
        return None
    market = SimpleNamespace(
        condition_id=trade.condition_id,
        slug=trade.slug,
        question=trade.title,
    )
    account = engine.get_account()
    if trade.side == "BUY":
        if usd > account.cash:
            usd = account.cash
            if usd <= 0 or trade.price <= 0:
                append_copy_event(
                    engine.db.data_dir,
                    tx_id=trade.tx_id,
                    leader_ts=trade.timestamp,
                    side=trade.side,
                    slug=trade.slug,
                    title=trade.title,
                    status="skipped",
                    error="insufficient cash",
                    detected_at=detected_at,
                )
                return None
            shares = usd / trade.price
        engine.db.update_cash(account.cash - usd)
        recorded = engine.db.insert_trade(
            market_condition_id=trade.condition_id,
            market_slug=trade.slug,
            market_question=trade.title,
            outcome=trade.outcome,
            side="buy",
            order_type="fak",
            avg_price=trade.price,
            amount_usd=usd,
            shares=shares,
            fee_rate_bps=0,
            fee=0.0,
            slippage=0.0,
            levels_filled=1,
            is_partial=False,
        )
        engine._update_position_after_buy(
            market=market,
            outcome=trade.outcome,
            new_shares=shares,
            cost=usd,
            avg_fill_price=trade.price,
        )
        append_copy_event(
            engine.db.data_dir,
            tx_id=trade.tx_id,
            leader_ts=trade.timestamp,
            side=trade.side,
            slug=trade.slug,
            title=trade.title,
            status="filled",
            trade_id=recorded.id,
            detected_at=detected_at,
            fill_latency_ms=(time.perf_counter() - fill_started) * 1000,
        )
        return Signal(
            action="buy",
            slug=trade.slug,
            outcome=trade.outcome,
            amount_usd=usd,
            shares=shares,
            event_slug=trade.event_slug,
            reason=f"copy @{trade.tx_id.split(':')[0][:10]} {trade.title}",
        )

    existing = engine.db.get_position(trade.condition_id, trade.outcome)
    if existing is None or existing.shares <= 0:
        append_copy_event(
            engine.db.data_dir,
            tx_id=trade.tx_id,
            leader_ts=trade.timestamp,
            side=trade.side,
            slug=trade.slug,
            title=trade.title,
            status="skipped",
            error="no position to sell",
            detected_at=detected_at,
        )
        return None
    sold = min(shares, existing.shares)
    proceeds = sold * trade.price
    engine.db.update_cash(account.cash + proceeds)
    recorded = engine.db.insert_trade(
        market_condition_id=trade.condition_id,
        market_slug=trade.slug,
        market_question=trade.title,
        outcome=trade.outcome,
        side="sell",
        order_type="fak",
        avg_price=trade.price,
        amount_usd=proceeds,
        shares=sold,
        fee_rate_bps=0,
        fee=0.0,
        slippage=0.0,
        levels_filled=1,
        is_partial=sold < shares,
    )
    engine._update_position_after_sell(
        market=market,
        outcome=trade.outcome,
        sold_shares=sold,
        proceeds=proceeds,
    )
    append_copy_event(
        engine.db.data_dir,
        tx_id=trade.tx_id,
        leader_ts=trade.timestamp,
        side=trade.side,
        slug=trade.slug,
        title=trade.title,
        status="filled",
        trade_id=recorded.id,
        detected_at=detected_at,
        fill_latency_ms=(time.perf_counter() - fill_started) * 1000,
    )
    return Signal(
        action="sell",
        slug=trade.slug,
        outcome=trade.outcome,
        shares=sold,
        event_slug=trade.event_slug,
        reason=f"copy @{trade.tx_id.split(':')[0][:10]} {trade.title}",
    )


def _resolve_scale(
    settings: Settings,
    state: dict[str, Any],
    trades: list[CopiedTrade],
) -> float:
    if settings.copy.scale is not None:
        scale = float(settings.copy.scale)
        state["scale"] = scale
        return scale
    scale = state.get("scale")
    if scale is None:
        scale = copy_scale(trades, settings.starting_balance)
        state["scale"] = scale
    return float(scale)


def _fast_live_seed(
    http: WeatherHttp,
    wallet: str,
    state: dict[str, Any],
    *,
    recent_limit: int,
) -> None:
    """Mark current leader activity as seen without paging full trade history."""
    recent = fetch_recent_trades(http, wallet, limit=recent_limit)
    seen = set(state.get("seen") or [])
    seen.update(t.tx_id for t in recent)
    state["seen"] = list(seen)
    if recent:
        state["last_leader_ts"] = max(t.timestamp for t in recent)
    state["live_seeded"] = True


def _pending_trades(
    trades: list[CopiedTrade],
    seen: set[str],
    last_leader_ts: int,
) -> list[CopiedTrade]:
    pending = [
        t
        for t in trades
        if t.tx_id not in seen and t.timestamp >= last_leader_ts
    ]
    pending.sort(key=lambda t: (t.timestamp, 0 if t.side == "BUY" else 1, t.tx_id))
    return pending


def _process_pending_trade(
    engine: Engine,
    trade: CopiedTrade,
    scale: float,
    *,
    dry_run: bool,
    live: bool,
    execute: Callable[[Signal], bool] | None,
    seen: set[str],
) -> Signal | None:
    detected_at = time.time()
    if live and execute is not None:
        sig = copied_signal(trade, scale)
        seen.add(trade.tx_id)
        if not sig:
            append_copy_event(
                engine.db.data_dir,
                tx_id=trade.tx_id,
                leader_ts=trade.timestamp,
                side=trade.side,
                slug=trade.slug,
                title=trade.title,
                status="skipped",
                error="signal too small or invalid",
                detected_at=detected_at,
            )
            return None
        fill_started = time.perf_counter()
        if execute(sig):
            append_copy_event(
                engine.db.data_dir,
                tx_id=trade.tx_id,
                leader_ts=trade.timestamp,
                side=trade.side,
                slug=trade.slug,
                title=trade.title,
                status="filled",
                detected_at=detected_at,
                fill_latency_ms=(time.perf_counter() - fill_started) * 1000,
            )
            log.info(
                "COPY LIVE %s %s @ %.3f (detect %.0f ms) — %s",
                trade.side,
                trade.slug,
                trade.price,
                (detected_at - trade.timestamp) * 1000,
                trade.title,
            )
            return sig
        append_skipped(
            engine.db.data_dir,
            strategy="copy",
            signal=sig,
            error="live execute returned false",
            source="copy",
        )
        append_copy_event(
            engine.db.data_dir,
            tx_id=trade.tx_id,
            leader_ts=trade.timestamp,
            side=trade.side,
            slug=trade.slug,
            title=trade.title,
            status="skipped",
            error="live execute returned false",
            detected_at=detected_at,
        )
        return None

    sig = apply_copied_trade(engine, trade, scale)
    seen.add(trade.tx_id)
    if sig:
        log.info(
            "COPY %s %s @ %.3f shares=%.2f — %s",
            sig.action.upper(),
            sig.slug,
            trade.price,
            sig.shares or 0,
            sig.reason,
        )
    return sig


def sync_copy_trades(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    dry_run: bool,
    live: bool = False,
    execute: Callable[[Signal], bool] | None = None,
    *,
    recent_limit: int | None = None,
) -> tuple[int, list[Signal]]:
    wallet = resolve_wallet(http, settings)
    if not wallet:
        log.warning("copy: could not resolve wallet for @%s", settings.copy.username)
        return 0, []
    limit = recent_limit if recent_limit is not None else settings.copy.recent_limit
    try:
        trades = fetch_recent_trades(http, wallet, limit=limit)
    except Exception as e:
        log.warning("copy: trade fetch failed: %s", e)
        return 0, []
    state = load_state(engine)
    seen = set(state.get("seen") or [])
    last_leader_ts = int(state.get("last_leader_ts") or 0)
    if state.get("live_seeded") and not state.get("last_leader_ts") and trades:
        last_leader_ts = max(t.timestamp for t in trades)
        state["last_leader_ts"] = last_leader_ts
        save_state(engine, state)
    scale = _resolve_scale(settings, state, trades)
    if live and not state.get("live_seeded"):
        _fast_live_seed(http, wallet, state, recent_limit=limit)
        save_state(engine, state)
        log.warning(
            "copy live: fast-seeded from last %s trades (ts>=%s); only new prints will be copied",
            limit,
            state.get("last_leader_ts", 0),
        )
        return 0, []
    pending = _pending_trades(trades, seen, last_leader_ts)
    signals: list[Signal] = []
    if dry_run:
        for t in pending:
            log.info(
                "DRY-RUN copy %s %s usd=%.4f — %s",
                t.side,
                t.slug,
                t.notional * scale,
                t.title,
            )
        if pending:
            state["last_leader_ts"] = max(last_leader_ts, max(t.timestamp for t in pending))
            state["seen"] = list(seen)
            save_state(engine, state)
        return len(pending), []
    for t in pending:
        sig = _process_pending_trade(
            engine,
            t,
            scale,
            dry_run=dry_run,
            live=live,
            execute=execute,
            seen=seen,
        )
        if sig:
            signals.append(sig)
        last_leader_ts = max(last_leader_ts, t.timestamp)
    if pending:
        state["last_leader_ts"] = last_leader_ts
        state["seen"] = list(seen)
        save_state(engine, state)
    return len(pending), signals
