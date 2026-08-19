from __future__ import annotations

from papertrader.accounts import make_engine
from papertrader.dashboard.app import app
from papertrader.dashboard.data import fetch_dashboard, reset_all_statistics, reset_strategy_budgets
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
    assert len(payload["portfolio"]["by_strategy"]) == 6
    assert payload["portfolio"]["total"] > 0
    assert payload["activity_log"] == []
    assert payload["decisions"] == []
    assert payload["copy"]["latency"]["count"] == 0


def test_fetch_dashboard_includes_all_strategies(tmp_path):
    payload = fetch_dashboard(data_dir=tmp_path, mode="paper")
    names = {s["name"] for s in payload["portfolio"]["by_strategy"]}
    assert names == {"safe", "asymmetric", "contrarian", "copy", "esports", "momentum"}


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


def test_reset_strategy_budgets(tmp_path):
    make_engine("safe", tmp_path, starting_balance=100.0, reset=True)
    make_engine("asymmetric", tmp_path, starting_balance=100.0, reset=True)
    result = reset_strategy_budgets(data_dir=tmp_path, mode="paper", balance=500.0)
    assert result["ok"] is True
    assert len(result["strategies"]) == 6
    assert all(s["cash"] == 500.0 for s in result["strategies"])
    payload = fetch_dashboard(data_dir=tmp_path, mode="paper")
    assert payload["portfolio"]["by_strategy"][0]["cash"] == 500.0
    assert payload["portfolio"]["by_strategy"][0]["trades"] == 0


def test_fetch_dashboard_activity_includes_strategy_decisions(tmp_path):
    from papertrader.decision_log import log_decision
    from papertrader.trade_log import build_activity_feed

    log_decision(
        tmp_path,
        strategy="contrarian",
        decision="scan",
        reason="contrarian scan test",
    )
    log_decision(
        tmp_path,
        strategy="esports",
        decision="buy",
        reason="esports buy test",
        slug="lol-test",
    )
    feed = build_activity_feed(tmp_path)
    strategies = {row.get("strategy") for row in feed}
    assert "contrarian" in strategies
    assert "esports" in strategies


def test_reset_balances_api(tmp_path):
    make_engine("safe", tmp_path, starting_balance=50.0, reset=True)
    client = app.test_client()
    resp = client.post(
        "/api/reset-balances?mode=paper",
        json={"balance": 500, "data_dir": str(tmp_path)},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["balance"] == 500.0
    engine = make_engine("safe", tmp_path, 50.0)
    try:
        assert engine.get_account().cash == 500.0
    finally:
        engine.close()


def test_reset_all_statistics_clears_logs_and_trades(tmp_path):
    make_engine("safe", tmp_path, starting_balance=100.0, reset=True)
    log_decision(
        tmp_path,
        strategy="safe",
        decision="scan",
        reason="test",
    )
    result = reset_all_statistics(data_dir=tmp_path, mode="paper")
    assert result["ok"] is True
    payload = fetch_dashboard(data_dir=tmp_path, mode="paper")
    safe_row = next(s for s in payload["portfolio"]["by_strategy"] if s["name"] == "safe")
    assert safe_row["trades"] == 0
    assert payload["decisions"] == []


def test_reset_statistics_api(tmp_path):
    make_engine("safe", tmp_path, starting_balance=100.0, reset=True)
    log_decision(
        tmp_path,
        strategy="safe",
        decision="scan",
        reason="test",
    )
    client = app.test_client()
    resp = client.post(
        "/api/reset-statistics?mode=paper",
        json={"data_dir": str(tmp_path)},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    payload = fetch_dashboard(data_dir=tmp_path, mode="paper")
    assert payload["decisions"] == []
