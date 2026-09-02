from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.buckets import parse_temperature_range
from papertrader.config import load_settings
from papertrader.markets import BucketMarket
from papertrader.strategies.obieweather import (
    _select_ladder_window,
    analyze_obieweather_event,
)
from papertrader.weather.ensemble import EnsembleForecast
from helpers import FakeLevel, sample_city


def _obie_city():
    return sample_city(
        name="Miami",
        slug="miami",
        strategies=("obieweather",),
    )


def _range_bucket(city, event_date, low: int, high: int, ask: float):
    slug = f"highest-temperature-in-miami-on-august-13-2026-{low}-{high}f"
    market = SimpleNamespace(
        slug=slug,
        question=f"Will the highest temperature in Miami be {low}-{high}°F?",
        closed=False,
        condition_id=f"0x{low}{high}",
        get_token_id=lambda outcome: f"token-{low}",
    )
    return BucketMarket(
        event_slug="highest-temperature-in-miami-on-august-13-2026",
        event_date=event_date,
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text=f"{low}-{high}°F",
        rng=parse_temperature_range(f"{low}-{high}°F"),  # type: ignore[arg-type]
        event_volume=5000,
    ), ask


def test_select_ladder_window_picks_contiguous_peak():
    from dataclasses import replace

    from papertrader.config import ObieWeatherSettings
    from papertrader.strategies.obieweather import _LadderLeg

    cfg = ObieWeatherSettings(
        min_yes_ask=0.03,
        max_yes_ask=0.40,
        min_yes_bets_per_event=3,
        max_yes_bets_per_event=4,
        max_event_usd=4.0,
        target_event_usd=0.60,
        max_ladder_price_sum=0.65,
        max_event_fraction=0.02,
        min_model_prob=0.08,
        min_ensemble_prob_sum=0.30,
        maker_tick=0.01,
        strict_limit=True,
        paper_fak_at_ask=False,
        min_event_volume=150,
        min_ensemble_members=8,
        max_open_positions=40,
        max_open_per_event=4,
        min_days_ahead=0,
        max_days_ahead=2,
        starting_balance=500.0,
        cities=(),
    )
    legs = [
        _LadderLeg(
            bucket=SimpleNamespace(bucket_text="80-81"),  # type: ignore[arg-type]
            p_model=0.10,
            src="gfs",
            ask=0.15,
            sort_key=80.5,
            limit_price=0.14,
        ),
        _LadderLeg(
            bucket=SimpleNamespace(bucket_text="82-83"),
            p_model=0.35,
            src="gfs",
            ask=0.18,
            sort_key=82.5,
            limit_price=0.17,
        ),
        _LadderLeg(
            bucket=SimpleNamespace(bucket_text="84-85"),
            p_model=0.30,
            src="gfs",
            ask=0.16,
            sort_key=84.5,
            limit_price=0.15,
        ),
        _LadderLeg(
            bucket=SimpleNamespace(bucket_text="86-87"),
            p_model=0.12,
            src="gfs",
            ask=0.20,
            sort_key=86.5,
            limit_price=0.19,
        ),
    ]
    window = _select_ladder_window(legs, cfg)
    assert window is not None
    assert len(window) == 4
    assert window[1].p_model == 0.35


def test_obieweather_emits_ladder_yes_signals(monkeypatch, tmp_path):
    settings = load_settings()
    city = _obie_city()
    event_date = date(2026, 8, 13)
    buckets = [
        _range_bucket(city, event_date, 82, 83, 0.15),
        _range_bucket(city, event_date, 84, 85, 0.18),
        _range_bucket(city, event_date, 86, 87, 0.12),
        _range_bucket(city, event_date, 88, 89, 0.10),
    ]
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)

    def _book(ask):
        return SimpleNamespace(
            asks=[FakeLevel(ask, 50)],
            bids=[FakeLevel(max(0.01, ask - 0.02), 20)],
        )

    engine.api.get_order_book.side_effect = lambda token: _book(
        {"token-82": 0.15, "token-84": 0.18, "token-86": 0.12, "token-88": 0.10}[token]
    )

    import papertrader.strategies.obieweather as obie_mod

    # Ensemble centered on 84–85°F range buckets.
    members = [83.0] * 4 + [84.5] * 8 + [86.0] * 8
    monkeypatch.setattr(
        obie_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast(tuple(members), "gfs:20"),
    )

    sigs = analyze_obieweather_event(
        engine,
        MagicMock(),
        city,
        event_date,
        [b[0] for b in buckets],
        settings,
        [],
        date(2026, 8, 12),
    )
    assert len(sigs) >= 3
    assert all(s.action == "buy" and s.outcome == "yes" for s in sigs)
    assert all(s.limit_price is not None and s.limit_price <= 0.40 for s in sigs)
    total_stake = sum(s.amount_usd or 0 for s in sigs)
    assert total_stake <= settings.obieweather.max_event_usd + 0.01


def test_obieweather_paper_fak_at_ask(monkeypatch, tmp_path):
    settings = load_settings()
    city = _obie_city()
    event_date = date(2026, 8, 13)
    buckets = [
        _range_bucket(city, event_date, 82, 83, 0.15),
        _range_bucket(city, event_date, 84, 85, 0.18),
        _range_bucket(city, event_date, 86, 87, 0.12),
        _range_bucket(city, event_date, 88, 89, 0.10),
    ]
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)

    def _book(ask):
        return SimpleNamespace(
            asks=[FakeLevel(ask, 50)],
            bids=[FakeLevel(max(0.01, ask - 0.02), 20)],
        )

    engine.api.get_order_book.side_effect = lambda token: _book(
        {"token-82": 0.15, "token-84": 0.18, "token-86": 0.12, "token-88": 0.10}[token]
    )

    import papertrader.strategies.obieweather as obie_mod

    members = [83.0] * 4 + [84.5] * 8 + [86.0] * 8
    monkeypatch.setattr(
        obie_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast(tuple(members), "gfs:20"),
    )

    sigs = analyze_obieweather_event(
        engine,
        MagicMock(),
        city,
        event_date,
        [b[0] for b in buckets],
        settings,
        [],
        date(2026, 8, 12),
        paper_mode=True,
    )
    assert len(sigs) >= 3
    assert all(s.order_type == "fak" for s in sigs)
    assert all(s.limit_price is not None and s.limit_price <= 0.40 for s in sigs)


def test_obieweather_skips_when_asks_too_high(monkeypatch, tmp_path):
    settings = load_settings()
    city = _obie_city()
    event_date = date(2026, 8, 13)
    bucket, ask = _range_bucket(city, event_date, 84, 85, 0.55)
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(ask, 50)],
        bids=[FakeLevel(0.50, 20)],
    )
    import papertrader.strategies.obieweather as obie_mod

    monkeypatch.setattr(
        obie_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast((84.0,) * 20, "gfs:20"),
    )
    sigs = analyze_obieweather_event(
        engine, MagicMock(), city, event_date, [bucket], settings, [], date(2026, 8, 12)
    )
    assert sigs == []
