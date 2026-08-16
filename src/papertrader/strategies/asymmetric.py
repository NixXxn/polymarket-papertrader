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
from papertrader.sizing import account_cash, scaled_size
from papertrader.weather import WeatherHttp, fetch_metar_observed_high, fetch_openmeteo_ensemble
from papertrader.weather.ensemble import fetch_combined_ensemble, tail_bucket_probability
from papertrader.weather.impossibility import is_mathematically_impossible
from papertrader.weather.probability import ensemble_p95


def _already_in(positions: list[Position], condition_id: str) -> bool:
    return any(
        p.market_condition_id == condition_id and p.shares > 0 and not p.is_resolved
        for p in positions
    )


def _city_allowed(city: City, settings: Settings) -> bool:
    allowed = settings.asymmetric.cities
    if allowed:
        return city.slug in allowed
    return "asymmetric" in city.strategies


def analyze_asymmetric_event(
    engine: Engine,
    http: WeatherHttp,
    city: City,
    event_date: date,
    buckets: list[BucketMarket],
    settings: Settings,
    open_positions: list[Position],
    today: date | None = None,
) -> Signal | None:
    """Tail-risk arb: buy cheap YES when GFS+ECMWF ensemble >> market price."""
    if not _city_allowed(city, settings):
        return None
    today = today or date.today()
    days_ahead = (event_date - today).days
    if days_ahead < 0:
        return None
    if len(open_positions) >= settings.asymmetric.max_open_positions:
        return None

    event_volume = buckets[0].event_volume if buckets else 0.0
    if event_volume < settings.asymmetric.min_event_volume:
        return None

    ensemble = fetch_combined_ensemble(http, city, event_date)
    if len(ensemble.members_f) < settings.asymmetric.min_ensemble_members:
        return None

    cfg = settings.asymmetric
    candidates: list[dict] = []
    for bucket in buckets:
        if _already_in(open_positions, bucket.market.condition_id):
            continue
        try:
            token = bucket.market.get_token_id("yes")
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        ask, ask_size = best_ask(book)
        if ask is None or ask_size < settings.min_best_ask_size:
            continue
        if not (cfg.min_ask <= ask <= cfg.max_ask):
            continue

        p_model, src = tail_bucket_probability(ensemble, bucket.rng)
        if p_model < cfg.min_model_prob:
            continue
        if ask <= 0 or p_model / ask < cfg.min_prob_ratio:
            continue
        if p_model - ask < cfg.min_edge:
            continue
        candidates.append(
            {
                "bucket": bucket,
                "ask": ask,
                "p_model": p_model,
                "src": src,
                "edge": p_model - ask,
                "ratio": p_model / ask,
            }
        )

    if not candidates:
        return None
    # Prefer the largest model-vs-market gap on the cheapest asks.
    best = max(candidates, key=lambda c: (c["ratio"], c["edge"]))
    bucket: BucketMarket = best["bucket"]
    remaining_slots = settings.asymmetric.max_open_positions - len(open_positions)
    size = scaled_size(
        cfg.position_usd,
        cash=account_cash(engine, settings.starting_balance),
        starting_balance=settings.starting_balance,
        remaining_slots=remaining_slots,
        min_usd=settings.min_position_usd,
        max_usd=cfg.max_position_usd,
    )
    if size is None:
        return None
    return Signal(
        action="buy",
        slug=bucket.market.slug,
        outcome="yes",
        amount_usd=size,
        city=city,
        event_slug=bucket.event_slug,
        reason=(
            f"tail {bucket.bucket_text} ask={best['ask']:.3f} "
            f"P={best['p_model']:.2f} ({best['src']}) "
            f"ratio={best['ratio']:.1f}x d+{days_ahead}"
        ),
    )


def asymmetric_exits(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    open_positions: list[Position],
    cities: dict[str, City],
    now: datetime | None = None,
) -> list[Signal]:
    """Hedge before resolution: take profit when forecast goes mainstream (~35¢)."""
    now = now or datetime.now(timezone.utc)
    cfg = settings.asymmetric
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
        if bid is None or bid < cfg.min_sell_bid:
            continue

        observed = fetch_metar_observed_high(http, city, event_date, now)
        members = fetch_openmeteo_ensemble(http, city, event_date)
        p95 = ensemble_p95(members)
        dead, why = is_mathematically_impossible(
            rng,
            city=city,
            event_date=event_date,
            settings=settings.asymmetric,
            observed_high_f=observed,
            ensemble_p95_f=p95,
            now=now,
        )
        reason: str | None = None
        if dead:
            reason = f"tail brake: {why}"
        elif bid >= cfg.take_profit_bid:
            reason = (
                f"forecast hedge bid={bid:.3f} "
                f">= take_profit {cfg.take_profit_bid:.2f} "
                f"entry={pos.avg_entry_price:.3f}"
            )
        elif bid <= cfg.stop_loss_bid and pos.avg_entry_price <= cfg.max_ask:
            reason = f"tail stop bid={bid:.3f} entry={pos.avg_entry_price:.3f}"
        else:
            ensemble = fetch_combined_ensemble(http, city, event_date)
            p_model, _ = tail_bucket_probability(ensemble, rng)
            if p_model < cfg.exit_model_prob and bid >= cfg.min_sell_bid:
                reason = (
                    f"model faded P={p_model:.2f} < {cfg.exit_model_prob:.2f} "
                    f"bid={bid:.3f}"
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
