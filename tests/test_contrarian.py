from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.buckets import parse_temperature_range
from papertrader.config import load_settings
from papertrader.markets import BucketMarket
from papertrader.quant.shin import shin_fair_probs_from_asks
from papertrader.strategies.contrarian import (
    _duplicate_event_trim_slugs,
    _event_key,
    _model_yes_for_fade,
    _rank_score,
    analyze_contrarian_event,
    contrarian_exits,
)
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


def test_parse_slug_exact_celsius_not_year_range():
    slug = "highest-temperature-in-guangzhou-on-august-22-2026-36c"
    rng = parse_temperature_range(slug)
    assert rng is not None
    assert rng.type == "exact"
    assert rng.value == 36
    assert rng.unit == "C"
    q = parse_temperature_range("Will the highest temperature in Guangzhou be 36°C?")
    assert q is not None and q.type == "exact" and q.value == 36


def test_event_key_strips_bucket():
    assert (
        _event_key("highest-temperature-in-guangzhou-on-august-22-2026-36c")
        == "highest-temperature-in-guangzhou-on-august-22-2026"
    )


def test_rank_score_prefers_upside_and_liquidity():
    tight = _rank_score(p_win=0.97, edge=0.05, fill_no=0.92, ask_size=80)
    roomy = _rank_score(p_win=0.97, edge=0.05, fill_no=0.80, ask_size=80)
    thin = _rank_score(p_win=0.97, edge=0.05, fill_no=0.80, ask_size=3)
    assert roomy > tight
    assert roomy > thin


def test_duplicate_event_trim_keeps_cheaper_entry():
    keep = SimpleNamespace(
        shares=10,
        is_resolved=False,
        outcome="no",
        avg_entry_price=0.84,
        market_slug="highest-temperature-in-munich-on-august-23-2026-24c",
    )
    drop = SimpleNamespace(
        shares=10,
        is_resolved=False,
        outcome="no",
        avg_entry_price=0.90,
        market_slug="highest-temperature-in-munich-on-august-23-2026-21c",
    )
    trim = _duplicate_event_trim_slugs([keep, drop])
    assert drop.market_slug in trim
    assert keep.market_slug not in trim


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
            asks=[
                FakeLevel(0.78, 20),
                FakeLevel(no_ask, 50),
                FakeLevel(0.95, 200),  # past EV for typical P(no)≈0.95 — must not take
            ],
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
    monkeypatch.setattr(con_mod._VARIANCE, "from_openweather", lambda *a, **k: None)

    sigs = analyze_contrarian_event(
        engine,
        MagicMock(),
        city,
        event_date,
        [bucket, mid, low],
        settings,
        [],
        date(2026, 8, 13),
    )
    assert len(sigs) >= 1
    assert sigs[0].outcome == "no"
    assert sigs[0].order_type == "limit"
    assert sigs[0].limit_price is not None
    assert sigs[0].limit_price <= no_ask + 1e-9
    assert "limit@" in sigs[0].reason
    assert sigs[0].amount_usd is not None and sigs[0].amount_usd > 0


def test_contrarian_skips_mode_when_ensemble_says_likely(monkeypatch, tmp_path):
    """max(ensemble, OW) must not fade a mode bucket that ensemble says is likely."""
    settings = load_settings()
    city = _contrarian_city()
    event_date = date(2026, 8, 13)
    mid, mid_yes, mid_no = _mid_bucket(city, event_date)
    # Make mid look like a fade candidate on the book (cheap YES / rich NO).
    mid_yes, mid_no = 0.15, 0.82
    books = {
        "token-yes-mid": SimpleNamespace(
            asks=[FakeLevel(mid_yes, 50)],
            bids=[FakeLevel(0.12, 20)],
        ),
        "token-no-mid": SimpleNamespace(
            asks=[FakeLevel(mid_no, 50)],
            bids=[FakeLevel(0.80, 20)],
        ),
    }
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    engine.api.get_order_book.side_effect = lambda token: books[token]

    import papertrader.strategies.contrarian as con_mod

    # Ensemble mode is 88-89F → high P(YES). Fake OW that would have understated it.
    monkeypatch.setattr(
        con_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast((88.5,) * 20, "gfs:20"),
    )
    ow = SimpleNamespace(p=0.02, source="openweather")
    monkeypatch.setattr(con_mod._VARIANCE, "from_openweather", lambda *a, **k: ow)

    p, _ = _model_yes_for_fade(
        EnsembleForecast((88.5,) * 20, "gfs:20"),
        mid.rng,
        http=MagicMock(),
        city=city,
        event_date=event_date,
        local_today=date(2026, 8, 13),
    )
    assert p > settings.contrarian.max_model_yes

    sigs = analyze_contrarian_event(
        engine, MagicMock(), city, event_date, [mid], settings, [], date(2026, 8, 13)
    )
    assert sigs == []


def test_contrarian_skips_when_already_in_event(monkeypatch, tmp_path):
    settings = load_settings()
    city = _contrarian_city()
    event_date = date(2026, 8, 13)
    bucket, yes_ask, no_ask = _tail_bucket(city, event_date, yes_ask=0.17, no_ask=0.81)
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
    }
    engine.api.get_order_book.side_effect = lambda token: books[token]

    import papertrader.strategies.contrarian as con_mod

    monkeypatch.setattr(
        con_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast((98.0,) * 19 + (101.0,), "gfs:20"),
    )
    monkeypatch.setattr(con_mod._VARIANCE, "from_openweather", lambda *a, **k: None)

    open_pos = [
        SimpleNamespace(
            shares=10,
            is_resolved=False,
            market_slug="highest-temperature-in-denver-on-august-13-2026-88-89f",
            market_condition_id="0xother",
        )
    ]
    sigs = analyze_contrarian_event(
        engine, MagicMock(), city, event_date, [bucket], settings, open_pos, date(2026, 8, 13)
    )
    assert sigs == []


def test_contrarian_exit_requires_bid_drop(monkeypatch, tmp_path):
    settings = load_settings()
    city = _contrarian_city()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    market = SimpleNamespace(
        get_token_id=lambda outcome: "token-no",
    )
    engine.api.get_market.return_value = market
    # Bid still near entry — do not dump even if model P(YES) is high.
    engine.api.get_order_book.return_value = SimpleNamespace(
        bids=[FakeLevel(0.90, 20)],
        asks=[FakeLevel(0.92, 20)],
    )
    pos = SimpleNamespace(
        shares=10,
        outcome="no",
        avg_entry_price=0.91,
        market_slug="highest-temperature-in-denver-on-august-13-2026-100f-or-higher",
        market_question="Will the highest temperature in Denver be 100°F or higher?",
        market_condition_id="0xtail",
        is_resolved=False,
    )

    import papertrader.strategies.contrarian as con_mod

    monkeypatch.setattr(
        con_mod,
        "fetch_combined_ensemble",
        lambda *a, **k: EnsembleForecast((101.0,) * 20, "gfs:20"),
    )
    monkeypatch.setattr(con_mod, "fetch_metar_observed_high", lambda *a, **k: None)
    monkeypatch.setattr(
        con_mod,
        "is_mathematically_impossible",
        lambda *a, **k: (False, ""),
    )
    monkeypatch.setattr(con_mod._VARIANCE, "from_openweather", lambda *a, **k: None)

    now = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
    sigs = contrarian_exits(
        engine,
        MagicMock(),
        settings,
        [pos],
        {"denver": city},
        now=now,
    )
    assert sigs == []


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
    monkeypatch.setattr(con_mod._VARIANCE, "from_openweather", lambda *a, **k: None)

    sigs = analyze_contrarian_event(
        engine, MagicMock(), city, event_date, [bucket, mid, low], settings, [], date(2026, 8, 13)
    )
    assert sigs == []


def test_contrarian_skips_when_city_crowded(tmp_path):
    settings = load_settings()
    city = _contrarian_city()
    event_date = date(2026, 8, 13)
    bucket, _, _ = _tail_bucket(city, event_date)
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    open_pos = [
        SimpleNamespace(
            shares=10,
            is_resolved=False,
            market_slug="highest-temperature-in-denver-on-august-11-2026-94-95f",
            market_condition_id="0xa",
        ),
        SimpleNamespace(
            shares=10,
            is_resolved=False,
            market_slug="highest-temperature-in-denver-on-august-12-2026-96-97f",
            market_condition_id="0xb",
        ),
    ]
    sigs = analyze_contrarian_event(
        engine, MagicMock(), city, event_date, [bucket], settings, open_pos, date(2026, 8, 13)
    )
    assert sigs == []
