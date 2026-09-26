from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from pm_trader.models import Position


@dataclass
class ForgeExitState:
    market_slug: str
    sleeve: str  # "hearth" | "strike"
    take_profit_limit_placed: bool = False
    take_profit_price: float | None = None


class ForgeExitStore:
    """Tracks sleeve tags, resting TPs, and the ratcheting waterline."""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir)
        if self._root.name == "forge":
            self._root = self._root.parent
        self._path = self._root / "forge_exit_state.json"
        self._states: dict[str, ForgeExitState] = {}
        self._waterline: float | None = None
        self._high_water: float | None = None
        self._load()

    @staticmethod
    def _key(condition_id: str, outcome: str) -> str:
        return f"{condition_id}:{outcome.lower()}"

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return
        meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
        if meta.get("waterline") is not None:
            try:
                self._waterline = float(meta["waterline"])
            except (TypeError, ValueError):
                self._waterline = None
        if meta.get("high_water") is not None:
            try:
                self._high_water = float(meta["high_water"])
            except (TypeError, ValueError):
                self._high_water = None
        for key, row in raw.items():
            if key == "_meta" or not isinstance(row, dict):
                continue
            slug = row.get("market_slug")
            sleeve = row.get("sleeve") or "hearth"
            if not isinstance(slug, str):
                continue
            tp = row.get("take_profit_price")
            self._states[key] = ForgeExitState(
                market_slug=slug,
                sleeve=str(sleeve),
                take_profit_limit_placed=bool(row.get("take_profit_limit_placed")),
                take_profit_price=float(tp) if tp is not None else None,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {k: asdict(v) for k, v in self._states.items()}
        payload["_meta"] = {
            "waterline": self._waterline,
            "high_water": self._high_water,
        }
        self._path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def waterline(self, starting_balance: float) -> float:
        if self._waterline is None:
            self._waterline = float(starting_balance)
            self._high_water = float(starting_balance)
            self._save()
        return float(self._waterline)

    def ratchet(self, equity: float, starting_balance: float, *, lock_fraction: float) -> float:
        """Raise waterline toward equity highs; never lower it. Returns current waterline."""
        wl = self.waterline(starting_balance)
        hw = self._high_water if self._high_water is not None else wl
        if equity > hw:
            # Lock a fraction of new highs into protected capital.
            gain = equity - hw
            wl = wl + max(0.0, gain * max(0.0, min(1.0, lock_fraction)))
            self._waterline = wl
            self._high_water = equity
            self._save()
        return float(self._waterline)

    def sleeve_of(self, condition_id: str, outcome: str) -> str:
        state = self._states.get(self._key(condition_id, outcome))
        return state.sleeve if state else "hearth"

    def mark_entry(
        self,
        condition_id: str,
        outcome: str,
        *,
        market_slug: str,
        sleeve: str,
    ) -> None:
        key = self._key(condition_id, outcome)
        prev = self._states.get(key)
        self._states[key] = ForgeExitState(
            market_slug=market_slug,
            sleeve=sleeve,
            take_profit_limit_placed=bool(prev.take_profit_limit_placed) if prev else False,
            take_profit_price=prev.take_profit_price if prev else None,
        )
        self._save()

    def take_profit_placed(self, condition_id: str, outcome: str) -> bool:
        state = self._states.get(self._key(condition_id, outcome))
        return bool(state and state.take_profit_limit_placed)

    def mark_take_profit(
        self,
        condition_id: str,
        outcome: str,
        *,
        market_slug: str,
        take_profit_price: float,
        sleeve: str | None = None,
    ) -> None:
        key = self._key(condition_id, outcome)
        prev = self._states.get(key)
        self._states[key] = ForgeExitState(
            market_slug=market_slug,
            sleeve=sleeve or (prev.sleeve if prev else "hearth"),
            take_profit_limit_placed=True,
            take_profit_price=take_profit_price,
        )
        self._save()

    def unmark_take_profit(self, condition_id: str, outcome: str) -> None:
        key = self._key(condition_id, outcome)
        state = self._states.get(key)
        if state is None:
            return
        state.take_profit_limit_placed = False
        state.take_profit_price = None
        self._save()

    def prune_closed(self, positions: list[Position]) -> None:
        open_keys = {
            self._key(p.market_condition_id, p.outcome)
            for p in positions
            if p.shares > 0 and not p.is_resolved
        }
        stale = [k for k in self._states if k not in open_keys]
        if not stale:
            return
        for key in stale:
            del self._states[key]
        self._save()
