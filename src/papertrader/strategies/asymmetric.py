from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.buckets import parse_temperature_range
from papertrader.config import City, Settings
from papertrader.decision_log import classify_ask_reject, format_skip_summary, log_decision
from papertrader.markets import (
    BucketMarket,
    best_ask,
    best_bid,
    city_from_market_slug,
    city_local_today,
    date_from_temp_slug,
)
from papertrader.quant.kelly import KellySizingEngine
from papertrader.quant.shadow_ledger import ShadowLedger
from papertrader.quant.variance import VarianceCalculator
from papertrader.signals import QuantMeta, Signal
from papertrader.sizing import account_cash
from papertrader.weather import WeatherHttp, fetch_metar_observed_high
from papertrader.weather.ensemble import fetch_combined_ensemble, tail_bucket_probability
from papertrader.weather.impossibility import is_mathematically_impossible
from papertrader.quant.monitor import monitor_config_from_settings, monitor_exits
from papertrader.quant.position_state import PositionExitStore
from papertrader.weather.probability import ensemble_p95

_KELLY = KellySizingEngine()
_VARIANCE = VarianceCalculator()


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


def _log_asym(
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
        strategy="asymmetric",
        decision=decision,
        reason=reason,
        city=city.slug,
        event_date=event_date,
        **extra,
    )


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
    local_today = today or city_local_today(city)
    days_ahead = (event_date - local_today).days
    if days_ahead < 0:
        _log_asym(
            engine,
            decision="skip",
            reason="event_date_in_past",
            city=city,
            event_date=event_date,
            days_ahead=days_ahead,
        )
        return None
    if len(open_positions) >= settings.asymmetric.max_open_positions:
        _log_asym(
            engine,
            decision="skip",
            reason="max_open_positions",
            city=city,
            event_date=event_date,
            open_positions=len(open_positions),
            max_open=settings.asymmetric.max_open_positions,
        )
        return None

    event_volume = buckets[0].event_volume if buckets else 0.0
    if event_volume < settings.asymmetric.min_event_volume:
        _log_asym(
            engine,
            decision="skip",
            reason="low_event_volume",
            city=city,
            event_date=event_date,
            event_volume=event_volume,
            min_volume=settings.asymmetric.min_event_volume,
        )
        return None

    ensemble = fetch_combined_ensemble(http, city, event_date)
    if len(ensemble.members_f) < settings.asymmetric.min_ensemble_members:
        skip_reason = "ensemble_unavailable" if ensemble.api_error else "thin_ensemble"
        _log_asym(
            engine,
            decision="skip",
            reason=skip_reason,
            city=city,
            event_date=event_date,
            ensemble_members=len(ensemble.members_f),
            ensemble_source=ensemble.source,
            min_members=settings.asymmetric.min_ensemble_members,
            api_error=ensemble.api_error,
        )
        return None

    cfg = settings.asymmetric
    candidates: list[dict] = []
    rejects: dict[str, int] = defaultdict(int)
    best_near: dict | None = None
    notable_buckets: list[dict] = []

    def _near_miss(
        bucket: BucketMarket,
        ask: float | None,
        ask_size: float,
        p_model: float,
        fail: str,
    ) -> None:
        nonlocal best_near
        ratio = p_model / ask if ask and ask > 0 else 0.0
        row = {
            "bucket": bucket.bucket_text,
            "ask": round(ask, 4) if ask is not None else None,
            "size": round(ask_size, 2),
            "p_model": round(p_model, 4),
            "ratio": round(ratio, 2) if ratio else None,
            "fail": fail,
        }
        notable_buckets.append(row)
        if fail in {"low_model_prob", "low_prob_ratio", "low_edge"} and ask and ask > 0:
            if best_near is None or (row["ratio"] or 0) > (best_near.get("ratio") or 0):
                best_near = row

    for bucket in buckets:
        if _already_in(open_positions, bucket.market.condition_id):
            rejects["already_in_position"] += 1
            continue
        try:
            token = bucket.market.get_token_id("yes")
            book = engine.api.get_order_book(token)
        except Exception:
            rejects["order_book_error"] += 1
            continue
        ask, ask_size = best_ask(book)
        p_model, src = tail_bucket_probability(ensemble, bucket.rng)

        ask_fail = classify_ask_reject(
            ask,
            ask_size,
            min_ask=cfg.min_ask,
            max_ask=cfg.max_ask,
            min_size=settings.min_best_ask_size,
        )
        if ask_fail:
            rejects[ask_fail] += 1
            if p_model >= cfg.min_model_prob * 0.5:
                _near_miss(bucket, ask, ask_size, p_model, ask_fail)
            continue

        ow_est = _VARIANCE.from_openweather(http, city, event_date, bucket.rng, today=local_today)
        sigma = _VARIANCE.sigma_for_horizon(days_ahead)
        if ow_est is not None:
            p_model = max(p_model, ow_est.p)
            src = f"{src}+openweather"
            sigma = ow_est.sigma_f

        p_model = min(max(p_model, 1e-6), 1.0 - 1e-6)

        if p_model < cfg.min_model_prob:
            rejects["low_model_prob"] += 1
            _near_miss(bucket, ask, ask_size, p_model, "low_model_prob")
            continue
        if ask <= 0 or p_model / ask < cfg.min_prob_ratio:
            rejects["low_prob_ratio"] += 1
            _near_miss(bucket, ask, ask_size, p_model, "low_prob_ratio")
            continue
        if p_model - ask < cfg.min_edge:
            rejects["low_edge"] += 1
            _near_miss(bucket, ask, ask_size, p_model, "low_edge")
            continue
        candidates.append(
            {
                "bucket": bucket,
                "ask": ask,
                "p_model": p_model,
                "src": src,
                "sigma": sigma,
                "edge": p_model - ask,
                "ratio": p_model / ask,
            }
        )

    if not candidates:
        notable_buckets.sort(key=lambda row: row["p_model"], reverse=True)
        _log_asym(
            engine,
            decision="skip",
            reason="no_tail_candidates",
            city=city,
            event_date=event_date,
            buckets_scanned=len(buckets),
            rejects=dict(rejects),
            skip_summary=format_skip_summary(dict(rejects)),
            near_miss=best_near,
            notable_buckets=notable_buckets[:5],
            ensemble_members=len(ensemble.members_f),
            ensemble_source=ensemble.source,
            openweather_high=ensemble.openweather_high_f,
            days_ahead=days_ahead,
        )
        return None
    # Prefer the largest model-vs-market gap on the cheapest asks.
    best = max(candidates, key=lambda c: (c["ratio"], c["edge"]))
    bucket: BucketMarket = best["bucket"]
    bankroll = account_cash(engine, settings.starting_balance)
    kelly = _KELLY.compute(best["p_model"], best["ask"], bankroll)
    if kelly.skipped or kelly.stake_usd is None:
        _log_asym(
            engine,
            decision="skip",
            reason="kelly_rejected",
            city=city,
            event_date=event_date,
            bucket=bucket.bucket_text,
            ask=best["ask"],
            p_model=best["p_model"],
            kelly_skip_reason=kelly.reason,
            days_ahead=days_ahead,
        )
        return None

    shadow = ShadowLedger(engine.db.data_dir)
    f_star = kelly.f_star
    shadow.log_entry(
        strategy="asymmetric",
        slug=bucket.market.slug,
        action="buy",
        share_price=best["ask"],
        p=best["p_model"],
        sigma=best["sigma"],
        f_star=f_star,
        stake_usd=kelly.stake_usd,
        extra={"source": best["src"], "quarter_f": kelly.quarter_f},
    )

    reason = (
        f"tail {bucket.bucket_text} limit@{best['ask']:.3f} "
        f"P={best['p_model']:.2f} f*={f_star:.3f} qk=${kelly.stake_usd:.2f} "
        f"({best['src']}) d+{days_ahead}"
    )
    _log_asym(
        engine,
        decision="buy",
        reason=reason,
        city=city,
        event_date=event_date,
        slug=bucket.market.slug,
        bucket=bucket.bucket_text,
        action="buy",
        ask=best["ask"],
        p_model=best["p_model"],
        stake_usd=kelly.stake_usd,
        edge=best["edge"],
        ratio=best["ratio"],
        source=best["src"],
        days_ahead=days_ahead,
    )

    return Signal(
        action="buy",
        slug=bucket.market.slug,
        outcome="yes",
        amount_usd=kelly.stake_usd,
        city=city,
        event_slug=bucket.event_slug,
        # Taker at ask: maker-below-ask tails rarely fill in paper/sim.
        order_type="fak",
        limit_price=None,
        quant=QuantMeta(
            p=best["p_model"],
            sigma=best["sigma"],
            f_star=f_star,
            kelly_fraction=kelly.quarter_f,
            source=best["src"],
        ),
        reason=(
            reason
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
    """Hedge before resolution: monitor exits + legacy brakes."""
    now = now or datetime.now(timezone.utc)
    cfg = settings.asymmetric
    shadow = ShadowLedger(engine.db.data_dir)
    exit_store = PositionExitStore(engine.db.data_dir)
    exit_store.prune_closed(open_positions)
    signals = monitor_exits(
        engine,
        open_positions,
        cities,
        cfg=monitor_config_from_settings(settings.asymmetric),
        now=now,
        shadow=shadow,
        exit_store=exit_store,
    )
    seen_slugs = {s.slug for s in signals}
    for pos in open_positions:
        if pos.shares <= 0 or pos.market_slug in seen_slugs:
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
        ensemble = fetch_combined_ensemble(http, city, event_date)
        p95 = ensemble_p95(list(ensemble.members_f))
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
        elif bid <= cfg.stop_loss_bid and pos.avg_entry_price <= cfg.max_ask:
            reason = f"tail stop bid={bid:.3f} entry={pos.avg_entry_price:.3f}"
        else:
            local_today = city_local_today(city, now)
            days_ahead = (event_date - local_today).days
            if days_ahead > cfg.exit_model_prob_min_days_ahead:
                continue
            ensemble = fetch_combined_ensemble(http, city, event_date)
            p_model, _ = tail_bucket_probability(ensemble, rng)
            if p_model < cfg.exit_model_prob and bid >= cfg.min_sell_bid:
                reason = (
                    f"model faded P={p_model:.2f} < {cfg.exit_model_prob:.2f} "
                    f"bid={bid:.3f} d+{days_ahead}"
                )
        if reason is None:
            continue
        exit_store.clear(pos.market_condition_id, pos.outcome)
        _log_asym(
            engine,
            decision="sell",
            reason=reason,
            city=city,
            event_date=event_date,
            slug=pos.market_slug,
            bucket=pos.market_question,
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
                order_type="limit",
                limit_price=bid,
                market_condition_id=pos.market_condition_id,
            )
        )
    return signals
