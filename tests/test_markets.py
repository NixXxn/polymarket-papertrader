from __future__ import annotations

from datetime import datetime, timezone

from papertrader.markets import city_local_today
from helpers import sample_city


def test_city_local_today_uses_city_timezone():
    seoul = sample_city(
        name="Seoul",
        slug="seoul",
        station="RKSI",
        lat=37.46,
        lon=126.44,
        tz="Asia/Seoul",
        strategies=("asymmetric",),
    )
    # 2026-08-17 22:00 UTC = 2026-08-18 07:00 in Seoul
    now = datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc)
    assert city_local_today(seoul, now).isoformat() == "2026-08-18"

    chicago = sample_city(
        name="Chicago",
        slug="chicago",
        station="KORD",
        lat=41.97,
        lon=-87.90,
        tz="America/Chicago",
        strategies=("asymmetric",),
    )
    # Same instant is still Aug 17 in Chicago (CDT)
    assert city_local_today(chicago, now).isoformat() == "2026-08-17"
