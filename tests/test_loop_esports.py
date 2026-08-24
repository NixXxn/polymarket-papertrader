from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.decision_log import load_decisions
from papertrader.esports_markets import EsportsCandidate, EsportsDiscoveryResult, EsportsScanStats
from papertrader.loop import scan_esports_once, scan_once
from papertrader.oddspapi import FairMatch
from papertrader.report import ScanCounts


def _candidate():
    end_at = datetime.now(timezone.utc) + timedelta(hours=1.5)
    market = SimpleNamespace(
        slug="lol-alpha-beta-2026-08-18-game1",
        question="LoL: Alpha vs Beta - Game 1 Winner",
        closed=False,
        condition_id="0xesports",
        outcomes=["Alpha", "Beta"],
    )
    market.get_token_id = lambda outcome: f"tok-{outcome.lower()}"
    return EsportsCandidate(
        event_slug="lol-alpha-beta-2026-08-18",
        event_title="LoL: Alpha vs Beta",
        market=market,  # type: ignore[arg-type]
        outcome="Beta",
        end_at=end_at,
        event_volume=5000,
        ask=0.08,
        ask_size=200,
    )


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


def test_scan_esports_once_logs_scan_and_buys(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path / "esports"
    engine.db.data_dir.mkdir(parents=True)
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    engine.get_open_positions.return_value = []
    engine.check_orders.return_value = None
    engine.resolve_all.return_value = []

    discovery = EsportsDiscoveryResult(
        candidates=[_candidate()],
        stats=EsportsScanStats(candidates=1, match_markets=1, events_in_horizon=1),
    )
    monkeypatch.setattr("papertrader.loop.discover_esports_markets", lambda *a, **k: discovery)
    monkeypatch.setattr("papertrader.loop.execute_signal", lambda *a, **k: True)
    monkeypatch.setattr("papertrader.loop.esports_exits", lambda *a, **k: [])
    monkeypatch.setattr("papertrader.loop.oddspapi_api_key", lambda: "test-key")
    monkeypatch.setattr("papertrader.strategies.esports.oddspapi_api_key", lambda: "test-key")
    fake_cache = SimpleNamespace(
        matches=[
            FairMatch(
                fixture_id="fx1",
                team1="Alpha",
                team2="Beta",
                fair_p1=0.80,
                fair_p2=0.20,
            )
        ]
    )
    monkeypatch.setattr(
        "papertrader.loop.OddsPapiService",
        lambda *a, **k: SimpleNamespace(
            refresh_if_needed=lambda: fake_cache,
            quota_snapshot=lambda: {"daily": 0, "monthly": 0},
        ),
    )

    sigs, counts = scan_esports_once(
        settings=settings,
        esports_engine=engine,
        dry_run=False,
    )
    assert len(sigs) == 1
    assert sigs[0].order_type == "fak"
    assert counts.orders_placed == 1
    decisions = load_decisions(tmp_path)
    assert any(d.get("strategy") == "esports" and d.get("decision") == "scan" for d in decisions)
    assert any(d.get("strategy") == "esports" and d.get("decision") == "buy" for d in decisions)
