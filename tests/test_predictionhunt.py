from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from papertrader.predictionhunt import (
    CrossPlatformBucket,
    PlatformQuote,
    PredictionHuntClient,
    PredictionHuntQuota,
    cross_platform_no_edge,
    extract_bucket_cross_platform,
    extract_sports_fades,
    normalize_platform_price,
    search_query_from_event_slug,
)


def test_search_query_from_event_slug():
    q = search_query_from_event_slug(
        "highest-temperature-in-london-on-august-31-2026"
    )
    assert "london" in q
    assert "august" in q
    assert "31" in q
    assert len(q) >= 3


def test_extract_bucket_cross_platform_finds_dislocation():
    payload = {
        "events": [
            {
                "groups": [
                    {
                        "title": "23°C or below",
                        "platform_count": 3,
                        "markets": [
                            {
                                "platform": "polymarket",
                                "market_id": "highest-temperature-in-london-on-august-31-2026-23c",
                                "yes_ask": 0.18,
                            },
                            {
                                "platform": "kalshi",
                                "market_id": "KXHIGHLON-23",
                                "yes_ask": 0.11,
                            },
                            {
                                "platform": "predictit",
                                "market_id": "123",
                                "yes_ask": 0.10,
                            },
                        ],
                    }
                ]
            }
        ]
    }
    hit = extract_bucket_cross_platform(
        payload,
        bucket_text="23°C",
        polymarket_slug="highest-temperature-in-london-on-august-31-2026-23c",
        source="search",
    )
    assert hit is not None
    assert hit.consensus_yes == pytest.approx(0.105, abs=0.001)
    assert hit.dislocation == pytest.approx(0.075, abs=0.001)
    assert hit.platform_count == 3


def test_cross_platform_no_edge_requires_dislocation():
    cross = CrossPlatformBucket(
        group_title="23°C",
        polymarket_yes_ask=0.18,
        consensus_yes=0.10,
        platform_count=3,
        dislocation=0.08,
        quotes=(),
        source="search",
    )
    edge, supports = cross_platform_no_edge(
        cross=cross, pm_no_ask=0.84, min_dislocation=0.02
    )
    assert edge == pytest.approx(0.06)
    assert supports is True

    weak = cross_platform_no_edge(
        cross=cross, pm_no_ask=0.84, min_dislocation=0.10
    )
    assert weak == (None, False)


def test_quota_tracks_monthly(tmp_path: Path):
    q = PredictionHuntQuota(tmp_path)
    assert q.can_spend(max_monthly=10, max_matched_monthly=2)
    q.record_request(headers={"X-RateLimit-Remaining-Month": "9"}, matched=False)
    snap = q.snapshot()
    assert snap["monthly_used"] == 1
    assert snap["remaining_month"] == 9


def test_client_uses_cache(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    class FakeResp:
        status_code = 200
        headers = {"X-RateLimit-Remaining-Month": "999"}

        @staticmethod
        def json():
            return {"events": []}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr("papertrader.predictionhunt.httpx.Client", FakeClient)
    cfg = MagicMock()
    cfg.enabled = True
    cfg.min_request_interval_seconds = 0.0
    cfg.max_monthly_requests = 100
    cfg.max_matched_monthly = 10
    cfg.cache_ttl_hours = 24
    cfg.min_cross_platform_count = 1
    cfg.min_dislocation = 0.02
    cfg.use_matching_markets = False

    client = PredictionHuntClient(tmp_path, cfg, api_key="pmx_test")
    client.lookup_bucket(
        event_slug="highest-temperature-in-london-on-august-31-2026",
        polymarket_slug="highest-temperature-in-london-on-august-31-2026-23c",
        bucket_text="23°C",
    )
    first_calls = len(calls)
    client.lookup_bucket(
        event_slug="highest-temperature-in-london-on-august-31-2026",
        polymarket_slug="highest-temperature-in-london-on-august-31-2026-23c",
        bucket_text="23°C",
    )
    assert first_calls >= 1
    assert len(calls) == first_calls


def test_extract_sports_fades_requires_dislocation():
    payload = {
        "games": [
            {
                "team": "Lakers",
                "markets": [
                    {"platform": "polymarket", "yes_ask": 0.51, "source_url": "https://x/event/nba"},
                    {"platform": "kalshi", "yes_ask": 50},
                ],
            }
        ]
    }
    opps = extract_sports_fades(
        payload,
        sport="nba",
        min_dislocation=0.05,
        slug_resolver=lambda **_: None,
    )
    assert opps == []

    opps2 = extract_sports_fades(
        payload,
        sport="nba",
        min_dislocation=0.005,
        slug_resolver=lambda **_: "lakers-slug",
    )
    assert len(opps2) == 1
    assert normalize_platform_price("kalshi", 50) == pytest.approx(0.5)
