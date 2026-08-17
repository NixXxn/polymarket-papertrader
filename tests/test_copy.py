from __future__ import annotations

from papertrader.copytrade import (
    apply_copied_trade,
    copy_scale,
    load_state,
    parse_trade,
    peak_capital,
    sync_copy_trades,
    trade_key,
)
from papertrader.config import load_settings
from pm_trader.engine import Engine


def _row(**kwargs):
    base = dict(
        transactionHash="0xabc",
        side="BUY",
        slug="highest-temperature-in-tokyo-on-august-13-2026-32c",
        title="Will the highest temperature in Tokyo be 32°C on August 13?",
        outcome="Yes",
        conditionId="0xcond",
        price=0.50,
        size=10,
        timestamp=1,
        eventSlug="highest-temperature-in-tokyo-on-august-13-2026",
    )
    base.update(kwargs)
    return base


def test_peak_capital_and_scale():
    trades = [
        parse_trade(_row(side="BUY", size=100, price=0.50, timestamp=1)),
        parse_trade(_row(side="SELL", size=100, price=0.60, timestamp=2, transactionHash="0xdef")),
    ]
    assert peak_capital(trades) == 50.0
    assert copy_scale(trades, 25.0) == 0.5
    assert copy_scale(trades, 100.0) == 1.0


def test_apply_buy_and_sell(tmp_path):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    buy = parse_trade(_row())
    sig = apply_copied_trade(engine, buy, 1.0)
    assert sig is not None and sig.action == "buy"
    assert engine.get_account().cash == 45.0
    pos = engine.db.get_open_positions()
    assert len(pos) == 1
    assert pos[0].shares == 10

    sell = parse_trade(_row(side="SELL", price=0.60, transactionHash="0xdef", timestamp=2))
    sig = apply_copied_trade(engine, sell, 1.0)
    assert sig is not None and sig.action == "sell"
    assert engine.get_account().cash == 51.0
    assert engine.db.get_open_positions() == []
    engine.close()


def test_trade_key_unique():
    a = _row()
    b = _row(side="SELL")
    assert trade_key(a) != trade_key(b)


def test_live_copy_seeds_history_without_fills(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    engine.init_account(50.0)
    history = [parse_trade(_row())]
    monkeypatch.setattr("papertrader.copytrade.resolve_wallet", lambda *a, **k: "0xabc")
    monkeypatch.setattr("papertrader.copytrade.fetch_recent_trades", lambda *a, **k: history)
    considered, copied = sync_copy_trades(
        engine, None, load_settings(), dry_run=False, live=True
    )
    assert considered == 0
    assert copied == []
    assert engine.get_account().cash == 50.0
    assert load_state(engine)["live_seeded"] is True

    fresh = parse_trade(_row(transactionHash="0xnew", timestamp=99))
    monkeypatch.setattr(
        "papertrader.copytrade.fetch_recent_trades", lambda *a, **k: history + [fresh]
    )
    ran: list = []
    considered, copied = sync_copy_trades(
        engine,
        None,
        load_settings(),
        dry_run=False,
        live=True,
        execute=lambda sig: ran.append(sig) or True,
    )
    assert considered == 1
    assert len(copied) == 1
    assert ran[0].action == "buy"
    engine.close()
