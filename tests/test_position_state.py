from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from papertrader.quant.monitor import monitor_exits
from papertrader.quant.position_state import PositionExitStore
from papertrader.strategies.asymmetric import asymmetric_exits
from helpers import FakeLevel, sample_city


def _denver_city():
    return sample_city(
        name="Denver",
        slug="denver",
        station="KDEN",
        lat=39.8561,
        lon=-104.6737,
        tz="America/Denver",
        strategies=("asymmetric",),
    )


def _position(*, shares=10.0, entry=0.05, condition_id="0xtail"):
    return SimpleNamespace(
        shares=shares,
        market_slug="highest-temperature-in-denver-on-august-13-2026-100f-or-higher",
        market_question="Will the highest temperature in Denver be 100°F or higher?",
        market_condition_id=condition_id,
        outcome="yes",
        avg_entry_price=entry,
        is_resolved=False,
    )


def test_position_exit_store_partial_tp_roundtrip(tmp_path):
    store = PositionExitStore(tmp_path)
    assert not store.partial_tp_done("0xabc", "yes")
    store.mark_partial_tp("0xabc", "yes", market_slug="slug-a")
    assert store.partial_tp_done("0xabc", "yes")
    store.unmark_partial_tp("0xabc", "yes")
    assert not store.partial_tp_done("0xabc", "yes")


def test_position_exit_store_ladder_levels(tmp_path):
    store = PositionExitStore(tmp_path)
    assert not store.ladder_level_hit("0xabc", "yes", 2.0)
    store.mark_ladder_level("0xabc", "yes", 2.0, market_slug="slug-a")
    store.mark_ladder_level("0xabc", "yes", 5.0, market_slug="slug-a")
    assert store.ladder_level_hit("0xabc", "yes", 2.0)
    assert store.ladder_level_hit("0xabc", "yes", 5.0)
    store.unmark_ladder_level("0xabc", "yes", 2.0)
    assert not store.ladder_level_hit("0xabc", "yes", 2.0)
    assert store.ladder_level_hit("0xabc", "yes", 5.0)


def test_monitor_exits_partial_tp_only_once(tmp_path, monkeypatch):
    city = _denver_city()
    pos = _position()
    engine = SimpleNamespace(
        api=SimpleNamespace(
            get_market=lambda slug: SimpleNamespace(get_token_id=lambda o: "token-yes"),
            get_order_book=lambda token: SimpleNamespace(
                bids=[FakeLevel(0.12, 100)],
                asks=[FakeLevel(0.14, 50)],
            ),
        )
    )
    store = PositionExitStore(tmp_path)
    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    signals = monitor_exits(
        engine,  # type: ignore[arg-type]
        [pos],  # type: ignore[arg-type]
        {"denver": city},
        now=now,
        exit_store=store,
    )
    assert len(signals) == 1
    assert signals[0].partial_exit is True
    assert signals[0].shares == pytest.approx(1.0)
    assert signals[0].ladder_multiple == 2.0
    assert store.ladder_level_hit("0xtail", "yes", 2.0)

    again = monitor_exits(
        engine,  # type: ignore[arg-type]
        [pos],  # type: ignore[arg-type]
        {"denver": city},
        now=now,
        exit_store=store,
    )
    assert again == []


def test_asymmetric_exits_skips_duplicate_partial_tp(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from papertrader.config import load_settings
    from papertrader.weather.ensemble import EnsembleForecast

    settings = load_settings()
    city = _denver_city()
    pos = _position()
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

    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    first = asymmetric_exits(
        engine, MagicMock(), settings, [pos], {"denver": city}, now=now
    )
    assert len(first) == 2
    assert all(s.partial_exit for s in first)
    assert "ladder trim" in first[0].reason

    second = asymmetric_exits(
        engine, MagicMock(), settings, [pos], {"denver": city}, now=now
    )
    assert second == []
