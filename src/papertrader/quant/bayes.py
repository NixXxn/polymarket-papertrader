"""Bayesian odds updates for market-prior × evidence trading.

Desk form (Parlex): posterior_odds = prior_odds × LR, with LR = P(E|H)/P(E|¬H).
Price is the crowd prior; trade only a real gap after fees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_EPS = 1e-9
_DEFAULT_CLAMP = 1e-6


def clamp_prob(p: float, *, eps: float = _DEFAULT_CLAMP) -> float:
    """Keep probabilities away from 0/1 so odds stay finite."""
    return min(max(float(p), eps), 1.0 - eps)


def price_to_odds(p: float, *, eps: float = _DEFAULT_CLAMP) -> float:
    """Convert probability / price to decimal odds-against form o = p / (1−p)."""
    p = clamp_prob(p, eps=eps)
    return p / (1.0 - p)


def odds_to_price(odds: float, *, eps: float = _DEFAULT_CLAMP) -> float:
    """Convert odds o = p/(1−p) back to probability."""
    if odds <= 0:
        return eps
    return clamp_prob(odds / (1.0 + odds), eps=eps)


def update_odds(prior_p: float, lr: float, *, eps: float = _DEFAULT_CLAMP) -> float:
    """Bayes update in odds space: posterior_odds = prior_odds × LR."""
    if lr <= 0:
        raise ValueError("likelihood ratio must be positive")
    return odds_to_price(price_to_odds(prior_p, eps=eps) * lr, eps=eps)


def accumulate_log_odds(
    prior_p: float,
    lrs: list[float] | tuple[float, ...],
    *,
    eps: float = _DEFAULT_CLAMP,
) -> float:
    """Independent evidence: ℓ′ = ℓ + Σ log(LR_i). Equivalent to chained odds updates."""
    if any(lr <= 0 for lr in lrs):
        raise ValueError("all likelihood ratios must be positive")
    log_odds = math.log(price_to_odds(prior_p, eps=eps))
    for lr in lrs:
        log_odds += math.log(lr)
    return odds_to_price(math.exp(log_odds), eps=eps)


def edge_after_fees(
    posterior_p: float,
    ask: float,
    *,
    fee_buffer: float = 0.0,
) -> float:
    """Buy-side edge: posterior win prob minus ask minus a flat fee buffer.

    fee_buffer absorbs spread/fees without modeling Polymarket fee curves.
    """
    return float(posterior_p) - float(ask) - max(0.0, float(fee_buffer))


def implied_likelihood_ratio(
    prior_p: float,
    posterior_p: float,
    *,
    eps: float = _DEFAULT_CLAMP,
) -> float:
    """Bayes factor that takes prior → posterior: LR = odds(post) / odds(prior)."""
    prior_odds = price_to_odds(prior_p, eps=eps)
    post_odds = price_to_odds(posterior_p, eps=eps)
    if prior_odds <= 0:
        return 1.0
    return post_odds / prior_odds


def shrink_lr(
    lr: float,
    *,
    shrink: float = 0.5,
    min_lr: float = 0.05,
    max_lr: float = 10.0,
) -> float:
    """Pull LR toward 1 in log-space (conservative calibration), then clamp.

    shrink=1 keeps the raw LR; shrink=0 forces LR=1 (no update).
    """
    if lr <= 0:
        raise ValueError("likelihood ratio must be positive")
    s = min(max(float(shrink), 0.0), 1.0)
    if s == 0.0:
        shrunk = 1.0
    elif abs(lr - 1.0) < _EPS:
        shrunk = 1.0
    else:
        shrunk = math.exp(s * math.log(lr))
    return min(max(shrunk, float(min_lr)), float(max_lr))


def conservative_posterior(
    prior_p: float,
    evidence_p: float,
    *,
    shrink: float = 0.5,
    min_lr: float = 0.05,
    max_lr: float = 10.0,
    eps: float = _DEFAULT_CLAMP,
) -> tuple[float, float]:
    """Update market prior toward an evidence/model probability with shrunk LR.

    Returns (posterior_p, applied_lr). With shrink=1 and no clamp binding,
    posterior ≈ evidence_p.
    """
    raw_lr = implied_likelihood_ratio(prior_p, evidence_p, eps=eps)
    lr = shrink_lr(raw_lr, shrink=shrink, min_lr=min_lr, max_lr=max_lr)
    return update_odds(prior_p, lr, eps=eps), lr


@dataclass(frozen=True)
class BayesShadow:
    """Counterfactual Bayes path vs model-edge sizing (no trade impact)."""

    prior_yes: float
    evidence_yes: float
    lr_yes: float
    posterior_yes: float
    posterior_no: float
    model_edge_no: float
    bayes_edge_no: float
    fee_buffer: float

    @property
    def agree_sign(self) -> bool:
        """Both edges positive or both non-positive."""
        return (self.model_edge_no > 0) == (self.bayes_edge_no > 0)


def shadow_no_fade(
    *,
    prior_yes: float,
    model_yes: float,
    no_ask: float,
    shrink: float = 0.5,
    max_lr: float = 8.0,
    min_lr: float = 0.05,
    fee_buffer: float = 0.01,
) -> BayesShadow:
    """Market-prior Bayes update for a NO fade; compare to model NO edge."""
    post_yes, lr = conservative_posterior(
        prior_yes,
        model_yes,
        shrink=shrink,
        min_lr=min_lr,
        max_lr=max_lr,
    )
    post_no = 1.0 - post_yes
    model_no = 1.0 - clamp_prob(model_yes)
    model_edge = model_no - float(no_ask)
    bayes_edge = edge_after_fees(post_no, no_ask, fee_buffer=fee_buffer)
    return BayesShadow(
        prior_yes=clamp_prob(prior_yes),
        evidence_yes=clamp_prob(model_yes),
        lr_yes=lr,
        posterior_yes=post_yes,
        posterior_no=post_no,
        model_edge_no=model_edge,
        bayes_edge_no=bayes_edge,
        fee_buffer=float(fee_buffer),
    )
