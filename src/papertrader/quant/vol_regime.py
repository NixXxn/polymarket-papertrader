"""Persisted per-market vol trackers for adaptive Kelly (past-only)."""

from __future__ import annotations

import json
from pathlib import Path

from papertrader.quant.adaptive_kelly import VolRegimeSnapshot, VolRegimeTracker


class VolRegimeStore:
    def __init__(
        self,
        data_dir: Path | str,
        *,
        rolling_window: int = 36,
        recent_window: int = 9,
        min_observations: int = 8,
        regime_floor: float = 0.0,
        regime_cap: float = 1.0,
    ) -> None:
        self._root = Path(data_dir)
        if self._root.name in (
            "safe",
            "asymmetric",
            "contrarian",
            "conviction",
            "copy",
            "edge",
            "esports",
            "fadefinder",
            "momentum",
            "meanrev",
            "volspike",
            "closingsoon",
            "btc5m",
        ):
            self._root = self._root.parent
        self._path = self._root / "vol_regime.json"
        self._rolling_window = rolling_window
        self._recent_window = recent_window
        self._min_obs = min_observations
        self._floor = regime_floor
        self._cap = regime_cap
        self._trackers: dict[str, VolRegimeTracker] = self._load()

    def _load(self) -> dict[str, VolRegimeTracker]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, VolRegimeTracker] = {}
        for key, row in raw.items():
            if isinstance(row, dict):
                out[str(key)] = VolRegimeTracker.from_state(
                    row,
                    rolling_window=self._rolling_window,
                    recent_window=self._recent_window,
                )
        return out

    def _save(self) -> None:
        payload = {
            k: v.to_state()
            for k, v in self._trackers.items()
            if v.observations > 0
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def observe(self, slug: str, price: float) -> VolRegimeSnapshot:
        key = slug.strip()
        tracker = self._trackers.get(key)
        if tracker is None:
            tracker = VolRegimeTracker(
                rolling_window=self._rolling_window,
                recent_window=self._recent_window,
            )
            self._trackers[key] = tracker
        tracker.observe(price)
        snap = tracker.snapshot(floor=self._floor, cap=self._cap)
        self._save()
        return snap

    def ready(self, snap: VolRegimeSnapshot) -> bool:
        return snap.observations >= self._min_obs and snap.regime_multiplier is not None
