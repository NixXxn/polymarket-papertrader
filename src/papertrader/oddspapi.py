from __future__ import annotations

import json
import logging
import os
import re
import time
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


_MAX_TOURNAMENT_IDS_PER_REQUEST = 5
_REQUEST_COOLDOWN_S = 1.05

# Sport-specific moneyline (match winner) market / outcome IDs from OddsPapi /markets.
_MONEYLINE_MARKET: dict[int, tuple[str, str, str]] = {
    17: ("171", "171", "172"),  # CS2 Winner: 1 / 2
    18: ("181", "181", "182"),  # LoL Winner: 1 / 2
    16: ("161", "161", "162"),  # Dota 2
    61: ("611", "611", "612"),  # Valorant
}


def _outcome_price(outcomes: dict[str, Any], outcome_id: str) -> tuple[float, bool] | None:
    row = outcomes.get(outcome_id) or outcomes.get(int(outcome_id))  # type: ignore[arg-type]
    if not isinstance(row, dict):
        return None
    players = row.get("players") or {}
    player = players.get("0") or players.get(0)
    if not isinstance(player, dict):
        return None
    try:
        price = float(player["price"])
    except (KeyError, TypeError, ValueError):
        return None
    active = bool(player.get("active", True))
    return price, active


def _extract_moneyline_prices(
    markets: dict[str, Any], sport_id: int
) -> tuple[float, float] | None:
    """Return (p1_decimal_odds, p2_decimal_odds) for the sport moneyline, if usable."""
    spec = _MONEYLINE_MARKET.get(sport_id)
    if spec is None:
        return None
    market_id, o1, o2 = spec
    market = markets.get(market_id)
    if market is None:
        for key, value in markets.items():
            if str(key) == market_id:
                market = value
                break
    if not isinstance(market, dict):
        return None
    outcomes = market.get("outcomes") or {}
    if not isinstance(outcomes, dict):
        return None
    p1 = _outcome_price(outcomes, o1)
    p2 = _outcome_price(outcomes, o2)
    if p1 is None or p2 is None:
        return None
    price1, active1 = p1
    price2, active2 = p2
    if not (active1 and active2):
        return None
    if not (1.05 <= price1 <= 25.0 and 1.05 <= price2 <= 25.0):
        return None
    return price1, price2


def _fair_match_from_fixture(fixture: dict[str, Any], bookmaker_names: tuple[str, ...]) -> FairMatch | None:
    team1 = str(fixture.get("participant1Name") or "").strip().lower()
    team2 = str(fixture.get("participant2Name") or "").strip().lower()
    if not team1 or not team2:
        return None
    try:
        sport_id = int(fixture.get("sportId"))
    except (TypeError, ValueError):
        return None
    book_odds = fixture.get("bookmakerOdds") or {}
    if not isinstance(book_odds, dict):
        return None
    bookmaker_data = None
    for name in bookmaker_names:
        row = book_odds.get(name)
        if isinstance(row, dict) and row.get("markets"):
            bookmaker_data = row
            break
    if bookmaker_data is None:
        return None
    markets = bookmaker_data.get("markets") or {}
    if not isinstance(markets, dict):
        return None
    prices = _extract_moneyline_prices(markets, sport_id)
    if prices is None:
        return None
    p1_price, p2_price = prices
    try:
        fair_p1, fair_p2 = shin_probabilities(p1_price, p2_price)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return FairMatch(
        fixture_id=str(fixture.get("fixtureId") or ""),
        team1=team1,
        team2=team2,
        fair_p1=fair_p1,
        fair_p2=fair_p2,
        start_time=fixture.get("startTime"),
    )


class OddsPapiClient:
    def __init__(self, cfg: OddsPapiSettings, *, api_key: str) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self._client = httpx.Client(timeout=30.0)
        self._last_request_at = 0.0

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _REQUEST_COOLDOWN_S:
            time.sleep(_REQUEST_COOLDOWN_S - elapsed)

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        query["apiKey"] = self.api_key
        url = f"{self.cfg.base_url.rstrip('/')}/{path.lstrip('/')}"
        self._throttle()
        resp = self._client.get(url, params=query, headers={"Accept": "application/json"})
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    def _bookmaker_chain(self) -> tuple[str, ...]:
        seen: list[str] = []
        for name in (self.cfg.primary_bookmaker, *self.cfg.fallback_bookmakers):
            slug = str(name).strip().lower()
            if slug and slug not in seen:
                seen.append(slug)
        return tuple(seen)

    def _odds_by_tournaments(self, tournament_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch odds for up to 5 tournament IDs, trying bookmakers one at a time."""
        if not tournament_ids:
            return []
        ids = tournament_ids[:_MAX_TOURNAMENT_IDS_PER_REQUEST]
        last_exc: Exception | None = None
        for bookmaker in self._bookmaker_chain():
            try:
                odds_resp = self._get_json(
                    "/odds-by-tournaments",
                    params={
                        "tournamentIds": ",".join(ids),
                        "bookmakers": bookmaker,
                        "verbosity": 2,
                    },
                )
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if status in (400, 404):
                    log.debug(
                        "OddsPapi odds chunk skipped bookmaker=%s status=%s",
                        bookmaker,
                        status,
                    )
                    continue
                raise
            if isinstance(odds_resp, list):
                return [row for row in odds_resp if isinstance(row, dict)]
        if last_exc is not None:
            log.warning(
                "OddsPapi odds chunk empty for tournaments=%s (%s)",
                ",".join(ids),
                last_exc,
            )
        return []

    def fetch_fair_matches(self) -> list[FairMatch]:
        matches: list[FairMatch] = []
        bookmakers = self._bookmaker_chain()
        seen_fixtures: set[str] = set()
        for sport_id in self.cfg.sport_ids:
            try:
                tournaments = self._get_json("/tournaments", params={"sportId": sport_id})
            except httpx.HTTPStatusError as exc:
                log.warning("OddsPapi tournaments failed sportId=%s: %s", sport_id, exc)
                continue
            if not isinstance(tournaments, list):
                continue
            # Prefer soon-starting tournaments, then future.
            ranked = sorted(
                (
                    t
                    for t in tournaments
                    if isinstance(t, dict)
                    and (t.get("upcomingFixtures", 0) or t.get("futureFixtures", 0))
                ),
                key=lambda t: (
                    -(int(t.get("upcomingFixtures") or 0)),
                    -(int(t.get("futureFixtures") or 0)),
                ),
            )
            active = [str(t["tournamentId"]) for t in ranked if t.get("tournamentId") is not None]
            if not active:
                continue
            limit = max(1, min(self.cfg.max_tournaments_per_sport, len(active)))
            active = active[:limit]
            for i in range(0, len(active), _MAX_TOURNAMENT_IDS_PER_REQUEST):
                chunk = active[i : i + _MAX_TOURNAMENT_IDS_PER_REQUEST]
                try:
                    fixtures = self._odds_by_tournaments(chunk)
                except Exception as exc:
                    log.warning(
                        "OddsPapi odds failed sportId=%s chunk=%s: %s",
                        sport_id,
                        ",".join(chunk),
                        exc,
                    )
                    continue
                for fixture in fixtures:
                    fair = _fair_match_from_fixture(fixture, bookmakers)
                    if fair is None or not fair.fixture_id:
                        continue
                    if fair.fixture_id in seen_fixtures:
                        continue
                    seen_fixtures.add(fair.fixture_id)
                    matches.append(fair)
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
        # tournaments call per sport + up to ceil(max_tournaments/5) odds chunks (primary book).
        chunks = max(1, (self.cfg.max_tournaments_per_sport + _MAX_TOURNAMENT_IDS_PER_REQUEST - 1)
                     // _MAX_TOURNAMENT_IDS_PER_REQUEST)
        return len(self.cfg.sport_ids) * (1 + chunks)

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
            matches = client.fetch_fair_matches()
            # Charge the planned refresh budget (actual calls are throttled/chunked).
            self._quota.spend(cost)
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
