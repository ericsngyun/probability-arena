"""Optional Kalshi WebSocket orderbook snapshot service.

Starts only when KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are both set
(see Settings.ws_enabled). Subscribes to the orderbook channel for a set of
tickers, maintains in-memory books from snapshot + delta messages, and
periodically persists them to orderbook_snapshots.

Read-only: this client never sends order messages, only channel subscriptions.
"""

import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import Market, OrderbookSnapshot

logger = logging.getLogger(__name__)

PERSIST_INTERVAL_SECONDS = 30
RECONNECT_DELAY_SECONDS = 5


def sign_ws_auth(private_key_path: str, key_id: str, ws_url: str) -> dict[str, str]:
    """Build Kalshi auth headers: RSA-PSS SHA256 over timestamp + method + path."""
    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    timestamp_ms = str(int(time.time() * 1000))
    path = urlparse(ws_url).path
    message = f"{timestamp_ms}GET{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


class OrderbookState:
    """In-memory book per ticker, built from snapshot + delta messages."""

    def __init__(self) -> None:
        # ticker -> {"yes": {price: qty}, "no": {price: qty}}
        self.books: dict[str, dict[str, dict[int, int]]] = {}
        self.dirty: set[str] = set()

    def apply_snapshot(self, ticker: str, msg: dict) -> None:
        self.books[ticker] = {
            "yes": {int(p): int(q) for p, q in (msg.get("yes") or [])},
            "no": {int(p): int(q) for p, q in (msg.get("no") or [])},
        }
        self.dirty.add(ticker)

    def apply_delta(self, ticker: str, msg: dict) -> None:
        book = self.books.setdefault(ticker, {"yes": {}, "no": {}})
        side = msg.get("side")
        if side not in ("yes", "no"):
            return
        price = int(msg["price"])
        qty = book[side].get(price, 0) + int(msg["delta"])
        if qty <= 0:
            book[side].pop(price, None)
        else:
            book[side][price] = qty
        self.dirty.add(ticker)

    def levels(self, ticker: str) -> tuple[list[list[int]], list[list[int]]]:
        book = self.books.get(ticker, {"yes": {}, "no": {}})
        yes = sorted(([p, q] for p, q in book["yes"].items()), key=lambda x: -x[0])
        no = sorted(([p, q] for p, q in book["no"].items()), key=lambda x: -x[0])
        return yes, no


def persist_dirty_books(state: OrderbookState) -> int:
    """Write one orderbook_snapshots row per dirty ticker. Returns rows written."""
    if not state.dirty:
        return 0
    dirty = list(state.dirty)
    state.dirty.clear()
    written = 0
    session = get_sessionmaker()()
    try:
        for ticker in dirty:
            market = session.execute(
                select(Market).where(Market.ticker == ticker)
            ).scalar_one_or_none()
            if market is None:
                market = Market(ticker=ticker, title="", status="unknown")
                session.add(market)
                session.flush()
            yes, no = state.levels(ticker)
            session.add(
                OrderbookSnapshot(
                    market_id=market.id,
                    captured_at=datetime.now(timezone.utc),
                    source="ws",
                    yes_levels=yes,
                    no_levels=no,
                )
            )
            written += 1
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to persist orderbook snapshots")
    finally:
        session.close()
    return written


class WsSnapshotService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.state = OrderbookState()

    def start(self) -> None:
        if not self.settings.ws_enabled:
            logger.info("Kalshi WS credentials not configured; snapshot service disabled")
            return
        tickers = self.settings.ws_ticker_list
        if not tickers:
            logger.info("KALSHI_WS_TICKERS empty; snapshot service disabled")
            return
        logger.info("Starting WS snapshot service for %d tickers", len(tickers))
        self._task = asyncio.create_task(self._run(tickers))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self, tickers: list[str]) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_and_stream(tickers)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WS connection failed; reconnecting in %ss", RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _connect_and_stream(self, tickers: list[str]) -> None:
        headers = sign_ws_auth(
            self.settings.kalshi_private_key_path,
            self.settings.kalshi_api_key_id,
            self.settings.kalshi_ws_url,
        )
        async with websockets.connect(
            self.settings.kalshi_ws_url, additional_headers=headers
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": tickers},
                    }
                )
            )
            last_persist = time.monotonic()
            async for raw in ws:
                self._handle_message(json.loads(raw))
                if time.monotonic() - last_persist >= PERSIST_INTERVAL_SECONDS:
                    written = await asyncio.to_thread(persist_dirty_books, self.state)
                    if written:
                        logger.info("Persisted %d orderbook snapshots", written)
                    last_persist = time.monotonic()

    def _handle_message(self, message: dict) -> None:
        msg_type = message.get("type")
        msg = message.get("msg") or {}
        ticker = msg.get("market_ticker")
        if not ticker:
            return
        if msg_type == "orderbook_snapshot":
            self.state.apply_snapshot(ticker, msg)
        elif msg_type == "orderbook_delta":
            self.state.apply_delta(ticker, msg)
