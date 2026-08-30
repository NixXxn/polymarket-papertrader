from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from papertrader.predictionhunt import (
    FadeAlert,
    extract_sports_fades,
    normalize_platform_price,
    _parse_alert,
)
from papertrader.strategies.fadefinder import analyze_fade_alert
from papertrader.fadefinder_state import FadeFinderState


def test_normalize_kalshi_cents():
    assert normalize_platform_price("kalshi", 65) == pytest.approx(0.65)
    assert normalize_platform_price("polymarket", 0.42) == pytest.approx(0.42)


def test_parse_fade_alert_whale_bought_yes():
    row = {
        "id": 42,
        "alert_type": "whale_fade",
        "title": "Whale bought YES on Chiefs",
        "market_slug": "chiefs-win-super-bowl",
        "data": {"side": "buy", "outcome": "yes", "stake_usd": 12000, "price": 0.55},
    }
    alert = _parse_alert(row)
    assert alert is not None
    assert alert.fade_outcome == "no"
    assert alert.stake_usd == 12000


def test_extract_sports_fades_pm_rich():
    payload = {
        "games": [
            {
                "event_name": "Chiefs vs Bills",
                "team": "Chiefs",
                "group_id": 99,
                "markets": [
                    {
                        "platform": "polymarket",
                        "yes_ask": 0.58,
                        "source_url": "https://polymarket.com/event/nfl-chiefs-bills",
                        "market_id": "0xabc",
                    },
                    {
                        "platform": "kalshi",
                        "yes_ask": 48,
                    },
                ],
            }
        ]
    }

    def resolver(**kwargs):
        return "chiefs-moneyline-slug"

    opps = extract_sports_fades(
        payload,
        sport="nfl",
        min_dislocation=0.04,
        slug_resolver=resolver,
    )
    assert len(opps) == 1
    assert opps[0].dislocation == pytest.approx(0.10, abs=0.01)
    assert opps[0].polymarket_slug == "chiefs-moneyline-slug"


def test_analyze_fade_alert_skips_small_whale(tmp_path):
    settings = MagicMock()
    settings.starting_balance = 1000
    settings.min_position_usd = 1
    settings.fadefinder.min_whale_stake_usd = 500
    settings.fadefinder.min_no_ask = 0.1
    settings.fadefinder.max_no_ask = 0.95
    settings.fadefinder.min_yes_ask = 0.05
    settings.fadefinder.max_yes_ask = 0.9
    settings.fadefinder.position_usd = 5
    settings.fadefinder.max_position_usd = 15

    engine = MagicMock()
    engine.db.data_dir = tmp_path

    alert = FadeAlert(
        alert_id=1,
        alert_type="whale_fade",
        title="small whale",
        market_slug="test-market",
        platform="polymarket",
        fade_outcome="no",
        reference_price=0.5,
        stake_usd=100,
        created_at=None,
        raw={},
    )
    state = FadeFinderState(tmp_path)
    sig = analyze_fade_alert(
        engine,
        alert,
        settings,
        [],
        state=state,
        remaining_slots=5,
        cash=500,
        source="fade-finder",
    )
    assert sig is None
    assert state.seen_alert(1)
