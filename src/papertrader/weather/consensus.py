from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from papertrader.config import City, Settings
from papertrader.weather.http import WeatherHttp
from papertrader.weather.noaa import fetch_noaa_high
from papertrader.weather.openmeteo import fetch_openmeteo_high


@dataclass(frozen=True)
class Consensus:
    temp_f: float
    confidence: str
    noaa_temp: float | None
    om_temp: float | None
    diff: float | None
    source: str


def get_consensus(
    http: WeatherHttp,
    city: City,
    event_date: date,
    settings: Settings,
) -> Consensus | None:
    noaa_temp = None
    om_temp = None
    try:
        noaa_temp = fetch_noaa_high(http, city, event_date)
    except Exception:
        noaa_temp = None
    try:
        om_temp = fetch_openmeteo_high(http, city, event_date)
    except Exception:
        om_temp = None

    if noaa_temp is not None and om_temp is not None:
        diff = abs(noaa_temp - om_temp)
        avg = (noaa_temp + om_temp) / 2.0
        if diff > settings.forecast_disagreement_f:
            return Consensus(avg, "skip", noaa_temp, om_temp, diff, "consensus")
        if diff <= 1:
            confidence = "very_high"
        elif diff <= 2:
            confidence = "high"
        else:
            confidence = "moderate"
        return Consensus(avg, confidence, noaa_temp, om_temp, diff, "consensus")
    if noaa_temp is not None:
        return Consensus(noaa_temp, "single_source", noaa_temp, None, None, "NOAA")
    if om_temp is not None:
        return Consensus(om_temp, "single_source", None, om_temp, None, "Open-Meteo")
    return None
