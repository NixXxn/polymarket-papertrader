from __future__ import annotations

import math

import pytest

from papertrader.buckets import parse_temperature_range
from papertrader.quant.kelly import KellySizingEngine
from papertrader.quant.variance import VarianceCalculator


def test_kelly_quarter_with_caps():
    k = KellySizingEngine(max_bankroll_fraction=0.05, max_usd=25.0)
    # p=0.25, price=0.10 => b=9, f*=(2.25-0.75)/9=0.1667
    r = k.compute(0.25, 0.10, bankroll=1000.0)
    assert not r.skipped
    assert r.f_star == pytest.approx(0.166666, rel=1e-3)
    assert r.quarter_f == pytest.approx(0.041666, rel=1e-3)
    assert r.stake_usd == 25.0  # capped at max_usd


def test_kelly_negative_edge_skips():
    k = KellySizingEngine()
    r = k.compute(0.05, 0.10, bankroll=500.0)
    assert r.skipped
    assert r.stake_usd is None


def test_variance_tail_probability():
    v = VarianceCalculator()
    rng = parse_temperature_range("95°F or higher")
    assert rng is not None
    est = v.from_forecast(93.0, rng, days_ahead=2, source="test")
    # P(high > 95) with mu=93, sigma=3.5 should be small but > 0
    assert 0.0 < est.p < 0.35
    assert est.sigma_f == pytest.approx(3.5)


def test_p_exceeds_threshold_matches_cdf():
    v = VarianceCalculator()
    p = v.p_exceeds_threshold(93.0, 2.0, 95.0)
    # 1 - Phi((94.5-93)/2)
    z = (94.5 - 93.0) / 2.0
    expected = 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    assert p == pytest.approx(expected, rel=1e-6)
