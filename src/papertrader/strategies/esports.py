from __future__ import annotations

from datetime import datetime, timezone

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.esports_markets import EsportsCandidate
from papertrader.esports_state import EsportsExitStore
from papertrader.markets import best_bid
from papertrader.signals import Signal
from papertrader.sizing import account_cash, scaled_size


def _log_esports(
    engine: Engine,
    *,
    decision: str,
    reason: str,
    **extra,
) -> None:
    log_decision(
        engine.db.data_dir,
        strategy="esports",
        decision=decision,
        reason=reason,
        **extra,
    )


def _in_position(positions: list[Position], condition_id: str, outcome: str) -> bool:
    return any(
        p.market_condition_id == condition_id and p.outcome == outcome and p.shares > 0
        for p in positions
    )


def analyze_esports_candidate(
    engine: Engine,
    candidate: EsportsCandidate,
    settings: Settings,
    open_positions: list[Position],
) -> Signal | None:
    """Buy cheap underdog side on live esports/sports matches ending soon."""
    cfg = settings.esports
    if len(open_positions) >= cfg.max_open_positions:
        _log_esports(
            engine,
            decision="skip",
            reason="max_open_positions",
            slug=candidate.market.slug,
            open_positions=len(open_positions),
            ask=candidate.ask,
            ends_at=candidate.end_at.isoformat(),
        )
        return None
    if _in_position(open_positions, candidate.market.condition_id, candidate.outcome):
        _log_esports(
            engine,
            decision="skip",
            reason="already_in_position",
            slug=candidate.market.slug,
            outcome=candidate.outcome,
        )
        return None
    for pos in open_positions:
        if pos.market_slug.startswith(candidate.event_slug):
            _log_esports(
                engine,
                decision="skip",
                reason="already_in_event",
                slug=candidate.market.slug,
                event_slug=candidate.event_slug,
            )
            return None

    remaining_slots = cfg.max_open_positions - len(open_positions)
    stake = scaled_size(
        cfg.position_usd,
        cash=account_cash(engine, settings.starting_balance),
        starting_balance=settings.starting_balance,
        remaining_slots=remaining_slots,
        min_usd=settings.min_position_usd,
        max_usd=cfg.max_position_usd,
    )
    if stake is None:
        _log_esports(
            engine,
            decision="skip",
            reason="insufficient_cash",
            slug=candidate.market.slug,
        )
        return None

    hours_left = (candidate.end_at - datetime.now(timezone.utc)).total_seconds() / 3600
    reason = (
        f"live swing {candidate.outcome} @ {candidate.ask:.3f} "
        f"ends in {hours_left:.1f}h — {candidate.event_title[:80]}"
    )
    _log_esports(
        engine,
        decision="buy",
        reason=reason,
        slug=candidate.market.slug,
        outcome=candidate.outcome,
        ask=candidate.ask,
        event_slug=candidate.event_slug,
        ends_at=candidate.end_at.isoformat(),
        event_volume=candidate.event_volume,
    )
    return Signal(
        action="buy",
        slug=candidate.market.slug,
        outcome=candidate.outcome,
        amount_usd=stake,
        reason=reason,
        order_type="fak",
        limit_price=None,
        market_condition_id=candidate.market.condition_id,
        event_slug=candidate.event_slug,
    )


def esports_exits(
    engine: Engine,
    settings: Settings,
    open_positions: list[Position],
    *,
    exit_store: EsportsExitStore | None = None,
) -> list[Signal]:
    """Place a resting limit sell at 2x entry for each open esports position."""
    cfg = settings.esports
    store = exit_store or EsportsExitStore(engine.db.data_dir)
    store.prune_closed(open_positions)
    signals: list[Signal] = []

    for pos in open_positions:
        if pos.shares <= 0 or pos.is_resolved or pos.avg_entry_price <= 0:
            continue
        if store.take_profit_placed(pos.market_condition_id, pos.outcome):
            continue

        tp_price = round(pos.avg_entry_price * cfg.take_profit_multiple, 4)
        if tp_price <= 0:
            continue

        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        bid, _ = best_bid(book)

        reason = (
            f"esports TP limit @ {tp_price:.3f} ({cfg.take_profit_multiple:.0f}x "
            f"entry {pos.avg_entry_price:.3f})"
        )
        _log_esports(
            engine,
            decision="sell",
            reason=reason,
            slug=pos.market_slug,
            outcome=pos.outcome,
            action="sell",
            shares=pos.shares,
            bid=bid,
            take_profit_price=tp_price,
            take_profit_multiple=cfg.take_profit_multiple,
        )
        signals.append(
            Signal(
                action="sell",
                slug=pos.market_slug,
                outcome=pos.outcome,
                shares=pos.shares,
                reason=reason,
                order_type="limit",
                limit_price=tp_price,
                market_condition_id=pos.market_condition_id,
                event_slug=pos.market_slug,
                esports_take_profit=True,
            )
        )
    return signals
