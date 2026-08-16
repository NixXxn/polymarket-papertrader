from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

from pm_trader.engine import Engine
from pm_trader.models import NoPositionError, OrderRejectedError, SimError

from papertrader.config import Settings
from papertrader.execution import ExecutionContext, log_fill_latency
from papertrader.live import LiveTrader
from papertrader.markets import discover_events
from papertrader.report import ScanCounts, combine_engines, format_scan_update
from papertrader.scan_history import append_scan
from papertrader.signals import Signal
from papertrader.strategies.asymmetric import analyze_asymmetric_event, asymmetric_exits
from papertrader.strategies.safe import analyze_safe_event, safe_exits
from papertrader.weather import WeatherHttp

log = logging.getLogger("papertrader")


def execute_signal(
    engine: Engine,
    signal: Signal,
    dry_run: bool,
    live: LiveTrader | None = None,
    ctx: ExecutionContext | None = None,
) -> bool:
    if dry_run:
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
            filled = live.fill(engine, signal, ctx=ctx)
            log_fill_latency(f"LIVE {signal.action.upper()} {signal.slug}", started)
            return filled
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
        return True
    except (OrderRejectedError, NoPositionError, SimError) as e:
        log.warning("Order skipped: %s (%s)", e, signal.reason)
        return False


def _resolve(engine: Engine) -> int:
    try:
        results = engine.resolve_all()
        for r in results:
            log.info("Resolved %s payout=%.2f", r.position.market_slug, r.payout)
        return len(results)
    except Exception as e:
        log.debug("resolve_all: %s", e)
        return 0


def scan_once(
    *,
    settings: Settings,
    http: WeatherHttp,
    safe_engine: Engine | None,
    asymmetric_engine: Engine | None = None,
    dry_run: bool,
    today: date | None = None,
    live: LiveTrader | None = None,
    ctx: ExecutionContext | None = None,
) -> tuple[list[Signal], ScanCounts]:
    emitted: list[Signal] = []
    counts = ScanCounts()
    today = today or date.today()
    ctx = ctx or ExecutionContext()
    if live is not None and not ctx.balance_checked:
        ctx.wallet_balance = live.client.get_balance()
        ctx.balance_checked = True

    if safe_engine:
        if live is None:
            counts.resolved += _resolve(safe_engine)
        positions = safe_engine.db.get_open_positions()
        for sig in safe_exits(safe_engine, http, settings, positions, settings.cities):
            filled = execute_signal(safe_engine, sig, dry_run, live=live, ctx=ctx)
            emitted.append(sig)
            if filled:
                counts.orders_placed += 1
                counts.fills += 1
                counts.risk_exits += 1
        cities = [settings.cities[s] for s in settings.safe.cities if s in settings.cities]
        events = discover_events(safe_engine, cities, settings, today)
        positions = safe_engine.db.get_open_positions()
        for _slug, event_date, city, buckets, _vol in events:
            counts.candidates += len(buckets)
            sig = analyze_safe_event(
                safe_engine, http, city, event_date, buckets, settings, positions
            )
            if sig:
                filled = execute_signal(safe_engine, sig, dry_run, live=live, ctx=ctx)
                emitted.append(sig)
                if filled:
                    counts.orders_placed += 1
                    counts.fills += 1
                    positions = safe_engine.db.get_open_positions()

    if asymmetric_engine:
        if live is None:
            counts.resolved += _resolve(asymmetric_engine)
        positions = asymmetric_engine.db.get_open_positions()
        for sig in asymmetric_exits(
            asymmetric_engine, http, settings, positions, settings.cities
        ):
            filled = execute_signal(asymmetric_engine, sig, dry_run, live=live, ctx=ctx)
            emitted.append(sig)
            if filled:
                counts.orders_placed += 1
                counts.fills += 1
                counts.risk_exits += 1
        cities = settings.cities_for("asymmetric")
        if settings.asymmetric.cities:
            cities = [settings.cities[s] for s in settings.asymmetric.cities if s in settings.cities]
        events = discover_events(asymmetric_engine, cities, settings, today)
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
                today,
            )
            if sig:
                filled = execute_signal(asymmetric_engine, sig, dry_run, live=live, ctx=ctx)
                emitted.append(sig)
                if filled:
                    counts.orders_placed += 1
                    counts.fills += 1
                    positions = asymmetric_engine.db.get_open_positions()

    engines = [e for e in (safe_engine, asymmetric_engine) if e is not None]
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
    last = ""
    try:
        _, counts = scan_once(
            settings=settings,
            http=http,
            safe_engine=safe_engine,
            asymmetric_engine=asymmetric_engine,
            dry_run=dry_run,
            live=live,
            ctx=ExecutionContext(),
        )
        last = print_scan_update(counts, named_engines, data_dir=data_dir)
        if once:
            return last
        import time

        while True:
            time.sleep(settings.poll_interval_seconds)
            _, counts = scan_once(
                settings=settings,
                http=http,
                safe_engine=safe_engine,
                asymmetric_engine=asymmetric_engine,
                dry_run=dry_run,
                live=live,
                ctx=ExecutionContext(),
            )
            last = print_scan_update(counts, named_engines, data_dir=data_dir)
        return last
    finally:
        http.close()
    return last
