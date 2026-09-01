"""ObieWeather ladder: 3–4 cheap YES legs across the forecast range per event."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.buckets import TempRange, bucket_bounds_f
from papertrader.config import City, ObieWeatherSettings, Settings
from papertrader.decision_log import format_skip_summary, log_decision
from papertrader.markets import (
    BucketMarket,
    best_ask,
    city_local_today,
    event_slug_from_market_slug,
)
from papertrader.quant.shadow_ledger import ShadowLedger
from papertrader.signals import QuantMeta, Signal
from papertrader.sizing import account_cash
from papertrader.weather import WeatherHttp
from papertrader.weather.ensemble import fetch_combined_ensemble, tail_bucket_probability


@dataclass(frozen=True)
class _LadderLeg:
    bucket: BucketMarket
    p_model: float
    src: str
    ask: float
    sort_key: float
    limit_price: float


def _bucket_sort_key(rng: TempRange) -> float:
    lo, hi = bucket_bounds_f(rng)
    if lo is not None and hi is not None:
        return (lo + hi) / 2.0
    if lo is not None:
        return lo + 5.0
    if hi is not None:
        return hi - 5.0
    return 0.0


def _city_allowed(city: City, settings: Settings) -> bool:
    allowed = settings.obieweather.cities
    if allowed:
        return city.slug in allowed
    return "obieweather" in city.strategies


def _event_key(slug: str) -> str:
    return event_slug_from_market_slug(slug)


def _already_in_event(positions: list[Position], event_slug: str) -> bool:
    prefix = event_slug
    for pos in positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue
        if pos.market_slug.startswith(prefix) or prefix in pos.market_slug:
            return True
    return False


def _log_obie(
    engine: Engine,
    *,
    decision: str,
    reason: str,
    city: City,
    event_date: date,
    **extra,
) -> None:
    log_decision(
        engine.db.data_dir,
        strategy="obieweather",
        decision=decision,
        reason=reason,
        city=city.slug,
        event_date=event_date,
        **extra,
    )


def _select_ladder_window(
    scored: list[_LadderLeg],
    cfg: ObieWeatherSettings,
) -> list[_LadderLeg] | None:
    """Best contiguous 3–4 leg window: asks ≤40¢, limit-price sum ≤60¢."""
    if len(scored) < cfg.min_yes_bets_per_event:
        return None

    ordered = sorted(scored, key=lambda row: row.sort_key)
    best: list[_LadderLeg] | None = None
    best_prob = -1.0

    for size in range(cfg.max_yes_bets_per_event, cfg.min_yes_bets_per_event - 1, -1):
        if size > len(ordered):
            continue
        for start in range(0, len(ordered) - size + 1):
            window = ordered[start : start + size]
            if any(leg.ask > cfg.max_yes_ask for leg in window):
                continue
            limit_sum = sum(leg.limit_price for leg in window)
            if limit_sum > cfg.max_ladder_price_sum + 1e-9:
                continue
            prob_sum = sum(leg.p_model for leg in window)
            if prob_sum < cfg.min_ensemble_prob_sum:
                continue
            if prob_sum > best_prob:
                best_prob = prob_sum
                best = window
    return best


def analyze_obieweather_event(
    engine: Engine,
    http: WeatherHttp,
    city: City,
    event_date: date,
    buckets: list[BucketMarket],
    settings: Settings,
    open_positions: list[Position],
    today: date | None = None,
) -> list[Signal]:
    """Buy 3–4 cheap YES buckets spanning the forecast range (one win covers losses)."""
    cfg = settings.obieweather
    if not _city_allowed(city, settings):
        return []

    local_today = today or city_local_today(city)
    days_ahead = (event_date - local_today).days
    if days_ahead < cfg.min_days_ahead or days_ahead > cfg.max_days_ahead:
        return []

    if len(open_positions) >= cfg.max_open_positions:
        _log_obie(
            engine,
            decision="skip",
            reason="max_open_positions",
            city=city,
            event_date=event_date,
            open_positions=len(open_positions),
        )
        return []

    event_slug = buckets[0].event_slug if buckets else None
    if event_slug and _already_in_event(open_positions, event_slug):
        _log_obie(
            engine,
            decision="skip",
            reason="already_in_event",
            city=city,
            event_date=event_date,
            event_slug=event_slug,
        )
        return []

    event_volume = buckets[0].event_volume if buckets else 0.0
    if event_volume < cfg.min_event_volume:
        _log_obie(
            engine,
            decision="skip",
            reason="low_event_volume",
            city=city,
            event_date=event_date,
            volume=event_volume,
        )
        return []

    event_positions = sum(
        1
        for p in open_positions
        if p.shares > 0
        and not p.is_resolved
        and event_slug
        and _event_key(p.market_slug) == event_slug
    )
    if event_positions >= cfg.max_open_per_event:
        return []

    ensemble = fetch_combined_ensemble(http, city, event_date)
    if len(ensemble.members_f) < cfg.min_ensemble_members:
        _log_obie(
            engine,
            decision="skip",
            reason="thin_ensemble",
            city=city,
            event_date=event_date,
            members=len(ensemble.members_f),
        )
        return []

    rejects: dict[str, int] = {}
    scored: list[_LadderLeg] = []
    for bucket in buckets:
        if bucket.rng is None:
            rejects["no_range"] = rejects.get("no_range", 0) + 1
            continue
        p_model, src = tail_bucket_probability(ensemble, bucket.rng)
        if p_model < cfg.min_model_prob:
            rejects["low_model_prob"] = rejects.get("low_model_prob", 0) + 1
            continue
        try:
            token = bucket.market.get_token_id("yes")
            book = engine.api.get_order_book(token)
        except Exception:
            rejects["order_book_error"] = rejects.get("order_book_error", 0) + 1
            continue
        ask, ask_size = best_ask(book)
        if ask is None:
            rejects["no_ask"] = rejects.get("no_ask", 0) + 1
            continue
        if ask_size < settings.min_best_ask_size:
            rejects["ask_size"] = rejects.get("ask_size", 0) + 1
            continue
        if ask < cfg.min_yes_ask:
            rejects["ask_too_low"] = rejects.get("ask_too_low", 0) + 1
            continue
        if ask > cfg.max_yes_ask:
            rejects["ask_too_high"] = rejects.get("ask_too_high", 0) + 1
            continue
        if _already_in(open_positions, bucket.market.condition_id):
            rejects["already_in_bucket"] = rejects.get("already_in_bucket", 0) + 1
            continue

        if cfg.strict_limit:
            limit_price = round(min(ask, cfg.max_yes_ask) - cfg.maker_tick, 4)
            limit_price = max(limit_price, cfg.min_yes_ask)
        else:
            limit_price = round(min(ask, cfg.max_yes_ask), 4)

        scored.append(
            _LadderLeg(
                bucket=bucket,
                p_model=p_model,
                src=src,
                ask=ask,
                sort_key=_bucket_sort_key(bucket.rng),
                limit_price=limit_price,
            )
        )

    window = _select_ladder_window(scored, cfg)
    if not window:
        _log_obie(
            engine,
            decision="skip",
            reason="no_ladder_window",
            city=city,
            event_date=event_date,
            buckets_scanned=len(buckets),
            scored=len(scored),
            rejects=rejects,
            skip_summary=format_skip_summary(rejects),
            days_ahead=days_ahead,
        )
        return []

    bankroll = account_cash(engine, cfg.starting_balance or settings.starting_balance)
    event_budget = min(
        cfg.max_event_usd,
        bankroll * cfg.max_event_fraction,
        bankroll,
    )
    per_leg = round(event_budget / len(window), 2)
    if per_leg < settings.min_position_usd:
        _log_obie(
            engine,
            decision="skip",
            reason="stake_below_min",
            city=city,
            event_date=event_date,
            per_leg=per_leg,
            event_budget=event_budget,
        )
        return []

    shadow = ShadowLedger(engine.db.data_dir)
    signals: list[Signal] = []
    ladder_prices = [leg.limit_price for leg in window]
    for leg in window:
        stake = per_leg
        reason = (
            f"ladder {leg.bucket.bucket_text} YES limit@{leg.limit_price:.3f} "
            f"P={leg.p_model:.2f} ask={leg.ask:.3f} ${stake:.2f} "
            f"({leg.src}) legs={len(window)} sum_ask={sum(ladder_prices):.2f} d+{days_ahead}"
        )
        _log_obie(
            engine,
            decision="buy",
            reason=reason,
            city=city,
            event_date=event_date,
            slug=leg.bucket.market.slug,
            bucket=leg.bucket.bucket_text,
            yes_ask=leg.ask,
            limit_price=leg.limit_price,
            p_model=leg.p_model,
            stake_usd=stake,
            ladder_legs=len(window),
            ladder_sum_limit=round(sum(ladder_prices), 4),
            days_ahead=days_ahead,
        )
        shadow.log_entry(
            strategy="obieweather",
            slug=leg.bucket.market.slug,
            action="buy",
            share_price=leg.limit_price,
            p=leg.p_model,
            sigma=0.0,
            f_star=leg.p_model - leg.limit_price,
            stake_usd=stake,
            extra={
                "outcome": "yes",
                "ladder_legs": len(window),
                "ladder_sum_limit": round(sum(ladder_prices), 4),
            },
        )
        signals.append(
            Signal(
                action="buy",
                slug=leg.bucket.market.slug,
                outcome="yes",
                amount_usd=stake,
                city=city,
                event_slug=leg.bucket.event_slug,
                order_type="limit" if cfg.strict_limit else "fak",
                limit_price=leg.limit_price,
                quant=QuantMeta(
                    p=leg.p_model,
                    sigma=0.0,
                    f_star=leg.p_model - leg.limit_price,
                    kelly_fraction=0.0,
                    source=leg.src,
                ),
                reason=reason,
                market_condition_id=leg.bucket.market.condition_id,
            )
        )
    return signals


def _already_in(positions: list[Position], condition_id: str) -> bool:
    return any(
        p.market_condition_id == condition_id and p.shares > 0 and not p.is_resolved
        for p in positions
    )


def obieweather_exits(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    open_positions: list[Position],
    cities: dict[str, City],
) -> list[Signal]:
    """Hold ladder legs to resolution — one winning bucket covers the rest."""
    _ = (engine, http, settings, open_positions, cities)
    return []
