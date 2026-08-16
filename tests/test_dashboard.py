from __future__ import annotations

from papertrader.scan_history import append_scan, load_scan_history
from papertrader.report import ScanCounts, CombinedStats


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
