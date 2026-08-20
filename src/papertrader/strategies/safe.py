from __future__ import annotations

from datetime import date

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.buckets import (
    bucket_width_score,
    forecast_matches_range,
    parse_temperature_range,
    select_best_bucket,
)
from papertrader.config import City, Settings
from papertrader.decision_log import format_skip_summary, log_decision
from papertrader.gfs import effective_edge_threshold, gfs_in_window
from papertrader.markets import (
    BucketMarket,
    best_ask,
    best_bid,
    city_from_market_slug,
    date_from_temp_slug,
)
from papertrader.signals import Signal
from papertrader.sizing import account_cash, scaled_size
from papertrader.weather import WeatherHttp
from papertrader.weather.consensus import get_consensus


def _has_event_position(positions: list[Position], event_slug: str) -> bool:
    prefix = event_slug
    for p in positions:
        if p.shares <= 0:
            continue
        if p.market_slug.startswith(prefix) or prefix in p.market_slug:
            return True
        if event_slug.replace("highest-temperature-in-", "") in (p.market_question or ""):
            return True
    return False


def _log_safe(
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
        strategy="safe",
        decision=decision,
        reason=reason,
        city=city.slug,
        event_date=event_date,
        **extra,
    )


def analyze_safe_event(
    engine: Engine,
    http: WeatherHttp,
    city: City,
    event_date: date,
    buckets: list[BucketMarket],
    settings: Settings,
    open_positions: list[Position],
) -> Signal | None:
    if city.slug not in settings.safe.cities:
        return None
    event_slug = buckets[0].event_slug if buckets else None
    if event_slug and _has_event_position(open_positions, event_slug):
        _log_safe(
            engine,
            decision="skip",
            reason="already_in_event",
            city=city,
            event_date=event_date,
            event_slug=event_slug,
        )
        return None
    if len(open_positions) >= settings.safe.max_open_positions:
        _log_safe(
            engine,
            decision="skip",
            reason="max_open_positions",
            city=city,
            event_date=event_date,
            open_positions=len(open_positions),
        )
        return None

    consensus = get_consensus(http, city, event_date, settings)
    if consensus is None or consensus.confidence == "skip":
        _log_safe(
            engine,
            decision="skip",
            reason="consensus_skip",
            city=city,
            event_date=event_date,
            consensus_temp=getattr(consensus, "temp_f", None),
            confidence=getattr(consensus, "confidence", None),
        )
        return None
    # Profitability filter: only trade when model confidence is very high.
    if consensus.confidence != "very_high":
        _log_safe(
            engine,
            decision="skip",
            reason="confidence_too_low",
            city=city,
            event_date=event_date,
            consensus_temp=consensus.temp_f,
            confidence=consensus.confidence,
        )
        return None

    in_window = gfs_in_window()
    threshold = effective_edge_threshold(
        confidence=consensus.confidence,
        in_window=in_window,
        min_edge=settings.safe.min_edge,
        min_edge_high=settings.safe.min_edge_high,
        min_edge_low=settings.safe.min_edge_low,
    )
    candidates: list[dict] = []
    rejects: dict[str, int] = {}
    for bucket in buckets:
        if not forecast_matches_range(consensus.temp_f, bucket.rng):
            rejects["forecast_mismatch"] = rejects.get("forecast_mismatch", 0) + 1
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
            rejects["ask_size_too_small"] = rejects.get("ask_size_too_small", 0) + 1
            continue
        if ask < settings.safe.min_ask:
            rejects["ask_too_low"] = rejects.get("ask_too_low", 0) + 1
            continue
        if ask > settings.safe.max_ask:
            rejects["ask_too_high"] = rejects.get("ask_too_high", 0) + 1
            continue
        # High win-rate mode: only buy when model is at least as bullish as the market.
        if ask > settings.forecast_confidence:
            rejects["model_below_market"] = rejects.get("model_below_market", 0) + 1
            continue
        # Prefer higher market certainty (closer to resolution favorite).
        edge = ask - settings.safe.min_ask
        if edge < threshold:
            rejects["low_edge"] = rejects.get("low_edge", 0) + 1
            continue
        candidates.append(
            {
                "bucket": bucket,
                "edge_percent": edge,
                "width_score": bucket_width_score(bucket.rng),
                "ask": ask,
            }
        )
    best = select_best_bucket(candidates)
    if best is None:
        _log_safe(
            engine,
            decision="skip",
            reason="no_matching_bucket",
            city=city,
            event_date=event_date,
            consensus_temp=consensus.temp_f,
            confidence=consensus.confidence,
            edge_threshold=threshold,
            buckets_scanned=len(buckets),
            rejects=rejects,
            skip_summary=format_skip_summary(rejects),
        )
        return None
    bucket: BucketMarket = best["bucket"]
    base = settings.safe.position_usd.get(city.slug, city.position_usd)
    safe_starting_balance = settings.safe.starting_balance or settings.starting_balance
    remaining_slots = settings.safe.max_open_positions - len(open_positions)
    size = scaled_size(
        base,
        cash=account_cash(engine, safe_starting_balance),
        starting_balance=safe_starting_balance,
        remaining_slots=remaining_slots,
        min_usd=settings.min_position_usd,
    )
    if size is None:
        _log_safe(
            engine,
            decision="skip",
            reason="insufficient_cash_for_size",
            city=city,
            event_date=event_date,
            bucket=bucket.bucket_text,
        )
        return None
    reason = (
        f"safe consensus {consensus.temp_f:.1f}F ({consensus.confidence}) "
        f"matches {bucket.bucket_text} ask={best['ask']:.3f} "
        f"edge={best['edge_percent']*100:.1f}%"
    )
    _log_safe(
        engine,
        decision="buy",
        reason=reason,
        city=city,
        event_date=event_date,
        slug=bucket.market.slug,
        bucket=bucket.bucket_text,
        action="buy",
        amount_usd=size,
        ask=best["ask"],
        consensus_temp=consensus.temp_f,
        confidence=consensus.confidence,
    )
    return Signal(
        action="buy",
        slug=bucket.market.slug,
        outcome="yes",
        amount_usd=size,
        city=city,
        event_slug=bucket.event_slug,
        reason=reason,
    )


def safe_exits(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    open_positions: list[Position],
    cities: dict[str, City],
) -> list[Signal]:
    """High win-rate exits: hold favorites toward resolution; avoid locking noise losses."""
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
        if bid is None or bid < settings.safe.min_sell_bid:
            continue

        # Hard stop only on collapse vs entry — otherwise hold for resolution ($1).
        # Absolute 0.35 stops wrongly kill mid-ask weather entries (~0.30).
        catastrophic_bid = max(0.08, min(0.25, pos.avg_entry_price * 0.45))
        if bid <= catastrophic_bid:
            reason = f"catastrophic stop bid={bid:.3f}"
            log_decision(
                engine.db.data_dir,
                strategy="safe",
                decision="sell",
                reason=reason,
                city=city.slug,
                event_date=event_date,
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
                    city=city,
                    reason=reason,
                )
            )
            continue

        consensus = get_consensus(http, city, event_date, settings)
        if consensus is None or consensus.confidence == "skip":
            continue
        if forecast_matches_range(consensus.temp_f, rng):
            continue
        # Forecast drifted: only exit if we can leave near breakeven/profit.
        # Do not crystallize large mark-to-market losses on noise.
        if bid < pos.avg_entry_price * 0.98:
            continue
        if consensus.confidence not in ("high", "very_high"):
            continue
        reason = (
            f"forecast shifted to {consensus.temp_f:.1f}F "
            f"({consensus.confidence}); exit near flat bid={bid:.3f}"
        )
        log_decision(
            engine.db.data_dir,
            strategy="safe",
            decision="sell",
            reason=reason,
            city=city.slug,
            event_date=event_date,
            slug=pos.market_slug,
            action="sell",
            shares=pos.shares,
            bid=bid,
            consensus_temp=consensus.temp_f,
        )
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