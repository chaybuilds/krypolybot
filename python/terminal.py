"""Terminal provider layer — read side.

The bot answers "should I act, right now, on this one thing I am watching?".
The terminal answers "what is going on, and what should I do about it?" — for
ANY market the user names, not just the ones an engine happened to watch.

Three rules govern everything below.

1. THE ONE RULE. A value nobody could produce is ``None``. It is never
   coerced to 0, to a default, or to a plausible-looking guess. Where a value
   is missing the row also carries WHY, and what would fix it.

2. PROVENANCE PER FIELD. Every number carries which provider produced it and
   when WE received it. The merge priority is:
     1. our own authoritative read (the authed CLOB, for our own orders)
     2. our own live feed (clob_ws, stamped on local arrival)
     3. the source that is best at that specific field
     4. None
   A provider returning ``None`` must NEVER erase a better provider's answer,
   which is why the merge goes through ``_Row.put`` and not a dict spread.

3. COST DISCIPLINE. A bot makes a handful of requests; a terminal polls four
   columns, a chart, a book and a portfolio forever. Every outbound call goes
   through a per-host gate that spaces request STARTS by a minimum gap and
   bounds how many may be in flight at once, a TTL cache sized to how fast
   that field actually changes, single-flight coalescing so two identical
   concurrent requests cost one, and a 429 park that fails FAST rather than
   holding the queue while a doomed call retries.

Trading lives in ``terminal_trade.py``. This module never spends money.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import polymarket_api as api
from categorize import categorize_by_keywords

logger = logging.getLogger("krypt.terminal")

_HOSTS: dict[str, dict[str, Any]] = {
    "gamma": {
        "base": api.GAMMA_BASE,
        "host": "gamma-api.polymarket.com",
        "purpose": "Market metadata, discovery lists, resolution rules",
        "min_gap": 0.12,
        "concurrency": 4,
    },
    "clob": {
        "base": api.CLOB_BASE,
        "host": "clob.polymarket.com",
        "purpose": "Order book, price history",
        "min_gap": 0.10,
        "concurrency": 4,
    },
    "data": {
        "base": api.DATA_BASE,
        "host": "data-api.polymarket.com",
        "purpose": "Public trade tape, wallet positions",
        "min_gap": 0.15,
        "concurrency": 4,
    },
    "kalshi": {
        "base": "https://api.elections.kalshi.com/trade-api/v2",
        "host": "api.elections.kalshi.com",
        "purpose": "Kalshi public market data (cross-venue comparison)",
        "min_gap": 0.20,
        "concurrency": 1,
    },
    "discord": {
        "base": "https://discord.com/api/v10",
        "host": "discord.com",
        "purpose": "Discord bot — your commands and replies (only if enabled)",
        "min_gap": 0.0,
        "concurrency": 1,
    },
    "telegram": {
        "base": "https://api.telegram.org",
        "host": "api.telegram.org",
        "purpose": "Telegram bot — your commands and replies (only if enabled)",
        "min_gap": 0.0,
        "concurrency": 1,
    },
    "anthropic": {
        "base": "https://api.anthropic.com/v1",
        "host": "api.anthropic.com",
        "purpose": "AI market analysis — public market text (only if enabled)",
        "min_gap": 0.5,
        "concurrency": 1,
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "host": "api.openai.com",
        "purpose": "AI market analysis — public market text (only if enabled)",
        "min_gap": 0.5,
        "concurrency": 1,
    },
}

TTL_DISCOVER = 25.0
TTL_MARKET = 12.0
TTL_BOOK = 3.0
TTL_HISTORY = 45.0
TTL_TAPE = 8.0
TTL_EVENT = 120.0

_PARK_SECS = 30.0
_CACHE_MAX = 600

_offline = False


def set_offline(v: bool) -> None:
    global _offline
    _offline = bool(v)


def is_offline() -> bool:
    return _offline


OFFLINE_REASON = (
    "Offline mode is on, so nothing was requested from any provider. Turn it "
    "off in Settings → Data & privacy to load live market data."
)


class _Gate:
    """Per-host pacing: a minimum gap between request STARTS, a bound on how
    many requests may be in flight at once, and a 429 park.

    The two controls are separate on purpose, and the split is what makes a
    terminal usable. The lock covers ONLY the spacing decision — work out the
    next allowed start, sleep the gap, stamp the start — and is released
    before the HTTP call. Holding it across the round trip as well made every
    request to a host strictly serial, so a Discover load needing ~20 Gamma
    calls cost 20 × (RTT + min_gap): five to eight seconds of staring at
    skeletons for work that overlaps almost perfectly. The semaphore is what
    keeps "not serial" from becoming "unbounded"; ``min_gap`` still governs
    the starts, so the cost discipline is unchanged — we simply stop waiting
    for each answer before asking the next question.

    Parking is checked BEFORE either primitive is taken. A parked host
    therefore fails instantly instead of queueing behind a retry — the bug
    this exists to prevent is a Discover refresh 429ing the provider and
    starving the chart, which then renders blank while a plain curl to the
    same endpoint returns 200.
    """

    def __init__(self, key: str, min_gap: float, concurrency: int = 1) -> None:
        self.key = key
        self.min_gap = min_gap
        self.concurrency = max(1, int(concurrency))
        self._lock: Optional[asyncio.Lock] = None
        self._slots: Optional[asyncio.Semaphore] = None
        self.last = 0.0
        self.parked_until = 0.0
        self.calls = 0
        self.errors = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self._lat: list[float] = []

    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def slot(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.concurrency)
        return self._slots

    def parked(self) -> bool:
        return time.monotonic() < self.parked_until

    def park(self, secs: float = _PARK_SECS) -> None:
        until = time.monotonic() + secs
        if until <= self.parked_until:
            return
        self.parked_until = until
        logger.info("terminal: parked %s for %.0fs after 429", self.key, secs)

    def note_latency(self, ms: float) -> None:
        self._lat.append(ms)
        if len(self._lat) > 50:
            del self._lat[: len(self._lat) - 50]

    def latency_ms(self) -> Optional[float]:
        if not self._lat:
            return None
        s = sorted(self._lat)
        return round(s[len(s) // 2], 1)


_gates: dict[str, _Gate] = {
    k: _Gate(k, float(v["min_gap"]), int(v.get("concurrency", 1)))
    for k, v in _HOSTS.items()
}

_cache: dict[str, tuple[float, Any, str]] = {}

_inflight: dict[str, "asyncio.Future"] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cache_key(host: str, path: str, params: Optional[dict]) -> str:
    return f"{host}{path}?{json.dumps(params or {}, sort_keys=True, default=str)}"


def _cache_get(key: str) -> Optional[tuple[Any, str]]:
    hit = _cache.get(key)
    if not hit:
        return None
    expires, value, observed = hit
    if time.monotonic() > expires:
        _cache.pop(key, None)
        return None
    return value, observed


def _cache_put(key: str, value: Any, ttl: float) -> str:
    observed = _now_iso()
    if len(_cache) >= _CACHE_MAX:
        for k in list(_cache.keys())[: _CACHE_MAX // 3]:
            _cache.pop(k, None)
    _cache[key] = (time.monotonic() + ttl, value, observed)
    return observed


async def _fetch(
    host: str, path: str, params: Optional[dict] = None, *,
    ttl: float = TTL_MARKET, timeout: float = 10.0, json_body: Any = None,
) -> tuple[Any, Optional[str]]:
    """GET through the cache + per-host gate — or a POST of `json_body`, for
    the CLOB's batch reads, which take their token list as a body.

    Returns ``(data, observed_at_iso)``. ``data`` is ``None`` on every failure
    mode — parked host, network error, 4xx, 5xx — and the caller turns that
    into an honest ``None`` field with a reason, never into a zero.
    """
    spec = _HOSTS.get(host)
    gate = _gates.get(host)
    if not spec or not gate:
        return None, None

    if _offline:
        return None, None

    key = _cache_key(host, path, params if json_body is None
                     else {"params": params, "body": json_body})
    hit = _cache_get(key)
    if hit is not None:
        gate.cache_hits += 1
        return hit[0], hit[1]
    gate.cache_misses += 1

    if gate.parked():
        return None, None

    pending = _inflight.get(key)
    if pending is not None:
        gate.cache_hits += 1
        return await asyncio.shield(pending)

    task: asyncio.Future = asyncio.ensure_future(
        _fetch_now(gate, spec, host, path, params, key, ttl=ttl, timeout=timeout,
                   json_body=json_body))
    _inflight[key] = task

    def _finished(done: "asyncio.Future") -> None:
        _inflight.pop(key, None)
        if not done.cancelled():
            exc = done.exception()
            if exc is not None:
                logger.debug("terminal fetch %s%s raised: %s", host, path, exc)

    task.add_done_callback(_finished)
    return await asyncio.shield(task)


async def _fetch_now(
    gate: _Gate, spec: dict, host: str, path: str, params: Optional[dict],
    key: str, *, ttl: float, timeout: float, json_body: Any = None,
) -> tuple[Any, Optional[str]]:
    """One real request: pace it, make it, count it, cache it.

    The caller has already resolved the cache, the offline switch and the
    park. This is the only place that touches the network.
    """
    async with gate.slot():
        if gate.parked():
            return None, None

        async with gate.lock():
            if gate.parked():
                return None, None
            wait = gate.min_gap - (time.monotonic() - gate.last)
            if wait > 0:
                await asyncio.sleep(wait)
            if gate.parked():
                return None, None
            started = time.monotonic()
            gate.last = started
            gate.calls += 1

        url = f"{spec['base']}{path}"
        try:
            client = await api._get_client()
            if json_body is not None:
                resp = await client.post(url, params=params, json=json_body, timeout=timeout)
            else:
                resp = await client.get(url, params=params, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — every transport failure is a None field
            gate.errors += 1
            logger.debug("terminal fetch %s%s failed: %s", host, path, e)
            return None, None

        gate.note_latency((time.monotonic() - started) * 1000.0)

        if resp.status_code == 429:
            gate.errors += 1
            gate.park()
            return None, None
        if resp.status_code >= 400:
            gate.errors += 1
            return None, None
        try:
            data = resp.json()
        except Exception:
            gate.errors += 1
            return None, None

    observed = _cache_put(key, data, ttl)
    return data, observed


def note_external_call(host_key: str, *, ok: bool = True,
                       ms: Optional[float] = None) -> None:
    """Count a request made OUTSIDE _fetch (the chat bots).

    Without this the privacy panel would list Discord and Telegram at zero
    calls forever while cheerfully relaying positions to both.
    """
    g = _gates.get(host_key)
    if not g:
        return
    g.calls += 1
    if not ok:
        g.errors += 1
    if ms is not None:
        g.note_latency(ms)


def health() -> dict:
    """Live per-host call/error/latency counts. Powers the privacy panel: the
    user can see exactly which hosts this machine talks to and how often."""
    out = []
    for k, spec in _HOSTS.items():
        g = _gates[k]
        parked = None
        if g.parked():
            remaining = g.parked_until - time.monotonic()
            parked = datetime.fromtimestamp(
                time.time() + remaining, timezone.utc
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        out.append({
            "host": spec["host"],
            "purpose": spec["purpose"],
            "calls": g.calls,
            "errors": g.errors,
            "latencyMs": g.latency_ms(),
            "parkedUntil": parked,
            "cacheHits": g.cache_hits,
            "cacheMisses": g.cache_misses,
        })
    return {
        "providers": out,
        "offline": _offline,
        "fetchedAt": _now_iso(),
    }



def num(v: Any) -> Optional[float]:
    """float(v), or None. An unparseable value is UNKNOWN, not zero."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def cents(v: Any) -> Optional[float]:
    """A 0..1 probability as 0..100 cents, or None."""
    f = num(v)
    if f is None:
        return None
    return round(f * 100.0, 2)


def _sentinel_free(c: Optional[float]) -> Optional[float]:
    """Drop the 0¢ / 100¢ placeholders Gamma emits for an empty book.

    Polymarket only accepts orders between 1¢ and 99¢ (place_limit_order
    enforces it), so neither extreme is ever a real quote — they are the
    provider's way of saying "nothing is resting here". Passing them through
    produced a specific, confident lie on every unlisted market: bid 0¢, ask
    100¢, and a 100¢ spread presented as fact. Caught by the live check, not
    by any unit test, because it needed a market that genuinely has no book.
    """
    if c is None:
        return None
    if c <= 0.0 or c >= 100.0:
        return None
    return c


def _jlist(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _iso(v: Any) -> Optional[str]:
    if not v or not isinstance(v, str):
        return None
    return v


def _hours_to(close_iso: Optional[str]) -> Optional[float]:
    if not close_iso:
        return None
    try:
        s = close_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return round((dt - datetime.now(timezone.utc)).total_seconds() / 3600.0, 3)


PRIO_OWN_AUTHED = 0
PRIO_OWN_FEED = 1
PRIO_BEST_SOURCE = 2
PRIO_FALLBACK = 3


class _Row:
    """Accumulates a market row with per-field provenance.

    ``put`` is the only way in. It drops ``None`` (so a failed provider cannot
    blank a good answer) and drops a worse-priority write (so Gamma's stale
    indicative mid cannot stomp our own live top-of-book).
    """

    def __init__(self) -> None:
        self.vals: dict[str, Any] = {}
        self.sources: dict[str, dict] = {}
        self._prio: dict[str, int] = {}
        self.unknowns: list[dict] = []

    def put(self, field: str, value: Any, source: str, prio: int,
            observed_at: Optional[str] = None) -> None:
        if value is None:
            return
        if field in self._prio and self._prio[field] <= prio:
            return
        self.vals[field] = value
        self._prio[field] = prio
        obs = observed_at or _now_iso()
        self.sources[field] = {
            "source": source,
            "observedAt": obs,
            "ageSec": _age_sec(obs),
        }

    def get(self, field: str) -> Any:
        return self.vals.get(field)

    def unknown(self, field: str, reason: str) -> None:
        """Record WHY a field is missing. 'No data' is useless; write the
        sentence that tells the user what would fix it."""
        if field in self.vals:
            return
        if any(u["field"] == field for u in self.unknowns):
            return
        self.unknowns.append({"field": field, "reason": reason})


def _age_sec(observed_at: Optional[str]) -> Optional[float]:
    if not observed_at:
        return None
    try:
        dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return round((datetime.now(timezone.utc) - dt).total_seconds(), 1)



def summary_from_gamma(raw: dict, observed_at: Optional[str] = None) -> Optional[dict]:
    """Build a MarketSummary from one Gamma market object, preserving nulls.

    Deliberately NOT ``polymarket_api._normalize_market``: that one is the
    engine's normaliser and coerces every missing number to 0.0, which is
    exactly the lie this whole module exists to avoid. We do call it for its
    side effect (it warms the tradable-meta cache the order path reads).
    """
    if not isinstance(raw, dict):
        return None
    condition_id = raw.get("conditionId") or raw.get("condition_id") or ""
    if not condition_id:
        return None

    try:
        api._normalize_market(raw)
    except Exception:
        pass

    row = _Row()
    prices = _jlist(raw.get("outcomePrices"))
    outcomes = _jlist(raw.get("outcomes"))
    token_ids = _jlist(raw.get("clobTokenIds"))

    yes_price = cents(prices[0]) if len(prices) > 0 else None
    if yes_price is None:
        row.unknown(
            "probability",
            "Gamma returned no outcome price for this market. It is usually "
            "unpriced until the book has its first quote.",
        )
    row.put("probability", yes_price, "gamma", PRIO_FALLBACK, observed_at)

    best_bid = _sentinel_free(cents(raw.get("bestBid")))
    best_ask = _sentinel_free(cents(raw.get("bestAsk")))
    row.put("bid", best_bid, "gamma", PRIO_FALLBACK, observed_at)
    row.put("ask", best_ask, "gamma", PRIO_FALLBACK, observed_at)
    if best_bid is None or best_ask is None:
        row.unknown(
            "bid" if best_bid is None else "ask",
            "Gamma reports the extremes (0¢ / 100¢) for a market with no "
            "resting orders. Neither is a price anyone could trade, so this "
            "side is unknown rather than quoted.",
        )

    v24 = num(raw.get("volume24hr"))
    row.put("volume24h", v24, "gamma", PRIO_BEST_SOURCE, observed_at)
    if v24 is None:
        row.unknown(
            "volume24h",
            "Gamma omits 24h volume on markets that have not traded in that "
            "window. It is unknown here, not zero.",
        )
    row.put("volumeTotal", num(raw.get("volumeNum")) if raw.get("volumeNum") is not None
            else num(raw.get("volume")), "gamma", PRIO_BEST_SOURCE, observed_at)
    liq = num(raw.get("liquidityNum"))
    if liq is None:
        liq = num(raw.get("liquidity"))
    row.put("liquidity", liq, "gamma", PRIO_BEST_SOURCE, observed_at)
    if liq is None:
        row.unknown(
            "liquidity",
            "Gamma reported no liquidity figure. Open the book to see the "
            "depth that actually exists.",
        )

    d1 = num(raw.get("oneDayPriceChange"))
    h1 = num(raw.get("oneHourPriceChange"))
    row.put("priceChange24h", None if d1 is None else d1 * 100.0,
            "gamma", PRIO_BEST_SOURCE, observed_at)
    row.put("priceChange1h", None if h1 is None else h1 * 100.0,
            "gamma", PRIO_BEST_SOURCE, observed_at)
    if d1 is None:
        row.unknown(
            "priceChange24h",
            "Gamma reports no 24h price change for this market — usually "
            "because it has not traded in that window. Unknown, not flat.",
        )

    close_time = _iso(raw.get("endDate")) or _iso(raw.get("endDateIso"))
    row.put("closeTime", close_time, "gamma", PRIO_BEST_SOURCE, observed_at)
    if close_time is None:
        row.unknown(
            "closeTime",
            "This market has no end date on Gamma — common for markets that "
            "resolve on an event rather than a date.",
        )

    closed = bool(raw.get("closed"))
    uma = str(raw.get("umaResolutionStatus") or "").lower()
    resolved_outcome: Optional[str] = None
    if closed and len(prices) >= 2:
        p0, p1 = num(prices[0]), num(prices[1])
        if p0 is not None and p0 >= 0.99:
            resolved_outcome = "yes"
        elif p1 is not None and p1 >= 0.99:
            resolved_outcome = "no"

    if closed:
        status = "settled" if (resolved_outcome or uma == "resolved") else "closed"
    elif raw.get("active") is False:
        status = "inactive"
    elif raw.get("active") is None and raw.get("closed") is None:
        status = "unknown"
    else:
        status = "open"
    row.put("status", status, "gamma", PRIO_BEST_SOURCE, observed_at)

    category = ""
    events = raw.get("events") or []
    if isinstance(events, list) and events:
        tags = events[0].get("tags") or []
        for t in tags:
            slug = str((t or {}).get("slug") or "").lower()
            if slug:
                category = categorize_by_keywords(slug, slug)
                if category:
                    break
    if not category:
        category = categorize_by_keywords(
            str(raw.get("question") or ""), str(raw.get("slug") or ""))

    event_slug = ""
    if isinstance(events, list) and events:
        event_slug = events[0].get("slug") or events[0].get("ticker") or ""
    if not event_slug:
        event_slug = raw.get("eventSlug") or ""

    bid, ask = row.get("bid"), row.get("ask")
    spread = round(ask - bid, 2) if (bid is not None and ask is not None) else None
    mid = round((ask + bid) / 2.0, 2) if (bid is not None and ask is not None) else None
    row.put("spread", spread, "local", PRIO_BEST_SOURCE, observed_at)
    row.put("mid", mid, "local", PRIO_BEST_SOURCE, observed_at)
    if spread is None:
        row.unknown(
            "spread",
            "A spread needs BOTH a bid and an ask. One side is unknown, so "
            "there is no honest spread to show — half a spread is not one.",
        )

    return {
        "conditionId": str(condition_id),
        "venue": "polymarket",
        "title": str(raw.get("question") or raw.get("slug") or condition_id),
        "slug": raw.get("slug") or None,
        "eventSlug": event_slug or None,
        "category": category or None,
        "status": status,
        "outcomes": [str(outcomes[0]), str(outcomes[1])] if len(outcomes) >= 2 else None,
        "probability": row.get("probability"),
        "bid": row.get("bid"),
        "ask": row.get("ask"),
        "spread": row.get("spread"),
        "mid": row.get("mid"),
        "volume24hUsd": row.get("volume24h"),
        "volumeTotalUsd": row.get("volumeTotal"),
        "liquidityUsd": row.get("liquidity"),
        "priceChange24h": row.get("priceChange24h"),
        "priceChange1h": row.get("priceChange1h"),
        "closeTime": close_time,
        "hoursToClose": _hours_to(close_time),
        "negRisk": bool(raw.get("negRisk")) if raw.get("negRisk") is not None else None,
        "tickSize": num(raw.get("orderPriceMinTickSize")),
        "minOrderSize": num(raw.get("orderMinSize")),
        "yesTokenId": str(token_ids[0]) if len(token_ids) > 0 else None,
        "noTokenId": str(token_ids[1]) if len(token_ids) > 1 else None,
        "resolvedOutcome": resolved_outcome,
        "sources": row.sources,
        "unknowns": row.unknowns,
        "_raw": raw,
    }


def _strip_raw(s: dict) -> dict:
    out = dict(s)
    out.pop("_raw", None)
    return out


_MICRO_MAX = 900
_MICRO_TTL = 20 * 60.0
_MICRO_PERIOD = 1.0

_micro: dict[str, dict] = {}
_micro_task: Optional[asyncio.Task] = None


def subscribe(condition_id: str, yes_token: Optional[str], no_token: Optional[str]) -> None:
    """Name the tokens the open screen cares about. Idempotent and cheap —
    call it on every poll; only a genuinely new token costs anything."""
    try:
        import clob_ws
    except Exception:
        return
    now = time.monotonic()
    for side, tok in (("yes", yes_token), ("no", no_token)):
        if not tok:
            continue
        entry = _micro.get(tok)
        if entry is None:
            _micro[tok] = {"samples": [], "last_named": now,
                           "condition": condition_id, "side": side}
        else:
            entry["last_named"] = now
    clob_ws.feed.observe(yes_token, no_token)
    try:
        import websockets  # noqa: F401
    except Exception:
        return
    clob_ws.feed.start()
    _ensure_recorder()


def _ensure_recorder() -> None:
    global _micro_task
    if _micro_task is None or _micro_task.done():
        try:
            _micro_task = asyncio.ensure_future(_record_loop())
        except RuntimeError:
            _micro_task = None


async def _record_loop() -> None:
    """Sample the live top-of-book once a second for every named token.

    Never invents a sample. When the feed has no fresh quote for a token the
    period simply produces nothing — a flat synthetic level in an illiquid
    market is how somebody buys a corpse.
    """
    while True:
        await asyncio.sleep(_MICRO_PERIOD)
        if not _micro:
            continue
        now_mono = time.monotonic()
        now_ts = time.time()
        for tok in list(_micro.keys()):
            entry = _micro[tok]
            if now_mono - entry["last_named"] > _MICRO_TTL:
                _micro.pop(tok, None)
                continue
            q = _live_quote(tok)
            if not q:
                continue
            bid, ask = q.get("bid_cents"), q.get("ask_cents")
            if bid is None and ask is None:
                continue
            samples: list = entry["samples"]
            samples.append((now_ts, bid, ask))
            if len(samples) > _MICRO_MAX:
                del samples[: len(samples) - _MICRO_MAX]


async def stop_recorder() -> None:
    """Cancel the microstructure sampler. Everything else the backend starts is
    cancelled on shutdown; this was not, so a clean exit left a task running on
    a closing loop."""
    global _micro_task
    t, _micro_task = _micro_task, None
    _micro.clear()
    if t and not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


def micro_series(condition_id: str, side: str = "yes") -> dict:
    """Our own microstructure for one market: spread over time, built from
    locally-timestamped quotes. Empty until the screen has been open a moment,
    and it says so rather than pretending."""
    for tok, entry in _micro.items():
        if entry["condition"] == condition_id and entry["side"] == side:
            samples = list(entry["samples"])
            pts = [
                {"t": int(ts), "bid": b, "ask": a,
                 "spread": (round(a - b, 2) if (a is not None and b is not None) else None)}
                for ts, b, a in samples
            ]
            spreads = [p["spread"] for p in pts if p["spread"] is not None]
            return {
                "conditionId": condition_id,
                "side": side,
                "points": pts,
                "medianSpreadCents": (
                    round(sorted(spreads)[len(spreads) // 2], 2) if spreads else None),
                "sampleCount": len(pts),
                "source": "clob-ws",
                "note": (
                    None if len(pts) >= 10 else
                    "Recording started when you opened this market — the series "
                    "fills in as quotes arrive."
                ),
            }
    return {
        "conditionId": condition_id, "side": side, "points": [],
        "medianSpreadCents": None, "sampleCount": 0, "source": "clob-ws",
        "note": "Not recording this market yet. It starts when the page opens.",
    }


def _live_quote(token_id: Optional[str]) -> Optional[dict]:
    """Top-of-book from our own feed, or None when it is stale/absent.

    The SAME 0¢/100¢ sentinel scrub Gamma needs applies here. Caught by
    driving the real app: on a market whose book had no asks at all, the feed
    reported an ask of 100¢, which produced a confident "spread 1.0¢" in the
    header AND a 1.0¢ median in our own microstructure panel — while the book
    right next to it correctly said "no resting asks". Scrubbing here rather
    than at each call site means no future reader of the feed can reintroduce
    it.
    """
    if not token_id:
        return None
    try:
        import clob_ws
    except Exception:
        return None
    try:
        q = clob_ws.feed.get_quote_cents(token_id)
    except Exception:
        return None
    if not q:
        return None
    bid = _sentinel_free(num(q.get("bid_cents")))
    ask = _sentinel_free(num(q.get("ask_cents")))
    if bid is None and ask is None:
        return None
    return {"bid_cents": bid, "ask_cents": ask}


def _overlay_live(summary: dict) -> dict:
    """Let our own feed win over Gamma's indicative price where it is fresh.

    This is merge priority 2 beating priority 3 — and it is the one place the
    terminal shows a number a hosted competitor using the same public REST
    cannot: a quote stamped when we received it, with no indexer lag.
    """
    q = _live_quote(summary.get("yesTokenId"))
    if not q:
        return summary
    bid, ask = num(q.get("bid_cents")), num(q.get("ask_cents"))
    if bid is None and ask is None:
        return summary
    obs = _now_iso()
    stamp = {"source": "clob-ws", "observedAt": obs, "ageSec": 0.0}
    if bid is not None:
        summary["bid"] = bid
        summary["sources"]["bid"] = dict(stamp)
    if ask is not None:
        summary["ask"] = ask
        summary["sources"]["ask"] = dict(stamp)
    b, a = summary.get("bid"), summary.get("ask")
    if b is not None and a is not None:
        summary["spread"] = round(a - b, 2)
        summary["mid"] = round((a + b) / 2.0, 2)
        summary["sources"]["spread"] = {"source": "local", "observedAt": obs, "ageSec": 0.0}
        summary["sources"]["mid"] = {"source": "local", "observedAt": obs, "ageSec": 0.0}
        summary["probability"] = summary["mid"]
        summary["sources"]["probability"] = {
            "source": "clob-ws", "observedAt": obs, "ageSec": 0.0}
        summary["unknowns"] = [u for u in summary.get("unknowns", [])
                               if u["field"] not in ("spread", "probability")]
    return summary


_NUMERIC_FILTERS: list[tuple[str, str, str]] = [
    ("minVolume24hUsd", "volume24hUsd", "min"),
    ("maxVolume24hUsd", "volume24hUsd", "max"),
    ("minLiquidityUsd", "liquidityUsd", "min"),
    ("minProbability", "probability", "min"),
    ("maxProbability", "probability", "max"),
    ("minHoursToClose", "hoursToClose", "min"),
    ("maxHoursToClose", "hoursToClose", "max"),
]


def apply_filters(rows: list[dict], filters: Optional[dict]) -> tuple[list[dict], int, int]:
    """→ (kept, skipped_for_unknown, excluded).

    The two counters are surfaced in the UI. A filter that silently drops rows
    it could not evaluate reads as "there is nothing here", which is a lie
    about the market rather than about our data.
    """
    if not filters:
        return list(rows), 0, 0

    kept: list[dict] = []
    skipped = 0
    excluded = 0

    cats = [str(c).lower() for c in (filters.get("categories") or []) if c]
    search = str(filters.get("search") or "").strip().lower()

    for r in rows:
        if cats:
            rc = r.get("category")
            if rc is None or str(rc).lower() not in cats:
                excluded += 1
                continue
        if search:
            hay = f"{r.get('title') or ''} {r.get('slug') or ''}".lower()
            if search not in hay:
                excluded += 1
                continue

        skip = False
        for fkey, field, comp in _NUMERIC_FILTERS:
            bound = filters.get(fkey)
            if bound is None:
                continue
            val = r.get(field)
            if val is None:
                skip = True
                break
            if comp == "min" and val < bound:
                skip = True
                break
            if comp == "max" and val > bound:
                skip = True
                break
        if skip:
            skipped += 1
            continue
        kept.append(r)

    return kept, skipped, excluded


_GAMMA_PAGE = 100


async def _gamma_markets(params: dict, ttl: float = TTL_DISCOVER) -> tuple[list[dict], Optional[str]]:
    data, observed = await _fetch("gamma", "/markets", params, ttl=ttl)
    if not isinstance(data, list):
        return [], observed
    out = []
    for raw in data:
        s = summary_from_gamma(raw, observed)
        if s:
            out.append(s)
    return out, observed


async def discover(column: str, limit: int = 40,
                   filters: Optional[dict] = None) -> dict:
    """One Discover column, honestly filtered.

    Sorting note (a trap that cost real time on the crypto build): asking a
    provider to sort by a stored metric returns stale garbage — year-old dead
    listings reporting huge numbers. So every column here sorts by an ACTIVITY
    field the venue keeps current, then we re-rank locally on the metric the
    column is actually about.
    """
    limit = max(1, min(int(limit or 40), 100))
    note: Optional[str] = None
    rows: list[dict] = []
    observed: Optional[str] = None

    if column == "watchlist":
        entries = watchlist_list()
        ids = [e["conditionId"] for e in entries]
        rows = await _summaries_for(ids)
        if rows:
            note = None
        elif not ids:
            note = "Nothing on your watchlist yet — star a market to add it."
        else:
            note = (
                f"You have {len(ids)} market(s) on your watchlist, but none "
                f"could be loaded just now. This is a provider failure, not an "
                f"empty list — your watchlist is intact."
            )
        if rows and len(rows) < len(ids):
            note = (
                f"{len(ids) - len(rows)} of your {len(ids)} watched market(s) "
                f"could not be loaded right now. They are still on the list."
            )

    elif column == "new":
        params = {
            "limit": _GAMMA_PAGE, "active": "true", "closed": "false",
            "order": "startDate", "ascending": "false",
        }
        rows, observed = await _gamma_markets(params)
        rows.sort(key=lambda r: str((r.get("_raw") or {}).get("startDate") or ""), reverse=True)
        note = (
            "Just-listed markets usually have no book yet, so their price and "
            "spread read as unknown until somebody quotes them. That is the "
            "market being empty, not our data being missing."
        )

    elif column == "closing":
        params = {
            "limit": _GAMMA_PAGE, "active": "true", "closed": "false",
            "order": "endDate", "ascending": "true",
            "end_date_min": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        rows, observed = await _gamma_markets(params)
        rows.sort(key=lambda r: (r.get("hoursToClose") is None,
                                 r.get("hoursToClose") if r.get("hoursToClose") is not None else 0))

    elif column == "volume":
        params = {
            "limit": _GAMMA_PAGE, "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
        }
        rows, observed = await _gamma_markets(params)
        rows.sort(key=lambda r: (r.get("volume24hUsd") is None,
                                 -(r.get("volume24hUsd") or 0.0)))

    elif column == "volatility":
        params = {
            "limit": _GAMMA_PAGE, "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
        }
        rows, observed = await _gamma_markets(params)
        rows.sort(key=lambda r: (r.get("priceChange24h") is None,
                                 -abs(r.get("priceChange24h") or 0.0)))
        unranked = sum(1 for r in rows if r.get("priceChange24h") is None)
        movers = len(rows) - unranked
        if movers:
            note = (
                "Ranked by how far the price moved in 24h, up or down — the "
                "direction is yours to read."
                + (f" {unranked} market(s) here report no 24h change and are "
                   f"listed last; that is unknown, not flat." if unranked else "")
            )
        else:
            note = (
                f"None of the {len(rows)} markets on the active page reports a "
                f"24h price change right now, so nothing can be ranked by "
                f"movement. That is a gap in Gamma's data, not a calm board."
            )

    else:
        return {
            "column": column, "rows": [], "fetchedAt": _now_iso(),
            "skippedForUnknown": 0, "excluded": 0,
            "note": f"Unknown column '{column}'.",
        }

    if not rows and note is None:
        g = _gates["gamma"]
        note = (
            OFFLINE_REASON if _offline else
            "Gamma is rate-limiting us right now — this column will refill "
            "automatically in about 30 seconds."
            if g.parked() else
            "The market list came back empty. That is a provider result, not "
            "a filter result."
        )

    kept, skipped, excluded = apply_filters(rows, filters)
    kept = kept[:limit]
    for r in kept:
        _overlay_live(r)

    if _offline and not kept:
        note = OFFLINE_REASON

    return {
        "column": column,
        "rows": [_strip_raw(r) for r in kept],
        "fetchedAt": observed or _now_iso(),
        "skippedForUnknown": skipped,
        "excluded": excluded,
        "note": note,
    }


async def _summaries_for(condition_ids: Iterable[str]) -> list[dict]:
    """Fetch several markets by condition id, concurrently but gated.

    Gamma's /markets defaults to ``closed=false``, so a plain condition_ids
    lookup returns NOTHING once a market has resolved. Left unhandled that
    silently deletes a settled market from the watchlist — the entry is still
    stored, it just stops rendering, which reads as "you never starred it".
    So: whatever the first pass misses is retried including closed markets.
    """
    ids = [str(c) for c in condition_ids if c]
    if not ids:
        return []
    out: list[dict] = []
    found: set[str] = set()

    async def _pass(chunk: list[str], include_closed: bool) -> None:
        params: dict = {"condition_ids": chunk, "limit": len(chunk)}
        if include_closed:
            params["closed"] = "true"
        data, observed = await _fetch("gamma", "/markets", params, ttl=TTL_MARKET)
        if not isinstance(data, list):
            return
        for raw in data:
            s = summary_from_gamma(raw, observed)
            if s and s["conditionId"] not in found:
                found.add(s["conditionId"])
                out.append(s)

    for i in range(0, len(ids), 20):
        await _pass(ids[i:i + 20], include_closed=False)
    missing = [i for i in ids if i not in found]
    for i in range(0, len(missing), 20):
        await _pass(missing[i:i + 20], include_closed=True)
    return out


async def search(query: str, limit: int = 30) -> dict:
    """Search by text, or resolve a pasted condition id directly.

    A user who pastes a 0x… condition id means that market, not a text match,
    so try the exact lookup first.
    """
    q = str(query or "").strip()
    if not q:
        return {"column": "new", "rows": [], "fetchedAt": _now_iso(),
                "skippedForUnknown": 0, "excluded": 0, "note": "Type something to search."}

    if re.fullmatch(r"0x[0-9a-fA-F]{40,}", q):
        rows = await _summaries_for([q])
        if not rows:
            data, observed = await _fetch(
                "gamma", "/markets",
                {"condition_ids": q, "closed": "true", "limit": 1}, ttl=TTL_MARKET)
            if isinstance(data, list):
                rows = [s for s in (summary_from_gamma(r, observed) for r in data) if s]
        for r in rows:
            _overlay_live(r)
        return {
            "column": "new", "rows": [_strip_raw(r) for r in rows],
            "fetchedAt": _now_iso(), "skippedForUnknown": 0, "excluded": 0,
            "note": None if rows else "No market with that condition id.",
        }

    try:
        found, observed = await _fetch(
            "gamma", "/public-search",
            {"q": q, "limit_per_type": 20, "events_status": "active"}, ttl=TTL_DISCOVER)
    except Exception:
        found, observed = None, None
    hits: list[dict] = []
    for ev in (found or {}).get("events") or [] if isinstance(found, dict) else []:
        for raw in ev.get("markets") or []:
            if raw.get("closed") or not raw.get("active", True):
                continue
            row = summary_from_gamma(raw, observed)
            if row:
                hits.append(row)
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    if hits:
        for r in hits:
            _overlay_live(r)
        return {
            "column": "volume", "rows": [_strip_raw(r) for r in hits],
            "fetchedAt": observed or _now_iso(), "skippedForUnknown": 0, "excluded": 0,
            "note": None,
        }

    rows, observed = await _gamma_markets(
        {"limit": _GAMMA_PAGE, "active": "true", "closed": "false",
         "order": "volume24hr", "ascending": "false"}, ttl=TTL_DISCOVER)
    ql = q.lower()
    hits = [r for r in rows
            if ql in f"{r.get('title') or ''} {r.get('slug') or ''}".lower()][:limit]
    for r in hits:
        _overlay_live(r)
    return {
        "column": "volume",
        "rows": [_strip_raw(r) for r in hits],
        "fetchedAt": observed or _now_iso(),
        "skippedForUnknown": 0, "excluded": 0,
        "note": (
            None if hits else
            "No match in the actively-traded markets. Search covers the live "
            "book, not the full archive — paste a condition id for an exact "
            "market."
        ),
    }



_LAST_MARKET_MAX = 500
_LAST_MARKET_TTL = 600.0
_last_market: dict[str, tuple[float, dict]] = {}


def _last_market_put(cid: str, s: dict) -> None:
    if len(_last_market) >= _LAST_MARKET_MAX and cid not in _last_market:
        for k in list(_last_market)[: _LAST_MARKET_MAX // 3]:
            _last_market.pop(k, None)
    _last_market[cid] = (time.monotonic(), dict(s))


def _last_market_get(cid: str) -> Optional[dict]:
    hit = _last_market.get(cid)
    if not hit:
        return None
    age = time.monotonic() - hit[0]
    if age > _LAST_MARKET_TTL:
        _last_market.pop(cid, None)
        return None
    s = dict(hit[1])
    s["metaStaleSec"] = round(age, 1)
    return s


async def market(condition_id: str, *, include_raw: bool = False) -> Optional[dict]:
    """One market, fully assembled. Also names its tokens to the live feed so
    the microstructure series starts recording while the page is open."""
    cid = str(condition_id or "").strip()
    if not cid:
        return None
    rows = await _summaries_for([cid])
    if not rows:
        data, observed = await _fetch(
            "gamma", "/markets",
            {"condition_ids": cid, "closed": "true", "limit": 1}, ttl=TTL_MARKET)
        if isinstance(data, list) and data:
            s = summary_from_gamma(data[0], observed)
            rows = [s] if s else []
    if rows:
        _last_market_put(cid, rows[0])
    else:
        stale = _last_market_get(cid)
        if stale is None:
            return None
        rows = [stale]
    s = rows[0]
    subscribe(cid, s.get("yesTokenId"), s.get("noTokenId"))
    _overlay_live(s)
    return s if include_raw else _strip_raw(s)


def _levels(raw_levels: Any, *, descending: bool) -> list[dict]:
    """Normalise one side of a CLOB book into cumulative levels.

    Providers emit duplicate price levels in a single response; collapse them
    (sum the size) and sort, or every cumulative figure downstream is wrong.
    """
    by_price: dict[float, float] = {}
    for lvl in raw_levels or []:
        try:
            p = round(float(lvl["price"]) * 100.0, 2)
            sz = float(lvl["size"])
        except (TypeError, ValueError, KeyError):
            continue
        if sz <= 0:
            continue
        by_price[p] = by_price.get(p, 0.0) + sz

    out: list[dict] = []
    cum = 0.0
    for p in sorted(by_price, reverse=descending):
        sz = by_price[p]
        cum += sz
        out.append({
            "priceCents": p,
            "size": round(sz, 2),
            "cumulative": round(cum, 2),
            "notionalUsd": round(sz * p / 100.0, 2),
        })
    return out


async def book(condition_id: str, side: str = "yes") -> dict:
    """Full depth for one outcome token.

    ``available: false`` means the venue returned nothing. That is NOT an empty
    book, and the UI has to be able to tell them apart — a market with no
    resting orders and a market we could not read look identical otherwise.
    """
    side = "no" if str(side).lower() == "no" else "yes"
    empty = {
        "conditionId": condition_id, "tokenSide": side,
        "bids": [], "asks": [], "bestBid": None, "bestAsk": None,
        "spreadCents": None, "midCents": None,
        "bidDepthUsd": None, "askDepthUsd": None,
        "available": False, "fetchedAt": _now_iso(), "source": "clob",
        "note": None,
    }

    meta = await api.get_market_meta(str(condition_id))
    token = (meta or {}).get(f"{side}_token")
    if not token:
        empty["note"] = (
            "No CLOB token id for this outcome — the market may not be listed "
            "for trading yet."
        )
        return empty

    data, observed = await _fetch("clob", "/book", {"token_id": token}, ttl=TTL_BOOK)
    if not isinstance(data, dict):
        g = _gates["clob"]
        empty["note"] = (
            OFFLINE_REASON if _offline else
            "The CLOB is rate-limiting us — the book will refresh in about "
            "30 seconds." if g.parked() else
            "The CLOB did not return a book for this token. This is a failed "
            "read, not an empty book."
        )
        return empty

    bids = _levels(data.get("bids"), descending=True)
    asks = _levels(data.get("asks"), descending=False)
    best_bid = bids[0]["priceCents"] if bids else None
    best_ask = asks[0]["priceCents"] if asks else None
    spread = round(best_ask - best_bid, 2) if (best_bid is not None and best_ask is not None) else None
    mid = round((best_ask + best_bid) / 2.0, 2) if spread is not None else None

    note = None
    if not bids and not asks:
        note = "The book is genuinely empty — nobody has a resting order here."
    elif best_bid is None:
        note = "No resting bids. You can offer, but nothing is there to hit."
    elif best_ask is None:
        note = "No resting asks. You can bid, but nothing is there to lift."

    return {
        "conditionId": condition_id, "tokenSide": side,
        "bids": bids, "asks": asks,
        "bestBid": best_bid, "bestAsk": best_ask,
        "spreadCents": spread, "midCents": mid,
        "bidDepthUsd": round(sum(l["notionalUsd"] for l in bids), 2) if bids else None,
        "askDepthUsd": round(sum(l["notionalUsd"] for l in asks), 2) if asks else None,
        "available": True,
        "fetchedAt": observed or _now_iso(),
        "source": "clob",
        "note": note,
    }


_BOOKS_BATCH = 20


async def _clob_tops(tokens: list[str]) -> dict[str, tuple]:
    """token → (best bid ¢, best ask ¢, observedAt) from the CLOB, for every
    token it returned a book for. A side with no resting orders is None."""
    out: dict[str, tuple] = {}
    for i in range(0, len(tokens), _BOOKS_BATCH):
        chunk = tokens[i:i + _BOOKS_BATCH]
        data, observed = await _fetch("clob", "/books", ttl=TTL_BOOK,
                                      json_body=[{"token_id": t} for t in chunk])
        books = [(b, observed) for b in data] if isinstance(data, list) else None
        if books is None:
            got = await asyncio.gather(*[
                _fetch("clob", "/book", {"token_id": t}, ttl=TTL_BOOK) for t in chunk])
            books = [(d, o) for d, o in got if isinstance(d, dict)]
        for b, obs in books:
            if not isinstance(b, dict) or str(b.get("asset_id") or "") not in chunk:
                continue
            bids = _levels(b.get("bids"), descending=True)
            asks = _levels(b.get("asks"), descending=False)
            out[str(b["asset_id"])] = (bids[0]["priceCents"] if bids else None,
                                       asks[0]["priceCents"] if asks else None,
                                       obs or _now_iso())
    return out


def _put_quote(row: dict, bid: Optional[float], ask: Optional[float],
               source: str, observed: str) -> None:
    """Overwrite a row's quote with a read of the book. An empty side is an
    answer (nobody is resting there), so it replaces Gamma's number with
    None rather than leaving a price nobody would trade at."""
    sources = row.setdefault("sources", {})
    unknowns = row.setdefault("unknowns", [])
    stamp = {"source": source, "observedAt": observed, "ageSec": _age_sec(observed)}
    row["bid"], row["ask"] = bid, ask
    for field, v in (("bid", bid), ("ask", ask)):
        sources[field] = dict(stamp)
        if v is None and not any(u["field"] == field for u in unknowns):
            unknowns.append({"field": field, "reason": (
                f"The live book has no resting {'bids' if field == 'bid' else 'offers'} "
                f"on this market.")})
    if bid is not None and ask is not None:
        row["spread"] = round(ask - bid, 2)
        row["mid"] = round((ask + bid) / 2.0, 2)
        row["probability"] = row["mid"]
        for field in ("spread", "mid", "probability"):
            sources[field] = dict(stamp)
    else:
        row["spread"] = row["mid"] = None


async def clob_quotes(rows: list[dict]) -> list[dict]:
    """Re-quote MarketSummary rows from the live CLOB, in place.

    Our own feed first when it is fresh (no request at all), then ONE batched
    book read for every remaining market, so a portfolio of N markets is not
    N round trips. A row the CLOB cannot price keeps its Gamma quote, whose
    `sources` entry says it is Gamma's and how old it is. Settled rows are
    left alone: their price is the payout."""
    need: dict[str, list[dict]] = {}
    for r in rows:
        tok = r.get("yesTokenId")
        if not tok or r.get("resolvedOutcome") is not None:
            continue
        q = _live_quote(str(tok))
        if q and q.get("bid_cents") is not None and q.get("ask_cents") is not None:
            _put_quote(r, q["bid_cents"], q["ask_cents"], "clob-ws", _now_iso())
            continue
        need.setdefault(str(tok), []).append(r)
    if need:
        for tok, (bid, ask, observed) in (await _clob_tops(list(need))).items():
            for r in need.get(tok, []):
                _put_quote(r, bid, ask, "clob", observed)
    return rows


async def marked_summaries(condition_ids: Iterable[str]) -> list[dict]:
    """`_summaries_for`, re-quoted from the live book — for marks."""
    return await clob_quotes(await _summaries_for(condition_ids))


_INTERVAL_SPEC = {
    "1h": ("1h", 1),
    "6h": ("6h", 5),
    "1d": ("1d", 10),
    "1w": ("1w", 60),
    "max": ("max", 180),
}


def _dedupe_points(points: list[dict]) -> list[dict]:
    """Sort ascending and collapse duplicate timestamps.

    Providers really do emit the same bucket twice in one response, and every
    charting library throws on a non-ascending series. Normalise at the source
    so no downstream renderer has to know.
    """
    by_t: dict[int, float] = {}
    for p in points:
        t = p.get("t")
        v = p.get("p")
        if t is None or v is None:
            continue
        try:
            by_t[int(t)] = float(v)
        except (TypeError, ValueError):
            continue
    return [{"t": t, "p": by_t[t]} for t in sorted(by_t)]


async def price_history(condition_id: str, side: str = "yes",
                        interval: str = "1d") -> dict:
    """Probability over time, 0..100.

    Falls back to our OWN tape when the venue index has nothing. The rule that
    matters: *any* real data beats none. A threshold that discards our own
    samples because there are only two of them, then reports "no data" while
    holding the only tape that exists, is the worst possible outcome — so a
    thin series is returned and LABELLED thin.
    """
    side = "no" if str(side).lower() == "no" else "yes"
    iv = interval if interval in _INTERVAL_SPEC else "1d"
    clob_iv, fidelity = _INTERVAL_SPEC[iv]

    base = {
        "conditionId": condition_id, "side": side, "interval": iv,
        "points": [], "source": "clob", "ownFeed": False,
        "thin": False, "note": None,
    }

    meta = await api.get_market_meta(str(condition_id))
    token = (meta or {}).get(f"{side}_token")
    if not token:
        base["note"] = "No CLOB token for this outcome, so there is no price history to read."
        return base

    data, observed = await _fetch(
        "clob", "/prices-history",
        {"market": token, "interval": clob_iv, "fidelity": fidelity},
        ttl=TTL_HISTORY)

    points: list[dict] = []
    if isinstance(data, dict):
        for h in data.get("history") or []:
            t, p = h.get("t"), cents(h.get("p"))
            if t is None or p is None:
                continue
            points.append({"t": int(t), "p": p})
    points = _dedupe_points(points)

    if points:
        base["points"] = points
        base["source"] = "clob"
        base["thin"] = len(points) < 5
        if base["thin"]:
            base["note"] = (
                f"Only {len(points)} price point(s) in this window — the market "
                "has barely traded. This is real data, not a smoothed guess."
            )
        return base

    own = micro_series(condition_id, side)
    own_pts = [
        {"t": p["t"], "p": (p["ask"] + p["bid"]) / 2.0}
        for p in own["points"]
        if p.get("bid") is not None and p.get("ask") is not None
    ]
    own_pts = _dedupe_points(own_pts)
    if own_pts:
        base.update({
            "points": own_pts, "source": "clob-ws", "ownFeed": True,
            "thin": len(own_pts) < 30,
            "note": (
                "Polymarket has no indexed history for this market at this "
                f"resolution. These {len(own_pts)} point(s) are our own live "
                "feed, timestamped when we received each quote."
            ),
        })
        return base

    g = _gates["clob"]
    base["note"] = (
        OFFLINE_REASON if _offline else
        "The CLOB is rate-limiting us — history will load in about 30 seconds."
        if g.parked() else
        "No price history exists for this market yet. Leave this page open and "
        "our own feed will start building one from live quotes."
    )
    return base


async def tape(condition_id: str, limit: int = 40) -> dict:
    """Recent public trades for one market."""
    limit = max(1, min(int(limit or 40), 200))
    body, observed = await _fetch(
        "data", "/v2/trades",
        {"condition": str(condition_id), "limit": limit}, ttl=TTL_TAPE)
    data = None
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        data = [api.v2_row_to_v1(r) for r in body["data"]]

    if not isinstance(data, list):
        g = _gates["data"]
        return {
            "conditionId": condition_id, "trades": [], "fetchedAt": _now_iso(),
            "note": (
                OFFLINE_REASON if _offline else
                "The Data API is rate-limiting us — the tape resumes in about "
                "30 seconds." if g.parked() else
                "Could not read the public trade tape for this market."
            ),
        }

    trades = []
    for t in data:
        if not isinstance(t, dict):
            continue
        idx = api.outcome_index_of(t)
        if idx is None:
            continue
        side = "yes" if idx == 0 else "no"
        price = cents(t.get("price"))
        size = num(t.get("size"))
        ts = t.get("timestamp")
        try:
            ts_iso = datetime.fromtimestamp(int(ts), timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            ts_iso = _now_iso()
        raw_side = str(t.get("side") or "").lower()
        wallet = str(t.get("proxyWallet") or t.get("maker") or "")
        trades.append({
            "ts": ts_iso,
            "side": side,
            "action": raw_side if raw_side in ("buy", "sell") else None,
            "priceCents": price,
            "size": size,
            "notionalUsd": (round(price * size / 100.0, 2)
                            if (price is not None and size is not None) else None),
            "taker": (wallet[:6] + "…" + wallet[-4:]) if len(wallet) > 12 else (wallet or None),
            "source": "data-api",
        })

    return {
        "conditionId": condition_id, "trades": trades,
        "fetchedAt": observed or _now_iso(),
        "note": None if trades else "No public trades recorded for this market yet.",
    }


MIN_RESOLVED_CHECKS = 4

_LEVEL_SCORE = {"ok": 100.0, "watch": 55.0, "risk": 10.0}

_AMBIGUOUS = [
    "at the discretion", "reasonable", "generally", "approximately", "about ",
    "widely reported", "consensus", "credible", "substantially", "significant",
    "in the opinion", "deemed", "may be", "or similar", "etc.",
]

_STRONG_SOURCES = [
    "associated press", "reuters", "bloomberg", "espn", "official",
    "sec.gov", ".gov", "nasa", "noaa", "cftc", "federalreserve",
]


def _check(cid: str, label: str, level: str, detail: str, resolved: bool = True) -> dict:
    return {"id": cid, "label": label, "level": level, "detail": detail,
            "resolved": resolved}


async def resolution_report(condition_id: str) -> dict:
    """Eleven checks on how this market will actually settle."""
    s = await market(condition_id, include_raw=True)
    if not s:
        return {
            "conditionId": condition_id, "checks": [], "resolvedCount": 0,
            "totalCount": 0, "score": None,
            "scoreNote": "Could not read this market, so no check could run.",
            "resolverName": None, "resolutionSource": None,
            "fetchedAt": _now_iso(),
        }

    raw: dict = s.get("_raw") or {}
    checks: list[dict] = []
    desc = str(raw.get("description") or "")
    question = str(raw.get("question") or "")
    res_source = str(raw.get("resolutionSource") or "").strip()

    if res_source:
        strong = any(k in res_source.lower() for k in _STRONG_SOURCES)
        checks.append(_check(
            "source", "Resolution source named",
            "ok" if strong else "watch",
            res_source if strong else
            f"Resolves on “{res_source}”. Not one of the sources that "
            "historically settles cleanly — read the rules before sizing up."))
    elif desc:
        checks.append(_check(
            "source", "Resolution source named", "watch",
            "No explicit resolution source field. The rules text below is the "
            "only statement of how this settles."))
    else:
        checks.append(_check(
            "source", "Resolution source named", "unknown",
            "Gamma returned neither a resolution source nor a description for "
            "this market.", resolved=False))

    if desc:
        n = len(desc)
        checks.append(_check(
            "rules", "Rules text present",
            "ok" if n >= 200 else "watch",
            f"{n} characters of rules." + (
                "" if n >= 200 else
                " That is short for a market that has to settle on a fact — "
                "short rules are where disputes come from.")))
    else:
        checks.append(_check(
            "rules", "Rules text present", "risk",
            "This market ships with no rules text at all. Nothing states how "
            "it settles."))

    blob = f"{question} {desc}".lower()
    found = [w.strip() for w in _AMBIGUOUS if w in blob]
    if desc or question:
        checks.append(_check(
            "ambiguity", "Wording is unambiguous",
            "ok" if not found else ("risk" if len(found) >= 3 else "watch"),
            "No judgement-language found in the question or rules."
            if not found else
            f"Judgement language: {', '.join(sorted(set(found))[:6])}. "
            "Each phrase hands the resolver a decision instead of a fact."))
    else:
        checks.append(_check(
            "ambiguity", "Wording is unambiguous", "unknown",
            "No text to read.", resolved=False))

    close = s.get("closeTime")
    hrs = s.get("hoursToClose")
    if close and hrs is not None:
        if hrs > 0:
            checks.append(_check(
                "settlement", "Settlement date set", "ok",
                f"Closes {close} — {hrs:.1f}h away."))
        else:
            checks.append(_check(
                "settlement", "Settlement date set",
                "watch" if hrs > -168 else "risk",
                f"Closed {abs(hrs):.0f}h ago and has not posted an outcome. "
                "Long gaps between close and settlement usually mean the "
                "resolver is waiting on a source, or a dispute is open."))
    else:
        checks.append(_check(
            "settlement", "Settlement date set", "unknown",
            "No end date on this market — it resolves on an event, not a "
            "date, so there is no schedule to check against.", resolved=False))

    uma = str(raw.get("umaResolutionStatus") or "").strip()
    if uma:
        low = uma.lower()
        level = "ok" if low in ("resolved", "settled") else (
            "risk" if "dispute" in low or "challenge" in low else "watch")
        checks.append(_check(
            "oracle", "Oracle status", level,
            f"UMA status: {uma}." + (
                " A disputed market can settle against the price you see."
                if level == "risk" else "")))
    else:
        checks.append(_check(
            "oracle", "Oracle status", "unknown",
            "Gamma exposes no UMA status for this market yet. It normally "
            "appears once the market closes.", resolved=False))

    accepting = raw.get("acceptingOrders")
    if accepting is None:
        checks.append(_check(
            "accepting", "Accepting orders", "unknown",
            "Gamma did not report an order-acceptance flag.", resolved=False))
    else:
        checks.append(_check(
            "accepting", "Accepting orders",
            "ok" if accepting else "watch",
            "The CLOB is accepting orders." if accepting else
            "Orders are NOT being accepted right now, so any price you see is "
            "historical, not tradable."))

    spread = s.get("spread")
    if spread is None:
        checks.append(_check(
            "spread", "Tradable spread", "unknown",
            "One side of the book is empty, so there is no spread to judge.",
            resolved=False))
    else:
        checks.append(_check(
            "spread", "Tradable spread",
            "ok" if spread <= 3 else ("watch" if spread <= 10 else "risk"),
            f"{spread:.1f}¢ wide." + (
                "" if spread <= 3 else
                " At that width the mid is not a price anyone can actually "
                "trade at.")))

    liq = s.get("liquidityUsd")
    if liq is None:
        checks.append(_check(
            "liquidity", "Liquidity present", "unknown",
            "Gamma reported no liquidity figure for this market.",
            resolved=False))
    else:
        checks.append(_check(
            "liquidity", "Liquidity present",
            "ok" if liq >= 20000 else ("watch" if liq >= 2000 else "risk"),
            f"${liq:,.0f} of resting liquidity." + (
                "" if liq >= 20000 else
                " Thin books move on small orders, and exiting can cost more "
                "than entering.")))

    v24 = s.get("volume24hUsd")
    if v24 is None:
        checks.append(_check(
            "activity", "Recent activity", "unknown",
            "Gamma omits 24h volume for markets that have not traded in that "
            "window — so this is unknown, not zero.", resolved=False))
    else:
        checks.append(_check(
            "activity", "Recent activity",
            "ok" if v24 >= 10000 else ("watch" if v24 >= 500 else "risk"),
            f"${v24:,.0f} traded in the last 24h."))

    checks.append(await _negrisk_check(s))

    checks.append(await _sibling_check(s))

    resolved = [c for c in checks if c["resolved"]]
    total = len(checks)
    if len(resolved) >= MIN_RESOLVED_CHECKS:
        score = round(sum(_LEVEL_SCORE.get(c["level"], 50.0) for c in resolved)
                      / len(resolved), 1)
        note = (f"averaged over the {len(resolved)} of {total} checks that "
                f"resolved")
    else:
        score = None
        note = (f"only {len(resolved)} of {total} checks resolved — fewer than "
                f"the {MIN_RESOLVED_CHECKS} needed for a score to mean "
                f"anything, so none is shown")

    return {
        "conditionId": condition_id,
        "checks": checks,
        "resolvedCount": len(resolved),
        "totalCount": total,
        "score": score,
        "scoreNote": note,
        "resolverName": (str(raw.get("resolvedBy")) if raw.get("resolvedBy") else None),
        "resolutionSource": res_source or None,
        "fetchedAt": _now_iso(),
    }


async def _event_markets(event_slug: Optional[str]) -> list[dict]:
    if not event_slug:
        return []
    data, observed = await _fetch(
        "gamma", "/events", {"slug": event_slug, "limit": 1}, ttl=TTL_EVENT)
    if not isinstance(data, list) or not data:
        return []
    out = []
    for raw in (data[0].get("markets") or []):
        s = summary_from_gamma(raw, observed)
        if s:
            out.append(s)
    return out


async def _negrisk_check(s: dict) -> dict:
    if not s.get("negRisk"):
        return _check(
            "negrisk", "Neg-risk group coherent", "ok",
            "Standalone market — no mutually-exclusive group to reconcile.")
    sibs = await _event_markets(s.get("eventSlug"))
    priced = [m.get("probability") for m in sibs if m.get("probability") is not None]
    if len(priced) < 2:
        return _check(
            "negrisk", "Neg-risk group coherent", "unknown",
            "This is a neg-risk market but we could not price enough of its "
            "siblings to check that the group sums to 100¢.", resolved=False)
    total = sum(priced)
    drift = abs(total - 100.0)
    return _check(
        "negrisk", "Neg-risk group coherent",
        "ok" if drift <= 4 else ("watch" if drift <= 12 else "risk"),
        f"{len(priced)} outcomes sum to {total:.1f}¢ (exactly one wins, so it "
        f"should be ~100¢)." + (
            "" if drift <= 4 else
            f" That is {drift:.1f}¢ off — either the group is mispriced or "
            "some legs are unpriced."))


async def _sibling_check(s: dict) -> dict:
    sibs = [m for m in await _event_markets(s.get("eventSlug"))
            if m.get("conditionId") != s.get("conditionId")]
    if not sibs:
        return _check(
            "sibling", "No contradicting sibling", "unknown",
            "No other markets found in this event to cross-check against.",
            resolved=False)
    mine = s.get("probability")
    if mine is None:
        return _check(
            "sibling", "No contradicting sibling", "unknown",
            "This market has no price, so there is nothing to contradict.",
            resolved=False)
    if s.get("negRisk"):
        return _check(
            "sibling", "No contradicting sibling", "ok",
            f"{len(sibs)} sibling market(s) in this mutually-exclusive group. "
            f"Legs are supposed to disagree, so their spread is not a "
            f"contradiction — the group sum is the check that matters.")
    worst = None
    for m in sibs:
        p = m.get("probability")
        if p is None:
            continue
        if _title_overlap(s.get("title") or "", m.get("title") or "") >= 0.7:
            gap = abs(p - mine)
            if worst is None or gap > worst[0]:
                worst = (gap, m)
    if worst is None:
        return _check(
            "sibling", "No contradicting sibling", "ok",
            f"{len(sibs)} sibling market(s) in this event, none asking a "
            "near-identical question.")
    gap, m = worst
    return _check(
        "sibling", "No contradicting sibling",
        "ok" if gap <= 5 else ("watch" if gap <= 15 else "risk"),
        f"“{m['title'][:70]}” is priced {gap:.1f}¢ away on a near-identical "
        f"question." + ("" if gap <= 5 else
                        " One of the two is wrong, and it might be this one."))


async def related(condition_id: str, limit: int = 25) -> dict:
    """The other markets in this market's event, priced.

    The resolution report already computes these to look for a contradiction
    and then throws the list away. Surfacing it is nearly free and it is the
    closest thing to cross-venue arbitrage available inside one venue: a
    sibling asking a near-identical question at a very different price means
    one of the two is wrong, and it might be the one you are about to trade.
    """
    s = await market(condition_id, include_raw=True)
    if not s:
        return {"conditionId": condition_id, "eventSlug": None, "rows": [],
                "sumProbability": None, "negRisk": None,
                "fetchedAt": _now_iso(),
                "note": "Could not read this market."}

    sibs = await _event_markets(s.get("eventSlug"))
    mine = s.get("probability")
    rows = []
    for m in sibs:
        if m.get("conditionId") == s.get("conditionId"):
            continue
        p = m.get("probability")
        overlap = _title_overlap(s.get("title") or "", m.get("title") or "")
        gap = (abs(p - mine)
               if (p is not None and mine is not None and overlap >= 0.7
                   and not s.get("negRisk"))
               else None)
        rows.append({
            **_strip_raw(m),
            "titleOverlap": round(overlap, 2),
            "gapCents": (round(gap, 1) if gap is not None else None),
            "contradicts": bool(gap is not None and gap > 15),
        })

    rows.sort(key=lambda r: (
        r["gapCents"] is None, -(r["gapCents"] or 0.0),
        -(r.get("volume24hUsd") or 0.0)))
    rows = rows[:limit]

    by_id: dict[str, float] = {}
    for m in list(sibs) + [s]:
        p = m.get("probability")
        if p is not None and m.get("conditionId"):
            by_id[str(m["conditionId"])] = p
    total = round(math.fsum(by_id.values()), 1) if len(by_id) >= 2 else None

    note = None
    if not rows:
        note = (
            "No other markets in this event to cross-check against."
            if s.get("eventSlug") else
            "This market has no parent event, so there is nothing to compare "
            "it with.")
    return {
        "conditionId": condition_id,
        "eventSlug": s.get("eventSlug"),
        "rows": rows,
        "sumProbability": total,
        "negRisk": s.get("negRisk"),
        "fetchedAt": _now_iso(),
        "note": note,
    }


_ANTONYMS = [
    ("increase", "decrease"), ("up", "down"), ("above", "below"),
    ("over", "under"), ("higher", "lower"), ("more", "less"),
    ("win", "lose"), ("yes", "no"), ("rise", "fall"), ("gain", "drop"),
    ("before", "after"), ("positive", "negative"),
]


_POLARITY_UP = {
    "increase", "increases", "increased", "hike", "hikes", "raise", "raises",
    "rise", "rises", "above", "over", "higher", "more", "gain", "gains",
    "exceed", "exceeds", "up", "upward", "positive", "win", "wins", "yes",
}
_POLARITY_DOWN = {
    "decrease", "decreases", "decreased", "cut", "cuts", "lower", "lowers",
    "fall", "falls", "below", "under", "drop", "drops", "less", "fewer",
    "down", "downward", "negative", "lose", "loses",
}


def polarity(text: str) -> Optional[str]:
    """'up' | 'down' | None — the direction a question claims.

    None when the text carries BOTH directions ("Bitcoin Up or Down") or
    neither: in both cases there is no directional claim to contradict, and
    guessing one would create false opposites.
    """
    w = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    up, down = w & _POLARITY_UP, w & _POLARITY_DOWN
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return None


def _are_opposites(a: str, b: str) -> bool:
    """True when two titles differ by an opposing term.

    Caught live: the related-markets panel flagged "increase by 25 bps" as
    contradicting "decrease by 25 bps" at a 33c gap. They are mutually
    exclusive legs of one event — the gap is the market working correctly, not
    a mispricing, and calling it a contradiction is a confident wrong claim in
    exactly the panel that exists to be trusted.
    """
    pa, pb = polarity(a), polarity(b)
    if pa and pb and pa != pb:
        return True
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    for x, y in _ANTONYMS:
        if (x in wa and y in wb) or (y in wa and x in wb):
            if not ((x in wa and y in wa) or (x in wb and y in wb)):
                return True
    return False


def _title_overlap(a: str, b: str) -> float:
    """Crude token-overlap similarity, 0..1. Good enough to spot two phrasings
    of the same question inside one event; deliberately not a fuzzy matcher.

    Returns 0 for titles that are OPPOSITES, so a mutually-exclusive pair is
    never read as two phrasings of the same question."""
    if _are_opposites(a, b):
        return 0.0
    wa = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 2}
    wb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


_WATCH_KEY = "terminal:watchlist"
_WATCH_MAX = 200


def _kv_json(key: str, default: Any) -> Any:
    try:
        import db
        with db.get_db() as conn:
            v = db.kv_get(conn, key)
        if not v:
            return default
        return json.loads(v)
    except Exception:
        return default


def _kv_put(key: str, value: Any) -> None:
    import db
    with db.get_db() as conn:
        db.kv_set(conn, key, json.dumps(value))


def watchlist_list() -> list[dict]:
    entries = _kv_json(_WATCH_KEY, [])
    return entries if isinstance(entries, list) else []


def watchlist_add(condition_id: str, title: Optional[str] = None,
                  note: Optional[str] = None) -> list[dict]:
    cid = str(condition_id or "").strip()
    if not cid:
        return watchlist_list()
    entries = [e for e in watchlist_list() if e.get("conditionId") != cid]
    entries.insert(0, {
        "conditionId": cid, "title": title or None,
        "addedAt": _now_iso(), "note": note or None,
    })
    entries = entries[:_WATCH_MAX]
    _kv_put(_WATCH_KEY, entries)
    return entries


def watchlist_remove(condition_id: str) -> list[dict]:
    cid = str(condition_id or "").strip()
    entries = [e for e in watchlist_list() if e.get("conditionId") != cid]
    _kv_put(_WATCH_KEY, entries)
    return entries
