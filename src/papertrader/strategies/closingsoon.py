"""Closing Soon strategy: buy markets resolving in 6-48hrs with strong directional momentum."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pm_trader.engine import Engine

from papertrader.config import ClosingSoonSettings, Settings
from papertrader.decision_log import log_decision
from papertrader.signals import Signal

log = logging.getLogger("papertrader")


def analyze_closingsoon(
    engine: Engine,
    settings: Settings,
    *,
    max_signals: int = 3,
    now: datetime | None = None,
) -> list[Signal]:
    """Scan markets resolving soon with strong directional momentum."""
    cfg = settings.closingsoon
    now = now or datetime.now(timezone.utc)

    try:
        data = engine.api._gamma_get(
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 200,
                "order": "endDate",
                "ascending": "true",
            },
        )
    except Exception as e:
        log.warning("closingsoon: failed to fetch markets: %s", e)
        return []

    if not isinstance(data, list):
        return []

    open_positions = engine.db.get_open_positions()
    open_slugs = {p.market_slug for p in open_positions}
    if len(open_positions) >= cfg.max_open_positions:
        return []

    signals: list[Signal] = []

    for m in data:
        try:
            end_date_str = m.get("endDate") or ""
            if not end_date_str:
                continue
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end_dt - now).total_seconds() / 3600
            if not (cfg.min_hours <= hours_left <= cfg.max_hours):
                continue

            tokens = m.get("tokens") or []
            yt = next((t for t in tokens if (t.get("outcome") or "").upper() == "YES"), {})
            nt = next((t for t in tokens if (t.get("outcome") or "").upper() == "NO"), {})
            yes_p = float(yt.get("price") or 0.5)
            no_p = float(nt.get("price") or 0.5)

            if not (cfg.price_min <= yes_p <= cfg.price_max):
                continue

            liq = float(m.get("liquidity") or 0)
            if liq < cfg.min_liquidity:
                continue

            slug = m.get("slug") or m.get("conditionId") or ""
            if slug in open_slugs:
                continue

            # Direction: distance from 0.5
            direction = abs(yes_p - 0.5)
            if direction < cfg.min_direction:
                continue

            # Edge scales with direction and time pressure
            time_factor = max(0.5, 1.0 - hours_left / cfg.max_hours)
            edge = direction * 0.10 * time_factor
            if edge < cfg.min_edge:
                continue

            # Follow the momentum direction
            if yes_p > 0.5:
                side = "Yes"
                price = yes_p
            else:
                side = "No"
                price = no_p

            # Kelly sizing
            p = min(max(0.55, 0.5 + edge), 0.92)
            b = max(0.01, (1 / price) - 1)
            q = 1 - p
            f_kelly = max(0, (p * b - q) / b)
            size = min(
                f_kelly * cfg.kelly_fraction * engine.get_account().cash,
                cfg.max_position_usd,
                cfg.position_usd,
            )
            if size < 1:
                continue

            signals.append(Signal(
                action="buy",
                slug=slug,
                outcome=side,
                reason=f"closingsoon {hours_left:.0f}h dir={direction:.2f}",
                amount_usd=round(size, 2),
                order_type="limit",
                limit_price=round(price, 2),
                market_condition_id=m.get("conditionId") or "",
            ))

            log_decision(
                engine.db.data_dir,
                strategy="closingsoon",
                decision="signal",
                reason=f"hours={hours_left:.0f} dir={direction:.2f}",
                slug=slug,
                action="buy",
                amount_usd=round(size, 2),
            )

            if len(signals) >= max_signals:
                break
        except Exception:
            continue

    return signals


def closingsoon_exits(
    engine: Engine,
    settings: Settings,
) -> list[Signal]:
    """Generate exit signals for closing-soon positions (SL only; TP = resolution at $1)."""
    cfg = settings.closingsoon
    positions = engine.db.get_open_positions()
    signals: list[Signal] = []

    for pos in positions:
        entry = pos.avg_entry_price
        try:
            book = engine.get_order_book(pos.market_slug, pos.outcome)
            if not book or not book.bids:
                continue
            current_bid = float(book.bids[0].price)
        except Exception:
            continue

        if current_bid <= entry * (1 - cfg.stop_loss_pct):
            signals.append(Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                reason=f"closingsoon_sl bid={current_bid:.3f}",
                shares=pos.shares,
                order_type="limit",
                limit_price=round(current_bid, 2),
            ))

    return signals
