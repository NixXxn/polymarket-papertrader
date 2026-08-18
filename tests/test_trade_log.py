from __future__ import annotations

from types import SimpleNamespace

from papertrader.report import _sell_realized_pnl
from papertrader.trade_log import (
    append_copy_event,
    append_skipped,
    copy_latency_stats,
    load_skipped_trades,
)
from papertrader.signals import Signal


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


def test_sell_realized_pnl():
    trades = [
        _trade(id=1, side="buy", amount_usd=5.0, shares=10.0),
        _trade(id=2, side="sell", amount_usd=6.0, shares=10.0, avg_price=0.6),
    ]
    pnl = _sell_realized_pnl(trades)
    assert pnl[2] == 1.0


def test_skipped_and_copy_event_logging(tmp_path):
    sig = Signal(action="buy", slug="test-market", outcome="yes", amount_usd=5, reason="test")
    append_skipped(tmp_path / "safe", strategy="safe", signal=sig, error="no liquidity")
    skipped = load_skipped_trades(tmp_path)
    assert len(skipped) == 1
    assert skipped[0]["error"] == "no liquidity"

    append_copy_event(
        tmp_path / "copy",
        tx_id="0xabc:buy:slug:1:10:0.5",
        leader_ts=1_700_000_000,
        side="BUY",
        slug="test-market",
        title="Test",
        status="filled",
        trade_id=42,
        detected_at=1_700_000_003.5,
    )
    stats = copy_latency_stats(tmp_path)
    assert stats["count"] == 1
    assert stats["avg_ms"] == 3500.0
