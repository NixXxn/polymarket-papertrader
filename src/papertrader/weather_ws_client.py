from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger("papertrader")

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True)
class MarketTick:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    last_price: float | None


def _level_price(levels: list, index: int = 0) -> float | None:
    if not levels or index >= len(levels):
        return None
    row = levels[index]
    if isinstance(row, dict):
        try:
            return float(row.get("price"))
        except (TypeError, ValueError):
            return None
    try:
        return float(getattr(row, "price", row[0] if row else None))
    except (TypeError, ValueError, IndexError):
        return None


def parse_market_update(data: dict) -> MarketTick | None:
    """Normalize a CLOB market websocket payload into a price tick."""
    asset_id = data.get("asset_id") or data.get("assetId")
    if not asset_id:
        return None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    best_bid = _level_price(bids)
    best_ask = _level_price(asks)
    last_raw = data.get("price")
    try:
        last_price = float(last_raw) if last_raw is not None else None
    except (TypeError, ValueError):
        last_price = None
    if last_price is None or last_price <= 0:
        last_price = best_ask
    return MarketTick(
        token_id=str(asset_id),
        best_bid=best_bid,
        best_ask=best_ask,
        last_price=last_price,
    )


def parse_market_message(raw: str | bytes) -> list[MarketTick]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    items = payload if isinstance(payload, list) else [payload]
    ticks: list[MarketTick] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tick = parse_market_update(item)
        if tick is not None:
            ticks.append(tick)
    return ticks


async def run_market_websocket(
    token_ids: Iterable[str],
    on_tick: Callable[[MarketTick], None],
    *,
    ws_url: str = DEFAULT_WS_URL,
    reconnect_delay: float = 2.0,
) -> None:
    """Subscribe to Polymarket CLOB market stream and invoke ``on_tick`` per update."""
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError(
            "WebSocket mode needs the websockets package. "
            "Install with: pip install -e '.[momentum]'"
        ) from exc

    subscribed = [str(tid) for tid in token_ids if tid]
    if not subscribed:
        log.warning("momentum WS: no token ids to subscribe")
        return

    subscribe_msg = json.dumps({"assets_ids": subscribed, "type": "market"})
    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                await ws.send(subscribe_msg)
                log.info("momentum WS connected (%d tokens)", len(subscribed))
                async for message in ws:
                    for tick in parse_market_message(message):
                        on_tick(tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("momentum WS disconnected: %s — retry in %.0fs", exc, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
