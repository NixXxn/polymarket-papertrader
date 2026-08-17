from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from papertrader.decision_log import (
    classify_ask_reject,
    format_skip_summary,
    log_decision,
    load_decisions,
    purge_stale_logs,
)


def test_log_and_load_decisions(tmp_path):
    log_decision(
        tmp_path,
        strategy="asymmetric",
        decision="skip",
        reason="no_tail_candidates",
        city="wellington",
        event_date="2026-08-18",
        rejects={"low_edge": 3},
    )
    rows = load_decisions(tmp_path)
    assert len(rows) == 1
    assert rows[0]["city"] == "wellington"
    assert rows[0]["decision"] == "skip"


def test_purge_stale_logs(tmp_path):
    path = tmp_path / "decisions.jsonl"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"ts": old_ts, "event": "decision", "reason": "old"}) + "\n"
        + json.dumps({"ts": new_ts, "event": "decision", "reason": "new"}) + "\n"
    )
    removed = purge_stale_logs(tmp_path, retention_days=3)
    assert removed["decisions.jsonl"] == 1
    kept = path.read_text().strip().splitlines()
    assert len(kept) == 1
    assert "new" in kept[0]


def test_classify_ask_reject():
    assert classify_ask_reject(None, 10, min_ask=0.02, max_ask=0.10, min_size=5) == "no_ask"
    assert classify_ask_reject(0.001, 100, min_ask=0.02, max_ask=0.10, min_size=5) == "ask_too_cheap"
    assert classify_ask_reject(0.15, 100, min_ask=0.02, max_ask=0.10, min_size=5) == "ask_too_expensive"
    assert classify_ask_reject(0.05, 1, min_ask=0.02, max_ask=0.10, min_size=5) == "ask_size_too_small"
    assert classify_ask_reject(0.05, 10, min_ask=0.02, max_ask=0.10, min_size=5) is None


def test_format_skip_summary():
    summary = format_skip_summary({"ask_too_cheap": 8, "no_ask": 1})
    assert "8×" in summary
    assert "Ask unter Mindestpreis" in summary
    assert "kein Ask" in summary
