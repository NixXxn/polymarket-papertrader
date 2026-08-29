from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_LINES = 2000


class ShadowLedger:
    """JSONL audit log for model inputs and counterfactual exit simulations."""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir)
        if self._root.name in ("safe", "asymmetric", "contrarian", "copy", "edge", "esports", "momentum"):
            self._root = self._root.parent
        self._path = self._root / "shadow_ledger.jsonl"

    def _append(self, row: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        lines = self._path.read_text().splitlines()
        if len(lines) > _MAX_LINES:
            self._path.write_text("\n".join(lines[-_MAX_LINES:]) + "\n")

    def log_entry(
        self,
        *,
        strategy: str,
        slug: str,
        action: str,
        share_price: float,
        p: float,
        sigma: float,
        f_star: float,
        stake_usd: float | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "entry",
            "strategy": strategy,
            "slug": slug,
            "action": action,
            "share_price": share_price,
            "p": p,
            "sigma": sigma,
            "f_star": f_star,
            "stake_usd": stake_usd,
        }
        if extra:
            row.update(extra)
        self._append(row)

    def log_bayes_shadow(
        self,
        *,
        strategy: str,
        slug: str,
        prior_yes: float,
        evidence_yes: float,
        lr_yes: float,
        posterior_yes: float,
        model_edge_no: float,
        bayes_edge_no: float,
        no_ask: float,
        required_edge: float,
        model_would_take: bool,
        bayes_would_take: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Counterfactual market-prior × LR path (does not affect sizing)."""
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "bayes_shadow",
            "strategy": strategy,
            "slug": slug,
            "prior_yes": round(prior_yes, 6),
            "evidence_yes": round(evidence_yes, 6),
            "lr_yes": round(lr_yes, 6),
            "posterior_yes": round(posterior_yes, 6),
            "posterior_no": round(1.0 - posterior_yes, 6),
            "no_ask": round(no_ask, 4),
            "model_edge_no": round(model_edge_no, 6),
            "bayes_edge_no": round(bayes_edge_no, 6),
            "required_edge": round(required_edge, 6),
            "model_would_take": model_would_take,
            "bayes_would_take": bayes_would_take,
            "agree": model_would_take == bayes_would_take,
        }
        if extra:
            row.update(extra)
        self._append(row)

    def log_exit_simulation(
        self,
        *,
        slug: str,
        entry_price: float,
        current_price: float,
        shares: float,
        target_pct: float = 0.20,
    ) -> None:
        """Counterfactual: full close at entry * (1 + target_pct)."""
        target = entry_price * (1.0 + target_pct)
        if current_price < target:
            return
        pnl_per_share = target - entry_price
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "shadow_exit_20pct",
            "slug": slug,
            "entry_price": entry_price,
            "sim_exit_price": target,
            "current_price": current_price,
            "shares": shares,
            "sim_pnl_usd": round(pnl_per_share * shares, 4),
        }
        self._append(row)
