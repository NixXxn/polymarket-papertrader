from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import City
from papertrader.markets import best_bid, city_from_market_slug, date_from_temp_slug
from papertrader.quant.position_state import PositionExitStore
from papertrader.quant.shadow_ledger import ShadowLedger
from papertrader.signals import Signal


@dataclass(frozen=True)
class MonitorConfig:
    take_profit_multiple: float = 2.0
    take_profit_fraction: float = 0.5
    hours_before_resolution: int = 6
    resolution_hour_local: int = 23


def resolution_deadline(city: City, event_date: date, hour: int = 23) -> datetime:
    local = datetime(event_date.year, event_date.month, event_date.day, hour, 0, 0)
    return local.replace(tzinfo=ZoneInfo(city.tz))


def monitor_exits(
    engine: Engine,
    positions: list[Position],
    cities: dict[str, City],
    *,
    cfg: MonitorConfig | None = None,
    now: datetime | None = None,
    shadow: ShadowLedger | None = None,
    exit_store: PositionExitStore | None = None,
) -> list[Signal]:
    """Limit-style exit rules: 50% at 2x entry; hard exit 6h before resolution."""
    cfg = cfg or MonitorConfig()
    now = now or datetime.now(timezone.utc)
    signals: list[Signal] = []
    for pos in positions:
        if pos.shares <= 0:
            continue
        city = city_from_market_slug(pos.market_slug, cities)
        event_date = date_from_temp_slug(pos.market_slug)
        if city is None or event_date is None:
            continue
        try:
            token = engine.api.get_market(pos.market_slug).get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        bid, _ = best_bid(book)
        if bid is None:
            continue
        if shadow is not None:
            shadow.log_exit_simulation(
                slug=pos.market_slug,
                entry_price=pos.avg_entry_price,
                current_price=bid,
                shares=pos.shares,
                target_pct=0.20,
            )
        deadline = resolution_deadline(city, event_date, cfg.resolution_hour_local)
        hard_exit_at = deadline - timedelta(hours=cfg.hours_before_resolution)
        hard_exit_at_utc = hard_exit_at.astimezone(timezone.utc)
        condition_id = getattr(pos, "market_condition_id", None)
        if now >= hard_exit_at_utc:
            if exit_store is not None and condition_id:
                exit_store.clear(condition_id, pos.outcome)
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    shares=pos.shares,
                    city=city,
                    reason=f"time stop: <{cfg.hours_before_resolution}h to resolution",
                    limit_price=bid,
                    order_type="limit",
                    market_condition_id=condition_id,
                )
            )
            continue
        tp_price = pos.avg_entry_price * cfg.take_profit_multiple
        partial_done = (
            exit_store is not None
            and condition_id
            and exit_store.partial_tp_done(condition_id, pos.outcome)
        )
        if not partial_done and bid >= tp_price:
            sell_shares = pos.shares * cfg.take_profit_fraction
            if sell_shares > 0:
                if exit_store is not None and condition_id:
                    exit_store.mark_partial_tp(
                        condition_id, pos.outcome, market_slug=pos.market_slug
                    )
                signals.append(
                    Signal(
                        action="sell",
                        slug=pos.market_slug,
                        outcome=pos.outcome,
                        shares=sell_shares,
                        city=city,
                        reason=f"limit TP 50% @ {tp_price:.3f} (bid={bid:.3f})",
                        limit_price=tp_price,
                        order_type="limit",
                        partial_exit=True,
                        market_condition_id=condition_id,
                    )
                )
    return signals
