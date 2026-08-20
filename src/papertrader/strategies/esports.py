from __future__ import annotations

from datetime import datetime, timezone

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.esports_markets import EsportsCandidate
from papertrader.esports_state import EsportsExitStore
from papertrader.markets import best_bid
from papertrader.oddspapi import (
    FairMatch,
    find_fair_probability,
    fractional_kelly_usd,
    oddspapi_api_key,
)
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


def _swing_buy_signal(
    engine: Engine,
    candidate: EsportsCandidate,
    settings: Settings,
    *,
    remaining_slots: int,
    cash: float,
    extra_reason: str = "",
) -> Signal | None:
    cfg = settings.esports
    stake = scaled_size(
        cfg.position_usd,
        cash=cash,
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
    if extra_reason:
        reason = f"{reason} ({extra_reason})"
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


def analyze_esports_candidate(
    engine: Engine,
    candidate: EsportsCandidate,
    settings: Settings,
    open_positions: list[Position],
    *,
    fair_matches: list[FairMatch] | None = None,
) -> Signal | None:
    """Buy cheap underdog swings or value bets when OddsPapi shows edge."""
    cfg = settings.esports
    oddsp = cfg.oddspapi
    use_oddsp = oddsp.enabled and bool(oddspapi_api_key()) and fair_matches is not None
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

    fair_match: FairMatch | None = None
    fair_p: float | None = None
    if use_oddsp:
        matched = find_fair_probability(candidate, fair_matches or [])
        if matched is not None:
            fair_match, fair_p = matched
        elif oddsp.require_match:
            _log_esports(
                engine,
                decision="skip",
                reason="no_oddspapi_match",
                slug=candidate.market.slug,
                outcome=candidate.outcome,
            )
            return None

    remaining_slots = cfg.max_open_positions - len(open_positions)
    cash = account_cash(engine, settings.starting_balance)

    if fair_p is not None:
        edge = fair_p - candidate.ask
        if edge >= oddsp.min_edge:
            # Size at the taker ask we actually pay (FAK), not a resting maker quote.
            fill_price = round(candidate.ask, 4)
            stake = fractional_kelly_usd(
                fair_p=fair_p,
                price=fill_price,
                cash=cash,
                kelly_fraction=oddsp.kelly_fraction,
                max_usd=cfg.max_position_usd,
                min_usd=settings.min_position_usd,
            )
            if stake is not None:
                hours_left = (
                    candidate.end_at - datetime.now(timezone.utc)
                ).total_seconds() / 3600
                reason = (
                    f"oddspapi {candidate.outcome} fair={fair_p:.3f} ask={candidate.ask:.3f} "
                    f"edge={edge:.3f} taker@{fill_price:.3f} "
                    f"ends in {hours_left:.1f}h — {candidate.event_title[:60]}"
                )
                _log_esports(
                    engine,
                    decision="buy",
                    reason=reason,
                    slug=candidate.market.slug,
                    outcome=candidate.outcome,
                    ask=candidate.ask,
                    fair_p=fair_p,
                    edge=edge,
                    limit_price=fill_price,
                    fixture_id=fair_match.fixture_id if fair_match else None,
                    event_slug=candidate.event_slug,
                    ends_at=candidate.end_at.isoformat(),
                )
                return Signal(
                    action="buy",
                    slug=candidate.market.slug,
                    outcome=candidate.outcome,
                    amount_usd=stake,
                    reason=reason,
                    # Taker at ask so OddsPapi edge actually fills (maker under-ask often rests).
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=candidate.market.condition_id,
                    event_slug=candidate.event_slug,
                )
            _log_esports(
                engine,
                decision="skip",
                reason="oddspapi_kelly_too_small",
                slug=candidate.market.slug,
                fair_p=round(fair_p, 4),
                limit_price=fill_price,
                **({"fallback": "swing"} if not oddsp.require_match else {}),
            )
            if oddsp.require_match:
                return None
            # Kelly too small with require_match=false: fall through to cheap swing.
        else:
            _log_esports(
                engine,
                decision="skip",
                reason="low_oddspapi_edge",
                slug=candidate.market.slug,
                outcome=candidate.outcome,
                ask=candidate.ask,
                fair_p=round(fair_p, 4),
                edge=round(edge, 4),
                min_edge=oddsp.min_edge,
                **({"fallback": "swing"} if not oddsp.require_match else {}),
            )
            if oddsp.require_match:
                return None
            # Low edge with require_match=false: fall through to cheap swing.

    # require_match / OddsPapi-only mode: never fall back to cheap live swings.
    if oddsp.require_match:
        _log_esports(
            engine,
            decision="skip",
            reason="oddspapi_unavailable" if not use_oddsp else "no_oddspapi_match",
            slug=candidate.market.slug,
            outcome=candidate.outcome,
            ask=candidate.ask,
        )
        return None

    return _swing_buy_signal(
        engine,
        candidate,
        settings,
        remaining_slots=remaining_slots,
        cash=cash,
    )


def esports_exits(
    engine: Engine,
    settings: Settings,
    open_positions: list[Position],
    *,
    exit_store: EsportsExitStore | None = None,
) -> list[Signal]:
    """Place +20% take-profit limits and exit at 80% of entry (stop loss)."""
    cfg = settings.esports
    store = exit_store or EsportsExitStore(engine.db.data_dir)
    store.prune_closed(open_positions)
    signals: list[Signal] = []

    for pos in open_positions:
        if pos.shares <= 0 or pos.is_resolved or pos.avg_entry_price <= 0:
            continue

        entry = pos.avg_entry_price
        tp_price = round(entry * (1 + cfg.take_profit_pct), 4)
        sl_price = round(entry * cfg.stop_loss_entry_pct, 4)
        if tp_price <= 0 or sl_price <= 0:
            continue

        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        bid, _ = best_bid(book)

        if bid is not None and bid <= sl_price:
            if store.take_profit_placed(pos.market_condition_id, pos.outcome):
                store.unmark_take_profit(pos.market_condition_id, pos.outcome)
            reason = (
                f"esports SL bid={bid:.3f} <= {sl_price:.3f} "
                f"({cfg.stop_loss_entry_pct:.0%} of entry {entry:.3f})"
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
                stop_loss_price=sl_price,
                stop_loss_entry_pct=cfg.stop_loss_entry_pct,
            )
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    shares=pos.shares,
                    reason=reason,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                    event_slug=pos.market_slug,
                )
            )
            continue

        if store.take_profit_placed(pos.market_condition_id, pos.outcome):
            continue

        reason = (
            f"esports TP limit @ {tp_price:.3f} (+{cfg.take_profit_pct:.0%} "
            f"on entry {entry:.3f})"
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
            take_profit_pct=cfg.take_profit_pct,
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
