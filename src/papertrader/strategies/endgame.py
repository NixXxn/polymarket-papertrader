"""Endgame lock: buy near-certain sports/esports favorites in the last minutes.

Inspired by landighertz-style flow: when a match outcome is essentially decided
and the market is about to resolve, buy the favorite in the high-90¢ band with
full strategy cash, then immediately rest a take-profit sell limit.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.endgame_state import EndgameExitStore
from papertrader.markets import best_ask, best_bid
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
    "first-corner",
    "over-under",
    "map-",
    "kill",
)

_CRYPTO_UPDOWN = ("-updown-", "updown-5m", "updown-15m", "btc-updown", "eth-updown")


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


def _is_crypto_updown(slug: str, question: str = "") -> bool:
    blob = f"{slug} {question}".lower()
    return any(x in blob for x in _CRYPTO_UPDOWN)


def _is_sports_or_esports(market: dict[str, Any]) -> bool:
    slug = str(market.get("slug") or "").lower()
    question = str(market.get("question") or "").lower()
    if _is_crypto_updown(slug, question):
        return False

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

    if any(slug.startswith(h) or h in slug for h in _SPORT_SLUG_HINTS):
        return True
    if " vs" in question or " vs." in question or " vs " in question:
        return True
    if "winner" in question or "win on" in question or "game " in question:
        if any(
            h in slug or h in question
            for h in ("lol", "cs2", "nba", "nfl", "mlb", "nhl", "fc", "united", "roma", "madrid")
        ):
            return True
    return False


def _is_noisy_prop(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _PROP_MARKERS)


def _fetch_markets_ending(
    engine: Engine,
    *,
    now: datetime,
    max_minutes: float,
) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + timedelta(minutes=max_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for offset in (0, 100, 200, 300, 400, 500):
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
    return data


def analyze_endgame(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 1,
    now: datetime | None = None,
    paper_mode: bool = False,
) -> list[Signal]:
    """Scan sports/esports ending soon; buy favorites with a resting limit."""
    cfg = settings.endgame
    now = now or datetime.now(timezone.utc)
    look_ahead = max(float(cfg.look_ahead_minutes), float(cfg.max_minutes))

    try:
        data = _fetch_markets_ending(engine, now=now, max_minutes=look_ahead)
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
            look_ahead_minutes=look_ahead,
            trade_window_minutes=cfg.max_minutes,
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

    rejects = {
        "no_end": 0,
        "outside_look_ahead": 0,
        "crypto_updown": 0,
        "not_sports": 0,
        "prop": 0,
        "no_price": 0,
        "not_lock": 0,
        "outside_trade_window": 0,
        "low_liquidity": 0,
        "already_open": 0,
        "book": 0,
        "ask_out_of_band": 0,
        "tiny_size": 0,
    }
    sports_seen: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []
    tradeable: list[tuple[float, float, dict[str, Any], str, float]] = []

    for m in data:
        try:
            end_date_str = m.get("endDate") or ""
            if not end_date_str:
                rejects["no_end"] += 1
                continue
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            minutes_left = (end_dt - now).total_seconds() / 60.0
            if minutes_left < cfg.min_minutes or minutes_left > look_ahead:
                rejects["outside_look_ahead"] += 1
                continue

            slug = str(m.get("slug") or m.get("conditionId") or "")
            question = str(m.get("question") or "")
            if _is_crypto_updown(slug, question):
                rejects["crypto_updown"] += 1
                continue
            if not _is_sports_or_esports(m):
                rejects["not_sports"] += 1
                continue
            if _is_noisy_prop(slug, question):
                rejects["prop"] += 1
                near_misses.append(
                    {
                        "slug": slug,
                        "minutes_left": round(minutes_left, 1),
                        "fail": "prop",
                    }
                )
                continue
            if not slug or slug in open_slugs:
                rejects["already_open"] += 1
                continue

            priced = _outcome_prices(m)
            if not priced:
                rejects["no_price"] += 1
                near_misses.append(
                    {
                        "slug": slug,
                        "minutes_left": round(minutes_left, 1),
                        "fail": "no_price",
                    }
                )
                continue
            side, mid = max(priced, key=lambda row: row[1])
            sports_row = {
                "slug": slug,
                "minutes_left": round(minutes_left, 1),
                "side": side,
                "mid": round(mid, 3),
                "in_trade_window": bool(cfg.min_minutes <= minutes_left <= cfg.max_minutes),
                "in_price_band": bool(cfg.price_min <= mid <= cfg.price_max),
            }
            sports_seen.append(sports_row)

            if not (cfg.price_min <= mid <= cfg.price_max):
                rejects["not_lock"] += 1
                near_misses.append({**sports_row, "fail": "not_lock"})
                continue

            if not (cfg.min_minutes <= minutes_left <= cfg.max_minutes):
                rejects["outside_trade_window"] += 1
                near_misses.append({**sports_row, "fail": "outside_trade_window"})
                continue

            liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            if liq < cfg.min_liquidity:
                rejects["low_liquidity"] += 1
                near_misses.append({**sports_row, "fail": "low_liquidity", "liq": liq})
                continue

            tradeable.append((minutes_left, mid, m, side, liq))
        except Exception as e:
            near_misses.append({"slug": str(m.get("slug") or ""), "fail": f"parse:{e}"})
            continue

    tradeable.sort(key=lambda row: (row[0], -row[1], -row[4]))
    sports_seen.sort(key=lambda row: row["minutes_left"])

    signals: list[Signal] = []
    for minutes_left, mid, m, side, _liq in tradeable:
        if len(signals) >= max_signals:
            break
        slug = str(m.get("slug") or "")
        try:
            market = engine.api.get_market(slug)
            token = market.get_token_id(side)
            book = engine.api.get_order_book(token)
            ask, ask_size = best_ask(book)
            condition_id = getattr(market, "condition_id", None) or m.get("conditionId") or ""
        except Exception as e:
            rejects["book"] += 1
            near_misses.append(
                {
                    "slug": slug,
                    "minutes_left": round(minutes_left, 1),
                    "fail": f"book:{e}",
                }
            )
            continue
        if ask is None or ask_size <= 0:
            rejects["book"] += 1
            near_misses.append(
                {
                    "slug": slug,
                    "minutes_left": round(minutes_left, 1),
                    "fail": "no_ask",
                }
            )
            continue
        if not (cfg.price_min <= ask <= cfg.price_max):
            rejects["ask_out_of_band"] += 1
            near_misses.append(
                {
                    "slug": slug,
                    "minutes_left": round(minutes_left, 1),
                    "fail": "ask_out_of_band",
                    "ask": round(ask, 3),
                    "mid": round(mid, 3),
                }
            )
            continue
        if ask_size < cfg.min_ask_size:
            rejects["book"] += 1
            near_misses.append(
                {
                    "slug": slug,
                    "minutes_left": round(minutes_left, 1),
                    "fail": "thin_ask",
                    "ask_size": ask_size,
                }
            )
            continue

        if cfg.use_full_capital:
            size = cash
        else:
            size = min(cfg.position_usd, cfg.max_position_usd, cash)
        size = round(size, 2)
        if size < settings.min_position_usd:
            rejects["tiny_size"] += 1
            continue

        limit_px = min(float(ask), float(cfg.price_max))
        fill_now = bool(paper_mode and cfg.paper_fill_at_limit)
        reason = (
            f"endgame {minutes_left:.1f}m sports lock limit@{limit_px:.2f} "
            f"ask={ask:.2f} → TP@{cfg.sell_limit:.2f} full_cap={cfg.use_full_capital}"
        )
        signals.append(
            Signal(
                action="buy",
                slug=slug,
                outcome=side,
                reason=reason,
                amount_usd=size,
                order_type="limit",
                limit_price=limit_px,
                paper_fill_at_limit=fill_now,
                market_condition_id=str(condition_id),
            )
        )
        log_decision(
            engine.db.data_dir,
            strategy="endgame",
            decision="buy",
            reason=reason,
            slug=slug,
            action="buy",
            outcome=side,
            amount_usd=size,
            ask=ask,
            limit_price=limit_px,
            sell_limit=cfg.sell_limit,
            cash=cash,
            use_full_capital=cfg.use_full_capital,
            paper_fill_at_limit=fill_now,
        )
        open_slugs.add(slug)
        if cfg.use_full_capital:
            break

    no_trade_why = "ok"
    if not signals:
        if not sports_seen:
            no_trade_why = (
                f"no_sports_in_{look_ahead:.0f}m_look_ahead "
                f"(scanned={len(data)} crypto_updown={rejects['crypto_updown']} "
                f"not_sports={rejects['not_sports']})"
            )
        elif rejects["outside_trade_window"] and not any(
            s.get("in_trade_window") and s.get("in_price_band") for s in sports_seen
        ):
            soonest = sports_seen[0]
            no_trade_why = (
                f"sports_found_but_outside_{cfg.max_minutes:.0f}m_trade_window "
                f"(soonest={soonest['slug']} in {soonest['minutes_left']}m "
                f"mid={soonest['mid']})"
            )
        elif rejects["not_lock"]:
            locked_out = [s for s in sports_seen if s.get("in_trade_window")]
            sample = locked_out[0] if locked_out else sports_seen[0]
            no_trade_why = (
                f"sports_in_window_but_not_in_{cfg.price_min:.2f}-{cfg.price_max:.2f}_band "
                f"(sample={sample['slug']} mid={sample['mid']} min={sample['minutes_left']})"
            )
        else:
            no_trade_why = f"filtered_after_sports_match rejects={rejects}"

    log.info(
        "Endgame scan — %s sports / %s tradeable / %s signals / look_ahead=%.0fm "
        "trade_window=%.0fm | %s | rejects=%s",
        len(sports_seen),
        len(tradeable),
        len(signals),
        look_ahead,
        cfg.max_minutes,
        no_trade_why,
        rejects,
    )
    log_decision(
        engine.db.data_dir,
        strategy="endgame",
        decision="scan",
        reason=no_trade_why if not signals else "complete",
        scanned=len(data),
        sports_seen=len(sports_seen),
        tradeable=len(tradeable),
        candidates=len(tradeable),
        signals=len(signals),
        rejects=rejects,
        cash=cash,
        look_ahead_minutes=look_ahead,
        trade_window_minutes=cfg.max_minutes,
        price_min=cfg.price_min,
        price_max=cfg.price_max,
        sports_sample=sports_seen[:8],
        near_misses=near_misses[:12],
    )
    return signals


def endgame_exits(
    engine: Engine,
    settings: Settings,
    open_positions: list[Position] | None = None,
    *,
    exit_store: EndgameExitStore | None = None,
) -> list[Signal]:
    """Rest take-profit sell immediately after fill; FAK-stop if lock collapses."""
    cfg = settings.endgame
    positions = open_positions if open_positions is not None else engine.db.get_open_positions()
    store = exit_store or EndgameExitStore(engine.db.data_dir)
    store.prune_closed(positions)
    signals: list[Signal] = []

    for pos in positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue

        # Emergency stop if favorite collapses.
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            bid, _ = best_bid(book)
        except Exception:
            bid = None
        if bid is not None and bid < cfg.stop_bid:
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    reason=f"endgame stop bid={bid:.2f} < {cfg.stop_bid:.2f}",
                    shares=pos.shares,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
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
            continue

        if store.take_profit_placed(pos.market_condition_id, pos.outcome):
            continue

        tp = float(cfg.sell_limit)
        reason = (
            f"endgame TP limit @{tp:.2f} after entry@{pos.avg_entry_price:.3f} "
            f"({pos.shares:.1f} sh)"
        )
        log_decision(
            engine.db.data_dir,
            strategy="endgame",
            decision="sell",
            reason=reason,
            slug=pos.market_slug,
            outcome=pos.outcome,
            action="sell",
            shares=pos.shares,
            take_profit_price=tp,
            entry=pos.avg_entry_price,
        )
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                reason=reason,
                order_type="limit",
                limit_price=tp,
                market_condition_id=pos.market_condition_id,
                endgame_take_profit=True,
            )
        )
    return signals
