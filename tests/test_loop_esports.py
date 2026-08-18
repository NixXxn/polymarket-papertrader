from __future__ import annotations

from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.loop import scan_once
from papertrader.report import ScanCounts


def test_scan_once_merges_esports_counts(monkeypatch):
    settings = load_settings()
    esports_engine = MagicMock()
    esports_engine.db.get_open_positions.return_value = []

    def fake_scan(*_args, **_kwargs):
        return [], ScanCounts(candidates=7, orders_placed=2, fills=1, risk_exits=0)

    monkeypatch.setattr("papertrader.loop.scan_esports_once", fake_scan)

    _, counts = scan_once(
        settings=settings,
        http=MagicMock(),
        safe_engine=None,
        esports_engine=esports_engine,
        dry_run=True,
    )
    assert counts.candidates == 7
    assert counts.orders_placed == 2
    assert counts.fills == 1
