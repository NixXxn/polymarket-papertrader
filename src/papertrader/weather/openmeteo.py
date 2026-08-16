from __future__ import annotations

from datetime import date

import httpx

from papertrader.config import City
from papertrader.weather.http import WeatherHttp


def fetch_openmeteo_high(http: WeatherHttp, city: City, event_date: date) -> float | None:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": city.lat,
        "longitude": city.lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": city.tz,
        "forecast_days": 7,
    }
    resp = http.client.get(url, params=params)
    resp.raise_for_status()
    daily = resp.json().get("daily") or {}
    times = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    key = event_date.isoformat()
    for t, h in zip(times, highs):
        if t == key and h is not None:
            return float(h)
    return None


def fetch_openmeteo_ensemble(
    http: WeatherHttp,
    city: City,
    event_date: date,
    *,
    models: str = "gfs_seamless",
) -> list[float]:
    """Member daily-max temperatures (°F). Empty if the ensemble API is unavailable."""
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"
    params = {
        "latitude": city.lat,
        "longitude": city.lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": city.tz,
        "forecast_days": 7,
        "models": models,
    }
    try:
        resp = http.client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPError:
        return []
    data = resp.json()
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    try:
        idx = times.index(event_date.isoformat())
    except ValueError:
        return []

    members: list[float] = []
    for key, series in daily.items():
        if key == "time":
            continue
        if "temperature_2m_max" not in key:
            continue
        if idx < len(series) and series[idx] is not None:
            members.append(float(series[idx]))
    return members
