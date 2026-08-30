from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from pm_trader.models import Position


@dataclass
class FadeExitState:
    market_slug: str
    take_profit_limit_placed: bool = False
    take_profit_price: float | None = None


class FadeFinderState:
    """Dedupe PH alerts / sports fades and track resting take-profit limits."""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir)
        if self._root.name in (
            "safe",
            "asymmetric",
            "contrarian",
            "conviction",
            "copy",
            "edge",
            "esports",
            "fadefinder",
            "momentum",
            "meanrev",
            "volspike",
            "closingsoon",
            "btc5m",
        ):
            self._root = self._root.parent
        self._seen_path = self._root / "fadefinder_seen.json"
        self._exit_path = self._root / "fadefinder_exit_state.json"
        self._seen_alerts: set[int] = set()
        self._seen_sports: set[str] = set()
        self._sport_idx: int = 0
        self._exits: dict[str, FadeExitState] = {}
        self._load()

    @staticmethod
    def _exit_key(condition_id: str, outcome: str) -> str:
        return f"{condition_id}:{outcome.lower()}"

    def _load(self) -> None:
        if self._seen_path.is_file():
            try:
                raw = json.loads(self._seen_path.read_text())
                if isinstance(raw, dict):
                    alerts = raw.get("alerts") or []
                    sports = raw.get("sports") or []
                    if isinstance(alerts, list):
                        self._seen_alerts = {int(x) for x in alerts}
                    if isinstance(sports, list):
                        self._seen_sports = {str(x) for x in sports}
                    idx = raw.get("sport_idx")
                    if idx is not None:
                        self._sport_idx = int(idx)
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        if not self._exit_path.is_file():
            return
        try:
            raw = json.loads(self._exit_path.read_text())
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
            self._exits[key] = FadeExitState(
                market_slug=slug,
                take_profit_limit_placed=bool(row.get("take_profit_limit_placed")),
                take_profit_price=float(tp) if tp is not None else None,
            )

    def _save_seen(self) -> None:
        self._seen_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "alerts": sorted(self._seen_alerts),
            "sports": sorted(self._seen_sports),
            "sport_idx": self._sport_idx,
        }
        self._seen_path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def _save_exits(self) -> None:
        self._exit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in self._exits.items()}
        self._exit_path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def seen_alert(self, alert_id: int) -> bool:
        return alert_id in self._seen_alerts

    def mark_alert(self, alert_id: int) -> None:
        self._seen_alerts.add(alert_id)
        self._save_seen()

    def seen_sports_key(self, key: str) -> bool:
        return key in self._seen_sports

    def mark_sports_key(self, key: str) -> None:
        self._seen_sports.add(key)
        self._save_seen()

    def next_sports(self, sports: tuple[str, ...], count: int) -> list[str]:
        """Rotate through sports list — one API call per scan on free tier."""
        if not sports or count <= 0:
            return []
        n = min(count, len(sports))
        out: list[str] = []
        idx = self._sport_idx
        for _ in range(n):
            out.append(sports[idx % len(sports)])
            idx += 1
        self._sport_idx = idx % len(sports)
        self._save_seen()
        return out

    def take_profit_placed(self, condition_id: str, outcome: str) -> bool:
        state = self._exits.get(self._exit_key(condition_id, outcome))
        return bool(state and state.take_profit_limit_placed)

    def mark_take_profit(
        self,
        condition_id: str,
        outcome: str,
        *,
        market_slug: str,
        take_profit_price: float,
    ) -> None:
        key = self._exit_key(condition_id, outcome)
        self._exits[key] = FadeExitState(
            market_slug=market_slug,
            take_profit_limit_placed=True,
            take_profit_price=take_profit_price,
        )
        self._save_exits()

    def unmark_take_profit(self, condition_id: str, outcome: str) -> None:
        key = self._exit_key(condition_id, outcome)
        if key in self._exits:
            del self._exits[key]
            self._save_exits()

    def prune_closed(self, open_positions: list[Position]) -> None:
        open_keys = {
            self._exit_key(p.market_condition_id, p.outcome)
            for p in open_positions
            if p.shares > 0 and not p.is_resolved
        }
        stale = [k for k in self._exits if k not in open_keys]
        if not stale:
            return
        for k in stale:
            del self._exits[k]
        self._save_exits()
