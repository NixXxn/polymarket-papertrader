from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pm_trader.engine import Engine
from pm_trader.models import Market

from papertrader.config import EsportsSettings, Settings
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
)


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


def _market_end(row: dict, event: dict) -> datetime | None:
    for key in ("endDate", "endDateIso", "umaEndDate"):
        dt = _parse_iso(row.get(key))
        if dt is not None:
            return dt
    return _parse_iso(event.get("endDate"))


def _is_prop_market(slug: str, extra_patterns: tuple[str, ...]) -> bool:
    slug_l = slug.lower()
    for marker in _PROP_SLUG_MARKERS + extra_patterns:
        if marker in slug_l:
            return True
    return False


def _looks_like_match_market(question: str, slug: str) -> bool:
    q = question.strip()
    if re.match(r"^(LoL|CS2|Valorant|Dota 2|DOTA 2):", q, re.I):
        return True
    if re.search(r"\bvs\b", q, re.I) and re.search(r"-game\d+$", slug):
        return True
    if re.match(r"^[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$", slug):
        return True
    return False


def discover_esports_markets(
    engine: Engine,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[EsportsCandidate]:
    """Find esports match markets resolving within the configured horizon."""
    cfg = settings.esports
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=cfg.horizon_hours)
    seen_market_slugs: set[str] = set()
    candidates: list[EsportsCandidate] = []

    def _consider_event(event: dict) -> None:
        if event.get("closed"):
            return
        event_slug = str(event.get("slug") or "")
        event_title = str(event.get("title") or event_slug)
        volume = float(event.get("volume") or event.get("volume24hr") or 0 or 0)
        if volume < cfg.min_event_volume:
            return
        for row in event.get("markets") or []:
            if row.get("closed") or row.get("active") is False:
                continue
            slug = str(row.get("slug") or "")
            if not slug or slug in seen_market_slugs:
                continue
            if _is_prop_market(slug, cfg.exclude_slug_patterns):
                continue
            question = str(row.get("question") or "")
            if not _looks_like_match_market(question, slug):
                continue
            end_at = _market_end(row, event)
            if end_at is None or end_at < now or end_at > horizon:
                continue
            market = _market_from_event_row(row)
            if market is None or market.closed:
                continue
            seen_market_slugs.add(slug)
            best: tuple[str, float, float] | None = None
            for outcome in market.outcomes:
                try:
                    token = market.get_token_id(outcome)
                    book = engine.api.get_order_book(token)
                except Exception:
                    continue
                if not book.asks:
                    continue
                ask_level = min(book.asks, key=lambda x: x.price)
                ask, ask_size = ask_level.price, ask_level.size
                if ask is None or ask_size < settings.min_best_ask_size:
                    continue
                if not (cfg.min_ask <= ask <= cfg.max_ask):
                    continue
                if best is None or ask < best[1]:
                    best = (outcome, ask, ask_size)
            if best is None:
                continue
            outcome, ask, ask_size = best
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

    for query in cfg.search_queries:
        try:
            data = engine.api._gamma_get(
                "/public-search",
                params={"q": query, "limit_per_type": cfg.search_limit},
            )
        except Exception:
            continue
        for event in data.get("events") or []:
            _consider_event(event)

    try:
        rows = engine.api._gamma_get(
            "/events",
            params={
                "tag_slug": cfg.tag_slug,
                "closed": "false",
                "limit": cfg.search_limit,
                "order": "endDate",
                "ascending": "true",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": horizon.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        if isinstance(rows, list):
            for event in rows:
                if isinstance(event, dict):
                    _consider_event(event)
    except Exception:
        pass

    candidates.sort(key=lambda c: (c.end_at, c.ask))
    return candidates
