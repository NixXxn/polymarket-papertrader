from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.markets import temperature_event_slug
from papertrader.signals import Signal
from papertrader.strategies.weatherlock import analyze_weatherlock_event, weatherlock_exits
from papertrader.weatherlock_state import WeatherlockExitStore


def test_weatherlock_settings_defaults():
    settings = load_settings()
    assert settings.weatherlock.buy_min == 0.88
    assert settings.weatherlock.buy_max == 0.91
    assert settings.weatherlock.sell_limit == 0.99
    assert settings.weatherlock.take_profit_offset == 0.07
    assert settings.weatherlock.stop_bid == 0.72
    assert settings.weatherlock.starting_balance == 500
    assert settings.weatherlock.include_lowest is True
    assert "nyc" in settings.weatherlock.cities
    assert "denver" in settings.weatherlock.cities


def test_lowest_temperature_event_slug():
    assert (
        temperature_event_slug("nyc", date(2026, 9, 15), kind="lowest")
        == "lowest-temperature-in-nyc-on-september-15-2026"
    )
    assert (
        temperature_event_slug("miami", date(2026, 9, 15))
        == "highest-temperature-in-miami-on-september-15-2026"
    )


def test_weatherlock_exits_places_take_profit(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path / "weatherlock"
    engine.db.data_dir.mkdir()

    pos = SimpleNamespace(
        shares=25.0,
        is_resolved=False,
        avg_entry_price=0.90,
        market_condition_id="cond-wl-1",
        outcome="no",
        market_slug="highest-temperature-in-denver-on-september-15-2026-78f",
    )
    sigs = weatherlock_exits(engine, settings, [pos])
    assert len(sigs) == 1
    sig = sigs[0]
    assert isinstance(sig, Signal)
    assert sig.action == "sell"
    assert sig.order_type == "limit"
    assert sig.limit_price == 0.97  # 0.90 + 0.07
    assert sig.outcome == "no"
    assert sig.weatherlock_take_profit is True

    store = WeatherlockExitStore(engine.db.data_dir)
    store.mark_take_profit(
        "cond-wl-1", "no", market_slug=pos.market_slug, take_profit_price=0.97
    )
    assert store.take_profit_placed("cond-wl-1", "no")
    assert weatherlock_exits(engine, settings, [pos], exit_store=store) == []


def test_weatherlock_exits_caps_at_sell_limit(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path / "weatherlock"
    engine.db.data_dir.mkdir()
    pos = SimpleNamespace(
        shares=25.0,
        is_resolved=False,
        avg_entry_price=0.95,
        market_condition_id="cond-wl-2",
        outcome="no",
        market_slug="highest-temperature-in-denver-on-september-15-2026-80f",
    )
    # 0.95 + 0.06 = 1.01 → capped at sell_limit 0.99
    sigs = weatherlock_exits(engine, settings, [pos])
    assert len(sigs) == 1
    assert sigs[0].limit_price == 0.99


def test_analyze_weatherlock_buys_no_in_band(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path / "weatherlock"
    engine.db.data_dir.mkdir()
    engine.db.get_account.return_value = SimpleNamespace(cash=500.0)

    market = MagicMock()
    market.slug = "highest-temperature-in-nyc-on-september-15-2026-72f"
    market.condition_id = "cond-nyc"
    market.get_token_id.return_value = "token-no"

    book = SimpleNamespace(asks=[SimpleNamespace(price=0.90, size=50.0)], bids=[])
    engine.api.get_order_book.return_value = book

    city = settings.cities["nyc"]
    bucket = SimpleNamespace(
        event_slug="highest-temperature-in-nyc-on-september-15-2026",
        event_date=date(2026, 9, 15),
        city=city,
        market=market,
        bucket_text="72°F",
        event_volume=500.0,
    )

    monkeypatch.setattr(
        "papertrader.strategies.weatherlock.best_ask",
        lambda _book: (0.90, 50.0),
    )
    sigs = analyze_weatherlock_event(
        engine,
        city,
        date(2026, 9, 15),
        [bucket],
        settings,
        [],
        today=date(2026, 9, 15),
        paper_mode=True,
    )
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.action == "buy"
    assert sig.outcome == "no"
    assert sig.order_type == "limit"
    assert sig.limit_price == 0.90
    assert sig.paper_fill_at_limit is True
    market.get_token_id.assert_called_with("no")


def test_analyze_weatherlock_skips_outside_band(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path / "weatherlock"
    engine.db.data_dir.mkdir()
    engine.db.get_account.return_value = SimpleNamespace(cash=500.0)

    market = MagicMock()
    market.slug = "highest-temperature-in-nyc-on-september-15-2026-72f"
    market.condition_id = "cond-nyc"
    market.get_token_id.return_value = "token-no"
    engine.api.get_order_book.return_value = SimpleNamespace(asks=[], bids=[])

    city = settings.cities["nyc"]
    bucket = SimpleNamespace(
        event_slug="highest-temperature-in-nyc-on-september-15-2026",
        event_date=date(2026, 9, 15),
        city=city,
        market=market,
        bucket_text="72°F",
        event_volume=500.0,
    )
    monkeypatch.setattr(
        "papertrader.strategies.weatherlock.best_ask",
        lambda _book: (0.97, 50.0),
    )
    assert (
        analyze_weatherlock_event(
            engine,
            city,
            date(2026, 9, 15),
            [bucket],
            settings,
            [],
            today=date(2026, 9, 15),
        )
        == []
    )


def test_weatherlock_stop_exits_on_collapsed_bid(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path / "weatherlock"
    engine.db.data_dir.mkdir()
    market = MagicMock()
    market.get_token_id.return_value = "token-no"
    engine.api.get_market.return_value = market
    engine.api.get_order_book.return_value = SimpleNamespace(bids=[], asks=[])

    pos = SimpleNamespace(
        shares=25.0,
        is_resolved=False,
        avg_entry_price=0.90,
        market_condition_id="cond-wl-sl",
        outcome="no",
        market_slug="highest-temperature-in-denver-on-september-15-2026-78f",
    )
    monkeypatch.setattr(
        "papertrader.strategies.weatherlock.best_bid",
        lambda _book: (0.70, 10.0),
    )
    sigs = weatherlock_exits(engine, settings, [pos])
    assert len(sigs) == 1
    assert sigs[0].action == "sell"
    assert sigs[0].order_type == "fak"
    assert "SL" in (sigs[0].reason or "")
