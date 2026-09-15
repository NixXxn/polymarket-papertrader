"""Endgame lock: buy near-certain sports/esports favorites in the last minutes.

Inspired by landighertz-style flow: when a match outcome is essentially decided
and the market is about to resolve, buy the favorite in the high-90¢ band and
size with the full strategy cash (hold to $1 resolution).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pm_trader.engine import Engine

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.markets import best_ask
from papertrader.signals import Signal

log = logging.getLogger("papertrader")

_SPORT_TAG_HINTS = (
    "sport",
    "sports",
    "esport",
    "esports",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "soccer",
    "football",
    "basketball",
    "tennis",
    "lol",
    "cs2",
    "dota",
    "valorant",
)

_SPORT_SLUG_HINTS = (
    "lol-",
    "cs2-",
    "dota-",
    "val-",
    "valorant",
    "mlb-",
    "nba-",
    "nfl-",
    "nhl-",
    "epl-",
    "lal-",
    "bun-",
    "serie-",
    "ucl-",
    "uel-",
    "mls-",
    "atp-",
    "wta-",
    "ufc-",
    "mma-",
    "cbb-",
    "cfb-",
    "wnba-",
)

_PROP_MARKERS = (
    "halftime",
    "first-to-score",
    "exact-score",
    "second-half",
    "total-",
    "spread",
    "btts",
    "corners",
    "over-under",
    "map-",
    "kill",
)


def _parse_json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _outcome_prices(market: dict[str, Any]) -> list[tuple[str, float]]:
    """Return (outcome_label, price) pairs for any binary or multi-outcome market."""
    tokens = market.get("tokens") or []
    out: list[tuple[str, float]] = []
    if tokens:
        for t in tokens:
            label = str(t.get("outcome") or "").strip()
            if not label:
                continue
            try:
                out.append((label, float(t["price"])))
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            return out

    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    prices_raw = _parse_json_list(market.get("outcomePrices"))
    if outcomes and prices_raw and len(outcomes) == len(prices_raw):
        for outcome, price in zip(outcomes, prices_raw):
            try:
                out.append((outcome, float(price)))
            except (TypeError, ValueError):
                continue
    return out


def _is_sports_or_esports(market: dict[str, Any]) -> bool:
    tags = _parse_json_list(market.get("tags"))
    tag_text = " ".join(
        str(t.get("label") if isinstance(t, dict) else t).lower() for t in tags
    )
    if any(h in tag_text for h in _SPORT_TAG_HINTS):
        return True

    events = market.get("events") or []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for t in _parse_json_list(ev.get("tags")):
            label = str(t.get("label") if isinstance(t, dict) else t).lower()
            if any(h in label for h in _SPORT_TAG_HINTS):
                return True
        cat = str(ev.get("category") or "").lower()
        if any(h in cat for h in _SPORT_TAG_HINTS):
            return True

    slug = str(market.get("slug") or "").lower()
    question = str(market.get("question") or "").lower()
    if any(slug.startswith(h) or h in slug for h in _SPORT_SLUG_HINTS):
        return True
    if " vs" in question or " vs." in question or " vs " in question:
        return True
    if "winner" in question or "win on" in question or "game " in question:
        # Common sports/esports phrasing; still require a sport-ish cue.
        if any(h in slug or h in question for h in ("lol", "cs2", "nba", "nfl", "mlb", "nhl", "fc", "united", "roma", "madrid")):
            return True
    return False


def _is_noisy_prop(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _PROP_MARKERS)


def analyze_endgame(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 1,
    now: datetime | None = None,
) -> list[Signal]:
    """Scan sports/esports ending soon; buy favorites in the configured ¢ band."""
    cfg = settings.endgame
    now = now or datetime.now(timezone.utc)

    try:
        data: list = []
        end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_max = (now + timedelta(minutes=cfg.max_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for offset in (0, 100, 200, 300):
            page = engine.api._gamma_get(
                "/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "true",
                    "end_date_min": end_min,
                    "end_date_max": end_max,
                },
            )
            if not isinstance(page, list) or not page:
                break
            data.extend(page)
            if len(page) < 100:
                break
    except Exception as e:
        log.warning("endgame: failed to fetch markets: %s", e)
        log_decision(
            engine.db.data_dir,
            strategy="endgame",
            decision="scan",
            reason=f"fetch_failed: {e}",
        )
        return []

    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions if p.shares > 0 and not p.is_resolved}
    if len(open_slugs) >= cfg.max_open_positions:
        log_decision(
            engine.db.data_dir,
            strategy="endgame",
            decision="scan",
            reason="max_open_positions",
            open_positions=len(open_slugs),
        )
        return []

    cash = float(engine.get_account().cash)
    if cash < settings.min_position_usd:
        log_decision(
            engine.db.data_dir,
            strategy="endgame",
            decision="scan",
            reason="insufficient_cash",
            cash=cash,
        )
        return []

    candidates: list[tuple[float, float, dict[str, Any], str, float]] = []
    rejects = {
        "no_end": 0,
        "outside_window": 0,
        "not_sports": 0,
        "prop": 0,
        "no_price": 0,
        "not_lock": 0,
        "low_liquidity": 0,
        "already_open": 0,
        "book": 0,
    }

    for m in data:
        try:
            end_date_str = m.get("endDate") or ""
            if not end_date_str:
                rejects["no_end"] += 1
                continue
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            minutes_left = (end_dt - now).total_seconds() / 60.0
            if not (cfg.min_minutes <= minutes_left <= cfg.max_minutes):
                rejects["outside_window"] += 1
                continue

            slug = str(m.get("slug") or m.get("conditionId") or "")
            question = str(m.get("question") or "")
            if not _is_sports_or_esports(m):
                rejects["not_sports"] += 1
                continue
            if _is_noisy_prop(slug, question):
                rejects["prop"] += 1
                continue
            if not slug or slug in open_slugs:
                rejects["already_open"] += 1
                continue

            priced = _outcome_prices(m)
            if not priced:
                rejects["no_price"] += 1
                continue
            side, mid = max(priced, key=lambda row: row[1])
            if not (cfg.price_min <= mid <= cfg.price_max):
                rejects["not_lock"] += 1
                continue

            liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            if liq < cfg.min_liquidity:
                rejects["low_liquidity"] += 1
                continue

            candidates.append((minutes_left, mid, m, side, liq))
        except Exception:
            continue

    # Prefer soonest expiry, then highest mid (most locked).
    candidates.sort(key=lambda row: (row[0], -row[1], -row[4]))

    signals: list[Signal] = []
    for minutes_left, mid, m, side, _liq in candidates:
        if len(signals) >= max_signals:
            break
        slug = str(m.get("slug") or "")
        try:
            market = engine.api.get_market(slug)
            token = market.get_token_id(side)
            book = engine.api.get_order_book(token)
            ask, ask_size = best_ask(book)
        except Exception:
            rejects["book"] += 1
            continue
        if ask is None or ask_size <= 0:
            rejects["book"] += 1
            continue
        if not (cfg.price_min <= ask <= cfg.price_max):
            rejects["not_lock"] += 1
            continue
        if ask_size < cfg.min_ask_size:
            rejects["book"] += 1
            continue

        if cfg.use_full_capital:
            size = cash
        else:
            size = min(cfg.position_usd, cfg.max_position_usd, cash)
        # Cap by visible ask depth so we don't post a huge unfilled FAK.
        depth_usd = ask * ask_size
        size = min(size, depth_usd)
        size = round(size, 2)
        if size < settings.min_position_usd:
            continue

        signals.append(
            Signal(
                action="buy",
                slug=slug,
                outcome=side,
                reason=(
                    f"endgame {minutes_left:.1f}m sports lock ask={ask:.2f} "
                    f"full_cap={cfg.use_full_capital}"
                ),
                amount_usd=size,
                order_type="fak",
                limit_price=None,
                market_condition_id=m.get("conditionId") or "",
            )
        )
        log_decision(
            engine.db.data_dir,
            strategy="endgame",
            decision="buy",
            reason=f"minutes={minutes_left:.1f} ask={ask:.3f} mid={mid:.3f}",
            slug=slug,
            action="buy",
            outcome=side,
            amount_usd=size,
            ask=ask,
            cash=cash,
            use_full_capital=cfg.use_full_capital,
        )
        open_slugs.add(slug)
        # Full-capital mode: one shot consumes the book for this scan.
        if cfg.use_full_capital:
            break

    log_decision(
        engine.db.data_dir,
        strategy="endgame",
        decision="scan",
        reason="complete",
        scanned=len(data),
        candidates=len(candidates),
        signals=len(signals),
        rejects=rejects,
        cash=cash,
    )
    return signals


def endgame_exits(engine: Engine, settings: Settings) -> list[Signal]:
    """Hold to resolution — only emergency-exit if the lock collapses."""
    cfg = settings.endgame
    signals: list[Signal] = []
    for pos in engine.db.get_open_positions():
        if pos.shares <= 0 or pos.is_resolved:
            continue
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            from papertrader.markets import best_bid

            bid, _ = best_bid(book)
        except Exception:
            continue
        if bid is None:
            continue
        # If favorite collapses below stop, dump remaining size.
        if bid < cfg.stop_bid:
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    reason=f"endgame stop bid={bid:.2f} < {cfg.stop_bid:.2f}",
                    shares=pos.shares,
                    order_type="fak",
                    limit_price=None,
                )
            )
            log_decision(
                engine.db.data_dir,
                strategy="endgame",
                decision="sell",
                reason="stop_bid",
                slug=pos.market_slug,
                bid=bid,
                shares=pos.shares,
            )
    return signals
