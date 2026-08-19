from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from papertrader.buckets import (
    forecast_matches_range,
    horizon_sigma_f,
    p_bucket_ensemble,
    p_bucket_gaussian,
    parse_temperature_range,
    select_best_bucket,
)
from papertrader.gfs import effective_edge_threshold, gfs_in_window
from papertrader.markets import (
    date_from_temp_slug,
    event_slug_from_market_slug,
    polymarket_event_url,
    temperature_event_slug,
)
from papertrader.weather.consensus import Consensus
from papertrader.weather.impossibility import is_mathematically_impossible
from papertrader.config import load_settings
from helpers import sample_city


def edge_settings():
    return load_settings().edge


def test_settings_default_mode_is_paper():
    settings = load_settings()
    assert settings.mode == "paper"
    assert not settings.is_live
    assert settings.live.chain_id == 137


def test_parse_range_and_or_higher():
    rng = parse_temperature_range("76-77°F")
    assert rng and rng.type == "range" and rng.min == 76 and rng.max == 77
    above = parse_temperature_range("90°F or higher")
    assert above and above.type == "above" and above.threshold == 90
    below = parse_temperature_range("50°F or below")
    assert below and below.type == "below"
    exact = parse_temperature_range("72°F")
    assert exact and exact.type == "exact" and exact.value == 72


def test_forecast_matches_range():
    rng = parse_temperature_range("76-77°F")
    assert forecast_matches_range(76.4, rng)
    assert not forecast_matches_range(78.0, rng)
    c_rng = parse_temperature_range("24-25°C")
    assert forecast_matches_range(75.2, c_rng)  # 24C = 75.2F


def test_select_best_bucket_prefers_width_near_edge():
    best = select_best_bucket(
        [
            {"edge_percent": 0.20, "width_score": 1},
            {"edge_percent": 0.19, "width_score": 100},
        ]
    )
    assert best["width_score"] == 100


def test_gaussian_and_ensemble_probability():
    rng = parse_temperature_range("80-81°F")
    p_center = p_bucket_gaussian(80.5, 1.5, rng)
    p_far = p_bucket_gaussian(70.0, 1.5, rng)
    assert p_center > 0.2
    assert p_far < 0.01
    members = [80.0, 80.2, 79.0, 90.0]
    assert p_bucket_ensemble(members, rng) == 0.5
    assert horizon_sigma_f(0) < horizon_sigma_f(3)


def test_gfs_threshold():
    now = datetime(2026, 8, 13, 3, 0, tzinfo=ZoneInfo("UTC"))  # 02-04 window after 00Z
    assert gfs_in_window(now) is True
    off = datetime(2026, 8, 13, 5, 0, tzinfo=ZoneInfo("UTC"))
    assert gfs_in_window(off) is False
    t = effective_edge_threshold(
        confidence="very_high",
        in_window=True,
        min_edge=0.15,
        min_edge_high=0.25,
        min_edge_low=0.10,
    )
    assert t == 0.10


def test_slug_helpers():
    d = date(2026, 8, 13)
    slug = temperature_event_slug("los-angeles", d)
    assert slug == "highest-temperature-in-los-angeles-on-august-13-2026"
    assert date_from_temp_slug(slug) == d
    market = f"{slug}-94-95f"
    assert event_slug_from_market_slug(market) == slug
    assert polymarket_event_url(market) == f"https://polymarket.com/event/{slug}"


def test_impossibility_already_above_bucket():
    city = sample_city()
    rng = parse_temperature_range("76-77°F")
    now = datetime(2026, 8, 13, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    dead, why = is_mathematically_impossible(
        rng,
        city=city,
        event_date=date(2026, 8, 13),
        settings=edge_settings(),
        observed_high_f=82.0,
        ensemble_p95_f=83.0,
        now=now,
    )
    assert dead
    assert "already above" in why


def test_impossibility_cannot_reach_min():
    city = sample_city()
    rng = parse_temperature_range("95-96°F")
    now = datetime(2026, 8, 13, 21, 0, tzinfo=ZoneInfo("America/New_York"))  # after high_hour 20
    dead, why = is_mathematically_impossible(
        rng,
        city=city,
        event_date=date(2026, 8, 13),
        settings=edge_settings(),
        observed_high_f=88.0,
        ensemble_p95_f=89.0,
        now=now,
    )
    assert dead
    assert "cannot reach" in why


def test_still_possible_future_day():
    city = sample_city()
    rng = parse_temperature_range("95-96°F")
    now = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    dead, _ = is_mathematically_impossible(
        rng,
        city=city,
        event_date=date(2026, 8, 13),
        settings=edge_settings(),
        observed_high_f=70.0,
        ensemble_p95_f=90.0,
        now=now,
    )
    assert not dead


def test_consensus_skip_confidence():
    c = Consensus(80.0, "skip", 78.0, 82.5, 4.5, "consensus")
    assert c.confidence == "skip"
    assert c.diff and c.diff > 3


def test_fmt_money():
    from papertrader.report import fmt_money, fmt_pnl

    assert fmt_money(1234.56) == "$1.234,56"
    assert fmt_pnl(-1.5) == "-$1,50"
    assert fmt_pnl(0) == "+$0,00"


def test_format_scan_update_includes_strategy_breakdown():
    from papertrader.report import (
        CombinedStats,
        OpenPositionLink,
        ScanCounts,
        StrategyStats,
        format_scan_update,
    )

    text = format_scan_update(
        ScanCounts(candidates=10, pending=3),
        CombinedStats(
            cash=72.0,
            positions=19.0,
            total=91.0,
            pnl=-9.0,
            unrealized_pnl=0.0,
            roi_pct=-9.0,
            trades=12,
            buys=12,
            sells=0,
            win_rate=0.0,
            max_drawdown=0.4,
            fees=0.0,
            avg_trade=2.33,
            by_strategy=[
                StrategyStats("safe", trades=2, buys=2, sells=0, win_rate=0.0),
                StrategyStats("edge", trades=10, buys=10, sells=0, win_rate=0.25),
            ],
            open_positions=[
                OpenPositionLink(
                    strategy="safe",
                    outcome="Yes",
                    label="Highest temperature in Atlanta on August 13? 94-95°F",
                    url="https://polymarket.com/event/highest-temperature-in-atlanta-on-august-13-2026",
                ),
            ],
        ),
    )
    assert "safe 2 (2 buys/0 sells), win rate 0.0%;" in text
    assert "edge 10 (10 buys/0 sells), win rate 25.0%;" in text
    assert "Open positions:" in text
    assert (
        "safe Yes Highest temperature in Atlanta on August 13? 94-95°F "
        "https://polymarket.com/event/highest-temperature-in-atlanta-on-august-13-2026"
    ) in text


def test_scaled_size_tracks_cash_budget():
    from papertrader.sizing import scaled_size

    kwargs = dict(starting_balance=50.0, remaining_slots=6, min_usd=1.0)
    assert scaled_size(5.0, cash=50.0, **kwargs) == 5.0
    assert scaled_size(5.0, cash=25.0, **kwargs) == 2.5
    assert scaled_size(5.0, cash=100.0, **kwargs) == 10.0
    assert scaled_size(5.0, cash=0.50, **kwargs) is None
    # Split remaining cash across last slots instead of dumping it in one bet.
    assert (
        scaled_size(5.0, cash=100.0, starting_balance=50.0, remaining_slots=20, min_usd=1.0)
        == 5.0
    )
    # Edge max also scales with bankroll.
    assert (
        scaled_size(
            2.0,
            cash=200.0,
            starting_balance=50.0,
            remaining_slots=10,
            min_usd=1.0,
            max_usd=5.0,
        )
        == 8.0
    )
