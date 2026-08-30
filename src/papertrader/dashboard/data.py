from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pm_trader.engine import Engine

from papertrader.accounts import STRATEGY_NAMES, make_engine, reset_all_strategies
from papertrader.config import ROOT, load_settings
from papertrader.markets import polymarket_event_url
from papertrader.mode import ResolvedMode, load_dotenv_file, resolve_mode
from papertrader.paths import DEFAULT_LIVE_DATA_DIR, data_dir_from_env
from papertrader.report import (
    account_stats,
    combine_engines,
    mark_positions,
    realized_pnl_total,
    _sell_realized_pnl,
)
from papertrader.live_sync import load_live_open_orders, load_live_sync_meta
from papertrader.scan_history import load_scan_history
from papertrader.decision_log import load_decisions
from papertrader.trade_log import (
    build_activity_feed,
    copy_latency_by_trade_id,
    copy_latency_stats,
    load_skipped_trades,
)


STRATEGIES = ("safe", "asymmetric", "contrarian", "conviction", "copy", "esports", "momentum", "meanrev", "volspike", "closingsoon", "btc5m")

_RESET_STATS_FILES = (
    "activity.jsonl",
    "decisions.jsonl",
    "skipped_trades.jsonl",
    "shadow_ledger.jsonl",
    "scan_history.jsonl",
    "oddspapi_cache.json",
    "oddspapi_quota.json",
    "predictionhunt_quota.json",
    "predictionhunt_cache.json",
    "live_sync_state.json",
    "esports_exit_state.json",
    "momentum_exit_state.json",
    "copy/copy_events.jsonl",
    "copy/copied_trades.json",
)


def _resolve_dashboard(
    data_dir: Path | None = None,
    mode: str | None = None,
) -> tuple[Any, ResolvedMode]:
    load_dotenv_file(ROOT / ".env")
    settings = load_settings()
    resolved = resolve_mode(
        settings_mode=settings.mode,
        cli_mode=mode,
        confirm_live=False,
        data_dir=data_dir or data_dir_from_env(),
        clob_host=settings.live.clob_host,
        chain_id=settings.live.chain_id,
        signature_type=settings.live.signature_type,
        funder=settings.live.funder,
        require_credentials=False,
    )
    return settings, resolved


def reset_strategy_budgets(
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
    balance: float,
) -> dict[str, Any]:
    """Wipe all strategy ledgers and set cash to ``balance``."""
    if balance <= 0:
        raise ValueError("balance must be positive")
    _settings, resolved = _resolve_dashboard(data_dir, mode)
    results = reset_all_strategies(resolved.data_dir, balance)
    return {
        "ok": True,
        "mode": resolved.mode,
        "data_dir": str(resolved.data_dir),
        "balance": balance,
        "is_live": resolved.is_live,
        "strategies": [
            {"name": name, "cash": cash, "starting_balance": starting}
            for name, cash, starting in results
        ],
    }


def set_strategy_budget(
    *,
    strategy: str,
    balance: float,
    data_dir: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Reset one strategy ledger and set its cash/start balance."""
    if strategy not in STRATEGY_NAMES:
        raise ValueError(f"unknown strategy: {strategy}")
    if balance <= 0:
        raise ValueError("balance must be positive")
    settings, resolved = _resolve_dashboard(data_dir, mode)
    if strategy == "safe":
        settings_balance = float(getattr(settings.safe, "starting_balance", 0) or settings.starting_balance)
    else:
        settings_balance = settings.starting_balance
    # Always reset the selected strategy ledger to make the new budget effective immediately.
    engine = make_engine(strategy, resolved.data_dir, balance, reset=True)
    try:
        acct = engine.get_account()
    finally:
        engine.close()
    return {
        "ok": True,
        "mode": resolved.mode,
        "data_dir": str(resolved.data_dir),
        "strategy": strategy,
        "balance": float(balance),
        "is_live": resolved.is_live,
        "account": {
            "name": strategy,
            "cash": acct.cash,
            "starting_balance": acct.starting_balance,
            "default_balance": settings_balance,
        },
    }


def reset_all_statistics(
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
    balance: float | None = None,
) -> dict[str, Any]:
    """Reset all ledgers and delete dashboard/log/statistics artifacts."""
    settings, resolved = _resolve_dashboard(data_dir, mode)
    amount = float(balance if balance is not None else settings.starting_balance)
    if amount <= 0:
        raise ValueError("balance must be positive")
    results = reset_all_strategies(resolved.data_dir, amount)
    deleted: list[str] = []
    root = resolved.data_dir
    for rel in _RESET_STATS_FILES:
        p = root / rel
        if p.is_file():
            p.unlink()
            deleted.append(rel)
    return {
        "ok": True,
        "mode": resolved.mode,
        "data_dir": str(resolved.data_dir),
        "balance": amount,
        "is_live": resolved.is_live,
        "deleted_files": deleted,
        "strategies": [
            {"name": name, "cash": cash, "starting_balance": starting}
            for name, cash, starting in results
        ],
    }


def _engine_exists(data_dir: Path, name: str) -> bool:
    return (data_dir / name / "paper.db").is_file()


def open_engines(data_dir: Path, settings: Any) -> list[tuple[str, Engine]]:
    """Open every configured strategy ledger (creates paper account on first view)."""
    pairs: list[tuple[str, Engine]] = []
    for name in STRATEGIES:
        if name == "safe":
            balance = float(getattr(settings.safe, "starting_balance", 0) or settings.starting_balance)
        else:
            block = getattr(settings, name, None)
            sb = getattr(block, "starting_balance", None) if block is not None else None
            balance = float(sb) if sb else settings.starting_balance
        pairs.append((name, make_engine(name, data_dir, balance)))
    return pairs


def _trade_row(
    strategy: str,
    trade: Any,
    *,
    realized_pnl: float | None = None,
    copy_latency_ms: float | None = None,
) -> dict[str, Any]:
    slug = trade.market_slug
    row = {
        "id": trade.id,
        "strategy": strategy,
        "side": trade.side,
        "market_slug": slug,
        "market_question": trade.market_question,
        "outcome": trade.outcome,
        "avg_price": trade.avg_price,
        "amount_usd": trade.amount_usd,
        "shares": trade.shares,
        "fee": trade.fee,
        "order_type": trade.order_type,
        "created_at": trade.created_at,
        "url": polymarket_event_url(slug),
    }
    if trade.side == "sell" and realized_pnl is not None:
        row["realized_pnl"] = realized_pnl
    if copy_latency_ms is not None:
        row["copy_latency_ms"] = copy_latency_ms
    return row


def _position_row(strategy: str, engine: Engine, pos: Any) -> dict[str, Any]:
    live_price = pos.avg_entry_price
    try:
        market = engine.api.get_market(pos.market_slug)
        token = market.get_token_id(pos.outcome)
        live_price = float(engine.api.get_midpoint(token))
    except Exception:
        pass
    current_value = pos.shares * live_price
    unrealized = current_value - pos.total_cost
    pct = (unrealized / pos.total_cost * 100) if pos.total_cost else 0.0
    return {
        "strategy": strategy,
        "market_slug": pos.market_slug,
        "market_question": pos.market_question,
        "outcome": pos.outcome,
        "shares": pos.shares,
        "avg_entry_price": pos.avg_entry_price,
        "total_cost": pos.total_cost,
        "live_price": live_price,
        "current_value": current_value,
        "unrealized_pnl": unrealized,
        "percent_pnl": pct,
        "url": polymarket_event_url(pos.market_slug),
    }


def _equity_curve(engines: list[tuple[str, Engine]]) -> list[dict[str, Any]]:
    """Cumulative realized P&L over time (only closed sells count)."""
    events: list[tuple[str, float]] = []
    for _name, engine in engines:
        trades = list(reversed(engine.db.get_trades(limit=10_000)))
        pnl_map = _sell_realized_pnl(trades)
        for t in trades:
            if t.side != "sell":
                continue
            pnl = pnl_map.get(t.id)
            if pnl is None:
                continue
            ts = t.created_at[:19] if t.created_at else ""
            events.append((ts, pnl))
    events.sort(key=lambda row: row[0])
    cumulative = 0.0
    points: list[dict[str, Any]] = []
    for ts, pnl in events:
        cumulative += pnl
        points.append({"ts": ts, "value": cumulative})
    return points[-200:]


def _copy_meta(data_dir: Path, settings: Any) -> dict[str, Any]:
    path = data_dir / "copy" / "copied_trades.json"
    seen = 0
    scale = None
    if path.is_file():
        try:
            raw = json.loads(path.read_text())
            seen = len(raw.get("seen") or [])
            scale = raw.get("scale")
        except json.JSONDecodeError:
            pass
    username = getattr(settings.copy, "username", "") if settings.copy else ""
    wallet = getattr(settings.copy, "wallet", "") if settings.copy else ""
    return {
        "username": username,
        "wallet": wallet,
        "seen_trades": seen,
        "scale": scale,
        "active": _engine_exists(data_dir, "copy"),
        "latency": copy_latency_stats(data_dir),
    }


def fetch_dashboard(
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    settings, resolved = _resolve_dashboard(data_dir, mode)
    engines = open_engines(resolved.data_dir, settings)
    try:
        if not engines:
            return {
                "ok": True,
                "mode": resolved.mode,
                "data_dir": str(resolved.data_dir),
                "starting_balance": settings.starting_balance,
                "is_live": resolved.is_live,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "portfolio": {
                    "cash": 0,
                    "positions": 0,
                    "total": 0,
                    "pnl": 0,
                    "roi_pct": 0,
                    "trades": 0,
                    "buys": 0,
                    "sells": 0,
                    "win_rate": 0,
                    "max_drawdown": 0,
                    "fees": 0,
                    "avg_trade": 0,
                    "by_strategy": [],
                },
                "positions": [],
                "trades": [],
                "equity_curve": [],
                "scan_history": load_scan_history(resolved.data_dir),
                "copy": _copy_meta(resolved.data_dir, settings),
                "skipped_trades": load_skipped_trades(resolved.data_dir),
                "activity_log": build_activity_feed(resolved.data_dir),
                "decisions": load_decisions(resolved.data_dir),
                "live_open_orders": [],
                "live_sync": load_live_sync_meta(resolved.data_dir),
            }

        combined = combine_engines(engines)
        copy_latencies = copy_latency_by_trade_id(resolved.data_dir)
        trades: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        for name, engine in engines:
            raw_trades = list(engine.db.get_trades(limit=500))
            pnl_map = _sell_realized_pnl(list(reversed(raw_trades)))
            for t in raw_trades:
                lat = copy_latencies.get(t.id) if name == "copy" else None
                trades.append(
                    _trade_row(
                        name,
                        t,
                        realized_pnl=pnl_map.get(t.id),
                        copy_latency_ms=lat,
                    )
                )
            for p in engine.db.get_open_positions():
                if p.shares > 0 and not p.is_resolved:
                    positions.append(_position_row(name, engine, p))

        trades.sort(key=lambda r: (r.get("created_at") or "", r.get("id") or 0), reverse=True)

        portfolio = {
            "cash": combined.cash,
            "positions": combined.positions,
            "total": combined.total,
            "pnl": combined.pnl,
            "realized_pnl": combined.pnl,
            "unrealized_pnl": combined.unrealized_pnl,
            "roi_pct": combined.roi_pct,
            "trades": combined.trades,
            "buys": combined.buys,
            "sells": combined.sells,
            "win_rate": combined.win_rate,
            "win_rate_pct": combined.win_rate * 100,
            "max_drawdown": combined.max_drawdown,
            "fees": combined.fees,
            "avg_trade": combined.avg_trade,
            "by_strategy": [],
        }
        engine_by_name = {name: engine for name, engine in engines}
        for s in combined.by_strategy:
            raw = account_stats(engine_by_name[s.name])
            portfolio["by_strategy"].append(
                {
                    "name": s.name,
                    "trades": s.trades,
                    "buys": s.buys,
                    "sells": s.sells,
                    "win_rate": s.win_rate,
                    "win_rate_pct": s.win_rate * 100,
                    "cash": raw["cash"],
                    "positions_value": raw["positions_value"],
                    "total_value": raw["total_value"],
                    "starting_balance": raw["starting_balance"],
                    "pnl": raw["pnl"],
                    "realized_pnl": raw.get("realized_pnl", raw["pnl"]),
                    "unrealized_pnl": raw.get("unrealized_pnl", 0.0),
                    "roi_pct": raw["roi_pct"],
                    "sharpe_ratio": raw["sharpe_ratio"],
                    "max_drawdown": raw["max_drawdown"],
                    "total_fees": raw["total_fees"],
                }
            )

        return {
            "ok": True,
            "mode": resolved.mode,
            "data_dir": str(resolved.data_dir),
            "is_live_ledger": resolved.data_dir == DEFAULT_LIVE_DATA_DIR,
            "is_live": resolved.is_live,
            "starting_balance": settings.starting_balance,
            "poll_interval_seconds": settings.poll_interval_seconds,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "portfolio": portfolio,
            "positions": positions,
            "trades": trades[:500],
            "equity_curve": _equity_curve(engines),
            "scan_history": load_scan_history(resolved.data_dir),
            "copy": _copy_meta(resolved.data_dir, settings),
            "skipped_trades": load_skipped_trades(resolved.data_dir),
            "activity_log": build_activity_feed(resolved.data_dir),
            "decisions": load_decisions(resolved.data_dir),
            "live_open_orders": load_live_open_orders(resolved.data_dir),
            "live_sync": load_live_sync_meta(resolved.data_dir),
        }
    finally:
        for _name, engine in engines:
            engine.close()
