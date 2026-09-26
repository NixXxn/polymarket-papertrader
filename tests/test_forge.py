"""Tests for Forge (Hearth cashflow + Strike surplus breakouts)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.forge_state import ForgeExitStore
from papertrader.strategies.forge import analyze_forge, forge_exits


def test_forge_settings_loaded():
    s = load_settings()
    assert s.forge.starting_balance == 2000
    assert s.forge.hearth_price_min == 0.88
    assert s.forge.hearth_price_max == 0.96
    assert s.forge.strike_enabled is True
    assert s.forge.waterline_lock_fraction > 0
    assert s.forge.strike_min_excess >= 50


def test_forge_waterline_ratchets_up_only(tmp_path):
    store = ForgeExitStore(tmp_path)
    wl0 = store.waterline(2000.0)
    assert wl0 == 2000.0
    wl1 = store.ratchet(2500.0, 2000.0, lock_fraction=0.4)
    assert wl1 > wl0
    # Equity drawdown must not lower the waterline.
    wl2 = store.ratchet(2100.0, 2000.0, lock_fraction=0.4)
    assert wl2 == wl1


def test_analyze_forge_hearth_buy(monkeypatch, tmp_path):
    settings = load_settings()
    now = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_row = {
        "slug": "will-forge-demo-win-2026-09-26",
        "question": "Will Forge Demo win?",
        "endDate": end,
        "conditionId": "0xforge",
        "liquidityNum": 5000,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.92","0.08"]',
        "tags": [{"label": "Esports"}],
    }
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=2000.0)
    engine.api._gamma_get.return_value = [market_row]
    market_obj = MagicMock()
    market_obj.get_token_id.return_value = "tok-yes"
    market_obj.condition_id = "0xforge"
    engine.api.get_market.return_value = market_obj
    engine.api.get_order_book.return_value = MagicMock()
    monkeypatch.setattr(
        "papertrader.strategies.forge.best_ask",
        lambda _book: (0.92, 500.0),
    )
    monkeypatch.setattr(
        "papertrader.strategies.forge.best_bid",
        lambda _book: (0.90, 500.0),
    )
    # Disable strike path noise for this test.
    monkeypatch.setattr(
        "papertrader.strategies.forge._fetch_active_binary",
        lambda *_a, **_k: [],
    )

    sigs = analyze_forge(engine, settings, now=now, paper_mode=True)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.action == "buy"
    assert "hearth" in sig.reason
    assert sig.amount_usd == 80.0
    assert sig.order_type == "limit"
    assert sig.paper_fill_at_limit is True
    store = ForgeExitStore(tmp_path)
    assert store.sleeve_of("0xforge", "Yes") == "hearth"


def test_forge_strike_dark_when_no_excess(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    # Exactly at waterline — Strike must stay dark.
    engine.get_account.return_value = SimpleNamespace(cash=2000.0)
    monkeypatch.setattr(
        "papertrader.strategies.forge._fetch_ending",
        lambda *_a, **_k: [],
    )
    called = {"strike": False}

    def _strike(*_a, **_k):
        called["strike"] = True
        return []

    monkeypatch.setattr("papertrader.strategies.forge._fetch_active_binary", _strike)
    sigs = analyze_forge(engine, settings, paper_mode=True)
    assert sigs == []
    assert called["strike"] is False


def test_forge_exits_hearth_take_profit(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    pos = SimpleNamespace(
        shares=50.0,
        is_resolved=False,
        market_slug="forge-demo",
        outcome="Yes",
        market_condition_id="0xforge",
        avg_entry_price=0.90,
    )
    store = ForgeExitStore(tmp_path)
    store.mark_entry("0xforge", "Yes", market_slug="forge-demo", sleeve="hearth")
    engine.api.get_market.side_effect = Exception("skip book for tp-only")
    sigs = forge_exits(engine, settings, [pos], exit_store=store)
    assert len(sigs) == 1
    assert sigs[0].forge_take_profit is True
    assert abs(sigs[0].limit_price - 0.95) < 1e-9
