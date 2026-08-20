from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from papertrader.config import OddsPapiSettings, load_settings
from papertrader.esports_markets import EsportsCandidate
from papertrader.oddspapi import (
    FairMatch,
    OddsPapiQuota,
    find_fair_probability,
    fractional_kelly_usd,
    maker_buy_price,
    match_fair_probability,
    shin_probabilities,
)
from papertrader.strategies.esports import analyze_esports_candidate
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


def _candidate(*, ask=0.08, outcome="Beta"):
    end_at = datetime.now(timezone.utc) + timedelta(hours=1.5)
    return EsportsCandidate(
        event_slug="lol-alpha-beta-2026-08-18",
        event_title="LoL: Alpha vs Beta",
        market=_market(),
        outcome=outcome,
        end_at=end_at,
        event_volume=5000,
        ask=ask,
        ask_size=200,
    )


def test_shin_probabilities_reasonable():
    p1, p2 = shin_probabilities(1.8, 2.1)
    assert 0.45 < p1 < 0.65
    assert abs((p1 + p2) - 1.0) < 0.02


def test_match_fair_probability_maps_outcome():
    candidate = _candidate(outcome="Beta")
    fair = FairMatch(
        fixture_id="fx1",
        team1="alpha",
        team2="beta",
        fair_p1=0.38,
        fair_p2=0.62,
    )
    assert match_fair_probability(candidate, fair) == 0.62
    assert find_fair_probability(candidate, [fair]) == (fair, 0.62)


def test_oddspapi_quota_daily_reset(tmp_path):
    quota = OddsPapiQuota(tmp_path)
    assert quota.can_spend(2, max_daily=8, max_monthly=245)
    quota.spend(2)
    snap = quota.snapshot()
    assert snap["daily_used"] == 2
    assert snap["monthly_used"] == 2


def test_value_bet_uses_fak_when_edge_sufficient(monkeypatch, tmp_path):
    monkeypatch.setenv("ODDSP_API_KEY", "test-key")
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    candidate = _candidate(ask=0.08)
    fair = FairMatch(
        fixture_id="fx1",
        team1="alpha",
        team2="beta",
        fair_p1=0.30,
        fair_p2=0.62,
    )
    sig = analyze_esports_candidate(
        engine, candidate, settings, [], fair_matches=[fair]
    )
    assert sig is not None
    assert sig.order_type == "fak"
    assert sig.limit_price is None
    assert "oddspapi" in sig.reason


def test_value_bet_falls_back_to_swing_on_low_edge(monkeypatch, tmp_path):
    monkeypatch.setenv("ODDSP_API_KEY", "test-key")
    settings = load_settings()
    settings = replace(
        settings,
        esports=replace(
            settings.esports,
            oddspapi=replace(settings.esports.oddspapi, require_match=False),
        ),
    )
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    candidate = _candidate(ask=0.40)
    fair = FairMatch(
        fixture_id="fx1",
        team1="alpha",
        team2="beta",
        fair_p1=0.30,
        fair_p2=0.42,
    )
    sig = analyze_esports_candidate(
        engine, candidate, settings, [], fair_matches=[fair]
    )
    assert sig is not None
    assert sig.order_type == "fak"
    assert "live swing" in sig.reason


def test_value_bet_skips_low_edge_when_match_required(monkeypatch, tmp_path):
    monkeypatch.setenv("ODDSP_API_KEY", "test-key")
    settings = load_settings()
    assert settings.esports.oddspapi.require_match is True
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    candidate = _candidate(ask=0.40)
    fair = FairMatch(
        fixture_id="fx1",
        team1="alpha",
        team2="beta",
        fair_p1=0.30,
        fair_p2=0.42,
    )
    sig = analyze_esports_candidate(
        engine, candidate, settings, [], fair_matches=[fair]
    )
    assert sig is None


def test_no_swing_when_match_required_without_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDSP_API_KEY", raising=False)
    settings = load_settings()
    assert settings.esports.oddspapi.require_match is True
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    sig = analyze_esports_candidate(engine, _candidate(), settings, [], fair_matches=[])
    assert sig is None


def test_swing_fallback_without_fair_match(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDSP_API_KEY", raising=False)
    settings = load_settings()
    settings = replace(
        settings,
        esports=replace(
            settings.esports,
            oddspapi=replace(settings.esports.oddspapi, require_match=False),
        ),
    )
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    sig = analyze_esports_candidate(engine, _candidate(), settings, [], fair_matches=[])
    assert sig is not None
    assert sig.order_type == "fak"


def test_maker_buy_price_and_kelly():
    price = maker_buy_price(ask=0.10, fair_p=0.62, maker_edge_cents=0.02)
    assert price == 0.09
    stake = fractional_kelly_usd(
        fair_p=0.62,
        price=price,
        cash=500.0,
        kelly_fraction=0.25,
        max_usd=25.0,
        min_usd=1.0,
    )
    assert stake is not None
    assert stake >= 1.0
