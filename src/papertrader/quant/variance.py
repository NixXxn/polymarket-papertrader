from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from papertrader.buckets import TempRange, gaussian_cdf, horizon_sigma_f, p_bucket_gaussian
from papertrader.config import City
from papertrader.weather.http import WeatherHttp
from papertrader.weather.openweather import fetch_openweather_daily_high


@dataclass(frozen=True)
class ProbabilityEstimate:
    """Model probability for a temperature bucket from a Gaussian forecast."""

    p: float
    mu_f: float
    sigma_f: float
    days_ahead: int
    source: str


class VarianceCalculator:
    """Map OpenWeather (or any point forecast) to bucket probability via Normal CDF."""

    def __init__(self, sigma_fn=horizon_sigma_f) -> None:
        self._sigma_fn = sigma_fn

    def sigma_for_horizon(self, days_ahead: int) -> float:
        return self._sigma_fn(days_ahead)

    @staticmethod
    def p_exceeds_threshold(mu_f: float, sigma_f: float, threshold_f: float) -> float:
        """P(daily high > threshold) with 0.5°F continuity correction."""
        if sigma_f <= 0:
            return 1.0 if mu_f > threshold_f else 0.0
        return 1.0 - gaussian_cdf(threshold_f - 0.5, mu_f, sigma_f)

    def from_forecast(
        self,
        mu_f: float,
        rng: TempRange,
        *,
        days_ahead: int,
        source: str = "openweather",
    ) -> ProbabilityEstimate:
        sigma = self.sigma_for_horizon(days_ahead)
        if rng.type == "above" and rng.threshold is not None:
            from papertrader.buckets import to_unit

            threshold_f = to_unit(rng.threshold, rng.unit)
            p = self.p_exceeds_threshold(mu_f, sigma, threshold_f)
        else:
            p = p_bucket_gaussian(mu_f, sigma, rng)
        return ProbabilityEstimate(
            p=p,
            mu_f=mu_f,
            sigma_f=sigma,
            days_ahead=days_ahead,
            source=source,
        )

    def from_openweather(
        self,
        http: WeatherHttp,
        city: City,
        event_date: date,
        rng: TempRange,
        *,
        today: date | None = None,
        api_key: str | None = None,
    ) -> ProbabilityEstimate | None:
        mu = fetch_openweather_daily_high(http, city, event_date, api_key=api_key)
        if mu is None:
            return None
        today = today or date.today()
        days_ahead = max(0, (event_date - today).days)
        return self.from_forecast(mu, rng, days_ahead=days_ahead, source="openweather")
