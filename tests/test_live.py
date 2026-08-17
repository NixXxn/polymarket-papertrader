from __future__ import annotations

from types import SimpleNamespace

import pytest
from pm_trader.engine import Engine
from pm_trader.models import OrderRejectedError

from papertrader.live import LiveTrader, parse_balance, parse_clob_fill, record_fill
from papertrader.loop import execute_signal
from papertrader.signals import Signal


class FakeLiveClient:
    def __init__(self, resp: dict | None = None, balance: float | None = 80.0):
        self.resp = resp or {
            "success": True,
            "status": "matched",
            "takingAmount": "2.5",
            "makingAmount": "1.25",
        }
        self.balance = balance
        self.orders: list[dict] = []

    def get_balance(self) -> float | None:
        return self.balance

    def market_order(self, **kwargs) -> dict:
        self.orders.append({"kind": "market", **kwargs})
        return self.resp

    def limit_order(self, **kwargs) -> dict:
        self.orders.append({"kind": "limit", **kwargs})
        return self.resp

    def get_open_orders(self) -> list[dict]:
        return []

    def get_trades(self) -> list[dict]:
        return []


def test_parse_balance_shapes():
    assert parse_balance({"balance": "12.5"}) == 12.5
    assert parse_balance({"balance": "12500000"}) == 12.5
    assert parse_balance({"data": {"available": 3}}) == 3.0
    assert parse_balance(None) is None


def test_parse_clob_fill_buy_taking_making():
    avg, shares, usd = parse_clob_fill(
        {"success": True, "takingAmount": "2.5", "makingAmount": "1.25"},
        side="BUY",
    )
    assert shares == 2.5
    assert usd == 1.25
    assert avg == 0.5


def test_parse_clob_fill_rejects_unmatched():
    with pytest.raises(OrderRejectedError):
        parse_clob_fill({"success": True, "status": "unmatched"}, side="BUY")


def test_execute_signal_live_records_fill(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    market = SimpleNamespace(
        condition_id="0xcond",
        slug="highest-temperature-in-wellington-on-august-15-2026-14c",
        question="Wellington 14C?",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: "token-yes",
        tick_size=0.01,
        neg_risk=False,
    )
    engine.api.get_market = lambda slug: market  # type: ignore[method-assign]
    engine.api.get_tick_size = lambda token_id: 0.01  # type: ignore[method-assign]
    client = FakeLiveClient()
    live = LiveTrader(client)
    sig = Signal(
        action="buy",
        slug=market.slug,
        outcome="yes",
        amount_usd=1.25,
        reason="test live buy",
    )
    assert execute_signal(engine, sig, dry_run=False, live=live)
    assert engine.get_account().cash == pytest.approx(48.75)
    pos = engine.db.get_open_positions()
    assert len(pos) == 1
    assert pos[0].shares == pytest.approx(2.5)
    assert client.orders[0]["side"] == "BUY"
    engine.close()


def test_execute_signal_live_limit_buy(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    market = SimpleNamespace(
        condition_id="0xcond",
        slug="highest-temperature-in-wellington-on-august-15-2026-14c",
        question="Wellington 14C?",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: "token-yes",
        tick_size=0.01,
        neg_risk=False,
    )
    engine.api.get_market = lambda slug: market  # type: ignore[method-assign]
    engine.api.get_tick_size = lambda token_id: 0.01  # type: ignore[method-assign]
    client = FakeLiveClient()
    live = LiveTrader(client)
    sig = Signal(
        action="buy",
        slug=market.slug,
        outcome="yes",
        amount_usd=1.25,
        order_type="limit",
        limit_price=0.50,
        reason="test live limit buy",
    )
    assert execute_signal(engine, sig, dry_run=False, live=live)
    assert client.orders[0]["kind"] == "limit"
    assert client.orders[0]["price"] == 0.50
    assert client.orders[0]["size"] == pytest.approx(2.5)
    assert engine.get_account().cash == pytest.approx(48.75)
    engine.close()


def test_execute_signal_live_limit_resting_does_not_fill_ledger(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    market = SimpleNamespace(
        condition_id="0xcond",
        slug="m",
        question="q",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: "token-yes",
        tick_size=0.01,
        neg_risk=False,
    )
    engine.api.get_market = lambda slug: market  # type: ignore[method-assign]
    engine.api.get_tick_size = lambda token_id: 0.01  # type: ignore[method-assign]
    client = FakeLiveClient(resp={"success": True, "status": "live", "orderID": "abc"})
    live = LiveTrader(client)
    sig = Signal(
        action="buy",
        slug="m",
        outcome="yes",
        amount_usd=5.0,
        order_type="limit",
        limit_price=0.10,
        reason="resting limit",
    )
    assert execute_signal(engine, sig, dry_run=False, live=live)
    assert engine.get_account().cash == 50.0
    assert engine.db.get_open_positions() == []
    engine.close()


def test_live_buy_rejects_when_wallet_is_short(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    market = SimpleNamespace(
        condition_id="0xcond",
        slug="m",
        question="q",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: "token-yes",
        tick_size=0.01,
        neg_risk=False,
    )
    engine.api.get_market = lambda slug: market  # type: ignore[method-assign]
    engine.api.get_tick_size = lambda token_id: 0.01  # type: ignore[method-assign]
    live = LiveTrader(FakeLiveClient(balance=1.0))
    sig = Signal(action="buy", slug="m", outcome="yes", amount_usd=5.0, reason="too big")
    assert execute_signal(engine, sig, dry_run=False, live=live) is False
    assert engine.get_account().cash == 50.0
    engine.close()


def test_record_fill_buy_and_sell(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(10.0)
    market = SimpleNamespace(
        condition_id="0xcond",
        slug="m",
        question="q",
    )
    record_fill(
        engine,
        market=market,
        outcome="yes",
        side="buy",
        avg_price=0.5,
        shares=4,
        usd=2,
    )
    record_fill(
        engine,
        market=market,
        outcome="yes",
        side="sell",
        avg_price=0.6,
        shares=4,
        usd=2.4,
    )
    assert engine.get_account().cash == pytest.approx(10.4)
    assert engine.db.get_open_positions() == []
    engine.close()
