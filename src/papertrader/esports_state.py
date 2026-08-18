from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from pm_trader.models import Position


@dataclass
class EsportsExitState:
    market_slug: str
    take_profit_limit_placed: bool = False
    take_profit_price: float | None = None


class EsportsExitStore:
    """Tracks resting take-profit limits for esports positions."""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir)
        if self._root.name in ("safe", "asymmetric", "copy", "edge", "esports", "momentum"):
            self._root = self._root.parent
        self._path = self._root / "esports_exit_state.json"
        self._states: dict[str, EsportsExitState] = {}
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
        for key, row in raw.items():
            if not isinstance(row, dict):
                continue
            slug = row.get("market_slug")
            if not isinstance(slug, str):
                continue
            tp = row.get("take_profit_price")
            self._states[key] = EsportsExitState(
                market_slug=slug,
                take_profit_limit_placed=bool(row.get("take_profit_limit_placed")),
                take_profit_price=float(tp) if tp is not None else None,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in self._states.items()}
        self._path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

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
    ) -> None:
        key = self._key(condition_id, outcome)
        self._states[key] = EsportsExitState(
            market_slug=market_slug,
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

    def clear(self, condition_id: str, outcome: str) -> None:
        key = self._key(condition_id, outcome)
        if key in self._states:
            del self._states[key]
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
