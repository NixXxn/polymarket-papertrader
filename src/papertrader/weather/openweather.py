from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from papertrader.config import City
from papertrader.weather.http import WeatherHttp


def openweather_api_key() -> str:
    return os.environ.get("OPENWEATHER_API_KEY", "").strip()


def fetch_openweather_daily_high(
    http: WeatherHttp,
    city: City,
    event_date: date,
    api_key: str | None = None,
) -> float | None:
    """Daily max temperature (°F) from OpenWeather 5-day / 3-hour forecast."""
    key = (api_key or openweather_api_key()).strip()
    if not key:
        return None
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {
        "lat": city.lat,
        "lon": city.lon,
        "appid": key,
        "units": "imperial",
    }
    try:
        resp = http.client.get(url, params=params)
        resp.raise_for_status()
    except Exception:
        return None
    tz = ZoneInfo(city.tz)
    target = event_date
    highs: list[float] = []
    for row in resp.json().get("list") or []:
        dt = datetime.fromtimestamp(row["dt"], tz=tz)
        if dt.date() != target:
            continue
        main = row.get("main") or {}
        temp = main.get("temp_max") or main.get("temp")
        if temp is not None:
            highs.append(float(temp))
    if not highs:
        return None
    return max(highs)
