from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.buckets import parse_temperature_range
from papertrader.config import load_settings
from papertrader.markets import BucketMarket
from papertrader.quant.shin import shin_fair_probs_from_asks
from papertrader.strategies.contrarian import analyze_contrarian_event
from papertrader.weather.ensemble import EnsembleForecast
from helpers import FakeLevel, sample_city


def _contrarian_city():
    return sample_city(
        name="Denver",
        slug="denver",
        station="KDEN",
        strategies=("contrarian",),
    )


def _tail_bucket(city, event_date, *, yes_ask=0.12, no_ask=0.86, slug_suffix="100f-or-higher"):
    market = SimpleNamespace(
        slug=f"highest-temperature-in-denver-on-august-13-2026-{slug_suffix}",
        question="Will the highest temperature in Denver be 100°F or higher?",
        closed=False,
        condition_id=f"0x{slug_suffix}",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome, s=slug_suffix: (
            f"token-yes-{s}" if outcome.lower() == "yes" else f"token-no-{s}"
        ),
    )
    return BucketMarket(
        event_slug="highest-temperature-in-denver-on-august-13-2026",
        event_date=event_date,
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text="100°F or higher",
        rng=parse_temperature_range("100°F or higher"),  # type: ignore[arg-type]
        event_volume=8000,
    ), yes_ask, no_ask


def _mid_bucket(city, event_date):
    market = SimpleNamespace(
        slug="highest-temperature-in-denver-on-august-13-2026-88-89f",
        question="Will the highest temperature in Denver be 88-89°F?",
        closed=False,
        condition_id="0xmid",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: (
            "token-yes-mid" if outcome.lower() == "yes" else "token-no-mid"
        ),
    )
    bucket = BucketMarket(
        event_slug="highest-temperature-in-denver-on-august-13-2026",
        event_date=event_date,
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text="88-89°F",
        rng=parse_temperature_range("88-89°F"),  # type: ignore[arg-type]
        event_volume=8000,
    )
    return bucket, 0.35, 0.62


def test_shin_fair_probs_sum_to_one():
    probs = shin_fair_probs_from_asks([0.05, 0.10, 0.15, 0.75])
    assert len(probs) == 4
    assert abs(sum(probs) - 1.0) < 0.05


def _low_bucket(city, event_date):
    market = SimpleNamespace(
        slug="highest-temperature-in-denver-on-august-13-2026-70-71f",
        question="Will the highest temperature in Denver be 70-71°F?",
        closed=False,
        condition_id="0xlow",
        outcomes=["Yes", "No"],
        get_token_id=lambda outcome: (
            "token-yes-low" if outcome.lower() == "yes" else "token-no-low"
        ),
    )
    bucket = BucketMarket(
        event_slug="highest-temperature-in-denver-on-august-13-2026",
        event_date=event_date,
        city=city,
        market=market,  # type: ignore[arg-type]
        bucket_text="70-71°F",
        rng=parse_temperature_range("70-71°F"),  # type: ignore[arg-type]
        event_volume=8000,
    )
    return bucket, 0.52, 0.46


def test_contrarian_buys_no_on_overpriced_tail(monkeypatch, tmp_path):
    settings = load_settings()
    city = _contrarian_city()
    event_date = date(2026, 8, 13)
    bucket, yes_ask, no_ask = _tail_bucket(city, event_date, yes_ask=0.17, no_ask=0.81)
    mid, mid_yes, mid_no = _mid_bucket(city, event_date)
    low, low_yes, low_no = _low_bucket(city, event_date)
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)

    books = {
        "token-yes-100f-or-higher": SimpleNamespace(
            asks=[FakeLevel(yes_ask, 50)],
            bids=[FakeLevel(0.10, 20)],
        ),
        "token-no-100f-or-higher": SimpleNamespace(
            asks=[FakeLevel(no_ask, 50)],
            bids=[FakeLevel(0.84, 20)],
        ),
        "token-yes-mid": SimpleNamespace(
            asks=[FakeLevel(mid_yes, 50)],
            bids=[FakeLevel(0.33, 20)],
        ),
        "token-no-mid": SimpleNamespace(
            asks=[FakeLevel(mid_no, 50)],
            bids=[FakeLevel(0.60, 20)],
        ),
        "token-yes-low": SimpleNamespace(
            asks=[FakeLevel(low_yes, 50)],
            bids=[FakeLevel(0.48, 20)],
        ),
        "token-no-low": SimpleNamespace(
            asks=[FakeLevel(low_no, 50)],
            bids=[FakeLevel(0.46, 20)],
        ),
    }

    engine.api.get_order_book.side_effect = lambda token: books[token]

    import papertrader.strategies.contrarian as con_mod

    members = [98.0] * 19 + [101.0]
    monkeypatch.setattr(
        con_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast(tuple(members), "gfs:20"),
    )

    sigs = analyze_contrarian_event(
        engine,
        MagicMock(),
        city,
        event_date,
        [bucket, mid, low],
        settings,
        [],
        date(2026, 8, 11),
    )
    assert len(sigs) >= 1
    assert sigs[0].outcome == "no"
    assert sigs[0].order_type == "limit"
    assert sigs[0].limit_price is not None
    assert sigs[0].limit_price < no_ask


def test_contrarian_skips_when_yes_not_overpriced(monkeypatch, tmp_path):
    settings = load_settings()
    city = _contrarian_city()
    event_date = date(2026, 8, 13)
    bucket, _, no_ask = _tail_bucket(city, event_date, yes_ask=0.03, no_ask=0.95)
    mid, mid_yes, mid_no = _mid_bucket(city, event_date)
    low, low_yes, low_no = _low_bucket(city, event_date)
    engine = MagicMock()
    engine.db.data_dir = tmp_path

    books = {
        "token-yes-100f-or-higher": SimpleNamespace(
            asks=[FakeLevel(0.03, 50)],
            bids=[FakeLevel(0.02, 20)],
        ),
        "token-no-100f-or-higher": SimpleNamespace(
            asks=[FakeLevel(no_ask, 50)],
            bids=[FakeLevel(0.93, 20)],
        ),
        "token-yes-mid": SimpleNamespace(
            asks=[FakeLevel(mid_yes, 50)],
            bids=[FakeLevel(0.33, 20)],
        ),
        "token-no-mid": SimpleNamespace(
            asks=[FakeLevel(mid_no, 50)],
            bids=[FakeLevel(0.60, 20)],
        ),
        "token-yes-low": SimpleNamespace(
            asks=[FakeLevel(low_yes, 50)],
            bids=[FakeLevel(0.48, 20)],
        ),
        "token-no-low": SimpleNamespace(
            asks=[FakeLevel(low_no, 50)],
            bids=[FakeLevel(0.46, 20)],
        ),
    }

    engine.api.get_order_book.side_effect = lambda token: books[token]

    import papertrader.strategies.contrarian as con_mod

    monkeypatch.setattr(
        con_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast((90.0,) * 20, "gfs:20"),
    )

    sigs = analyze_contrarian_event(
        engine, MagicMock(), city, event_date, [bucket, mid, low], settings, [], date(2026, 8, 11)
    )
    assert sigs == []
