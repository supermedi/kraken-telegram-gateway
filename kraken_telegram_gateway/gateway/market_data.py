import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import websockets

from kraken_telegram_gateway.gateway.scalping import MarketSnapshot


KRAKEN_FUTURES_WS_URL = "wss://futures.kraken.com/ws/v1"


class KrakenFuturesBook:
    def __init__(self, product_id: str, *, volume_window: int = 20):
        self.product_id = product_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_bid: float | None = None
        self.last_ask: float | None = None
        self.last_timestamp: datetime | None = None
        self._volume_samples: deque[float] = deque(maxlen=volume_window)

    def apply(self, message: dict) -> MarketSnapshot | None:
        if message.get("product_id") != self.product_id:
            return None

        feed = message.get("feed")
        timestamp = _message_datetime(message)
        if timestamp is not None:
            self.last_timestamp = timestamp

        if feed == "book_snapshot":
            self.bids = _levels_to_book(message.get("bids", []))
            self.asks = _levels_to_book(message.get("asks", []))
        elif feed == "book":
            self._apply_book_delta(message)
        elif feed == "ticker_lite":
            self.last_bid = _optional_float(message.get("bid")) or self.last_bid
            self.last_ask = _optional_float(message.get("ask")) or self.last_ask
            volume = _optional_float(message.get("volume"))
            if volume is not None:
                self._volume_samples.append(volume)
        else:
            return None

        return self.snapshot()

    def snapshot(self) -> MarketSnapshot | None:
        best_bid = max(self.bids) if self.bids else self.last_bid
        best_ask = min(self.asks) if self.asks else self.last_ask
        if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
            return None

        bid_size = self.bids.get(best_bid, 1)
        ask_size = self.asks.get(best_ask, 1)
        return MarketSnapshot(
            timestamp=self.last_timestamp or datetime.now(timezone.utc),
            bid=best_bid,
            ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            volume_ratio=self.volume_ratio,
        )

    @property
    def volume_ratio(self) -> float:
        if len(self._volume_samples) < 2:
            return 1
        previous = self._volume_samples[-2]
        current = self._volume_samples[-1]
        if previous <= 0:
            return 1
        return max(current / previous, 0)

    def _apply_book_delta(self, message: dict) -> None:
        side = message.get("side")
        price = _optional_float(message.get("price"))
        qty = _optional_float(message.get("qty"))
        if side not in {"buy", "sell"} or price is None or qty is None:
            return

        book = self.bids if side == "buy" else self.asks
        if qty <= 0:
            book.pop(price, None)
        else:
            book[price] = qty


async def stream_kraken_futures_snapshots(
    product_id: str,
    *,
    ws_url: str = KRAKEN_FUTURES_WS_URL,
) -> AsyncIterator[MarketSnapshot]:
    book = KrakenFuturesBook(product_id)
    async with websockets.connect(ws_url, ping_interval=30, open_timeout=10) as websocket:
        await websocket.send(json.dumps({"event": "subscribe", "feed": "book", "product_ids": [product_id]}))
        await websocket.send(json.dumps({"event": "subscribe", "feed": "ticker_lite", "product_ids": [product_id]}))
        async for raw_message in websocket:
            message = json.loads(raw_message)
            if not isinstance(message, dict):
                continue
            snapshot = book.apply(message)
            if snapshot is not None:
                yield snapshot


async def collect_kraken_futures_snapshots(
    product_id: str,
    *,
    limit: int = 1,
    timeout_seconds: float = 10,
    ws_url: str = KRAKEN_FUTURES_WS_URL,
) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []

    async def collect() -> None:
        async for snapshot in stream_kraken_futures_snapshots(product_id, ws_url=ws_url):
            snapshots.append(snapshot)
            if len(snapshots) >= limit:
                break

    try:
        await asyncio.wait_for(collect(), timeout=timeout_seconds)
    except TimeoutError:
        return snapshots
    return snapshots


def _levels_to_book(levels: list[dict]) -> dict[float, float]:
    book: dict[float, float] = {}
    for level in levels:
        price = _optional_float(level.get("price"))
        qty = _optional_float(level.get("qty"))
        if price is not None and qty is not None and qty > 0:
            book[price] = qty
    return book


def _message_datetime(message: dict) -> datetime | None:
    raw_timestamp = message.get("timestamp") or message.get("time")
    timestamp = _optional_float(raw_timestamp)
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp / 1000, timezone.utc)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
