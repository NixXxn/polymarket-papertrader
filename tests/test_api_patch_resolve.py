"""Tests for Gamma closed-market lookup patch and resilient resolve."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from papertrader.api_patch import patch_polymarket_client
from papertrader.loop import _resolve, _resolve_per_position
from pm_trader.models import MarketNotFoundError


def test_get_market_retries_closed_true(monkeypatch):
    patch_polymarket_client()
    from pm_trader.api import PolymarketClient

    client = PolymarketClient.__new__(PolymarketClient)
    calls: list[dict] = []

    def fake_gamma(_path, params=None):
        calls.append(dict(params or {}))
        if params and params.get("closed") == "true":
            return [
                {
                    "slug": "old-market",
                    "conditionId": "0xabc",
                    "closed": True,
                    "active": False,
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["1", "0"]',
                    "clobTokenIds": '["t1", "t2"]',
                    "question": "Old?",
                }
            ]
        return []

    monkeypatch.setattr(client, "_gamma_get", fake_gamma)
    monkeypatch.setattr(client, "_get_cached", lambda _k: None)
    monkeypatch.setattr(client, "_set_cached", lambda _k, _v: None)
    # Avoid CLOB path for non-0x slug
    monkeypatch.setattr(
        client,
        "_clob_get",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no clob")),
    )

    market = PolymarketClient.get_market(client, "old-market")
    assert market.slug == "old-market"
    assert market.closed is True
    assert any(c.get("closed") == "true" for c in calls)


def test_resolve_falls_back_per_position_on_missing_market():
    engine = MagicMock()
    engine.resolve_all.side_effect = MarketNotFoundError("gone-slug")
    pos = SimpleNamespace(
        market_condition_id="0x1",
        market_slug="gone-slug",
        shares=10,
        outcome="yes",
    )
    engine.db.get_open_positions.return_value = [pos]
    engine.api.get_market.side_effect = MarketNotFoundError("gone-slug")

    n = _resolve(engine)
    assert n == 0
    engine.resolve_all.assert_called_once()


def test_resolve_per_position_resolves_closed_markets():
    engine = MagicMock()
    open_pos = SimpleNamespace(
        market_condition_id="0x1",
        market_slug="closed-slug",
        shares=5,
        outcome="yes",
    )
    engine.db.get_open_positions.return_value = [open_pos]
    engine.api.get_market.return_value = SimpleNamespace(closed=True, slug="closed-slug")
    engine.resolve_market.return_value = [
        SimpleNamespace(
            position=SimpleNamespace(market_slug="closed-slug"),
            payout=5.0,
        )
    ]

    n = _resolve_per_position(engine)
    assert n == 1
    engine.resolve_market.assert_called_once_with("closed-slug")
