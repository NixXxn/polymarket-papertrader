"""Penny flip: bid 1¢ on near-term weather YES, then rest a 3¢ sell after fill."""

from __future__ import annotations

import logging
from datetime import date

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import City, Settings
from papertrader.decision_log import log_decision
from papertrader.markets import BucketMarket, best_ask, city_local_today
from papertrader.penny_state import PennyExitStore
from papertrader.signals import Signal

log = logging.getLogger(__name__)


def _city_allowed(city: City, settings: Settings) -> bool:
    allowed = settings.penny.cities
    if allowed:
        return city.slug in allowed
    return "penny" in city.strategies


def _already_open(open_positions: list[Position], slug: str, outcome: str = "yes") -> bool:
    outcome_l = outcome.lower()
    return any(
        p.shares > 0
        and not p.is_resolved
        and p.market_slug == slug
        and p.outcome.lower() == outcome_l
        for p in open_positions
    )


def _log_penny(engine: Engine, **kwargs) -> None:
    log_decision(engine.db.data_dir, strategy="penny", **kwargs)


def analyze_penny_event(
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
    """Place 1¢ YES limit buys on weather buckets resolving within max_days_ahead."""
    cfg = settings.penny
    if not _city_allowed(city, settings):
        return []

    local_today = today or city_local_today(city)
    days_ahead = (event_date - local_today).days
    if days_ahead < cfg.min_days_ahead or days_ahead > cfg.max_days_ahead:
        return []

    if len(open_positions) >= cfg.max_open_positions:
        _log_penny(
            engine,
            decision="skip",
            reason="max_open_positions",
            city=city,
            event_date=str(event_date),
            open_positions=len(open_positions),
        )
        return []

    event_volume = buckets[0].event_volume if buckets else 0.0
    if event_volume < cfg.min_event_volume:
        _log_penny(
            engine,
            decision="skip",
            reason="low_event_volume",
            city=city,
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
        if _already_open(open_positions, slug, "yes"):
            continue
        if any(s.slug == slug and s.action == "buy" for s in signals):
            continue

        try:
            token = bucket.market.get_token_id("yes")
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        ask, ask_size = best_ask(book)
        if ask is None:
            continue
        if ask > cfg.max_ask_to_bid:
            continue
        if ask_size < cfg.min_ask_size:
            continue

        amount = min(cfg.position_usd, cfg.max_position_usd, cash)
        if amount < settings.min_position_usd:
            continue

        fill_now = bool(
            paper_mode
            and cfg.paper_fill_at_limit
            and ask <= cfg.buy_limit + 1e-9
        )
        reason = (
            f"penny buy@{cfg.buy_limit:.2f} ask={ask:.3f} "
            f"d+{days_ahead} → TP@{cfg.sell_limit:.2f} ({bucket.bucket_text})"
        )
        _log_penny(
            engine,
            decision="buy",
            reason=reason,
            city=city,
            event_date=str(event_date),
            slug=slug,
            action="buy",
            amount_usd=amount,
            ask=ask,
            buy_limit=cfg.buy_limit,
            sell_limit=cfg.sell_limit,
        )
        signals.append(
            Signal(
                action="buy",
                slug=slug,
                outcome="yes",
                reason=reason,
                city=city,
                amount_usd=amount,
                event_slug=event_slug,
                order_type="limit",
                limit_price=cfg.buy_limit,
                paper_fill_at_limit=fill_now,
                market_condition_id=bucket.market.condition_id,
            )
        )
        slots -= 1
        per_event_left -= 1
        cash -= amount

    return signals


def penny_exits(
    engine: Engine,
    settings: Settings,
    open_positions: list[Position],
    *,
    exit_store: PennyExitStore | None = None,
) -> list[Signal]:
    """Immediately after a fill, rest a sell limit at sell_limit (default 3¢)."""
    cfg = settings.penny
    store = exit_store or PennyExitStore(engine.db.data_dir)
    store.prune_closed(open_positions)
    signals: list[Signal] = []

    for pos in open_positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue
        if store.take_profit_placed(pos.market_condition_id, pos.outcome):
            continue

        tp = float(cfg.sell_limit)
        reason = (
            f"penny TP limit @{tp:.2f} after entry@{pos.avg_entry_price:.3f} "
            f"({pos.shares:.1f} sh)"
        )
        _log_penny(
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
                penny_take_profit=True,
            )
        )
    return signals
