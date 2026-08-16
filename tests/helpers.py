from __future__ import annotations

from dataclasses import dataclass

from papertrader.config import City


def sample_city(**kwargs) -> City:
    base = dict(
        name="Miami",
        slug="miami",
        station="KMIA",
        lat=25.7959,
        lon=-80.2870,
        tz="America/New_York",
        country="US",
        strategies=("safe", "asymmetric"),
        position_usd=40.0,
    )
    base.update(kwargs)
    return City(**base)


@dataclass
class FakeLevel:
    price: float
    size: float
