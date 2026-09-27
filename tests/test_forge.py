"""Tests for Forge (Hearth cashflow; Strike off by default after overnight wipe)."""

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
    assert s.forge.hearth_price_min == 0.91
    assert s.forge.hearth_price_max == 0.94
    assert s.forge.hearth_max_minutes == 6
    assert s.forge.strike_enabled is False
    assert s.forge.max_drawdown_halt_pct == 0.05
    assert s.forge.hearth_stop_bid == 0.70
    assert s.forge.hearth_stop_drop == 0.15


def test_forge_waterline_ratchets_up_only(tmp_path):
    store = ForgeExitStore(tmp_path)
    wl0 = store.waterline(2000.0)
    assert wl0 == 2000.0
    wl1 = store.ratchet(2500.0, 2000.0, lock_fraction=0.4)
    assert wl1 > wl0
    wl2 = store.ratchet(2100.0, 2000.0, lock_fraction=0.4)
    assert wl2 == wl1


def test_analyze_forge_hearth_buy(monkeypatch, tmp_path):
    settings = load_settings()
    now = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    monkeypatch.setattr(
        "papertrader.strategies.forge._fetch_active_binary",
        lambda *_a, **_k: [],
    )

    sigs = analyze_forge(engine, settings, now=now, paper_mode=True)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.action == "buy"
    assert "hearth" in sig.reason
    assert sig.amount_usd == 40.0
    assert sig.order_type == "limit"
    assert sig.paper_fill_at_limit is True
    store = ForgeExitStore(tmp_path)
    assert store.sleeve_of("0xforge", "Yes") == "hearth"


def test_forge_drawdown_halt_blocks_buys(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    # Down ~12.9% from $2000 — must freeze new entries.
    engine.get_account.return_value = SimpleNamespace(cash=1742.63)
    monkeypatch.setattr(
        "papertrader.strategies.forge._fetch_ending",
        lambda *_a, **_k: [{"slug": "should-not-trade"}],
    )
    sigs = analyze_forge(engine, settings, paper_mode=True)
    assert sigs == []


def test_forge_strike_dark_when_disabled(monkeypatch, tmp_path):
    settings = load_settings()
    assert settings.forge.strike_enabled is False
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=3000.0)  # surplus
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
        avg_entry_price=0.91,
    )
    store = ForgeExitStore(tmp_path)
    store.mark_entry("0xforge", "Yes", market_slug="forge-demo", sleeve="hearth")
    engine.api.get_market.side_effect = Exception("skip book for tp-only")
    sigs = forge_exits(engine, settings, [pos], exit_store=store)
    assert len(sigs) == 1
    assert sigs[0].forge_take_profit is True
    # 0.91 + 0.03 = 0.94
    assert abs(sigs[0].limit_price - 0.94) < 1e-9


def test_forge_exits_hearth_ignores_mild_dip(tmp_path):
    """0.80-style stops were the overnight killer — mild dips must hold."""
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    pos = SimpleNamespace(
        shares=40.0,
        is_resolved=False,
        market_slug="forge-dip",
        outcome="Yes",
        market_condition_id="0xdip",
        avg_entry_price=0.93,
    )
    store = ForgeExitStore(tmp_path)
    store.mark_entry("0xdip", "Yes", market_slug="forge-dip", sleeve="hearth")
    store.mark_take_profit(
        "0xdip", "Yes", market_slug="forge-dip", take_profit_price=0.96, sleeve="hearth"
    )
    market = MagicMock()
    market.get_token_id.return_value = "tok"
    engine.api.get_market.return_value = market
    engine.api.get_order_book.return_value = MagicMock()

    from papertrader.strategies import forge as forge_mod
    import papertrader.strategies.forge as fm

    # Bid 0.82 would have stopped old Forge; new floor is 0.70 / drop 0.15.
    def _bid(_book):
        return (0.82, 10.0)

    # Patch via module used by forge_exits
    orig = fm.best_bid
    fm.best_bid = _bid
    try:
        sigs = forge_exits(engine, settings, [pos], exit_store=store)
    finally:
        fm.best_bid = orig
    assert sigs == []  # TP already placed; no stop on mild dip
