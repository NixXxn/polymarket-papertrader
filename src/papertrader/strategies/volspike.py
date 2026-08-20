"""Volume / activity spike strategy: follow sudden price+volume bursts."""
from __future__ import annotations

import logging
from collections import deque

from pm_trader.engine import Engine

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.markets import best_bid
from papertrader.signals import Signal

log = logging.getLogger("papertrader")


class _ActivityTracker:
    """Tracks per-market price/volume samples between polls."""

    def __init__(self, maxlen: int = 48):
        self._prices: dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[float]] = {}
        self._maxlen = maxlen

    def configure(self, maxlen: int) -> None:
        if maxlen == self._maxlen:
            return
        self._maxlen = max(8, maxlen)
        for cid, hist in list(self._prices.items()):
            self._prices[cid] = deque(hist, maxlen=self._maxlen)
        for cid, hist in list(self._volumes.items()):
            self._volumes[cid] = deque(hist, maxlen=self._maxlen)

    def update(self, cid: str, *, price: float, volume: float) -> None:
        if cid not in self._prices:
            self._prices[cid] = deque(maxlen=self._maxlen)
            self._volumes[cid] = deque(maxlen=self._maxlen)
        self._prices[cid].append(price)
        self._volumes[cid].append(volume)

    def spike_score(self, cid: str, *, spike_threshold: float) -> float | None:
        """
        Return activity score when recent price jump is large vs history.

        Gamma volume24hr barely moves between polls, so we primarily use
        inter-poll price velocity, boosted when volume is elevated.
        """
        prices = self._prices.get(cid)
        volumes = self._volumes.get(cid)
        if not prices or len(prices) < 3:
            return None
        last = prices[-1]
        prev = prices[-2]
        move = abs(last - prev)
        if move < 0.015:
            return None
        # Compare current move to typical absolute move.
        moves = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        baseline = (sum(moves[:-1]) / max(1, len(moves) - 1)) if len(moves) > 1 else move
        baseline = max(baseline, 0.005)
        score = move / baseline
        if volumes and len(volumes) >= 3:
            vol_now = volumes[-1]
            vol_avg = sum(list(volumes)[:-1]) / max(1, len(volumes) - 1)
            if vol_avg > 0 and vol_now > vol_avg:
                score *= min(2.0, vol_now / vol_avg)
        if score < spike_threshold:
            return None
        return score


_tracker = _ActivityTracker()


def analyze_volspike(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 3,
) -> list[Signal]:
    """Scan markets for volume/activity spike opportunities."""
    cfg = settings.volspike
    from papertrader.strategies.meanrev import discover_general_markets

    _tracker.configure(cfg.volume_history_len)
    markets = discover_general_markets(
        engine,
        settings,
        min_liquidity=cfg.min_liquidity,
        price_min=cfg.price_min,
        price_max=cfg.price_max,
    )
    signals: list[Signal] = []

    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions}
    if len(open_positions) >= cfg.max_open_positions:
        log_decision(
            engine.db.data_dir,
            strategy="volspike",
            decision="skip",
            reason="max_open_positions",
            open_positions=len(open_positions),
        )
        return []

    for m in markets:
        if m.slug in open_slugs:
            continue
        _tracker.update(m.condition_id, price=m.yes_price, volume=m.volume_24h)
        spike = _tracker.spike_score(m.condition_id, spike_threshold=cfg.spike_threshold)
        # Cold-start: follow high-volume markets with a clear directional skew.
        if spike is None:
            skew = abs(m.yes_price - 0.5)
            if m.volume_24h < 5_000 or skew < 0.12:
                continue
            spike = cfg.spike_threshold + skew
        if spike is None:
            continue

        # Follow clear favorites so Kelly/sizing stays positive.
        if m.yes_price >= 0.55:
            side = m.yes_outcome
            price = m.yes_price
        elif m.no_price >= 0.55:
            side = m.no_outcome
            price = m.no_price
        else:
            continue

        edge = min(0.12, (spike - cfg.spike_threshold) * 0.02 + cfg.min_edge)
        p = min(max(0.58, price + edge * 0.35), 0.92)
        b = max(0.01, (1 / max(price, 0.01)) - 1)
        q = 1 - p
        f_kelly = max(0, (p * b - q) / b)
        cash = float(engine.get_account().cash)
        size = min(
            max(f_kelly * cfg.kelly_fraction * cash, cfg.position_usd * 0.5),
            cfg.max_position_usd,
            cfg.position_usd,
            cash,
        )
        if size < settings.min_position_usd:
            continue

        reason = f"volspike {spike:.1f}x edge={edge:.3f} @ {price:.3f}"
        signals.append(
            Signal(
                action="buy",
                slug=m.slug,
                outcome=side,
                reason=reason,
                amount_usd=round(size, 2),
                order_type="fak",
                limit_price=None,
                market_condition_id=m.condition_id,
            )
        )
        log_decision(
            engine.db.data_dir,
            strategy="volspike",
            decision="buy",
            reason=reason,
            slug=m.slug,
            action="buy",
            amount_usd=round(size, 2),
            spike=round(spike, 2),
        )
        open_slugs.add(m.slug)
        if len(signals) + len(open_positions) >= cfg.max_open_positions:
            break
        if len(signals) >= max_signals:
            break

    log_decision(
        engine.db.data_dir,
        strategy="volspike",
        decision="scan",
        reason=(
            f"volspike scan: {len(signals)} signals / {len(markets)} markets / "
            f"open={len(open_positions)}"
        ),
        signals=len(signals),
        markets=len(markets),
    )
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
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            bid, _ = best_bid(book)
            if bid is None:
                continue
            current_bid = float(bid)
        except Exception:
            continue

        if current_bid >= entry * (1 + cfg.take_profit_pct):
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    reason=f"volspike_tp bid={current_bid:.3f}",
                    shares=pos.shares,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                )
            )
            continue

        if current_bid <= entry * (1 - cfg.stop_loss_pct):
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    reason=f"volspike_sl bid={current_bid:.3f}",
                    shares=pos.shares,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                )
            )

    return signals
