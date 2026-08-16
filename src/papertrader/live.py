from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from pm_trader.engine import Engine
from pm_trader.models import OrderRejectedError, SimError

from papertrader.execution import ExecutionContext, _tick_str
from papertrader.mode import ResolvedMode
from papertrader.signals import Signal

log = logging.getLogger("papertrader")


class LiveClient(Protocol):
    def get_balance(self) -> float | None: ...

    def market_order(
        self,
        *,
        token_id: str,
        side: str,
        amount: float,
        tick_size: str,
        neg_risk: bool,
    ) -> dict[str, Any]: ...


def parse_balance(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(0.0, float(raw))
    if isinstance(raw, str):
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
    if isinstance(raw, dict):
        for key in ("balance", "available", "allowance", "collateral"):
            if key in raw:
                parsed = parse_balance(raw[key])
                if parsed is not None:
                    return parsed
        nested = raw.get("data")
        if nested is not None:
            return parse_balance(nested)
    return None


def parse_clob_fill(resp: dict[str, Any], *, side: str) -> tuple[float, float, float]:
    """Return (avg_price, shares, usd) from a CLOB post response."""
    if not isinstance(resp, dict):
        raise OrderRejectedError("empty CLOB response")
    if resp.get("success") is False:
        raise OrderRejectedError(str(resp.get("errorMsg") or resp.get("error") or resp))
    status = str(resp.get("status") or "").lower()
    if status in {"unmatched", "rejected", "cancelled", "canceled"}:
        raise OrderRejectedError(f"CLOB status {status}")

    avg = _num(resp.get("average_price") or resp.get("avgPrice") or resp.get("price"))
    shares = _num(
        resp.get("size_matched")
        or resp.get("sizeMatched")
        or resp.get("filledSize")
        or resp.get("takingAmount")
    )
    usd = _num(resp.get("amount_usd") or resp.get("makingAmount"))
    taking = _num(resp.get("takingAmount"))
    making = _num(resp.get("makingAmount"))
    side_u = side.upper()
    if avg <= 0 and taking > 0 and making > 0:
        # BUY: spend `making` USDC, receive `taking` shares (common CLOB shape).
        if side_u == "BUY":
            shares = taking
            usd = making
            avg = usd / shares if shares else 0.0
        else:
            shares = making
            usd = taking
            avg = usd / shares if shares else 0.0
    if shares <= 0 and usd > 0 and avg > 0:
        shares = usd / avg
    if usd <= 0 and shares > 0 and avg > 0:
        usd = shares * avg
    if avg <= 0 or shares <= 0 or usd <= 0:
        raise OrderRejectedError(f"CLOB fill missing size/price: {resp}")
    return avg, shares, usd


def _num(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def record_fill(
    engine: Engine,
    *,
    market: Any,
    outcome: str,
    side: str,
    avg_price: float,
    shares: float,
    usd: float,
    fee: float = 0.0,
) -> None:
    account = engine.get_account()
    if side == "buy":
        engine.db.update_cash(account.cash - usd - fee)
        engine.db.insert_trade(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            market_question=market.question,
            outcome=outcome,
            side="buy",
            order_type="fak",
            avg_price=avg_price,
            amount_usd=usd,
            shares=shares,
            fee_rate_bps=0,
            fee=fee,
            slippage=0.0,
            levels_filled=1,
            is_partial=False,
        )
        engine._update_position_after_buy(
            market=market,
            outcome=outcome,
            new_shares=shares,
            cost=usd + fee,
            avg_fill_price=avg_price,
        )
        return
    engine.db.update_cash(account.cash + usd - fee)
    engine.db.insert_trade(
        market_condition_id=market.condition_id,
        market_slug=market.slug,
        market_question=market.question,
        outcome=outcome,
        side="sell",
        order_type="fak",
        avg_price=avg_price,
        amount_usd=usd,
        shares=shares,
        fee_rate_bps=0,
        fee=fee,
        slippage=0.0,
        levels_filled=1,
        is_partial=False,
    )
    engine._update_position_after_sell(
        market=market,
        outcome=outcome,
        sold_shares=shares,
        proceeds=usd - fee,
    )


class PyClobLiveClient:
    """Thin wrapper around py-clob-client-v2. Imported only in live mode."""

    def __init__(self, resolved: ResolvedMode) -> None:
        try:
            from py_clob_client_v2 import ClobClient
        except ImportError as e:
            raise SimError(
                "Live mode needs py-clob-client-v2. Install with: pip install -e '.[live]'"
            ) from e
        kwargs: dict[str, Any] = {
            "host": resolved.clob_host,
            "chain_id": resolved.chain_id,
            "key": resolved.private_key,
        }
        if resolved.signature_type:
            kwargs["signature_type"] = resolved.signature_type
        if resolved.funder:
            kwargs["funder"] = resolved.funder
        client = ClobClient(**kwargs)
        derive = getattr(client, "create_or_derive_api_key", None) or getattr(
            client, "create_or_derive_api_creds", None
        )
        creds = derive() if callable(derive) else None
        if creds is not None and hasattr(client, "set_api_creds"):
            client.set_api_creds(creds)
        self._client = client

    def get_balance(self) -> float | None:
        try:
            raw = self._client.get_balance_allowance()
        except Exception as e:
            log.warning("live balance fetch failed: %s", e)
            return None
        return parse_balance(raw)

    def market_order(
        self,
        *,
        token_id: str,
        side: str,
        amount: float,
        tick_size: str,
        neg_risk: bool,
    ) -> dict[str, Any]:
        from py_clob_client_v2 import (
            MarketOrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        side_enum = Side.BUY if side.upper() == "BUY" else Side.SELL
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side_enum,
            order_type=OrderType.FAK,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        resp = self._client.create_and_post_market_order(
            order_args=args,
            options=options,
            order_type=OrderType.FAK,
        )
        if not isinstance(resp, dict):
            return {"raw": resp}
        return resp


class LiveTrader:
    def __init__(self, client: LiveClient) -> None:
        self.client = client

    def sync_cash(self, engine: Engine) -> None:
        bal = self.client.get_balance()
        if bal is None:
            return
        engine.db.update_cash(bal)

    def fill(self, engine: Engine, signal: Signal, ctx: ExecutionContext | None = None) -> bool:
        ctx = ctx or ExecutionContext()
        started = time.perf_counter()
        market = ctx.get_market(engine, signal.slug)
        outcome = engine._validate_outcome(signal.outcome, market)
        if signal.action == "sell":
            position = engine.db.get_position(market.condition_id, outcome)
            if position is None or position.shares <= 0:
                raise OrderRejectedError(f"no live position to sell for {signal.slug}")
        token_id = market.get_token_id(outcome)
        tick = ctx.get_tick_size(
            engine, token_id, _tick_str(getattr(market, "tick_size", 0.01))
        )
        neg_risk = bool(getattr(market, "neg_risk", False))
        if signal.action == "buy":
            amount = float(signal.amount_usd or 0)
            side = "BUY"
            bal = ctx.wallet_balance
            if bal is None:
                bal = self.client.get_balance()
                ctx.wallet_balance = bal
            if bal is not None and amount > bal:
                raise OrderRejectedError(
                    f"wallet balance ${bal:.2f} below live buy ${amount:.2f}"
                )
        else:
            amount = float(signal.shares or 0)
            side = "SELL"
        if amount <= 0:
            raise OrderRejectedError("live order amount is zero")
        resp = self.client.market_order(
            token_id=str(token_id),
            side=side,
            amount=amount,
            tick_size=tick,
            neg_risk=neg_risk,
        )
        avg, shares, usd = parse_clob_fill(resp, side=side)
        record_fill(
            engine,
            market=market,
            outcome=outcome,
            side=signal.action,
            avg_price=avg,
            shares=shares,
            usd=usd,
        )
        if signal.action == "buy" and ctx.wallet_balance is not None:
            ctx.wallet_balance -= usd
        elif signal.action == "sell" and ctx.wallet_balance is not None:
            ctx.wallet_balance += usd
        log.info(
            "LIVE %s %s @ %.3f shares=%.2f usd=%.4f (%.0f ms) — %s",
            signal.action.upper(),
            signal.slug,
            avg,
            shares,
            usd,
            (time.perf_counter() - started) * 1000,
            signal.reason,
        )
        return True

