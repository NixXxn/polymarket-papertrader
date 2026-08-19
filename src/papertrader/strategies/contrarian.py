from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.buckets import parse_temperature_range
from papertrader.config import City, Settings
from papertrader.decision_log import format_skip_summary, log_decision
from papertrader.markets import (
    BucketMarket,
    best_ask,
    best_bid,
    city_from_market_slug,
    city_local_today,
    date_from_temp_slug,
)
from papertrader.quant.event_kelly import CorrelatedBet, allocate_correlated_kelly
from papertrader.quant.shadow_ledger import ShadowLedger
from papertrader.quant.shin import shin_fair_probs_from_asks
from papertrader.quant.variance import VarianceCalculator
from papertrader.signals import QuantMeta, Signal
from papertrader.sizing import account_cash
from papertrader.weather import WeatherHttp, fetch_metar_observed_high
from papertrader.weather.ensemble import fetch_combined_ensemble, tail_bucket_probability
from papertrader.weather.impossibility import is_mathematically_impossible
from papertrader.weather.probability import ensemble_p95

_VARIANCE = VarianceCalculator()


class _BucketQuote:
    __slots__ = ("bucket", "yes_ask", "yes_bid", "no_ask", "no_bid", "ask_size")

    def __init__(
        self,
        bucket: BucketMarket,
        yes_ask: float,
        yes_bid: float,
        no_ask: float,
        no_bid: float,
        ask_size: float,
    ) -> None:
        self.bucket = bucket
        self.yes_ask = yes_ask
        self.yes_bid = yes_bid
        self.no_ask = no_ask
        self.no_bid = no_bid
        self.ask_size = ask_size


def _log_contrarian(
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
        strategy="contrarian",
        decision=decision,
        reason=reason,
        city=city.slug,
        event_date=event_date,
        **extra,
    )


def _city_allowed(city: City, settings: Settings) -> bool:
    allowed = settings.contrarian.cities
    if allowed:
        return city.slug in allowed
    return "contrarian" in city.strategies


def _already_in(positions: list[Position], condition_id: str) -> bool:
    return any(
        p.market_condition_id == condition_id and p.shares > 0 and not p.is_resolved
        for p in positions
    )


def _maker_buy_price(*, ask: float, fair: float, tick: float) -> float:
    price = min(round(ask - tick, 2), round(fair - tick, 2))
    return max(0.01, min(0.99, price))


def _maker_sell_price(*, bid: float, tick: float) -> float:
    return max(0.01, min(0.99, round(bid + tick, 2)))


def _collect_bucket_quotes(
    engine: Engine,
    buckets: list[BucketMarket],
    settings: Settings,
) -> tuple[list[_BucketQuote], dict[str, int]]:
    quotes: list[_BucketQuote] = []
    rejects: dict[str, int] = defaultdict(int)
    for bucket in buckets:
        try:
            yes_book = engine.api.get_order_book(bucket.market.get_token_id("yes"))
            no_book = engine.api.get_order_book(bucket.market.get_token_id("no"))
        except Exception:
            rejects["order_book_error"] += 1
            continue
        yes_ask, ask_size = best_ask(yes_book)
        yes_bid, _ = best_bid(yes_book)
        no_ask, _ = best_ask(no_book)
        no_bid, _ = best_bid(no_book)
        if yes_ask is None or no_ask is None:
            rejects["no_ask"] += 1
            continue
        if ask_size < settings.min_best_ask_size:
            rejects["ask_size_too_small"] += 1
            continue
        quotes.append(
            _BucketQuote(
                bucket=bucket,
                yes_ask=yes_ask,
                yes_bid=yes_bid or 0.0,
                no_ask=no_ask,
                no_bid=no_bid or 0.0,
                ask_size=ask_size,
            )
        )
    return quotes, rejects


def analyze_contrarian_event(
    engine: Engine,
    http: WeatherHttp,
    city: City,
    event_date: date,
    buckets: list[BucketMarket],
    settings: Settings,
    open_positions: list[Position],
    today: date | None = None,
) -> list[Signal]:
    """Fade overpriced YES longshots via maker NO buys; Shin devig + event Kelly."""
    if not _city_allowed(city, settings):
        return []
    cfg = settings.contrarian
    local_today = today or city_local_today(city)
    days_ahead = (event_date - local_today).days
    if days_ahead < 0:
        return []

    if len(open_positions) >= cfg.max_open_positions:
        _log_contrarian(
            engine,
            decision="skip",
            reason="max_open_positions",
            city=city,
            event_date=event_date,
            open_positions=len(open_positions),
        )
        return []

    event_volume = buckets[0].event_volume if buckets else 0.0
    if event_volume < cfg.min_event_volume:
        return []

    ensemble = fetch_combined_ensemble(http, city, event_date)
    if len(ensemble.members_f) < cfg.min_ensemble_members:
        return []

    quotes, rejects = _collect_bucket_quotes(engine, buckets, settings)
    if not quotes:
        return []

    fair_yes_list = shin_fair_probs_from_asks([q.yes_ask for q in quotes])
    fair_yes_by_slug = {
        q.bucket.market.slug: fair_p for q, fair_p in zip(quotes, fair_yes_list)
    }
    bankroll = account_cash(engine, settings.starting_balance)
    notable: list[dict] = []
    candidates: list[tuple[_BucketQuote, CorrelatedBet, float, str]] = []

    for quote, fair_p_yes in zip(quotes, fair_yes_list):
        bucket = quote.bucket
        if _already_in(open_positions, bucket.market.condition_id):
            rejects["already_in_position"] += 1
            continue
        if not (cfg.min_yes_ask <= quote.yes_ask <= cfg.max_yes_ask):
            rejects["yes_ask_out_of_range"] += 1
            continue

        p_model_yes, src = tail_bucket_probability(ensemble, bucket.rng)
        ow_est = _VARIANCE.from_openweather(http, city, event_date, bucket.rng, today=local_today)
        if ow_est is not None:
            p_model_yes = min(p_model_yes, ow_est.p)

        if p_model_yes > cfg.max_model_yes:
            rejects["model_yes_not_tail"] += 1
            continue

        fair_p_no = max(0.0, 1.0 - fair_p_yes)
        p_model_no = max(0.0, 1.0 - p_model_yes)
        if quote.yes_ask <= fair_p_yes + cfg.min_vig_edge:
            rejects["yes_not_overpriced"] += 1
            notable.append(
                {
                    "bucket": bucket.bucket_text,
                    "yes_ask": quote.yes_ask,
                    "fair_yes": round(fair_p_yes, 4),
                    "p_model_yes": round(p_model_yes, 4),
                    "fail": "yes_not_overpriced",
                }
            )
            continue

        limit_no = _maker_buy_price(
            ask=quote.no_ask,
            fair=fair_p_no,
            tick=cfg.maker_tick,
        )
        edge = p_model_no - fair_p_no
        if edge < cfg.min_edge or p_model_no - limit_no < cfg.min_edge:
            rejects["low_edge"] += 1
            continue

        candidates.append(
            (
                quote,
                CorrelatedBet(
                    p=p_model_no,
                    price=limit_no,
                    edge=edge,
                    label=bucket.bucket_text,
                ),
                p_model_yes,
                src,
            )
        )

    if not candidates:
        _log_contrarian(
            engine,
            decision="skip",
            reason="no_contrarian_candidates",
            city=city,
            event_date=event_date,
            buckets_scanned=len(buckets),
            rejects=dict(rejects),
            skip_summary=format_skip_summary(dict(rejects)),
            notable_buckets=notable[:5],
            days_ahead=days_ahead,
        )
        return []

    candidates.sort(key=lambda row: row[1].edge, reverse=True)
    candidates = candidates[: cfg.max_no_bets_per_event]
    kelly_divisor = 1.0 / cfg.kelly_fraction if cfg.kelly_fraction > 0 else 4.0
    allocated = allocate_correlated_kelly(
        [row[1] for row in candidates],
        bankroll,
        kelly_divisor=kelly_divisor,
        max_usd_per_bet=cfg.max_position_usd,
        min_usd=settings.min_position_usd,
        max_event_fraction=cfg.max_event_fraction,
    )
    if not allocated:
        return []

    shadow = ShadowLedger(engine.db.data_dir)
    by_label = {row[1].label: row for row in candidates}
    signals: list[Signal] = []
    for bet, stake in allocated:
        quote, _, p_model_yes, src = by_label[bet.label]
        bucket = quote.bucket
        fair_p_yes = fair_yes_by_slug[bucket.market.slug]
        reason = (
            f"fade {bucket.bucket_text} NO limit@{bet.price:.3f} "
            f"P_no={bet.p:.2f} fair_yes={fair_p_yes:.3f} yes_ask={quote.yes_ask:.3f} "
            f"edge={bet.edge:.3f} qk=${stake:.2f} ({src}) d+{days_ahead}"
        )
        _log_contrarian(
            engine,
            decision="buy",
            reason=reason,
            city=city,
            event_date=event_date,
            slug=bucket.market.slug,
            bucket=bucket.bucket_text,
            action="buy",
            outcome="no",
            yes_ask=quote.yes_ask,
            fair_yes=fair_p_yes,
            p_model_yes=p_model_yes,
            limit_price=bet.price,
            stake_usd=stake,
            edge=bet.edge,
            days_ahead=days_ahead,
        )
        shadow.log_entry(
            strategy="contrarian",
            slug=bucket.market.slug,
            action="buy",
            share_price=bet.price,
            p=bet.p,
            sigma=0.0,
            f_star=bet.edge,
            stake_usd=stake,
            extra={"outcome": "no", "fair_yes": fair_p_yes},
        )
        signals.append(
            Signal(
                action="buy",
                slug=bucket.market.slug,
                outcome="no",
                amount_usd=stake,
                city=city,
                event_slug=bucket.event_slug,
                order_type="limit",
                limit_price=bet.price,
                quant=QuantMeta(
                    p=bet.p,
                    sigma=0.0,
                    f_star=bet.edge,
                    kelly_fraction=cfg.kelly_fraction,
                    source=src,
                ),
                reason=reason,
                market_condition_id=bucket.market.condition_id,
            )
        )
    return signals


def contrarian_exits(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    open_positions: list[Position],
    cities: dict[str, City],
    now: datetime | None = None,
) -> list[Signal]:
    """Maker exits on NO positions: take profit, model fade, time stop."""
    now = now or datetime.now(timezone.utc)
    cfg = settings.contrarian
    signals: list[Signal] = []

    for pos in open_positions:
        if pos.shares <= 0 or pos.outcome.lower() != "no":
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

        reason: str | None = None
        if bid >= cfg.take_profit_no_bid:
            reason = f"NO TP bid={bid:.3f} >= {cfg.take_profit_no_bid:.3f}"
        else:
            observed = fetch_metar_observed_high(http, city, event_date, now)
            ensemble = fetch_combined_ensemble(http, city, event_date)
            p95 = ensemble_p95(list(ensemble.members_f))
            dead, why = is_mathematically_impossible(
                rng,
                city=city,
                event_date=event_date,
                settings=settings.contrarian,
                observed_high_f=observed,
                ensemble_p95_f=p95,
                now=now,
            )
            if dead:
                reason = f"contrarian brake: {why}"
            else:
                p_model_yes, _ = tail_bucket_probability(ensemble, rng)
                local_today = city_local_today(city, now)
                days_ahead = (event_date - local_today).days
                if days_ahead <= cfg.exit_model_prob_min_days_ahead:
                    if p_model_yes >= cfg.exit_model_yes:
                        reason = (
                            f"YES rallied P={p_model_yes:.2f} >= {cfg.exit_model_yes:.2f} "
                            f"bid={bid:.3f}"
                        )
                if bid <= cfg.stop_loss_no_bid and pos.avg_entry_price >= cfg.min_no_entry:
                    reason = reason or (
                        f"NO stop bid={bid:.3f} entry={pos.avg_entry_price:.3f}"
                    )

        if reason is None:
            continue
        limit_price = _maker_sell_price(bid=bid, tick=cfg.maker_tick)
        _log_contrarian(
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
            limit_price=limit_price,
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
                limit_price=limit_price,
                market_condition_id=pos.market_condition_id,
            )
        )
    return signals
