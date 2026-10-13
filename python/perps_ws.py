"""Real-time Polymarket Perps market data (public WebSocket).

Keeps a live best-bid/offer cache and a trade tape for the instruments the
market maker quotes, and wakes the engine the moment a market's mid moves —
so quotes are re-centred within milliseconds of a move instead of at the next
poll. That latency is most of a maker's adverse selection.

Protocol (verified live 2026-10-05):
  url    wss://ws.perpetuals.polymarket.com/v1/ws            (public)
  sub    {"id": n, "req": "sub",   "chs": ["bbo::<iid>", "trades::<iid>"]}
  unsub  {"id": n, "req": "unsub", "chs": [...]}   — no reconnect needed
  ack    {"id": n, "data": [{"status": "ok"}, ...]}
  bbo    {"ch": "bbo::34", "ts", "ets", "sq",
          "data": {"iid": 34, "bp": "3.8289", "bq": "720.24", "ap": "3.8317", "aq": "1043.9"}}
  trades {"ch": "trades::6", "ts", "sq",
          "data": [{"tid", "iid", "side": "long", "p": "85884", "qty": "0.01327",
                    "settlement": false, "ts", "hash"}]}
  ping   {"id": 0, "req": "post", "op": {"type": "ping"}} every ~25s; the
         connection is stale after ~65s without any message.

STRICT ACCELERATOR (like clob_ws): every reader falls back to REST whenever
the socket is down, an instrument isn't subscribed yet, or a quote is stale.
The engine is never MORE fragile than pure polling — only faster.

The trade tape and periodic BBO samples are also buffered for the recorder
(perps_record), which is what makes the backtester's data denser than the
REST trade history alone.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("perps")

WS_URL = "wss://ws.perpetuals.polymarket.com/v1/ws"
_PING_INTERVAL = 25.0
_STALE_SOCKET = 65.0
_STALE_QUOTE = 5.0
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 30.0
_SAMPLE_EVERY = 5.0
_BUFFER_MAX = 20_000



def parse_bbo(msg: dict) -> Optional[dict]:
    d = msg.get("data")
    if not isinstance(d, dict):
        return None
    try:
        bid, ask = float(d["bp"]), float(d["ap"])
        iid = int(d.get("iid") or str(msg.get("ch", "")).split("::")[1])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if bid <= 0 or ask <= 0 or bid >= ask:
        return None
    return {"iid": iid, "bid": bid, "ask": ask,
            "bq": _f(d.get("bq")), "aq": _f(d.get("aq")),
            "ts": int(msg.get("ts") or 0)}


def parse_trades(msg: dict) -> list[dict]:
    rows = msg.get("data")
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        try:
            if r.get("settlement"):
                continue
            out.append({"trade_id": int(r["tid"]), "iid": int(r["iid"]),
                        "side": str(r["side"]), "price": float(r["p"]),
                        "qty": float(r["qty"]), "ts": int(r["ts"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None



class PerpsFeed:
    def __init__(self) -> None:
        self._want: set[int] = set()
        self._subbed: set[int] = set()
        self._bbo: dict[int, dict] = {}
        self._last_sample: dict[int, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._ws = None
        self._req_id = 1
        self._changed = asyncio.Event()
        self.wake: Optional[asyncio.Event] = None
        self.wake_bps = 1.0
        self._wake_mid: dict[int, float] = {}
        self.moved: set[int] = set()
        self.connected = False
        self.messages = 0
        self.last_message_at = 0.0
        self.connected_since = 0.0
        self.trades: collections.deque = collections.deque(maxlen=_BUFFER_MAX)
        self.samples: collections.deque = collections.deque(maxlen=_BUFFER_MAX)
        self.recent_trades: dict[int, collections.deque] = {}

    def set_instruments(self, iids) -> None:
        want = {int(i) for i in iids}
        if want != self._want:
            self._want = want
            self._changed.set()

    def instruments(self) -> set[int]:
        return set(self._want)

    def bbo(self, iid: int, max_age: float = _STALE_QUOTE) -> Optional[dict]:
        q = self._bbo.get(int(iid))
        if not q or not self.connected:
            return None
        if time.monotonic() - q["at"] > max_age:
            return None
        return q

    def trades_since(self, iid: int, since_ms: int) -> list[dict]:
        dq = self.recent_trades.get(int(iid))
        return [t for t in dq if t["ts"] >= since_ms] if dq else []

    def take_moved(self) -> set[int]:
        out, self.moved = self.moved, set()
        return out

    def drain(self) -> tuple[list[dict], list[dict]]:
        """(trades, bbo samples) buffered since the last drain."""
        tr, sm = list(self.trades), list(self.samples)
        self.trades.clear()
        self.samples.clear()
        return tr, sm

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "running": self._task is not None and not self._task.done(),
            "connected": self.connected,
            "subscribed": sorted(self._subbed),
            "messages": self.messages,
            "lastMessageAgoSec": (now - self.last_message_at) if self.last_message_at else None,
            "uptimeSec": (now - self.connected_since) if self.connected else 0.0,
        }

    def handle(self, msg) -> None:
        if isinstance(msg, list):
            for m in msg:
                self.handle(m)
            return
        if not isinstance(msg, dict):
            return
        ch = str(msg.get("ch") or "")
        if ch.startswith("bbo::"):
            q = parse_bbo(msg)
            if q is None:
                return
            now = time.monotonic()
            q["at"] = now
            q["mid"] = (q["bid"] + q["ask"]) / 2
            self._bbo[q["iid"]] = q
            if now - self._last_sample.get(q["iid"], 0.0) >= _SAMPLE_EVERY:
                self._last_sample[q["iid"]] = now
                self.samples.append({"iid": q["iid"], "ts": q["ts"] or int(time.time() * 1000),
                                     "bid": q["bid"], "ask": q["ask"]})
            ref = self._wake_mid.get(q["iid"])
            if ref is None or abs(q["mid"] - ref) / ref * 1e4 >= self.wake_bps:
                self._wake_mid[q["iid"]] = q["mid"]
                if ref is not None:
                    self.moved.add(q["iid"])
                    if self.wake is not None:
                        self.wake.set()
        elif ch.startswith("trades::"):
            for t in parse_trades(msg):
                self.trades.append(t)
                dq = self.recent_trades.setdefault(t["iid"], collections.deque(maxlen=500))
                dq.append(t)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.is_running():
            self._stop = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        self._changed.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.connected = False
        self._subbed.clear()

    async def _run(self) -> None:
        import websockets
        import ws_ssl
        backoff = _RECONNECT_BASE
        while not self._stop:
            if not self._want:
                self._changed.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=1.0)
                continue
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=None, open_timeout=10, close_timeout=5,
                    ssl=ws_ssl.client_context(), max_size=2**22,
                ) as ws:
                    self._ws = ws
                    self._subbed = set()
                    self.connected = True
                    self.connected_since = time.monotonic()
                    self.last_message_at = time.monotonic()
                    backoff = _RECONNECT_BASE
                    await self._sync_subs(ws)
                    ping = asyncio.ensure_future(self._ping_loop(ws))
                    try:
                        await self._recv_loop(ws)
                    finally:
                        ping.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await ping
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"perps_ws connection error: {e}")
            finally:
                self.connected = False
                self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(_RECONNECT_MAX, backoff * 2)

    async def _sync_subs(self, ws) -> None:
        add = self._want - self._subbed
        drop = self._subbed - self._want
        if drop:
            await ws.send(json.dumps({"id": self._next_id(), "req": "unsub",
                                      "chs": _channels(drop)}))
            for i in drop:
                self._bbo.pop(i, None)
                self._wake_mid.pop(i, None)
        if add:
            await ws.send(json.dumps({"id": self._next_id(), "req": "sub",
                                      "chs": _channels(add)}))
        self._subbed = set(self._want)

    async def _recv_loop(self, ws) -> None:
        while not self._stop:
            if self._changed.is_set():
                self._changed.clear()
                await self._sync_subs(ws)
            recv = asyncio.ensure_future(ws.recv())
            changed = asyncio.ensure_future(self._changed.wait())
            done, _ = await asyncio.wait({recv, changed}, timeout=_STALE_SOCKET,
                                         return_when=asyncio.FIRST_COMPLETED)
            if recv not in done:
                recv.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await recv
            if changed not in done:
                changed.cancel()
            if not done:
                return
            if recv in done:
                raw = recv.result()
                self.messages += 1
                self.last_message_at = time.monotonic()
                try:
                    self.handle(json.loads(raw))
                except (ValueError, TypeError):
                    continue

    async def _ping_loop(self, ws) -> None:
        while not self._stop:
            await asyncio.sleep(_PING_INTERVAL)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"id": 0, "req": "post", "op": {"type": "ping"}}))

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id


def _channels(iids) -> list[str]:
    out = []
    for i in sorted(iids):
        out += [f"bbo::{i}", f"trades::{i}"]
    return out


feed = PerpsFeed()
