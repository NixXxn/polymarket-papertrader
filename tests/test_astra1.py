from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from papertrader.config import load_settings
from papertrader.strategies.astra1 import AstraMarket, Walk, analyze_astra1, v2_fee


def test_astra1_settings_are_paper_first():
    cfg = load_settings().astra1
    assert cfg.live_enabled is False
    assert cfg.max_all_in_cost < 1.0
    assert cfg.min_locked_edge > 0
    assert cfg.confirmation_reads >= 2


def test_v2_fee_is_share_price_based():
    # 7% taker schedule at 50c: 10 × .07 × .5 × .5 = 17.5 cents.
    assert v2_fee(10, 0.5, 700) == pytest.approx(0.175)
    assert v2_fee(10, 0.0, 700) == 0.0


def test_astra1_emits_equal_share_fee_aware_pair(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1_000.0)
    market = AstraMarket("0xastra", "test-astra", "Test", "Yes", "No", 10_000, 10_000)

    import papertrader.strategies.astra1 as astra

    monkeypatch.setattr(astra, "discover_astra1_markets", lambda *_args: [market])
    monkeypatch.setattr(astra, "_sizing_capacity", lambda *_args: (200.0, 200.0))
    monkeypatch.setattr(
        astra,
        "_quote",
        lambda *_args: (
            Walk(shares=25.0, cost=10.0, fee=0.07, worst_price=0.40),
            Walk(shares=25.0, cost=13.0, fee=0.07, worst_price=0.52),
            700,
            700,
        ),
    )

    signals = analyze_astra1(engine, settings)
    assert len(signals) == 2
    assert {signal.outcome for signal in signals} == {"yes", "no"}
    assert all(signal.order_type == "fak" for signal in signals)
    # $23.14 / 25 + 1c reserve = 93.56c all-in; the pair is accepted.
    assert sum(signal.amount_usd or 0 for signal in signals) == 23.0


def test_astra1_rejects_quote_after_fee_and_latency(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1_000.0)
    market = AstraMarket("0xnoedge", "test-noedge", "Test", "Yes", "No", 10_000, 10_000)

    import papertrader.strategies.astra1 as astra

    monkeypatch.setattr(astra, "discover_astra1_markets", lambda *_args: [market])
    monkeypatch.setattr(astra, "_sizing_capacity", lambda *_args: (200.0, 200.0))
    monkeypatch.setattr(
        astra,
        "_quote",
        lambda *_args: (
            Walk(shares=25.0, cost=12.2, fee=0.2, worst_price=0.488),
            Walk(shares=25.0, cost=12.2, fee=0.2, worst_price=0.488),
            700,
            700,
        ),
    )
    assert analyze_astra1(engine, settings) == []
