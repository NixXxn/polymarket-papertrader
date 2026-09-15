"""Tests for endgame (sports/esports near-expiry lock) strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.strategies.endgame import (
    _is_sports_or_esports,
    analyze_endgame,
)


def test_is_sports_detects_lol_and_tags():
    assert _is_sports_or_esports(
        {
            "slug": "lol-big-vs-koi-game-3-winner",
            "question": "LoL: BIG vs Movistar KOI - Game 3 Winner",
            "tags": [{"label": "Esports"}],
        }
    )
    assert _is_sports_or_esports(
        {
            "slug": "will-as-roma-win-on-2026-09-14",
            "question": "Will AS Roma win on 2026-09-14?",
            "events": [{"tags": [{"label": "Soccer"}]}],
        }
    )
    assert not _is_sports_or_esports(
        {
            "slug": "highest-temperature-in-miami-on-september-15",
            "question": "Highest temperature in Miami?",
            "tags": [{"label": "Weather"}],
        }
    )


def test_analyze_endgame_buys_full_cash_on_lock(monkeypatch):
    settings = load_settings()
    assert settings.endgame.use_full_capital is True
    assert settings.endgame.price_min == 0.97
    assert settings.endgame.max_minutes == 15

    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_row = {
        "slug": "lol-demo-game-1-winner",
        "question": "LoL: Demo vs Demo - Game 1 Winner",
        "endDate": end,
        "conditionId": "0xabc",
        "liquidityNum": 5000,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.985","0.015"]',
        "tags": [{"label": "Esports"}],
    }

    engine = MagicMock()
    engine.db.data_dir = MagicMock()
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)
    engine.api._gamma_get.return_value = [market_row]

    market_obj = MagicMock()
    market_obj.get_token_id.return_value = "tok-yes"
    engine.api.get_market.return_value = market_obj

    book = MagicMock()
    engine.api.get_order_book.return_value = book

    monkeypatch.setattr(
        "papertrader.strategies.endgame.best_ask",
        lambda _book: (0.98, 2000.0),
    )

    sigs = analyze_endgame(engine, settings, now=now)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.action == "buy"
    assert sig.slug == "lol-demo-game-1-winner"
    assert sig.outcome.lower() == "yes"
    # Full capital, capped by ask depth (0.98 * 2000 = 1960) → cash 1000
    assert sig.amount_usd == 1000.0
    assert sig.order_type == "fak"


def test_analyze_endgame_skips_outside_price_band(monkeypatch):
    settings = load_settings()
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_row = {
        "slug": "nba-demo-winner",
        "question": "NBA: Demo vs Demo Winner",
        "endDate": end,
        "liquidityNum": 5000,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.90","0.10"]',
        "tags": [{"label": "Sports"}],
    }
    engine = MagicMock()
    engine.db.data_dir = MagicMock()
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)
    engine.api._gamma_get.return_value = [market_row]

    sigs = analyze_endgame(engine, settings, now=now)
    assert sigs == []
