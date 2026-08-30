from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.buckets import parse_temperature_range
from papertrader.config import load_settings
from papertrader.markets import BucketMarket
from papertrader.strategies.asymmetric import analyze_asymmetric_event, asymmetric_exits
from papertrader.weather.ensemble import EnsembleForecast
from helpers import FakeLevel, sample_city


def _asymmetric_city():
    return sample_city(
        name="Denver",
        slug="denver",
        station="KDEN",
        lat=39.8561,
        lon=-104.6737,
        tz="America/Denver",
        strategies=("asymmetric",),
    )


def _tail_bucket(city, event_date, ask=0.05):
    market = SimpleNamespace(
        slug="highest-temperature-in-denver-on-august-13-2026-100f-or-higher",
        question="Will the highest temperature in Denver be 100°F or higher?",
        closed=False,
        condition_id="0xtail",
        get_token_id=lambda outcome: "token-yes",
    )
    return BucketMarket(
        event_slug="highest-temperature-in-denver-on-august-13-2026",
        event_date=event_date,
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text="100°F or higher",
        rng=parse_temperature_range("100°F or higher"),  # type: ignore[arg-type]
        event_volume=8000,
    ), ask


def test_asymmetric_entry_when_ensemble_beats_market(monkeypatch):
    from dataclasses import replace

    settings = load_settings()
    settings = replace(
        settings,
        asymmetric=replace(settings.asymmetric, max_open_positions=5),
    )
    city = _asymmetric_city()
    event_date = date(2026, 8, 13)
    bucket, ask = _tail_bucket(city, event_date)
    engine = MagicMock()
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(ask, 30), FakeLevel(0.08, 100), FakeLevel(0.20, 500)],
        bids=[FakeLevel(0.04, 20)],
    )

    import papertrader.strategies.asymmetric as asym_mod

    members = [98.0] * 5 + [101.0] * 15  # ~75% hit rate on 100°F+
    monkeypatch.setattr(
        asym_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast(tuple(members), "gfs:20"),
    )

    sig = analyze_asymmetric_event(
        engine, MagicMock(), city, event_date, [bucket], settings, [], date(2026, 8, 11)
    )
    assert sig is not None
    assert sig.action == "buy"
    assert sig.order_type == "limit"
    assert sig.limit_price is not None
    # EV ceiling ≈ 0.75; must not walk into 0.20 asks
    assert sig.limit_price <= 0.20
    assert "tail" in sig.reason
    assert "limit@" in sig.reason


def test_asymmetric_skips_when_ask_too_high(monkeypatch):
    settings = load_settings()
    city = _asymmetric_city()
    event_date = date(2026, 8, 13)
    bucket, _ = _tail_bucket(city, event_date, ask=0.25)
    engine = MagicMock()
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(0.25, 50)],
        bids=[FakeLevel(0.20, 20)],
    )
    import papertrader.strategies.asymmetric as asym_mod

    members = [101.0] * 20
    monkeypatch.setattr(
        asym_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast(tuple(members), "gfs:20"),
    )
    sig = analyze_asymmetric_event(
        engine, MagicMock(), city, event_date, [bucket], settings, [], date(2026, 8, 11)
    )
    assert sig is None


def test_asymmetric_skips_model_fade_before_event_day(monkeypatch, tmp_path):
    settings = load_settings()
    city = _asymmetric_city()
    pos = SimpleNamespace(
        shares=10.0,
        market_slug="highest-temperature-in-denver-on-august-13-2026-100f-or-higher",
        market_question="Will the highest temperature in Denver be 100°F or higher?",
        market_condition_id="0xtail",
        outcome="yes",
        avg_entry_price=0.05,
        is_resolved=False,
    )
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.api.get_market.return_value = SimpleNamespace(
        get_token_id=lambda outcome: "token-yes"
    )
    engine.api.get_order_book.return_value = SimpleNamespace(
        bids=[FakeLevel(0.08, 100)],
        asks=[FakeLevel(0.10, 50)],
    )

    import papertrader.strategies.asymmetric as asym_mod

    monkeypatch.setattr(asym_mod, "fetch_metar_observed_high", lambda *a, **k: None)
    monkeypatch.setattr(
        asym_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast((90.0,) * 10, "gfs:10"),
    )

    # Two days before event: model faded but should NOT exit yet.
    signals = asymmetric_exits(
        engine,
        MagicMock(),
        settings,
        [pos],
        {"denver": city},
        now=datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
    )
    assert signals == []


def test_asymmetric_holds_without_ladder_trims(monkeypatch, tmp_path):
    """Lottery profile: no staged ladder sells on the way up."""
    settings = load_settings()
    city = _asymmetric_city()
    pos = SimpleNamespace(
        shares=10.0,
        market_slug="highest-temperature-in-denver-on-august-13-2026-100f-or-higher",
        market_question="Will the highest temperature in Denver be 100°F or higher?",
        market_condition_id="0xtail",
        outcome="yes",
        avg_entry_price=0.05,
        is_resolved=False,
    )
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.api.get_market.return_value = SimpleNamespace(
        get_token_id=lambda outcome: "token-yes"
    )
    engine.api.get_order_book.return_value = SimpleNamespace(
        bids=[FakeLevel(0.36, 100)],
        asks=[FakeLevel(0.38, 50)],
    )

    import papertrader.strategies.asymmetric as asym_mod

    monkeypatch.setattr(asym_mod, "fetch_metar_observed_high", lambda *a, **k: None)
    monkeypatch.setattr(
        asym_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast((100.0,) * 10, "gfs:10"),
    )

    signals = asymmetric_exits(
        engine,
        MagicMock(),
        settings,
        [pos],
        {"denver": city},
        now=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
    )
    assert signals == []
