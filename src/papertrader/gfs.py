from __future__ import annotations

from datetime import datetime, timezone


def gfs_in_window(now: datetime | None = None) -> bool:
    """GFS runs 00/06/12/18 UTC; optimal window is +2h to +4h after each run."""
    now = now or datetime.now(timezone.utc)
    hour_frac = now.hour + now.minute / 60.0
    for run in (0, 6, 12, 18):
        if run + 2 <= hour_frac < run + 4:
            return True
    return False


def effective_edge_threshold(
    *,
    confidence: str,
    in_window: bool,
    min_edge: float,
    min_edge_high: float,
    min_edge_low: float,
) -> float:
    threshold = min_edge if in_window else min_edge_high
    if confidence == "very_high":
        return min(threshold, min_edge_low)
    if confidence == "high":
        return threshold
    if confidence in ("moderate", "single_source"):
        return max(threshold, min_edge)
    return threshold
