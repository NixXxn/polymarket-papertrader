from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from pm_trader.engine import Engine

from papertrader.execution import ExecutionContext
from papertrader.live import LiveTrader
from papertrader.signals import Signal
from papertrader.trade_log import append_activity

log = logging.getLogger("papertrader")

STRATEGY_NAME = "counter-trade"
SOURCE_STRATEGIES = ("asymmetric", "weatherlock")


def opposite_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized == "yes":
        return "no"
    if normalized == "no":
        return "yes"
    raise ValueError(f"counter-trade only supports YES/NO outcomes, got {outcome!r}")


def _cost_for_shares(book: object, target_shares: float) -> tuple[float, float]:
    """Return (USD cost, shares available) while walking asks cheapest-first."""
    remaining = max(0.0, float(target_shares))
    cost = 0.0
    bought = 0.0
    asks = sorted(getattr(book, "asks", None) or (), key=lambda level: float(level.price))
    for level in asks:
        price = float(level.price)
        size = float(level.size)
        if price <= 0 or size <= 0:
            continue
        take = min(size, remaining)
        cost += take * price
        bought += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return cost, bought


class CounterTradeManager:
    """Mirrors newly recorded BUY fills from the two source strategies.

    Cursors are persisted so restarts do not duplicate counter trades. On the
    very first start, existing history is treated as the baseline; only later
    fills are mirrored.
    """

    def __init__(
        self,
        counter_engine: Engine,
        source_engines: dict[str, Engine],
    ) -> None:
        self.counter_engine = counter_engine
        self.source_engines = {
            name: engine
            for name, engine in source_engines.items()
            if name in SOURCE_STRATEGIES
        }
        self.state_path = Path(counter_engine.db.data_dir).parent / "counter_trade_state.json"
        stored = self._load()
        self.cursors: dict[str, int] = {}
        for name, engine in self.source_engines.items():
            latest = self._latest_trade_id(engine)
            cursor = int(stored.get(name, latest))
            # A reset ledger starts its IDs over; do not retain an unreachable cursor.
            self.cursors[name] = latest if cursor > latest else cursor
        self._save()

    @staticmethod
    def _latest_trade_id(engine: Engine) -> int:
        trades = engine.db.get_trades(limit=1)
        return int(trades[0].id) if trades else 0

    def _load(self) -> dict[str, int]:
        if not self.state_path.is_file():
            return {}
        try:
            raw = json.loads(self.state_path.read_text())
            return {str(k): int(v) for k, v in raw.get("cursors", {}).items()}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            log.warning("Ignoring invalid counter-trade state: %s", self.state_path)
            return {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cursors": self.cursors}, sort_keys=True) + "\n")
        tmp.replace(self.state_path)

    def _signal_for(self, source: str, trade: object, ctx: ExecutionContext) -> Signal | None:
        try:
            outcome = opposite_outcome(str(trade.outcome))
            market = ctx.get_market(self.counter_engine, str(trade.market_slug))
            token_id = market.get_token_id(outcome)
            book = ctx.get_order_book(self.counter_engine, token_id)
            cost, shares = _cost_for_shares(book, float(trade.shares))
        except Exception as exc:
            log.warning("counter-trade could not quote source %s trade %s: %s", source, trade.id, exc)
            append_activity(
                self.counter_engine.db.data_dir,
                level="warn",
                event="counter_trade_quote_failed",
                strategy=STRATEGY_NAME,
                message=str(exc),
                source_strategy=source,
                source_trade_id=trade.id,
            )
            return None
        if cost < 1.0 or shares <= 0:
            log.warning(
                "counter-trade skipped source %s trade %s: opposite book has only %.4f shares / $%.4f",
                source,
                trade.id,
                shares,
                cost,
            )
            append_activity(
                self.counter_engine.db.data_dir,
                level="warn",
                event="counter_trade_no_liquidity",
                strategy=STRATEGY_NAME,
                message="opposite book cannot currently fill the mirrored BUY",
                source_strategy=source,
                source_trade_id=trade.id,
                target_shares=float(trade.shares),
                available_shares=shares,
                amount_usd=cost,
            )
            return None
        return Signal(
            action="buy",
            slug=str(trade.market_slug),
            outcome=outcome,
            amount_usd=cost,
            reason=(
                f"counter-trade of {source} BUY trade {trade.id}; "
                f"target={float(trade.shares):.6f} shares, quoted={shares:.6f}"
            ),
        )

    def process_new_buys(
        self,
        *,
        dry_run: bool,
        live: LiveTrader | None,
        ctx: ExecutionContext,
        execute: Callable[..., bool],
    ) -> tuple[list[Signal], int]:
        emitted: list[Signal] = []
        fills = 0
        for source, engine in self.source_engines.items():
            cursor = self.cursors.get(source, 0)
            trades = [t for t in engine.db.get_trades(limit=1000) if int(t.id) > cursor]
            for trade in reversed(trades):
                if str(trade.side).lower() != "buy":
                    self.cursors[source] = int(trade.id)
                    self._save()
                    continue
                signal = self._signal_for(source, trade, ctx)
                if signal is None:
                    # Retry on a later scan when the opposite book may have liquidity.
                    break
                emitted.append(signal)
                try:
                    filled = execute(
                        self.counter_engine,
                        signal,
                        dry_run,
                        live=live,
                        ctx=ctx,
                        strategy=STRATEGY_NAME,
                    )
                except Exception:
                    log.exception("counter-trade execution failed for %s trade %s", source, trade.id)
                    break
                if dry_run or not filled:
                    break
                fills += 1
                self.cursors[source] = int(trade.id)
                self._save()
        return emitted, fills
