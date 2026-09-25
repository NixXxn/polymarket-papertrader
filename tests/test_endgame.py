"""Tests for endgame (sports/esports Yes/No near-expiry) strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.endgame_state import EndgameExitStore
from papertrader.strategies.endgame import (
    _is_sports_or_esports,
    _is_yes_no_market,
    analyze_endgame,
    endgame_exits,
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
    assert not _is_sports_or_esports(
        {
            "slug": "btc-updown-15m-1789462800",
            "question": "Bitcoin Up or Down",
        }
    )


def test_is_yes_no_market():
    assert _is_yes_no_market({"outcomes": '["Yes","No"]'})
    assert not _is_yes_no_market({"outcomes": '["WRAITH","MORROW"]'})


def test_analyze_endgame_buys_capped_size(monkeypatch, tmp_path):
    settings = load_settings()
    assert settings.endgame.use_full_capital is False
    assert settings.endgame.price_min == 0.88
    assert settings.endgame.price_max == 0.96
    assert settings.endgame.sell_limit == 0.99
    assert settings.endgame.take_profit_offset == 0.05
    assert settings.endgame.stop_bid == 0.80
    assert settings.endgame.max_minutes == 10
    assert settings.endgame.position_usd == 60
    assert settings.endgame.paper_fill_at_limit is True
    assert settings.endgame.yes_no_only is True

    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_row = {
        "slug": "will-demo-team-win-2026-09-15",
        "question": "Will Demo Team win on 2026-09-15?",
        "endDate": end,
        "conditionId": "0xabc",
        "liquidityNum": 5000,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.90","0.10"]',
        "tags": [{"label": "Esports"}],
    }

    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)
    engine.api._gamma_get.return_value = [market_row]

    market_obj = MagicMock()
    market_obj.get_token_id.return_value = "tok-yes"
    market_obj.condition_id = "0xabc"
    engine.api.get_market.return_value = market_obj
    engine.api.get_order_book.return_value = MagicMock()

    monkeypatch.setattr(
        "papertrader.strategies.endgame.best_ask",
        lambda _book: (0.92, 2000.0),
    )

    sigs = analyze_endgame(engine, settings, now=now, paper_mode=True)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.action == "buy"
    assert sig.slug == "will-demo-team-win-2026-09-15"
    assert sig.outcome.lower() == "yes"
    assert sig.amount_usd == 60.0
    assert sig.order_type == "limit"
    assert sig.limit_price == 0.92
    assert sig.paper_fill_at_limit is True


def test_analyze_endgame_rejects_team_name_moneyline(monkeypatch, tmp_path):
    settings = load_settings()
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_row = {
        "slug": "cs2-wraith-morrow-2026-09-15",
        "question": "CS2: Wraith vs Morrow",
        "endDate": end,
        "conditionId": "0xteam",
        "liquidityNum": 5000,
        "outcomes": '["Wraith","Morrow"]',
        "outcomePrices": '["0.90","0.10"]',
        "tags": [{"label": "Esports"}],
    }
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)
    engine.api._gamma_get.return_value = [market_row]
    logged = []
    monkeypatch.setattr(
        "papertrader.strategies.endgame.log_decision",
        lambda data_dir, **kwargs: logged.append(kwargs),
    )
    assert analyze_endgame(engine, settings, now=now, paper_mode=True) == []
    scan = next(r for r in logged if r.get("decision") == "scan")
    assert scan["rejects"]["not_yes_no"] >= 1


def test_analyze_endgame_logs_outside_window_sports(monkeypatch, tmp_path):
    settings = load_settings()
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_row = {
        "slug": "will-nba-demo-win-2026-09-15",
        "question": "Will NBA Demo win?",
        "endDate": end,
        "liquidityNum": 5000,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.90","0.10"]',
        "tags": [{"label": "Sports"}],
    }
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)
    engine.api._gamma_get.return_value = [market_row]

    logged = []

    def _log(data_dir, **kwargs):
        logged.append(kwargs)

    monkeypatch.setattr("papertrader.strategies.endgame.log_decision", _log)
    sigs = analyze_endgame(engine, settings, now=now)
    assert sigs == []
    scan = next(r for r in logged if r.get("decision") == "scan")
    assert scan["sports_seen"] == 1
    assert scan["rejects"]["outside_trade_window"] == 1
    assert "outside_" in scan["reason"]


def test_analyze_endgame_rejects_ask_at_or_above_sell_limit(monkeypatch, tmp_path):
    settings = load_settings()
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    end = (now + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market_row = {
        "slug": "will-parity-demo-win-2026-09-15",
        "question": "Will Parity Demo win?",
        "endDate": end,
        "conditionId": "0xpar",
        "liquidityNum": 5000,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.90","0.10"]',
        "tags": [{"label": "Esports"}],
    }
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)
    engine.api._gamma_get.return_value = [market_row]
    market_obj = MagicMock()
    market_obj.get_token_id.return_value = "tok-yes"
    market_obj.condition_id = "0xpar"
    engine.api.get_market.return_value = market_obj
    engine.api.get_order_book.return_value = MagicMock()
    monkeypatch.setattr(
        "papertrader.strategies.endgame.best_ask",
        lambda _book: (0.98, 500.0),
    )
    logged = []
    monkeypatch.setattr(
        "papertrader.strategies.endgame.log_decision",
        lambda data_dir, **kwargs: logged.append(kwargs),
    )
    assert analyze_endgame(engine, settings, now=now, paper_mode=True) == []
    scan = next(r for r in logged if r.get("decision") == "scan")
    assert scan["rejects"]["ask_out_of_band"] == 1


def test_endgame_exits_place_soft_take_profit(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    pos = SimpleNamespace(
        shares=100.0,
        is_resolved=False,
        market_slug="lol-demo",
        outcome="Yes",
        market_condition_id="0xabc",
        avg_entry_price=0.90,
    )
    engine.api.get_market.side_effect = Exception("no book needed for tp-only path if bid high")
    store = EndgameExitStore(tmp_path)
    sigs = endgame_exits(engine, settings, [pos], exit_store=store)
    assert len(sigs) == 1
    assert sigs[0].action == "sell"
    assert sigs[0].order_type == "limit"
    # Soft TP: min(sell_limit 0.99, entry 0.90 + offset 0.05) = 0.95
    assert abs(sigs[0].limit_price - 0.95) < 1e-9
    assert sigs[0].endgame_take_profit is True
