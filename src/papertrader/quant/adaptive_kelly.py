"""Adaptive Kelly sizing from vol regime (past-only, no lookahead).

f* = (μ / σ_rolling²) · (1 - σ_current / σ_rolling)

Bet larger when recent vol sits below its rolling average; shrink or skip when
vol spikes. For binary markets we apply the regime multiplier to standard Kelly.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class VolRegimeSnapshot:
    sigma_current: float | None
    sigma_rolling: float | None
    regime_multiplier: float | None
    observations: int


def vol_regime_multiplier(
    sigma_current: float | None,
    sigma_rolling: float | None,
    *,
    floor: float = 0.0,
    cap: float = 1.0,
) -> float | None:
    """Scale factor: 1 when calm, →0 when σ_current ≥ σ_rolling."""
    if sigma_current is None or sigma_rolling is None or sigma_rolling <= 0:
        return None
    raw = 1.0 - sigma_current / sigma_rolling
    if raw <= 0:
        return floor
    return min(cap, raw)


def adaptive_kelly_fraction(
    mu: float,
    sigma_current: float,
    sigma_rolling: float,
) -> float:
    """Continuous Kelly with vol-regime dial (μ/σ² · (1 - σ_c/σ_r))."""
    if sigma_rolling <= 0 or sigma_current <= 0:
        return 0.0
    regime = 1.0 - sigma_current / sigma_rolling
    if regime <= 0:
        return 0.0
    return (mu / (sigma_rolling**2)) * regime


class VolRegimeTracker:
    """Rolling return vol using only bars observed so far (no future data)."""

    def __init__(
        self,
        *,
        rolling_window: int = 36,
        recent_window: int = 9,
    ) -> None:
        self._rolling_window = max(3, rolling_window)
        self._recent_window = max(2, recent_window)
        self._returns: deque[float] = deque(maxlen=self._rolling_window)
        self._last_price: float | None = None

    @property
    def observations(self) -> int:
        return len(self._returns)

    def observe(self, price: float) -> None:
        """Append one price tick; return is vs the prior tick only."""
        if price <= 0:
            return
        if self._last_price is not None and self._last_price > 0:
            ret = (price - self._last_price) / self._last_price
            self._returns.append(ret)
        self._last_price = price

    def _sigma(self, values: list[float]) -> float | None:
        if len(values) < 2:
            return None
        return statistics.pstdev(values)

    def sigma_rolling(self) -> float | None:
        return self._sigma(list(self._returns))

    def sigma_current(self) -> float | None:
        if not self._returns:
            return None
        recent = list(self._returns)[-self._recent_window :]
        return self._sigma(recent)

    def snapshot(
        self,
        *,
        floor: float = 0.0,
        cap: float = 1.0,
    ) -> VolRegimeSnapshot:
        sig_c = self.sigma_current()
        sig_r = self.sigma_rolling()
        mult = vol_regime_multiplier(sig_c, sig_r, floor=floor, cap=cap)
        return VolRegimeSnapshot(
            sigma_current=sig_c,
            sigma_rolling=sig_r,
            regime_multiplier=mult,
            observations=self.observations,
        )

    def to_state(self) -> dict:
        return {
            "returns": list(self._returns),
            "last_price": self._last_price,
        }

    @classmethod
    def from_state(
        cls,
        state: dict,
        *,
        rolling_window: int = 36,
        recent_window: int = 9,
    ) -> VolRegimeTracker:
        tracker = cls(rolling_window=rolling_window, recent_window=recent_window)
        raw = state.get("returns") or []
        if isinstance(raw, list):
            for r in raw[-tracker._rolling_window :]:
                try:
                    tracker._returns.append(float(r))
                except (TypeError, ValueError):
                    pass
        lp = state.get("last_price")
        if lp is not None:
            try:
                tracker._last_price = float(lp)
            except (TypeError, ValueError):
                pass
        return tracker
