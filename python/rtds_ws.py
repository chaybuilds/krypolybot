"""Chainlink oracle prices for the crypto up/down markets — the series they
actually SETTLE on — over Polymarket's PolyBolt socket, with the legacy RTDS
socket as a fallback.

SETTLEMENT (since 2026-08-07; 5m windows moved to 60s on 2026-08-14). The
up/down markets resolve on Chainlink's 60-second TWAP stream
(`data.chain.link/streams/<asset>-usd-twap-60s-streams`): Up wins when the
TWAP at the window's close is ≥ the TWAP at its open. So this module keeps
two series per asset:
  * spot  — Chainlink spot prints, ~1/s (`price.crypto`), and
  * twap  — Chainlink 60s TWAP points, ~1/s (`price.crypto.twap`); the point
            stamped at a window's open second IS that window's official strike.

LATENCY (measured 2026-10-05, 3-second-return cross-correlation against the
Coinbase ticker, ETH, 5 minutes): legacy RTDS Chainlink spot ~1s behind;
PolyBolt `price.crypto` Chainlink spot ~20s behind (correlation 0.4 — a
delayed redistribution); PolyBolt `price.crypto` pinned to Pyth ~0s
(correlation 0.81); the 60s TWAP (identical values on both transports) tracks
the trailing 60s Coinbase mean to ~1-2s. So on PolyBolt the LIVE spot is the
Pyth pin, and a Chainlink spot point there is recorded but never served as
live — a 20s-stale "live" price is exactly what the final-minute sniper must
not see. HYPE has no Pyth feed: on PolyBolt it gets TWAP only, and its live
spot falls back to the next layer.

TRANSPORTS
  PolyBolt (primary): wss://ws-live-v2.polymarket.com/ws
    auth       {"op":"auth","rid":..,"auth":{apiKey,secret,passphrase}} → {"op":"authed"}
               (the CLOB API credentials the bot already derives)
    subscribe  {"op":"subscribe","rid":..,"subscriptions":[
                  {"channel":"price.crypto","filter":{"symbol":"btcusd"}},
                  {"channel":"price.crypto.twap","filter":{"symbol":"btcusd","window_seconds":60}}]}
    data       {"v":1,"channel","seq","ts","snapshot"?,"payload":{symbol,value,timestamp,source,..}}
               Each subscription opens with a snapshot of the previous ~2 minutes
               (payload.data: [points]) — so a strike is recoverable after a
               late start. Verified live 2026-10-05.
  RTDS (legacy fallback): wss://ws-live-data.polymarket.com, topics
    `crypto_prices_chainlink` and `crypto_prices_twap_sixty`. Deprecated by
    Polymarket on 2026-09-23 with removal planned about a month later; used
    only when there are no CLOB credentials or PolyBolt is unreachable.

STRICT ACCELERATOR like spot_ws/clob_ws: consumers fall back per asset to the
Coinbase WS overlay and then the REST chain whenever this feed is down, cold
or stale. Never MORE fragile than what it upgrades.

Opt out with KRYPT_RTDS_WS=0 (env) or the `crypto15m_rtds_ws` config key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("rtds_ws")

try:
    import websockets  # type: ignore
    _WS_IMPORT_OK = True
except Exception:  # pragma: no cover - websockets missing
    websockets = None  # type: ignore
    _WS_IMPORT_OK = False

_POLYBOLT_URL = os.environ.get("KRYPT_POLYBOLT_URL") or "wss://ws-live-v2.polymarket.com/ws"
_URL = os.environ.get("KRYPT_RTDS_WS_URL") or "wss://ws-live-data.polymarket.com"

_DISABLED = os.environ.get("KRYPT_RTDS_WS", "1").strip().lower() in (
    "0", "off", "false", "no",
)

_TOPIC = "crypto_prices_chainlink"
_TWAP_TOPIC = "crypto_prices_twap_sixty"
TWAP_WINDOW_SEC = 60

_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB", "ZEC")
_PB_LIVE_SOURCES = {"pyth"}
_BASIS_ALPHA = 0.02
_BASIS_MIN_SAMPLES = 50
_BASIS_MAX_BPS = 15.0
_PB_PYTH = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
_ASSET_BY_SYMBOL: dict[str, str] = {f"{a.lower()}/usd": a for a in _ASSETS}
_ASSET_BY_PB_SYMBOL: dict[str, str] = {f"{a.lower()}usd": a for a in _ASSETS}

_FRESH_SEC = 10.0
_PING_INTERVAL_SEC = 5.0
_PB_PING_SEC = 20.0
_SILENT_TIMEOUT_SEC = 30.0
_RECONNECT_MAX_SEC = 60.0
_SAMPLES_MAX = 300
_SETTLE_WINDOW_SEC = TWAP_WINDOW_SEC
_PB_FAILS_BEFORE_FALLBACK = 2
_ROUTINE_CLOSE_CODES = (1000, 4003)
_ROUTINE_RECONNECT_SEC = 1.0


def _close_code(e: BaseException) -> Optional[int]:
    """The close code the server sent, if `e` is a websocket close."""
    return getattr(getattr(e, "rcvd", None), "code", None)


def _creds() -> Optional[dict]:
    """CLOB API credentials for PolyBolt, or None (→ legacy RTDS)."""
    try:
        import polymarket_auth
        c = polymarket_auth.get_api_creds()
    except Exception:
        return None
    if isinstance(c, dict) and all(c.get(k) for k in ("apiKey", "secret", "passphrase")):
        return c
    return None


class _Client:
    def __init__(self) -> None:
        self.connected: bool = False
        self.transport: str = ""
        self.spot_sources: dict[str, str] = {}
        self.basis: dict[str, float] = {}
        self._stop: bool = False
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._pb_fails = 0
        self.last_error: str = ""
        self.last_msg_t: float = 0.0
        self.prices: dict[str, tuple[float, float]] = {}
        self.twaps: dict[str, tuple[float, float]] = {}
        self.samples: dict[str, deque] = {a: deque(maxlen=_SAMPLES_MAX) for a in _ASSETS}
        self.twap_samples: dict[str, deque] = {a: deque(maxlen=_SAMPLES_MAX) for a in _ASSETS}

    def start(self) -> None:
        if _DISABLED or not _WS_IMPORT_OK:
            if not _WS_IMPORT_OK and not _DISABLED:
                logger.warning("rtds_ws: `websockets` not installed — staying on fallbacks")
            return
        self._stop = False
        loop = asyncio.get_event_loop()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run(), name="rtds_ws")
            logger.info("rtds_ws: starting (chainlink spot + 60s TWAP)")

    async def stop(self) -> None:
        self._stop = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self.connected = False

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        attempt = 0
        while not self._stop:
            creds = _creds()
            use_pb = creds is not None and self._pb_fails < _PB_FAILS_BEFORE_FALLBACK
            try:
                if use_pb:
                    await self._connect_polybolt(creds)
                    self._pb_fails = 0
                else:
                    await self._connect_rtds()
                    self._pb_fails = 0
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                routine = self.connected and _close_code(e) in _ROUTINE_CLOSE_CODES
                if use_pb and not routine:
                    self._pb_fails += 1
                elif not use_pb:
                    self._pb_fails = 0
                self.last_error = f"{type(e).__name__}: {e}"
                if routine:
                    attempt = 0
                    backoff = _ROUTINE_RECONNECT_SEC
                else:
                    attempt = min(attempt + 1, 16)
                    backoff = min(2.0 ** attempt, _RECONNECT_MAX_SEC)
                    backoff *= random.uniform(0.75, 1.25)
                logger.log(
                    logging.INFO if routine else logging.WARNING,
                    f"rtds_ws: {'polybolt' if use_pb else 'rtds'} disconnected "
                    f"({self.last_error}); reconnect in {backoff:.0f}s"
                )
                self.connected = False
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
            finally:
                self.connected = False

    async def _connect_polybolt(self, creds: dict) -> None:
        import ws_ssl
        kwargs = dict(close_timeout=5, max_size=2 ** 22, ssl=ws_ssl.client_context())
        async with websockets.connect(_POLYBOLT_URL, **kwargs) as ws:
            self._ws = ws
            self.last_msg_t = time.time()
            await ws.send(json.dumps({"op": "auth", "rid": "auth", "auth": {
                "apiKey": creds["apiKey"], "secret": creds["secret"],
                "passphrase": creds["passphrase"]}}))
            deadline = time.time() + 10
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise RuntimeError("polybolt auth timed out")
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                if m.get("op") == "authed":
                    break
                if m.get("op") == "error":
                    raise RuntimeError(f"polybolt auth refused: {m.get('code')}")
            subs = []
            for sym, asset in _ASSET_BY_PB_SYMBOL.items():
                if asset in _PB_PYTH:
                    subs.append({"channel": "price.crypto",
                                 "filter": {"symbol": sym, "provider": "pyth"}})
                subs.append({"channel": "price.crypto.twap",
                             "filter": {"symbol": sym, "window_seconds": TWAP_WINDOW_SEC}})
            await ws.send(json.dumps({"op": "subscribe", "rid": "subs", "subscriptions": subs}))
            self._pb_fails = 0
            self.connected = True
            self.transport = "polybolt"
            logger.info(f"rtds_ws: connected → {_POLYBOLT_URL} (polybolt)")
            last_ping = time.time()
            while not self._stop:
                if time.time() - last_ping >= _PB_PING_SEC:
                    try:
                        await ws.send(json.dumps({"op": "ping"}))
                    except Exception:
                        return
                    last_ping = time.time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if time.time() - self.last_msg_t > _SILENT_TIMEOUT_SEC:
                        logger.warning("rtds_ws: polybolt silent — forcing reconnect")
                        return
                    continue
                self.last_msg_t = time.time()
                try:
                    self.handle_polybolt(json.loads(raw))
                except Exception as e:
                    logger.debug(f"rtds_ws: polybolt handle error: {e}")

    async def _connect_rtds(self) -> None:
        import ws_ssl
        kwargs = dict(ping_interval=None, close_timeout=5, max_size=2 ** 22,
                      ssl=ws_ssl.client_context())
        async with websockets.connect(_URL, **kwargs) as ws:
            self._ws = ws
            self.connected = True
            self.transport = "rtds"
            self.last_msg_t = time.time()
            await ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{"topic": _TOPIC, "type": "*"},
                                  {"topic": _TWAP_TOPIC, "type": "*"}],
            }))
            logger.info(f"rtds_ws: connected → {_URL} (legacy rtds)")
            last_ping = time.time()
            started = time.time()
            while not self._stop:
                if time.time() - started > 300 and _creds() is not None:
                    return
                if time.time() - last_ping >= _PING_INTERVAL_SEC:
                    try:
                        await ws.send("PING")
                    except Exception:
                        return
                    last_ping = time.time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if time.time() - self.last_msg_t > _SILENT_TIMEOUT_SEC:
                        logger.warning("rtds_ws: silent link — forcing reconnect")
                        return
                    continue
                self.last_msg_t = time.time()
                if raw == "PONG":
                    continue
                try:
                    self.handle_message(json.loads(raw))
                except Exception as e:
                    logger.debug(f"rtds_ws: handle error: {e}")

    def _record(self, asset: str, px: float, ts_ms: Optional[float], *, twap: bool,
                live: bool) -> None:
        if px is None or px <= 0:
            return
        now = time.time()
        sec = int((ts_ms / 1000.0) if ts_ms else now)
        ring = (self.twap_samples if twap else self.samples)[asset]
        if not ring or ring[-1][0] < sec:
            ring.append((sec, px))
        elif ring[-1][0] == sec:
            ring[-1] = (sec, px)
        elif all(s != sec for s, _p in ring):
            items = sorted(list(ring) + [(sec, px)])
            ring.clear()
            ring.extend(items[-ring.maxlen:])
        if live:
            (self.twaps if twap else self.prices)[asset] = (now, px)
        if twap and self.spot_sources.get(asset) == "pyth":
            self._update_basis(asset, sec, px)

    def _update_basis(self, asset: str, twap_sec: int, twap_px: float) -> None:
        lo, hi = twap_sec - 61, twap_sec - 1
        xs = [px for sec, px in self.samples[asset] if lo < sec <= hi]
        if len(xs) < _BASIS_MIN_SAMPLES or twap_px <= 0:
            return
        b = sum(xs) / len(xs) / twap_px - 1.0
        if abs(b) * 1e4 > _BASIS_MAX_BPS:
            return
        cur = self.basis.get(asset)
        self.basis[asset] = b if cur is None else cur + _BASIS_ALPHA * (b - cur)

    def _adj(self, asset: str, px: float) -> float:
        """A Pyth price in Chainlink terms (unchanged for Chainlink spots)."""
        if self.spot_sources.get(asset) == "pyth":
            return px / (1.0 + self.basis.get(asset, 0.0))
        return px

    def handle_polybolt(self, m) -> None:
        """PolyBolt envelopes: snapshots seed the rings (they carry the
        previous ~2 minutes, i.e. a window open the app started after), live
        points update the latest price too."""
        if not isinstance(m, dict):
            return
        ch = m.get("channel")
        if ch not in ("price.crypto", "price.crypto.twap"):
            if m.get("op") == "error":
                self.last_error = f"polybolt {m.get('code')} on {m.get('channel')}"
            return
        p = m.get("payload")
        if not isinstance(p, dict):
            return
        asset = _ASSET_BY_PB_SYMBOL.get(str(p.get("symbol") or "").lower())
        if not asset:
            return
        twap = ch == "price.crypto.twap"
        src = str(p.get("source") or "").lower()
        if not twap:
            if src and src not in _PB_LIVE_SOURCES:
                self.spot_sources[asset] = src
                return
            self.spot_sources[asset] = src or "pyth"
        points = p.get("data") if m.get("snapshot") else [p]
        for i, pt in enumerate(points or []):
            try:
                px = float(pt.get("value"))
            except (TypeError, ValueError, AttributeError):
                continue
            last = i == len(points) - 1
            self._record(asset, px, pt.get("timestamp"), twap=twap,
                         live=(not m.get("snapshot")) or last)

    def handle_message(self, m) -> None:
        """Legacy RTDS `update` envelopes (spot or 60s TWAP). History bundles
        (payload.data lists), acks, and unknown symbols are ignored."""
        if not isinstance(m, dict) or m.get("topic") not in (_TOPIC, _TWAP_TOPIC):
            return
        p = m.get("payload")
        if m.get("type") != "update" or not isinstance(p, dict):
            return
        asset = _ASSET_BY_SYMBOL.get((p.get("symbol") or "").lower())
        if not asset:
            return
        try:
            px = float(p.get("value"))
        except (TypeError, ValueError):
            return
        if m.get("topic") == _TOPIC:
            self.spot_sources[asset] = "chainlink"
        self._record(asset, px, p.get("timestamp"), twap=m.get("topic") == _TWAP_TOPIC, live=True)

    def spot(self, asset: str) -> Optional[float]:
        a = (asset or "").upper()
        rec = self.prices.get(a)
        if not rec or not self.connected:
            return None
        ts, px = rec
        if time.time() - ts > _FRESH_SEC:
            return None
        return self._adj(a, px)

    def twap(self, asset: str) -> Optional[float]:
        """Latest live 60s TWAP point, or None when cold/stale."""
        rec = self.twaps.get((asset or "").upper())
        if not rec or not self.connected:
            return None
        ts, px = rec
        if time.time() - ts > _FRESH_SEC:
            return None
        return px

    def fresh_spots(self) -> dict[str, float]:
        """Live oracle prices fresh enough to trust, keyed by asset. Empty
        when the socket is down/cold — callers then use the next layer."""
        if not self.connected:
            return {}
        now = time.time()
        return {
            a: self._adj(a, px) for a, (ts, px) in self.prices.items()
            if now - ts <= _FRESH_SEC
        }

    @staticmethod
    def _nearest(ring, epoch_sec: float, tolerance_sec: float) -> Optional[float]:
        if not ring:
            return None
        target = int(epoch_sec)
        best_px, best_gap = None, None
        for sec, px in ring:
            gap = abs(sec - target)
            if gap <= tolerance_sec and (best_gap is None or gap < best_gap):
                best_px, best_gap = px, gap
                if gap == 0:
                    break
        return best_px

    def sample_at(
        self, asset: str, epoch_sec: float, tolerance_sec: float = 3.0,
    ) -> Optional[float]:
        """The ring's ORACLE spot print nearest `epoch_sec` (within
        tolerance), in Chainlink terms, or None."""
        a = (asset or "").upper()
        px = self._nearest(self.samples.get(a), epoch_sec, tolerance_sec)
        return None if px is None else self._adj(a, px)

    def twap_at(
        self, asset: str, epoch_sec: float, tolerance_sec: float = 1.0,
    ) -> Optional[float]:
        """The official 60s TWAP point stamped at `epoch_sec` — a window's
        strike when `epoch_sec` is its open. Tight tolerance: a TWAP a few
        seconds off is a different number from the one that settles."""
        return self._nearest(self.twap_samples.get((asset or "").upper()), epoch_sec, tolerance_sec)

    def window_partial(
        self, asset: str, close_epoch: float, now: Optional[float] = None,
    ) -> tuple[float, int]:
        """(sum, count) of one-per-second ORACLE spot prints observed so far
        inside a window's settlement average (close−60s, close]. (0, 0) when
        nothing observed."""
        a = (asset or "").upper()
        ring = self.samples.get(a)
        if not ring:
            return 0.0, 0
        if now is None:
            now = time.time()
        start = close_epoch - _SETTLE_WINDOW_SEC
        end = min(now, close_epoch)
        total, count = 0.0, 0
        for sec, px in ring:
            if start < sec <= end:
                total += px
                count += 1
        if count and self.spot_sources.get(a) == "pyth":
            total = total / (1.0 + self.basis.get(a, 0.0))
        return total, count

    def stats(self) -> dict:
        return {
            "enabled": not _DISABLED and _WS_IMPORT_OK,
            "connected": self.connected,
            "transport": self.transport if self.connected else "",
            "spotSources": dict(self.spot_sources),
            "basisBps": {a: round(b * 1e4, 3) for a, b in self.basis.items()},
            "assets": sorted(self.fresh_spots()),
            "twapAssets": sorted(a for a in self.twaps if self.twap(a) is not None),
            "lastMsgAgeSec": round(max(0.0, time.time() - self.last_msg_t), 1)
            if self.connected else None,
            "lastError": self.last_error,
        }


_client = _Client()


def start() -> None:
    _client.start()


async def stop() -> None:
    await _client.stop()


def is_connected() -> bool:
    return _client.connected


def is_running() -> bool:
    return _client.is_running()


def spot(asset: str) -> Optional[float]:
    return _client.spot(asset)


def twap(asset: str) -> Optional[float]:
    return _client.twap(asset)


def fresh_spots() -> dict[str, float]:
    return _client.fresh_spots()


def sample_at(asset: str, epoch_sec: float, tolerance_sec: float = 3.0) -> Optional[float]:
    return _client.sample_at(asset, epoch_sec, tolerance_sec)


def twap_at(asset: str, epoch_sec: float, tolerance_sec: float = 1.0) -> Optional[float]:
    return _client.twap_at(asset, epoch_sec, tolerance_sec)


def window_partial(asset: str, close_epoch: float, now: Optional[float] = None) -> tuple[float, int]:
    return _client.window_partial(asset, close_epoch, now)


def stats() -> dict:
    return _client.stats()
