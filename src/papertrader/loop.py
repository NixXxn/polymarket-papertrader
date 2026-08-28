from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from pm_trader.engine import Engine
from pm_trader.models import MarketNotFoundError, NoPositionError, OrderRejectedError, SimError

from papertrader.config import Settings
from papertrader.execution import ExecutionContext, log_fill_latency
from papertrader.live import LiveTrader
from papertrader.quant.position_state import PositionExitStore
from papertrader.copytrade import sync_copy_trades
from papertrader.decision_log import log_decision, purge_stale_logs, format_skip_summary
from papertrader.markets import city_local_today, discover_events, event_dates, temperature_event_slug
from papertrader.report import ScanCounts, combine_engines, format_scan_update
from papertrader.scan_history import append_scan
from papertrader.signals import Signal
from papertrader.trade_log import append_activity, append_skipped
from papertrader.esports_state import EsportsExitStore
from papertrader.momentum_state import MomentumExitStore
from papertrader.strategies.esports import analyze_esports_candidate, esports_exits
from papertrader.strategies.momentum import (
    TokenWatch,
    analyze_momentum_entry,
    build_token_watches,
    momentum_exits,
    tick_from_order_book,
)
from papertrader.weather_ws_client import MarketTick, run_market_websocket
from papertrader.esports_markets import discover_esports_markets
from papertrader.oddspapi import OddsPapiService, oddspapi_api_key
from papertrader.strategies.asymmetric import analyze_asymmetric_event, asymmetric_exits
from papertrader.strategies.contrarian import analyze_contrarian_event, contrarian_exits
from papertrader.strategies.conviction import analyze_conviction_event, conviction_exits
from papertrader.strategies.safe import analyze_safe_event, safe_exits
from papertrader.weather import WeatherHttp
from papertrader.weather.ensemble import prefetch_combined_ensembles

log = logging.getLogger("papertrader")


def _mark_momentum_take_profit(engine: Engine, signal: Signal) -> None:
    if not signal.momentum_take_profit or signal.limit_price is None:
        return
    condition_id = signal.market_condition_id
    if not condition_id:
        return
    MomentumExitStore(engine.db.data_dir).mark_take_profit(
        condition_id,
        signal.outcome,
        market_slug=signal.slug,
        take_profit_price=float(signal.limit_price),
    )


def _rollback_momentum_take_profit(
    engine: Engine, signal: Signal, ctx: ExecutionContext | None = None
) -> None:
    if not signal.momentum_take_profit:
        return
    condition_id = signal.market_condition_id
    if not condition_id:
        try:
            market = (ctx or ExecutionContext()).get_market(engine, signal.slug)
            condition_id = market.condition_id
        except Exception:
            return
    MomentumExitStore(engine.db.data_dir).unmark_take_profit(condition_id, signal.outcome)


def _rollback_esports_take_profit(
    engine: Engine, signal: Signal, ctx: ExecutionContext | None = None
) -> None:
    if not signal.esports_take_profit:
        return
    condition_id = signal.market_condition_id
    if not condition_id:
        try:
            market = (ctx or ExecutionContext()).get_market(engine, signal.slug)
            condition_id = market.condition_id
        except Exception:
            return
    EsportsExitStore(engine.db.data_dir).unmark_take_profit(condition_id, signal.outcome)


def _mark_esports_take_profit(engine: Engine, signal: Signal) -> None:
    if not signal.esports_take_profit or signal.limit_price is None:
        return
    condition_id = signal.market_condition_id
    if not condition_id:
        return
    EsportsExitStore(engine.db.data_dir).mark_take_profit(
        condition_id,
        signal.outcome,
        market_slug=signal.slug,
        take_profit_price=float(signal.limit_price),
    )


def _rollback_partial_exit(
    engine: Engine, signal: Signal, ctx: ExecutionContext | None = None
) -> None:
    if not signal.partial_exit:
        return
    condition_id = signal.market_condition_id
    if not condition_id:
        try:
            market = (ctx or ExecutionContext()).get_market(engine, signal.slug)
            condition_id = market.condition_id
        except Exception:
            return
    store = PositionExitStore(engine.db.data_dir)
    if signal.ladder_multiple is not None:
        store.unmark_ladder_level(condition_id, signal.outcome, signal.ladder_multiple)
    else:
        store.unmark_partial_tp(condition_id, signal.outcome)


def _sync_live_engines(
    live: LiveTrader,
    engines: list[tuple[str, Engine]],
) -> None:
    for strategy, engine in engines:
        try:
            live.sync_live_orders(engine, strategy=strategy)
        except Exception as e:
            log.warning("live sync failed (%s): %s", strategy, e)
            append_activity(
                engine.db.data_dir,
                level="error",
                event="live_sync_failed",
                strategy=strategy,
                message=str(e),
            )


def _purge_logs(engines: list[Engine]) -> None:
    if not engines:
        return
    removed = purge_stale_logs(engines[0].db.data_dir)
    total = sum(removed.values())
    if total:
        append_activity(
            engines[0].db.data_dir,
            level="info",
            event="log_purge",
            strategy="system",
            message=f"purged {total} log line(s) older than 3 days",
            removed=removed,
        )


def _log_missing_markets(
    engine: Engine,
    *,
    strategy: str,
    cities: list,
    events: list,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    seen = {(city.slug, event_date) for _slug, event_date, city, _buckets, _vol in events}
    for city in cities:
        for event_date in event_dates(settings.horizon_days, city_local_today(city, now)):
            if (city.slug, event_date) in seen:
                continue
            log_decision(
                engine.db.data_dir,
                strategy=strategy,
                decision="skip",
                reason="no_polymarket_event",
                city=city.slug,
                event_date=event_date,
                event_slug=temperature_event_slug(city.slug, event_date),
            )


def execute_signal(
    engine: Engine,
    signal: Signal,
    dry_run: bool,
    live: LiveTrader | None = None,
    ctx: ExecutionContext | None = None,
    strategy: str = "unknown",
) -> bool:
    if dry_run:
        log_decision(
            engine.db.data_dir,
            strategy=strategy,
            decision="dry_run",
            reason=signal.reason,
            city=signal.city.slug if signal.city else None,
            slug=signal.slug,
            action=signal.action,
            amount_usd=signal.amount_usd,
            shares=signal.shares,
        )
        log.info(
            "DRY-RUN %s %s %s usd=%s shares=%s — %s",
            signal.action,
            signal.slug,
            signal.outcome,
            signal.amount_usd,
            signal.shares,
            signal.reason,
        )
        return False
    try:
        started = time.perf_counter()
        if live is not None:
            filled = live.fill(engine, signal, ctx=ctx, strategy=strategy)
            log_fill_latency(f"LIVE {signal.action.upper()} {signal.slug}", started)
            if filled and signal.esports_take_profit:
                _mark_esports_take_profit(engine, signal)
            if filled and signal.momentum_take_profit:
                _mark_momentum_take_profit(engine, signal)
            if filled:
                log_decision(
                    engine.db.data_dir,
                    strategy=strategy,
                    decision="executed",
                    reason=signal.reason,
                    city=signal.city.slug if signal.city else None,
                    slug=signal.slug,
                    action=signal.action,
                    amount_usd=signal.amount_usd,
                    shares=signal.shares,
                )
            return filled
        if signal.order_type == "limit" and signal.limit_price is not None:
            before = engine.db.get_trades(limit=1)
            before_trade_id = before[0].id if before else None
            if signal.esports_take_profit and signal.action == "sell":
                _mark_esports_take_profit(engine, signal)
            if signal.momentum_take_profit and signal.action == "sell":
                _mark_momentum_take_profit(engine, signal)
            if signal.action == "buy":
                amount = float(signal.amount_usd or 0)
                if amount <= 0:
                    raise OrderRejectedError("limit buy amount is zero")
                engine.place_limit_order(
                    signal.slug,
                    signal.outcome,
                    "buy",
                    amount,
                    signal.limit_price,
                    order_type="gtc",
                )
            else:
                shares = float(signal.shares or 0)
                if shares <= 0:
                    raise OrderRejectedError("limit sell shares is zero")
                engine.place_limit_order(
                    signal.slug,
                    signal.outcome,
                    "sell",
                    shares,
                    signal.limit_price,
                    order_type="gtc",
                )
            engine.check_orders()
            after = engine.db.get_trades(limit=1)
            after_trade_id = after[0].id if after else None
            filled = after_trade_id != before_trade_id
            log_fill_latency(f"PAPER LIMIT {signal.action.upper()} {signal.slug}", started)
            if filled:
                log_decision(
                    engine.db.data_dir,
                    strategy=strategy,
                    decision="executed",
                    reason=signal.reason,
                    city=signal.city.slug if signal.city else None,
                    slug=signal.slug,
                    action=signal.action,
                    amount_usd=signal.amount_usd,
                    shares=signal.shares,
                )
                return True
            append_activity(
                engine.db.data_dir,
                level="info",
                event="limit_order_submitted",
                strategy=strategy,
                message="limit order placed but not immediately filled",
                slug=signal.slug,
                outcome=signal.outcome,
                action=signal.action,
                limit_price=signal.limit_price,
            )
            log_decision(
                engine.db.data_dir,
                strategy=strategy,
                decision="limit_submitted",
                reason=signal.reason,
                city=signal.city.slug if signal.city else None,
                slug=signal.slug,
                action=signal.action,
                amount_usd=signal.amount_usd,
                shares=signal.shares,
                limit_price=signal.limit_price,
            )
            return False
        if signal.action == "buy":
            result = engine.buy(
                signal.slug, signal.outcome, float(signal.amount_usd or 0), order_type="fak"
            )
            log.info(
                "BUY %s @ %.3f shares=%.2f fee=%.4f — %s",
                signal.slug,
                result.trade.avg_price,
                result.trade.shares,
                result.trade.fee,
                signal.reason,
            )
            log_fill_latency(f"PAPER BUY {signal.slug}", started)
        else:
            result = engine.sell(
                signal.slug, signal.outcome, float(signal.shares or 0), order_type="fak"
            )
            log.info(
                "SELL %s @ %.3f shares=%.2f — %s",
                signal.slug,
                result.trade.avg_price,
                result.trade.shares,
                signal.reason,
            )
            log_fill_latency(f"PAPER SELL {signal.slug}", started)
        log_decision(
            engine.db.data_dir,
            strategy=strategy,
            decision="executed",
            reason=signal.reason,
            city=signal.city.slug if signal.city else None,
            slug=signal.slug,
            action=signal.action,
            amount_usd=signal.amount_usd,
            shares=signal.shares,
        )
        return True
    except (OrderRejectedError, NoPositionError, SimError) as e:
        log.warning("Order skipped: %s (%s)", e, signal.reason)
        if not dry_run:
            _rollback_partial_exit(engine, signal, ctx=ctx)
            _rollback_esports_take_profit(engine, signal, ctx=ctx)
            _rollback_momentum_take_profit(engine, signal, ctx=ctx)
            append_skipped(engine.db.data_dir, strategy=strategy, signal=signal, error=str(e))
            append_activity(
                engine.db.data_dir,
                level="warn",
                event="order_skipped",
                strategy=strategy,
                message=str(e),
                slug=signal.slug,
                outcome=signal.outcome,
                action=signal.action,
                reason=signal.reason,
            )
            log_decision(
                engine.db.data_dir,
                strategy=strategy,
                decision="execution_failed",
                reason=str(e),
                level="warn",
                city=signal.city.slug if signal.city else None,
                slug=signal.slug,
                action=signal.action,
                signal_reason=signal.reason,
            )
        return False


def _resolve_one_market(engine: Engine, slug: str) -> int:
    """Resolve a single closed market; 0 if still open or unavailable."""
    try:
        market = engine.api.get_market(slug)
    except MarketNotFoundError:
        log.warning("resolve skip (market gone): %s", slug)
        return 0
    except Exception as e:
        log.debug("resolve lookup %s: %s", slug, e)
        return 0
    if not getattr(market, "closed", False):
        return 0
    try:
        results = engine.resolve_market(slug)
    except Exception as e:
        log.debug("resolve_market %s: %s", slug, e)
        return 0
    for r in results:
        log.info("Resolved %s payout=%.2f", r.position.market_slug, r.payout)
    return len(results)


def _resolve_per_position(engine: Engine) -> int:
    """Continue past missing markets so one stale slug cannot block the book."""
    resolved = 0
    seen: set[str] = set()
    for pos in engine.db.get_open_positions():
        key = pos.market_condition_id or pos.market_slug
        if not key or key in seen:
            continue
        seen.add(key)
        resolved += _resolve_one_market(engine, pos.market_slug)
    return resolved


def _resolve(engine: Engine) -> int:
    try:
        results = engine.resolve_all()
        for r in results:
            log.info("Resolved %s payout=%.2f", r.position.market_slug, r.payout)
        return len(results)
    except MarketNotFoundError as e:
        log.warning("resolve_all hit missing market (%s); falling back per-position", e)
        return _resolve_per_position(engine)
    except Exception as e:
        log.debug("resolve_all: %s", e)
        return _resolve_per_position(engine)


def scan_once(
    *,
    settings: Settings,
    http: WeatherHttp,
    safe_engine: Engine | None,
    asymmetric_engine: Engine | None = None,
    contrarian_engine: Engine | None = None,
    conviction_engine: Engine | None = None,
    copy_engine: Engine | None = None,
    esports_engine: Engine | None = None,
    momentum_engine: Engine | None = None,
    meanrev_engine: Engine | None = None,
    volspike_engine: Engine | None = None,
    closingsoon_engine: Engine | None = None,
    btc5m_engine: Engine | None = None,
    dry_run: bool,
    today: date | None = None,
    live: LiveTrader | None = None,
    ctx: ExecutionContext | None = None,
) -> tuple[list[Signal], ScanCounts]:
    emitted: list[Signal] = []
    counts = ScanCounts()
    today = today or date.today()
    now = datetime.now(timezone.utc)
    ctx = ctx or ExecutionContext()
    if live is not None and not ctx.balance_checked:
        ctx.wallet_balance = live.client.get_balance()
        ctx.balance_checked = True

    live_engines: list[tuple[str, Engine]] = []
    if safe_engine is not None:
        live_engines.append(("safe", safe_engine))
    if asymmetric_engine is not None:
        live_engines.append(("asymmetric", asymmetric_engine))
    if contrarian_engine is not None:
        live_engines.append(("contrarian", contrarian_engine))
    if conviction_engine is not None:
        live_engines.append(("conviction", conviction_engine))
    if copy_engine is not None:
        live_engines.append(("copy", copy_engine))
    if esports_engine is not None:
        live_engines.append(("esports", esports_engine))
    if momentum_engine is not None:
        live_engines.append(("momentum", momentum_engine))
    if live is not None and live_engines:
        _sync_live_engines(live, live_engines)

    purge_engines = [
        e
        for e in (
            safe_engine,
            asymmetric_engine,
            contrarian_engine,
            conviction_engine,
            copy_engine,
            esports_engine,
            momentum_engine,
            meanrev_engine,
            volspike_engine,
            closingsoon_engine,
            btc5m_engine,
        )
        if e is not None
    ]
    _purge_logs(purge_engines)

    if safe_engine:
        if live is None:
            counts.resolved += _resolve(safe_engine)
        positions = safe_engine.db.get_open_positions()
        for sig in safe_exits(safe_engine, http, settings, positions, settings.cities):
            filled = execute_signal(safe_engine, sig, dry_run, live=live, ctx=ctx, strategy="safe")
            emitted.append(sig)
            if filled:
                counts.orders_placed += 1
                counts.fills += 1
                counts.risk_exits += 1
        cities = [settings.cities[s] for s in settings.safe.cities if s in settings.cities]
        events = discover_events(safe_engine, cities, settings, now=now)
        _log_missing_markets(
            safe_engine, strategy="safe", cities=cities, events=events, settings=settings, now=now
        )
        positions = safe_engine.db.get_open_positions()
        for _slug, event_date, city, buckets, _vol in events:
            counts.candidates += len(buckets)
            sig = analyze_safe_event(
                safe_engine, http, city, event_date, buckets, settings, positions
            )
            if sig:
                filled = execute_signal(safe_engine, sig, dry_run, live=live, ctx=ctx, strategy="safe")
                emitted.append(sig)
                if filled:
                    counts.orders_placed += 1
                    counts.fills += 1
                    positions = safe_engine.db.get_open_positions()

    if asymmetric_engine:
        try:
            if live is None:
                try:
                    asymmetric_engine.check_orders()
                except Exception as e:
                    log.debug("check_orders: %s", e)
            if live is None:
                counts.resolved += _resolve(asymmetric_engine)
            positions = asymmetric_engine.db.get_open_positions()
            for sig in asymmetric_exits(
                asymmetric_engine, http, settings, positions, settings.cities
            ):
                filled = execute_signal(asymmetric_engine, sig, dry_run, live=live, ctx=ctx, strategy="asymmetric")
                emitted.append(sig)
                if filled:
                    counts.orders_placed += 1
                    counts.fills += 1
                    counts.risk_exits += 1
            cities = settings.cities_for("asymmetric")
            if settings.asymmetric.cities:
                cities = [settings.cities[s] for s in settings.asymmetric.cities if s in settings.cities]
            events = discover_events(asymmetric_engine, cities, settings, now=now)
            _log_missing_markets(
                asymmetric_engine,
                strategy="asymmetric",
                cities=cities,
                events=events,
                settings=settings,
                now=now,
            )
            prefetch_combined_ensembles(http, events)
            positions = asymmetric_engine.db.get_open_positions()
            for _slug, event_date, city, buckets, _vol in events:
                counts.candidates += len(buckets)
                sig = analyze_asymmetric_event(
                    asymmetric_engine,
                    http,
                    city,
                    event_date,
                    buckets,
                    settings,
                    positions,
                )
                if sig:
                    filled = execute_signal(asymmetric_engine, sig, dry_run, live=live, ctx=ctx, strategy="asymmetric")
                    emitted.append(sig)
                    if filled:
                        counts.orders_placed += 1
                        counts.fills += 1
                        positions = asymmetric_engine.db.get_open_positions()
        except Exception as e:
            log.exception("asymmetric scan failed: %s", e)
            append_activity(
                asymmetric_engine.db.data_dir,
                level="error",
                event="scan_failed",
                strategy="asymmetric",
                message=str(e),
            )
            log_decision(
                asymmetric_engine.db.data_dir,
                strategy="asymmetric",
                decision="scan_failed",
                reason=str(e),
                level="error",
            )

    if contrarian_engine:
        if live is None:
            try:
                contrarian_engine.check_orders()
            except Exception as e:
                log.debug("check_orders: %s", e)
        if live is None:
            counts.resolved += _resolve(contrarian_engine)
        positions = contrarian_engine.db.get_open_positions()
        for sig in contrarian_exits(
            contrarian_engine, http, settings, positions, settings.cities
        ):
            filled = execute_signal(
                contrarian_engine, sig, dry_run, live=live, ctx=ctx, strategy="contrarian"
            )
            emitted.append(sig)
            if filled:
                counts.orders_placed += 1
                counts.fills += 1
                counts.risk_exits += 1
        cities = settings.cities_for("contrarian")
        if settings.contrarian.cities:
            cities = [
                settings.cities[s] for s in settings.contrarian.cities if s in settings.cities
            ]
        events = discover_events(contrarian_engine, cities, settings, now=now)
        _log_missing_markets(
            contrarian_engine,
            strategy="contrarian",
            cities=cities,
            events=events,
            settings=settings,
            now=now,
        )
        prefetch_combined_ensembles(http, events)
        positions = contrarian_engine.db.get_open_positions()
        for _slug, event_date, city, buckets, _vol in events:
            counts.candidates += len(buckets)
            sigs = analyze_contrarian_event(
                contrarian_engine,
                http,
                city,
                event_date,
                buckets,
                settings,
                positions,
            )
            for sig in sigs:
                filled = execute_signal(
                    contrarian_engine, sig, dry_run, live=live, ctx=ctx, strategy="contrarian"
                )
                emitted.append(sig)
                if filled:
                    counts.orders_placed += 1
                    counts.fills += 1
                    positions = contrarian_engine.db.get_open_positions()

    if conviction_engine:
        if live is None:
            try:
                conviction_engine.check_orders()
            except Exception as e:
                log.debug("check_orders: %s", e)
        if live is None:
            counts.resolved += _resolve(conviction_engine)
        positions = conviction_engine.db.get_open_positions()
        for sig in conviction_exits(
            conviction_engine, http, settings, positions, settings.cities
        ):
            filled = execute_signal(
                conviction_engine, sig, dry_run, live=live, ctx=ctx, strategy="conviction"
            )
            emitted.append(sig)
            if filled:
                counts.orders_placed += 1
                counts.fills += 1
                counts.risk_exits += 1
        cities = settings.cities_for("conviction")
        if settings.conviction.cities:
            cities = [
                settings.cities[s] for s in settings.conviction.cities if s in settings.cities
            ]
        events = discover_events(conviction_engine, cities, settings, now=now)
        _log_missing_markets(
            conviction_engine,
            strategy="conviction",
            cities=cities,
            events=events,
            settings=settings,
            now=now,
        )
        prefetch_combined_ensembles(http, events)
        positions = conviction_engine.db.get_open_positions()
        for _slug, event_date, city, buckets, _vol in events:
            counts.candidates += len(buckets)
            sigs = analyze_conviction_event(
                conviction_engine,
                http,
                city,
                event_date,
                buckets,
                settings,
                positions,
            )
            for sig in sigs:
                filled = execute_signal(
                    conviction_engine, sig, dry_run, live=live, ctx=ctx, strategy="conviction"
                )
                emitted.append(sig)
                if filled:
                    counts.orders_placed += 1
                    counts.fills += 1
                    positions = conviction_engine.db.get_open_positions()

    if copy_engine:
        is_live = live is not None

        def _execute_copy(sig: Signal) -> bool:
            return execute_signal(
                copy_engine, sig, dry_run, live=live, ctx=ctx, strategy="copy"
            )

        considered, copied = sync_copy_trades(
            copy_engine,
            http,
            settings,
            dry_run,
            live=is_live,
            execute=_execute_copy if is_live else None,
        )
        counts.candidates += considered
        counts.orders_placed += len(copied)
        counts.fills += len(copied)
        emitted.extend(copied)

    if esports_engine:
        es_sigs, es_counts = scan_esports_once(
            settings=settings,
            esports_engine=esports_engine,
            dry_run=dry_run,
            live=live,
            ctx=ctx,
        )
        emitted.extend(es_sigs)
        counts.candidates += es_counts.candidates
        counts.orders_placed += es_counts.orders_placed
        counts.fills += es_counts.fills
        counts.resolved += es_counts.resolved
        counts.risk_exits += es_counts.risk_exits

    if momentum_engine:
        momentum_counts = MomentumRunner(
            settings=settings,
            engine=momentum_engine,
            dry_run=dry_run,
            live=live,
            data_dir=None,
        ).poll_once()
        counts.candidates += momentum_counts.candidates
        counts.orders_placed += momentum_counts.orders_placed
        counts.fills += momentum_counts.fills
        counts.resolved += momentum_counts.resolved
        counts.risk_exits += momentum_counts.risk_exits

    if meanrev_engine:
        try:
            from papertrader.strategies.meanrev import analyze_meanrev, meanrev_exits
            if live is None:
                counts.resolved += _resolve(meanrev_engine)
            for sig in meanrev_exits(meanrev_engine, settings):
                if execute_signal(meanrev_engine, sig, dry_run, live=live, ctx=ctx, strategy="meanrev"):
                    counts.risk_exits += 1
                emitted.append(sig)
            for sig in analyze_meanrev(meanrev_engine, settings):
                if execute_signal(meanrev_engine, sig, dry_run, live=live, ctx=ctx, strategy="meanrev"):
                    counts.fills += 1
                counts.orders_placed += 1
                emitted.append(sig)
        except Exception as e:
            log.exception("meanrev scan failed: %s", e)
            append_activity(
                meanrev_engine.db.data_dir,
                level="error",
                event="scan_failed",
                strategy="meanrev",
                message=str(e),
            )

    if volspike_engine:
        try:
            from papertrader.strategies.volspike import analyze_volspike, volspike_exits
            if live is None:
                counts.resolved += _resolve(volspike_engine)
            for sig in volspike_exits(volspike_engine, settings):
                if execute_signal(volspike_engine, sig, dry_run, live=live, ctx=ctx, strategy="volspike"):
                    counts.risk_exits += 1
                emitted.append(sig)
            for sig in analyze_volspike(volspike_engine, settings):
                if execute_signal(volspike_engine, sig, dry_run, live=live, ctx=ctx, strategy="volspike"):
                    counts.fills += 1
                counts.orders_placed += 1
                emitted.append(sig)
        except Exception as e:
            log.exception("volspike scan failed: %s", e)
            append_activity(
                volspike_engine.db.data_dir,
                level="error",
                event="scan_failed",
                strategy="volspike",
                message=str(e),
            )

    if closingsoon_engine:
        try:
            from papertrader.strategies.closingsoon import analyze_closingsoon, closingsoon_exits
            if live is None:
                counts.resolved += _resolve(closingsoon_engine)
            for sig in closingsoon_exits(closingsoon_engine, settings):
                if execute_signal(closingsoon_engine, sig, dry_run, live=live, ctx=ctx, strategy="closingsoon"):
                    counts.risk_exits += 1
                emitted.append(sig)
            for sig in analyze_closingsoon(closingsoon_engine, settings):
                if execute_signal(closingsoon_engine, sig, dry_run, live=live, ctx=ctx, strategy="closingsoon"):
                    counts.fills += 1
                counts.orders_placed += 1
                emitted.append(sig)
        except Exception as e:
            log.exception("closingsoon scan failed: %s", e)
            append_activity(
                closingsoon_engine.db.data_dir,
                level="error",
                event="scan_failed",
                strategy="closingsoon",
                message=str(e),
            )

    if btc5m_engine:
        try:
            from papertrader.strategies.btc5m import analyze_btc5m, btc5m_exits
            if live is None:
                counts.resolved += _resolve(btc5m_engine)
            for sig in btc5m_exits(btc5m_engine, settings):
                if execute_signal(btc5m_engine, sig, dry_run, live=live, ctx=ctx, strategy="btc5m"):
                    counts.risk_exits += 1
                emitted.append(sig)
            for sig in analyze_btc5m(btc5m_engine, settings):
                if execute_signal(btc5m_engine, sig, dry_run, live=live, ctx=ctx, strategy="btc5m"):
                    counts.fills += 1
                counts.orders_placed += 1
                emitted.append(sig)
        except Exception as e:
            log.exception("btc5m scan failed: %s", e)
            append_activity(
                btc5m_engine.db.data_dir,
                level="error",
                event="scan_failed",
                strategy="btc5m",
                message=str(e),
            )

    engines = [
        e
        for e in (
            safe_engine,
            asymmetric_engine,
            contrarian_engine,
            conviction_engine,
            copy_engine,
            esports_engine,
            momentum_engine,
            meanrev_engine,
            volspike_engine,
            closingsoon_engine,
            btc5m_engine,
        )
        if e is not None
    ]
    counts.pending = sum(len(e.db.get_open_positions()) for e in engines)
    counts.fills += counts.resolved
    return emitted, counts


def print_scan_update(
    counts: ScanCounts,
    named_engines: list[tuple[str, Engine]],
    data_dir: Path | None = None,
) -> str:
    text = format_scan_update(counts, combine_engines(named_engines))
    if data_dir is not None:
        append_scan(data_dir, counts, combine_engines(named_engines))
    log.info("\n%s", text)
    print(text, flush=True)
    return text


def run_loop(
    *,
    settings: Settings,
    safe_engine: Engine | None,
    asymmetric_engine: Engine | None = None,
    contrarian_engine: Engine | None = None,
    conviction_engine: Engine | None = None,
    copy_engine: Engine | None = None,
    esports_engine: Engine | None = None,
    momentum_engine: Engine | None = None,
    meanrev_engine: Engine | None = None,
    volspike_engine: Engine | None = None,
    closingsoon_engine: Engine | None = None,
    btc5m_engine: Engine | None = None,
    dry_run: bool,
    once: bool,
    live: LiveTrader | None = None,
    data_dir: Path | None = None,
) -> str:
    http = WeatherHttp(settings.user_agent)
    named_engines: list[tuple[str, Engine]] = []
    if safe_engine is not None:
        named_engines.append(("safe", safe_engine))
    if asymmetric_engine is not None:
        named_engines.append(("asymmetric", asymmetric_engine))
    if contrarian_engine is not None:
        named_engines.append(("contrarian", contrarian_engine))
    if conviction_engine is not None:
        named_engines.append(("conviction", conviction_engine))
    if copy_engine is not None:
        named_engines.append(("copy", copy_engine))
    if esports_engine is not None:
        named_engines.append(("esports", esports_engine))
    if momentum_engine is not None:
        named_engines.append(("momentum", momentum_engine))
    if meanrev_engine is not None:
        named_engines.append(("meanrev", meanrev_engine))
    if volspike_engine is not None:
        named_engines.append(("volspike", volspike_engine))
    if closingsoon_engine is not None:
        named_engines.append(("closingsoon", closingsoon_engine))
    if btc5m_engine is not None:
        named_engines.append(("btc5m", btc5m_engine))
    poll_seconds = (
        settings.copy.poll_interval_seconds
        if copy_engine is not None
        else settings.poll_interval_seconds
    )
    if esports_engine is not None and copy_engine is None:
        poll_seconds = min(poll_seconds, settings.esports.poll_interval_seconds)
    if momentum_engine is not None:
        poll_seconds = min(poll_seconds, settings.momentum.poll_interval_seconds)
    if btc5m_engine is not None:
        # 5m windows need faster scans than weather strategies.
        poll_seconds = min(poll_seconds, 15)
    last = ""
    try:
        ctx = ExecutionContext()
        _, counts = scan_once(
            settings=settings,
            http=http,
            safe_engine=safe_engine,
            asymmetric_engine=asymmetric_engine,
            contrarian_engine=contrarian_engine,
            conviction_engine=conviction_engine,
            copy_engine=copy_engine,
            esports_engine=esports_engine,
            momentum_engine=momentum_engine,
            meanrev_engine=meanrev_engine,
            volspike_engine=volspike_engine,
            closingsoon_engine=closingsoon_engine,
            btc5m_engine=btc5m_engine,
            dry_run=dry_run,
            live=live,
            ctx=ctx,
        )
        last = print_scan_update(counts, named_engines, data_dir=data_dir)
        if once:
            return last

        while True:
            time.sleep(poll_seconds)
            ctx = ExecutionContext()
            _, counts = scan_once(
                settings=settings,
                http=http,
                safe_engine=safe_engine,
                asymmetric_engine=asymmetric_engine,
                contrarian_engine=contrarian_engine,
                conviction_engine=conviction_engine,
                copy_engine=copy_engine,
                esports_engine=esports_engine,
                momentum_engine=momentum_engine,
                meanrev_engine=meanrev_engine,
                volspike_engine=volspike_engine,
                closingsoon_engine=closingsoon_engine,
                btc5m_engine=btc5m_engine,
                dry_run=dry_run,
                live=live,
                ctx=ctx,
            )
            last = print_scan_update(counts, named_engines, data_dir=data_dir)
        return last
    finally:
        http.close()
    return last


def run_copy_loop(
    *,
    settings: Settings,
    copy_engine: Engine,
    dry_run: bool,
    once: bool,
    live: LiveTrader | None = None,
    data_dir: Path | None = None,
) -> str:
    """Tight polling loop for leader copy — targets sub-second detection via data-api."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    http = WeatherHttp(settings.user_agent)
    named_engines = [("copy", copy_engine)]
    poll_ms = max(50, settings.copy.poll_interval_ms)
    is_live = live is not None
    ctx = ExecutionContext()
    if is_live and not ctx.balance_checked:
        ctx.wallet_balance = live.client.get_balance()  # type: ignore[union-attr]
        ctx.balance_checked = True
    last_summary = ""
    heartbeat = time.monotonic()
    try:

        def _execute(sig: Signal) -> bool:
            return execute_signal(
                copy_engine, sig, dry_run, live=live, ctx=ctx, strategy="copy"
            )

        while True:
            loop_started = time.perf_counter()
            if is_live and live is not None:
                _sync_live_engines(live, [("copy", copy_engine)])
            _, copied = sync_copy_trades(
                copy_engine,
                http,
                settings,
                dry_run,
                live=is_live,
                execute=_execute if is_live else None,
            )
            if copied:
                counts = ScanCounts(orders_placed=len(copied), fills=len(copied))
                last_summary = print_scan_update(counts, named_engines, data_dir=data_dir)
                heartbeat = time.monotonic()
            elif time.monotonic() - heartbeat >= 60:
                _purge_logs([copy_engine])
                counts = ScanCounts()
                last_summary = print_scan_update(counts, named_engines, data_dir=data_dir)
                heartbeat = time.monotonic()
            if once:
                return last_summary
            elapsed_ms = (time.perf_counter() - loop_started) * 1000
            sleep_ms = max(0.0, poll_ms - elapsed_ms)
            if sleep_ms:
                time.sleep(sleep_ms / 1000)
    finally:
        http.close()
    return last_summary


def _log_esports_scan(
    engine: Engine,
    *,
    stats,
    orders_placed: int,
    pending: int,
    fair_matches: int | None = None,
    oddsp_quota: dict[str, int] | None = None,
) -> None:
    skip_summary = format_skip_summary(dict(stats.rejects)) if stats.rejects else ""
    reason = (
        f"esports scan: {stats.candidates} buyable / {stats.match_markets} match markets / "
        f"{stats.events_in_horizon} live events / {stats.events_seen} scanned"
    )
    if fair_matches is not None:
        reason += f" / {fair_matches} oddspapi fair"
    if oddsp_quota:
        reason += (
            f" / oddsp quota {oddsp_quota.get('daily_used', 0)}d"
            f" {oddsp_quota.get('monthly_used', 0)}m"
        )
    log_decision(
        engine.db.data_dir,
        strategy="esports",
        decision="scan",
        reason=reason,
        candidates=stats.candidates,
        match_markets=stats.match_markets,
        events_in_horizon=stats.events_in_horizon,
        events_seen=stats.events_seen,
        orders_placed=orders_placed,
        pending_positions=pending,
        skip_summary=skip_summary or None,
        notable_buckets=stats.notable or None,
    )
    append_activity(
        engine.db.data_dir,
        level="info",
        event="esports_scan",
        strategy="esports",
        message=reason if not skip_summary else f"{reason} — {skip_summary}",
        candidates=stats.candidates,
        match_markets=stats.match_markets,
        events_in_horizon=stats.events_in_horizon,
        events_seen=stats.events_seen,
        orders_placed=orders_placed,
        pending_positions=pending,
        skip_summary=skip_summary or None,
        notable_buckets=stats.notable or None,
    )
    log.info("Esports scan — %s%s", stats.summary(), f" | {skip_summary}" if skip_summary else "")


def _esports_exit_pass(
    esports_engine: Engine,
    settings: Settings,
    *,
    dry_run: bool,
    live: LiveTrader | None,
    ctx: ExecutionContext,
    emitted: list[Signal],
    counts: ScanCounts,
) -> None:
    positions = esports_engine.db.get_open_positions()
    for sig in esports_exits(esports_engine, settings, positions):
        filled = execute_signal(
            esports_engine, sig, dry_run, live=live, ctx=ctx, strategy="esports"
        )
        emitted.append(sig)
        if filled:
            counts.orders_placed += 1
            counts.fills += 1
            counts.risk_exits += 1


def scan_esports_once(
    *,
    settings: Settings,
    esports_engine: Engine,
    dry_run: bool,
    live: LiveTrader | None = None,
    ctx: ExecutionContext | None = None,
) -> tuple[list[Signal], ScanCounts]:
    emitted: list[Signal] = []
    counts = ScanCounts()
    now = datetime.now(timezone.utc)
    ctx = ctx or ExecutionContext()
    if live is not None and not ctx.balance_checked:
        ctx.wallet_balance = live.client.get_balance()
        ctx.balance_checked = True
    if live is not None:
        _sync_live_engines(live, [("esports", esports_engine)])
    _purge_logs([esports_engine])
    if live is None:
        try:
            esports_engine.check_orders()
        except Exception as e:
            log.debug("check_orders: %s", e)
        counts.resolved += _resolve(esports_engine)
    _esports_exit_pass(
        esports_engine, settings, dry_run=dry_run, live=live, ctx=ctx, emitted=emitted, counts=counts
    )
    discovery = discover_esports_markets(esports_engine, settings, now=now)
    candidates = discovery.candidates
    stats = discovery.stats
    counts.candidates = stats.candidates
    fair_matches = None
    oddsp_quota: dict[str, int] | None = None
    oddsp = settings.esports.oddspapi
    if oddsp.enabled and oddspapi_api_key():
        oddsp_service = OddsPapiService(esports_engine.db.data_dir, oddsp)
        cache = oddsp_service.refresh_if_needed()
        fair_matches = cache.matches if cache else []
        oddsp_quota = oddsp_service.quota_snapshot()
    positions = esports_engine.db.get_open_positions()
    for candidate in candidates:
        sig = analyze_esports_candidate(
            esports_engine,
            candidate,
            settings,
            positions,
            fair_matches=fair_matches,
        )
        if sig is None:
            continue
        filled = execute_signal(
            esports_engine, sig, dry_run, live=live, ctx=ctx, strategy="esports"
        )
        emitted.append(sig)
        if filled:
            counts.orders_placed += 1
            counts.fills += 1
            positions = esports_engine.db.get_open_positions()
    if live is None:
        try:
            esports_engine.check_orders()
        except Exception as e:
            log.debug("check_orders: %s", e)
        _esports_exit_pass(
            esports_engine,
            settings,
            dry_run=dry_run,
            live=live,
            ctx=ctx,
            emitted=emitted,
            counts=counts,
        )
    counts.pending = len(esports_engine.db.get_open_positions())
    counts.fills += counts.resolved
    _log_esports_scan(
        esports_engine,
        stats=stats,
        orders_placed=counts.orders_placed,
        pending=counts.pending,
        fair_matches=len(fair_matches) if fair_matches is not None else None,
        oddsp_quota=oddsp_quota,
    )
    return emitted, counts


def run_esports_loop(
    *,
    settings: Settings,
    esports_engine: Engine,
    dry_run: bool,
    once: bool,
    live: LiveTrader | None = None,
    data_dir: Path | None = None,
) -> str:
    """Poll esports match markets ending soon; buy cheap, TP +20%, SL at 80% of entry."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    named_engines = [("esports", esports_engine)]
    poll_seconds = max(15, settings.esports.poll_interval_seconds)
    last = ""
    try:
        ctx = ExecutionContext()
        _, counts = scan_esports_once(
            settings=settings,
            esports_engine=esports_engine,
            dry_run=dry_run,
            live=live,
            ctx=ctx,
        )
        last = print_scan_update(counts, named_engines, data_dir=data_dir)
        if once:
            return last
        while True:
            time.sleep(poll_seconds)
            ctx = ExecutionContext()
            _, counts = scan_esports_once(
                settings=settings,
                esports_engine=esports_engine,
                dry_run=dry_run,
                live=live,
                ctx=ctx,
            )
            last = print_scan_update(counts, named_engines, data_dir=data_dir)
    finally:
        pass
    return last


class MomentumRunner:
    """Stream-driven weather momentum trader with HTTP poll fallback."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine: Engine,
        dry_run: bool,
        live: LiveTrader | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.dry_run = dry_run
        self.live = live
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._executing = False
        self._watch_by_token: dict[str, TokenWatch] = {}
        self._exit_store = MomentumExitStore(engine.db.data_dir)
        self._ctx = ExecutionContext()
        self._counts = ScanCounts()
        self._refresh_universe()

    def _refresh_universe(self) -> None:
        watches = build_token_watches(self.engine, self.settings)
        self._watch_by_token = {w.token_id: w for w in watches}
        log.info(
            "momentum universe: %d buckets across %d tokens",
            len(watches),
            len(self._watch_by_token),
        )

    def _handle_tick(self, tick: MarketTick) -> None:
        watch = self._watch_by_token.get(tick.token_id)
        if watch is None:
            return
        with self._lock:
            if self._executing:
                return
            self._executing = True
        try:
            self._process_tick(watch, tick)
        finally:
            with self._lock:
                self._executing = False

    def _process_tick(self, watch: TokenWatch, tick: MarketTick) -> None:
        if self.live is not None:
            _sync_live_engines(self.live, [("momentum", self.engine)])
        positions = self.engine.db.get_open_positions()
        self._exit_store.prune_closed(positions)

        for sig in momentum_exits(
            self.engine,
            watch,
            tick,
            self.settings,
            positions,
            exit_store=self._exit_store,
        ):
            filled = execute_signal(
                self.engine,
                sig,
                self.dry_run,
                live=self.live,
                ctx=self._ctx,
                strategy="momentum",
            )
            if filled:
                self._counts.orders_placed += 1
                self._counts.fills += 1
                self._counts.risk_exits += 1
            positions = self.engine.db.get_open_positions()

        sig = analyze_momentum_entry(
            self.engine, watch, tick, self.settings, positions
        )
        if sig is None:
            return
        filled = execute_signal(
            self.engine,
            sig,
            self.dry_run,
            live=self.live,
            ctx=self._ctx,
            strategy="momentum",
        )
        if filled:
            self._counts.orders_placed += 1
            self._counts.fills += 1

    def poll_once(self) -> ScanCounts:
        self._counts = ScanCounts()
        if self.live is not None:
            _sync_live_engines(self.live, [("momentum", self.engine)])
        _purge_logs([self.engine])
        if self.live is None:
            try:
                self.engine.check_orders()
            except Exception as e:
                log.debug("check_orders: %s", e)
            self._counts.resolved += _resolve(self.engine)

        self._refresh_universe()
        for token_id, watch in list(self._watch_by_token.items()):
            try:
                book = self.engine.api.get_order_book(token_id)
            except Exception:
                continue
            self._process_tick(watch, tick_from_order_book(token_id, book))

        self._counts.pending = len(self.engine.db.get_open_positions())
        self._log_scan()
        return self._counts

    def _log_scan(self) -> None:
        n_tokens = len(self._watch_by_token)
        reason = (
            f"momentum scan: {n_tokens} tokens watched / "
            f"{self._counts.orders_placed} orders / {self._counts.pending} open"
        )
        log_decision(
            self.engine.db.data_dir,
            strategy="momentum",
            decision="scan",
            reason=reason,
            tokens_watched=n_tokens,
            orders_placed=self._counts.orders_placed,
            pending_positions=self._counts.pending,
        )
        append_activity(
            self.engine.db.data_dir,
            level="info",
            event="momentum_scan",
            strategy="momentum",
            message=reason,
            tokens_watched=n_tokens,
            orders_placed=self._counts.orders_placed,
            pending_positions=self._counts.pending,
        )
        log.info("Momentum — %s", reason)

    def run_poll_loop(self, *, once: bool) -> str:
        poll_seconds = max(2, self.settings.momentum.poll_interval_seconds)
        named = [("momentum", self.engine)]
        last = print_scan_update(self.poll_once(), named, data_dir=self.data_dir)
        if once:
            return last
        refresh_at = time.monotonic()
        while True:
            time.sleep(poll_seconds)
            if time.monotonic() - refresh_at >= 300:
                self._refresh_universe()
                refresh_at = time.monotonic()
            last = print_scan_update(self.poll_once(), named, data_dir=self.data_dir)
        return last

    def run_ws_loop(self) -> str:
        named = [("momentum", self.engine)]
        scan_counts = ScanCounts()
        last = ""

        def on_tick(tick: MarketTick) -> None:
            nonlocal last
            before = self._counts.orders_placed
            self._handle_tick(tick)
            if self._counts.orders_placed != before:
                scan_counts.orders_placed = self._counts.orders_placed
                scan_counts.fills = self._counts.fills
                scan_counts.pending = len(self.engine.db.get_open_positions())
                last = print_scan_update(scan_counts, named, data_dir=self.data_dir)

        async def _main() -> None:
            refresh_at = time.monotonic()
            while True:
                if time.monotonic() - refresh_at >= 300:
                    self._refresh_universe()
                    refresh_at = time.monotonic()
                await run_market_websocket(
                    self._watch_by_token.keys(),
                    on_tick,
                    ws_url=self.settings.momentum.ws_url,
                )

        try:
            asyncio.run(_main())
        except KeyboardInterrupt:
            pass
        return last

    def run(self, *, once: bool) -> str:
        cfg = self.settings.momentum
        if cfg.use_websocket and not once:
            try:
                return self.run_ws_loop()
            except RuntimeError as exc:
                log.warning("momentum WS unavailable (%s) — falling back to HTTP poll", exc)
        return self.run_poll_loop(once=once)


def run_momentum_loop(
    *,
    settings: Settings,
    momentum_engine: Engine,
    dry_run: bool,
    once: bool,
    live: LiveTrader | None = None,
    data_dir: Path | None = None,
) -> str:
    """Weather momentum: stream buckets and trade 85¢ entries with TP/SL."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    runner = MomentumRunner(
        settings=settings,
        engine=momentum_engine,
        dry_run=dry_run,
        live=live,
        data_dir=data_dir,
    )
    return runner.run(once=once)
