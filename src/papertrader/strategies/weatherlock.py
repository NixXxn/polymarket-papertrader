"""Weatherlock: buy weather NO with real edge, rest TP at entry+offset (cap sell_limit)."""

from __future__ import annotations

import logging
from datetime import date

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import City, Settings
from papertrader.decision_log import log_decision
from papertrader.markets import BucketMarket, best_ask, best_bid, city_local_today
from papertrader.signals import Signal
from papertrader.weatherlock_state import WeatherlockExitStore

log = logging.getLogger(__name__)


def _take_profit_price(entry: float, offset: float, sell_limit: float) -> float:
    return round(min(float(entry) + float(offset), float(sell_limit)), 4)


def _city_allowed(city: City, settings: Settings) -> bool:
    allowed = settings.weatherlock.cities
    if allowed:
        return city.slug in allowed
    return "weatherlock" in city.strategies


def _already_open(open_positions: list[Position], slug: str, outcome: str = "no") -> bool:
    outcome_l = outcome.lower()
    return any(
        p.shares > 0
        and not p.is_resolved
        and p.market_slug == slug
        and p.outcome.lower() == outcome_l
        for p in open_positions
    )


def _log_weatherlock(engine: Engine, **kwargs) -> None:
    log_decision(engine.db.data_dir, strategy="weatherlock", **kwargs)


def analyze_weatherlock_event(
    engine: Engine,
    city: City,
    event_date: date,
    buckets: list[BucketMarket],
    settings: Settings,
    open_positions: list[Position],
    today: date | None = None,
    *,
    paper_mode: bool = False,
) -> list[Signal]:
    """Buy NO when ask is in [buy_min, buy_max]; rest sell_limit take-profit after fill."""
    cfg = settings.weatherlock
    if not _city_allowed(city, settings):
        return []

    local_today = today or city_local_today(city)
    days_ahead = (event_date - local_today).days
    if days_ahead < cfg.min_days_ahead or days_ahead > cfg.max_days_ahead:
        return []

    if len(open_positions) >= cfg.max_open_positions:
        _log_weatherlock(
            engine,
            decision="skip",
            reason="max_open_positions",
            city=city.slug,
            event_date=str(event_date),
            open_positions=len(open_positions),
        )
        return []

    event_volume = buckets[0].event_volume if buckets else 0.0
    if event_volume < cfg.min_event_volume:
        _log_weatherlock(
            engine,
            decision="skip",
            reason="low_event_volume",
            city=city.slug,
            event_date=str(event_date),
            volume=event_volume,
        )
        return []

    event_slug = buckets[0].event_slug if buckets else None
    event_open = sum(
        1
        for p in open_positions
        if p.shares > 0
        and not p.is_resolved
        and event_slug
        and (p.market_slug.startswith(event_slug) or event_slug in p.market_slug)
    )
    if event_open >= cfg.max_open_per_event:
        return []

    cash = float(engine.db.get_account().cash)
    signals: list[Signal] = []
    slots = cfg.max_open_positions - len(open_positions)
    per_event_left = cfg.max_open_per_event - event_open

    for bucket in buckets:
        if slots <= 0 or per_event_left <= 0:
            break
        if cash < max(cfg.position_usd, settings.min_position_usd):
            break
        slug = bucket.market.slug
        if _already_open(open_positions, slug, "no"):
            continue
        if any(s.slug == slug and s.action == "buy" for s in signals):
            continue

        try:
            token = bucket.market.get_token_id("no")
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        ask, ask_size = best_ask(book)
        if ask is None:
            continue
        if ask < cfg.buy_min - 1e-9 or ask > cfg.buy_max + 1e-9:
            continue
        if ask_size < cfg.min_ask_size:
            continue

        amount = min(cfg.position_usd, cfg.max_position_usd, cash)
        # Size down near the top of the band — less edge to sell_limit, same wipe risk.
        band = max(1e-6, float(cfg.buy_max) - float(cfg.buy_min))
        edge_frac = max(0.0, min(1.0, (float(cfg.buy_max) - float(ask)) / band))
        amount = round(amount * (0.55 + 0.45 * edge_frac), 2)
        if amount < settings.min_position_usd:
            continue

        limit_px = min(float(ask), float(cfg.buy_max))
        tp_px = _take_profit_price(limit_px, cfg.take_profit_offset, cfg.sell_limit)
        if tp_px <= limit_px + 1e-12:
            continue
        fill_now = bool(
            paper_mode
            and cfg.paper_fill_at_limit
            and cfg.buy_min - 1e-9 <= ask <= cfg.buy_max + 1e-9
        )
        reason = (
            f"weatherlock NO buy@{limit_px:.2f} ask={ask:.3f} "
            f"d+{days_ahead} → TP@{tp_px:.2f} (+{cfg.take_profit_offset:.2f}) "
            f"({bucket.bucket_text})"
        )
        _log_weatherlock(
            engine,
            decision="buy",
            reason=reason,
            city=city.slug,
            event_date=str(event_date),
            slug=slug,
            action="buy",
            amount_usd=amount,
            ask=ask,
            buy_min=cfg.buy_min,
            buy_max=cfg.buy_max,
            sell_limit=cfg.sell_limit,
            take_profit_price=tp_px,
            take_profit_offset=cfg.take_profit_offset,
        )
        signals.append(
            Signal(
                action="buy",
                slug=slug,
                outcome="no",
                reason=reason,
                city=city,
                amount_usd=amount,
                event_slug=event_slug,
                order_type="limit",
                limit_price=limit_px,
                paper_fill_at_limit=fill_now,
                market_condition_id=bucket.market.condition_id,
            )
        )
        slots -= 1
        per_event_left -= 1
        cash -= amount

    return signals


def weatherlock_exits(
    engine: Engine,
    settings: Settings,
    open_positions: list[Position],
    *,
    exit_store: WeatherlockExitStore | None = None,
) -> list[Signal]:
    """Rest TP after fill; FAK-stop if bid collapses before resolution wipe."""
    cfg = settings.weatherlock
    store = exit_store or WeatherlockExitStore(engine.db.data_dir)
    store.prune_closed(open_positions)
    signals: list[Signal] = []

    for pos in open_positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue

        if cfg.stop_bid is not None:
            try:
                market = engine.api.get_market(pos.market_slug)
                token = market.get_token_id(pos.outcome)
                book = engine.api.get_order_book(token)
                bid, _ = best_bid(book)
            except Exception:
                bid = None
            if bid is not None and float(bid) <= float(cfg.stop_bid):
                exit_px = max(round(float(bid) - 0.01, 2), 0.01)
                reason = (
                    f"weatherlock SL bid={bid:.3f} <= {cfg.stop_bid:.3f} "
                    f"exit@{exit_px:.3f} (entry={pos.avg_entry_price:.3f})"
                )
                store.clear(pos.market_condition_id, pos.outcome)
                _log_weatherlock(
                    engine,
                    decision="sell",
                    reason=reason,
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    action="sell",
                    shares=pos.shares,
                    bid=bid,
                    stop_bid=cfg.stop_bid,
                    entry=pos.avg_entry_price,
                )
                signals.append(
                    Signal(
                        action="sell",
                        slug=pos.market_slug,
                        outcome=pos.outcome,
                        shares=pos.shares,
                        reason=reason,
                        order_type="fak",
                        limit_price=None,
                        market_condition_id=pos.market_condition_id,
                        event_slug=pos.market_slug,
                    )
                )
                continue

        if store.take_profit_placed(pos.market_condition_id, pos.outcome):
            continue

        tp = _take_profit_price(
            pos.avg_entry_price, cfg.take_profit_offset, cfg.sell_limit
        )
        if tp <= float(pos.avg_entry_price) + 1e-12:
            continue
        reason = (
            f"weatherlock TP limit @{tp:.2f} after entry@{pos.avg_entry_price:.3f} "
            f"(+{cfg.take_profit_offset:.2f}, {pos.shares:.1f} sh)"
        )
        _log_weatherlock(
            engine,
            decision="sell",
            reason=reason,
            slug=pos.market_slug,
            outcome=pos.outcome,
            action="sell",
            shares=pos.shares,
            take_profit_price=tp,
            entry=pos.avg_entry_price,
        )
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                reason=reason,
                order_type="limit",
                limit_price=tp,
                market_condition_id=pos.market_condition_id,
                event_slug=pos.market_slug,
                weatherlock_take_profit=True,
            )
        )
    return signals
