from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.penny_state import PennyExitStore
from papertrader.signals import Signal
from papertrader.strategies.penny import penny_exits


def test_penny_exits_places_three_cent_limit(tmp_path):
    settings = load_settings()
    assert settings.penny.buy_limit == 0.01
    assert settings.penny.sell_limit == 0.03

    engine = MagicMock()
    engine.db.data_dir = tmp_path / "penny"
    engine.db.data_dir.mkdir()

    pos = SimpleNamespace(
        shares=100.0,
        is_resolved=False,
        avg_entry_price=0.01,
        market_condition_id="cond1",
        outcome="yes",
        market_slug="highest-temperature-in-miami-on-september-8-2026-78f",
    )
    sigs = penny_exits(engine, settings, [pos])
    assert len(sigs) == 1
    sig = sigs[0]
    assert isinstance(sig, Signal)
    assert sig.action == "sell"
    assert sig.order_type == "limit"
    assert sig.limit_price == 0.03
    assert sig.penny_take_profit is True

    store = PennyExitStore(engine.db.data_dir)
    store.mark_take_profit("cond1", "yes", market_slug=pos.market_slug, take_profit_price=0.03)
    assert store.take_profit_placed("cond1", "yes")
    assert penny_exits(engine, settings, [pos], exit_store=store) == []
