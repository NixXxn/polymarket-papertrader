from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.buckets import parse_temperature_range
from papertrader.config import load_settings
from papertrader.markets import BucketMarket
from papertrader.strategies.edge import _stop_price, analyze_edge_event, edge_exits
from helpers import FakeLevel, sample_city


def _bucket(city, text, slug_suffix, volume=8000):
    rng = parse_temperature_range(text)
    market = SimpleNamespace(
        slug=f"highest-temperature-in-miami-on-august-13-2026-{slug_suffix}",
        question=f"Will the highest temperature in Miami be {text}?",
        closed=False,
        condition_id=f"0x{slug_suffix}",
        get_token_id=lambda outcome: f"tok-{slug_suffix}",
    )
    return BucketMarket(
        event_slug="highest-temperature-in-miami-on-august-13-2026",
        event_date=date(2026, 8, 13),
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text=text,
        rng=rng,  # type: ignore[arg-type]
        event_volume=volume,
    )


def _mid_book(ask=0.48, bid=0.46, size=80):
    return SimpleNamespace(asks=[FakeLevel(ask, size)], bids=[FakeLevel(bid, 40)])


def _patch_model(monkeypatch, p=0.52):
    import papertrader.strategies.edge as edge_mod
    from papertrader.weather.consensus import Consensus

    monkeypatch.setattr(
        edge_mod,
        "get_consensus",
        lambda *a, **k: Consensus(88.0, "high", 88.0, 88.0, 0.0, "consensus"),
    )
    monkeypatch.setattr(edge_mod, "p_high_in_bucket", lambda *a, **k: (p, "gaussian"))


def test_stop_price_uses_44_sell_bias_around_48_entry():
    settings = load_settings()
    assert _stop_price(0.48, settings) == 0.44
    assert _stop_price(0.45, settings) == 0.41


def test_edge_buys_liquid_midboard_near_48(monkeypatch):
    settings = load_settings()
    city = sample_city()
    favorite = _bucket(city, "90-91°F", "rich")
    grind = _bucket(city, "88-89°F", "mid")
    tail = _bucket(city, "94-95°F", "tail")

    def books(token_id):
        if "rich" in token_id:
            return _mid_book(ask=0.70, bid=0.68)
        if "tail" in token_id:
            return _mid_book(ask=0.04, bid=0.03)
        return _mid_book()

    engine = MagicMock()
    engine.api.get_order_book.side_effect = books
    _patch_model(monkeypatch, p=0.52)

    sigs = analyze_edge_event(
        engine, MagicMock(), city, date(2026, 8, 13), [favorite, grind, tail], settings, []
    )
    assert len(sigs) == 1
    assert sigs[0].action == "buy"
    assert "88-89" in sigs[0].reason
    assert "grind" in sigs[0].reason
    assert sigs[0].amount_usd == 2.0


def test_edge_autoscales_with_cash(monkeypatch):
    settings = load_settings()
    city = sample_city()
    grind = _bucket(city, "88-89°F", "mid")
    engine = MagicMock()
    engine.get_account.return_value = SimpleNamespace(cash=25.0)
    engine.api.get_order_book.return_value = _mid_book()
    _patch_model(monkeypatch, p=0.52)

    sigs = analyze_edge_event(
        engine, MagicMock(), city, date(2026, 8, 13), [grind], settings, []
    )
    assert len(sigs) == 1
    assert sigs[0].amount_usd == 1.0


def test_edge_skips_penny_tails_and_thin_edge(monkeypatch):
    settings = load_settings()
    city = sample_city()
    tail = _bucket(city, "40-41°F", "dead")
    engine = MagicMock()
    engine.api.get_order_book.return_value = _mid_book(ask=0.04, bid=0.03)
    _patch_model(monkeypatch, p=0.001)

    sigs = analyze_edge_event(
        engine, MagicMock(), city, date(2026, 8, 13), [tail], settings, []
    )
    assert sigs == []


def test_edge_skips_wide_spread(monkeypatch):
    settings = load_settings()
    city = sample_city()
    grind = _bucket(city, "88-89°F", "mid")
    engine = MagicMock()
    engine.api.get_order_book.return_value = _mid_book(ask=0.48, bid=0.40, size=80)
    _patch_model(monkeypatch, p=0.55)

    sigs = analyze_edge_event(
        engine, MagicMock(), city, date(2026, 8, 13), [grind], settings, []
    )
    assert sigs == []


def test_edge_take_profit_and_sell_bias(monkeypatch):
    settings = load_settings()
    city = sample_city()
    pos = SimpleNamespace(
        shares=10.0,
        avg_entry_price=0.48,
        market_slug="highest-temperature-in-miami-on-august-13-2026-88-89f",
        market_question="Will the highest temperature in Miami be 88-89°F?",
        outcome="yes",
        is_resolved=False,
        total_cost=4.8,
    )
    engine = MagicMock()
    import papertrader.strategies.edge as edge_mod

    monkeypatch.setattr(edge_mod, "fetch_metar_observed_high", lambda *a, **k: None)
    monkeypatch.setattr(edge_mod, "fetch_openmeteo_ensemble", lambda *a, **k: [])
    monkeypatch.setattr(edge_mod, "ensemble_p95", lambda *a, **k: None)
    monkeypatch.setattr(
        edge_mod, "is_mathematically_impossible", lambda *a, **k: (False, "")
    )

    engine.api.get_market.return_value.get_token_id.return_value = "tok"
    engine.api.get_order_book.return_value = _mid_book(ask=0.59, bid=0.58)
    tp = edge_exits(engine, MagicMock(), settings, [pos], {city.slug: city})
    assert len(tp) == 1
    assert tp[0].action == "sell"
    assert "take profit" in tp[0].reason

    engine.api.get_order_book.return_value = _mid_book(ask=0.45, bid=0.43)
    cut = edge_exits(engine, MagicMock(), settings, [pos], {city.slug: city})
    assert len(cut) == 1
    assert "sell bias" in cut[0].reason


def test_edge_rotates_legacy_tails(monkeypatch):
    settings = load_settings()
    city = sample_city()
    pos = SimpleNamespace(
        shares=20.0,
        avg_entry_price=0.04,
        market_slug="highest-temperature-in-miami-on-august-13-2026-94-95f",
        market_question="Will the highest temperature in Miami be 94-95°F?",
        outcome="yes",
        is_resolved=False,
        total_cost=0.8,
    )
    engine = MagicMock()
    import papertrader.strategies.edge as edge_mod

    monkeypatch.setattr(edge_mod, "fetch_metar_observed_high", lambda *a, **k: None)
    monkeypatch.setattr(edge_mod, "fetch_openmeteo_ensemble", lambda *a, **k: [])
    monkeypatch.setattr(edge_mod, "ensemble_p95", lambda *a, **k: None)
    monkeypatch.setattr(
        edge_mod, "is_mathematically_impossible", lambda *a, **k: (False, "")
    )
    engine.api.get_market.return_value.get_token_id.return_value = "tok"
    engine.api.get_order_book.return_value = _mid_book(ask=0.05, bid=0.03, size=40)

    sigs = edge_exits(engine, MagicMock(), settings, [pos], {city.slug: city})
    assert len(sigs) == 1
    assert "grind rotation" in sigs[0].reason
