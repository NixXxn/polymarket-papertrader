from __future__ import annotations

from datetime import datetime, timezone

from papertrader.strategies.btc5m import predict_direction


def test_predict_direction_late_confirmed_up():
    pred = predict_direction(
        spot=70_100.0,
        open_px=70_000.0,
        seconds_left=45.0,
        min_confirm_bps=8.0,
        max_entry_seconds_left=120.0,
        min_entry_seconds_left=12.0,
    )
    assert pred is not None
    assert pred.side == "Up"
    assert pred.model_p >= 0.72
    assert pred.confidence in ("high", "very_high")


def test_predict_direction_rejects_early_window():
    pred = predict_direction(
        spot=70_200.0,
        open_px=70_000.0,
        seconds_left=200.0,
        min_confirm_bps=8.0,
        max_entry_seconds_left=120.0,
        min_entry_seconds_left=12.0,
    )
    assert pred is None


def test_predict_direction_rejects_weak_move():
    pred = predict_direction(
        spot=70_010.0,
        open_px=70_000.0,
        seconds_left=40.0,
        min_confirm_bps=8.0,
        max_entry_seconds_left=120.0,
        min_entry_seconds_left=12.0,
    )
    assert pred is None


def test_predict_direction_down_side():
    pred = predict_direction(
        spot=69_850.0,
        open_px=70_000.0,
        seconds_left=30.0,
        min_confirm_bps=8.0,
        max_entry_seconds_left=120.0,
        min_entry_seconds_left=12.0,
    )
    assert pred is not None
    assert pred.side == "Down"
    assert pred.move_bps < 0


def test_load_settings_includes_btc5m():
    from papertrader.config import load_settings

    settings = load_settings()
    assert settings.btc5m.max_open_positions >= 1
    assert settings.btc5m.min_confirm_bps > 0
