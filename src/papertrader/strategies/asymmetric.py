from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.buckets import parse_temperature_range
from papertrader.config import AsymmetricSettings, City, Settings
from papertrader.decision_log import format_skip_summary, log_decision
from papertrader.markets import (
    BucketMarket,
    best_ask,
    best_bid,
    city_from_market_slug,
    city_local_today,
    date_from_temp_slug,
)
from papertrader.quant.book_walk import max_price_for_positive_ev
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


def _tail_model_probs(
    ensemble,
    rng,
    *,
    http: WeatherHttp,
    city: City,
    event_date: date,
    local_today: date,
) -> tuple[float, float | None, float, str, float]:
    """Open-Meteo ensemble + OpenWeather bucket P(YES) for cheap YES limits."""
    p_ens, src = tail_bucket_probability(ensemble, rng)
    ow_est = _VARIANCE.from_openweather(http, city, event_date, rng, today=local_today)
    p_ow = ow_est.p if ow_est is not None else None
    p_model = max(p_ens, p_ow or 0.0)
    if p_ow is not None:
        src = f"{src}+openweather"
    sigma = ow_est.sigma_f if ow_est is not None else _VARIANCE.sigma_for_horizon(
        (event_date - local_today).days
    )
    return p_ens, p_ow, p_model, src, sigma


def _edge_ok_at_limit(
    *,
    limit: float,
    p_model: float,
    p_ens: float,
    p_ow: float | None,
    cfg: AsymmetricSettings,
    dual: bool,
) -> bool:
    if limit <= 0 or p_model < cfg.min_model_prob:
        return False
    if p_model - limit < cfg.min_edge:
        return False
    if p_model / limit < cfg.min_prob_ratio:
        return False
    if dual and p_ow is not None:
        if p_ens - limit < cfg.min_dual_edge or p_ow - limit < cfg.min_dual_edge:
            return False
    return True


def _pick_cheap_limit(
    *,
    p_ens: float,
    p_ow: float | None,
    p_model: float,
    cfg: AsymmetricSettings,
) -> tuple[float | None, str]:
    """Prefer 1–2¢ resting limits; step up only when both weather APIs agree."""
    for limit, label in (
        (cfg.preferred_limit, "1c"),
        (cfg.fallback_limit, "2c"),
    ):
        if _edge_ok_at_limit(
            limit=limit,
            p_model=p_model,
            p_ens=p_ens,
            p_ow=p_ow,
            cfg=cfg,
            dual=p_ow is not None,
        ):
            return limit, label

    if p_ow is not None and p_ens >= cfg.min_model_prob and p_ow >= cfg.min_model_prob:
        min_p = min(p_ens, p_ow)
        if min_p / max(cfg.preferred_limit, 1e-6) >= cfg.high_conf_min_ratio:
            ceiling = min(
                cfg.max_ask,
                cfg.high_conf_max_limit,
                max_price_for_positive_ev(min_p, min_ev=cfg.min_edge),
            )
            limit = round(cfg.fallback_limit + cfg.maker_tick, 4)
            best: float | None = None
            while limit <= ceiling + 1e-9:
                if (
                    min_p - limit >= cfg.min_edge
                    and min_p / limit >= cfg.min_prob_ratio
                ):
                    best = limit
                limit = round(limit + cfg.maker_tick, 4)
            if best is not None:
                return best, "high_conf"

    if p_ow is None:
        for limit in (cfg.preferred_limit, cfg.fallback_limit):
            if _edge_ok_at_limit(
                limit=limit,
                p_model=p_model,
                p_ens=p_ens,
                p_ow=None,
                cfg=cfg,
                dual=False,
            ):
                return limit, "openmeteo_only"
    return None, "no_edge_at_cheap_limit"


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
        p_ens, p_ow, p_model, src, sigma = _tail_model_probs(
            ensemble,
            bucket.rng,
            http=http,
            city=city,
            event_date=event_date,
            local_today=local_today,
        )
        p_model = min(max(p_model, 1e-6), 1.0 - 1e-6)

        limit_price, limit_tier = _pick_cheap_limit(
            p_ens=p_ens,
            p_ow=p_ow,
            p_model=p_model,
            cfg=cfg,
        )
        if limit_price is None:
            rejects[limit_tier] += 1
            if p_model >= cfg.min_model_prob * 0.5:
                _near_miss(bucket, ask, ask_size, p_model, limit_tier)
            continue

        if ask is not None and ask_size < settings.min_best_ask_size:
            rejects["thin_book"] += 1
            continue

        if p_model < cfg.min_model_prob:
            rejects["low_model_prob"] += 1
            _near_miss(bucket, ask, ask_size, p_model, "low_model_prob")
            continue
        edge = p_model - limit_price
        ratio = p_model / limit_price
        if ratio < cfg.min_prob_ratio or edge < cfg.min_edge:
            rejects["low_edge"] += 1
            _near_miss(bucket, ask, ask_size, p_model, "low_edge")
            continue
        candidates.append(
            {
                "bucket": bucket,
                "ask": ask,
                "p_model": p_model,
                "p_ens": p_ens,
                "p_ow": p_ow,
                "src": src,
                "sigma": sigma,
                "edge": edge,
                "ratio": ratio,
                "limit_price": limit_price,
                "limit_tier": limit_tier,
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
    # Prefer largest model gap on the cheapest resting limit.
    best = max(candidates, key=lambda c: (c["ratio"], c["edge"], -c["limit_price"]))
    bucket: BucketMarket = best["bucket"]
    limit_price = best["limit_price"]
    bankroll = account_cash(engine, settings.starting_balance)
    kelly = _KELLY.compute(best["p_model"], limit_price, bankroll)
    if kelly.skipped or kelly.stake_usd is None:
        _log_asym(
            engine,
            decision="skip",
            reason="kelly_rejected",
            city=city,
            event_date=event_date,
            bucket=bucket.bucket_text,
            ask=best["ask"],
            limit_price=limit_price,
            p_model=best["p_model"],
            kelly_skip_reason=kelly.reason,
            days_ahead=days_ahead,
        )
        return None

    stake = round(min(kelly.stake_usd, cfg.max_position_usd), 2)
    if stake < settings.min_position_usd:
        return None

    shadow = ShadowLedger(engine.db.data_dir)
    f_star = kelly.f_star
    shadow.log_entry(
        strategy="asymmetric",
        slug=bucket.market.slug,
        action="buy",
        share_price=limit_price,
        p=best["p_model"],
        sigma=best["sigma"],
        f_star=f_star,
        stake_usd=stake,
        extra={
            "source": best["src"],
            "quarter_f": kelly.quarter_f,
            "limit_price": limit_price,
            "limit_tier": best["limit_tier"],
            "p_ens": best["p_ens"],
            "p_ow": best["p_ow"],
            "market_ask": best["ask"],
        },
    )

    ow_part = f" ow={best['p_ow']:.2f}" if best["p_ow"] is not None else ""
    reason = (
        f"tail {bucket.bucket_text} limit@{limit_price:.3f} ({best['limit_tier']}) "
        f"P={best['p_model']:.2f} ens={best['p_ens']:.2f}{ow_part} "
        f"f*={f_star:.3f} qk=${stake:.2f} ({best['src']}) d+{days_ahead}"
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
        p_ens=best["p_ens"],
        p_ow=best["p_ow"],
        stake_usd=stake,
        edge=best["edge"],
        ratio=best["ratio"],
        source=best["src"],
        days_ahead=days_ahead,
        limit_price=limit_price,
        limit_tier=best["limit_tier"],
    )

    return Signal(
        action="buy",
        slug=bucket.market.slug,
        outcome="yes",
        amount_usd=stake,
        city=city,
        event_slug=bucket.event_slug,
        order_type="limit",
        limit_price=limit_price,
        quant=QuantMeta(
            p=best["p_model"],
            sigma=best["sigma"],
            f_star=f_star,
            kelly_fraction=kelly.quarter_f,
            source=best["src"],
        ),
        reason=reason,
        market_condition_id=bucket.market.condition_id,
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
