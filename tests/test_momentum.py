from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.buckets import parse_temperature_range
from papertrader.config import load_settings
from papertrader.markets import BucketMarket
from papertrader.momentum_state import MomentumExitStore
from papertrader.strategies.momentum import TokenWatch, analyze_momentum_entry, momentum_exits
from papertrader.weather_ws_client import MarketTick, parse_market_message, parse_market_update
from helpers import sample_city


def _watch(*, slug="highest-temperature-in-nyc-on-august-18-2026-90-91f", label="90-91°F"):
    city = sample_city(name="NYC", slug="nyc", station="KLGA", strategies=("momentum",))
    market = SimpleNamespace(
        slug=slug,
        question="Will the highest temperature in NYC be 90-91°F?",
        closed=False,
        condition_id="0xmom",
        outcomes=["yes", "no"],
        get_token_id=lambda outcome: "token-yes" if outcome == "yes" else "token-no",
    )
    bucket = BucketMarket(
        event_slug="highest-temperature-in-nyc-on-august-18-2026",
        event_date=date(2026, 8, 18),
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text=label,
        rng=parse_temperature_range("90-91°F"),  # type: ignore[arg-type]
        event_volume=5000,
    )
    return TokenWatch(
        token_id="token-yes",
        event_slug="highest-temperature-in-nyc-on-august-18-2026",
        event_date=date(2026, 8, 18),
        city=city,
        bucket=bucket,
        label=label,
    )


def test_parse_market_update_extracts_prices():
    tick = parse_market_update(
        {
            "asset_id": "abc123",
            "bids": [{"price": "0.88"}],
            "asks": [{"price": "0.91"}],
            "price": "0.90",
        }
    )
    assert tick is not None
    assert tick.token_id == "abc123"
    assert tick.best_bid == 0.88
    assert tick.best_ask == 0.91
    assert tick.last_price == 0.90


def test_parse_market_message_handles_list_payload():
    ticks = parse_market_message(
        '[{"asset_id":"t1","asks":[{"price":"0.92"}],"bids":[{"price":"0.90"}]}]'
    )
    assert len(ticks) == 1
    assert ticks[0].best_ask == 0.92


def test_momentum_entry_triggers_at_threshold(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    watch = _watch()
    tick = MarketTick(token_id="token-yes", best_bid=0.89, best_ask=0.91, last_price=0.90)
    sig = analyze_momentum_entry(engine, watch, tick, settings, [])
    assert sig is not None
    assert sig.action == "buy"
    assert sig.order_type == "fak"
    assert sig.limit_price is None
    assert sig.amount_usd is not None
    assert sig.amount_usd > 0


def test_momentum_skips_when_below_threshold(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    watch = _watch()
    tick = MarketTick(token_id="token-yes", best_bid=0.70, best_ask=0.72, last_price=0.71)
    assert analyze_momentum_entry(engine, watch, tick, settings, []) is None


def test_momentum_take_profit_exit(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    watch = _watch()
    pos = SimpleNamespace(
        shares=50.0,
        market_slug=watch.bucket.market.slug,
        market_condition_id="0xmom",
        outcome="yes",
        avg_entry_price=0.91,
        is_resolved=False,
    )
    tick = MarketTick(token_id="token-yes", best_bid=0.985, best_ask=0.99, last_price=0.985)
    signals = momentum_exits(engine, watch, tick, settings, [pos])
    assert len(signals) == 1
    assert signals[0].action == "sell"
    assert signals[0].limit_price == 0.98
    assert signals[0].momentum_take_profit is True


def test_momentum_stop_loss_exit(tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    watch = _watch()
    pos = SimpleNamespace(
        shares=50.0,
        market_slug=watch.bucket.market.slug,
        market_condition_id="0xmom",
        outcome="yes",
        avg_entry_price=0.91,
        is_resolved=False,
    )
    tick = MarketTick(token_id="token-yes", best_bid=0.60, best_ask=0.62, last_price=0.60)
    signals = momentum_exits(engine, watch, tick, settings, [pos])
    assert len(signals) == 1
    assert signals[0].action == "sell"
    assert signals[0].limit_price == 0.59


def test_momentum_exit_store_roundtrip(tmp_path):
    store = MomentumExitStore(tmp_path)
    assert not store.take_profit_placed("0xabc", "yes")
    store.mark_take_profit("0xabc", "yes", market_slug="slug", take_profit_price=0.98)
    assert store.take_profit_placed("0xabc", "yes")
