from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from papertrader.config import City


@dataclass
class QuantMeta:
    p: float
    sigma: float
    f_star: float
    kelly_fraction: float
    source: str = ""


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
    quant: QuantMeta | None = None
    order_type: Literal["fak", "limit"] = "limit"
    limit_price: float | None = None
    partial_exit: bool = False
    ladder_multiple: float | None = None
    esports_take_profit: bool = False
    momentum_take_profit: bool = False
    market_condition_id: str | None = None
