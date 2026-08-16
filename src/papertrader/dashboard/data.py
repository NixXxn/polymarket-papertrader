from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pm_trader.engine import Engine

from papertrader.accounts import data_dir_from_env, make_engine
from papertrader.config import load_settings
from papertrader.markets import polymarket_event_url
from papertrader.mode import resolve_mode
from papertrader.paths import DEFAULT_LIVE_DATA_DIR, data_dir_from_env
from papertrader.report import account_stats, combine_engines, mark_positions
from papertrader.scan_history import load_scan_history


STRATEGIES = ("safe", "asymmetric", "copy")


def _engine_exists(data_dir: Path, name: str) -> bool:
    return (data_dir / name / "paper.db").is_file()


def open_engines(data_dir: Path, starting_balance: float) -> list[tuple[str, Engine]]:
    engines: list[tuple[str, Engine]] = []
    for name in STRATEGIES:
        if not _engine_exists(data_dir, name):
            continue
        engines.append((name, make_engine(name, data_dir, starting_balance)))
    return engines


def _trade_row(strategy: str, trade: Any) -> dict[str, Any]:
    slug = trade.market_slug
    return {
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
    points: list[tuple[str, float]] = []
    for _name, engine in engines:
        account = engine.get_account()
        trades = list(reversed(engine.db.get_trades(limit=10_000)))
        cumulative = account.starting_balance
        for t in trades:
            if t.side == "buy":
                cumulative -= t.amount_usd + t.fee
            else:
                cumulative += t.amount_usd - t.fee
            ts = t.created_at[:19] if t.created_at else ""
            points.append((ts, cumulative))
    points.sort(key=lambda p: p[0])
    return [{"ts": ts, "value": val} for ts, val in points[-200:]]


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
    }


def fetch_dashboard(
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
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
    engines = open_engines(resolved.data_dir, settings.starting_balance)
    try:
        if not engines:
            return {
                "ok": True,
                "mode": resolved.mode,
                "data_dir": str(resolved.data_dir),
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
            }

        combined = combine_engines(engines)
        trades: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        for name, engine in engines:
            for t in engine.db.get_trades(limit=500):
                trades.append(_trade_row(name, t))
            for p in engine.db.get_open_positions():
                if p.shares > 0 and not p.is_resolved:
                    positions.append(_position_row(name, engine, p))

        trades.sort(key=lambda r: (r.get("created_at") or "", r.get("id") or 0), reverse=True)

        portfolio = {
            "cash": combined.cash,
            "positions": combined.positions,
            "total": combined.total,
            "pnl": combined.pnl,
            "roi_pct": combined.roi_pct,
            "trades": combined.trades,
            "buys": combined.buys,
            "sells": combined.sells,
            "win_rate": combined.win_rate,
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
                    "cash": raw["cash"],
                    "positions_value": raw["positions_value"],
                    "total_value": raw["total_value"],
                    "starting_balance": raw["starting_balance"],
                    "pnl": raw["pnl"],
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
            "poll_interval_seconds": settings.poll_interval_seconds,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "portfolio": portfolio,
            "positions": positions,
            "trades": trades[:500],
            "equity_curve": _equity_curve(engines),
            "scan_history": load_scan_history(resolved.data_dir),
            "copy": _copy_meta(resolved.data_dir, settings),
        }
    finally:
        for _name, engine in engines:
            engine.close()
