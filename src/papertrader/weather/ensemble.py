from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from papertrader.buckets import TempRange, p_bucket_ensemble
from papertrader.config import City
from papertrader.weather.http import WeatherHttp
from papertrader.weather.openmeteo import fetch_openmeteo_ensemble_detail
from papertrader.weather.openweather import fetch_openweather_daily_high, openweather_api_key

_COMBINED_MODELS = "gfs_seamless,ecmwf_ifs025"


@dataclass(frozen=True)
class EnsembleForecast:
    members_f: tuple[float, ...]
    source: str
    openweather_high_f: float | None = None
    api_error: str | None = None


def fetch_combined_ensemble(
    http: WeatherHttp,
    city: City,
    event_date: date,
) -> EnsembleForecast:
    """GFS + ECMWF ensemble members (°F daily max), optional OpenWeather spot check."""
    members, gfs, ecmwf, api_error = fetch_openmeteo_ensemble_detail(
        http, city, event_date, models=_COMBINED_MODELS
    )
    parts = []
    if gfs:
        parts.append(f"gfs:{gfs}")
    if ecmwf:
        parts.append(f"ecmwf:{ecmwf}")
    if api_error:
        source = "api_error"
    else:
        source = "+".join(parts) if parts else "none"
    ow = fetch_openweather_daily_high(http, city, event_date) if openweather_api_key() else None
    return EnsembleForecast(
        members_f=tuple(members),
        source=source,
        openweather_high_f=ow,
        api_error=api_error,
    )


def tail_bucket_probability(
    ensemble: EnsembleForecast,
    rng: TempRange,
    *,
    openweather_weight: float = 0.15,
) -> tuple[float, str]:
    """Probability the daily high lands in a tail bucket."""
    members = list(ensemble.members_f)
    if ensemble.openweather_high_f is not None and members:
        # Light blend: duplicate OpenWeather as extra pseudo-members near its reading.
        n = max(1, int(len(members) * openweather_weight))
        members.extend([ensemble.openweather_high_f] * n)
    if len(members) >= 8:
        return p_bucket_ensemble(members, rng), ensemble.source
    if members:
        return p_bucket_ensemble(members, rng), f"{ensemble.source}(thin)"
    return 0.0, "none"
