from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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
    event_slug_from_market_slug,
)
from papertrader.quant.bayes import shadow_no_fade
from papertrader.quant.book_walk import walk_asks_for_buy
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
_COOLDOWN_FILE = "contrarian_fade_cooldown.json"
_COOLDOWN_HOURS = 6.0
_TIGHT_NO_ASK = 0.85
_TIGHT_MIN_EDGE = 0.07
_DPLUS1_EXTRA_EDGE = 0.025
_HIGH_CONF_MIN_P = 0.95
_HIGH_CONF_MIN_EDGE = 0.06
_HIGH_CONF_SIZE_MULT = 1.5
_THIN_ASK_SIZE = 12.0
_THIN_SIZE_MULT = 0.55
_CAPACITY_TIGHT_SLOTS = 3
_CAPACITY_MIN_EDGE = 0.055


def _event_key(slug: str) -> str:
    """Strip the trailing bucket token so one NO per city/date event."""
    return event_slug_from_market_slug(slug)


def _already_in_event(positions: list[Position], slug: str) -> bool:
    key = _event_key(slug)
    return any(
        p.shares > 0 and not p.is_resolved and _event_key(p.market_slug) == key
        for p in positions
    )


def _cooldown_path(engine: Engine) -> Path:
    root = Path(engine.db.data_dir)
    if root.name == "contrarian":
        root = root.parent
    return root / _COOLDOWN_FILE


def _load_cooldown(engine: Engine) -> dict[str, str]:
    path = _cooldown_path(engine)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _slug_cooling_down(engine: Engine, slug: str, *, now: datetime) -> bool:
    key = _event_key(slug)
    until = _load_cooldown(engine).get(key)
    if not until:
        return False
    try:
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return False
    return end > now


def _mark_cooldown(engine: Engine, slug: str, *, now: datetime) -> None:
    key = _event_key(slug)
    data = _load_cooldown(engine)
    data[key] = (now + timedelta(hours=_COOLDOWN_HOURS)).isoformat()
    path = _cooldown_path(engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _model_yes_for_fade(
    ensemble,
    rng,
    *,
    http: WeatherHttp,
    city: City,
    event_date: date,
    local_today: date,
) -> tuple[float, str]:
    """Conservative P(YES) for fading: take the *higher* of ensemble vs OpenWeather.

    min() previously made mode buckets look like tails and caused buy/sell churn
    when exits used raw ensemble P(YES)≈1.
    """
    p_ens, src = tail_bucket_probability(ensemble, rng)
    ow_est = _VARIANCE.from_openweather(http, city, event_date, rng, today=local_today)
    if ow_est is None:
        return p_ens, src
    if ow_est.p > p_ens:
        return ow_est.p, f"{src}+ow"
    return p_ens, src


class _BucketQuote:
    __slots__ = ("bucket", "yes_ask", "yes_bid", "no_ask", "no_bid", "ask_size", "no_book")

    def __init__(
        self,
        bucket: BucketMarket,
        yes_ask: float,
        yes_bid: float,
        no_ask: float,
        no_bid: float,
        ask_size: float,
        no_book: object | None = None,
    ) -> None:
        self.bucket = bucket
        self.yes_ask = yes_ask
        self.yes_bid = yes_bid
        self.no_ask = no_ask
        self.no_bid = no_bid
        self.ask_size = ask_size
        self.no_book = no_book


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


def _city_open_count(positions: list[Position], city_slug: str) -> int:
    needle = f"in-{city_slug}-on-"
    return sum(
        1
        for p in positions
        if p.shares > 0
        and not p.is_resolved
        and needle in (p.market_slug or "")
    )


def _rank_score(*, p_win: float, edge: float, fill_no: float, ask_size: float) -> float:
    """Prioritize P(win)×edge×upside×liquidity — not raw fill volume."""
    upside = max(0.02, 1.0 - fill_no)
    liq = min(1.5, math.log1p(max(0.0, ask_size)) / math.log1p(25.0))
    return max(0.0, p_win) * max(0.0, edge) * upside * max(0.15, liq)


def _stake_liquidity_mult(ask_size: float) -> float:
    if ask_size < _THIN_ASK_SIZE:
        return _THIN_SIZE_MULT
    if ask_size >= 40.0:
        return 1.15
    return 1.0


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
                no_book=no_book,
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
    """Fade overpriced YES longshots via L2-capped LIMIT NO buys (Kelly ∩ EV depth)."""
    if not _city_allowed(city, settings):
        return []
    cfg = settings.contrarian
    local_today = today or city_local_today(city)
    days_ahead = (event_date - local_today).days
    min_days = int(getattr(cfg, "min_days_ahead", 0))
    if days_ahead < min_days or days_ahead > cfg.max_days_ahead:
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

    city_open = _city_open_count(open_positions, city.slug)
    if city_open >= cfg.max_open_per_city:
        _log_contrarian(
            engine,
            decision="skip",
            reason="city_crowded",
            city=city,
            event_date=event_date,
            open_positions=len(open_positions),
            city_open=city_open,
            max_open_per_city=cfg.max_open_per_city,
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
    shadow = ShadowLedger(engine.db.data_dir)

    for quote, fair_p_yes in zip(quotes, fair_yes_list):
        bucket = quote.bucket
        if _already_in(open_positions, bucket.market.condition_id):
            rejects["already_in_position"] += 1
            continue
        if _already_in_event(open_positions, bucket.market.slug):
            rejects["already_in_event"] += 1
            continue
        if _slug_cooling_down(engine, bucket.market.slug, now=datetime.now(timezone.utc)):
            rejects["cooldown"] += 1
            continue
        if not (cfg.min_yes_ask <= quote.yes_ask <= cfg.max_yes_ask):
            rejects["yes_ask_out_of_range"] += 1
            continue
        # High WR band: NO must be a strong favorite, but not so tight that R:R dies.
        if quote.no_ask < cfg.min_no_entry or quote.no_ask > cfg.max_no_ask:
            rejects["no_ask_out_of_range"] += 1
            continue

        p_model_yes, src = _model_yes_for_fade(
            ensemble,
            bucket.rng,
            http=http,
            city=city,
            event_date=event_date,
            local_today=local_today,
        )

        if p_model_yes > cfg.max_model_yes:
            rejects["model_yes_not_tail"] += 1
            continue

        p_model_no = min(max(1e-6, 1.0 - p_model_yes), 1.0 - 1e-6)
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

        # Size / edge vs the FAK fill we actually pay (no_ask), not a resting maker quote.
        fill_no = round(quote.no_ask, 4)
        edge = p_model_no - fill_no
        required_edge = cfg.min_edge
        if days_ahead >= 1:
            required_edge += _DPLUS1_EXTRA_EDGE
        # Near capacity: only take cleaner edges (free slots for best names).
        slots_left = cfg.max_open_positions - len(open_positions)
        if slots_left <= _CAPACITY_TIGHT_SLOTS:
            required_edge = max(required_edge, _CAPACITY_MIN_EDGE)
            if days_ahead >= 1:
                rejects["capacity_prefer_d0"] += 1
                continue
        # Tight NO asks leave little to $1 — only take them with fat model edge.
        if fill_no >= _TIGHT_NO_ASK:
            required_edge = max(required_edge, _TIGHT_MIN_EDGE)

        if cfg.bayes_shadow:
            bayes = shadow_no_fade(
                prior_yes=fair_p_yes,
                model_yes=p_model_yes,
                no_ask=fill_no,
                shrink=cfg.bayes_lr_shrink,
                max_lr=cfg.bayes_max_lr,
                fee_buffer=cfg.bayes_fee_buffer,
            )
            model_would = edge >= required_edge
            bayes_would = bayes.bayes_edge_no >= required_edge
            shadow.log_bayes_shadow(
                strategy="contrarian",
                slug=bucket.market.slug,
                prior_yes=bayes.prior_yes,
                evidence_yes=bayes.evidence_yes,
                lr_yes=bayes.lr_yes,
                posterior_yes=bayes.posterior_yes,
                model_edge_no=bayes.model_edge_no,
                bayes_edge_no=bayes.bayes_edge_no,
                no_ask=fill_no,
                required_edge=required_edge,
                model_would_take=model_would,
                bayes_would_take=bayes_would,
                extra={
                    "fair_yes": round(fair_p_yes, 6),
                    "yes_ask": round(quote.yes_ask, 4),
                    "source": src,
                    "days_ahead": days_ahead,
                    "bucket": bucket.bucket_text,
                },
            )

        if edge < required_edge:
            rejects["low_edge"] += 1
            continue

        score = _rank_score(
            p_win=p_model_no,
            edge=edge,
            fill_no=fill_no,
            ask_size=quote.ask_size,
        )
        candidates.append(
            (
                quote,
                CorrelatedBet(
                    p=p_model_no,
                    price=fill_no,
                    edge=edge,
                    label=bucket.bucket_text,
                ),
                p_model_yes,
                src,
                score,
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

    # Prefer P(win)×edge×upside×liquidity (safer + larger expected wins).
    candidates.sort(key=lambda row: row[4], reverse=True)
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

    by_label = {row[1].label: row for row in candidates}
    signals: list[Signal] = []
    for bet, stake in allocated:
        quote, _, p_model_yes, src, _score = by_label[bet.label]
        bucket = quote.bucket
        fair_p_yes = fair_yes_by_slug[bucket.market.slug]
        stake = round(stake * _stake_liquidity_mult(quote.ask_size), 2)
        if (
            days_ahead == 0
            and bet.p >= _HIGH_CONF_MIN_P
            and bet.edge >= _HIGH_CONF_MIN_EDGE
            and quote.ask_size >= _THIN_ASK_SIZE
        ):
            stake = min(cfg.max_position_usd, round(stake * _HIGH_CONF_SIZE_MULT, 2))
        stake = min(cfg.max_position_usd, max(settings.min_position_usd, stake))

        # HEART: walk NO asks under EV ceiling, clip Kelly to fillable depth, strict LIMIT.
        walk_min_ev = max(0.0, float(getattr(cfg, "book_walk_min_ev", 0.0)))
        if cfg.strict_limit and quote.no_book is not None:
            walk = walk_asks_for_buy(
                quote.no_book,
                p_win=bet.p,
                budget_usd=stake,
                min_ev=walk_min_ev,
                hard_max_price=cfg.max_no_ask,
                min_usd=settings.min_position_usd,
            )
            if walk.skipped:
                _log_contrarian(
                    engine,
                    decision="skip",
                    reason="book_walk_no_depth",
                    city=city,
                    event_date=event_date,
                    slug=bucket.market.slug,
                    bucket=bucket.bucket_text,
                    walk_reason=walk.reason,
                    max_ev_price=walk.max_ev_price,
                    kelly_usd=stake,
                )
                continue
            stake = min(stake, walk.fillable_usd)
            stake = round(stake, 2)
            if stake < settings.min_position_usd:
                continue
            limit_price = walk.limit_price
            fill_ref = walk.vwap
            order_type = "limit"
            exec_tag = (
                f"limit@{limit_price:.3f} vwap={walk.vwap:.3f} "
                f"depth=${walk.fillable_usd:.2f}/{walk.levels_taken}lvl"
            )
        else:
            limit_price = None
            fill_ref = bet.price
            order_type = "fak"
            exec_tag = f"fak@{bet.price:.3f}"

        bayes_extra: dict = {}
        if cfg.bayes_shadow:
            bayes = shadow_no_fade(
                prior_yes=fair_p_yes,
                model_yes=p_model_yes,
                no_ask=bet.price,
                shrink=cfg.bayes_lr_shrink,
                max_lr=cfg.bayes_max_lr,
                fee_buffer=cfg.bayes_fee_buffer,
            )
            bayes_extra = {
                "bayes_prior_yes": round(bayes.prior_yes, 4),
                "bayes_lr_yes": round(bayes.lr_yes, 4),
                "bayes_post_yes": round(bayes.posterior_yes, 4),
                "bayes_edge_no": round(bayes.bayes_edge_no, 4),
            }

        reason = (
            f"fade {bucket.bucket_text} NO {exec_tag} "
            f"P_no={bet.p:.2f} fair_yes={fair_p_yes:.3f} yes_ask={quote.yes_ask:.3f} "
            f"edge={bet.edge:.3f} qk=${stake:.2f} ({src}) d+{days_ahead}"
        )
        if bayes_extra:
            reason += (
                f" bayes_post_yes={bayes_extra['bayes_post_yes']:.3f}"
                f" bayes_edge={bayes_extra['bayes_edge_no']:.3f}"
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
            limit_price=limit_price if limit_price is not None else bet.price,
            stake_usd=stake,
            edge=bet.edge,
            days_ahead=days_ahead,
            vwap=fill_ref,
            order_type=order_type,
            **bayes_extra,
        )
        shadow.log_entry(
            strategy="contrarian",
            slug=bucket.market.slug,
            action="buy",
            share_price=fill_ref,
            p=bet.p,
            sigma=0.0,
            f_star=bet.edge,
            stake_usd=stake,
            extra={
                "outcome": "no",
                "fair_yes": fair_p_yes,
                "order_type": order_type,
                "limit_price": limit_price,
                **bayes_extra,
            },
        )
        signals.append(
            Signal(
                action="buy",
                slug=bucket.market.slug,
                outcome="no",
                amount_usd=stake,
                city=city,
                event_slug=bucket.event_slug,
                order_type=order_type,  # type: ignore[arg-type]
                limit_price=limit_price,
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


def _duplicate_event_trim_slugs(open_positions: list[Position]) -> set[str]:
    """If legacy dual-NO on same city/date, keep cheapest entry (best R:R to $1)."""
    by_event: dict[str, list[Position]] = defaultdict(list)
    for pos in open_positions:
        if pos.shares <= 0 or pos.is_resolved or pos.outcome.lower() != "no":
            continue
        by_event[_event_key(pos.market_slug)].append(pos)
    trim: set[str] = set()
    for group in by_event.values():
        if len(group) < 2:
            continue
        keep = min(group, key=lambda p: (p.avg_entry_price, -p.shares))
        for pos in group:
            if pos.market_slug != keep.market_slug:
                trim.add(pos.market_slug)
    return trim


def contrarian_exits(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    open_positions: list[Position],
    cities: dict[str, City],
    now: datetime | None = None,
) -> list[Signal]:
    """Exit NO positions: near-$1 trim, model fade, impossibility, hard stop (FAK)."""
    now = now or datetime.now(timezone.utc)
    cfg = settings.contrarian
    signals: list[Signal] = []
    dup_trim = _duplicate_event_trim_slugs(open_positions)

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
        if pos.market_slug in dup_trim:
            reason = (
                f"duplicate_event_trim keep cheaper NO; "
                f"bid={bid:.3f} entry={pos.avg_entry_price:.3f}"
            )
            _mark_cooldown(engine, pos.market_slug, now=now)
        # Prefer holding to resolution; only take profit when essentially done.
        elif bid >= cfg.take_profit_no_bid:
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
                local_today = city_local_today(city, now)
                p_model_yes, _ = _model_yes_for_fade(
                    ensemble,
                    rng,
                    http=http,
                    city=city,
                    event_date=event_date,
                    local_today=local_today,
                )
                days_ahead = (event_date - local_today).days
                # Only dump on model fade when YES is clearly live AND NO mark is down.
                bid_drop = pos.avg_entry_price - bid
                min_drop = 0.05 if pos.avg_entry_price >= _TIGHT_NO_ASK else 0.08
                if (
                    days_ahead <= cfg.exit_model_prob_min_days_ahead
                    and p_model_yes >= cfg.exit_model_yes
                    and bid_drop >= min_drop
                ):
                    reason = (
                        f"YES rallied P={p_model_yes:.2f} >= {cfg.exit_model_yes:.2f} "
                        f"bid={bid:.3f} drop={bid_drop:.3f}"
                    )
                    _mark_cooldown(engine, pos.market_slug, now=now)
                # Catastrophic only: NO lost most of a high-confidence entry.
                if bid <= cfg.stop_loss_no_bid and pos.avg_entry_price >= cfg.min_no_entry:
                    if reason is None:
                        reason = (
                            f"NO stop bid={bid:.3f} entry={pos.avg_entry_price:.3f}"
                        )
                        _mark_cooldown(engine, pos.market_slug, now=now)

        if reason is None:
            continue
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
        )
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                city=city,
                reason=reason,
                order_type="fak",
                limit_price=None,
                market_condition_id=pos.market_condition_id,
            )
        )
    return signals
