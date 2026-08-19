from __future__ import annotations

import math


def shin_probabilities(odds1: float, odds2: float) -> tuple[float, float]:
    """Two-way Shin de-vig (decimal odds in, fair probs out)."""
    if odds1 <= 0 or odds2 <= 0:
        raise ValueError("odds must be positive")
    pi1, pi2 = 1.0 / odds1, 1.0 / odds2
    sum_pi = pi1 + pi2
    z_low, z_high = 0.0, 0.9999
    for _ in range(50):
        z = (z_low + z_high) / 2.0
        p1 = ((z**2 + 4 * (1 - z) * (pi1**2 / sum_pi)) ** 0.5 - z) / (2 * (1 - z))
        p2 = ((z**2 + 4 * (1 - z) * (pi2**2 / sum_pi)) ** 0.5 - z) / (2 * (1 - z))
        if p1 + p2 > 1.0:
            z_low = z
        else:
            z_high = z
    z = (z_low + z_high) / 2.0
    p1 = ((z**2 + 4 * (1 - z) * (pi1**2 / sum_pi)) ** 0.5 - z) / (2 * (1 - z))
    p2 = ((z**2 + 4 * (1 - z) * (pi2**2 / sum_pi)) ** 0.5 - z) / (2 * (1 - z))
    return p1, p2


def _shin_multi(implied: list[float]) -> list[float]:
    total = sum(implied)
    if total <= 0:
        return [1.0 / len(implied)] * len(implied)
    z_low, z_high = 0.0, 0.9999
    for _ in range(60):
        z = (z_low + z_high) / 2.0
        probs = [
            (math.sqrt(z * z + 4 * (1 - z) * (pi * pi / total)) - z) / (2 * (1 - z))
            for pi in implied
        ]
        if sum(probs) > 1.0:
            z_low = z
        else:
            z_high = z
    z = (z_low + z_high) / 2.0
    return [
        (math.sqrt(z * z + 4 * (1 - z) * (pi * pi / total)) - z) / (2 * (1 - z))
        for pi in implied
    ]


def shin_fair_probs_from_asks(asks: list[float]) -> list[float]:
    """Multi-outcome Shin fair probabilities from YES ask prices."""
    if not asks:
        return []
    implied = [max(a, 1e-4) for a in asks]
    total = sum(implied)
    if total <= 0:
        return [1.0 / len(implied)] * len(implied)
    if total <= 1.0 + 1e-9:
        return [pi / total for pi in implied]
    if len(implied) == 2:
        return list(shin_probabilities(1.0 / implied[0], 1.0 / implied[1]))
    return _shin_multi(implied)
