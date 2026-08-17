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
        if self._root.name in ("safe", "asymmetric", "copy", "edge"):
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
