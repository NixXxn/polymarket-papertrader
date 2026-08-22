"""Temperature bucket parsing, ported from weather-bot-v2.mjs."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

BucketType = Literal["range", "below", "above", "exact"]


@dataclass(frozen=True)
class TempRange:
    type: BucketType
    unit: Literal["F", "C"]
    min: float | None = None
    max: float | None = None
    threshold: float | None = None
    value: float | None = None


def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def to_unit(temp_f: float, unit: str) -> float:
    if unit.upper() == "C":
        return fahrenheit_to_celsius(temp_f)
    return temp_f


def parse_temperature_range(text: str | None) -> TempRange | None:
    if not text:
        return None

    # Polymarket slugs first — before loose "N-N[FC]" which falsely matches "...-2026-36c".
    m = re.search(r"-(\d+)-(\d+)f(?:$|-)", text, re.I)
    if m and float(m.group(1)) <= 150 and float(m.group(2)) <= 150:
        return TempRange(type="range", min=float(m.group(1)), max=float(m.group(2)), unit="F")
    m = re.search(r"-(\d+)-(\d+)c(?:$|-)", text, re.I)
    if m and float(m.group(1)) <= 80 and float(m.group(2)) <= 80:
        return TempRange(type="range", min=float(m.group(1)), max=float(m.group(2)), unit="C")
    m = re.search(r"-(\d+)forhigher(?:$|-)", text, re.I)
    if m:
        return TempRange(type="above", threshold=float(m.group(1)), unit="F")
    m = re.search(r"-(\d+)forbelow(?:$|-)", text, re.I)
    if m:
        return TempRange(type="below", threshold=float(m.group(1)), unit="F")
    m = re.search(r"-(\d+)corhigher(?:$|-)", text, re.I)
    if m:
        return TempRange(type="above", threshold=float(m.group(1)), unit="C")
    m = re.search(r"-(\d+)c(?:$|-)", text, re.I)
    if m and float(m.group(1)) <= 80:
        return TempRange(type="exact", value=float(m.group(1)), unit="C")

    # Question text: "be 36°C" / exact mode buckets.
    m = re.search(r"\bbe\s+(-?\d+)\s*°?\s*([FC])\b", text, re.I)
    if m:
        return TempRange(
            type="exact",
            value=float(m.group(1)),
            unit=m.group(2).upper(),  # type: ignore[arg-type]
        )

    # Require ° or whitespace before unit so years in slugs never look like ranges.
    m = re.search(r"(-?\d+)\s*-\s*(-?\d+)\s*(?:°\s*|\s+)([FC])\b", text, re.I)
    if m:
        return TempRange(
            type="range",
            min=float(m.group(1)),
            max=float(m.group(2)),
            unit=m.group(3).upper(),  # type: ignore[arg-type]
        )

    m = re.search(r"(-?\d+)\s*°?\s*([FC])\s+or\s+(below|lower)", text, re.I)
    if m:
        return TempRange(
            type="below",
            threshold=float(m.group(1)),
            unit=m.group(2).upper(),  # type: ignore[arg-type]
        )

    m = re.search(r"(-?\d+)\s*°?\s*([FC])\s+or\s+(above|higher)", text, re.I)
    if m:
        return TempRange(
            type="above",
            threshold=float(m.group(1)),
            unit=m.group(2).upper(),  # type: ignore[arg-type]
        )

    m = re.search(r"^(-?\d+)\s*°?\s*([FC])$", text.strip(), re.I)
    if m:
        return TempRange(
            type="exact",
            value=float(m.group(1)),
            unit=m.group(2).upper(),  # type: ignore[arg-type]
        )

    return None


def forecast_matches_range(forecast_f: float, rng: TempRange) -> bool:
    temp = to_unit(forecast_f, rng.unit)
    if rng.type == "range":
        assert rng.min is not None and rng.max is not None
        return rng.min <= temp <= rng.max
    if rng.type == "below":
        assert rng.threshold is not None
        return temp <= rng.threshold
    if rng.type == "above":
        assert rng.threshold is not None
        return temp >= rng.threshold
    if rng.type == "exact":
        assert rng.value is not None
        return round(temp) == rng.value
    return False


def bucket_width_score(rng: TempRange | None) -> float:
    if rng is None:
        return 0.0
    if rng.type in ("above", "below"):
        return 100.0
    if rng.type == "range":
        assert rng.min is not None and rng.max is not None
        return rng.max - rng.min + 1
    if rng.type == "exact":
        return 1.0
    return 0.0


def bucket_bounds_f(rng: TempRange) -> tuple[float | None, float | None]:
    """Inclusive low/high bounds in Fahrenheit. None means unbounded."""
    if rng.type == "range":
        lo, hi = rng.min, rng.max
    elif rng.type == "below":
        lo, hi = None, rng.threshold
    elif rng.type == "above":
        lo, hi = rng.threshold, None
    elif rng.type == "exact":
        lo = hi = rng.value
    else:
        return None, None
    if rng.unit == "C":
        lo = None if lo is None else celsius_to_fahrenheit(lo)
        hi = None if hi is None else celsius_to_fahrenheit(hi)
    return lo, hi


def select_best_bucket(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    ranked = sorted(candidates, key=lambda c: c["edge_percent"], reverse=True)
    best_edge = ranked[0]["edge_percent"]
    competitive = [c for c in ranked if best_edge - c["edge_percent"] <= 0.05]
    competitive.sort(key=lambda c: c["width_score"], reverse=True)
    return competitive[0]


def gaussian_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def p_bucket_gaussian(mu_f: float, sigma_f: float, rng: TempRange) -> float:
    """P(daily high in bucket) with 0.5° continuity correction on discrete °F buckets."""
    lo, hi = bucket_bounds_f(rng)
    if lo is None and hi is None:
        return 0.0
    if lo is None:
        return gaussian_cdf(hi + 0.5, mu_f, sigma_f)  # type: ignore[operator]
    if hi is None:
        return 1.0 - gaussian_cdf(lo - 0.5, mu_f, sigma_f)
    return max(0.0, gaussian_cdf(hi + 0.5, mu_f, sigma_f) - gaussian_cdf(lo - 0.5, mu_f, sigma_f))


def p_bucket_ensemble(members_f: list[float], rng: TempRange) -> float:
    if not members_f:
        return 0.0
    hits = sum(1 for t in members_f if forecast_matches_range(t, rng))
    return hits / len(members_f)


def horizon_sigma_f(days_ahead: int) -> float:
    if days_ahead <= 0:
        return 1.5
    if days_ahead == 1:
        return 2.5
    if days_ahead == 2:
        return 3.5
    return 4.5
