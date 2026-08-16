from __future__ import annotations

from datetime import date

from papertrader.buckets import TempRange, p_bucket_ensemble, p_bucket_gaussian, horizon_sigma_f
from papertrader.config import City
from papertrader.weather.consensus import Consensus
from papertrader.weather.http import WeatherHttp
from papertrader.weather.openmeteo import fetch_openmeteo_ensemble


def p_high_in_bucket(
    http: WeatherHttp,
    city: City,
    event_date: date,
    rng: TempRange,
    consensus: Consensus | None,
    days_ahead: int,
) -> tuple[float, str]:
    """Return (probability, source). Ensemble preferred; Gaussian fallback."""
    members = fetch_openmeteo_ensemble(http, city, event_date)
    if len(members) >= 8:
        return p_bucket_ensemble(members, rng), "ensemble"
    if consensus is None:
        return 0.0, "none"
    sigma = horizon_sigma_f(days_ahead)
    return p_bucket_gaussian(consensus.temp_f, sigma, rng), "gaussian"


def ensemble_p95(members: list[float]) -> float | None:
    if not members:
        return None
    ordered = sorted(members)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]
