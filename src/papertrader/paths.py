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
