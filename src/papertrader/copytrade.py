from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pm_trader.engine import Engine

from papertrader.config import Settings
from papertrader.signals import Signal
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
    shares = trade.size * scale
    usd = shares * trade.price
    if shares <= 0 or usd < 1e-6 or not trade.slug or not trade.condition_id:
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
                return None
            shares = usd / trade.price
        engine.db.update_cash(account.cash - usd)
        engine.db.insert_trade(
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
        return None
    sold = min(shares, existing.shares)
    proceeds = sold * trade.price
    engine.db.update_cash(account.cash + proceeds)
    engine.db.insert_trade(
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
    return Signal(
        action="sell",
        slug=trade.slug,
        outcome=trade.outcome,
        shares=sold,
        event_slug=trade.event_slug,
        reason=f"copy @{trade.tx_id.split(':')[0][:10]} {trade.title}",
    )


def sync_copy_trades(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    dry_run: bool,
    live: bool = False,
    execute: Callable[[Signal], bool] | None = None,
) -> tuple[int, list[Signal]]:
    wallet = resolve_wallet(http, settings)
    if not wallet:
        log.warning("copy: could not resolve wallet for @%s", settings.copy.username)
        return 0, []
    try:
        trades = fetch_trades(http, wallet)
    except Exception as e:
        log.warning("copy: trade fetch failed: %s", e)
        return 0, []
    state = load_state(engine)
    seen = set(state.get("seen") or [])
    scale = state.get("scale")
    if scale is None:
        scale = copy_scale(trades, settings.starting_balance)
        state["scale"] = scale
    if live and not state.get("live_seeded"):
        state["seen"] = [t.tx_id for t in trades]
        state["live_seeded"] = True
        save_state(engine, state)
        log.warning(
            "copy live: seeded %s historical trades as seen; only new prints will be copied",
            len(trades),
        )
        return 0, []
    pending = [t for t in trades if t.tx_id not in seen]
    signals: list[Signal] = []
    if dry_run:
        for t in pending:
            log.info(
                "DRY-RUN copy %s %s usd=%.4f — %s",
                t.side,
                t.slug,
                t.notional * float(scale),
                t.title,
            )
        return len(pending), []
    for t in pending:
        if live and execute is not None:
            sig = copied_signal(t, float(scale))
            seen.add(t.tx_id)
            if sig and execute(sig):
                signals.append(sig)
            continue
        sig = apply_copied_trade(engine, t, float(scale))
        seen.add(t.tx_id)
        if sig:
            signals.append(sig)
            log.info(
                "COPY %s %s @ %.3f shares=%.2f — %s",
                sig.action.upper(),
                sig.slug,
                t.price,
                sig.shares or 0,
                sig.reason,
            )
    state["seen"] = list(seen)
    save_state(engine, state)
    return len(pending), signals
