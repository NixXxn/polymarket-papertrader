"""Patches for pm_trader PolymarketClient quirks."""

from __future__ import annotations

from dataclasses import replace

from pm_trader.api import PolymarketClient, _parse_market
from pm_trader.models import Market, MarketNotFoundError

_PATCHED = False


def _v2_fill_fee(result, fee_rate_bps: int):
    """Reprice simulator fees from actual per-level fills.

    ``pm_trader`` 0.1.7's simulator applies a legacy ``min(price, 1-price)``
    formula and, for buys, passes USD not shares.  Polymarket's current V2
    schedule is ``shares * fee_rate * price * (1-price)``.  Keeping paper
    accounting aligned matters particularly for a complete-set strategy whose
    whole edge is a few cents.
    """
    if fee_rate_bps <= 0:
        return result
    fee = sum(
        float(fill.shares)
        * (float(fee_rate_bps) / 10_000.0)
        * float(fill.price)
        * (1.0 - float(fill.price))
        for fill in result.fills
    )
    return replace(result, fee=fee)


def patch_polymarket_client() -> None:
    """Retry Gamma market lookup with closed=true.

    Resolved markets often disappear from the default Gamma ``/markets?slug=``
    list but remain available when ``closed=true`` is set. Without this,
    ``resolve_all`` raises MarketNotFoundError on the first stale position and
    aborts the whole pass — leaving overnight books stuck at max_open.
    """
    global _PATCHED
    if _PATCHED:
        _PATCHED = True
        return
    if not getattr(PolymarketClient.get_market, "_closed_fallback", False):
        _orig = PolymarketClient.get_market

        def get_market(self: PolymarketClient, slug_or_id: str) -> Market:
            try:
                return _orig(self, slug_or_id)
            except MarketNotFoundError:
                data = self._gamma_get(
                    "/markets", params={"slug": slug_or_id, "closed": "true"}
                )
                if isinstance(data, list) and data:
                    market_data = data[0]
                    self._set_cached(f"market:{slug_or_id}", market_data)
                    return _parse_market(market_data)
                raise

        get_market._closed_fallback = True  # type: ignore[attr-defined]
        PolymarketClient.get_market = get_market  # type: ignore[method-assign]

    # Engine imported the functions directly, so patch both modules once.
    import pm_trader.engine as engine_module
    import pm_trader.orderbook as orderbook_module

    if not getattr(orderbook_module.simulate_buy_fill, "_v2_fee_patch", False):
        original_buy = orderbook_module.simulate_buy_fill
        original_sell = orderbook_module.simulate_sell_fill

        def simulate_buy_fill(*args, **kwargs):
            fee_rate_bps = int(kwargs.get("fee_rate_bps", args[2] if len(args) > 2 else 0))
            return _v2_fill_fee(original_buy(*args, **kwargs), fee_rate_bps)

        def simulate_sell_fill(*args, **kwargs):
            fee_rate_bps = int(kwargs.get("fee_rate_bps", args[2] if len(args) > 2 else 0))
            return _v2_fill_fee(original_sell(*args, **kwargs), fee_rate_bps)

        simulate_buy_fill._v2_fee_patch = True  # type: ignore[attr-defined]
        simulate_sell_fill._v2_fee_patch = True  # type: ignore[attr-defined]
        orderbook_module.simulate_buy_fill = simulate_buy_fill
        orderbook_module.simulate_sell_fill = simulate_sell_fill
        engine_module.simulate_buy_fill = simulate_buy_fill
        engine_module.simulate_sell_fill = simulate_sell_fill
    _PATCHED = True
