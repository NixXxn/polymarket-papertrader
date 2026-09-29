"""Astra1 — fee-aware complete-set arbitrage with L2 capacity controls.

The invariant is simple: in a binary Polymarket market, one equal YES/NO pair
redeems for $1.  Astra1 buys both legs only when *current executable depth*,
the per-token fee schedule and a latency reserve leave a material locked edge.

This is intentionally a paper-first system.  Two CLOB orders cannot be made
atomic, so the loop keeps real execution disabled by default and immediately
unwinds/rebalances any paper orphan it observes.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.markets import best_bid
from papertrader.signals import QuantMeta, Signal
from papertrader.sizing import account_cash, scaled_size

log = logging.getLogger("papertrader")


@dataclass(frozen=True)
class AstraMarket:
    condition_id: str
    slug: str
    question: str
    outcome_a: str
    outcome_b: str
    liquidity: float
    volume_24h: float


@dataclass(frozen=True)
class Walk:
    shares: float
    cost: float
    fee: float
    worst_price: float


def v2_fee(shares: float, price: float, fee_rate_bps: int) -> float:
    """Current Polymarket V2 fee: shares × rate × price × (1-price)."""
    if shares <= 0 or price <= 0 or price >= 1 or fee_rate_bps <= 0:
        return 0.0
    return float(shares) * (float(fee_rate_bps) / 10_000.0) * price * (1.0 - price)


def _walk_asks(book: Any, shares: float, fee_rate_bps: int) -> Walk | None:
    """Price a precise share quantity through ask-side L2 depth."""
    if shares <= 0:
        return None
    remaining = float(shares)
    cost = fee = 0.0
    worst = 0.0
    levels = sorted(getattr(book, "asks", []) or [], key=lambda row: float(row.price))
    for level in levels:
        price = float(level.price)
        available = max(0.0, float(level.size))
        take = min(remaining, available)
        if take <= 0:
            continue
        cost += take * price
        fee += v2_fee(take, price, fee_rate_bps)
        worst = price
        remaining -= take
        if remaining <= 1e-9:
            return Walk(shares=float(shares), cost=cost, fee=fee, worst_price=worst)
    return None


def _capacity(book: Any) -> float:
    return sum(max(0.0, float(level.size)) for level in (getattr(book, "asks", []) or []))


def _parse_outcomes(raw: Any) -> tuple[str, str] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list):
        return None
    by_lower = {str(outcome).lower(): str(outcome) for outcome in raw}
    if "yes" in by_lower and "no" in by_lower:
        return by_lower["yes"], by_lower["no"]
    if "up" in by_lower and "down" in by_lower:
        return by_lower["up"], by_lower["down"]
    return None


def discover_astra1_markets(engine: Engine, settings: Settings) -> list[AstraMarket]:
    """Fetch liquid active binary markets without direction/category priors."""
    cfg = settings.astra1
    try:
        rows = engine.api._gamma_get(
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": cfg.scan_limit,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    except Exception as exc:
        log.debug("astra1 market fetch failed: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    markets: list[AstraMarket] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pair = _parse_outcomes(row.get("outcomes"))
        slug = str(row.get("slug") or "")
        if pair is None or not slug:
            continue
        try:
            liquidity = float(row.get("liquidity") or row.get("liquidityNum") or 0)
            volume = float(row.get("volume24hr") or 0)
        except (TypeError, ValueError):
            continue
        if liquidity < cfg.min_liquidity and volume < cfg.min_volume_24h:
            continue
        markets.append(
            AstraMarket(
                condition_id=str(row.get("conditionId") or ""),
                slug=slug,
                question=str(row.get("question") or ""),
                outcome_a=pair[0],
                outcome_b=pair[1],
                liquidity=liquidity,
                volume_24h=volume,
            )
        )
    return markets


def _log(engine: Engine, decision: str, reason: str, **extra: Any) -> None:
    log_decision(engine.db.data_dir, strategy="astra1", decision=decision, reason=reason, **extra)


def _open_pairs(positions: Iterable[Position]) -> dict[str, dict[str, Position]]:
    grouped: dict[str, dict[str, Position]] = defaultdict(dict)
    for pos in positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue
        grouped[pos.market_condition_id or pos.market_slug][pos.outcome.lower()] = pos
    return grouped


def _quote(
    engine: Engine,
    market: AstraMarket,
    shares: float,
) -> tuple[Walk, Walk, int, int] | None:
    try:
        full = engine.api.get_market(market.slug)
        token_a = full.get_token_id(market.outcome_a)
        token_b = full.get_token_id(market.outcome_b)
        book_a = engine.api.get_order_book(token_a)
        book_b = engine.api.get_order_book(token_b)
        fee_a = int(engine.api.get_fee_rate(token_a))
        fee_b = int(engine.api.get_fee_rate(token_b))
    except Exception as exc:
        log.debug("astra1 quote %s failed: %s", market.slug, exc)
        return None
    walk_a = _walk_asks(book_a, shares, fee_a)
    walk_b = _walk_asks(book_b, shares, fee_b)
    if walk_a is None or walk_b is None:
        return None
    return walk_a, walk_b, fee_a, fee_b


def _sizing_capacity(engine: Engine, market: AstraMarket) -> tuple[float, float] | None:
    """Return both-leg ask capacity, preserving a conservative depth fraction."""
    # This helper is deliberately kept tiny; it is only used from the scanner
    # after settings have already bounded desired shares.  Avoid a third book
    # fetch because the confirmation reads below are the authoritative quotes.
    try:
        full = engine.api.get_market(market.slug)
        book_a = engine.api.get_order_book(full.get_token_id(market.outcome_a))
        book_b = engine.api.get_order_book(full.get_token_id(market.outcome_b))
    except Exception:
        return None
    return _capacity(book_a), _capacity(book_b)


def analyze_astra1(engine: Engine, settings: Settings) -> list[Signal]:
    """Return exactly two equal-share FAK buys when a robust complete-set lock exists."""
    cfg = settings.astra1
    positions = engine.db.get_open_positions()
    pairs = _open_pairs(positions)
    complete = sum(1 for legs in pairs.values() if len(legs) >= 2)
    if complete >= cfg.max_open_pairs:
        _log(engine, "skip", "max_open_pairs", open_pairs=complete)
        return []
    cash = account_cash(engine, cfg.starting_balance or settings.starting_balance)
    budget = scaled_size(
        cfg.position_usd,
        cash=cash,
        starting_balance=cfg.starting_balance or settings.starting_balance,
        remaining_slots=max(1, cfg.max_open_pairs - complete),
        min_usd=settings.min_position_usd * 2,
        max_usd=cfg.max_position_usd,
    )
    if budget is None:
        _log(engine, "skip", "insufficient_cash", cash=cash)
        return []

    rejects: dict[str, int] = defaultdict(int)
    for market in discover_astra1_markets(engine, settings):
        key = market.condition_id or market.slug
        if key in pairs or any(pos.market_slug == market.slug for legs in pairs.values() for pos in legs.values()):
            rejects["already_open"] += 1
            continue
        capacity = _sizing_capacity(engine, market)
        if capacity is None:
            rejects["book_unavailable"] += 1
            continue
        max_shares = min(capacity) * max(0.01, min(1.0, cfg.book_fill_fraction))
        # A $1 pair is a conservative upper bound for initial sizing.  Exact
        # L2 cost/fees are checked below and may reduce the effective spend.
        shares = min(max_shares, budget / 1.0)
        if shares + 1e-9 < cfg.min_leg_shares:
            rejects["thin_depth"] += 1
            continue

        snapshots: list[tuple[Walk, Walk, int, int]] = []
        for _ in range(cfg.confirmation_reads):
            quote = _quote(engine, market, shares)
            if quote is None:
                break
            snapshots.append(quote)
        if len(snapshots) != cfg.confirmation_reads:
            rejects["confirm_failed"] += 1
            continue

        # Use the worst all-in observed confirmation, never the friendliest one.
        def all_in(q: tuple[Walk, Walk, int, int]) -> float:
            left, right, _fee_left, _fee_right = q
            return (left.cost + right.cost + left.fee + right.fee) / shares + cfg.latency_buffer_per_share

        selected = max(snapshots, key=all_in)
        left, right, fee_left, fee_right = selected
        first_cost = all_in(snapshots[0])
        if max(abs(all_in(q) - first_cost) for q in snapshots) > cfg.max_quote_drift:
            rejects["quote_drift"] += 1
            continue
        if not (cfg.min_leg_price <= left.worst_price <= cfg.max_leg_price and cfg.min_leg_price <= right.worst_price <= cfg.max_leg_price):
            rejects["price_band"] += 1
            continue
        total_all_in = all_in(selected)
        edge = 1.0 - total_all_in
        if total_all_in > cfg.max_all_in_cost + 1e-9 or edge + 1e-9 < cfg.min_locked_edge:
            rejects["edge_after_fees"] += 1
            continue
        if left.cost < settings.min_position_usd or right.cost < settings.min_position_usd:
            rejects["leg_too_small"] += 1
            continue
        if left.cost + right.cost + left.fee + right.fee > cash + 1e-9:
            rejects["cash_after_fees"] += 1
            continue

        reason = (
            f"Astra1 complete-set lock {market.outcome_a}/{market.outcome_b} "
            f"all_in={total_all_in:.4f} edge={edge:.4f} shares={shares:.2f} "
            f"fees=${left.fee + right.fee:.4f} buffer={cfg.latency_buffer_per_share:.3f}"
        )
        _log(
            engine,
            "buy",
            reason,
            slug=market.slug,
            shares=round(shares, 4),
            all_in_cost=round(total_all_in, 6),
            locked_edge=round(edge, 6),
            fee_bps_a=fee_left,
            fee_bps_b=fee_right,
            fee_usd=round(left.fee + right.fee, 6),
        )
        quant = QuantMeta(p=edge, sigma=0.0, f_star=edge, kelly_fraction=0.0, source="astra1")
        return [
            Signal(action="buy", slug=market.slug, outcome=market.outcome_a.lower(), amount_usd=round(left.cost, 6), order_type="fak", limit_price=left.worst_price, market_condition_id=market.condition_id or None, quant=quant, reason=reason + f" leg={market.outcome_a}"),
            Signal(action="buy", slug=market.slug, outcome=market.outcome_b.lower(), amount_usd=round(right.cost, 6), order_type="fak", limit_price=right.worst_price, market_condition_id=market.condition_id or None, quant=quant, reason=reason + f" leg={market.outcome_b}"),
        ]
    _log(engine, "skip", "no_robust_lock", rejects=dict(rejects))
    return []


def astra1_exits(engine: Engine, settings: Settings) -> list[Signal]:
    """Unwind single-leg or excess-leg risk; exit a paired position only at profit."""
    cfg = settings.astra1
    signals: list[Signal] = []
    for _key, legs in _open_pairs(engine.db.get_open_positions()).items():
        positions = list(legs.values())
        quotes: list[tuple[Position, float | None]] = []
        for pos in positions:
            try:
                market = engine.api.get_market(pos.market_slug)
                bid, _size = best_bid(engine.api.get_order_book(market.get_token_id(pos.outcome)))
            except Exception:
                bid = None
            quotes.append((pos, bid))
        if len(positions) == 1:
            pos, bid = quotes[0]
            signals.append(Signal(action="sell", slug=pos.market_slug, outcome=pos.outcome, shares=pos.shares, order_type="fak", limit_price=bid, partial_exit=False, market_condition_id=pos.market_condition_id, reason="Astra1 orphan unwind: non-atomic paired entry left one leg"))
            continue
        # Equalize any partial paired entry before it turns into directional risk.
        target = min(pos.shares for pos, _bid in quotes)
        for pos, bid in quotes:
            excess = pos.shares - target
            if excess > 1e-6:
                signals.append(Signal(action="sell", slug=pos.market_slug, outcome=pos.outcome, shares=excess, order_type="fak", limit_price=bid, partial_exit=True, market_condition_id=pos.market_condition_id, reason="Astra1 excess-leg rebalance after partial paired entry"))
        if signals:
            continue
        if len(quotes) != 2 or any(bid is None for _pos, bid in quotes):
            continue
        pair_bid_sum = sum(float(bid) for _pos, bid in quotes if bid is not None)
        cost = sum(float(pos.total_cost or pos.shares * pos.avg_entry_price) for pos, _bid in quotes)
        proceeds = sum(pos.shares * float(bid) for pos, bid in quotes if bid is not None)
        if pair_bid_sum < cfg.pair_bid_sum_exit and proceeds < cost * (1.0 + cfg.min_pair_profit_pct):
            continue
        for pos, bid in quotes:
            signals.append(Signal(action="sell", slug=pos.market_slug, outcome=pos.outcome, shares=pos.shares, order_type="fak", limit_price=bid, partial_exit=False, market_condition_id=pos.market_condition_id, reason=f"Astra1 paired-profit exit bid_sum={pair_bid_sum:.4f}"))
    return signals
