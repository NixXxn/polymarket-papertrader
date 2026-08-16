from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from papertrader.buckets import celsius_to_fahrenheit
from papertrader.config import City
from papertrader.weather.http import WeatherHttp


def fetch_metar_observed_high(
    http: WeatherHttp,
    city: City,
    event_date: date,
    now: datetime | None = None,
) -> float | None:
    """Running daily high (°F) from METAR observations at the resolution station."""
    now = now or datetime.now(timezone.utc)
    url = "https://aviationweather.gov/api/data/metar"
    params = {"ids": city.station, "format": "json", "hours": 24, "taf": "false"}
    try:
        resp = http.client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    payload = resp.json()
    if not isinstance(payload, list):
        return None
    tz = ZoneInfo(city.tz)
    highs: list[float] = []
    for obs in payload:
        temp_c = obs.get("temp")
        if temp_c is None:
            continue
        obs_time = obs.get("obsTime") or obs.get("reportTime")
        if not obs_time:
            continue
        try:
            dt = datetime.fromisoformat(str(obs_time).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.astimezone(tz).date() != event_date:
            continue
        highs.append(celsius_to_fahrenheit(float(temp_c)))
    return max(highs) if highs else None
