"""Volume Spike strategy: follow 3x+ unusual volume surges (informed trading)."""
from __future__ import annotations

import logging
import math
from collections import deque

from pm_trader.engine import Engine

from papertrader.config import Settings, VolumeSpikeSettings
from papertrader.decision_log import log_decision
from papertrader.signals import Signal

log = logging.getLogger("papertrader")


class _VolumeTracker:
    def __init__(self, maxlen: int = 48):
        self._hist: dict[str, deque[float]] = {}
        self._maxlen = maxlen

    def update(self, cid: str, volume: float) -> None:
        if cid not in self._hist:
            self._hist[cid] = deque(maxlen=self._maxlen)
        self._hist[cid].append(volume)

    def spike_ratio(self, cid: str) -> float | None:
        hist = self._hist.get(cid)
        if not hist or len(hist) < 5:
            return None
        current = hist[-1]
        if current == 0:
            return None
        prev = list(hist)[:-1]
        avg = sum(prev) / len(prev)
        if avg < 100:
            return None
        return current / avg


_tracker = _VolumeTracker()


def analyze_volspike(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 3,
) -> list[Signal]:
    """Scan markets for volume spike opportunities."""
    cfg = settings.volspike
    from papertrader.strategies.meanrev import discover_general_markets

    markets = discover_general_markets(engine, settings)
    signals: list[Signal] = []

    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions}
    if len(open_positions) >= cfg.max_open_positions:
        return []

    for m in markets:
        if m.slug in open_slugs:
            continue
        if m.liquidity < cfg.min_liquidity:
            continue

        _tracker.update(m.condition_id, m.volume_24h)
        spike = _tracker.spike_ratio(m.condition_id)
        if spike is None or spike < cfg.spike_threshold:
            continue

        # Follow the direction: if price > 0.5, smart money is buying YES
        if m.yes_price > 0.5:
            side = "Yes"
            price = m.yes_price
        else:
            side = "No"
            price = m.no_price

        edge = min(0.12, (spike - cfg.spike_threshold) * 0.01 + cfg.min_edge)

        # Kelly sizing
        p = min(max(0.55, 0.5 + edge), 0.90)
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
            reason=f"volspike {spike:.1f}x edge={edge:.3f}",
            amount_usd=round(size, 2),
            order_type="limit",
            limit_price=round(price, 2),
            market_condition_id=m.condition_id,
        ))

        log_decision(
            engine.db.data_dir,
            strategy="volspike",
            decision="signal",
            reason=f"spike={spike:.1f}x",
            slug=m.slug,
            action="buy",
            amount_usd=round(size, 2),
        )

        if len(signals) >= max_signals:
            break

    return signals


def volspike_exits(
    engine: Engine,
    settings: Settings,
) -> list[Signal]:
    """Generate exit signals for volume-spike positions."""
    cfg = settings.volspike
    positions = engine.db.get_open_positions()
    signals: list[Signal] = []

    for pos in positions:
        entry = pos.avg_entry_price
        try:
            book = engine.get_order_book(pos.market_slug, pos.outcome)
            if not book or not book.bids:
                continue
            current_bid = float(book.bids[0].price)
        except Exception:
            continue

        if current_bid >= entry * (1 + cfg.take_profit_pct):
            signals.append(Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                reason=f"volspike_tp bid={current_bid:.3f}",
                shares=pos.shares,
                order_type="limit",
                limit_price=round(current_bid, 2),
            ))
            continue

        if current_bid <= entry * (1 - cfg.stop_loss_pct):
            signals.append(Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                reason=f"volspike_sl bid={current_bid:.3f}",
                shares=pos.shares,
                order_type="limit",
                limit_price=round(current_bid, 2),
            ))

    return signals
