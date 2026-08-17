from __future__ import annotations

import time
from datetime import date
from typing import Any

import httpx

from papertrader.config import City
from papertrader.weather.http import WeatherHttp

_ENSEMBLE_MIN_INTERVAL_S = 0.55
_ENSEMBLE_MAX_RETRIES = 4


def _ensemble_cache_key(city: City, event_date: date, models: str) -> tuple[Any, ...]:
    return (round(city.lat, 4), round(city.lon, 4), city.tz, event_date.isoformat(), models)


def _throttle_ensemble(http: WeatherHttp) -> None:
    last = getattr(http, "_ensemble_last_req", 0.0)
    now = time.monotonic()
    wait = _ENSEMBLE_MIN_INTERVAL_S - (now - last)
    if wait > 0:
        time.sleep(wait)
    http._ensemble_last_req = time.monotonic()


def _parse_ensemble_members(
    data: dict[str, Any],
    event_date: date,
) -> tuple[list[float], int, int]:
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    try:
        idx = times.index(event_date.isoformat())
    except ValueError:
        return [], 0, 0

    members: list[float] = []
    gfs = ecmwf = 0
    for key, series in daily.items():
        if key == "time" or "temperature_2m_max" not in key:
            continue
        if idx >= len(series) or series[idx] is None:
            continue
        members.append(float(series[idx]))
        lk = key.lower()
        if "gfs" in lk:
            gfs += 1
        elif "ecmwf" in lk:
            ecmwf += 1
    return members, gfs, ecmwf


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
    """Member daily-max temperatures (°F). Cached per scan; empty if unavailable."""
    members, _gfs, _ecmwf, _err = fetch_openmeteo_ensemble_detail(
        http, city, event_date, models=models
    )
    return members


def fetch_openmeteo_ensemble_detail(
    http: WeatherHttp,
    city: City,
    event_date: date,
    *,
    models: str = "gfs_seamless",
) -> tuple[list[float], int, int, str | None]:
    """Return members, gfs count, ecmwf count, and optional API error text."""
    cache = getattr(http, "_ensemble_cache", None)
    if cache is None:
        http._ensemble_cache = {}
        cache = http._ensemble_cache
    key = _ensemble_cache_key(city, event_date, models)
    if key in cache:
        return cache[key]

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
    api_error: str | None = None
    data: dict[str, Any] | None = None
    for attempt in range(_ENSEMBLE_MAX_RETRIES):
        _throttle_ensemble(http)
        try:
            resp = http.client.get(url, params=params)
            if resp.status_code == 429:
                api_error = "rate_limited"
                wait = min(60.0, 5.0 * (2**attempt))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            api_error = None
            break
        except httpx.HTTPError as exc:
            api_error = str(exc)
            if attempt + 1 < _ENSEMBLE_MAX_RETRIES:
                time.sleep(2.0 * (attempt + 1))
                continue
            break

    if data is None:
        result: tuple[list[float], int, int, str | None] = ([], 0, 0, api_error or "request_failed")
        cache[key] = result
        return result

    members, gfs, ecmwf = _parse_ensemble_members(data, event_date)
    result = (members, gfs, ecmwf, None)
    cache[key] = result
    return result
