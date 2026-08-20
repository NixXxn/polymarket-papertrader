"""BTC 5-minute Up/Down scalper with spot-vs-open prediction (high win-rate late confirms)."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pm_trader.engine import Engine

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.markets import best_ask, best_bid
from papertrader.signals import Signal

log = logging.getLogger("papertrader")

WINDOW_SECONDS = 300
SLUG_PREFIX = "btc-updown-5m-"


@dataclass(frozen=True)
class BtcPrediction:
    side: str  # "Up" or "Down"
    model_p: float
    move_bps: float
    spot: float
    open_px: float
    seconds_left: float
    confidence: str


def _parse_json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _http_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-papertrader/btc5m"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_btc_spot_usd() -> float | None:
    """Binance BTCUSDT last price (proxy for Chainlink; same direction for short windows)."""
    try:
        data = _http_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
        return float(data["price"])
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError) as e:
        log.warning("btc5m: spot fetch failed: %s", e)
        return None


def fetch_btc_open_usd(window_start_unix: int) -> float | None:
    """1m candle open at/just after the 5m window start."""
    try:
        start_ms = int(window_start_unix) * 1000
        url = (
            "https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval=1m&startTime={start_ms}&limit=1"
        )
        data = _http_json(url)
        if not data:
            return None
        return float(data[0][1])  # open
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, IndexError) as e:
        log.warning("btc5m: open fetch failed: %s", e)
        return None


def predict_direction(
    *,
    spot: float,
    open_px: float,
    seconds_left: float,
    min_confirm_bps: float,
    max_entry_seconds_left: float,
    min_entry_seconds_left: float,
) -> BtcPrediction | None:
    """High-WR predictor: only fire when spot already confirmed vs window open late in the window."""
    if open_px <= 0 or spot <= 0:
        return None
    if not (min_entry_seconds_left <= seconds_left <= max_entry_seconds_left):
        return None
    move_bps = (spot - open_px) / open_px * 10_000.0
    if abs(move_bps) < min_confirm_bps:
        return None
    side = "Up" if move_bps > 0 else "Down"
    # Stronger move + less time left => higher model probability.
    time_factor = 1.0 - (seconds_left / WINDOW_SECONDS) * 0.35
    strength = min(1.0, abs(move_bps) / max(min_confirm_bps * 3.0, 1e-6))
    model_p = min(0.97, 0.55 + 0.40 * strength * time_factor)
    if model_p >= 0.85:
        confidence = "very_high"
    elif model_p >= 0.72:
        confidence = "high"
    else:
        confidence = "medium"
    if confidence == "medium":
        return None
    return BtcPrediction(
        side=side,
        model_p=model_p,
        move_bps=move_bps,
        spot=spot,
        open_px=open_px,
        seconds_left=seconds_left,
        confidence=confidence,
    )


def _window_starts(now: float | None = None, look_ahead: int = 2) -> list[int]:
    t = int(now if now is not None else time.time())
    base = t - (t % WINDOW_SECONDS)
    return [base + i * WINDOW_SECONDS for i in range(0, look_ahead + 1)]


def discover_btc5m_windows(engine: Engine, *, look_ahead: int = 2) -> list[dict[str, Any]]:
    """Load current/upcoming btc-updown-5m events from Gamma."""
    out: list[dict[str, Any]] = []
    for start in _window_starts(look_ahead=look_ahead):
        slug = f"{SLUG_PREFIX}{start}"
        try:
            events = engine.api._gamma_get("/events", params={"slug": slug})
        except Exception as e:
            log.debug("btc5m: event fetch failed %s: %s", slug, e)
            continue
        if not isinstance(events, list) or not events:
            continue
        event = events[0]
        markets = event.get("markets") or []
        if not markets:
            continue
        market = markets[0]
        outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
        prices = [float(p) for p in _parse_json_list(market.get("outcomePrices"))]
        token_ids = [str(t) for t in _parse_json_list(market.get("clobTokenIds"))]
        if set(o.lower() for o in outcomes) != {"up", "down"}:
            continue
        end_raw = event.get("endDate") or market.get("endDate") or ""
        try:
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        except ValueError:
            end_dt = datetime.fromtimestamp(start + WINDOW_SECONDS, tz=timezone.utc)
        out.append(
            {
                "window_start": start,
                "event_slug": slug,
                "market_slug": market.get("slug") or slug,
                "condition_id": market.get("conditionId") or "",
                "question": market.get("question") or event.get("title") or slug,
                "outcomes": outcomes,
                "prices": dict(zip([o.lower() for o in outcomes], prices)) if len(prices) == len(outcomes) else {},
                "token_ids": dict(zip([o.lower() for o in outcomes], token_ids))
                if len(token_ids) == len(outcomes)
                else {},
                "end_dt": end_dt,
                "liquidity": float(market.get("liquidity") or market.get("liquidityNum") or 0),
            }
        )
    return out


def analyze_btc5m(
    engine: Engine,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[Signal]:
    """Scan live BTC 5m windows and buy confirmed Up/Down favorites via FAK."""
    cfg = settings.btc5m
    now = now or datetime.now(timezone.utc)
    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions}
    if len(open_positions) >= cfg.max_open_positions:
        log_decision(
            engine.db.data_dir,
            strategy="btc5m",
            decision="scan",
            reason="max_open_positions",
            open_positions=len(open_positions),
        )
        return []

    windows = discover_btc5m_windows(engine, look_ahead=cfg.look_ahead_windows)
    spot = fetch_btc_spot_usd()
    if spot is None:
        log_decision(
            engine.db.data_dir,
            strategy="btc5m",
            decision="scan",
            reason="spot_unavailable",
        )
        return []

    signals: list[Signal] = []
    rejects: dict[str, int] = {
        "not_started": 0,
        "outside_time": 0,
        "weak_move": 0,
        "no_open": 0,
        "no_book": 0,
        "ask_band": 0,
        "low_edge": 0,
        "already_open": 0,
        "low_liq": 0,
        "tiny_size": 0,
    }

    for w in windows:
        start = int(w["window_start"])
        if now.timestamp() < start:
            rejects["not_started"] += 1
            continue
        seconds_left = (w["end_dt"] - now).total_seconds()
        open_px = fetch_btc_open_usd(start)
        if open_px is None:
            rejects["no_open"] += 1
            continue
        pred = predict_direction(
            spot=spot,
            open_px=open_px,
            seconds_left=seconds_left,
            min_confirm_bps=cfg.min_confirm_bps,
            max_entry_seconds_left=cfg.max_entry_seconds_left,
            min_entry_seconds_left=cfg.min_entry_seconds_left,
        )
        if pred is None:
            # Distinguish weak move vs time window for logging.
            move_bps = (spot - open_px) / open_px * 10_000.0
            if not (cfg.min_entry_seconds_left <= seconds_left <= cfg.max_entry_seconds_left):
                rejects["outside_time"] += 1
            elif abs(move_bps) < cfg.min_confirm_bps:
                rejects["weak_move"] += 1
            else:
                rejects["weak_move"] += 1
            continue

        if float(w["liquidity"]) < cfg.min_liquidity:
            rejects["low_liq"] += 1
            continue

        slug = str(w["market_slug"])
        if slug in open_slugs:
            rejects["already_open"] += 1
            continue

        side = pred.side
        try:
            market = engine.api.get_market(slug)
            token = market.get_token_id(side)
            book = engine.api.get_order_book(token)
            ask, ask_size = best_ask(book)
        except Exception:
            rejects["no_book"] += 1
            continue
        if ask is None or ask_size <= 0:
            rejects["no_book"] += 1
            continue
        if not (cfg.min_ask <= ask <= cfg.max_ask):
            rejects["ask_band"] += 1
            continue
        edge = pred.model_p - ask
        if edge < cfg.min_edge:
            rejects["low_edge"] += 1
            continue

        cash = float(engine.get_account().cash)
        size = min(cfg.position_usd, cfg.max_position_usd, cash)
        if size < settings.min_position_usd:
            rejects["tiny_size"] += 1
            continue

        reason = (
            f"btc5m {pred.side} conf={pred.confidence} "
            f"move={pred.move_bps:+.1f}bps left={pred.seconds_left:.0f}s "
            f"model={pred.model_p:.2f} ask={ask:.3f} edge={edge:.3f} "
            f"spot={pred.spot:.1f}/open={pred.open_px:.1f}"
        )
        signals.append(
            Signal(
                action="buy",
                slug=slug,
                outcome=side,
                reason=reason,
                amount_usd=round(size, 2),
                order_type="fak",
                limit_price=None,
                market_condition_id=str(w["condition_id"]),
            )
        )
        log_decision(
            engine.db.data_dir,
            strategy="btc5m",
            decision="buy",
            reason=reason,
            slug=slug,
            action="buy",
            amount_usd=round(size, 2),
            ask=ask,
            model_p=pred.model_p,
            move_bps=pred.move_bps,
            seconds_left=pred.seconds_left,
        )
        open_slugs.add(slug)
        if len(signals) + len(open_positions) >= cfg.max_open_positions:
            break

    log_decision(
        engine.db.data_dir,
        strategy="btc5m",
        decision="scan",
        reason=f"btc5m scan: {len(signals)} signals / {len(windows)} windows / rejects={rejects}",
        candidates=len(signals),
        rejects=rejects,
        spot=spot,
    )
    return signals


def btc5m_exits(engine: Engine, settings: Settings) -> list[Signal]:
    """Hold to resolution; exit only on catastrophic reverse vs entry."""
    cfg = settings.btc5m
    signals: list[Signal] = []
    for pos in engine.db.get_open_positions():
        if pos.shares <= 0 or pos.avg_entry_price <= 0:
            continue
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            bid, _ = best_bid(book)
        except Exception:
            continue
        if bid is None:
            continue
        stop = max(0.15, pos.avg_entry_price * (1.0 - cfg.stop_loss_pct))
        if bid > stop:
            continue
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                reason=f"btc5m stop bid={bid:.3f}",
                order_type="fak",
            )
        )
        log_decision(
            engine.db.data_dir,
            strategy="btc5m",
            decision="sell",
            reason=f"stop bid={bid:.3f} entry={pos.avg_entry_price:.3f}",
            slug=pos.market_slug,
            action="sell",
            shares=pos.shares,
            bid=bid,
        )
    return signals


