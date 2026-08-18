from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pm_trader.engine import Engine
from pm_trader.models import Market

from papertrader.config import Settings
from papertrader.decision_log import classify_ask_reject
from papertrader.markets import _market_from_event_row


@dataclass(frozen=True)
class EsportsCandidate:
    event_slug: str
    event_title: str
    market: Market
    outcome: str
    end_at: datetime
    event_volume: float
    ask: float
    ask_size: float


@dataclass
class EsportsScanStats:
    events_seen: int = 0
    events_in_horizon: int = 0
    match_markets: int = 0
    candidates: int = 0
    rejects: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    notable: list[dict[str, object]] = field(default_factory=list)

    def bump(self, reason: str, count: int = 1) -> None:
        self.rejects[reason] = self.rejects.get(reason, 0) + count

    def summary(self) -> str:
        parts = [
            f"{self.candidates} buyable",
            f"{self.match_markets} match markets",
            f"{self.events_in_horizon} live events",
            f"{self.events_seen} events scanned",
        ]
        if self.rejects:
            top = sorted(self.rejects.items(), key=lambda item: (-item[1], item[0]))[:4]
            parts.append("rejects: " + ", ".join(f"{k}={v}" for k, v in top))
        return "; ".join(parts)


@dataclass(frozen=True)
class EsportsDiscoveryResult:
    candidates: list[EsportsCandidate]
    stats: EsportsScanStats


_PROP_SLUG_MARKERS = (
    "-both-teams-",
    "-odd-even-",
    "-total-games-",
    "-game-handicap-",
    "-any-player-",
    "-baron-",
    "-dragon-",
    "-destroy-",
    "-penta-",
    "-quadra-",
    "-spread-",
    "-total-",
    "-o-u-",
    "player-props",
    "more-markets",
    "-handicap-",
    "-first-half-",
    "-1h-",
)

_GAME_WINNER_SLUG = re.compile(r"-game\d+$")
_SERIES_SLUG = re.compile(r"^[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")
_DATE_IN_SLUG = re.compile(r"-(\d{4}-\d{2}-\d{2})(?:-|$)")
_SPORTS_LEAGUE_PREFIX = re.compile(
    r"^(?:ucl|epl|mls|lal|bund|serie|lig|ered|nba|nfl|mlb|nhl|cbb|ncaa|"
    r"atp|wta|ufc|mma|cs2|lol|lck|lpl|lec|vct|valorant|dota2|cblol|lcs)"
)
_WIN_ON_DATE = re.compile(r"will .+ win on \d{4}-\d{2}-\d{2}", re.I)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_end(row: dict) -> datetime | None:
    for key in ("endDate", "endDateIso", "umaEndDate"):
        dt = _parse_iso(row.get(key))
        if dt is not None:
            return dt
    return None


def _resolve_end_at(row: dict, event: dict, *, now: datetime) -> datetime | None:
    """Use series end when per-game end times are stale (common on live LoL/Valorant)."""
    event_end = _parse_iso(event.get("endDate"))
    market_end = _row_end(row)
    game_start = _parse_iso(row.get("gameStartTime"))
    if game_start is not None and market_end is None:
        market_end = game_start + timedelta(hours=3)
    slug_end = _slug_event_date(str(row.get("slug") or ""))
    if slug_end is not None:
        slug_end = slug_end + timedelta(hours=4)
        if market_end is None or (slug_end >= now and slug_end < market_end):
            market_end = slug_end
    if event_end is None:
        return market_end
    if market_end is None:
        return event_end
    if market_end < now <= event_end:
        return event_end
    if market_end >= now and event_end >= now:
        return min(market_end, event_end)
    if event_end >= now:
        return event_end
    return market_end if market_end >= now else None


def _is_prop_market(slug: str, extra_patterns: tuple[str, ...]) -> bool:
    slug_l = slug.lower()
    for marker in _PROP_SLUG_MARKERS + extra_patterns:
        if marker in slug_l:
            return True
    return slug_l.endswith(("-spread", "-total", "-o-u"))


def _slug_event_date(slug: str) -> datetime | None:
    match = _DATE_IN_SLUG.search(slug.lower())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _looks_like_match_market(question: str, slug: str) -> bool:
    q = question.strip()
    slug_l = slug.lower()
    if re.match(r"^(LoL|CS2|Valorant|Dota 2|DOTA 2):", q, re.I):
        return True
    if re.search(r"\bvs\.?\b", q, re.I) and _GAME_WINNER_SLUG.search(slug):
        return True
    if _SERIES_SLUG.match(slug):
        return True
    if _WIN_ON_DATE.search(q):
        return True
    if "end in a draw" in q.lower() and _DATE_IN_SLUG.search(slug_l):
        return True
    if _SPORTS_LEAGUE_PREFIX.match(slug_l) and _DATE_IN_SLUG.search(slug_l):
        return True
    if re.search(r"\bvs\.?\b", q, re.I) and _DATE_IN_SLUG.search(slug_l):
        return True
    return False


def _fetch_events(engine: Engine, cfg, *, now: datetime, horizon: datetime) -> list[dict]:
    seen: set[str] = set()
    events: list[dict] = []

    def _add(batch: list[dict] | None) -> None:
        for event in batch or []:
            if not isinstance(event, dict):
                continue
            slug = str(event.get("slug") or "")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            events.append(event)

    for query in cfg.search_queries:
        try:
            data = engine.api._gamma_get(
                "/public-search",
                params={"q": query, "limit_per_type": cfg.search_limit},
            )
            _add(data.get("events"))
        except Exception:
            continue

    today = now.strftime("%Y-%m-%d")
    for query in (today, f"nba {today}", f"ucl {today}", f"epl {today}", f"nhl {today}"):
        try:
            data = engine.api._gamma_get(
                "/public-search",
                params={"q": query, "limit_per_type": min(cfg.search_limit, 25)},
            )
            _add(data.get("events"))
        except Exception:
            continue

    for tag in cfg.event_tags:
        try:
            rows = engine.api._gamma_get(
                "/events",
                params={
                    "tag_slug": tag,
                    "closed": "false",
                    "limit": cfg.search_limit,
                    "order": "endDate",
                    "ascending": "true",
                },
            )
            if isinstance(rows, list):
                _add(rows)
        except Exception:
            continue

    if cfg.tag_slug and cfg.tag_slug not in cfg.event_tags:
        try:
            rows = engine.api._gamma_get(
                "/events",
                params={
                    "tag_slug": cfg.tag_slug,
                    "closed": "false",
                    "limit": cfg.search_limit,
                    "order": "endDate",
                    "ascending": "true",
                },
            )
            if isinstance(rows, list):
                _add(rows)
        except Exception:
            pass

    return events


def discover_esports_markets(
    engine: Engine,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> EsportsDiscoveryResult:
    """Find esports match markets resolving within the configured horizon."""
    cfg = settings.esports
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=cfg.horizon_hours)
    stats = EsportsScanStats()
    seen_market_slugs: set[str] = set()
    candidates: list[EsportsCandidate] = []

    def _note_near_miss(
        *,
        slug: str,
        outcome: str,
        ask: float | None,
        ask_size: float,
        fail: str,
        end_at: datetime,
    ) -> None:
        if len(stats.notable) >= 8:
            return
        hours_left = (end_at - now).total_seconds() / 3600
        stats.notable.append(
            {
                "bucket": slug.rsplit("-", 1)[-1][:24],
                "slug": slug,
                "outcome": outcome,
                "ask": ask,
                "ask_size": ask_size,
                "fail": fail,
                "ends_in_hours": round(hours_left, 1),
            }
        )

    def _consider_event(event: dict) -> None:
        stats.events_seen += 1
        if event.get("closed"):
            stats.bump("event_closed")
            return
        event_slug = str(event.get("slug") or "")
        event_title = str(event.get("title") or event_slug)
        volume = float(event.get("volume") or event.get("volume24hr") or 0 or 0)
        event_end = _parse_iso(event.get("endDate"))
        slug_event_end = _slug_event_date(event_slug)
        if slug_event_end is not None:
            slug_event_end = slug_event_end + timedelta(hours=4)
        if event_end is None and slug_event_end is not None:
            event_end = slug_event_end
        elif (
            slug_event_end is not None
            and event_end is not None
            and slug_event_end >= now
            and slug_event_end < event_end
        ):
            event_end = slug_event_end
        if event_end is None:
            stats.bump("no_event_end")
            return
        if event_end < now or event_end > horizon:
            stats.bump("event_outside_horizon")
            return
        if volume < cfg.min_event_volume:
            stats.bump("low_event_volume")
            return
        stats.events_in_horizon += 1

        for row in event.get("markets") or []:
            if row.get("closed") or row.get("active") is False:
                stats.bump("market_closed")
                continue
            slug = str(row.get("slug") or "")
            if not slug or slug in seen_market_slugs:
                continue
            if _is_prop_market(slug, cfg.exclude_slug_patterns):
                stats.bump("prop_market")
                continue
            question = str(row.get("question") or "")
            if not _looks_like_match_market(question, slug):
                stats.bump("not_match_market")
                continue
            stats.match_markets += 1
            end_at = _resolve_end_at(row, event, now=now)
            if end_at is None or end_at < now or end_at > horizon:
                stats.bump("market_outside_horizon")
                continue
            market = _market_from_event_row(row)
            if market is None or market.closed:
                stats.bump("market_unavailable")
                continue
            seen_market_slugs.add(slug)

            cheapest_any: tuple[str, float, float] | None = None
            best_valid: tuple[str, float, float] | None = None
            for outcome in market.outcomes:
                try:
                    token = market.get_token_id(outcome)
                    book = engine.api.get_order_book(token)
                except Exception:
                    stats.bump("order_book_error")
                    continue
                if not book.asks:
                    stats.bump("no_ask")
                    continue
                ask_level = min(book.asks, key=lambda x: x.price)
                ask, ask_size = ask_level.price, ask_level.size
                if ask is None:
                    stats.bump("no_ask")
                    continue
                if cheapest_any is None or ask < cheapest_any[1]:
                    cheapest_any = (outcome, ask, ask_size)
                if ask_size < settings.min_best_ask_size:
                    continue
                reject = classify_ask_reject(
                    ask,
                    ask_size,
                    min_ask=cfg.min_ask,
                    max_ask=cfg.max_ask,
                    min_size=settings.min_best_ask_size,
                )
                if reject:
                    continue
                if best_valid is None or ask < best_valid[1]:
                    best_valid = (outcome, ask, ask_size)

            if best_valid is not None:
                outcome, ask, ask_size = best_valid
                stats.candidates += 1
                candidates.append(
                    EsportsCandidate(
                        event_slug=event_slug,
                        event_title=event_title,
                        market=market,
                        outcome=outcome,
                        end_at=end_at,
                        event_volume=volume,
                        ask=ask,
                        ask_size=ask_size,
                    )
                )
                continue

            if cheapest_any is None:
                stats.bump("no_ask")
                continue
            outcome, ask, ask_size = cheapest_any
            fail = classify_ask_reject(
                ask,
                ask_size,
                min_ask=cfg.min_ask,
                max_ask=cfg.max_ask,
                min_size=settings.min_best_ask_size,
            ) or "no_valid_ask"
            stats.bump(fail)
            _note_near_miss(
                slug=slug,
                outcome=outcome,
                ask=ask,
                ask_size=ask_size,
                fail=fail,
                end_at=end_at,
            )

    for event in _fetch_events(engine, cfg, now=now, horizon=horizon):
        _consider_event(event)

    candidates.sort(key=lambda c: (c.end_at, c.ask))
    return EsportsDiscoveryResult(candidates=candidates, stats=stats)
