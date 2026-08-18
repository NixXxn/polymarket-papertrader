from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import AsymmetricSettings, City
from papertrader.markets import best_bid, city_from_market_slug, date_from_temp_slug
from papertrader.quant.position_state import PositionExitStore
from papertrader.quant.shadow_ledger import ShadowLedger
from papertrader.decision_log import log_decision
from papertrader.signals import Signal


@dataclass(frozen=True)
class LadderStep:
    multiple: float
    fraction: float


@dataclass(frozen=True)
class MonitorConfig:
    ladder: tuple[LadderStep, ...] = (
        LadderStep(2.0, 0.10),
        LadderStep(5.0, 0.10),
        LadderStep(10.0, 0.15),
        LadderStep(20.0, 0.15),
        LadderStep(50.0, 0.10),
    )
    hours_before_resolution: int = 1
    resolution_hour_local: int = 23


def monitor_config_from_settings(settings: AsymmetricSettings) -> MonitorConfig:
    ladder = tuple(
        LadderStep(step.multiple, step.fraction) for step in settings.exit_ladder
    )
    return MonitorConfig(
        ladder=ladder,
        hours_before_resolution=settings.hours_before_resolution,
    )


def resolution_deadline(city, event_date: date, hour: int = 23) -> datetime:
    local = datetime(event_date.year, event_date.month, event_date.day, hour, 0, 0)
    return local.replace(tzinfo=ZoneInfo(city.tz))


def _engine_data_dir(engine: Engine) -> Path | None:
    db = getattr(engine, "db", None)
    data_dir = getattr(db, "data_dir", None)
    return Path(data_dir) if data_dir else None


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
    """Staged ladder trims on the way up; hard exit before resolution."""
    cfg = cfg or MonitorConfig()
    now = now or datetime.now(timezone.utc)
    signals: list[Signal] = []
    ladder = sorted(cfg.ladder, key=lambda step: step.multiple)
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
            data_dir = _engine_data_dir(engine)
            if data_dir is not None:
                log_decision(
                    data_dir,
                    strategy="asymmetric",
                    decision="sell",
                    reason=f"time stop: <{cfg.hours_before_resolution}h to resolution",
                    city=city.slug,
                    event_date=event_date,
                    slug=pos.market_slug,
                    action="sell",
                    shares=pos.shares,
                    bid=bid,
                )
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

        entry = pos.avg_entry_price
        if entry <= 0:
            continue
        for step in ladder:
            if exit_store is not None and condition_id:
                if exit_store.ladder_level_hit(condition_id, pos.outcome, step.multiple):
                    continue
            tp_price = entry * step.multiple
            if bid < tp_price:
                continue
            sell_shares = pos.shares * step.fraction
            if sell_shares <= 0:
                continue
            pct = int(round(step.fraction * 100))
            gain_pct = int(round((step.multiple - 1) * 100))
            reason = (
                f"ladder trim {pct}% @ {step.multiple:.0f}x entry "
                f"(+{gain_pct}% bid={bid:.3f} target={tp_price:.3f})"
            )
            if exit_store is not None and condition_id:
                exit_store.mark_ladder_level(
                    condition_id,
                    pos.outcome,
                    step.multiple,
                    market_slug=pos.market_slug,
                )
            data_dir = _engine_data_dir(engine)
            if data_dir is not None:
                log_decision(
                    data_dir,
                    strategy="asymmetric",
                    decision="sell",
                    reason=reason,
                    city=city.slug,
                    event_date=event_date,
                    slug=pos.market_slug,
                    action="sell",
                    shares=sell_shares,
                    bid=bid,
                    partial_exit=True,
                    ladder_multiple=step.multiple,
                )
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    shares=sell_shares,
                    city=city,
                    reason=reason,
                    limit_price=tp_price,
                    order_type="limit",
                    partial_exit=True,
                    ladder_multiple=step.multiple,
                    market_condition_id=condition_id,
                )
            )
    return signals
