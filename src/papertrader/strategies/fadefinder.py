"""Prediction Hunt fade-finder / smart-money strategy for sports & whale fades."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pm_trader.engine import Engine
from pm_trader.models import Position

from papertrader.config import Settings
from papertrader.decision_log import log_decision
from papertrader.fadefinder_state import FadeFinderState
from papertrader.markets import best_ask, best_bid
from papertrader.predictionhunt import (
    FadeAlert,
    PredictionHuntClient,
    SportsFadeOpportunity,
    extract_sports_fades,
    resolve_polymarket_slug_from_sports,
)
from papertrader.signals import Signal
from papertrader.sizing import account_cash, scaled_size


def _log_fade(
    engine: Engine,
    *,
    decision: str,
    reason: str,
    **extra,
) -> None:
    log_decision(
        engine.db.data_dir,
        strategy="fadefinder",
        decision=decision,
        reason=reason,
        **extra,
    )


def _in_position(positions: list[Position], condition_id: str, outcome: str) -> bool:
    return any(
        p.market_condition_id == condition_id and p.outcome == outcome and p.shares > 0
        for p in positions
    )


def _sports_dedupe_key(opp: SportsFadeOpportunity) -> str:
    gid = opp.group_id if opp.group_id is not None else "na"
    return f"{opp.sport}:{gid}:{opp.team}:{opp.polymarket_slug}"


def _buy_signal(
    engine: Engine,
    settings: Settings,
    *,
    slug: str,
    outcome: str,
    ask: float,
    reason: str,
    remaining_slots: int,
    cash: float,
    condition_id: str | None = None,
    event_slug: str | None = None,
    source: str,
    extra: dict | None = None,
) -> Signal | None:
    cfg = settings.fadefinder
    stake = scaled_size(
        cfg.position_usd,
        cash=cash,
        starting_balance=settings.starting_balance,
        remaining_slots=remaining_slots,
        min_usd=settings.min_position_usd,
        max_usd=cfg.max_position_usd,
    )
    if stake is None:
        _log_fade(engine, decision="skip", reason="insufficient_cash", slug=slug, source=source)
        return None

    log_extra = {"slug": slug, "outcome": outcome, "ask": ask, "source": source}
    if extra:
        log_extra.update(extra)
    _log_fade(engine, decision="buy", reason=reason, **log_extra)
    return Signal(
        action="buy",
        slug=slug,
        outcome=outcome,
        amount_usd=stake,
        reason=reason,
        order_type="fak",
        limit_price=None,
        market_condition_id=condition_id,
        event_slug=event_slug,
    )


def analyze_fade_alert(
    engine: Engine,
    alert: FadeAlert,
    settings: Settings,
    open_positions: list[Position],
    *,
    state: FadeFinderState,
    remaining_slots: int,
    cash: float,
    source: str,
) -> Signal | None:
    cfg = settings.fadefinder
    if state.seen_alert(alert.alert_id):
        return None
    if not alert.market_slug:
        _log_fade(
            engine,
            decision="skip",
            reason="alert_missing_slug",
            alert_id=alert.alert_id,
            source=source,
        )
        state.mark_alert(alert.alert_id)
        return None
    if alert.stake_usd is not None and alert.stake_usd < cfg.min_whale_stake_usd:
        _log_fade(
            engine,
            decision="skip",
            reason="whale_stake_too_small",
            alert_id=alert.alert_id,
            stake_usd=alert.stake_usd,
            source=source,
        )
        state.mark_alert(alert.alert_id)
        return None

    outcome = alert.fade_outcome
    try:
        market = engine.api.get_market(alert.market_slug)
        token = market.get_token_id(outcome)
        book = engine.api.get_order_book(token)
    except Exception as e:
        _log_fade(
            engine,
            decision="skip",
            reason="market_unavailable",
            alert_id=alert.alert_id,
            slug=alert.market_slug,
            error=str(e),
            source=source,
        )
        return None

    ask, _ = best_ask(book)
    if ask is None:
        _log_fade(
            engine,
            decision="skip",
            reason="no_ask",
            alert_id=alert.alert_id,
            slug=alert.market_slug,
            source=source,
        )
        return None

    if outcome == "no":
        if ask < cfg.min_no_ask or ask > cfg.max_no_ask:
            _log_fade(
                engine,
                decision="skip",
                reason="no_ask_out_of_range",
                alert_id=alert.alert_id,
                ask=ask,
                source=source,
            )
            state.mark_alert(alert.alert_id)
            return None
    elif ask < cfg.min_yes_ask or ask > cfg.max_yes_ask:
        _log_fade(
            engine,
            decision="skip",
            reason="yes_ask_out_of_range",
            alert_id=alert.alert_id,
            ask=ask,
            source=source,
        )
        state.mark_alert(alert.alert_id)
        return None

    if _in_position(open_positions, market.condition_id, outcome):
        state.mark_alert(alert.alert_id)
        return None

    reason = (
        f"PH {source} fade {outcome.upper()} @ {ask:.3f} — {alert.title[:100]}"
        f" (alert #{alert.alert_id})"
    )
    sig = _buy_signal(
        engine,
        settings,
        slug=alert.market_slug,
        outcome=outcome,
        ask=ask,
        reason=reason,
        remaining_slots=remaining_slots,
        cash=cash,
        condition_id=market.condition_id,
        source=source,
        extra={"alert_id": alert.alert_id, "alert_type": alert.alert_type},
    )
    if sig is not None:
        state.mark_alert(alert.alert_id)
    return sig


def analyze_sports_fade(
    engine: Engine,
    opp: SportsFadeOpportunity,
    settings: Settings,
    open_positions: list[Position],
    *,
    state: FadeFinderState,
    remaining_slots: int,
    cash: float,
) -> Signal | None:
    cfg = settings.fadefinder
    key = _sports_dedupe_key(opp)
    if state.seen_sports_key(key):
        return None

    try:
        market = engine.api.get_market(opp.polymarket_slug)
        token = market.get_token_id("no")
        book = engine.api.get_order_book(token)
    except Exception as e:
        _log_fade(
            engine,
            decision="skip",
            reason="sports_market_unavailable",
            slug=opp.polymarket_slug,
            error=str(e),
            sport=opp.sport,
        )
        return None

    no_ask, _ = best_ask(book)
    if no_ask is None:
        _log_fade(
            engine,
            decision="skip",
            reason="sports_no_ask",
            slug=opp.polymarket_slug,
            sport=opp.sport,
        )
        return None
    if no_ask < cfg.min_no_ask or no_ask > cfg.max_no_ask:
        _log_fade(
            engine,
            decision="skip",
            reason="sports_no_ask_out_of_range",
            slug=opp.polymarket_slug,
            no_ask=no_ask,
            sport=opp.sport,
        )
        state.mark_sports_key(key)
        return None

    if _in_position(open_positions, market.condition_id, "no"):
        state.mark_sports_key(key)
        return None

    reason = (
        f"sports fade NO @ {no_ask:.3f} — PM YES {opp.polymarket_yes:.3f} vs "
        f"consensus {opp.consensus_yes:.3f} (+{opp.dislocation:.3f}) "
        f"{opp.team} ({opp.sport})"
    )
    sig = _buy_signal(
        engine,
        settings,
        slug=opp.polymarket_slug,
        outcome="no",
        ask=no_ask,
        reason=reason,
        remaining_slots=remaining_slots,
        cash=cash,
        condition_id=market.condition_id,
        source="sports-matching",
        extra={
            "sport": opp.sport,
            "dislocation": round(opp.dislocation, 4),
            "consensus_yes": round(opp.consensus_yes, 4),
        },
    )
    if sig is not None:
        state.mark_sports_key(key)
    return sig


def fadefinder_exits(
    engine: Engine,
    settings: Settings,
    open_positions: list[Position],
    *,
    state: FadeFinderState | None = None,
) -> list[Signal]:
    """Take-profit and stop-loss for fade-finder positions."""
    cfg = settings.fadefinder
    store = state or FadeFinderState(engine.db.data_dir)
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
                f"fadefinder SL bid={bid:.3f} <= {sl_price:.3f} "
                f"(entry {entry:.3f})"
            )
            _log_fade(
                engine,
                decision="exit",
                reason=reason,
                slug=pos.market_slug,
                outcome=pos.outcome,
            )
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    amount_usd=pos.shares * bid,
                    reason=reason,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                )
            )
            continue

        if bid is not None and bid >= tp_price:
            reason = (
                f"fadefinder TP bid={bid:.3f} >= {tp_price:.3f} "
                f"(entry {entry:.3f})"
            )
            _log_fade(
                engine,
                decision="exit",
                reason=reason,
                slug=pos.market_slug,
                outcome=pos.outcome,
            )
            signals.append(
                Signal(
                    action="sell",
                    slug=pos.market_slug,
                    outcome=pos.outcome,
                    amount_usd=pos.shares * bid,
                    reason=reason,
                    order_type="fak",
                    limit_price=None,
                    market_condition_id=pos.market_condition_id,
                )
            )

    return signals


def discover_fade_opportunities(
    engine: Engine,
    settings: Settings,
    *,
    ph_client: PredictionHuntClient,
    state: FadeFinderState,
) -> dict[str, int | str | None]:
    """Poll PH fade-finder, smart-money, and sports matching."""
    cfg = settings.fadefinder
    stats: dict[str, int | str | None] = {
        "fade_alerts": 0,
        "smart_alerts": 0,
        "sports_opps": 0,
        "fade_blocked": None,
        "smart_blocked": None,
    }
    since = (
        datetime.now(timezone.utc) - timedelta(hours=cfg.alert_lookback_hours)
    ).isoformat()

    if cfg.use_fade_alerts:
        alerts, block = ph_client.fetch_fade_finder_alerts(
            platform="polymarket",
            limit=cfg.alert_limit,
            since=since,
        )
        stats["fade_alerts"] = len(alerts)
        stats["fade_blocked"] = block
        stats["_fade_alerts"] = alerts  # type: ignore[assignment]

    if cfg.use_smart_money_alerts:
        smart, block = ph_client.fetch_smart_money_alerts(
            platform="polymarket",
            limit=cfg.alert_limit,
            since=since,
        )
        stats["smart_alerts"] = len(smart)
        stats["smart_blocked"] = block
        stats["_smart_alerts"] = smart  # type: ignore[assignment]

    sports: list[SportsFadeOpportunity] = []
    if cfg.sports_fallback:
        sports_batch = state.next_sports(cfg.sports, cfg.sports_per_scan)
        stats["sports_scanned"] = len(sports_batch)
        for sport in sports_batch:
            payload = ph_client.fetch_sports_matching(
                sport=sport,
                cache_ttl_hours=cfg.sports_cache_ttl_hours,
            )
            if not payload:
                continue
            sports.extend(
                extract_sports_fades(
                    payload,
                    sport=sport,
                    min_dislocation=cfg.min_dislocation,
                    slug_resolver=resolve_polymarket_slug_from_sports,
                )
            )
    stats["sports_opps"] = len(sports)
    stats["_sports_opps"] = sports  # type: ignore[assignment]
    return stats
