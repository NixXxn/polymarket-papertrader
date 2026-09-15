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


def test_analyze_penny_logs_city_slug_not_object(tmp_path):
    """Regression: logging City objects crashed the whole penny scan."""
    from datetime import date

    from papertrader.config import City
    from papertrader.strategies.penny import analyze_penny_event

    settings = load_settings()
    city = City(
        name="Miami",
        slug="miami",
        station="KMIA",
        lat=25.8,
        lon=-80.3,
        tz="America/New_York",
        country="US",
        strategies=("penny",),
        position_usd=2.0,
    )
    engine = MagicMock()
    engine.db.data_dir = tmp_path / "penny"
    engine.db.data_dir.mkdir()
    engine.db.get_account.return_value = SimpleNamespace(cash=500.0)

    market = MagicMock()
    market.slug = "highest-temperature-in-miami-on-september-15-2026-80f"
    market.condition_id = "0xpenny"
    market.get_token_id.return_value = "tok-yes"
    bucket = SimpleNamespace(
        market=market,
        event_slug="highest-temperature-in-miami-on-september-15-2026",
        event_volume=500.0,
        bucket_text="80-81°F",
    )
    engine.api.get_order_book.return_value = MagicMock()

    import papertrader.strategies.penny as penny_mod

    original_best_ask = penny_mod.best_ask

    def _ask(_book):
        return (0.001, 100.0)

    penny_mod.best_ask = _ask
    try:
        sigs = analyze_penny_event(
            engine,
            city,
            date(2026, 9, 15),
            [bucket],
            settings,
            [],
            today=date(2026, 9, 15),
            paper_mode=True,
        )
    finally:
        penny_mod.best_ask = original_best_ask

    assert len(sigs) == 1
    assert sigs[0].action == "buy"
    assert sigs[0].paper_fill_at_limit is True
    # Decision log should have written without raising.
    decisions = (tmp_path / "penny" / "decisions.jsonl")
    # root_data_dir may remap; also check parent decisions via log path
    from papertrader.decision_log import load_decisions

    rows = load_decisions(engine.db.data_dir, limit=20)
    buy_rows = [r for r in rows if r.get("decision") == "buy" and r.get("strategy") == "penny"]
    assert buy_rows
    assert buy_rows[0]["city"] == "miami"
