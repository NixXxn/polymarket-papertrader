"""Entry gates: world-intel overlays as veto / size-down (never blind buys)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from papertrader.intel.service import EventRisk, IntelSnapshot, event_risk, get_intel_service


@dataclass(frozen=True)
class GateDecision:
    allow: bool
    reason: str
    size_mult: float
    event: EventRisk
    macro_verdict: str
    fear_greed: int | None
    shadow_only: bool


def evaluate_entry_gate(
    *,
    strategy: str,
    slug: str,
    question: str = "",
    data_dir: Path | str,
    cfg: Any,
) -> GateDecision:
    """Apply intel gates for a candidate entry.

    ``cfg`` is Settings.intel (IntelSettings). Fail-open on network if
    ``fail_open_on_error`` is true — taxonomy gates still apply offline.
    """
    enabled = bool(getattr(cfg, "enabled", False))
    shadow = bool(getattr(cfg, "shadow_only", True))
    risk = event_risk(slug, question)

    snap: IntelSnapshot | None = None
    macro = "NEUTRAL"
    fear: int | None = None
    if enabled:
        try:
            ttl = float(getattr(cfg, "ttl_seconds", 900))
            svc = get_intel_service(data_dir, ttl_seconds=ttl)
            snap = svc.snapshot()
            macro = snap.macro_verdict
            fear = snap.fear_greed
        except Exception as e:
            if not bool(getattr(cfg, "fail_open_on_error", True)):
                return GateDecision(
                    allow=False,
                    reason=f"intel_fetch_failed:{e}",
                    size_mult=0.0,
                    event=risk,
                    macro_verdict="NEUTRAL",
                    fear_greed=None,
                    shadow_only=shadow,
                )

    block_score = int(getattr(cfg, "block_event_score", 70))
    size_down = float(getattr(cfg, "caution_size_mult", 0.55))
    strategies = set(getattr(cfg, "strategies", ()) or ())
    applies = not strategies or strategy in strategies

    allow = True
    reason = "intel_ok"
    size_mult = 1.0

    if applies and enabled:
        scanners = {"meanrev", "volspike", "closingsoon"}
        # Hard block elevated narrative/geopolitics/elections/sports for scanners.
        if strategy in scanners and risk.score >= block_score:
            allow = False
            reason = f"intel_event_block score={risk.score} cat={risk.category}"
        # Macro RISK_OFF: block elevated categories; size-down others.
        elif macro == "RISK_OFF":
            if risk.category in {
                "geopolitics",
                "macro_longshot",
                "crypto_narrative",
                "election",
                "sports",
            } or risk.is_elevated:
                allow = False
                reason = f"intel_risk_off+{risk.category}"
            else:
                size_mult = size_down
                reason = "intel_risk_off_size_down"
        # CAUTION: block elevated (steadier book); size-down clean generals.
        elif macro == "CAUTION":
            if strategy in scanners and risk.is_elevated:
                allow = False
                reason = f"intel_caution_block cat={risk.category}"
            else:
                size_mult = size_down
                reason = "intel_caution_size_down"

        # BTC 5m: death-cross is choppy for short scalps — skip until SMAs heal.
        if strategy == "btc5m" and allow:
            death = snap.btc_death_cross if snap is not None else None
            min_fg = int(getattr(cfg, "btc_min_fear_greed", 45))
            if death is True:
                allow = False
                reason = f"intel_btc_death_cross fg={fear}"
            elif fear is not None and fear <= min_fg:
                allow = False
                reason = f"intel_btc_extreme_fear fg={fear}"

    # Shadow mode: never block, only annotate.
    if shadow and not allow:
        return GateDecision(
            allow=True,
            reason=f"shadow:{reason}",
            size_mult=1.0,
            event=risk,
            macro_verdict=macro,
            fear_greed=fear,
            shadow_only=True,
        )

    return GateDecision(
        allow=allow,
        reason=reason,
        size_mult=size_mult if allow else 0.0,
        event=risk,
        macro_verdict=macro,
        fear_greed=fear,
        shadow_only=shadow,
    )
