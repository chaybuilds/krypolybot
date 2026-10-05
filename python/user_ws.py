from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("user_ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
_PING_SEC = 10.0
_SILENT_SEC = 45.0
_RECONNECT_MAX = 60.0


def _creds() -> Optional[dict]:
    try:
        import polymarket_auth
        c = polymarket_auth.get_api_creds()
    except Exception:
        return None
    if isinstance(c, dict) and all(c.get(k) for k in ("apiKey", "secret", "passphrase")):
        return c
    return None


class UserFeed:
    def __init__(self) -> None:
        self.connected = False
        self.wake: Optional[asyncio.Event] = None
        self.orders: dict[str, dict] = {}
        self.trades: collections.deque = collections.deque(maxlen=500)
        self.events = 0
        self.last_event_at = 0.0
        self.last_error = ""
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._api_key = ""

    def handle(self, msg) -> bool:
        if isinstance(msg, list):
            hit = False
            for m in msg:
                hit = self.handle(m) or hit
            return hit
        if not isinstance(msg, dict):
            return False
        et = str(msg.get("event_type") or "").lower()
        if et == "order":
            oid = str(msg.get("id") or "")
            if not oid:
                return False
            self.orders[oid] = {
                "status": msg.get("status"), "type": msg.get("type"),
                "sizeMatched": _f(msg.get("size_matched")),
                "originalSize": _f(msg.get("original_size")),
                "price": _f(msg.get("price")), "assetId": msg.get("asset_id"),
                "at": time.time(),
            }
            if len(self.orders) > 2000:
                for k in list(self.orders)[:500]:
                    self.orders.pop(k, None)
        elif et == "trade":
            ours = []
            if msg.get("taker_order_id") and (
                    not self._api_key or msg.get("trade_owner") == self._api_key
                    or msg.get("owner") == self._api_key):
                ours.append(str(msg["taker_order_id"]))
            for mo in msg.get("maker_orders") or []:
                if not self._api_key or mo.get("owner") == self._api_key:
                    if mo.get("order_id"):
                        ours.append(str(mo["order_id"]))
            if not ours:
                return False
            self.trades.append({
                "id": msg.get("id"), "orders": ours, "status": msg.get("status"),
                "side": msg.get("side"), "size": _f(msg.get("size")),
                "price": _f(msg.get("price")), "traderSide": msg.get("trader_side"),
                "assetId": msg.get("asset_id"), "at": time.time(),
            })
        else:
            return False
        self.events += 1
        self.last_event_at = time.time()
        if self.wake is not None:
            self.wake.set()
        return True

    def order(self, order_id: str) -> Optional[dict]:
        return self.orders.get(str(order_id))

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "connected": self.connected,
            "events": self.events,
            "lastEventAgoSec": (time.time() - self.last_event_at) if self.last_event_at else None,
            "lastError": self.last_error,
        }

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.is_running():
            self._stop = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.connected = False

    async def _run(self) -> None:
        import websockets
        import ws_ssl
        attempt = 0
        while not self._stop:
            creds = _creds()
            if creds is None:
                await asyncio.sleep(5)
                continue
            self._api_key = creds["apiKey"]
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=None, open_timeout=10, close_timeout=5,
                    ssl=ws_ssl.client_context(),
                ) as ws:
                    await ws.send(json.dumps({"auth": {
                        "apiKey": creds["apiKey"], "secret": creds["secret"],
                        "passphrase": creds["passphrase"]}, "type": "user"}))
                    self.connected = True
                    attempt = 0
                    last_ping = 0.0
                    last_msg = time.time()
                    while not self._stop:
                        if time.time() - last_ping >= _PING_SEC:
                            await ws.send("PING")
                            last_ping = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            if time.time() - last_msg > _SILENT_SEC:
                                break
                            continue
                        last_msg = time.time()
                        if raw == "PONG":
                            continue
                        try:
                            self.handle(json.loads(raw))
                        except (ValueError, TypeError):
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.debug(f"user_ws: {self.last_error}")
            finally:
                self.connected = False
            if self._stop:
                break
            attempt = min(attempt + 1, 6)
            await asyncio.sleep(min(_RECONNECT_MAX, 2 ** attempt))


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


feed = UserFeed()
