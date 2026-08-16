from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from papertrader.config import City


@dataclass
class Signal:
    action: Literal["buy", "sell"]
    slug: str
    outcome: str
    reason: str
    city: City | None = None
    amount_usd: float | None = None
    shares: float | None = None
    event_slug: str | None = None
