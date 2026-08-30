"""PredictionHunt cross-platform price API (free-tier aware).

Docs: https://www.predictionhunt.com/api/docs
Auth: X-API-Key header. Env: PREDICTION_HUNT_API_KEY.

Free tier: 1 req/s, 1000 req/month, 10 matched-market req/month.
Prefer /v2/search (cross-platform groups with prices) over /v2/matching-markets.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from papertrader.config import PredictionHuntSettings
from papertrader.paths import root_data_dir
from papertrader.quant.shin import shin_fair_probs_from_asks

log = logging.getLogger("papertrader.predictionhunt")

_BASE_URL = "https://www.predictionhunt.com/api/v2"
_QUOTA_FILE = "predictionhunt_quota.json"
_CACHE_FILE = "predictionhunt_cache.json"


def predictionhunt_api_key() -> str:
    return os.environ.get("PREDICTION_HUNT_API_KEY", "").strip()


@dataclass(frozen=True)
class PlatformQuote:
    platform: str
    market_id: str
    yes_ask: float | None
    yes_bid: float | None
    last_price: float | None
    source_url: str | None = None


@dataclass(frozen=True)
class CrossPlatformBucket:
    """Cross-platform YES consensus for one bucket/outcome."""

    group_title: str
    polymarket_yes_ask: float | None
    consensus_yes: float | None
    platform_count: int
    dislocation: float | None  # PM yes_ask - consensus_yes (positive => PM rich)
    quotes: tuple[PlatformQuote, ...]
    source: str  # search | matching-markets


@dataclass
class _CacheEntry:
    fetched_at: str
    payload: dict[str, Any]

    def age_hours(self) -> float:
        try:
            fetched = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
        except ValueError:
            return float("inf")
        return (datetime.now(timezone.utc) - fetched).total_seconds() / 3600


class PredictionHuntQuota:
    """Local + header-aware quota for free-tier budgets."""

    def __init__(self, data_dir: Path | str) -> None:
        self._path = root_data_dir(Path(data_dir)) / _QUOTA_FILE
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return self._fresh_state()
        try:
            raw = json.loads(self._path.read_text())
            if isinstance(raw, dict):
                return raw
        except (json.JSONDecodeError, OSError):
            pass
        return self._fresh_state()

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "month": now.month,
            "monthly_used": 0,
            "matched_monthly_used": 0,
            "last_request_ts": 0.0,
            "remaining_month": None,
            "remaining_matched_month": None,
        }

    def _roll_month(self) -> None:
        now_month = datetime.now(timezone.utc).month
        if self._state.get("month") != now_month:
            self._state["month"] = now_month
            self._state["monthly_used"] = 0
            self._state["matched_monthly_used"] = 0

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2))

    def can_spend(
        self,
        *,
        count: int = 1,
        matched: bool = False,
        max_monthly: int,
        max_matched_monthly: int,
    ) -> bool:
        self._roll_month()
        monthly = int(self._state.get("monthly_used", 0))
        matched_used = int(self._state.get("matched_monthly_used", 0))
        if monthly + count > max_monthly:
            return False
        if matched and matched_used + count > max_matched_monthly:
            return False
        rem = self._state.get("remaining_month")
        if rem is not None and int(rem) < count:
            return False
        if matched:
            rem_m = self._state.get("remaining_matched_month")
            if rem_m is not None and int(rem_m) < count:
                return False
        return True

    def wait_for_rate_limit(self, min_interval: float) -> None:
        last = float(self._state.get("last_request_ts") or 0.0)
        elapsed = time.monotonic() - last
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    def record_request(
        self,
        *,
        headers: httpx.Headers,
        matched: bool = False,
    ) -> None:
        self._roll_month()
        self._state["monthly_used"] = int(self._state.get("monthly_used", 0)) + 1
        if matched:
            self._state["matched_monthly_used"] = (
                int(self._state.get("matched_monthly_used", 0)) + 1
            )
        self._state["last_request_ts"] = time.monotonic()
        rem = headers.get("X-RateLimit-Remaining-Month")
        if rem is not None:
            try:
                self._state["remaining_month"] = int(rem)
            except ValueError:
                pass
        rem_m = headers.get("X-RateLimit-Remaining-Matched-Month")
        if rem_m is None:
            rem_m = headers.get("X-RateLimit-Remaining-Match-Month")
        if rem_m is not None:
            try:
                self._state["remaining_matched_month"] = int(rem_m)
            except ValueError:
                pass
        self._save()
        log.info(
            "PredictionHunt quota: monthly %s (rem=%s) matched %s (rem=%s)",
            self._state["monthly_used"],
            self._state.get("remaining_month"),
            self._state.get("matched_monthly_used"),
            self._state.get("remaining_matched_month"),
        )

    def snapshot(self) -> dict[str, int | None]:
        self._roll_month()
        return {
            "monthly_used": int(self._state.get("monthly_used", 0)),
            "matched_monthly_used": int(self._state.get("matched_monthly_used", 0)),
            "remaining_month": self._state.get("remaining_month"),
            "remaining_matched_month": self._state.get("remaining_matched_month"),
        }


class PredictionHuntCache:
    def __init__(self, data_dir: Path | str) -> None:
        self._path = root_data_dir(Path(data_dir)) / _CACHE_FILE
        self._entries: dict[str, _CacheEntry] = self._load()

    def _load(self) -> dict[str, _CacheEntry]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, _CacheEntry] = {}
        for key, row in raw.items():
            if isinstance(row, dict) and "fetched_at" in row and "payload" in row:
                out[str(key)] = _CacheEntry(
                    fetched_at=str(row["fetched_at"]),
                    payload=row["payload"] if isinstance(row["payload"], dict) else {},
                )
        return out

    def get(self, key: str, *, ttl_hours: float) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None or entry.age_hours() >= ttl_hours:
            return None
        return entry.payload

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self._entries[key] = _CacheEntry(
            fetched_at=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serial = {
            k: {"fetched_at": v.fetched_at, "payload": v.payload}
            for k, v in self._entries.items()
        }
        self._path.write_text(json.dumps(serial, indent=2))


def search_query_from_event_slug(event_slug: str) -> str:
    """Build a >=3 char search query from a Polymarket weather event slug."""
    m = re.match(
        r"highest-temperature-in-([a-z0-9-]+)-on-([a-z]+)-(\d{1,2})-\d{4}",
        event_slug,
    )
    if m:
        city = m.group(1).replace("-", " ")
        return f"highest temperature {city} {m.group(2)} {m.group(3)}"
    return event_slug.replace("-", " ")[:80]


def search_queries_from_event_slug(event_slug: str) -> tuple[str, ...]:
    """Primary + broader fallback queries (cached separately)."""
    primary = search_query_from_event_slug(event_slug)
    m = re.match(
        r"highest-temperature-in-([a-z0-9-]+)-on-([a-z]+)-(\d{1,2})-\d{4}",
        event_slug,
    )
    if not m:
        return (primary,)
    city = m.group(1).replace("-", " ")
    month = m.group(2)
    broader = f"highest temperature {city} {month}"
    return (primary, broader)


def _norm_bucket_token(bucket_text: str) -> str:
    """Extract a match token from bucket labels like '23°C' or '73-74°F'."""
    text = bucket_text.lower().replace("°", "").replace("f", "").replace("c", "")
    nums = re.findall(r"\d+", text)
    return nums[0] if nums else bucket_text.lower()[:12]


def _group_matches_bucket(group_title: str, bucket_text: str) -> bool:
    title = group_title.lower()
    token = _norm_bucket_token(bucket_text)
    if token and token in title:
        return True
    # Celsius markets often use bare integer in title.
    bare = bucket_text.lower().replace("°c", "c").replace("°", "")
    return bare in title or bucket_text.lower() in title


def _quote_from_market(row: dict[str, Any]) -> PlatformQuote:
    def _f(key: str) -> float | None:
        val = row.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    return PlatformQuote(
        platform=str(row.get("platform") or row.get("source") or "").lower(),
        market_id=str(row.get("market_id") or row.get("id") or ""),
        yes_ask=_f("yes_ask"),
        yes_bid=_f("yes_bid"),
        last_price=_f("last_price"),
        source_url=row.get("source_url"),
    )


def _yes_price(q: PlatformQuote) -> float | None:
    for val in (q.yes_ask, q.last_price, q.yes_bid):
        if val is not None and 0.0 < val < 1.0:
            return val
    return None


def _consensus_yes(quotes: list[PlatformQuote], *, exclude_platform: str = "polymarket") -> float | None:
    prices: list[float] = []
    for q in quotes:
        if q.platform == exclude_platform:
            continue
        p = _yes_price(q)
        if p is not None:
            prices.append(p)
    if len(prices) >= 2:
        return statistics.median(prices)
    if len(prices) == 1:
        return prices[0]
    # Fallback: Shin fair across all platforms with asks.
    asks = [_yes_price(q) for q in quotes]
    clean = [a for a in asks if a is not None]
    if len(clean) >= 2:
        fair = shin_fair_probs_from_asks(clean)
        return statistics.mean(fair)
    return None


def extract_bucket_cross_platform(
    payload: dict[str, Any],
    *,
    bucket_text: str,
    polymarket_slug: str,
    source: str,
) -> CrossPlatformBucket | None:
    """Find the group matching bucket_text and compute PM vs consensus."""
    events = payload.get("events") or []
    if not isinstance(events, list):
        return None

    slug_tail = polymarket_slug.rsplit("-", 1)[-1]
    best: CrossPlatformBucket | None = None

    for event in events:
        if not isinstance(event, dict):
            continue
        groups = event.get("groups") or []
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            title = str(group.get("title") or "")
            if not _group_matches_bucket(title, bucket_text):
                continue
            markets = group.get("markets") or []
            if not isinstance(markets, list):
                continue
            quotes = [_quote_from_market(m) for m in markets if isinstance(m, dict)]
            pm_q = next((q for q in quotes if q.platform == "polymarket"), None)
            pm_yes = _yes_price(pm_q) if pm_q else None
            # Also accept market_id/slug match when title match is weak.
            if pm_q is None:
                for q in quotes:
                    if q.platform == "polymarket" and (
                        polymarket_slug in q.market_id or slug_tail in q.market_id
                    ):
                        pm_q = q
                        pm_yes = _yes_price(q)
                        break
            consensus = _consensus_yes(quotes)
            platform_count = int(group.get("platform_count") or len({q.platform for q in quotes}))
            dislocation = (
                (pm_yes - consensus)
                if pm_yes is not None and consensus is not None
                else None
            )
            candidate = CrossPlatformBucket(
                group_title=title,
                polymarket_yes_ask=pm_yes,
                consensus_yes=consensus,
                platform_count=platform_count,
                dislocation=dislocation,
                quotes=tuple(quotes),
                source=source,
            )
            if best is None or (dislocation or 0) > (best.dislocation or 0):
                best = candidate
    return best


class PredictionHuntClient:
    """Rate-limited, cached PredictionHunt client."""

    def __init__(
        self,
        data_dir: Path | str,
        settings: PredictionHuntSettings,
        *,
        api_key: str | None = None,
        timeout: float = 12.0,
    ) -> None:
        self._settings = settings
        self._api_key = (api_key or predictionhunt_api_key()).strip()
        self._quota = PredictionHuntQuota(data_dir)
        self._cache = PredictionHuntCache(data_dir)
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return self._settings.enabled and bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key, "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, Any], *, matched: bool = False) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        cfg = self._settings
        if not self._quota.can_spend(
            matched=matched,
            max_monthly=cfg.max_monthly_requests,
            max_matched_monthly=cfg.max_matched_monthly,
        ):
            log.warning("PredictionHunt quota exhausted (matched=%s)", matched)
            return None

        cache_key = f"{path}?{urlencode(sorted((k, str(v)) for k, v in params.items()))}"
        cached = self._cache.get(cache_key, ttl_hours=cfg.cache_ttl_hours)
        if cached is not None:
            return cached

        self._quota.wait_for_rate_limit(cfg.min_request_interval_seconds)
        url = f"{_BASE_URL}{path}"
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                resp = client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            log.warning("PredictionHunt request failed: %s", e)
            return None

        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "2")
            log.warning("PredictionHunt 429; retry after %ss", retry)
            return None

        if resp.status_code >= 400:
            log.warning("PredictionHunt HTTP %s: %s", resp.status_code, resp.text[:200])
            return None

        self._quota.record_request(headers=resp.headers, matched=matched)
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("success") is False:
            log.info("PredictionHunt no match: %s", data.get("message") or data.get("error"))
            return None

        self._cache.put(cache_key, data)
        return data

    def lookup_bucket(
        self,
        *,
        event_slug: str,
        polymarket_slug: str,
        bucket_text: str,
    ) -> CrossPlatformBucket | None:
        """Cross-platform YES prices for a weather bucket (search-first)."""
        if not self.enabled:
            return None

        search_hit: CrossPlatformBucket | None = None
        for query in search_queries_from_event_slug(event_slug):
            data = self._get(
                "/search", {"q": query, "limit": 20, "status": "active"}, matched=False
            )
            if not data:
                continue
            search_hit = extract_bucket_cross_platform(
                data,
                bucket_text=bucket_text,
                polymarket_slug=polymarket_slug,
                source="search",
            )
            if (
                search_hit is not None
                and search_hit.platform_count >= self._settings.min_cross_platform_count
            ):
                return search_hit
            if int(data.get("count") or 0) > 0 and search_hit is not None:
                break

        if not self._settings.use_matching_markets:
            return search_hit

        matched_data = self._get(
            "/matching-markets",
            {"polymarket_key": event_slug},
            matched=True,
        )
        if not matched_data:
            return search_hit
        matched = extract_bucket_cross_platform(
            matched_data,
            bucket_text=bucket_text,
            polymarket_slug=polymarket_slug,
            source="matching-markets",
        )
        if matched is not None and matched.platform_count >= self._settings.min_cross_platform_count:
            return matched
        return search_hit


def cross_platform_no_edge(
    *,
    cross: CrossPlatformBucket,
    pm_no_ask: float,
    min_dislocation: float,
) -> tuple[float | None, bool]:
    """NO edge from cross-platform consensus: (1-consensus) - no_ask if PM is rich."""
    if cross.consensus_yes is None or cross.dislocation is None:
        return None, False
    if cross.dislocation < min_dislocation:
        return None, False
    consensus_no = 1.0 - cross.consensus_yes
    edge = consensus_no - pm_no_ask
    return edge, edge > 0
