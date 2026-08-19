from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from papertrader.config import OddsPapiSettings
from papertrader.esports_markets import EsportsCandidate
from papertrader.paths import root_data_dir
from papertrader.quant.shin import shin_probabilities

log = logging.getLogger("papertrader.oddspapi")

_QUOTA_FILE = "oddspapi_quota.json"
_CACHE_FILE = "oddspapi_cache.json"


def oddspapi_api_key() -> str:
    return os.environ.get("ODDSP_API_KEY", "").strip()


@dataclass(frozen=True)
class FairMatch:
    fixture_id: str
    team1: str
    team2: str
    fair_p1: float
    fair_p2: float
    start_time: str | None = None


@dataclass
class OddsPapiCache:
    fetched_at: str
    matches: list[FairMatch]

    def age_hours(self) -> float:
        try:
            fetched = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
        except ValueError:
            return float("inf")
        return (datetime.now(timezone.utc) - fetched).total_seconds() / 3600


class OddsPapiQuota:
    """Tracks daily/monthly OddsPapi request usage (250/month plan)."""

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
            "current_day": str(date.today()),
            "daily_used": 0,
            "month": now.month,
            "monthly_used": 0,
        }

    def _roll_periods(self) -> None:
        today_str = str(date.today())
        now_month = datetime.now(timezone.utc).month
        if self._state.get("month") != now_month:
            self._state["month"] = now_month
            self._state["monthly_used"] = 0
        if self._state.get("current_day") != today_str:
            self._state["current_day"] = today_str
            self._state["daily_used"] = 0

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2))

    def can_spend(self, count: int, *, max_daily: int, max_monthly: int) -> bool:
        self._roll_periods()
        daily = int(self._state.get("daily_used", 0))
        monthly = int(self._state.get("monthly_used", 0))
        return daily + count <= max_daily and monthly + count <= max_monthly

    def spend(self, count: int = 1) -> None:
        self._roll_periods()
        self._state["daily_used"] = int(self._state.get("daily_used", 0)) + count
        self._state["monthly_used"] = int(self._state.get("monthly_used", 0)) + count
        self._save()
        log.info(
            "OddsPapi quota: daily %s | monthly %s",
            self._state["daily_used"],
            self._state["monthly_used"],
        )

    def snapshot(self) -> dict[str, int]:
        self._roll_periods()
        return {
            "daily_used": int(self._state.get("daily_used", 0)),
            "monthly_used": int(self._state.get("monthly_used", 0)),
        }


def _norm_team(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _team_in_text(team: str, text: str) -> bool:
    team = team.strip().lower()
    if not team:
        return False
    if team in text:
        return True
    slug = _norm_team(team)
    if len(slug) >= 3 and slug in _norm_team(text):
        return True
    return False


def match_fair_probability(candidate: EsportsCandidate, fair: FairMatch) -> float | None:
    """Return Shin fair probability for the candidate outcome, if teams match."""
    text = " ".join(
        [
            candidate.event_title,
            candidate.market.slug,
            getattr(candidate.market, "question", ""),
        ]
    ).lower()
    if not (_team_in_text(fair.team1, text) and _team_in_text(fair.team2, text)):
        return None
    outcome = candidate.outcome.lower()
    if _team_in_text(fair.team1, outcome):
        return fair.fair_p1
    if _team_in_text(fair.team2, outcome):
        return fair.fair_p2
    return None


def find_fair_probability(
    candidate: EsportsCandidate, matches: list[FairMatch]
) -> tuple[FairMatch, float] | None:
    for fair in matches:
        prob = match_fair_probability(candidate, fair)
        if prob is not None:
            return fair, prob
    return None


def maker_buy_price(*, ask: float, fair_p: float, maker_edge_cents: float) -> float:
    maker = min(round(ask - 0.01, 2), round(fair_p - maker_edge_cents, 2))
    return max(0.01, min(0.99, maker))


def fractional_kelly_usd(
    *,
    fair_p: float,
    price: float,
    cash: float,
    kelly_fraction: float,
    max_usd: float,
    min_usd: float,
) -> float | None:
    if price <= 0 or price >= 1 or cash <= 0:
        return None
    b = (1.0 / price) - 1.0
    q = 1.0 - fair_p
    if b <= 0:
        return None
    kelly_f = (fair_p * b - q) / b
    if kelly_f <= 0.01:
        return None
    usd = min(max_usd, kelly_f * kelly_fraction * cash)
    usd = round(usd, 2)
    if usd < min_usd:
        return None
    return usd


class OddsPapiClient:
    def __init__(self, cfg: OddsPapiSettings, *, api_key: str) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        query["apiKey"] = self.api_key
        url = f"{self.cfg.base_url.rstrip('/')}/{path.lstrip('/')}"
        resp = self._client.get(url, params=query, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()

    def fetch_fair_matches(self) -> list[FairMatch]:
        matches: list[FairMatch] = []
        bookmakers = ",".join(
            [self.cfg.primary_bookmaker, *self.cfg.fallback_bookmakers]
        )
        for sport_id in self.cfg.sport_ids:
            tournaments = self._get_json(
                "/tournaments", params={"sportId": sport_id}
            )
            if not isinstance(tournaments, list):
                continue
            active = [
                str(t["tournamentId"])
                for t in tournaments
                if isinstance(t, dict)
                and (t.get("upcomingFixtures", 0) or t.get("futureFixtures", 0))
            ]
            if not active:
                continue
            odds_resp = self._get_json(
                "/odds-by-tournaments",
                params={
                    "tournamentIds": ",".join(active[: self.cfg.max_tournaments_per_sport]),
                    "bookmakers": bookmakers,
                },
            )
            if not isinstance(odds_resp, list):
                continue
            for fixture in odds_resp:
                if not isinstance(fixture, dict):
                    continue
                book_odds = fixture.get("bookmakerOdds") or {}
                if not isinstance(book_odds, dict):
                    continue
                bookmaker_data = None
                for name in (self.cfg.primary_bookmaker, *self.cfg.fallback_bookmakers):
                    row = book_odds.get(name)
                    if isinstance(row, dict) and row.get("markets"):
                        bookmaker_data = row
                        break
                if bookmaker_data is None:
                    continue
                markets = bookmaker_data.get("markets") or {}
                if not isinstance(markets, dict):
                    continue
                for market in markets.values():
                    if not isinstance(market, dict):
                        continue
                    outcomes = market.get("outcomes") or {}
                    if not isinstance(outcomes, dict) or len(outcomes) < 2:
                        continue
                    keys = list(outcomes.keys())
                    try:
                        p1_price = outcomes[keys[0]]["players"]["0"]["price"]
                        p2_price = outcomes[keys[1]]["players"]["0"]["price"]
                        fair_p1, fair_p2 = shin_probabilities(float(p1_price), float(p2_price))
                    except (KeyError, TypeError, ValueError, ZeroDivisionError):
                        continue
                    matches.append(
                        FairMatch(
                            fixture_id=str(fixture.get("fixtureId") or ""),
                            team1=str(fixture.get("participant1Name") or "").lower(),
                            team2=str(fixture.get("participant2Name") or "").lower(),
                            fair_p1=fair_p1,
                            fair_p2=fair_p2,
                            start_time=fixture.get("startTime"),
                        )
                    )
                    break
        return matches


class OddsPapiService:
    """Cached fair-odds lookup with quota-aware refresh."""

    def __init__(self, data_dir: Path | str, cfg: OddsPapiSettings) -> None:
        self._root = root_data_dir(Path(data_dir))
        self.cfg = cfg
        self._quota = OddsPapiQuota(self._root)
        self._cache_path = self._root / _CACHE_FILE
        self._cache: OddsPapiCache | None = None

    def _load_cache(self) -> OddsPapiCache | None:
        if not self._cache_path.is_file():
            return None
        try:
            raw = json.loads(self._cache_path.read_text())
            if not isinstance(raw, dict):
                return None
            rows = raw.get("matches") or []
            matches = [
                FairMatch(**row)
                for row in rows
                if isinstance(row, dict) and row.get("team1") and row.get("team2")
            ]
            fetched_at = str(raw.get("fetched_at") or "")
            if not fetched_at:
                return None
            return OddsPapiCache(fetched_at=fetched_at, matches=matches)
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def _save_cache(self, matches: list[FairMatch]) -> OddsPapiCache:
        fetched_at = datetime.now(timezone.utc).isoformat()
        cache = OddsPapiCache(fetched_at=fetched_at, matches=matches)
        payload = {
            "fetched_at": fetched_at,
            "matches": [asdict(m) for m in matches],
        }
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(payload, indent=2))
        self._cache = cache
        return cache

    def _requests_per_refresh(self) -> int:
        return 2 * len(self.cfg.sport_ids)

    def _needs_refresh(self, cache: OddsPapiCache | None) -> bool:
        if cache is None:
            return True
        return cache.age_hours() >= self.cfg.refresh_interval_hours

    def refresh_if_needed(self) -> OddsPapiCache | None:
        api_key = oddspapi_api_key()
        if not self.cfg.enabled or not api_key:
            return self._load_cache()

        cache = self._cache or self._load_cache()
        if not self._needs_refresh(cache):
            self._cache = cache
            return cache

        cost = self._requests_per_refresh()
        if not self._quota.can_spend(
            cost,
            max_daily=self.cfg.max_daily_requests,
            max_monthly=self.cfg.max_monthly_requests,
        ):
            log.warning(
                "OddsPapi quota exhausted — using cached fair odds (%s matches)",
                len(cache.matches) if cache else 0,
            )
            self._cache = cache
            return cache

        client = OddsPapiClient(self.cfg, api_key=api_key)
        try:
            matches: list[FairMatch] = []
            for sport_id in self.cfg.sport_ids:
                if not self._quota.can_spend(
                    2,
                    max_daily=self.cfg.max_daily_requests,
                    max_monthly=self.cfg.max_monthly_requests,
                ):
                    break
                tournaments = client._get_json(
                    "/tournaments", params={"sportId": sport_id}
                )
                self._quota.spend(1)
                if not isinstance(tournaments, list):
                    continue
                active = [
                    str(t["tournamentId"])
                    for t in tournaments
                    if isinstance(t, dict)
                    and (t.get("upcomingFixtures", 0) or t.get("futureFixtures", 0))
                ]
                if not active:
                    continue
                if not self._quota.can_spend(
                    1,
                    max_daily=self.cfg.max_daily_requests,
                    max_monthly=self.cfg.max_monthly_requests,
                ):
                    break
                odds_resp = client._get_json(
                    "/odds-by-tournaments",
                    params={
                        "tournamentIds": ",".join(
                            active[: self.cfg.max_tournaments_per_sport]
                        ),
                        "bookmakers": ",".join(
                            [self.cfg.primary_bookmaker, *self.cfg.fallback_bookmakers]
                        ),
                    },
                )
                self._quota.spend(1)
                if not isinstance(odds_resp, list):
                    continue
                for fixture in odds_resp:
                    if not isinstance(fixture, dict):
                        continue
                    book_odds = fixture.get("bookmakerOdds") or {}
                    if not isinstance(book_odds, dict):
                        continue
                    bookmaker_data = None
                    for name in (
                        self.cfg.primary_bookmaker,
                        *self.cfg.fallback_bookmakers,
                    ):
                        row = book_odds.get(name)
                        if isinstance(row, dict) and row.get("markets"):
                            bookmaker_data = row
                            break
                    if bookmaker_data is None:
                        continue
                    markets = bookmaker_data.get("markets") or {}
                    if not isinstance(markets, dict):
                        continue
                    for market in markets.values():
                        if not isinstance(market, dict):
                            continue
                        outcomes = market.get("outcomes") or {}
                        if not isinstance(outcomes, dict) or len(outcomes) < 2:
                            continue
                        keys = list(outcomes.keys())
                        try:
                            p1_price = outcomes[keys[0]]["players"]["0"]["price"]
                            p2_price = outcomes[keys[1]]["players"]["0"]["price"]
                            fair_p1, fair_p2 = shin_probabilities(
                                float(p1_price), float(p2_price)
                            )
                        except (KeyError, TypeError, ValueError, ZeroDivisionError):
                            continue
                        matches.append(
                            FairMatch(
                                fixture_id=str(fixture.get("fixtureId") or ""),
                                team1=str(fixture.get("participant1Name") or "").lower(),
                                team2=str(fixture.get("participant2Name") or "").lower(),
                                fair_p1=fair_p1,
                                fair_p2=fair_p2,
                                start_time=fixture.get("startTime"),
                            )
                        )
                        break
        except Exception as exc:
            log.warning("OddsPapi refresh failed: %s", exc)
            self._cache = cache
            return cache
        finally:
            client.close()

        cache = self._save_cache(matches)
        log.info("OddsPapi refreshed: %d fair matches", len(matches))
        return cache

    def fair_matches(self) -> list[FairMatch]:
        cache = self.refresh_if_needed()
        return cache.matches if cache else []

    def quota_snapshot(self) -> dict[str, int]:
        return self._quota.snapshot()
