from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.accounts import make_engine
from papertrader.counter_trade import CounterTradeManager, opposite_outcome
from papertrader.execution import ExecutionContext
from papertrader.loop import execute_signal


def _market():
    return SimpleNamespace(
        condition_id="0xcounter",
        slug="binary-market",
        question="Will this happen?",
        closed=False,
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: f"token-{outcome.lower()}",
    )


def _configure(engine, market, *, yes_price: float, no_price: float) -> None:
    engine.api.get_market = MagicMock(return_value=market)
    engine.api.get_fee_rate = MagicMock(return_value=0)
    engine.api.get_order_book = MagicMock(
        side_effect=lambda token: SimpleNamespace(
            asks=[
                SimpleNamespace(
                    price=yes_price if token == "token-yes" else no_price,
                    size=100.0,
                )
            ],
            bids=[],
        )
    )


def test_opposite_outcome():
    assert opposite_outcome("YES") == "no"
    assert opposite_outcome("no") == "yes"


def test_counter_trade_mirrors_new_buy_shares_once(tmp_path):
    source = make_engine("asymmetric", tmp_path, 100.0, reset=True)
    counter = make_engine("counter-trade", tmp_path, 100.0, reset=True)
    market = _market()
    _configure(source, market, yes_price=0.30, no_price=0.70)
    _configure(counter, market, yes_price=0.30, no_price=0.70)

    # Establish the deployment baseline before the triggering source fill.
    manager = CounterTradeManager(counter, {"asymmetric": source})
    source_trade = source.buy("binary-market", "yes", 3.0, order_type="fak").trade
    assert abs(source_trade.shares - 10.0) < 1e-9

    signals, fills = manager.process_new_buys(
        dry_run=False,
        live=None,
        ctx=ExecutionContext(),
        execute=execute_signal,
    )
    assert fills == 1
    assert len(signals) == 1
    assert signals[0].outcome == "no"
    mirrored = counter.db.get_trades(limit=10)
    assert len(mirrored) == 1
    assert abs(mirrored[0].shares - source_trade.shares) < 1e-9
    assert mirrored[0].outcome.lower() == "no"

    # Persisted source-trade cursors make reprocessing (and recursion) impossible.
    signals, fills = manager.process_new_buys(
        dry_run=False,
        live=None,
        ctx=ExecutionContext(),
        execute=execute_signal,
    )
    assert signals == []
    assert fills == 0
    assert len(counter.db.get_trades(limit=10)) == 1
