from __future__ import annotations

from papertrader.accounts import make_engine
from papertrader.dashboard.data import fetch_dashboard
from papertrader.decision_log import log_decision
from papertrader.report import CombinedStats, ScanCounts
from papertrader.scan_history import append_scan, load_scan_history
from papertrader.trade_log import append_copy_event, append_skipped
from papertrader.signals import Signal


def test_scan_history_roundtrip(tmp_path):
    stats = CombinedStats(
        cash=40,
        positions=10,
        total=50,
        pnl=0,
        roi_pct=0,
        trades=0,
        buys=0,
        sells=0,
        win_rate=0,
        max_drawdown=0,
        fees=0,
        avg_trade=0,
    )
    counts = ScanCounts(candidates=5, pending=2)
    append_scan(tmp_path, counts, stats)
    rows = load_scan_history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["total"] == 50
    assert rows[0]["pending"] == 2


def test_fetch_dashboard_empty_data_dir(tmp_path):
    payload = fetch_dashboard(data_dir=tmp_path, mode="paper")
    assert payload["ok"] is True
    assert len(payload["portfolio"]["by_strategy"]) == 4
    assert payload["portfolio"]["total"] > 0
    assert payload["activity_log"] == []
    assert payload["decisions"] == []
    assert payload["copy"]["latency"]["count"] == 0


def test_fetch_dashboard_includes_all_strategies(tmp_path):
    payload = fetch_dashboard(data_dir=tmp_path, mode="paper")
    names = {s["name"] for s in payload["portfolio"]["by_strategy"]}
    assert names == {"safe", "asymmetric", "copy", "esports"}


def test_fetch_dashboard_with_engines_and_logs(tmp_path):
    make_engine("safe", tmp_path, starting_balance=1000.0, reset=True)
    make_engine("copy", tmp_path, starting_balance=1000.0, reset=True)

    sig = Signal(action="buy", slug="test-market", outcome="yes", amount_usd=5, reason="test")
    append_skipped(tmp_path / "safe", strategy="safe", signal=sig, error="no liquidity")
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
    log_decision(
        tmp_path,
        decision="skip",
        strategy="asymmetric",
        reason="thin ensemble",
        city="warsaw",
        level="info",
    )

    payload = fetch_dashboard(data_dir=tmp_path, mode="paper")
    assert payload["ok"] is True
    assert payload["portfolio"]["total"] > 0
    assert len(payload["skipped_trades"]) == 1
    assert payload["copy"]["latency"]["count"] == 1
    assert any(r.get("feed") == "decision" for r in payload["activity_log"])
    assert any(r.get("decision") == "skip" for r in payload["decisions"])
