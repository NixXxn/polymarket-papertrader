from __future__ import annotations

import pytest

from papertrader.quant.adaptive_kelly import (
    VolRegimeTracker,
    adaptive_kelly_fraction,
    vol_regime_multiplier,
)
from papertrader.quant.kelly import KellySizingEngine
from papertrader.quant.vol_regime import VolRegimeStore


def test_vol_regime_multiplier_calm_vs_hot():
    assert vol_regime_multiplier(0.01, 0.04) == pytest.approx(0.75)
    assert vol_regime_multiplier(0.04, 0.04) == 0.0
    assert vol_regime_multiplier(0.08, 0.04) == 0.0


def test_adaptive_kelly_fraction():
    f = adaptive_kelly_fraction(mu=0.02, sigma_current=0.01, sigma_rolling=0.04)
    # regime = 0.75, mu/sigma_r^2 = 0.02/0.0016 = 12.5 => 9.375
    assert f == pytest.approx(9.375)


def test_vol_tracker_past_only_no_lookahead():
    t = VolRegimeTracker(rolling_window=10, recent_window=3)
    prices = [0.10, 0.11, 0.105, 0.12, 0.115, 0.13, 0.125, 0.14]
    snaps = []
    for p in prices:
        t.observe(p)
        snaps.append(t.snapshot())
    # Each snapshot uses only returns seen so far.
    assert snaps[0].observations == 0
    assert snaps[3].observations == 3
    assert snaps[-1].sigma_rolling is not None


def test_kelly_shrinks_in_hot_regime():
    k = KellySizingEngine(max_bankroll_fraction=0.10, max_usd=100.0, min_usd=1.0)
    calm = k.compute(0.25, 0.10, bankroll=1000.0).stake_usd
    from papertrader.quant.adaptive_kelly import VolRegimeSnapshot

    hot = k.compute(
        0.25,
        0.10,
        bankroll=1000.0,
        regime=VolRegimeSnapshot(
            sigma_current=0.05,
            sigma_rolling=0.04,
            regime_multiplier=0.0,
            observations=20,
        ),
        min_regime_observations=8,
    )
    assert hot.skipped
    assert hot.reason.startswith("vol regime hot")

    warm = k.compute(
        0.25,
        0.10,
        bankroll=1000.0,
        regime=VolRegimeSnapshot(
            sigma_current=0.02,
            sigma_rolling=0.04,
            regime_multiplier=0.5,
            observations=20,
        ),
        min_regime_observations=8,
    )
    assert not warm.skipped
    assert warm.stake_usd == pytest.approx(calm * 0.5, rel=0.01)


def test_vol_regime_store_persists(tmp_path):
    store = VolRegimeStore(tmp_path, min_observations=2)
    for p in [0.10, 0.11, 0.12, 0.115]:
        snap = store.observe("slug-a", p)
    assert store.ready(snap)
    store2 = VolRegimeStore(tmp_path, min_observations=2)
    snap2 = store2.observe("slug-a", 0.12)
    assert snap2.observations >= 3
