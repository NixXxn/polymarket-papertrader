from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".pm-trader"
DEFAULT_LIVE_DATA_DIR = Path.home() / ".pm-trader-live"


def data_dir_from_env(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    raw = os.environ.get("PAPERTRADER_DATA_DIR", "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_DATA_DIR


def root_data_dir(path: Path | str) -> Path:
    """Strategy engines live in {root}/{strategy}/; logs go under {root}/."""
    root = Path(path)
    if root.name in ("safe", "asymmetric", "copy", "edge", "esports", "momentum"):
        return root.parent
    return root
