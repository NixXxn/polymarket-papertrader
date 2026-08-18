from __future__ import annotations

from types import SimpleNamespace

from papertrader.report import realized_pnl_total


def _trade(**kwargs):
    base = dict(
        id=1,
        market_condition_id="0x1",
        outcome="yes",
        side="buy",
        amount_usd=5.0,
        shares=10.0,
        fee=0.0,
        avg_price=0.5,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_realized_pnl_excludes_open_positions():
    trades = [
        _trade(id=1, side="buy", amount_usd=5.0, shares=10.0),
        _trade(id=2, side="sell", amount_usd=6.0, shares=10.0, avg_price=0.6),
        _trade(id=3, side="buy", amount_usd=4.0, shares=8.0),
    ]
    assert realized_pnl_total(trades) == 1.0
