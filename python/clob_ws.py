"""Real-time CLOB market-data feed (public WebSocket).

Maintains a live best-bid/best-ask cache for a set of outcome tokens by
subscribing to Polymarket's public `market` channel — no auth, no orders. This
replaces REST-polling the CLOB `/book` for read-side signals and, critically,
gives the REAL book the bot fills on (vs the lagging Gamma mid the snapshot
decides on — see the Gamma-vs-CLOB divergence finding). Reads are surfaced +
recorded first; nothing here changes a trading decision on its own.

Protocol (verified live 2026-06-28):
  url   wss://ws-subscriptions-clob.polymarket.com/ws/market   (public)
  sub   {"assets_ids": [<token_id>, ...], "type": "market"}
  more  {"assets_ids": [...], "operation": "subscribe"|"unsubscribe"} changes
        the set on the open socket (docs: market-data/realtime-data); a new
        asset is answered with its `book` snapshot
  msgs  event_type "book"  -> {asset_id, bids[], asks[]} full snapshot
        event_type "price_change" -> {price_changes:[{asset_id, best_bid,
              best_ask, ...}]}  (server gives best bid/ask directly per token)
        event_type "tick_size_change" -> ignored here
  ping  send the text "PING" every 10s or the server drops the socket in ~10s

V2 custom features (subscribe with "custom_feature_enabled": true — verified
live 2026-07-16; legacy events keep flowing unchanged):
  best_bid_ask      -> {asset_id, best_bid, best_ask, timestamp ms} — finer-
                       grained top-of-book than price_change; feeds _set().
  last_trade_price  -> {asset_id, price, size, side, timestamp ms,
                       transaction_hash} — buffered for the recorder
                       (drain_trades); we used to DISCARD these, which made
                       every maker/queue/adverse-selection question
                       unanswerable on our own data.
  new_market        -> {market, slug, question, ...} pushed at listing for ALL
                       markets (not just subscribed tokens) — kills the 20-30s
                       Gamma listing lag; buffered (drain_new_markets).
  market_resolved   -> buffered (drain_resolutions) for future settlement
                       accounting; recording only for now.

Design: fully degradable. Any failure (no connection, stale data, parse error)
leaves `get_quote_cents` returning None, so every caller falls back to its
existing REST/Gamma path — behaviour is never worse than before the feed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("clob_ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_PING_INTERVAL = 10.0
_STALE_AFTER = 12.0
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 30.0
_SUB_CONFIRM_SEC = 10.0



def _order_cents(c) -> Optional[int]:
    """A quote in cents that an ORDER could actually use, else None.

    Polymarket accepts prices from 1¢ to 99¢ only (place_limit_order enforces
    it), so 0¢ and 100¢ are not quotes — they are the empty-book sentinels
    (Gamma's `bestBid: 0` / `bestAsk: 1`). Passing one through as a price is
    how a bot crosses to 100¢ against a book with nothing resting in it."""
    if c is None:
        return None
    try:
        c = int(c)
    except (TypeError, ValueError):
        return None
    return c if 1 <= c <= 99 else None


def _best_from_levels(levels, *, is_bid: bool) -> Optional[float]:
    """Best price from a `book` snapshot side: highest bid / lowest ask.
    Levels are [{"price": "0.55", "size": "..."}], price ascending — but we
    don't trust ordering, we min/max explicitly. None if empty/unparseable."""
    prices = []
    for lvl in levels or []:
        try:
            sz = float(lvl.get("size", 0))
            if sz <= 0:
                continue
            prices.append(float(lvl["price"]))
        except (TypeError, ValueError, KeyError):
            continue
    if not prices:
        return None
    return max(prices) if is_bid else min(prices)


def parse_book(msg: dict) -> Optional[tuple[str, Optional[float], Optional[float]]]:
    """A `book` snapshot -> (asset_id, best_bid, best_ask). None if no asset_id."""
    aid = msg.get("asset_id")
    if not aid:
        return None
    bid = _best_from_levels(msg.get("bids"), is_bid=True)
    ask = _best_from_levels(msg.get("asks"), is_bid=False)
    return str(aid), bid, ask


def parse_price_changes(msg: dict) -> list[tuple[str, Optional[float], Optional[float]]]:
    """A `price_change` -> [(asset_id, best_bid, best_ask), ...]. The server
    publishes best_bid/best_ask per token directly, so we just read them."""
    out: list[tuple[str, Optional[float], Optional[float]]] = []
    for pc in msg.get("price_changes") or []:
        aid = pc.get("asset_id")
        if not aid:
            continue

        def _f(k):
            v = pc.get(k)
            try:
                return float(v) if v is not None and v != "" else None
            except (TypeError, ValueError):
                return None

        out.append((str(aid), _f("best_bid"), _f("best_ask")))
    return out


def parse_last_trade(msg: dict) -> Optional[dict]:
    """A `last_trade_price` event -> recorder row, or None if unusable.
    Both the exchange timestamp (ms) and our ingest time are kept — queue and
    latency studies need the pair."""
    aid = msg.get("asset_id")
    try:
        price = float(msg.get("price"))
    except (TypeError, ValueError):
        return None
    if not aid or not (0.0 < price < 1.0):
        return None
    try:
        size = float(msg.get("size")) if msg.get("size") not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    try:
        src_ms = int(msg.get("timestamp")) if msg.get("timestamp") else None
    except (TypeError, ValueError):
        src_ms = None
    return {
        "asset_id": str(aid), "price": price, "size": size,
        "side": (msg.get("side") or "").upper() or None,
        "source_ts_ms": src_ms, "ingest_ts": time.time(),
        "market": msg.get("market"), "tx": msg.get("transaction_hash"),
    }


def _iter_messages(raw: str) -> list[dict]:
    """The feed sends either a single object or a JSON array per frame."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    return []



class ClobMarketFeed:
    def __init__(self) -> None:
        self._books: dict[str, dict] = {}
        self._tokens: dict[str, float] = {}
        self._subscribed: set[str] = set()
        self._pending_subs: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._resub = asyncio.Event()
        self._stop = False
        self._trades: list[dict] = []
        self._new_markets: list[dict] = []
        self._resolutions: list[dict] = []
    _TRADES_MAX = 5000
    _EVENTS_MAX = 500

    def get_quote_cents(self, token_id: Optional[str], max_age: float = _STALE_AFTER) -> Optional[dict]:
        """{'bid_cents','ask_cents'} for a token from the live book, or None if
        unknown/stale (caller then falls back to its REST path)."""
        if not token_id:
            return None
        b = self._books.get(str(token_id))
        if not b:
            return None
        if (time.monotonic() - b["ts"]) > max_age:
            return None
        bid, ask = b.get("bid"), b.get("ask")
        return {
            "bid_cents": int(round(bid * 100)) if bid is not None else None,
            "ask_cents": int(round(ask * 100)) if ask is not None else None,
        }

    def quote_cents_if_fresh(
        self, token_id: Optional[str], max_age: float = _STALE_AFTER
    ) -> Optional[tuple[int, int]]:
        """(bid_cents, ask_cents) for a token — ONLY when the live book is
        fresh AND both sides are real prices. None otherwise.

        This is the ORDER-PATH read: `get_quote_cents` above is the permissive
        display read (either side may be None, sentinels pass through), which is
        fine for a header but not for choosing a limit price. Here a caller gets
        an answer only when the feed can stand in for the REST quote it would
        otherwise pay for:

          • not stale (same `_STALE_AFTER` wall),
          • both sides present — a one-sided book can't price an entry AND an
            exit, and `limit_mid` needs the pair,
          • neither side is an empty-book sentinel. Polymarket only accepts
            orders between 1¢ and 99¢, so a 0¢ bid / 100¢ ask is the venue
            saying "nothing rests here" — the same scrub `terminal._sentinel_free`
            applies to Gamma's `bestBid: 0` / `bestAsk: 1`. Crossing to a
            sentinel "ask" of 100¢ would pay the cap on an empty book.
          • bid <= ask — a crossed/garbled book is ignored, never acted on.

        None means "no opinion": every caller falls back to its REST quote, so
        the websocket is a strict accelerator and never a second authority.
        """
        q = self.get_quote_cents(token_id, max_age=max_age)
        if not q:
            return None
        bid = _order_cents(q.get("bid_cents"))
        ask = _order_cents(q.get("ask_cents"))
        if bid is None or ask is None or bid > ask:
            return None
        return bid, ask

    def _set(self, aid: str, bid: Optional[float], ask: Optional[float],
             *, snapshot: bool = False) -> None:
        self._pending_subs.pop(aid, None)
        if snapshot:
            self._books[aid] = {"bid": bid, "ask": ask, "ts": time.monotonic()}
            return
        cur = self._books.get(aid) or {}
        self._books[aid] = {
            "bid": bid if bid is not None else cur.get("bid"),
            "ask": ask if ask is not None else cur.get("ask"),
            "ts": time.monotonic(),
        }

    def _dispatch(self, msg: dict) -> None:
        et = msg.get("event_type") or msg.get("type")
        if et == "book":
            parsed = parse_book(msg)
            if parsed:
                aid, bid, ask = parsed
                self._set(aid, bid, ask, snapshot=True)
        elif et == "price_change":
            for aid, bid, ask in parse_price_changes(msg):
                self._set(aid, bid, ask)
        elif et == "best_bid_ask":
            aid = msg.get("asset_id")
            if aid:
                def _f(k):
                    try:
                        v = msg.get(k)
                        return float(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        return None
                self._set(str(aid), _f("best_bid"), _f("best_ask"))
        elif et == "last_trade_price":
            row = parse_last_trade(msg)
            if row:
                self._trades.append(row)
                if len(self._trades) > self._TRADES_MAX:
                    del self._trades[: len(self._trades) - self._TRADES_MAX]
        elif et == "new_market":
            self._new_markets.append(msg)
            if len(self._new_markets) > self._EVENTS_MAX:
                del self._new_markets[: len(self._new_markets) - self._EVENTS_MAX]
        elif et == "market_resolved":
            self._resolutions.append(msg)
            if len(self._resolutions) > self._EVENTS_MAX:
                del self._resolutions[: len(self._resolutions) - self._EVENTS_MAX]

    def drain_trades(self) -> list[dict]:
        out, self._trades = self._trades, []
        return out

    def drain_new_markets(self) -> list[dict]:
        out, self._new_markets = self._new_markets, []
        return out

    def drain_resolutions(self) -> list[dict]:
        out, self._resolutions = self._resolutions, []
        return out

    _TOKEN_TTL = 2 * 3600.0

    _BOOKS_MAX = 256

    def observe(self, *token_ids: Optional[str]) -> None:
        """Register interest in tokens; (re)subscribes if the set changed. Cheap
        to call every snapshot — refreshing a known token is a dict write; only
        a genuinely new token (or an expiry) triggers a re-sub."""
        now = time.monotonic()
        grew = False
        for t in token_ids:
            if not t:
                continue
            t = str(t)
            if t not in self._tokens:
                grew = True
            self._tokens[t] = now
        expired = [t for t, ts in self._tokens.items() if now - ts > self._TOKEN_TTL]
        for t in expired:
            del self._tokens[t]
            self._books.pop(t, None)
        if grew or expired:
            if len(self._books) > self._BOOKS_MAX:
                for aid in list(self._books):
                    if aid not in self._tokens:
                        self._books.pop(aid, None)
            self._resub.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        self._resub.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        import websockets
        import ws_ssl

        backoff = _RECONNECT_BASE
        while not self._stop:
            if not self._tokens:
                await asyncio.sleep(0.5)
                continue
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=None, open_timeout=10, close_timeout=5,
                    ssl=ws_ssl.client_context(),
                ) as ws:
                    subscribed = set(self._tokens)
                    await ws.send(json.dumps({
                        "assets_ids": sorted(subscribed), "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    self._subscribed = subscribed
                    self._pending_subs.clear()
                    self._resub.clear()
                    backoff = _RECONNECT_BASE
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
                logger.debug(f"clob_ws connection error: {e}")
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(_RECONNECT_MAX, backoff * 2)

    async def _recv_loop(self, ws) -> None:
        while not self._stop:
            if self._resub.is_set():
                self._resub.clear()
                if not await self._apply_token_changes(ws):
                    return
            if self._subscribe_unanswered():
                logger.debug("clob_ws: a subscribe went unanswered; reconnecting")
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_PING_INTERVAL * 2)
            except asyncio.TimeoutError:
                return
            if raw == "PONG" or not isinstance(raw, (str, bytes)):
                continue
            for msg in _iter_messages(raw if isinstance(raw, str) else raw.decode()):
                self._dispatch(msg)

    async def _apply_token_changes(self, ws) -> bool:
        """Subscribe the open socket to new tokens and unsubscribe it from
        expired ones. False if a frame couldn't be sent."""
        want = set(self._tokens)
        add = sorted(want - self._subscribed)
        drop = sorted(self._subscribed - want)
        try:
            if add:
                await ws.send(json.dumps({
                    "assets_ids": add, "operation": "subscribe",
                    "custom_feature_enabled": True,
                }))
            if drop:
                await ws.send(json.dumps({
                    "assets_ids": drop, "operation": "unsubscribe",
                }))
        except Exception as e:
            logger.debug(f"clob_ws: subscription change not sent ({e}); reconnecting")
            return False
        sent_at = time.monotonic()
        for t in add:
            self._pending_subs[t] = sent_at
        for t in drop:
            self._pending_subs.pop(t, None)
        self._subscribed = want
        return True

    def _subscribe_unanswered(self) -> bool:
        now = time.monotonic()
        return any(now - at > _SUB_CONFIRM_SEC for at in self._pending_subs.values())

    async def _ping_loop(self, ws) -> None:
        while not self._stop:
            await asyncio.sleep(_PING_INTERVAL)
            with contextlib.suppress(Exception):
                await ws.send("PING")


feed = ClobMarketFeed()



def quote_cents_if_fresh(
    token_id: Optional[str], max_age: float = _STALE_AFTER
) -> Optional[tuple[int, int]]:
    """(bid_cents, ask_cents) from the live feed, or None — see
    `ClobMarketFeed.quote_cents_if_fresh`. Never raises: every failure mode
    (feed down, cold, stale, one-sided, sentinel book) is None, and None means
    the caller pays its usual REST quote."""
    try:
        return feed.quote_cents_if_fresh(token_id, max_age=max_age)
    except Exception:
        return None


def warm(*token_ids: Optional[str]) -> None:
    """Subscribe to these tokens so their book is already live when an order
    path asks for it, starting the socket if it isn't running.

    `feed.start()` spawns a task that raises ModuleNotFoundError deep inside
    `_run()` when the optional `websockets` dependency is missing, which
    surfaces as an unretrieved-exception warning on every call and never
    recovers — so check the import first (same guard terminal.subscribe uses).
    Safe to call from anywhere: any failure just leaves callers on REST."""
    try:
        if not any(token_ids):
            return
        feed.observe(*token_ids)
        import websockets  # noqa: F401
        feed.start()
    except Exception as e:
        logger.debug(f"clob_ws warm skipped: {e}")
