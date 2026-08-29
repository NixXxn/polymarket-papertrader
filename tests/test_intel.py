from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from papertrader.config import load_settings
from papertrader.intel.gates import evaluate_entry_gate
from papertrader.intel.service import IntelSnapshot, _macro_verdict, event_risk
from papertrader.intel.taxonomy import classify_event_text


def _patch_intel_snapshot(monkeypatch, snap: IntelSnapshot) -> None:
    import papertrader.intel.gates as gates_mod

    class FakeSvc:
        def snapshot(self, *, force: bool = False):
            return snap

    monkeypatch.setattr(gates_mod, "get_intel_service", lambda *a, **k: FakeSvc())


def test_taxonomy_flags_geopolitics():
    cat, score, tags = classify_event_text(
        "nato-x-russia-military-clash-by-december-31-2026",
        "Will NATO and Russia clash?",
    )
    assert cat == "geopolitics"
    assert score >= 70
    assert tags


def test_taxonomy_flags_fed_longshot():
    cat, score, _ = classify_event_text(
        "will-the-fed-increase-interest-rates-by-25-bps-after-the-september-2026-meeting-649",
        "",
    )
    assert cat == "macro_longshot"
    assert score >= 70


def test_taxonomy_flags_bundesliga_and_gta():
    cat, score, _ = classify_event_text("bun-bay-stu-2026-08-28-bay", "")
    assert cat == "sports"
    assert score >= 65
    cat2, score2, _ = classify_event_text("another-gta-vi-trailer-released-by-august-31", "")
    assert cat2 == "celebrity"
    assert score2 >= 65
    cat3, score3, _ = classify_event_text("ere-gro-sit-2026-08-28-gro", "")
    assert cat3 == "sports"
    assert score3 >= 65
    cat4, score4, _ = classify_event_text("fl1-lil-psg-2026-08-28-psg", "")
    assert cat4 == "sports"
    assert score4 >= 65
    cat5, score5, _ = classify_event_text(
        "will-grand-theft-auto-vi-extended-look-get-less-than-10-million-views-on-day-1",
        "",
    )
    assert cat5 == "celebrity"
    assert score5 >= 65

    cat, score, _ = classify_event_text("lal-bar-bil-2026-08-27-bar", "Barcelona vs Bilbao")
    assert cat == "sports"
    assert score >= 65
    cat2, score2, _ = classify_event_text(
        "will-michael-birch-win-the-2026-greater-wellington-regional-council-by-election",
        "",
    )
    assert cat2 == "election"
    assert score2 >= 70


def test_macro_verdict_mapping():
    assert _macro_verdict(80, False) == "RISK_ON"
    assert _macro_verdict(50, None) == "NEUTRAL"
    assert _macro_verdict(40, True) in {"CAUTION", "RISK_OFF"}
    assert _macro_verdict(20, True) == "RISK_OFF"


def test_gate_blocks_high_risk_meanrev(tmp_path: Path, monkeypatch):
    settings = load_settings()
    cfg = replace(settings.intel, enabled=True, shadow_only=False, block_event_score=65)
    _patch_intel_snapshot(
        monkeypatch,
        IntelSnapshot(
            fetched_at="2026-08-27T00:00:00+00:00",
            fear_greed=55,
            macro_verdict="NEUTRAL",
            btc_sma50=100.0,
            btc_sma200=90.0,
            btc_death_cross=False,
        ),
    )

    gate = evaluate_entry_gate(
        strategy="meanrev",
        slug="nato-x-russia-military-clash-by-december-31-2026",
        question="clash?",
        data_dir=tmp_path,
        cfg=cfg,
    )
    assert gate.allow is False
    assert "intel_event_block" in gate.reason


def test_gate_blocks_sports_under_score(tmp_path: Path, monkeypatch):
    settings = load_settings()
    cfg = replace(settings.intel, enabled=True, shadow_only=False, block_event_score=65)
    _patch_intel_snapshot(
        monkeypatch,
        IntelSnapshot(
            fetched_at="2026-08-27T00:00:00+00:00",
            fear_greed=60,
            macro_verdict="NEUTRAL",
            btc_sma50=100.0,
            btc_sma200=90.0,
            btc_death_cross=False,
        ),
    )
    gate = evaluate_entry_gate(
        strategy="meanrev",
        slug="lal-bar-bil-2026-08-27-bar",
        question="Barcelona vs Bilbao",
        data_dir=tmp_path,
        cfg=cfg,
    )
    assert gate.allow is False
    assert "intel_event_block" in gate.reason


def test_gate_caution_blocks_election(tmp_path: Path, monkeypatch):
    settings = load_settings()
    cfg = replace(settings.intel, enabled=True, shadow_only=False, block_event_score=65)
    _patch_intel_snapshot(
        monkeypatch,
        IntelSnapshot(
            fetched_at="2026-08-27T00:00:00+00:00",
            fear_greed=50,
            macro_verdict="CAUTION",
            btc_sma50=80.0,
            btc_sma200=100.0,
            btc_death_cross=True,
        ),
    )
    gate = evaluate_entry_gate(
        strategy="closingsoon",
        slug="will-michael-birch-win-the-2026-greater-wellington-regional-council-by-election",
        data_dir=tmp_path,
        cfg=cfg,
    )
    assert gate.allow is False
    assert "intel_" in gate.reason


def test_gate_caution_sizes_down_general(tmp_path: Path, monkeypatch):
    settings = load_settings()
    cfg = replace(
        settings.intel,
        enabled=True,
        shadow_only=False,
        caution_size_mult=0.40,
    )
    _patch_intel_snapshot(
        monkeypatch,
        IntelSnapshot(
            fetched_at="2026-08-27T00:00:00+00:00",
            fear_greed=50,
            macro_verdict="CAUTION",
            btc_sma50=80.0,
            btc_sma200=100.0,
            btc_death_cross=True,
        ),
    )
    gate = evaluate_entry_gate(
        strategy="meanrev",
        slug="will-some-generic-event-happen-in-2026",
        data_dir=tmp_path,
        cfg=cfg,
    )
    assert gate.allow is True
    assert gate.size_mult == 0.40
    assert "caution_size_down" in gate.reason


def test_gate_shadow_does_not_block(tmp_path: Path, monkeypatch):
    settings = load_settings()
    cfg = replace(settings.intel, enabled=True, shadow_only=True, block_event_score=65)
    _patch_intel_snapshot(
        monkeypatch,
        IntelSnapshot(
            fetched_at="2026-08-27T00:00:00+00:00",
            fear_greed=10,
            macro_verdict="RISK_OFF",
            btc_sma50=80.0,
            btc_sma200=100.0,
            btc_death_cross=True,
        ),
    )

    gate = evaluate_entry_gate(
        strategy="meanrev",
        slug="nato-x-russia-military-clash",
        data_dir=tmp_path,
        cfg=cfg,
    )
    assert gate.allow is True
    assert gate.reason.startswith("shadow:")


def test_btc_gate_sizes_down_on_death_cross(tmp_path: Path, monkeypatch):
    settings = load_settings()
    cfg = replace(
        settings.intel,
        enabled=True,
        shadow_only=False,
        btc_min_fear_greed=45,
        caution_size_mult=0.40,
    )
    _patch_intel_snapshot(
        monkeypatch,
        IntelSnapshot(
            fetched_at="2026-08-27T00:00:00+00:00",
            fear_greed=71,
            macro_verdict="CAUTION",
            btc_sma50=80.0,
            btc_sma200=100.0,
            btc_death_cross=True,
        ),
    )

    gate = evaluate_entry_gate(
        strategy="btc5m",
        slug="btc-updown-5m",
        data_dir=tmp_path,
        cfg=cfg,
    )
    assert gate.allow is True
    assert gate.size_mult == 0.40
    assert "death_cross_size_down" in gate.reason


def test_should_force_exit_toxic():
    from papertrader.intel.gates import should_force_exit

    settings = load_settings()
    cfg = replace(settings.intel, enabled=True, shadow_only=False, block_event_score=65)
    reason = should_force_exit(slug="will-carlos-alcaraz-win-the-2026-mens-us-open", cfg=cfg)
    assert reason is not None
    assert "sports" in reason
    assert should_force_exit(slug="some-clean-weather-market", cfg=cfg) is None


def test_event_risk_helper():
    risk = event_risk("clarity-act-signed-into-law-in-2026")
    assert risk.category == "macro_longshot"
    assert risk.score >= 70


def test_load_settings_includes_intel():
    s = load_settings()
    assert s.intel.enabled is True
    assert "meanrev" in s.intel.strategies
    assert s.intel.block_event_score <= 65
    assert s.intel.btc_min_fear_greed >= 45
