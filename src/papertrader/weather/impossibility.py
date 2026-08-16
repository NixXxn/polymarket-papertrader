"""Emergency brake: a daily-high bucket is dead only if it cannot still verify."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from papertrader.buckets import TempRange, bucket_bounds_f
from papertrader.config import AsymmetricSettings, City, EdgeSettings


def remaining_rise_f(
    city: City,
    event_date: date,
    settings: EdgeSettings | AsymmetricSettings,
    now: datetime,
    observed_high_f: float | None,
) -> float:
    local = now.astimezone(ZoneInfo(city.tz))
    if local.date() < event_date:
        return 40.0  # future day: do not treat as impossible from METAR
    if local.date() > event_date:
        return 0.0
    hours_left = max(0, settings.high_hour_local - local.hour)
    return hours_left * settings.max_hourly_rise_f


def is_mathematically_impossible(
    rng: TempRange,
    *,
    city: City,
    event_date: date,
    settings: EdgeSettings | AsymmetricSettings,
    observed_high_f: float | None,
    ensemble_p95_f: float | None,
    now: datetime,
) -> tuple[bool, str]:
    lo, hi = bucket_bounds_f(rng)
    remaining = remaining_rise_f(city, event_date, settings, now, observed_high_f)

    if observed_high_f is not None:
        if hi is not None and observed_high_f > hi + 0.05:
            return True, f"observed high {observed_high_f:.1f}F already above bucket max {hi:.1f}F"
        if lo is not None:
            ceiling = observed_high_f + remaining
            if ceiling < lo - 0.05:
                return True, (
                    f"observed {observed_high_f:.1f}F + remaining {remaining:.1f}F "
                    f"cannot reach {lo:.1f}F"
                )

    if ensemble_p95_f is not None and lo is not None and remaining == 0:
        if ensemble_p95_f < lo - 0.05:
            return True, f"ensemble p95 {ensemble_p95_f:.1f}F below bucket min {lo:.1f}F with no time left"

    return False, "still possible"
