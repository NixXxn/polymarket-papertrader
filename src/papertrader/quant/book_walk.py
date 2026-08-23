"""L2 ask walk: size and limit-price without destroying EV via slippage.

For binary $1 contracts, EV of buying at price ``c`` with win probability ``p`` is
``p - c``. Walking asks stops at the first level that would make EV negative (or
below ``min_ev``). Stake is then clipped to fractional-Kelly budget so we never
blind-buy through the book past the mathematical edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol


class _Level(Protocol):
    price: float
    size: float


@dataclass(frozen=True)
class AskWalkResult:
    """Pre-trade walk of the ask side for a BUY."""

    limit_price: float
    vwap: float
    fillable_usd: float
    fillable_shares: float
    levels_taken: int
    max_ev_price: float
    skipped: bool
    reason: str


def max_price_for_positive_ev(p_win: float, *, min_ev: float = 0.0) -> float:
    """Highest share price that still leaves EV >= min_ev (binary $1 payout)."""
    if not 0.0 < p_win < 1.0:
        raise ValueError(f"p_win must be in (0, 1), got {p_win}")
    # EV = p - price  =>  price <= p - min_ev
    return max(0.01, min(0.99, p_win - min_ev))


def _iter_asks_ascending(book: Any) -> list[_Level]:
    asks: Iterable[_Level] = getattr(book, "asks", None) or ()
    return sorted(asks, key=lambda lvl: float(lvl.price))


def walk_asks_for_buy(
    book: Any,
    *,
    p_win: float,
    budget_usd: float,
    min_ev: float = 0.0,
    hard_max_price: float | None = None,
    min_usd: float = 1.0,
) -> AskWalkResult:
    """Virtually walk asks until EV hard-limit or Kelly budget is exhausted.

    Returns a STRICT limit price (= highest ask level accepted) and the USD/shares
    that remain inside that contour. Callers must place a BUY LIMIT at
    ``limit_price`` — never an unbounded market/FAK that can slip past it.
    """
    max_ev_price = max_price_for_positive_ev(p_win, min_ev=min_ev)
    ceiling = max_ev_price
    if hard_max_price is not None:
        ceiling = min(ceiling, float(hard_max_price))

    if budget_usd < min_usd:
        return AskWalkResult(
            limit_price=0.0,
            vwap=0.0,
            fillable_usd=0.0,
            fillable_shares=0.0,
            levels_taken=0,
            max_ev_price=max_ev_price,
            skipped=True,
            reason="budget_below_min",
        )

    remaining = float(budget_usd)
    spent = 0.0
    shares = 0.0
    limit_price = 0.0
    levels_taken = 0

    for level in _iter_asks_ascending(book):
        price = float(level.price)
        size = float(level.size)
        if size <= 0 or price <= 0:
            continue
        if price > ceiling + 1e-12:
            break
        level_cost = price * size
        if level_cost <= remaining + 1e-12:
            take_shares = size
            take_cost = level_cost
        else:
            take_shares = remaining / price
            take_cost = remaining
        if take_shares <= 1e-12:
            break
        spent += take_cost
        shares += take_shares
        remaining -= take_cost
        limit_price = price
        levels_taken += 1
        if remaining <= 1e-9:
            break

    if shares <= 0 or spent < min_usd:
        return AskWalkResult(
            limit_price=0.0,
            vwap=0.0,
            fillable_usd=0.0,
            fillable_shares=0.0,
            levels_taken=0,
            max_ev_price=max_ev_price,
            skipped=True,
            reason="no_depth_inside_ev",
        )

    vwap = spent / shares
    return AskWalkResult(
        limit_price=round(limit_price, 4),
        vwap=round(vwap, 6),
        fillable_usd=round(spent, 4),
        fillable_shares=round(shares, 6),
        levels_taken=levels_taken,
        max_ev_price=round(max_ev_price, 4),
        skipped=False,
        reason="ok",
    )
