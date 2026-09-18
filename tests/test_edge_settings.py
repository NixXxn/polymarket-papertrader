from papertrader.config import load_settings


def test_conviction_is_d0_only():
    s = load_settings()
    assert s.conviction.min_days_ahead == 0
    assert s.conviction.max_days_ahead == 0
    assert s.conviction.max_model_yes <= 0.05
    assert s.conviction.min_edge >= 0.042
    assert s.conviction.starting_balance == 2500


def test_momentum_meanrev_volspike_enabled():
    s = load_settings()
    assert s.momentum.max_open_positions >= 1
    assert s.meanrev.max_open_positions >= 1
    assert s.volspike.max_open_positions >= 1
