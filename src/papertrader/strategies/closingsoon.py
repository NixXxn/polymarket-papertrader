"""Closing Soon strategy: buy favorites resolving soon (high win-rate hold-to-$1)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pm_trader.engine import Engine

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.intel import evaluate_entry_gate, should_force_exit
from papertrader.signals import Signal

log = logging.getLogger("papertrader")


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


def _yes_no_prices(market: dict[str, Any]) -> tuple[float | None, float | None]:
    """Gamma list endpoints usually ship outcomePrices/outcomes, not tokens[]."""
    tokens = market.get("tokens") or []
    if tokens:
        yt = next((t for t in tokens if (t.get("outcome") or "").upper() == "YES"), {})
        nt = next((t for t in tokens if (t.get("outcome") or "").upper() == "NO"), {})
        try:
            yes_p = float(yt.get("price")) if yt.get("price") is not None else None
            no_p = float(nt.get("price")) if nt.get("price") is not None else None
            if yes_p is not None and no_p is not None:
                return yes_p, no_p
        except (TypeError, ValueError):
            pass

    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    prices_raw = _parse_json_list(market.get("outcomePrices"))
    if outcomes and prices_raw and len(outcomes) == len(prices_raw):
        mapped: dict[str, float] = {}
        for outcome, price in zip(outcomes, prices_raw):
            try:
                mapped[outcome.lower()] = float(price)
            except (TypeError, ValueError):
                continue
        if "yes" in mapped and "no" in mapped:
            return mapped["yes"], mapped["no"]

    best_ask = market.get("bestAsk")
    if best_ask is not None:
        try:
            yes_p = float(best_ask)
            return yes_p, max(0.0, 1.0 - yes_p)
        except (TypeError, ValueError):
            pass
    return None, None


def analyze_closingsoon(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 5,
    now: datetime | None = None,
) -> list[Signal]:
    """Scan markets resolving soon and buy high-priced favorites."""
    cfg = settings.closingsoon
    now = now or datetime.now(timezone.utc)

    try:
        data: list = []
        # Start at min_hours so page 0 is already inside the trading window
        # (ascending endDate otherwise fills with sub-1h markets).
        end_min = (now + timedelta(hours=cfg.min_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_max = (now + timedelta(hours=cfg.max_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for offset in (0, 100, 200, 300, 400, 500, 600, 700):
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
        log.warning("closingsoon: failed to fetch markets: %s", e)
        log_decision(
            engine.db.data_dir,
            strategy="closingsoon",
            decision="scan",
            reason=f"fetch_failed: {e}",
        )
        return []

    if not isinstance(data, list):
        log_decision(
            engine.db.data_dir,
            strategy="closingsoon",
            decision="scan",
            reason="empty_or_invalid_markets",
        )
        return []

    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions}
    if len(open_positions) >= cfg.max_open_positions:
        log_decision(
            engine.db.data_dir,
            strategy="closingsoon",
            decision="scan",
            reason="max_open_positions",
            open_positions=len(open_positions),
        )
        return []

    signals: list[Signal] = []
    scanned = 0
    seen_events: set[str] = set()
    rejects = {
        "no_end": 0,
        "outside_window": 0,
        "no_price": 0,
        "not_favorite": 0,
        "low_liquidity": 0,
        "already_open": 0,
        "tiny_size": 0,
        "event_dup": 0,
    }

    for m in data:
        try:
            scanned += 1
            end_date_str = m.get("endDate") or ""
            if not end_date_str:
                rejects["no_end"] += 1
                continue
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end_dt - now).total_seconds() / 3600
            if not (cfg.min_hours <= hours_left <= cfg.max_hours):
                rejects["outside_window"] += 1
                continue

            yes_p, no_p = _yes_no_prices(m)
            outcomes = [str(o) for o in _parse_json_list(m.get("outcomes"))]
            # High WR: only standard Yes/No binaries (skip over/under, exact score, multi-outcome).
            outcome_set = {o.lower() for o in outcomes}
            if outcome_set != {"yes", "no"}:
                rejects["not_favorite"] += 1
                continue
            if yes_p is None or no_p is None:
                rejects["no_price"] += 1
                continue

            slug = m.get("slug") or m.get("conditionId") or ""
            slug_l = slug.lower()
            question = (m.get("question") or "").lower()
            # Skip sports props / match markets — noisy for a high-WR paper book.
            sport_markers = (
                "halftime",
                "first-to-score",
                "exact-score",
                "second-half",
                "total-",
                "spread",
                "btts",
                "corners",
                "-draw",
                "moneyline",
            )
            sport_prefixes = (
                "uel-",
                "uzb",
                "col-",
                "mlb-",
                "nba-",
                "nfl-",
                "nhl-",
                "epl-",
                "lal-",
                "bun-",
                "serie-",
                "cbb-",
                "cfb-",
                "lol-",
                "cs2-",
                "dota-",
                "qat",
                "afc-",
                "spl-",
                "sud-",
                "bra-",
                "arg-",
                "mex-",
                "jpn-",
                "kor-",
                "aus-",
                "sea-",
                "lig-",
            )
            if any(x in slug_l for x in sport_markers) or slug_l.startswith(sport_prefixes):
                rejects["not_favorite"] += 1
                continue
            if " vs" in question or " vs." in question:
                rejects["not_favorite"] += 1
                continue
            if cfg.weather_only and "highest-temperature-in-" not in slug_l:
                rejects["not_favorite"] += 1
                continue
            crypto_markers = ("bitcoin", "btc-", "price-of-bitcoin", "ethereum", "eth-")
            if any(m in slug_l for m in crypto_markers):
                rejects["not_favorite"] += 1
                continue

            # Buy the favorite side only.
            if yes_p >= no_p and cfg.price_min <= yes_p <= cfg.price_max:
                side = next(o for o in outcomes if o.lower() == "yes")
                price = yes_p
            elif no_p > yes_p and cfg.price_min <= no_p <= cfg.price_max:
                side = next(o for o in outcomes if o.lower() == "no")
                price = no_p
            else:
                rejects["not_favorite"] += 1
                continue

            direction = abs(price - 0.5)
            if direction < cfg.min_direction:
                rejects["not_favorite"] += 1
                continue

            liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            if liq < cfg.min_liquidity:
                rejects["low_liquidity"] += 1
                continue

            if not slug or slug in open_slugs:
                rejects["already_open"] += 1
                continue

            # One market per event family to avoid correlated sports-prop stacks.
            event_key = "-".join(slug.split("-")[:4]) if "-" in slug else slug
            if event_key in seen_events:
                rejects["event_dup"] += 1
                continue

            cash = float(engine.get_account().cash)
            size = min(cfg.position_usd, cfg.max_position_usd, cash)
            if size < settings.min_position_usd:
                rejects["tiny_size"] += 1
                continue

            # Gamma mid can sit below the live CLOB ask; maker limits then never fill.
            # Near expiry we take the ask (FAK taker) when the book still looks like a favorite.
            try:
                market = engine.api.get_market(slug)
                token = market.get_token_id(side)
                book = engine.api.get_order_book(token)
                from papertrader.markets import best_ask

                ask, ask_size = best_ask(book)
            except Exception:
                rejects["no_price"] += 1
                continue
            if ask is None or ask_size <= 0:
                rejects["no_price"] += 1
                continue
            if not (cfg.price_min <= ask <= cfg.price_max):
                rejects["not_favorite"] += 1
                continue

            gate = evaluate_entry_gate(
                strategy="closingsoon",
                slug=slug,
                question=question,
                data_dir=engine.db.data_dir,
                cfg=settings.intel,
            )
            if not gate.allow:
                rejects["intel_block"] = rejects.get("intel_block", 0) + 1
                log_decision(
                    engine.db.data_dir,
                    strategy="closingsoon",
                    decision="skip",
                    reason=gate.reason,
                    slug=slug,
                    intel_score=gate.event.score,
                    intel_category=gate.event.category,
                    macro=gate.macro_verdict,
                )
                continue
            size = round(size * gate.size_mult, 2)
            if size < settings.min_position_usd:
                rejects["tiny_size"] += 1
                continue

            signals.append(
                Signal(
                    action="buy",
                    slug=slug,
                    outcome=side,
                    reason=(
                        f"closingsoon {hours_left:.0f}h favorite ask={ask:.2f} (taker) "
                        f"intel={gate.event.category}:{gate.event.score}"
                    ),
                    amount_usd=round(size, 2),
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=m.get("conditionId") or "",
                )
            )
            log_decision(
                engine.db.data_dir,
                strategy="closingsoon",
                decision="buy",
                reason=f"hours={hours_left:.0f} ask={ask:.3f} taker",
                slug=slug,
                action="buy",
                amount_usd=round(size, 2),
                ask=ask,
                intel_score=gate.event.score,
                intel_category=gate.event.category,
                macro=gate.macro_verdict,
                fear_greed=gate.fear_greed,
                intel_gate=gate.reason,
            )
            open_slugs.add(slug)
            seen_events.add(event_key)
            if len(signals) >= max_signals:
                break
        except Exception:
            continue

    log_decision(
        engine.db.data_dir,
        strategy="closingsoon",
        decision="scan",
        reason=(
            f"closingsoon scan: {len(signals)} signals / {scanned} markets "
            f"/ rejects={rejects}"
        ),
        candidates=len(signals),
        orders_placed=len(signals),
        rejects=rejects,
    )
    return signals


def closingsoon_exits(
    engine: Engine,
    settings: Settings,
) -> list[Signal]:
    """Hold favorites to resolution; exit only on true book collapse."""
    from papertrader.markets import best_bid

    cfg = settings.closingsoon
    positions = engine.db.get_open_positions()
    signals: list[Signal] = []

    for pos in positions:
        entry = pos.avg_entry_price
        if entry <= 0:
            continue
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            bid, _ = best_bid(book)
        except Exception:
            continue
        if bid is None:
            continue
        # Use best bid (max), not bids[0] which is often the worst/lowest level.
        catastrophic = max(0.12, entry * (1.0 - cfg.stop_loss_pct))
        force = should_force_exit(slug=pos.market_slug, cfg=settings.intel)
        if force and float(bid) >= 0.02:
            reason = f"{force} bid={float(bid):.3f}"
        elif float(bid) <= catastrophic:
            reason = f"closingsoon stop bid={float(bid):.3f}"
        else:
            continue
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                reason=reason,
            )
        )
        log_decision(
            engine.db.data_dir,
            strategy="closingsoon",
            decision="sell",
            reason=reason,
            slug=pos.market_slug,
            action="sell",
            shares=pos.shares,
            bid=float(bid),
        )
    return signals
