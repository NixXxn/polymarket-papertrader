"""Mean Reversion strategy: fade price deviations from rolling average."""
from __future__ import annotations

import json
import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from pm_trader.engine import Engine

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.markets import best_bid
from papertrader.signals import Signal

log = logging.getLogger("papertrader")

# Paper losses clustered on crypto bursts and match props — skip for meanrev/volspike.
_NOISY_SLUG_MARKERS = (
    "bitcoin",
    "btc-",
    "ethereum",
    "eth-",
    "solana",
    "doge",
    "xrp-",
    "epl-",
    "mlb-",
    "nba-",
    "nfl-",
    "nhl-",
    "lol-",
    "cs2-",
    "mex-",
    "spl-",
    "qat",
    "sud-",
    "uel-",
    "fed-increase",
    "fed-decrease",
    "interest-rates",
    "nato-",
    "clarity-act",
    "signed-into-law",
    "military-clash",
)


def _is_noisy_general_market(slug: str, question: str = "") -> bool:
    slug_l = slug.lower()
    q_l = question.lower()
    if any(m in slug_l for m in _NOISY_SLUG_MARKERS):
        return True
    if " vs" in q_l or " vs." in q_l:
        return True
    return False


@dataclass
class _MarketSnapshot:
    condition_id: str
    slug: str
    question: str
    yes_price: float
    no_price: float
    liquidity: float
    volume_24h: float
    yes_outcome: str
    no_outcome: str


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


def _yes_no_snapshot(
    market: dict[str, Any],
) -> tuple[float, float, str, str] | None:
    """Return yes/no prices and canonical outcome labels for binary markets only."""
    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    outcome_set = {o.lower() for o in outcomes}
    if outcome_set != {"yes", "no"}:
        return None
    yes_label = next(o for o in outcomes if o.lower() == "yes")
    no_label = next(o for o in outcomes if o.lower() == "no")

    tokens = market.get("tokens") or []
    if tokens:
        yt = next((t for t in tokens if (t.get("outcome") or "").upper() == "YES"), {})
        nt = next((t for t in tokens if (t.get("outcome") or "").upper() == "NO"), {})
        try:
            if yt.get("price") is not None and nt.get("price") is not None:
                return float(yt["price"]), float(nt["price"]), yes_label, no_label
        except (TypeError, ValueError):
            pass

    prices_raw = _parse_json_list(market.get("outcomePrices"))
    if outcomes and prices_raw and len(outcomes) == len(prices_raw):
        mapped: dict[str, float] = {}
        for outcome, price in zip(outcomes, prices_raw):
            try:
                mapped[outcome.lower()] = float(price)
            except (TypeError, ValueError):
                continue
        if "yes" in mapped and "no" in mapped:
            return mapped["yes"], mapped["no"], yes_label, no_label

    best_ask = market.get("bestAsk")
    if best_ask is not None:
        try:
            yes_p = float(best_ask)
            return yes_p, max(0.0, 1.0 - yes_p), yes_label, no_label
        except (TypeError, ValueError):
            pass
    return None


class MeanReversionEngine:
    """Tracks price history per market and generates mean-reversion signals."""

    def __init__(self, window: int = 168):
        self._history: dict[str, deque[float]] = {}
        self._window = window

    def configure(self, window: int) -> None:
        if window == self._window:
            return
        self._window = max(10, window)
        for cid, hist in list(self._history.items()):
            self._history[cid] = deque(hist, maxlen=self._window)

    def update(self, cid: str, price: float) -> None:
        if cid not in self._history:
            self._history[cid] = deque(maxlen=self._window)
        self._history[cid].append(price)

    def z_score(self, cid: str, price: float, *, min_samples: int = 6) -> float | None:
        hist = self._history.get(cid)
        if not hist or len(hist) < min_samples:
            return None
        arr = list(hist)
        mu = sum(arr) / len(arr)
        variance = sum((x - mu) ** 2 for x in arr) / len(arr)
        sigma = math.sqrt(variance)
        if sigma < 0.004:
            return None
        return (price - mu) / sigma

    def mean(self, cid: str, *, min_samples: int = 6) -> float | None:
        hist = self._history.get(cid)
        if not hist or len(hist) < min_samples:
            return None
        return sum(hist) / len(hist)


_engine = MeanReversionEngine()


def discover_general_markets(
    engine: Engine,
    settings: Settings,
    *,
    min_liquidity: float | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    limit: int = 200,
) -> list[_MarketSnapshot]:
    """Fetch active Yes/No markets from Gamma (volume-ranked)."""
    cfg = settings.meanrev
    min_liq = float(min_liquidity if min_liquidity is not None else cfg.min_liquidity)
    pmin = float(price_min if price_min is not None else cfg.price_min)
    pmax = float(price_max if price_max is not None else cfg.price_max)
    try:
        data = engine.api._gamma_get(
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    except Exception as e:
        log.warning("meanrev: failed to fetch markets: %s", e)
        return []
    if not isinstance(data, list):
        return []
    out: list[_MarketSnapshot] = []
    for m in data:
        try:
            liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            if liq < min_liq:
                continue
            parsed = _yes_no_snapshot(m)
            if parsed is None:
                continue
            yes_p, no_p, yes_label, no_label = parsed
            if not (pmin <= yes_p <= pmax):
                continue
            slug = m.get("slug") or m.get("conditionId") or ""
            if not slug:
                continue
            question = m.get("question") or ""
            if _is_noisy_general_market(slug, question):
                continue
            out.append(
                _MarketSnapshot(
                    condition_id=m.get("conditionId") or "",
                    slug=slug,
                    question=m.get("question") or "",
                    yes_price=yes_p,
                    no_price=no_p,
                    liquidity=liq,
                    volume_24h=float(m.get("volume24hr") or 0),
                    yes_outcome=yes_label,
                    no_outcome=no_label,
                )
            )
        except Exception:
            continue
    return out


def analyze_meanrev(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 3,
) -> list[Signal]:
    """Scan general markets for mean-reversion opportunities."""
    cfg = settings.meanrev
    _engine.configure(cfg.rolling_window)
    markets = discover_general_markets(engine, settings)
    signals: list[Signal] = []

    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions}
    if len(open_positions) >= cfg.max_open_positions:
        log_decision(
            engine.db.data_dir,
            strategy="meanrev",
            decision="skip",
            reason="max_open_positions",
            open_positions=len(open_positions),
        )
        return []

    for m in markets:
        if m.slug in open_slugs:
            continue
        _engine.update(m.condition_id, m.yes_price)
        z = _engine.z_score(m.condition_id, m.yes_price)
        mu = _engine.mean(m.condition_id)
        # Cold-start / flat tape: fade strong Yes/No skews so paper can fill while history warms.
        if z is None or mu is None:
            skew = m.yes_price - 0.5
            if abs(skew) < max(0.18, cfg.min_edge * 6):
                continue
            z = skew / 0.05
            mu = 0.5
        if abs(z) < cfg.min_z_score:
            continue

        ev = abs(m.yes_price - mu)
        if ev < cfg.min_edge:
            continue

        # Edge case: near-certain prices need fatter reversion signal (spread noise).
        tail_price = max(m.yes_price, m.no_price)
        if tail_price >= 0.88 and abs(z) < cfg.min_z_score + 0.35:
            continue
        if tail_price >= 0.88 and ev < cfg.min_edge * 1.5:
            continue

        # Fade the deviation: spike up → buy NO; dump → buy YES.
        if z > 0:
            side = m.no_outcome
            price = m.no_price
        else:
            side = m.yes_outcome
            price = m.yes_price

        p = min(max(0.55, 0.5 + ev), 0.95)
        b = max(0.01, (1 / max(price, 0.01)) - 1)
        q = 1 - p
        f_kelly = max(0, (p * b - q) / b)
        size = min(
            f_kelly * cfg.kelly_fraction * engine.get_account().cash,
            cfg.max_position_usd,
            cfg.position_usd,
        )
        if size < settings.min_position_usd:
            continue

        reason = f"meanrev z={z:+.2f} ev={ev:.3f} @ {price:.3f}"
        signals.append(
            Signal(
                action="buy",
                slug=m.slug,
                outcome=side,
                reason=reason,
                amount_usd=round(size, 2),
                order_type="fak",
                limit_price=None,
                market_condition_id=m.condition_id,
            )
        )
        log_decision(
            engine.db.data_dir,
            strategy="meanrev",
            decision="buy",
            reason=reason,
            slug=m.slug,
            action="buy",
            amount_usd=round(size, 2),
            z=round(z, 3),
        )
        open_slugs.add(m.slug)
        if len(signals) + len(open_positions) >= cfg.max_open_positions:
            break
        if len(signals) >= max_signals:
            break

    log_decision(
        engine.db.data_dir,
        strategy="meanrev",
        decision="scan",
        reason=(
            f"meanrev scan: {len(signals)} signals / {len(markets)} markets / "
            f"open={len(open_positions)}"
        ),
        signals=len(signals),
        markets=len(markets),
    )
    return signals


def meanrev_exits(
    engine: Engine,
    settings: Settings,
) -> list[Signal]:
    """Generate exit signals for mean-reversion positions."""
    cfg = settings.meanrev
    positions = engine.db.get_open_positions()
    signals: list[Signal] = []

    for pos in positions:
        entry = pos.avg_entry_price
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
            bid, _ = best_bid(book)
            if bid is None:
                continue
            current_bid = float(bid)
        except Exception:
            continue

        if current_bid >= entry * (1 + cfg.take_profit_pct):
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    reason=f"meanrev_tp bid={current_bid:.3f}",
                    shares=pos.shares,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                )
            )
            continue

        if current_bid <= entry * (1 - cfg.stop_loss_pct):
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    reason=f"meanrev_sl bid={current_bid:.3f}",
                    shares=pos.shares,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                )
            )

    return signals
