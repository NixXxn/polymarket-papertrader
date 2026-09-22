"""Runtime-managed copy-leader wallets (dashboard + settings).

Leaders are stored in the paper copy dir (~/.pm-trader/copy/wallets.json) so the
dashboard (any mode) and `papertrader run --strategy copy` share one list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from papertrader.config import Settings
from papertrader.paths import DEFAULT_DATA_DIR, DEFAULT_LIVE_DATA_DIR

_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def wallets_path(data_dir: Path | None = None) -> Path:
    """Leader list path.

    Live dashboard mode redirects to the paper copy dir so ``papertrader run
    --strategy copy`` always sees wallets added from either mode. Explicit
    temp/test data dirs keep their own file.
    """
    if data_dir is None:
        return DEFAULT_DATA_DIR / "copy" / "wallets.json"
    root = Path(data_dir)
    try:
        resolved = root.resolve()
        live_root = DEFAULT_LIVE_DATA_DIR.resolve()
        if resolved == live_root or resolved == (live_root / "copy"):
            return DEFAULT_DATA_DIR / "copy" / "wallets.json"
    except OSError:
        pass
    if root.name == "copy":
        return root / "wallets.json"
    return root / "copy" / "wallets.json"


def normalize_wallet(address: str) -> str:
    raw = str(address or "").strip().lower()
    if not _WALLET_RE.match(raw):
        raise ValueError("wallet must be a 0x-prefixed 40-hex address")
    return raw


def _load_raw(data_dir: Path | None = None) -> dict[str, Any]:
    path = wallets_path(data_dir)
    if not path.is_file():
        return {"wallets": []}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"wallets": []}
    if not isinstance(raw, dict):
        return {"wallets": []}
    rows = raw.get("wallets")
    if not isinstance(rows, list):
        return {"wallets": []}
    return {"wallets": rows}


def _save_raw(data_dir: Path | None, payload: dict[str, Any]) -> None:
    path = wallets_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def list_managed_wallets(data_dir: Path | None = None) -> list[dict[str, str]]:
    """Wallets stored in ~/.pm-trader/copy/wallets.json (dashboard-managed)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in _load_raw(data_dir).get("wallets") or []:
        if not isinstance(row, dict):
            continue
        try:
            addr = normalize_wallet(str(row.get("address") or row.get("wallet") or ""))
        except ValueError:
            continue
        if addr in seen:
            continue
        seen.add(addr)
        out.append(
            {
                "address": addr,
                "label": str(row.get("label") or "").strip(),
                "source": "dashboard",
            }
        )
    return out


def list_copy_wallets(data_dir: Path | None, settings: Settings) -> list[dict[str, str]]:
    """Leader wallets: dashboard wallets.json first, then optional settings fallbacks."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(addr: str, *, label: str = "", source: str) -> None:
        try:
            normalized = normalize_wallet(addr)
        except ValueError:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        out.append({"address": normalized, "label": label, "source": source})

    for row in list_managed_wallets(data_dir):
        _add(row["address"], label=row.get("label") or "", source="dashboard")

    cfg = settings.copy
    if getattr(cfg, "wallet", ""):
        _add(cfg.wallet, label=getattr(cfg, "username", "") or "", source="settings")
    for item in getattr(cfg, "wallets", ()) or ():
        if isinstance(item, dict):
            _add(
                str(item.get("address") or item.get("wallet") or ""),
                label=str(item.get("label") or ""),
                source="settings",
            )
        else:
            _add(str(item), source="settings")

    return out


def add_copy_wallet(
    data_dir: Path | None, address: str, *, label: str = ""
) -> dict[str, str]:
    addr = normalize_wallet(address)
    clean_label = str(label or "").strip()
    payload = _load_raw(data_dir)
    rows = [r for r in (payload.get("wallets") or []) if isinstance(r, dict)]
    for row in rows:
        try:
            if normalize_wallet(str(row.get("address") or row.get("wallet") or "")) == addr:
                row["address"] = addr
                if clean_label:
                    row["label"] = clean_label
                _save_raw(data_dir, {"wallets": rows})
                return {
                    "address": addr,
                    "label": str(row.get("label") or ""),
                    "source": "dashboard",
                }
        except ValueError:
            continue
    rows.append({"address": addr, "label": clean_label})
    _save_raw(data_dir, {"wallets": rows})
    return {"address": addr, "label": clean_label, "source": "dashboard"}


def remove_copy_wallet(data_dir: Path | None, address: str) -> bool:
    addr = normalize_wallet(address)
    payload = _load_raw(data_dir)
    rows = [r for r in (payload.get("wallets") or []) if isinstance(r, dict)]
    kept: list[dict[str, Any]] = []
    removed = False
    for row in rows:
        try:
            current = normalize_wallet(str(row.get("address") or row.get("wallet") or ""))
        except ValueError:
            kept.append(row)
            continue
        if current == addr:
            removed = True
            continue
        kept.append(row)
    if removed:
        _save_raw(data_dir, {"wallets": kept})
    return removed
