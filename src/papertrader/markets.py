from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pm_trader.engine import Engine
from pm_trader.models import ApiError, Market, MarketNotFoundError, OrderBook

from papertrader.buckets import TempRange, parse_temperature_range
from papertrader.config import City, Settings


@dataclass
class BucketMarket:
    event_slug: str
    event_date: date
    city: City
    market: Market
    bucket_text: str
    rng: TempRange
    event_volume: float


def temperature_event_slug(city_slug: str, event_date: date) -> str:
    month = calendar.month_name[event_date.month].lower()
    return f"highest-temperature-in-{city_slug}-on-{month}-{event_date.day}-{event_date.year}"


def city_from_market_slug(market_slug: str, cities: dict[str, City]) -> City | None:
    for city in sorted(cities.values(), key=lambda c: len(c.slug), reverse=True):
        if f"in-{city.slug}-on-" in market_slug:
            return city
        if market_slug.startswith(f"highest-temperature-in-{city.slug}"):
            return city
    return None


def event_slug_from_market_slug(market_slug: str) -> str:
    """Strip the bucket suffix so the event page URL works."""
    m = re.search(r"^(.*?-on-[a-z]+-\d{1,2}-\d{4})", market_slug)
    return m.group(1) if m else market_slug


def polymarket_event_url(market_slug: str) -> str:
    return f"https://polymarket.com/event/{event_slug_from_market_slug(market_slug)}"


def date_from_temp_slug(slug: str) -> date | None:
    m = re.search(r"-on-([a-z]+)-(\d{1,2})-(\d{4})", slug)
    if not m:
        return None
    month_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    months = {calendar.month_name[i].lower(): i for i in range(1, 13)}
    month = months.get(month_name)
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def event_dates(horizon_days: int, today: date | None = None) -> list[date]:
    today = today or date.today()
    return [today + timedelta(days=i) for i in range(horizon_days + 1)]


def city_local_today(city: City, now: datetime | None = None) -> date:
    """Calendar 'today' in the city's timezone (used for horizon and day-ahead math)."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(city.tz)).date()


def best_ask(book: OrderBook) -> tuple[float | None, float]:
    if not book.asks:
        return None, 0.0
    level = min(book.asks, key=lambda x: x.price)
    return level.price, level.size


def best_bid(book: OrderBook) -> tuple[float | None, float]:
    if not book.bids:
        return None, 0.0
    level = max(book.bids, key=lambda x: x.price)
    return level.price, level.size


def _market_from_event_row(row: dict) -> Market | None:
    """Build a Market from a Gamma event.markets entry, or None."""
    from pm_trader.api import _parse_market, _has_condition_id

    if not _has_condition_id(row):
        return None
    try:
        return _parse_market(row)
    except Exception:
        return None


def fetch_event(engine: Engine, slug: str) -> dict:
    try:
        data = engine.api.get_event(slug)
        if isinstance(data, dict) and (data.get("markets") or data.get("id") or data.get("slug")):
            return data
    except (ApiError, MarketNotFoundError):
        pass
    except Exception:
        pass
    try:
        data = engine.api._gamma_get("/events", params={"slug": slug})
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else {}
        if isinstance(data, dict) and data.get("markets"):
            return data
    except Exception:
        pass
    return {}


def discover_events(
    engine: Engine,
    cities: list[City],
    settings: Settings,
    today: date | None = None,
    now: datetime | None = None,
) -> list[tuple[str, date, City, list[BucketMarket], float]]:
    """Return (event_slug, date, city, buckets, event_volume) for live temperature events."""
    now = now or datetime.now(timezone.utc)
    found: list[tuple[str, date, City, list[BucketMarket], float]] = []
    for city in cities:
        anchor = today or city_local_today(city, now)
        for event_date in event_dates(settings.horizon_days, anchor):
            slug = temperature_event_slug(city.slug, event_date)
            event = fetch_event(engine, slug)
            if not event:
                continue
            volume = float(event.get("volume") or event.get("volume24hr") or 0 or 0)
            raw_markets = event.get("markets") or []
            buckets: list[BucketMarket] = []
            for row in raw_markets:
                if row.get("closed") or row.get("active") is False:
                    continue
                market = _market_from_event_row(row)
                if market is None or market.closed:
                    continue
                title = row.get("groupItemTitle") or market.question
                rng = parse_temperature_range(title) or parse_temperature_range(market.question)
                if rng is None:
                    continue
                buckets.append(
                    BucketMarket(
                        event_slug=slug,
                        event_date=event_date,
                        city=city,
                        market=market,
                        bucket_text=str(title),
                        rng=rng,
                        event_volume=volume,
                    )
                )
            if buckets:
                found.append((slug, event_date, city, buckets, volume))
    return found


def liquid_enough(
    engine: Engine,
    bucket: BucketMarket,
    settings: Settings,
    event_volume: float,
) -> bool:
    if event_volume < settings.min_event_volume:
        return False
    try:
        token = bucket.market.get_token_id("yes")
        book = engine.api.get_order_book(token)
    except Exception:
        return False
    ask, size = best_ask(book)
    if ask is None:
        return False
    return size >= settings.min_best_ask_size
