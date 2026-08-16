from __future__ import annotations

from datetime import date

from papertrader.buckets import celsius_to_fahrenheit
from papertrader.config import City
from papertrader.weather.http import WeatherHttp


def fetch_noaa_high(http: WeatherHttp, city: City, event_date: date) -> float | None:
    """Daily forecast high (°F) at the airport lat/lon via api.weather.gov."""
    if city.country != "US":
        return None
    points = http.client.get(f"https://api.weather.gov/points/{city.lat},{city.lon}")
    points.raise_for_status()
    forecast_url = points.json().get("properties", {}).get("forecast")
    if not forecast_url:
        return None
    forecast = http.client.get(forecast_url)
    forecast.raise_for_status()
    periods = forecast.json().get("properties", {}).get("periods") or []
    target = event_date.isoformat()
    highs: list[float] = []
    for p in periods:
        start = p.get("startTime") or ""
        if not start.startswith(target):
            continue
        if p.get("isDaytime") is False:
            continue
        temp = p.get("temperature")
        unit = (p.get("temperatureUnit") or "F").upper()
        if temp is None:
            continue
        temp = float(temp)
        if unit == "C":
            temp = celsius_to_fahrenheit(temp)
        highs.append(temp)
    return max(highs) if highs else None
