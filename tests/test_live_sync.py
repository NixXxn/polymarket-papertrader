from __future__ import annotations

from types import SimpleNamespace

import pytest
from pm_trader.engine import Engine

from papertrader.live_sync import (
    LiveSyncState,
    parse_clob_trade,
    register_clob_response,
    sync_live_orders,
)
from papertrader.trade_log import build_activity_feed, load_activity_log


class FakeClobClient:
    def __init__(
        self,
        *,
        trades: list[dict] | None = None,
        open_orders: list[dict] | None = None,
    ):
        self.trades = trades or []
        self.open_orders = open_orders or []

    def get_trades(self) -> list[dict]:
        return self.trades

    def get_open_orders(self) -> list[dict]:
        return self.open_orders


def test_parse_clob_trade_micro_size():
    trade = {
        "id": "t1",
        "side": "BUY",
        "price": "0.5",
        "size": "2500000",
        "market": "0xcond",
        "outcome": "Yes",
        "status": "TRADE_STATUS_CONFIRMED",
    }
    parsed = parse_clob_trade(trade)
    assert parsed is not None
    assert parsed["shares"] == pytest.approx(2.5)
    assert parsed["usd"] == pytest.approx(1.25)


def test_sync_bootstrap_marks_existing_trades(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    client = FakeClobClient(
        trades=[
            {
                "id": "old-1",
                "side": "BUY",
                "price": "0.4",
                "size": "10",
                "market": "0xcond",
                "outcome": "Yes",
                "status": "TRADE_STATUS_CONFIRMED",
            }
        ]
    )
    result = sync_live_orders(client, engine, strategy="copy")
    assert result.bootstrap is True
    assert result.fills_applied == 0
    state = LiveSyncState.load(tmp_path)
    assert state.initialized is True
    assert "old-1" in state.seen_trade_ids
    engine.close()


def test_sync_applies_new_trade_for_known_order(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    market = SimpleNamespace(
        condition_id="0xcond",
        slug="market-slug",
        question="Q?",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: "token-yes",
    )
    engine.api.get_market = lambda slug: market  # type: ignore[method-assign]

    register_clob_response(
        tmp_path,
        strategy="copy",
        resp={"orderID": "order-1", "tradeIDs": ["seen-1"]},
    )
    state = LiveSyncState.load(tmp_path)
    state.initialized = True
    state.save(tmp_path)

    client = FakeClobClient(
        trades=[
            {
                "id": "trade-2",
                "taker_order_id": "order-1",
                "side": "BUY",
                "price": "0.5",
                "size": "4",
                "market": "0xcond",
                "outcome": "Yes",
                "status": "TRADE_STATUS_CONFIRMED",
            }
        ],
        open_orders=[
            {
                "id": "order-1",
                "side": "BUY",
                "price": "0.5",
                "original_size": "4",
                "size_matched": "4",
                "status": "matched",
            }
        ],
    )
    result = sync_live_orders(client, engine, strategy="copy")
    assert result.fills_applied == 1
    assert engine.get_account().cash == pytest.approx(48.0)
    pos = engine.db.get_open_positions()
    assert len(pos) == 1
    assert pos[0].shares == pytest.approx(4.0)
    logs = load_activity_log(tmp_path)
    assert any(row.get("event") == "live_fill_synced" for row in logs)
    engine.close()


def test_build_activity_feed_merges_sources(tmp_path):
    from papertrader.trade_log import append_activity, append_skipped
    from papertrader.signals import Signal

    append_activity(tmp_path, level="info", event="test", message="hello", strategy="copy")
    append_skipped(
        tmp_path,
        strategy="copy",
        signal=Signal(action="buy", slug="s", outcome="yes", reason="r"),
        error="boom",
    )
    feed = build_activity_feed(tmp_path)
    events = {row.get("event") for row in feed}
    assert "test" in events
    assert "skipped_trade" in events
