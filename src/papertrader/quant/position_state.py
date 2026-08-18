from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pm_trader.models import Position


@dataclass
class ExitState:
    market_slug: str
    partial_tp_done: bool = False
    ladder_levels_hit: list[float] = field(default_factory=list)


class PositionExitStore:
    """Tracks staged ladder take-profits per open position."""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir)
        if self._root.name in ("safe", "asymmetric", "copy", "edge", "esports"):
            self._root = self._root.parent
        self._path = self._root / "position_exit_state.json"
        self._states: dict[str, ExitState] = {}
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
            levels = [float(x) for x in (row.get("ladder_levels_hit") or [])]
            partial_tp_done = bool(row.get("partial_tp_done"))
            if partial_tp_done and 2.0 not in levels:
                levels.append(2.0)
            levels = sorted(set(levels))
            self._states[key] = ExitState(
                market_slug=slug,
                partial_tp_done=partial_tp_done or bool(levels),
                ladder_levels_hit=levels,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in self._states.items()}
        self._path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def ladder_level_hit(self, condition_id: str, outcome: str, multiple: float) -> bool:
        state = self._states.get(self._key(condition_id, outcome))
        if not state:
            return False
        return multiple in state.ladder_levels_hit

    def mark_ladder_level(
        self, condition_id: str, outcome: str, multiple: float, *, market_slug: str
    ) -> None:
        key = self._key(condition_id, outcome)
        state = self._states.get(key)
        if state is None:
            state = ExitState(market_slug=market_slug, ladder_levels_hit=[])
            self._states[key] = state
        if multiple not in state.ladder_levels_hit:
            state.ladder_levels_hit.append(multiple)
            state.ladder_levels_hit.sort()
        state.partial_tp_done = bool(state.ladder_levels_hit)
        self._save()

    def unmark_ladder_level(self, condition_id: str, outcome: str, multiple: float) -> None:
        key = self._key(condition_id, outcome)
        state = self._states.get(key)
        if state is None:
            return
        state.ladder_levels_hit = [m for m in state.ladder_levels_hit if m != multiple]
        state.partial_tp_done = bool(state.ladder_levels_hit)
        self._save()

    def partial_tp_done(self, condition_id: str, outcome: str) -> bool:
        state = self._states.get(self._key(condition_id, outcome))
        return bool(state and state.ladder_levels_hit)

    def mark_partial_tp(self, condition_id: str, outcome: str, *, market_slug: str) -> None:
        self.mark_ladder_level(condition_id, outcome, 2.0, market_slug=market_slug)

    def unmark_partial_tp(self, condition_id: str, outcome: str) -> None:
        self.unmark_ladder_level(condition_id, outcome, 2.0)

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
