from __future__ import annotations

from dataclasses import dataclass

from papertrader.quant.kelly import KellySizingEngine


@dataclass(frozen=True)
class CorrelatedBet:
    p: float
    price: float
    edge: float
    label: str


def allocate_correlated_kelly(
    bets: list[CorrelatedBet],
    bankroll: float,
    *,
    kelly_divisor: float = 4.0,
    max_usd_per_bet: float,
    min_usd: float,
    max_event_fraction: float,
) -> list[tuple[CorrelatedBet, float]]:
    """Quarter-Kelly per bet, scaled when multiple correlated legs share one event."""
    engine = KellySizingEngine(
        kelly_divisor=kelly_divisor,
        max_usd=max_usd_per_bet,
        min_usd=min_usd,
    )
    allocated: list[tuple[CorrelatedBet, float]] = []
    for bet in bets:
        result = engine.compute(bet.p, bet.price, bankroll)
        if result.skipped or result.stake_usd is None:
            continue
        allocated.append((bet, result.stake_usd))
    if not allocated:
        return []
    total = sum(stake for _, stake in allocated)
    cap = round(bankroll * max_event_fraction, 2)
    if total <= cap or cap <= 0:
        return allocated
    scale = cap / total
    return [(bet, round(stake * scale, 2)) for bet, stake in allocated if stake * scale >= min_usd]
