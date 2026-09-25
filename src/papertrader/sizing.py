from __future__ import annotations

import math
from typing import Any


def account_cash(engine: Any, fallback: float) -> float:
    """Read spendable cash; fall back when the engine has no real account (tests)."""
    try:
        raw = engine.get_account().cash
    except AttributeError:
        return fallback
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return fallback
    cash = float(raw)
    if cash != cash:
        return fallback
    return max(0.0, cash)


def spendable_usd(cash: float, *, buffer: float = 0.02) -> float:
    """Cash available for a buy after float/fee dust so we never trip == balance errors."""
    room = float(cash) - float(buffer)
    if room <= 0:
        return 0.0
    return math.floor(room * 100.0) / 100.0


def budget_scale(cash: float, starting_balance: float) -> float:
    if starting_balance <= 0:
        return 1.0
    return cash / starting_balance


def scaled_size(
    base_usd: float,
    *,
    cash: float,
    starting_balance: float,
    remaining_slots: int,
    min_usd: float,
    max_usd: float | None = None,
    extra_cap: float | None = None,
    bankroll: float | None = None,
) -> float | None:
    """Size a bet as a share of current cash, using yaml sizes at starting_balance.

    Scales down when cash is tight and up after the bankroll grows. Never spends
    more than cash, and splits remaining cash across remaining position slots.
    `bankroll` is the account cash used for the up/down scale (defaults to `cash`).
    """
    spendable = spendable_usd(cash)
    if remaining_slots <= 0 or spendable < min_usd or base_usd <= 0:
        return None
    scale = budget_scale(cash if bankroll is None else bankroll, starting_balance)
    size = base_usd * scale
    if max_usd is not None:
        size = min(size, max_usd * scale)
    size = min(size, spendable / remaining_slots, spendable)
    if extra_cap is not None:
        size = min(size, extra_cap)
    size = round(size, 2)
    if size < min_usd:
        return None
    return size
