from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from dataclasses import replace

from papertrader.buckets import parse_temperature_range
from papertrader.config import load_settings
from papertrader.markets import BucketMarket
from papertrader.strategies.safe import analyze_safe_event
from helpers import FakeLevel, sample_city


def test_safe_entry_on_matching_value_bucket(monkeypatch):
    base = load_settings()
    settings = replace(base, safe=replace(base.safe, max_open_positions=8))
    city = sample_city()
    event_date = date(2026, 8, 13)
    market = SimpleNamespace(
        slug="highest-temperature-in-miami-on-august-13-2026-76-77f",
        question="Will the highest temperature in Miami be 76-77°F?",
        closed=False,
        condition_id="0xabc",
        get_token_id=lambda outcome: "token-yes",
    )
    bucket = BucketMarket(
        event_slug="highest-temperature-in-miami-on-august-13-2026",
        event_date=event_date,
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text="76-77°F",
        rng=parse_temperature_range("76-77°F"),  # type: ignore[arg-type]
        event_volume=5000,
    )
    engine = MagicMock()
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(0.72, 200), FakeLevel(0.74, 500)],
        bids=[FakeLevel(0.70, 50)],
    )

    from papertrader.weather.consensus import Consensus
    import papertrader.strategies.safe as safe_mod

    monkeypatch.setattr(
        safe_mod,
        "get_consensus",
        lambda *a, **k: Consensus(76.5, "very_high", 76.0, 77.0, 1.0, "consensus"),
    )
    monkeypatch.setattr(safe_mod, "gfs_in_window", lambda: True)

    sig = analyze_safe_event(engine, MagicMock(), city, event_date, [bucket], settings, [])
    assert sig is not None
    assert sig.action == "buy"
    assert sig.order_type == "limit"
    assert sig.limit_price is not None
    assert sig.amount_usd == 100.0


def test_safe_autoscales_with_cash(monkeypatch):
    base = load_settings()
    settings = replace(base, safe=replace(base.safe, max_open_positions=8))
    city = sample_city()
    event_date = date(2026, 8, 13)
    market = SimpleNamespace(
        slug="highest-temperature-in-miami-on-august-13-2026-76-77f",
        question="Will the highest temperature in Miami be 76-77°F?",
        closed=False,
        condition_id="0xabc",
        get_token_id=lambda outcome: "token-yes",
    )
    bucket = BucketMarket(
        event_slug="highest-temperature-in-miami-on-august-13-2026",
        event_date=event_date,
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text="76-77°F",
        rng=parse_temperature_range("76-77°F"),  # type: ignore[arg-type]
        event_volume=5000,
    )
    engine = MagicMock()
    engine.get_account.return_value = SimpleNamespace(cash=100.0)
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(0.72, 100)],
        bids=[FakeLevel(0.70, 50)],
    )
    from papertrader.weather.consensus import Consensus
    import papertrader.strategies.safe as safe_mod

    monkeypatch.setattr(
        safe_mod,
        "get_consensus",
        lambda *a, **k: Consensus(76.5, "very_high", 76.0, 77.0, 1.0, "consensus"),
    )
    monkeypatch.setattr(safe_mod, "gfs_in_window", lambda: True)

    sig = analyze_safe_event(engine, MagicMock(), city, event_date, [bucket], settings, [])
    assert sig is not None
    assert sig.amount_usd == 1.0
    assert "76-77" in sig.reason


def test_safe_skips_non_whitelist_city():
    settings = load_settings()
    city = sample_city(slug="london", name="London", station="EGLC")
    sig = analyze_safe_event(
        MagicMock(), MagicMock(), city, date(2026, 8, 13), [], settings, []
    )
    assert sig is None
