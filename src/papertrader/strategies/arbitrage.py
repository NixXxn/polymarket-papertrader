"""Arbitrage / spread-capture: buy both sides when combined cost < $1 (locked edge).

Two-legged strategy — buy YES+NO (or Up+Down) so one side always pays $1/share.
When ask_yes + ask_no + fees < 1, the locked edge is independent of the outcome.
Prefers fast crypto / weather markets and ranks LP-reward markets higher when present.

After entry, exit by selling BOTH legs when combined mark-to-market is in profit
(or when bid_a + bid_b recovers near $1). Incomplete orphan fills are still unwound.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.markets import best_ask, best_bid
from papertrader.predictionhunt import (
    PredictionHuntClient,
    append_ph_signal,
    polymarket_slug_from_leg,
    predictionhunt_api_key,
)
from papertrader.signals import QuantMeta, Signal
from papertrader.sizing import account_cash, scaled_size

log = logging.getLogger("papertrader")

# Prefer fast-moving crypto + weather; still allow other binary markets at lower rank.
_PREFERRED_MARKERS = (
    "bitcoin",
    "btc-",
    "btc ",
    "ethereum",
    "eth-",
    "solana",
    "sol-",
    "xrp",
    "doge",
    "crypto",
    "updown",
    "up-down",
    "highest-temperature",
    "lowest-temperature",
    "temperature-in-",
)

# Skip slow / noisy prop markets where two-leg arb is rarely fillable.
_SKIP_MARKERS = (
    "player-props",
    "more-markets",
    "-spread-",
    "-total-",
    "-handicap-",
    "-o-u-",
    "first-blood",
    "correct-score",
)


@dataclass(frozen=True)
class _ArbMarket:
    condition_id: str
    slug: str
    question: str
    outcome_a: str  # "Yes" / "Up"
    outcome_b: str  # "No" / "Down"
    liquidity: float
    volume_24h: float
    lp_reward_score: float
    preferred: bool


@dataclass(frozen=True)
class _ArbQuote:
    market: _ArbMarket
    ask_a: float
    ask_b: float
    size_a: float
    size_b: float
    pair_cost: float
    edge: float


def _parse_json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _is_preferred(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _PREFERRED_MARKERS)


def _should_skip(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _SKIP_MARKERS)


def _lp_reward_score(market: dict[str, Any]) -> float:
    """Rank markets that advertise CLOB/LP rewards higher (maker incentives)."""
    score = 0.0
    rewards = market.get("clobRewards") or market.get("rewards") or []
    if isinstance(rewards, dict):
        rewards = [rewards]
    if not isinstance(rewards, list):
        return 0.0
    for row in rewards:
        if not isinstance(row, dict):
            continue
        for key in ("rewardsDailyRate", "rewardsAmount", "ratePerDay", "dailyRate"):
            try:
                score = max(score, float(row.get(key) or 0))
            except (TypeError, ValueError):
                continue
    try:
        score = max(score, float(market.get("competitive") or 0) * 0.01)
    except (TypeError, ValueError):
        pass
    return score


def _binary_outcomes(market: dict[str, Any]) -> tuple[str, str] | None:
    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    lowered = {o.lower(): o for o in outcomes}
    if "yes" in lowered and "no" in lowered:
        return lowered["yes"], lowered["no"]
    if "up" in lowered and "down" in lowered:
        return lowered["up"], lowered["down"]
    return None


def _log_arb(
    engine: Engine,
    *,
    decision: str,
    reason: str,
    **extra: Any,
) -> None:
    log_decision(
        engine.db.data_dir,
        strategy="arbitrage",
        decision=decision,
        reason=reason,
        **extra,
    )


def _scan_predictionhunt_arb(engine: Engine, settings: Settings) -> set[str]:
    """Pull /v2/arb, log detections, return Polymarket slugs to prioritize."""
    ph_cfg = settings.predictionhunt
    if not (
        ph_cfg.enabled
        and ph_cfg.scan_arb
        and "arbitrage" in ph_cfg.strategies
        and predictionhunt_api_key()
    ):
        return set()

    client = PredictionHuntClient(engine.db.data_dir, ph_cfg)
    opps, blocked = client.fetch_arb_opportunities()
    if blocked:
        append_ph_signal(
            engine.db.data_dir,
            {
                "event": "ph_arb_blocked",
                "strategy": "arbitrage",
                "reason": blocked,
            },
        )
        _log_arb(
            engine,
            decision="skip",
            reason=f"predictionhunt_arb_{blocked}",
        )
        return set()

    preferred: set[str] = set()
    for opp in opps:
        pm_legs = [
            {
                "side": leg.side,
                "platform": leg.platform,
                "market_id": leg.market_id,
                "price": leg.price,
                "slug": polymarket_slug_from_leg(leg),
                "liquidity_usd": leg.liquidity_usd,
            }
            for leg in opp.legs
        ]
        append_ph_signal(
            engine.db.data_dir,
            {
                "event": "ph_arb",
                "strategy": "arbitrage",
                "group_id": opp.group_id,
                "group_title": opp.group_title,
                "roi_pct": opp.roi_pct,
                "total_cost": opp.total_cost,
                "max_wager_usd": opp.max_wager_usd,
                "event_type": opp.event_type,
                "event_date": opp.event_date,
                "is_polymarket_pair": opp.is_polymarket_pair,
                "legs": pm_legs,
            },
        )
        if not (ph_cfg.execute_polymarket_arb_legs and opp.is_polymarket_pair):
            continue
        if opp.total_cost >= 1.0 - 1e-9:
            continue
        for leg in opp.polymarket_legs:
            slug = polymarket_slug_from_leg(leg)
            if slug:
                preferred.add(slug)

    _log_arb(
        engine,
        decision="scan",
        reason=f"predictionhunt arb: {len(opps)} opps / {len(preferred)} pm-pair slugs",
        ph_arb_count=len(opps),
        ph_pm_slugs=len(preferred),
    )
    return preferred


def _markets_from_ph_slugs(
    engine: Engine,
    settings: Settings,
    slugs: set[str],
) -> list[_ArbMarket]:
    """Resolve PH-prioritized Polymarket slugs into arb market rows."""
    cfg = settings.arbitrage
    out: list[_ArbMarket] = []
    for slug in slugs:
        try:
            market = engine.api.get_market(slug)
        except Exception:
            continue
        if getattr(market, "closed", False):
            continue
        pair: tuple[str, str] | None = None
        for a, b in (("Yes", "No"), ("yes", "no"), ("Up", "Down"), ("up", "down")):
            try:
                market.get_token_id(a)
                market.get_token_id(b)
                pair = (a, b)
                break
            except Exception:
                continue
        if pair is None:
            continue
        out.append(
            _ArbMarket(
                condition_id=str(getattr(market, "condition_id", "") or ""),
                slug=str(getattr(market, "slug", slug) or slug),
                question=str(getattr(market, "question", "") or ""),
                outcome_a=pair[0],
                outcome_b=pair[1],
                liquidity=float(getattr(market, "liquidity", 0) or 0),
                volume_24h=0.0,
                lp_reward_score=1.0,
                preferred=True,
            )
        )
        if len(out) >= max(5, cfg.max_open_pairs * 2):
            break
    return out


def discover_arb_markets(
    engine: Engine,
    settings: Settings,
    *,
    limit: int | None = None,
) -> list[_ArbMarket]:
    """Fetch active binary markets; prefer crypto/weather + LP-rewarded books."""
    cfg = settings.arbitrage
    lim = int(limit if limit is not None else cfg.scan_limit)
    try:
        data = engine.api._gamma_get(
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": lim,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    except Exception as e:
        log.warning("arbitrage: market fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        return []

    out: list[_ArbMarket] = []
    for m in data:
        try:
            slug = str(m.get("slug") or "")
            if not slug:
                continue
            question = str(m.get("question") or "")
            if _should_skip(slug, question):
                continue
            pair = _binary_outcomes(m)
            if pair is None:
                continue
            liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            vol = float(m.get("volume24hr") or 0)
            if liq < cfg.min_liquidity and vol < cfg.min_volume_24h:
                continue
            preferred = _is_preferred(slug, question)
            if cfg.prefer_crypto_weather and not preferred and vol < cfg.min_volume_24h * 3:
                # Keep some non-preferred depth for pure arb, but require more volume.
                if vol < cfg.min_volume_24h * 5:
                    continue
            out.append(
                _ArbMarket(
                    condition_id=str(m.get("conditionId") or ""),
                    slug=slug,
                    question=question,
                    outcome_a=pair[0],
                    outcome_b=pair[1],
                    liquidity=liq,
                    volume_24h=vol,
                    lp_reward_score=_lp_reward_score(m),
                    preferred=preferred,
                )
            )
        except Exception:
            continue

    def _rank(row: _ArbMarket) -> tuple:
        reward = row.lp_reward_score if cfg.prefer_lp_rewards else 0.0
        return (
            1 if row.preferred else 0,
            reward,
            row.volume_24h,
            row.liquidity,
        )

    out.sort(key=_rank, reverse=True)
    return out


def _open_pairs(positions: list[Position]) -> dict[str, dict[str, Position]]:
    """Group open legs by condition_id → outcome → position."""
    grouped: dict[str, dict[str, Position]] = defaultdict(dict)
    for pos in positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue
        key = pos.market_condition_id or pos.market_slug
        grouped[key][pos.outcome.lower()] = pos
    return grouped


def _quote_pair(
    engine: Engine,
    market: _ArbMarket,
    settings: Settings,
) -> _ArbQuote | None:
    cfg = settings.arbitrage
    try:
        full = engine.api.get_market(market.slug)
        token_a = full.get_token_id(market.outcome_a)
        token_b = full.get_token_id(market.outcome_b)
        book_a = engine.api.get_order_book(token_a)
        book_b = engine.api.get_order_book(token_b)
    except Exception as e:
        log.debug("arbitrage book %s: %s", market.slug, e)
        return None
    ask_a, size_a = best_ask(book_a)
    ask_b, size_b = best_ask(book_b)
    if ask_a is None or ask_b is None:
        return None
    if size_a < cfg.min_ask_size or size_b < cfg.min_ask_size:
        return None
    if not (cfg.min_ask <= ask_a <= cfg.max_ask and cfg.min_ask <= ask_b <= cfg.max_ask):
        return None
    pair_cost = ask_a + ask_b
    # Allow maker posts when asks are only slightly over $1 (spread capture).
    if pair_cost > cfg.max_maker_ask_sum + 1e-9:
        return None
    edge = 1.0 - min(pair_cost, cfg.max_pair_cost) - cfg.fee_buffer
    return _ArbQuote(
        market=market,
        ask_a=ask_a,
        ask_b=ask_b,
        size_a=size_a,
        size_b=size_b,
        pair_cost=pair_cost,
        edge=edge,
    )


def analyze_arbitrage(
    engine: Engine,
    settings: Settings,
    *,
    paper_mode: bool = False,
) -> list[Signal]:
    """Emit paired YES+NO (or Up+Down) buys when combined ask locks an edge under $1."""
    cfg = settings.arbitrage
    # Always scan PH arb (logs signals / blocked) even when we cannot open new pairs.
    ph_slugs = _scan_predictionhunt_arb(engine, settings)

    positions = engine.db.get_open_positions()
    pairs = _open_pairs(positions)
    # Only complete (2-leg) pairs consume capacity — orphans are unwound and must
    # not permanently block new entries (legacy lose-leg exits left many orphans).
    complete_pairs = sum(1 for legs in pairs.values() if len(legs) >= 2)
    orphan_count = sum(1 for legs in pairs.values() if len(legs) == 1)
    open_pair_count = complete_pairs
    if complete_pairs >= cfg.max_open_pairs:
        _log_arb(
            engine,
            decision="skip",
            reason="max_open_pairs",
            open_pairs=complete_pairs,
            orphans=orphan_count,
            max_open_pairs=cfg.max_open_pairs,
            ph_slugs=len(ph_slugs),
        )
        return []

    bankroll = account_cash(engine, cfg.starting_balance or settings.starting_balance)
    remaining_slots = max(1, cfg.max_open_pairs - complete_pairs)
    pair_budget = scaled_size(
        cfg.position_usd,
        cash=bankroll,
        starting_balance=cfg.starting_balance or settings.starting_balance,
        remaining_slots=remaining_slots,
        min_usd=settings.min_position_usd * 2,
        max_usd=cfg.max_position_usd,
    )
    if pair_budget is None:
        _log_arb(
            engine,
            decision="skip",
            reason="insufficient_cash",
            cash=bankroll,
            ph_slugs=len(ph_slugs),
        )
        return []

    markets = discover_arb_markets(engine, settings)
    if ph_slugs:
        existing = {m.slug for m in markets}
        ph_markets = _markets_from_ph_slugs(engine, settings, ph_slugs - existing)
        # Prefer PH-flagged markets first.
        flagged = [replace(m, preferred=True) for m in markets if m.slug in ph_slugs]
        rest = [m for m in markets if m.slug not in ph_slugs]
        markets = ph_markets + flagged + rest
    _log_arb(
        engine,
        decision="scan",
        reason=(
            f"arbitrage scan: {len(markets)} binary candidates / "
            f"budget=${pair_budget:.2f} / open_pairs={open_pair_count}"
            + (f" / orphans={orphan_count}" if orphan_count else "")
            + (f" / ph_slugs={len(ph_slugs)}" if ph_slugs else "")
        ),
        candidates=len(markets),
        open_pairs=open_pair_count,
        orphans=orphan_count,
        pair_budget=pair_budget,
        ph_slugs=len(ph_slugs),
    )

    signals: list[Signal] = []
    rejects: dict[str, int] = defaultdict(int)
    for market in markets:
        if open_pair_count + (len(signals) // 2) >= cfg.max_open_pairs:
            break
        if market.condition_id and market.condition_id in pairs:
            rejects["already_in"] += 1
            continue
        if any(p.market_slug == market.slug for legs in pairs.values() for p in legs.values()):
            rejects["already_in"] += 1
            continue

        quote = _quote_pair(engine, market, settings)
        if quote is None:
            rejects["no_quote"] += 1
            continue

        # Same fee/edge gates in paper and live so paper P&L matches live fills.
        taker_cap = cfg.max_pair_cost
        taker_ok = quote.pair_cost + cfg.fee_buffer <= taker_cap + 1e-9
        taker_ok = taker_ok and (1.0 - quote.pair_cost) >= cfg.min_edge
        # Take clear edges with FAK (paper walks book; live CLOB FAK). paper_fak=false
        # forces maker-only for paper sims that want resting-only behavior.
        if paper_mode and not cfg.paper_fak:
            use_fak = False
        else:
            use_fak = bool(taker_ok)

        if use_fak:
            limit_a = round(quote.ask_a, 4)
            limit_b = round(quote.ask_b, 4)
            order_type = "fak"
            pair_ref = quote.pair_cost
        else:
            # Maker spread-capture: post both legs so limits sum to max_pair_cost.
            # Split budget proportional to asks (cheaper leg gets more shares notionally).
            target_sum = cfg.max_pair_cost
            # Keep limits at/below ask so they can rest as bids into the book.
            raw_a = min(quote.ask_a, target_sum * (quote.ask_a / quote.pair_cost))
            raw_b = target_sum - raw_a
            if raw_b > quote.ask_b:
                raw_b = quote.ask_b
                raw_a = target_sum - raw_b
            tick = cfg.maker_tick
            limit_a = round(max(cfg.min_ask, min(raw_a, quote.ask_a) - (0 if taker_ok else 0)), 4)
            limit_b = round(max(cfg.min_ask, min(raw_b, quote.ask_b)), 4)
            # Shave a tick on both when asks are above target (true maker).
            if not taker_ok:
                limit_a = round(max(cfg.min_ask, min(quote.ask_a - tick, target_sum * 0.5)), 4)
                limit_b = round(max(cfg.min_ask, target_sum - limit_a), 4)
                if limit_b >= quote.ask_b:
                    limit_b = round(max(cfg.min_ask, quote.ask_b - tick), 4)
                    limit_a = round(max(cfg.min_ask, target_sum - limit_b), 4)
            if limit_a + limit_b > cfg.max_pair_cost + 1e-9:
                rejects["limit_sum_too_high"] += 1
                continue
            if (1.0 - (limit_a + limit_b)) < cfg.min_edge:
                rejects["edge_too_small"] += 1
                continue
            order_type = "limit"
            pair_ref = limit_a + limit_b

        # Paper maker: fill at the posted limit so the book isn't an empty sim.
        # Live still rests GTC until the quote is hit.
        fill_at_limit = bool(paper_mode and order_type == "limit")

        # Equal shares so $1 payout covers both legs regardless of winner.
        # Cap to a fraction of top-of-book so we don't walk thin asks.
        book_frac = max(0.1, min(1.0, float(getattr(cfg, "book_fill_fraction", 0.5))))
        target_shares = pair_budget / pair_ref
        max_by_book = min(quote.size_a, quote.size_b) * book_frac
        target_shares = min(target_shares, max_by_book)
        amount_a = round(target_shares * limit_a, 2)
        amount_b = round(target_shares * limit_b, 2)
        if amount_a < settings.min_position_usd or amount_b < settings.min_position_usd:
            rejects["size_too_small"] += 1
            continue
        if amount_a + amount_b > bankroll:
            rejects["insufficient_cash"] += 1
            break

        locked_edge = 1.0 - (limit_a + limit_b)
        reason = (
            f"arb pair {market.outcome_a}/{market.outcome_b} "
            f"sum={limit_a + limit_b:.3f} edge={locked_edge:.3f} "
            f"${amount_a + amount_b:.2f} shares≈{target_shares:.1f} "
            f"({'LP+' if market.lp_reward_score > 0 else ''}"
            f"{'crypto/wx' if market.preferred else 'general'})"
        )
        _log_arb(
            engine,
            decision="buy",
            reason=reason,
            slug=market.slug,
            ask_a=quote.ask_a,
            ask_b=quote.ask_b,
            limit_a=limit_a,
            limit_b=limit_b,
            pair_cost=round(limit_a + limit_b, 4),
            edge=round(locked_edge, 4),
            stake_usd=round(amount_a + amount_b, 2),
            lp_reward_score=market.lp_reward_score,
            preferred=market.preferred,
        )

        quant = QuantMeta(
            p=locked_edge,
            sigma=0.0,
            f_star=locked_edge,
            kelly_fraction=0.0,
            source="arbitrage",
        )
        signals.append(
            Signal(
                action="buy",
                slug=market.slug,
                outcome=market.outcome_a.lower(),
                amount_usd=amount_a,
                order_type=order_type,
                limit_price=limit_a,
                paper_fill_at_limit=fill_at_limit,
                market_condition_id=market.condition_id or None,
                quant=quant,
                reason=reason + f" leg={market.outcome_a}",
            )
        )
        signals.append(
            Signal(
                action="buy",
                slug=market.slug,
                outcome=market.outcome_b.lower(),
                amount_usd=amount_b,
                order_type=order_type,
                limit_price=limit_b,
                paper_fill_at_limit=fill_at_limit,
                market_condition_id=market.condition_id or None,
                quant=quant,
                reason=reason + f" leg={market.outcome_b}",
            )
        )

    if not signals and rejects:
        _log_arb(
            engine,
            decision="skip",
            reason="no_arb_window",
            rejects=dict(rejects),
            markets_scanned=len(markets),
        )
    return signals


def arbitrage_exits(engine: Engine, settings: Settings) -> list[Signal]:
    """Exit arb pairs by selling BOTH legs when overall MTM is in profit.

    Incomplete (orphan) pairs are still unwound. Ladder / lose-leg / rebalance
    directional exits are intentionally disabled — they broke the locked hedge.
    """
    from papertrader.arbitrage_state import ArbExitStore

    cfg = settings.arbitrage
    positions = engine.db.get_open_positions()
    pairs = _open_pairs(positions)
    store = ArbExitStore(engine.db.data_dir)
    store.prune_closed(positions)
    signals: list[Signal] = []
    min_sell_usd = float(settings.min_position_usd)
    min_profit_pct = float(getattr(cfg, "min_pair_profit_pct", 0.005))
    sum_exit = float(getattr(cfg, "pair_bid_sum_exit", 0.99))

    def _leg_book(pos: Position) -> tuple[float | None, float | None]:
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
        except Exception:
            return None, None
        bid, _ = best_bid(book)
        ask, _ = best_ask(book)
        return bid, ask

    def _leg_cost(pos: Position) -> float:
        cost = float(getattr(pos, "total_cost", 0.0) or 0.0)
        if cost > 0:
            return cost
        return float(pos.shares) * float(pos.avg_entry_price or 0.0)

    def _sell(
        pos: Position,
        shares: float,
        *,
        reason: str,
        bid: float | None,
    ) -> Signal | None:
        sell_shares = min(float(pos.shares), float(shares))
        if sell_shares <= 0:
            return None
        px = float(bid) if bid is not None and bid > 0 else 0.0
        if px > 0 and sell_shares * px < min_sell_usd and sell_shares < pos.shares - 1e-9:
            return None
        _log_arb(
            engine,
            decision="sell",
            reason=reason,
            slug=pos.market_slug,
            outcome=pos.outcome,
            shares=round(sell_shares, 4),
            bid=px or None,
            partial_exit=False,
        )
        return Signal(
            action="sell",
            slug=pos.market_slug,
            outcome=pos.outcome,
            shares=sell_shares,
            order_type="fak",
            # Market FAK when book is empty/thin so orphans never stick forever.
            limit_price=px if px >= 0.01 else None,
            partial_exit=False,
            market_condition_id=pos.market_condition_id,
            reason=reason,
        )

    for key, legs in pairs.items():
        if len(legs) < 2:
            for _outcome, pos in legs.items():
                bid, _ask = _leg_book(pos)
                bid_txt = f"{bid:.3f}" if bid is not None else "none"
                reason = (
                    f"arb orphan exit {_outcome} bid={bid_txt} "
                    f"(incomplete pair on {pos.market_slug})"
                )
                sig = _sell(pos, pos.shares, reason=reason, bid=bid)
                if sig:
                    signals.append(sig)
            continue

        quoted: list[tuple[str, Position, float]] = []
        for outcome, pos in legs.items():
            bid, _ask = _leg_book(pos)
            if bid is None:
                continue
            quoted.append((outcome, pos, bid))
        if len(quoted) < 2:
            continue

        condition_id = key or (
            quoted[0][1].market_condition_id or quoted[0][1].market_slug
        )
        market_slug = quoted[0][1].market_slug

        mtm = sum(bid * float(pos.shares) for _o, pos, bid in quoted)
        cost = sum(_leg_cost(pos) for _o, pos, _b in quoted)
        bid_sum = sum(bid for _o, _p, bid in quoted)
        profit_ok = cost > 0 and mtm >= cost * (1.0 + min_profit_pct)
        sum_ok = bid_sum + 1e-9 >= sum_exit
        if not (profit_ok or sum_ok):
            continue

        for outcome, pos, bid in quoted:
            store.set_baseline(
                condition_id, outcome, pos.shares, market_slug=market_slug
            )
            pnl = mtm - cost
            reason = (
                f"arb pair profit exit {outcome} bid={bid:.3f} "
                f"mtm=${mtm:.2f} cost=${cost:.2f} pnl=${pnl:.2f} "
                f"bid_sum={bid_sum:.3f}"
            )
            sig = _sell(pos, pos.shares, reason=reason, bid=bid)
            if sig:
                signals.append(sig)

    return signals
