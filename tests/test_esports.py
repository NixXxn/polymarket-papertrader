from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.esports_markets import (
    EsportsCandidate,
    _is_prop_market,
    _looks_like_match_market,
    discover_esports_markets,
)
from papertrader.esports_state import EsportsExitStore
from papertrader.strategies.esports import analyze_esports_candidate, esports_exits
from helpers import FakeLevel


def _market(**kwargs):
    base = dict(
        slug="lol-alpha-beta-2026-08-18-game1",
        question="LoL: Alpha vs Beta - Game 1 Winner",
        closed=False,
        condition_id="0xesports",
        outcomes=["Alpha", "Beta"],
    )
    base.update(kwargs)
    m = SimpleNamespace(**base)
    m.get_token_id = lambda outcome: f"tok-{outcome.lower()}"
    return m


def _candidate(*, ask=0.08, end_hours=1.5):
    end_at = datetime.now(timezone.utc) + timedelta(hours=end_hours)
    return EsportsCandidate(
        event_slug="lol-alpha-beta-2026-08-18",
        event_title="LoL: Alpha vs Beta",
        market=_market(),
        outcome="Beta",
        end_at=end_at,
        event_volume=5000,
        ask=ask,
        ask_size=200,
    )


def test_match_and_prop_filters():
    assert _looks_like_match_market("LoL: Alpha vs Beta", "lol-alpha-beta-2026-08-18-game1")
    assert not _looks_like_match_market("Total maps", "lol-alpha-beta-both-teams-win")
    assert _is_prop_market("lol-alpha-beta-both-teams-win", ())
    assert not _is_prop_market("lol-alpha-beta-2026-08-18-game1", ())


def test_analyze_esports_buy_cheap(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    sig = analyze_esports_candidate(engine, _candidate(), settings, [])
    assert sig is not None
    assert sig.action == "buy"
    assert sig.limit_price == 0.08
    assert sig.order_type == "limit"


def test_analyze_esports_skips_when_max_positions(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    open_pos = [
        SimpleNamespace(
            market_condition_id=f"0x{i}",
            outcome="yes",
            shares=1.0,
            market_slug=f"slug-{i}",
        )
        for i in range(settings.esports.max_open_positions)
    ]
    assert analyze_esports_candidate(engine, _candidate(), settings, open_pos) is None


def test_esports_exits_place_2x_limit(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.api.get_market.return_value = _market()
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(0.12, 50)],
        bids=[FakeLevel(0.06, 50)],
    )
    pos = SimpleNamespace(
        shares=25.0,
        market_slug="lol-alpha-beta-2026-08-18-game1",
        market_condition_id="0xesports",
        outcome="Beta",
        avg_entry_price=0.05,
        is_resolved=False,
    )
    signals = esports_exits(engine, settings, [pos])
    assert len(signals) == 1
    assert signals[0].action == "sell"
    assert signals[0].limit_price == 0.1
    assert signals[0].esports_take_profit is True


def test_esports_exit_store_roundtrip(tmp_path):
    store = EsportsExitStore(tmp_path)
    assert not store.take_profit_placed("0xabc", "beta")
    store.mark_take_profit("0xabc", "beta", market_slug="slug", take_profit_price=0.1)
    assert store.take_profit_placed("0xabc", "beta")
    store.unmark_take_profit("0xabc", "beta")
    assert not store.take_profit_placed("0xabc", "beta")


def test_discover_esports_skips_out_of_horizon(monkeypatch):
    settings = load_settings()
    engine = MagicMock()
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    far_end = (now + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    near_end = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_near = {
        "slug": "lol-alpha-beta-2026-08-18",
        "title": "LoL: Alpha vs Beta",
        "volume": 5000,
        "closed": False,
        "markets": [
            {
                "slug": "lol-alpha-beta-2026-08-18-game1",
                "question": "LoL: Alpha vs Beta - Game 1 Winner",
                "closed": False,
                "active": True,
                "endDate": near_end,
                "conditionId": "0xnear",
                "outcomes": '["Alpha", "Beta"]',
                "clobTokenIds": '["tok-a", "tok-b"]',
            }
        ],
    }
    event_far = {
        **event_near,
        "markets": [{**event_near["markets"][0], "endDate": far_end, "conditionId": "0xfar"}],
    }

    def _gamma_get(path, params=None):
        if path == "/public-search":
            return {"events": [event_far, event_near]}
        if path == "/events":
            return [event_near]
        return {}

    engine.api._gamma_get.side_effect = _gamma_get
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(0.08, 100)],
        bids=[FakeLevel(0.06, 50)],
    )
    found = discover_esports_markets(engine, settings, now=now)
    assert len(found) == 1
    assert found[0].market.condition_id == "0xnear"
