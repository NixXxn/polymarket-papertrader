from __future__ import annotations

import math

import pytest

from papertrader.quant.bayes import (
    accumulate_log_odds,
    clamp_prob,
    conservative_posterior,
    edge_after_fees,
    implied_likelihood_ratio,
    odds_to_price,
    price_to_odds,
    shadow_no_fade,
    shrink_lr,
    update_odds,
)


def test_article_example_thirty_cents_times_lr_three():
    """Parlex desk example: market 0.30, LR=3 → posterior ≈ 0.5625."""
    post = update_odds(0.30, 3.0)
    assert post == pytest.approx(0.5625, abs=1e-9)
    # Gap the crowd left on the table before fees.
    assert post - 0.30 == pytest.approx(0.2625, abs=1e-9)


def test_odds_round_trip():
    for p in (0.05, 0.30, 0.5, 0.87, 0.99):
        assert odds_to_price(price_to_odds(p)) == pytest.approx(p, abs=1e-9)


def test_accumulate_matches_chained_update():
    prior = 0.30
    lrs = (2.0, 1.5, 0.8)
    chained = prior
    for lr in lrs:
        chained = update_odds(chained, lr)
    assert accumulate_log_odds(prior, lrs) == pytest.approx(chained, abs=1e-9)


def test_reject_nonpositive_lr():
    with pytest.raises(ValueError):
        update_odds(0.3, 0.0)
    with pytest.raises(ValueError):
        accumulate_log_odds(0.3, [2.0, -1.0])


def test_edge_after_fees():
    assert edge_after_fees(0.90, 0.84, fee_buffer=0.01) == pytest.approx(0.05)
    assert edge_after_fees(0.90, 0.90, fee_buffer=0.01) < 0


def test_implied_lr_recovers_posterior():
    prior, post = 0.20, 0.05
    lr = implied_likelihood_ratio(prior, post)
    assert update_odds(prior, lr) == pytest.approx(clamp_prob(post), abs=1e-9)


def test_shrink_lr_toward_one():
    assert shrink_lr(4.0, shrink=0.0) == pytest.approx(1.0)
    assert shrink_lr(4.0, shrink=1.0) == pytest.approx(4.0)
    # log-space half-way: exp(0.5 * log(4)) = 2
    assert shrink_lr(4.0, shrink=0.5) == pytest.approx(2.0)
    assert shrink_lr(100.0, shrink=1.0, max_lr=8.0) == pytest.approx(8.0)


def test_conservative_posterior_less_aggressive_than_model():
    prior, model = 0.18, 0.03
    post, lr = conservative_posterior(prior, model, shrink=0.5, max_lr=8.0)
    assert lr < 1.0
    # Shrunk update sits between prior and raw model.
    assert model < post < prior


def test_shadow_no_fade_fields():
    s = shadow_no_fade(
        prior_yes=0.18,
        model_yes=0.03,
        no_ask=0.84,
        shrink=0.5,
        fee_buffer=0.01,
    )
    assert s.posterior_no == pytest.approx(1.0 - s.posterior_yes)
    assert s.model_edge_no == pytest.approx(0.97 - 0.84)
    assert s.bayes_edge_no == pytest.approx(
        s.posterior_no - 0.84 - 0.01, abs=1e-9
    )
    assert math.isfinite(s.lr_yes)
