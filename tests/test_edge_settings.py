from papertrader.config import load_settings


def test_conviction_is_d0_only():
    s = load_settings()
    assert s.conviction.min_days_ahead == 0
    assert s.conviction.max_days_ahead == 0
    assert s.conviction.max_model_yes <= 0.05
    assert s.conviction.min_edge >= 0.045
    assert s.conviction.starting_balance == 2500


def test_closingsoon_weather_only():
    s = load_settings()
    assert s.closingsoon.weather_only is True


def test_safe_entries_disabled():
    s = load_settings()
    assert s.safe.max_open_positions == 0
