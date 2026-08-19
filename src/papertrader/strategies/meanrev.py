"""Mean Reversion strategy: fade 2σ+ price deviations from rolling average."""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

from pm_trader.engine import Engine

from papertrader.config import MeanReversionSettings, Settings
from papertrader.decision_log import log_decision
from papertrader.signals import Signal
from papertrader.trade_log import append_activity

log = logging.getLogger("papertrader")


@dataclass
class _MarketSnapshot:
    condition_id: str
    slug: str
    question: str
    yes_price: float
    no_price: float
    liquidity: float
    volume_24h: float
    token_id_yes: str
    token_id_no: str


class MeanReversionEngine:
    """Tracks price history per market and generates mean-reversion signals."""

    def __init__(self, window: int = 168):
        self._history: dict[str, deque[float]] = {}
        self._window = window

    def update(self, cid: str, price: float) -> None:
        if cid not in self._history:
            self._history[cid] = deque(maxlen=self._window)
        self._history[cid].append(price)

    def z_score(self, cid: str, price: float) -> float | None:
        hist = self._history.get(cid)
        if not hist or len(hist) < 10:
            return None
        arr = list(hist)
        mu = sum(arr) / len(arr)
        variance = sum((x - mu) ** 2 for x in arr) / len(arr)
        sigma = math.sqrt(variance)
        if sigma < 0.005:
            return None
        return (price - mu) / sigma

    def mean(self, cid: str) -> float | None:
        hist = self._history.get(cid)
        if not hist or len(hist) < 10:
            return None
        return sum(hist) / len(hist)


_engine = MeanReversionEngine()


def discover_general_markets(engine: Engine, settings: Settings) -> list[_MarketSnapshot]:
    """Fetch active markets from Gamma API."""
    cfg = settings.meanrev
    try:
        data = engine.api._gamma_get(
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 200,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    except Exception as e:
        log.warning("meanrev: failed to fetch markets: %s", e)
        return []
    if not isinstance(data, list):
        return []
    out: list[_MarketSnapshot] = []
    for m in data:
        try:
            liq = float(m.get("liquidity") or 0)
            if liq < cfg.min_liquidity:
                continue
            tokens = m.get("tokens") or []
            yt = next((t for t in tokens if (t.get("outcome") or "").upper() == "YES"), {})
            nt = next((t for t in tokens if (t.get("outcome") or "").upper() == "NO"), {})
            yes_p = float(yt.get("price") or 0.5)
            no_p = float(nt.get("price") or 0.5)
            if not (cfg.price_min <= yes_p <= cfg.price_max):
                continue
            slug = m.get("slug") or m.get("conditionId") or ""
            out.append(_MarketSnapshot(
                condition_id=m.get("conditionId") or "",
                slug=slug,
                question=m.get("question") or "",
                yes_price=yes_p,
                no_price=no_p,
                liquidity=liq,
                volume_24h=float(m.get("volume24hr") or 0),
                token_id_yes=yt.get("tokenId") or "",
                token_id_no=nt.get("tokenId") or "",
            ))
        except Exception:
            continue
    return out


def analyze_meanrev(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 3,
) -> list[Signal]:
    """Scan general markets for mean-reversion opportunities."""
    cfg = settings.meanrev
    markets = discover_general_markets(engine, settings)
    signals: list[Signal] = []

    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions}
    if len(open_positions) >= cfg.max_open_positions:
        return []

    for m in markets:
        if m.slug in open_slugs:
            continue
        _engine.update(m.condition_id, m.yes_price)
        z = _engine.z_score(m.condition_id, m.yes_price)
        if z is None:
            continue
        if abs(z) < cfg.min_z_score:
            continue

        mu = _engine.mean(m.condition_id)
        if mu is None:
            continue
        ev = abs(m.yes_price - mu)
        if ev < cfg.min_edge:
            continue

        # Fade the deviation: if price spiked up, sell/short (buy NO); if down, buy YES
        if z > 0:
            side = "No"
            price = m.no_price
        else:
            side = "Yes"
            price = m.yes_price

        # Kelly sizing
        p = min(max(0.55, 0.5 + ev), 0.95)
        b = max(0.01, (1 / price) - 1)
        q = 1 - p
        f_kelly = max(0, (p * b - q) / b)
        size = min(
            f_kelly * cfg.kelly_fraction * engine.get_account().cash,
            cfg.max_position_usd,
            cfg.position_usd,
        )
        if size < 1:
            continue

        signals.append(Signal(
            action="buy",
            slug=m.slug,
            outcome=side,
            reason=f"meanrev z={z:+.2f} ev={ev:.3f}",
            amount_usd=round(size, 2),
            order_type="limit",
            limit_price=round(price, 2),
            market_condition_id=m.condition_id,
        ))

        log_decision(
            engine.db.data_dir,
            strategy="meanrev",
            decision="signal",
            reason=f"z={z:+.2f} ev={ev:.3f}",
            slug=m.slug,
            action="buy",
            amount_usd=round(size, 2),
        )

        if len(signals) >= max_signals:
            break

    return signals


def meanrev_exits(
    engine: Engine,
    settings: Settings,
) -> list[Signal]:
    """Generate exit signals for mean-reversion positions."""
    cfg = settings.meanrev
    positions = engine.db.get_open_positions()
    signals: list[Signal] = []

    for pos in positions:
        entry = pos.avg_entry_price
        current_bid = entry  # approximate; real impl would fetch live bid
        try:
            book = engine.get_order_book(pos.market_slug, pos.outcome)
            if book and book.bids:
                current_bid = float(book.bids[0].price)
        except Exception:
            continue

        # Take profit
        if current_bid >= entry * (1 + cfg.take_profit_pct):
            signals.append(Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                reason=f"meanrev_tp bid={current_bid:.3f}",
                shares=pos.shares,
                order_type="limit",
                limit_price=round(current_bid, 2),
            ))
            continue

        # Stop loss
        if current_bid <= entry * (1 - cfg.stop_loss_pct):
            signals.append(Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                reason=f"meanrev_sl bid={current_bid:.3f}",
                shares=pos.shares,
                order_type="limit",
                limit_price=round(current_bid, 2),
            ))

    return signals
