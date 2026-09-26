"""Forge: finance life with Hearth locks; fund breakouts only from surplus.

Barbell for personal capital:
  Hearth — near-expiry Yes/No favorites (high win rate, small edge) = rent money.
  Strike — cheap underdogs with confirmed momentum, sized only from equity
           ABOVE a ratcheting waterline = breakout sleeve you can afford to lose.

The waterline only rises. When underwater vs waterline, Strike is dark.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.forge_state import ForgeExitStore
from papertrader.markets import best_ask, best_bid
from papertrader.signals import QuantMeta, Signal
from papertrader.sizing import spendable_usd

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
    "ucl-",
    "mls-",
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
    "player-props",
    "more-markets",
    "o-u-",
    "over-under",
)

_CRYPTO_UPDOWN = ("updown", "up-or-down", "up or down", "-updown-")


class _PulseTracker:
    """Inter-poll price/volume velocity for Strike confirmation."""

    def __init__(self, maxlen: int = 24) -> None:
        self._prices: dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[float]] = {}
        self._maxlen = maxlen

    def update(self, cid: str, *, price: float, volume: float) -> None:
        if cid not in self._prices:
            self._prices[cid] = deque(maxlen=self._maxlen)
            self._volumes[cid] = deque(maxlen=self._maxlen)
        self._prices[cid].append(price)
        self._volumes[cid].append(volume)

    def upward_pulse(self, cid: str, *, min_move: float, min_score: float) -> float | None:
        prices = self._prices.get(cid)
        volumes = self._volumes.get(cid)
        if not prices or len(prices) < 3:
            return None
        last, prev = prices[-1], prices[-2]
        move = last - prev
        if move < min_move:
            return None
        moves = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        baseline = (sum(moves[:-1]) / max(1, len(moves) - 1)) if len(moves) > 1 else move
        baseline = max(baseline, 0.005)
        score = move / baseline
        if volumes and len(volumes) >= 3:
            vol_now = volumes[-1]
            vol_avg = sum(list(volumes)[:-1]) / max(1, len(volumes) - 1)
            if vol_avg > 0 and vol_now > vol_avg:
                score *= min(2.5, vol_now / vol_avg)
        if score < min_score:
            return None
        return score


_pulse = _PulseTracker()


def _parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _outcome_prices(market: dict[str, Any]) -> list[tuple[str, float]]:
    labels = [str(o).strip() for o in _parse_json_list(market.get("outcomes"))]
    prices_raw = _parse_json_list(market.get("outcomePrices"))
    out: list[tuple[str, float]] = []
    for i, label in enumerate(labels):
        try:
            px = float(prices_raw[i]) if i < len(prices_raw) else None
        except (TypeError, ValueError, IndexError):
            px = None
        if px is None:
            continue
        out.append((label, px))
    return out


def _is_yes_no(market: dict[str, Any]) -> bool:
    labels = [str(o).strip().lower() for o in _parse_json_list(market.get("outcomes"))]
    return len(labels) == 2 and set(labels) == {"yes", "no"}


def _is_sports(market: dict[str, Any]) -> bool:
    slug = str(market.get("slug") or "").lower()
    if any(h in slug for h in _SPORT_SLUG_HINTS):
        return True
    tags = market.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            label = ""
            if isinstance(t, dict):
                label = str(t.get("label") or t.get("slug") or "").lower()
            else:
                label = str(t).lower()
            if any(h in label for h in _SPORT_TAG_HINTS):
                return True
    return False


def _is_noisy_prop(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _PROP_MARKERS)


def _is_crypto_updown(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _CRYPTO_UPDOWN)


def _fetch_ending(
    engine: Engine,
    *,
    now: datetime,
    max_minutes: float,
) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + timedelta(minutes=max_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for offset in (0, 100, 200, 300, 400):
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


def _fetch_active_binary(engine: Engine, *, limit_pages: int = 4) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for offset in range(0, limit_pages * 100, 100):
        page = engine.api._gamma_get(
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        if not isinstance(page, list) or not page:
            break
        data.extend(page)
        if len(page) < 100:
            break
    return data


def _equity(engine: Engine) -> float:
    cash = float(engine.get_account().cash)
    mtm = 0.0
    for pos in engine.db.get_open_positions():
        if pos.shares <= 0 or pos.is_resolved:
            continue
        # Conservative mark: entry until we have a live bid.
        mark = float(pos.avg_entry_price or 0.0)
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            bid, _ = best_bid(book)
            if bid is not None:
                mark = float(bid)
        except Exception:
            pass
        mtm += float(pos.shares) * mark
    return cash + mtm


def analyze_forge(
    engine: Engine,
    settings: Settings,
    *,
    now: datetime | None = None,
    paper_mode: bool = False,
    exit_store: ForgeExitStore | None = None,
) -> list[Signal]:
    """Emit Hearth locks and (when surplus exists) Strike breakouts."""
    cfg = settings.forge
    now = now or datetime.now(timezone.utc)
    store = exit_store or ForgeExitStore(engine.db.data_dir)
    start_bal = float(cfg.starting_balance or settings.starting_balance)

    open_positions = engine.db.get_open_positions()
    store.prune_closed(open_positions)
    open_slugs = {p.market_slug for p in open_positions if p.shares > 0 and not p.is_resolved}
    hearth_open = sum(
        1
        for p in open_positions
        if p.shares > 0
        and not p.is_resolved
        and store.sleeve_of(p.market_condition_id, p.outcome) == "hearth"
    )
    strike_open = sum(
        1
        for p in open_positions
        if p.shares > 0
        and not p.is_resolved
        and store.sleeve_of(p.market_condition_id, p.outcome) == "strike"
    )

    cash = float(engine.get_account().cash)
    equity = _equity(engine)
    waterline = store.ratchet(equity, start_bal, lock_fraction=cfg.waterline_lock_fraction)
    excess = max(0.0, equity - waterline)
    spendable = spendable_usd(cash)

    log_decision(
        engine.db.data_dir,
        strategy="forge",
        decision="scan",
        reason=(
            f"forge equity=${equity:.2f} waterline=${waterline:.2f} "
            f"excess=${excess:.2f} hearth={hearth_open}/{cfg.hearth_max_open} "
            f"strike={strike_open}/{cfg.strike_max_open}"
        ),
        equity=round(equity, 2),
        waterline=round(waterline, 2),
        excess=round(excess, 2),
        cash=round(cash, 2),
        hearth_open=hearth_open,
        strike_open=strike_open,
    )

    signals: list[Signal] = []

    # --- Hearth: near-expiry sports Yes/No favorites ---
    if hearth_open < cfg.hearth_max_open and spendable >= settings.min_position_usd:
        try:
            ending = _fetch_ending(
                engine,
                now=now,
                max_minutes=max(cfg.hearth_max_minutes, cfg.hearth_look_ahead_minutes),
            )
        except Exception as e:
            log.warning("forge hearth fetch: %s", e)
            ending = []
            log_decision(
                engine.db.data_dir,
                strategy="forge",
                decision="skip",
                reason=f"hearth_fetch_failed: {e}",
            )

        hearth_candidates: list[tuple[float, float, dict[str, Any], str, float]] = []
        for m in ending:
            try:
                end_s = m.get("endDate") or ""
                if not end_s:
                    continue
                end_dt = datetime.fromisoformat(str(end_s).replace("Z", "+00:00"))
                minutes_left = (end_dt - now).total_seconds() / 60.0
                if not (cfg.hearth_min_minutes <= minutes_left <= cfg.hearth_max_minutes):
                    continue
                slug = str(m.get("slug") or "")
                question = str(m.get("question") or "")
                if slug in open_slugs:
                    continue
                if _is_crypto_updown(slug, question) or _is_noisy_prop(slug, question):
                    continue
                if not _is_sports(m) or not _is_yes_no(m):
                    continue
                priced = _outcome_prices(m)
                if not priced:
                    continue
                side, mid = max(priced, key=lambda row: row[1])
                if side.strip().lower() not in {"yes", "no"}:
                    continue
                if not (cfg.hearth_price_min <= mid <= cfg.hearth_price_max):
                    continue
                liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
                if liq < cfg.hearth_min_liquidity:
                    continue
                hearth_candidates.append((minutes_left, mid, m, side, liq))
            except Exception:
                continue

        hearth_candidates.sort(key=lambda row: (row[0], -row[1], -row[4]))
        for minutes_left, mid, m, side, _liq in hearth_candidates:
            if len([s for s in signals if "hearth" in s.reason]) >= 1:
                break
            if hearth_open + len([s for s in signals if "hearth" in s.reason]) >= cfg.hearth_max_open:
                break
            slug = str(m.get("slug") or "")
            try:
                market = engine.api.get_market(slug)
                token = market.get_token_id(side)
                book = engine.api.get_order_book(token)
                ask, ask_size = best_ask(book)
                condition_id = getattr(market, "condition_id", None) or m.get("conditionId") or ""
            except Exception:
                continue
            if ask is None or ask_size < cfg.hearth_min_ask_size:
                continue
            if ask >= cfg.hearth_sell_limit - 1e-12 or not (
                cfg.hearth_price_min <= ask <= cfg.hearth_price_max
            ):
                continue
            depth_usd = float(ask) * float(ask_size) * cfg.book_fill_fraction
            size = min(cfg.hearth_position_usd, cfg.hearth_max_position_usd, spendable, depth_usd)
            size = round(size, 2)
            if size < settings.min_position_usd:
                continue
            limit_px = min(float(ask), float(cfg.hearth_price_max))
            tp_px = min(
                float(cfg.hearth_sell_limit),
                float(limit_px) + float(cfg.hearth_take_profit_offset),
            )
            fill_now = bool(paper_mode and cfg.paper_fill_at_limit)
            reason = (
                f"forge hearth {minutes_left:.1f}m {side}@{limit_px:.2f} "
                f"→ TP@{tp_px:.2f} mid={mid:.2f}"
            )
            store.mark_entry(
                str(condition_id), side, market_slug=slug, sleeve="hearth"
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
                    quant=QuantMeta(
                        p=mid, sigma=0.0, f_star=mid, kelly_fraction=0.0, source="forge_hearth"
                    ),
                )
            )
            log_decision(
                engine.db.data_dir,
                strategy="forge",
                decision="buy",
                reason=reason,
                slug=slug,
                sleeve="hearth",
                amount_usd=size,
                ask=ask,
                limit_price=limit_px,
            )
            open_slugs.add(slug)
            spendable = spendable_usd(cash - size)

    # --- Strike: surplus-only momentum underdogs ---
    strike_budget = excess * cfg.strike_excess_fraction
    if (
        cfg.strike_enabled
        and excess >= cfg.strike_min_excess
        and strike_open < cfg.strike_max_open
        and spendable >= settings.min_position_usd
        and strike_budget >= settings.min_position_usd
    ):
        try:
            active = _fetch_active_binary(engine)
        except Exception as e:
            log.warning("forge strike fetch: %s", e)
            active = []

        strike_hits: list[tuple[float, dict[str, Any], str, float, float]] = []
        for m in active:
            try:
                slug = str(m.get("slug") or "")
                question = str(m.get("question") or "")
                if slug in open_slugs or not _is_yes_no(m):
                    continue
                if _is_noisy_prop(slug, question):
                    continue
                cid = str(m.get("conditionId") or slug)
                vol = float(m.get("volume24hr") or m.get("volumeNum") or 0)
                liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
                if vol < cfg.strike_min_volume or liq < cfg.strike_min_liquidity:
                    continue
                priced = _outcome_prices(m)
                if not priced:
                    continue
                # Prefer the cheaper side that is already moving up (story breaking).
                for side, mid in priced:
                    if not (cfg.strike_price_min <= mid <= cfg.strike_price_max):
                        continue
                    _pulse.update(f"{cid}:{side.lower()}", price=mid, volume=vol)
                    score = _pulse.upward_pulse(
                        f"{cid}:{side.lower()}",
                        min_move=cfg.strike_min_move,
                        min_score=cfg.strike_min_pulse,
                    )
                    if score is None:
                        continue
                    strike_hits.append((score, m, side, mid, vol))
            except Exception:
                continue

        strike_hits.sort(key=lambda row: -row[0])
        for score, m, side, mid, _vol in strike_hits:
            if len([s for s in signals if "strike" in s.reason]) >= 1:
                break
            if strike_open + len([s for s in signals if "strike" in s.reason]) >= cfg.strike_max_open:
                break
            slug = str(m.get("slug") or "")
            if slug in open_slugs:
                continue
            try:
                market = engine.api.get_market(slug)
                token = market.get_token_id(side)
                book = engine.api.get_order_book(token)
                ask, ask_size = best_ask(book)
                condition_id = getattr(market, "condition_id", None) or m.get("conditionId") or ""
            except Exception:
                continue
            if ask is None or ask_size < cfg.strike_min_ask_size:
                continue
            if not (cfg.strike_price_min <= ask <= cfg.strike_price_max):
                continue
            depth_usd = float(ask) * float(ask_size) * cfg.book_fill_fraction
            raw = min(
                strike_budget * cfg.strike_kelly,
                cfg.strike_max_position_usd,
                spendable,
                depth_usd,
            )
            size = round(raw, 2)
            if size < settings.min_position_usd:
                continue
            limit_px = float(ask)
            fill_now = bool(paper_mode and cfg.paper_fill_at_limit)
            reason = (
                f"forge strike {side}@{limit_px:.2f} pulse={score:.1f} "
                f"excess=${excess:.0f} size=${size:.2f}"
            )
            store.mark_entry(
                str(condition_id), side, market_slug=slug, sleeve="strike"
            )
            signals.append(
                Signal(
                    action="buy",
                    slug=slug,
                    outcome=side,
                    reason=reason,
                    amount_usd=size,
                    order_type="fak",
                    limit_price=limit_px,
                    paper_fill_at_limit=fill_now,
                    market_condition_id=str(condition_id),
                    quant=QuantMeta(
                        p=mid,
                        sigma=score,
                        f_star=cfg.strike_kelly,
                        kelly_fraction=cfg.strike_kelly,
                        source="forge_strike",
                    ),
                )
            )
            log_decision(
                engine.db.data_dir,
                strategy="forge",
                decision="buy",
                reason=reason,
                slug=slug,
                sleeve="strike",
                amount_usd=size,
                ask=ask,
                pulse=score,
                excess=excess,
            )
            open_slugs.add(slug)
            break

    if not signals:
        log_decision(
            engine.db.data_dir,
            strategy="forge",
            decision="skip",
            reason="no_forge_window",
            excess=round(excess, 2),
            waterline=round(waterline, 2),
        )
    return signals


def forge_exits(
    engine: Engine,
    settings: Settings,
    open_positions: list[Position] | None = None,
    *,
    exit_store: ForgeExitStore | None = None,
) -> list[Signal]:
    """Hearth: soft TP + stop. Strike: multiple TP / hard stop on fade."""
    cfg = settings.forge
    positions = open_positions if open_positions is not None else engine.db.get_open_positions()
    store = exit_store or ForgeExitStore(engine.db.data_dir)
    store.prune_closed(positions)
    signals: list[Signal] = []

    for pos in positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue
        sleeve = store.sleeve_of(pos.market_condition_id, pos.outcome)
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            bid, _ = best_bid(book)
        except Exception:
            bid = None

        entry = float(pos.avg_entry_price or 0.0)

        if sleeve == "strike":
            if bid is not None and entry > 0 and bid <= entry * cfg.strike_stop_mult:
                signals.append(
                    Signal(
                        action="sell",
                        slug=pos.market_slug,
                        outcome=pos.outcome,
                        reason=(
                            f"forge strike stop bid={bid:.3f} "
                            f"<= {cfg.strike_stop_mult:.2f}× entry={entry:.3f}"
                        ),
                        shares=pos.shares,
                        order_type="fak",
                        limit_price=None,
                        market_condition_id=pos.market_condition_id,
                    )
                )
                continue
            if bid is not None and (
                bid >= cfg.strike_take_profit_bid
                or (entry > 0 and bid >= entry * cfg.strike_take_profit_mult)
            ):
                signals.append(
                    Signal(
                        action="sell",
                        slug=pos.market_slug,
                        outcome=pos.outcome,
                        reason=(
                            f"forge strike TP bid={bid:.3f} "
                            f"(cap {cfg.strike_take_profit_bid:.2f} / "
                            f"{cfg.strike_take_profit_mult:.1f}×)"
                        ),
                        shares=pos.shares,
                        order_type="fak",
                        limit_price=bid,
                        market_condition_id=pos.market_condition_id,
                    )
                )
                continue
            continue

        # Hearth exits
        stop = cfg.hearth_stop_bid
        if bid is not None and bid < stop:
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    reason=f"forge hearth stop bid={bid:.2f} < {stop:.2f}",
                    shares=pos.shares,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                )
            )
            continue
        if store.take_profit_placed(pos.market_condition_id, pos.outcome):
            continue
        tp = min(
            float(cfg.hearth_sell_limit),
            entry + float(cfg.hearth_take_profit_offset),
        )
        if tp <= entry + 1e-12:
            continue
        reason = (
            f"forge hearth TP @{tp:.2f} after entry@{entry:.3f} "
            f"(+{cfg.hearth_take_profit_offset:.2f})"
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
                forge_take_profit=True,
                market_condition_id=pos.market_condition_id,
            )
        )
    return signals
