"""Conviction: event-day (d+0) weather NO fades with stricter model edge than contrarian."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import City, Settings
from papertrader.markets import BucketMarket
from papertrader.signals import Signal
from papertrader.strategies.contrarian import analyze_contrarian_event, contrarian_exits
from papertrader.weather import WeatherHttp


def _patched_settings(settings: Settings) -> Settings:
    return replace(settings, contrarian=settings.conviction)


def analyze_conviction_event(
    engine: Engine,
    http: WeatherHttp,
    city: City,
    event_date: date,
    buckets: list[BucketMarket],
    settings: Settings,
    open_positions: list[Position],
    today: date | None = None,
) -> list[Signal]:
    return analyze_contrarian_event(
        engine,
        http,
        city,
        event_date,
        buckets,
        _patched_settings(settings),
        open_positions,
        today=today,
    )


def conviction_exits(
    engine: Engine,
    http: WeatherHttp,
    settings: Settings,
    positions: list[Position],
    cities: dict[str, City],
) -> list[Signal]:
    return contrarian_exits(
        engine, http, _patched_settings(settings), positions, cities
    )
