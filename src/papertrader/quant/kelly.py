from __future__ import annotations

from dataclasses import dataclass

from papertrader.quant.adaptive_kelly import VolRegimeSnapshot


@dataclass(frozen=True)
class KellyResult:
    """Quarter-Kelly sizing outcome for a single trade opportunity."""

    p: float
    q: float
    share_price: float
    b: float
    f_star: float
    quarter_f: float
    stake_usd: float | None
    stake_fraction: float
    skipped: bool
    reason: str
    regime_multiplier: float | None = None
    sigma_current: float | None = None
    sigma_rolling: float | None = None


class KellySizingEngine:
    """Quarter-Kelly bet sizing with hard bankroll caps."""

    def __init__(
        self,
        *,
        kelly_divisor: float = 4.0,
        max_bankroll_fraction: float = 0.05,
        max_usd: float = 25.0,
        min_usd: float = 1.0,
    ) -> None:
        self.kelly_divisor = kelly_divisor
        self.max_bankroll_fraction = max_bankroll_fraction
        self.max_usd = max_usd
        self.min_usd = min_usd

    @staticmethod
    def net_odds(share_price: float) -> float:
        if share_price <= 0 or share_price >= 1:
            raise ValueError(f"share_price must be in (0, 1), got {share_price}")
        return (1.0 / share_price) - 1.0

    @staticmethod
    def kelly_fraction(p: float, share_price: float) -> tuple[float, float, float]:
        """Return (f*, b, q). f* <= 0 means no edge."""
        if not 0.0 < p < 1.0:
            raise ValueError(f"p must be in (0, 1), got {p}")
        b = KellySizingEngine.net_odds(share_price)
        q = 1.0 - p
        f_star = (p * b - q) / b
        return f_star, b, q

    def compute(
        self,
        p: float,
        share_price: float,
        bankroll: float,
        *,
        regime: VolRegimeSnapshot | None = None,
        min_regime_observations: int = 0,
    ) -> KellyResult:
        f_star, b, q = self.kelly_fraction(p, share_price)
        if f_star <= 0:
            return KellyResult(
                p=p,
                q=q,
                share_price=share_price,
                b=b,
                f_star=f_star,
                quarter_f=0.0,
                stake_usd=None,
                stake_fraction=0.0,
                skipped=True,
                reason="negative kelly — market edge",
            )
        quarter_f = f_star / self.kelly_divisor
        cap_fraction = min(quarter_f, self.max_bankroll_fraction)
        cap_usd = min(cap_fraction * bankroll, self.max_usd)

        regime_mult: float | None = None
        sig_c: float | None = None
        sig_r: float | None = None
        if regime is not None and regime.observations >= min_regime_observations:
            sig_c = regime.sigma_current
            sig_r = regime.sigma_rolling
            regime_mult = regime.regime_multiplier
            if regime_mult is not None:
                if regime_mult <= 0:
                    return KellyResult(
                        p=p,
                        q=q,
                        share_price=share_price,
                        b=b,
                        f_star=f_star,
                        quarter_f=quarter_f,
                        stake_usd=None,
                        stake_fraction=0.0,
                        skipped=True,
                        reason="vol regime hot — σ_current ≥ σ_rolling",
                        regime_multiplier=regime_mult,
                        sigma_current=sig_c,
                        sigma_rolling=sig_r,
                    )
                cap_usd = min(cap_usd * regime_mult, self.max_usd)
                cap_fraction *= regime_mult

        if cap_usd < self.min_usd or bankroll < self.min_usd:
            return KellyResult(
                p=p,
                q=q,
                share_price=share_price,
                b=b,
                f_star=f_star,
                quarter_f=quarter_f,
                stake_usd=None,
                stake_fraction=cap_fraction,
                skipped=True,
                reason="stake below minimum or empty bankroll",
                regime_multiplier=regime_mult,
                sigma_current=sig_c,
                sigma_rolling=sig_r,
            )
        stake = round(cap_usd, 2)
        reason = "quarter-kelly"
        if regime_mult is not None and regime_mult < 1.0:
            reason = f"adaptive-kelly x{regime_mult:.2f}"
        return KellyResult(
            p=p,
            q=q,
            share_price=share_price,
            b=b,
            f_star=f_star,
            quarter_f=quarter_f,
            stake_usd=stake,
            stake_fraction=stake / bankroll if bankroll > 0 else 0.0,
            skipped=False,
            reason=reason,
            regime_multiplier=regime_mult,
            sigma_current=sig_c,
            sigma_rolling=sig_r,
        )
