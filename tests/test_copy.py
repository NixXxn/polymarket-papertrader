from __future__ import annotations

from pathlib import Path

import pytest

from papertrader.config import load_settings
from papertrader.copy_wallets import (
    add_copy_wallet,
    list_copy_wallets,
    normalize_wallet,
    remove_copy_wallet,
)
from papertrader.copytrade import (
    apply_copied_trade,
    copy_scale,
    load_state,
    parse_trade,
    peak_capital,
    sync_copy_trades,
    trade_key,
)
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
    monkeypatch.setattr("papertrader.copytrade.resolve_wallets", lambda *a, **k: ["0xabc"])
    monkeypatch.setattr("papertrader.copytrade.fetch_recent_trades", lambda *a, **k: history)
    considered, copied, fetch_ok = sync_copy_trades(
        engine, None, load_settings(), dry_run=False, live=True
    )
    assert considered == 0
    assert copied == []
    assert fetch_ok is True
    assert engine.get_account().cash == 50.0
    assert load_state(engine)["live_seeded"] is True
    engine.close()


def test_new_dashboard_wallet_is_seeded_not_backfilled(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    engine.init_account(100.0)
    # Legacy state from a previous leader must not block a newly added wallet.
    from papertrader.copytrade import save_state

    save_state(
        engine,
        {
            "seen": ["old"],
            "last_leader_ts": 9_999_999_999,
            "scale": 0.1,
        },
    )
    wallet = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    history = [
        parse_trade(_row(timestamp=100, transactionHash="0xnew1")),
        parse_trade(_row(timestamp=200, transactionHash="0xnew2", side="SELL")),
    ]
    monkeypatch.setattr(
        "papertrader.copytrade.resolve_wallets", lambda *a, **k: [wallet]
    )
    monkeypatch.setattr(
        "papertrader.copytrade.fetch_recent_trades", lambda *a, **k: history
    )
    considered, copied, ok = sync_copy_trades(
        engine, None, load_settings(), dry_run=False, live=False
    )
    assert ok and considered == 0 and copied == []
    st = load_state(engine)
    bucket = st["wallet_state"][wallet]
    assert bucket["seeded"] is True
    assert engine.get_account().cash == 100.0

    # A trade newer than the seed watermark is copied.
    newer = [parse_trade(_row(timestamp=300, transactionHash="0xnew3", size=4))]
    monkeypatch.setattr(
        "papertrader.copytrade.fetch_recent_trades", lambda *a, **k: history + newer
    )
    considered, copied, ok = sync_copy_trades(
        engine, None, load_settings(), dry_run=False, live=False
    )
    assert ok and considered == 1 and len(copied) == 1
    assert copied[0].action == "buy"
    engine.close()


def test_prune_seen_keeps_recent(tmp_path):
    from papertrader.copytrade import prune_seen

    recent = {f"0xnew:BUY:slug:{1_700_000_000 + i}:1:0.5" for i in range(10)}
    old = {f"0xold:BUY:slug:{1_600_000_000 + i}:1:0.5" for i in range(2000)}
    pruned = prune_seen(
        recent | old,
        recent_ids=recent,
        last_leader_ts=1_700_000_010,
        max_keep=50,
    )
    assert recent <= pruned
    assert len(pruned) <= 50


def test_normalize_and_manage_wallets(tmp_path: Path):
    settings = load_settings()
    assert not settings.copy.wallet
    with pytest.raises(ValueError):
        normalize_wallet("not-a-wallet")
    addr = "0x09b045baad1fbe115c70785635a261411774a3b6"
    row = add_copy_wallet(tmp_path, addr, label="leader")
    assert row["address"] == addr
    assert row["label"] == "leader"
    wallets = list_copy_wallets(tmp_path, settings)
    assert wallets == [{"address": addr, "label": "leader", "source": "dashboard"}]
    assert remove_copy_wallet(tmp_path, addr) is True
    assert list_copy_wallets(tmp_path, settings) == []
    assert remove_copy_wallet(tmp_path, addr) is False


def test_dashboard_wallet_wins_over_settings(tmp_path: Path):
    from papertrader.config import CopySettings
    from types import SimpleNamespace

    # Simulate a leftover settings hardcode that must not hide the dashboard entry.
    settings = SimpleNamespace(
        copy=CopySettings(
            username="",
            wallet="0x09b045baad1fbe115c70785635a261411774a3b6",
            wallets=(),
            scale=0.1,
            poll_interval_ms=2000,
            recent_limit=50,
        )
    )
    dash = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    add_copy_wallet(tmp_path, dash, label="ui")
    wallets = list_copy_wallets(tmp_path, settings)  # type: ignore[arg-type]
    assert wallets[0]["address"] == dash
    assert wallets[0]["source"] == "dashboard"
    assert any(w["address"].startswith("0x09b0") and w["source"] == "settings" for w in wallets)
