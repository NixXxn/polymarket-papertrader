from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from papertrader.decision_log import log_decision, load_decisions, purge_stale_logs


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
