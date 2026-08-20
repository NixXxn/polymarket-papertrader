from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import City, Settings
from papertrader.decision_log import log_decision
from papertrader.markets import BucketMarket, best_ask, best_bid, discover_events
from papertrader.momentum_state import MomentumExitStore
from papertrader.signals import Signal
from papertrader.sizing import account_cash, scaled_size
from papertrader.weather_ws_client import MarketTick


@dataclass(frozen=True)
class TokenWatch:
    token_id: str
    event_slug: str
    event_date: date
    city: City
    bucket: BucketMarket
    label: str


def _log_momentum(
    engine: Engine,
    *,
    decision: str,
    reason: str,
    city: City | None = None,
    event_date: date | None = None,
    **extra,
) -> None:
    log_decision(
        engine.db.data_dir,
        strategy="momentum",
        decision=decision,
        reason=reason,
        city=city.slug if city else None,
        event_date=event_date,
        **extra,
    )


def build_token_watches(engine: Engine, settings: Settings) -> list[TokenWatch]:
    """Resolve YES token ids for all temperature buckets in configured cities."""
    cfg = settings.momentum
    cities = [settings.cities[s] for s in cfg.cities if s in settings.cities]
    watches: list[TokenWatch] = []
    for event_slug, event_date, city, buckets, volume in discover_events(
        engine, cities, settings
    ):
        if volume < cfg.min_event_volume:
            continue
        for bucket in buckets:
            try:
                token_id = str(bucket.market.get_token_id("yes"))
            except Exception:
                continue
            watches.append(
                TokenWatch(
                    token_id=token_id,
                    event_slug=event_slug,
                    event_date=event_date,
                    city=city,
                    bucket=bucket,
                    label=bucket.bucket_text,
                )
            )
    return watches


def _open_positions(positions: list[Position]) -> list[Position]:
    return [p for p in positions if p.shares > 0 and not p.is_resolved]


def _in_position(positions: list[Position], condition_id: str, outcome: str) -> bool:
    return any(
        p.market_condition_id == condition_id and p.outcome == outcome and p.shares > 0
        for p in positions
    )


def _position_in_event(positions: list[Position], event_slug: str) -> bool:
    prefix = event_slug.rstrip("/")
    return any(
        p.shares > 0
        and not p.is_resolved
        and (p.market_slug == prefix or p.market_slug.startswith(prefix + "-"))
        for p in positions
    )


def _trigger_price(tick: MarketTick) -> float:
    ask = tick.best_ask or 0.0
    last = tick.last_price or 0.0
    return max(ask, last)


def analyze_momentum_entry(
    engine: Engine,
    watch: TokenWatch,
    tick: MarketTick,
    settings: Settings,
    open_positions: list[Position],
) -> Signal | None:
    """Buy YES when a bucket crosses the momentum entry threshold."""
    cfg = settings.momentum
    if cfg.mode.upper() == "SPECIFIC" and watch.token_id != cfg.specific_token_id:
        return None

    positions = _open_positions(open_positions)
    if len(positions) >= cfg.max_open_positions:
        _log_momentum(
            engine,
            decision="skip",
            reason="max_open_positions",
            city=watch.city,
            event_date=watch.event_date,
            slug=watch.bucket.market.slug,
            open_positions=len(positions),
        )
        return None
    if _position_in_event(positions, watch.event_slug):
        _log_momentum(
            engine,
            decision="skip",
            reason="already_in_event",
            city=watch.city,
            event_date=watch.event_date,
            slug=watch.bucket.market.slug,
            event_slug=watch.event_slug,
        )
        return None
    if _in_position(positions, watch.bucket.market.condition_id, "yes"):
        return None

    trigger = _trigger_price(tick)
    if trigger < cfg.entry_trigger_price:
        return None

    fill_price = min(
        round(max(tick.best_ask or trigger, tick.last_price or trigger) + cfg.entry_price_buffer, 2),
        0.99,
    )
    if cfg.use_share_sizing:
        stake = round(cfg.order_size_shares * fill_price, 2)
        if stake < settings.min_position_usd:
            return None
    else:
        remaining = cfg.max_open_positions - len(positions)
        stake = scaled_size(
            cfg.position_usd,
            cash=account_cash(engine, settings.starting_balance),
            starting_balance=settings.starting_balance,
            remaining_slots=remaining,
            min_usd=settings.min_position_usd,
            max_usd=cfg.max_position_usd,
        )
        if stake is None:
            return None

    reason = (
        f"momentum entry {watch.label} trigger={trigger:.3f} "
        f"taker@{fill_price:.3f} — {watch.event_slug}"
    )
    _log_momentum(
        engine,
        decision="buy",
        reason=reason,
        city=watch.city,
        event_date=watch.event_date,
        slug=watch.bucket.market.slug,
        bucket=watch.label,
        trigger_price=trigger,
        fill_price=fill_price,
        stake_usd=stake,
    )
    return Signal(
        action="buy",
        slug=watch.bucket.market.slug,
        outcome="yes",
        amount_usd=stake,
        city=watch.city,
        event_slug=watch.event_slug,
        # Cross the ask via FAK so entries actually fill (GTC at mid often rests).
        order_type="fak",
        limit_price=None,
        market_condition_id=watch.bucket.market.condition_id,
        reason=reason,
    )


def momentum_exits(
    engine: Engine,
    watch: TokenWatch,
    tick: MarketTick,
    settings: Settings,
    open_positions: list[Position],
    *,
    exit_store: MomentumExitStore | None = None,
) -> list[Signal]:
    """Take profit or stop loss for an open momentum position on this bucket."""
    cfg = settings.momentum
    store = exit_store or MomentumExitStore(engine.db.data_dir)
    signals: list[Signal] = []

    pos = next(
        (
            p
            for p in open_positions
            if p.market_condition_id == watch.bucket.market.condition_id
            and p.outcome == "yes"
            and p.shares > 0
            and not p.is_resolved
        ),
        None,
    )
    if pos is None:
        return signals

    bid = tick.best_bid
    if bid is None:
        return signals

    if cfg.take_profit_price and bid >= cfg.take_profit_price:
        if store.take_profit_placed(pos.market_condition_id, pos.outcome):
            return signals
        reason = (
            f"momentum TP @ {cfg.take_profit_price:.3f} "
            f"(bid={bid:.3f} entry={pos.avg_entry_price:.3f})"
        )
        _log_momentum(
            engine,
            decision="sell",
            reason=reason,
            city=watch.city,
            event_date=watch.event_date,
            slug=pos.market_slug,
            action="sell",
            shares=pos.shares,
            bid=bid,
            take_profit_price=cfg.take_profit_price,
        )
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                city=watch.city,
                reason=reason,
                order_type="limit",
                limit_price=cfg.take_profit_price,
                market_condition_id=pos.market_condition_id,
                momentum_take_profit=True,
            )
        )
        return signals

    if bid <= cfg.stop_loss_price:
        exit_price = max(round(bid - cfg.exit_slippage_buffer, 2), 0.01)
        reason = (
            f"momentum SL bid={bid:.3f} <= {cfg.stop_loss_price:.3f} "
            f"exit@{exit_price:.3f}"
        )
        store.clear(pos.market_condition_id, pos.outcome)
        _log_momentum(
            engine,
            decision="sell",
            reason=reason,
            city=watch.city,
            event_date=watch.event_date,
            slug=pos.market_slug,
            action="sell",
            shares=pos.shares,
            bid=bid,
        )
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                city=watch.city,
                reason=reason,
                order_type="limit",
                limit_price=exit_price,
                market_condition_id=pos.market_condition_id,
            )
        )
    return signals


def tick_from_order_book(token_id: str, book) -> MarketTick:
    bid, _ = best_bid(book)
    ask, _ = best_ask(book)
    return MarketTick(
        token_id=token_id,
        best_bid=bid,
        best_ask=ask,
        last_price=ask,
    )
