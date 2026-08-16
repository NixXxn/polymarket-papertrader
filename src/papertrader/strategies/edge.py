from __future__ import annotations

from datetime import date, datetime, timezone

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.buckets import parse_temperature_range
from papertrader.config import City, Settings
from papertrader.markets import (
    BucketMarket,
    best_ask,
    best_bid,
    city_from_market_slug,
    date_from_temp_slug,
)
from papertrader.signals import Signal
from papertrader.sizing import account_cash, budget_scale, scaled_size
from papertrader.weather import WeatherHttp, fetch_metar_observed_high, fetch_openmeteo_ensemble
from papertrader.weather.consensus import get_consensus
from papertrader.weather.impossibility import is_mathematically_impossible
from papertrader.weather.probability import ensemble_p95, p_high_in_bucket


def _open_notional(positions: list[Position]) -> float:
    return sum(p.total_cost for p in positions if p.shares > 0 and not p.is_resolved)


def _already_in(positions: list[Position], condition_id: str) -> bool:
    return any(
        p.market_condition_id == condition_id and p.shares > 0 and not p.is_resolved
        for p in positions
    )


def _quote(
    engine: Engine,
    bucket: BucketMarket,
    settings: Settings,
    event_volume: float,
) -> tuple[float, float, float] | None:
    """Return (ask, bid, ask_size) when the book is liquid enough to scalp."""
    min_vol = settings.edge.min_event_volume
    if event_volume < min_vol:
        return None
    try:
        token = bucket.market.get_token_id("yes")
        book = engine.api.get_order_book(token)
    except Exception:
        return None
    ask, ask_size = best_ask(book)
    bid, _ = best_bid(book)
    if ask is None or bid is None:
        return None
    if ask_size < settings.edge.min_best_ask_size:
        return None
    if ask - bid > settings.edge.max_spread:
        return None
    return ask, bid, ask_size


def _stop_price(entry: float, settings: Settings) -> float:
    """44¢ sell-bias cut, with a relative stop so tight 45¢ entries still have room."""
    relative = round(entry - settings.edge.stop_loss, 4)
    bias = settings.edge.sell_bias
    stop = min(bias, relative)
    if stop >= entry:
        return relative
    return round(stop, 4)


def analyze_edge_event(
    engine: Engine,
    http: WeatherHttp,
    city: City,
    event_date: date,
    buckets: list[BucketMarket],
    settings: Settings,
    open_positions: list[Position],
    today: date | None = None,
) -> list[Signal]:
    today = today or date.today()
    days_ahead = (event_date - today).days
    if len(open_positions) >= settings.edge.max_open_positions:
        return []
    cash = account_cash(engine, settings.starting_balance)
    deployed = _open_notional(open_positions)
    risk_cap = settings.edge.max_notional_at_risk * budget_scale(
        cash + deployed, settings.starting_balance
    )
    if deployed >= risk_cap:
        return []

    event_volume = buckets[0].event_volume if buckets else 0.0
    consensus = get_consensus(http, city, event_date, settings)
    quotes: list[tuple[BucketMarket, float]] = []
    for bucket in buckets:
        quoted = _quote(engine, bucket, settings, event_volume)
        if quoted is None:
            continue
        ask, _bid, _size = quoted
        if not (settings.edge.min_ask <= ask <= settings.edge.max_ask):
            continue
        if _already_in(open_positions, bucket.market.condition_id):
            continue
        quotes.append((bucket, ask))
    # Prefer fills nearest the 48¢ average-entry target.
    quotes.sort(key=lambda item: abs(item[1] - settings.edge.target_ask))
    if not quotes:
        return []

    signals: list[Signal] = []
    for bucket, ask in quotes:
        p_model, src = p_high_in_bucket(
            http, city, event_date, bucket.rng, consensus, days_ahead
        )
        if consensus is not None and consensus.confidence == "skip" and src != "ensemble":
            continue
        if p_model < settings.edge.min_possible:
            continue
        if p_model - ask < settings.edge.min_edge:
            continue
        pending_usd = sum(s.amount_usd or 0 for s in signals)
        remaining_slots = settings.edge.max_open_positions - len(open_positions) - len(signals)
        remaining_risk = risk_cap - deployed - pending_usd
        size = scaled_size(
            settings.edge.position_usd,
            cash=cash - pending_usd,
            starting_balance=settings.starting_balance,
            remaining_slots=remaining_slots,
            min_usd=settings.min_position_usd,
            max_usd=settings.edge.max_position_usd,
            extra_cap=remaining_risk,
            bankroll=cash,
        )
        if size is None:
            break
        signals.append(
            Signal(
                action="buy",
                slug=bucket.market.slug,
                outcome="yes",
                amount_usd=size,
                city=city,
                event_slug=bucket.event_slug,
                reason=(
                    f"grind {bucket.bucket_text} ask={ask:.3f} "
                    f"target={settings.edge.target_ask:.2f} "
                    f"P={p_model:.3f} ({src})"
                ),
            )
        )
        if len(open_positions) + len(signals) >= settings.edge.max_open_positions:
            break
    return signals


def edge_exits(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    open_positions: list[Position],
    cities: dict[str, City],
    now: datetime | None = None,
) -> list[Signal]:
    now = now or datetime.now(timezone.utc)
    signals: list[Signal] = []
    for pos in open_positions:
        if pos.shares <= 0:
            continue
        rng = parse_temperature_range(pos.market_question) or parse_temperature_range(pos.market_slug)
        city = city_from_market_slug(pos.market_slug, cities)
        event_date = date_from_temp_slug(pos.market_slug)
        if rng is None or city is None or event_date is None:
            continue
        try:
            token = engine.api.get_market(pos.market_slug).get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        bid, _ = best_bid(book)
        if bid is None:
            continue

        observed = fetch_metar_observed_high(http, city, event_date, now)
        members = fetch_openmeteo_ensemble(http, city, event_date)
        p95 = ensemble_p95(members)
        dead, why = is_mathematically_impossible(
            rng,
            city=city,
            event_date=event_date,
            settings=settings.edge,
            observed_high_f=observed,
            ensemble_p95_f=p95,
            now=now,
        )
        reason: str | None = None
        if dead and bid >= settings.edge.min_sell_bid:
            reason = f"emergency brake: {why}"
        elif pos.avg_entry_price < settings.edge.min_ask and bid >= settings.edge.min_sell_bid:
            reason = (
                f"grind rotation: entry {pos.avg_entry_price:.3f} "
                f"below scalp band {settings.edge.min_ask:.2f}"
            )
        elif bid >= pos.avg_entry_price + settings.edge.take_profit:
            reason = (
                f"take profit bid={bid:.3f} "
                f"entry={pos.avg_entry_price:.3f} +{settings.edge.take_profit:.2f}"
            )
        elif bid <= _stop_price(pos.avg_entry_price, settings):
            reason = (
                f"sell bias bid={bid:.3f} "
                f"stop={_stop_price(pos.avg_entry_price, settings):.3f} "
                f"entry={pos.avg_entry_price:.3f}"
            )
        if reason is None:
            continue
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                city=city,
                reason=reason,
            )
        )
    return signals
