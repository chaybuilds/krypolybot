"""Polymarket API client — public reads + authenticated CLOB trading.

Three independent Polymarket services are used:

  Gamma   (gamma-api.polymarket.com)  — market/event metadata, discovery,
                                         tags/categories, resolution status.
  Data    (data-api.polymarket.com)   — the public trades feed (whale
                                         tracking), wallet positions, value.
  CLOB    (clob.polymarket.com)        — order books, live prices, and all
                                         authenticated trading (orders/fills).

To keep the rest of the bot (scanner, trader) almost unchanged, the read
functions here return dicts keyed the way that engine code already expects:

  market identifier  -> `ticker`            (we use the on-chain conditionId)
  prices             -> `*_dollars` (0..1 fraction)
  sizes / volume     -> `*_fp`
  taker direction    -> `taker_side` ("yes"/"no")

A binary Polymarket market has two outcomes (index 0 = "yes/up/first",
index 1 = "no/down/second"), each an ERC1155 token. The token ids live in
the Gamma `clobTokenIds` array (index-aligned with `outcomes`). Trading a
"yes" position = BUY of token[0]; "no" = BUY of token[1].

Public market data is read from the production endpoints (no auth). Signed
trading calls use the L1/L2 scheme in `polymarket_auth`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Optional

import httpx

import polymarket_auth as auth
from categorize import categorize_by_keywords

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
GAMMA_PAGE_MAX = 100
DATA_BASE = "https://data-api.polymarket.com"
DATA_V2 = f"{DATA_BASE}/v2"
CLOB_BASE = "https://clob.polymarket.com"

POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://polygon.gateway.tenderly.co",
    "https://polygon.api.onfinality.io/public",
    "https://1rpc.io/matic",
    "https://polygon-pokt.nodies.app",
    "https://rpc-mainnet.matic.quiknode.pro",
]

USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NATIVE_USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cc03d5c3359"

REQUEST_TIMEOUT = 12.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

_pub_client: Optional[httpx.AsyncClient] = None

_net_fail_streak = 0
_NET_FAIL_RECYCLE = 5
_last_client_recycle = 0.0
_CLIENT_RECYCLE_MIN_GAP = 20.0
_last_net_ok = 0.0
_NET_STALL_SECS = 45.0

_get_fail_log_at: dict = {}
_GET_FAIL_LOG_GAP = 30.0


def _log_get_failed(url: str, e: Exception) -> None:
    now = time.monotonic()
    last = _get_fail_log_at.get(url)
    if last is not None and now - last < _GET_FAIL_LOG_GAP:
        return
    _get_fail_log_at[url] = now
    logger.warning(f"GET failed {url}: {type(e).__name__}: {e}")


async def _get_client() -> httpx.AsyncClient:
    global _pub_client
    if _pub_client is None or _pub_client.is_closed:
        _pub_client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": "KryptPolyBot/1.0",
            },
            limits=httpx.Limits(max_connections=48, max_keepalive_connections=24),
            follow_redirects=True,
        )
    return _pub_client


def _note_net_ok() -> None:
    """A response (any status) came back, so the transport/pool is healthy —
    clear the failure streak and mark the moment of last success."""
    global _net_fail_streak, _last_net_ok
    _net_fail_streak = 0
    _last_net_ok = time.monotonic()


async def _note_net_fail() -> None:
    """Record a terminal network failure. Once the streak shows every endpoint is
    failing (pool wedged) AND nothing has succeeded for a real stall window,
    rebuild the shared client's connection pool — the in-process equivalent of
    the restart users currently do by hand."""
    global _net_fail_streak, _last_client_recycle
    _net_fail_streak += 1
    if _net_fail_streak < _NET_FAIL_RECYCLE:
        return
    now = time.monotonic()
    if _last_net_ok and (now - _last_net_ok) < _NET_STALL_SECS:
        return
    if now - _last_client_recycle < _CLIENT_RECYCLE_MIN_GAP:
        return
    _last_client_recycle = now
    _net_fail_streak = 0
    logger.warning(
        "network failures across all endpoints — rebuilding HTTP client "
        "(fresh connection pool) to clear a poisoned pool"
    )
    await _recycle_client()


_extra_recycle_hooks: list = []


def register_recycle_hook(fn) -> None:
    """fn: async callable that closes/renews that module's HTTP client."""
    if fn not in _extra_recycle_hooks:
        _extra_recycle_hooks.append(fn)


async def _recycle_client() -> None:
    """Swap in a fresh client on the next request, then close the old one.
    Swapping to None FIRST means concurrent callers immediately get a clean
    client; the dead one is closed in the background (errors swallowed)."""
    global _pub_client
    old, _pub_client = _pub_client, None
    if old is not None and not old.is_closed:
        try:
            await old.aclose()
        except Exception:
            pass
    for hook in list(_extra_recycle_hooks):
        try:
            res = hook()
            if asyncio.iscoroutine(res) or asyncio.isfuture(res):
                await res
        except Exception:
            pass


async def close_clients() -> None:
    global _net_fail_streak
    _net_fail_streak = 0
    await _recycle_client()


class PolymarketAPIError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


_geoblocked_at = 0.0


def geoblock_active() -> bool:
    return (time.time() - _geoblocked_at) < 1800.0


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
REGION_TTL_SEC = 600.0
REGION_UNKNOWN_TTL_SEC = 60.0
_BLOCKED_COUNTRIES = frozenset({"IR", "SY", "CU", "KP"})
_BLOCKED_REGIONS = frozenset({"UA-43", "UA-14", "UA-09"})
_CLOSE_ONLY_COUNTRIES = frozenset({
    "AU", "BY", "BE", "BI", "BR", "CF", "CD", "ET", "FR", "DE", "IQ", "IT", "LB",
    "LY", "MM", "NZ", "NI", "PL", "RU", "SG", "SO", "SK", "SS", "SD", "TW", "TH",
    "GB", "US", "UM", "VE", "YE", "ZW",
})
_CLOSE_ONLY_REGIONS = frozenset({"CA-BC", "CA-ON", "CA-AB", "CA-QC"})
_REGION_NAMES = {
    "BRITISH COLUMBIA": "BC", "ONTARIO": "ON", "ALBERTA": "AB", "QUEBEC": "QC",
    "QUÉBEC": "QC", "CRIMEA": "43", "DONETSK": "14", "LUHANSK": "09",
}

REGION_BLOCKED_MSG = (
    "Polymarket doesn't allow trading from the region this connection is in, "
    "so live orders would all be refused.")
REGION_CLOSE_ONLY_MSG = (
    "Polymarket only allows closing existing positions from the region this "
    "connection is in: new buys would be refused, sells of what you already "
    "hold still work.")
REGION_UNKNOWN_MSG = (
    "Polymarket's region check could not be reached, so whether this "
    "connection may trade is unconfirmed.")
REGION_REFUSED_MSG = (
    "Polymarket refused a test order because of the region this connection is "
    "in, so live orders would all be refused.")

_region_state: dict = {"value": None, "at": 0.0, "task": None}


def _region_code(country: str, region: str) -> str:
    r = (region or "").strip().upper()
    if not r:
        return ""
    if "-" in r:
        return r
    r = _REGION_NAMES.get(r, r)
    return f"{country}-{r}" if country else r


def classify_region(body: Any) -> dict:
    """Turn a /api/geoblock reply into {status, country, region, message}.
    status: allowed | close_only | blocked | unknown. Pure; no I/O."""
    if not isinstance(body, dict) or "blocked" not in body:
        return {"status": "unknown", "country": "", "region": "",
                "message": REGION_UNKNOWN_MSG}
    country = str(body.get("country") or "").strip().upper()
    code = _region_code(country, str(body.get("region") or ""))
    flag = body.get("blocked")
    if isinstance(flag, str):
        flag = flag.strip().lower() in ("true", "1", "yes")
    if country in _BLOCKED_COUNTRIES or code in _BLOCKED_REGIONS:
        status = "blocked"
    elif country in _CLOSE_ONLY_COUNTRIES or code in _CLOSE_ONLY_REGIONS:
        status = "close_only"
    elif flag:
        status = "blocked"
    else:
        status = "allowed"
    message = {
        "blocked": REGION_BLOCKED_MSG,
        "close_only": REGION_CLOSE_ONLY_MSG,
        "allowed": "Polymarket allows trading from this connection.",
    }[status]
    return {"status": status, "country": country, "region": code, "message": message}


async def _fetch_geoblock() -> Any:
    """The raw /api/geoblock reply, or None when it could not be read."""
    return await _get(GEOBLOCK_URL, timeout=6.0, max_retries=2)


async def region_status(force: bool = False) -> dict:
    """Polymarket's verdict on the region this connection is in, cached for
    ~10 minutes: {status, country, region, checkedAt, message}.

    status is 'allowed', 'close_only' (sells only), 'blocked' (no orders), or
    'unknown' (the check could not be read; never treated as a block, but
    surfaced). Never raises."""
    st = _region_state
    v = st.get("value")
    if not force and v is not None:
        ttl = REGION_UNKNOWN_TTL_SEC if v.get("status") == "unknown" else REGION_TTL_SEC
        if time.time() - float(st.get("at") or 0.0) < ttl:
            return dict(v)
    try:
        body = await _fetch_geoblock()
    except Exception as e:  # noqa: BLE001 — a failed check is 'unknown', never a crash
        logger.debug(f"[region] geoblock check failed: {type(e).__name__}: {e}")
        body = None
    out = classify_region(body)
    now = time.time()
    out["checkedAt"] = now
    st["value"], st["at"] = dict(out), now
    if out["status"] in ("blocked", "close_only"):
        where = out["region"] or out["country"] or "?"
        logger.warning(f"[region] Polymarket region check: {out['status']} ({where})")
    return out


def region_cached() -> Optional[dict]:
    """The last region verdict without touching the network, or None when no
    check has run yet. Adds `ageSec`. For sync callers (the agents' vet())."""
    v = _region_state.get("value")
    if v is None:
        return None
    return {**v, "ageSec": max(0.0, time.time() - float(_region_state.get("at") or 0.0))}


def region_order_refusal(action: str) -> Optional[str]:
    """Why an order of this `action` ('buy' / 'sell') must not be sent from
    this region, from the cached verdict only (no network), else None.
    blocked refuses both; close_only refuses buys and keeps sells; unknown or
    never-checked refuses nothing."""
    v = _region_state.get("value") or {}
    status = v.get("status")
    if status == "blocked":
        return REGION_BLOCKED_MSG
    if status == "close_only" and str(action or "").strip().lower() == "buy":
        return REGION_CLOSE_ONLY_MSG
    return None


def region_snapshot(refresh: bool = False) -> Optional[dict]:
    """The cached verdict for a status snapshot. With `refresh`, a stale or
    missing verdict schedules ONE background re-check (never awaited here),
    so the header can light from the proactive check, not only after a
    refused order."""
    if refresh:
        st = _region_state
        v = st.get("value")
        ttl = REGION_UNKNOWN_TTL_SEC if (v or {}).get("status") == "unknown" else REGION_TTL_SEC
        stale = v is None or time.time() - float(st.get("at") or 0.0) >= ttl
        task = st.get("task")
        if stale and (task is None or task.done()):
            try:
                st["task"] = asyncio.get_running_loop().create_task(region_status(force=True))
            except RuntimeError:
                pass
    return region_cached()


def _is_region_refusal(status: int, body: Any) -> bool:
    """A CLOB order refusal because of where the connection is."""
    low = str(body).lower()
    if "restricted in your region" in low:
        return True
    return int(status or 0) == 403 and ("region" in low or "geoblock" in low
                                        or "jurisdiction" in low)


_restart_until = 0.0
_post_only_until = 0.0
_RESTART_DEFAULT_SEC = 30.0
_POST_ONLY_SEC = 120.0


class EngineRestartError(PolymarketAPIError):
    """The matching engine is restarting (HTTP 425). Nothing was placed."""


class PostOnlyModeError(PolymarketAPIError):
    """Post-restart post-only window: a non-post-only order was refused."""


TRADABLE_MARKET_VERSIONS = ("v1",)


class UnsupportedMarketVersionError(PolymarketAPIError):
    """The market runs on a protocol version this app can't sign for. Nothing
    was signed or sent."""

    def __str__(self) -> str:
        return str(self.body)


class OrderStatusUnknownError(PolymarketAPIError):
    """POST /order failed without saying whether the CLOB took the order (a
    timeout, a dropped connection, a 5xx), and looking the order up did not
    settle it either. It may be resting, filled, or have never arrived.

    `order_id` is the order's own hash, which is its CLOB id if it did
    arrive. A subclass of PolymarketAPIError, so every caller that only knows
    "failed" still treats it as a failure; a caller whose reconcile can poll
    an order by id takes `response` (the place_limit_order shape, status
    "unknown") and tracks it instead of dropping it."""

    def __init__(self, order_id: str, cause: Exception):
        self.order_id = order_id
        super().__init__(
            int(getattr(cause, "status", 0) or 0),
            f"order status unknown: the send failed ({type(cause).__name__}: "
            f"{cause}) and order {order_id} could not be found yet — it may be "
            f"live, filled, or never have arrived")

    @property
    def response(self) -> dict:
        return {"order": {"order_id": self.order_id, "status": "unknown"},
                "raw": {"errorMsg": str(self.body), "unknown": True}}


def _note_restart(retry_after: Optional[float]) -> float:
    global _restart_until
    wait = retry_after if retry_after and retry_after > 0 else _RESTART_DEFAULT_SEC
    _restart_until = max(_restart_until, time.time() + wait)
    return wait


def _note_post_only() -> None:
    global _post_only_until
    _post_only_until = max(_post_only_until, time.time() + _POST_ONLY_SEC)


def _is_post_only_mode(body: Any) -> bool:
    if isinstance(body, dict) and str(body.get("code") or "").lower() == "post_only_mode":
        return True
    return "post_only_mode" in str(body).lower()


def trading_restriction() -> Optional[dict]:
    """{kind, untilEpoch, message} while the CLOB is restarting or in its
    post-restart post-only window, else None."""
    now = time.time()
    if now < _restart_until:
        return {"kind": "restarting", "untilEpoch": _restart_until,
                "message": "Polymarket's matching engine is restarting — orders are paused "
                           f"for ~{int(_restart_until - now)}s."}
    if now < _post_only_until:
        return {"kind": "post_only", "untilEpoch": _post_only_until,
                "message": "Polymarket is in post-restart post-only mode — only resting maker "
                           f"orders are accepted for ~{int(_post_only_until - now)}s."}
    return None


def _to_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _json_list(v) -> list:
    """Gamma returns `outcomes`/`outcomePrices`/`clobTokenIds` as JSON-encoded
    strings. Decode tolerantly; pass through real lists."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except Exception:
            return []
    return []



async def _get(
    url: str, params: dict | None = None,
    *, timeout: float | None = None, max_retries: int | None = None,
) -> Any:
    """GET with retry. `timeout`/`max_retries` override the module defaults for
    latency-sensitive callers (e.g. the crypto snapshot, which has its own hard
    per-asset budget and must fail FAST rather than burn it on retries — the
    difference between one card briefly showing 'no market' and the whole board
    hard-timing-out to red)."""
    retries = max_retries if max_retries is not None else MAX_RETRIES
    for attempt in range(1, retries + 1):
        try:
            client = await _get_client()
            if timeout is not None:
                resp = await client.get(url, params=params, timeout=timeout)
            else:
                resp = await client.get(url, params=params)
            _note_net_ok()
            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", 2))
                await asyncio.sleep(min(wait, 10))
                continue
            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            try:
                return resp.json() if resp.status_code < 400 else None
            except Exception:
                return None
        except httpx.PoolTimeout as e:
            if attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            _log_get_failed(url, e)
            return None
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            if attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            await _note_net_fail()
            _log_get_failed(url, e)
            return None
    return None


_TAG_CATEGORY = {
    "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "sports": "sports", "soccer": "sports", "nba": "sports", "nfl": "sports",
    "mlb": "sports", "nhl": "sports", "tennis": "sports", "ufc": "sports",
    "fifa-world-cup": "sports", "world-cup": "sports", "football": "sports",
    "epl": "sports", "premier-league": "sports", "champions-league": "sports",
    "la-liga": "sports", "serie-a": "sports", "bundesliga": "sports",
    "wimbledon": "sports", "atp": "sports", "wta": "sports",
    "boxing": "sports", "mma": "sports", "esports": "sports",
    "golf": "sports", "f1": "sports", "formula-1": "sports",
    "cricket": "sports", "rugby": "sports", "olympics": "sports",
    "politics": "politics", "elections": "politics", "us-politics": "politics",
    "geopolitics": "politics", "world": "world",
    "economy": "economics", "economics": "economics", "business": "economics",
    "finance": "economics", "fed": "economics",
    "pop-culture": "entertainment", "entertainment": "entertainment",
    "culture": "entertainment", "mentions": "entertainment",
    "weather": "climate", "climate": "climate",
    "tech": "world", "science": "world", "ai": "world",
}


def _category_from_tags(tags: list) -> str:
    for t in tags or []:
        slug = (t.get("slug") if isinstance(t, dict) else str(t)).lower()
        if slug in _TAG_CATEGORY:
            return _TAG_CATEGORY[slug]
    return ""


_meta: dict[str, dict] = {}
_META_MAX = 8000


def _fee_schedule_from_gamma(raw: dict) -> dict | None:
    """Per-market taker-fee descriptor Gamma stamps on fee-enabled markets
    (verified live 2026-07-03: `feeType: crypto_fees_v2`, `feeSchedule:
    {rate: 0.07, exponent: 1, takerOnly: true}` on the crypto up/down
    windows). Fee per share = rate × (p·(1−p))^exponent, takers only.
    Returns {"enabled": False} for an explicitly fee-free market, a
    {"enabled", "rate", "exponent"} dict when Gamma provides the schedule,
    or None when Gamma says nothing (caller falls back to category defaults).
    The CLOB's maker_base_fee/taker_base_fee=1000 fields are legacy protocol
    caps, NOT the operative rate — don't read fees from them."""
    fs = raw.get("feeSchedule")
    if "feesEnabled" not in raw and not isinstance(fs, dict):
        return None
    if not raw.get("feesEnabled"):
        return {"enabled": False}
    if not isinstance(fs, dict):
        return None
    try:
        return {
            "enabled": True,
            "rate": float(fs.get("rate") or 0.0),
            "exponent": float(fs.get("exponent") or 1.0),
            "rebateRate": float(fs.get("rebateRate") or 0.0),
        }
    except (TypeError, ValueError):
        return None


def _top_of_book(raw: dict, mid: float) -> tuple[float, float]:
    """(yes_bid, yes_ask) from Gamma's own bestBid/bestAsk.

    Gamma marks an empty side by leaving it out (bestBid is missing on about
    one open market in five) or with the 0 / 1 placeholders, and Polymarket
    only trades strictly between those, so neither is a price. A real
    two-sided book is used as is. Anything less (an empty side, a crossed
    pair, a payload without the fields) puts the mid on both sides, as before,
    so nothing downstream is handed a spread that only half exists."""
    def _side(v) -> Optional[float]:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if 0.0 < f < 1.0 else None

    bid, ask = _side(raw.get("bestBid")), _side(raw.get("bestAsk"))
    if bid is None or ask is None or bid > ask:
        return mid, mid
    return bid, ask


def _normalize_market(raw: dict) -> dict | None:
    """Map a Gamma market object into the dict shape the scanner/trader read
    (internal dict keys), and cache its trading meta."""
    if not isinstance(raw, dict):
        return None
    condition_id = raw.get("conditionId") or raw.get("condition_id") or ""
    if not condition_id:
        return None
    outcomes = _json_list(raw.get("outcomes"))
    prices = _json_list(raw.get("outcomePrices"))
    token_ids = _json_list(raw.get("clobTokenIds"))
    yes_token = str(token_ids[0]) if len(token_ids) > 0 else ""
    no_token = str(token_ids[1]) if len(token_ids) > 1 else ""
    yes_price = _to_float(prices[0]) if len(prices) > 0 else 0.0
    no_price = _to_float(prices[1]) if len(prices) > 1 else (1.0 - yes_price)
    yes_bid, yes_ask = _top_of_book(raw, yes_price)

    category = ""
    events = raw.get("events") or []
    if isinstance(events, list) and events:
        category = _category_from_tags(events[0].get("tags") or [])
    if not category:
        category = categorize_by_keywords(
            raw.get("question") or "", raw.get("slug") or "")
    event_slug = ""
    if isinstance(events, list) and events:
        event_slug = events[0].get("slug") or events[0].get("ticker") or ""
    if not event_slug:
        event_slug = raw.get("eventSlug") or ""
    price_to_beat = None
    if isinstance(events, list) and events and isinstance(events[0], dict):
        try:
            price_to_beat = float((events[0].get("eventMetadata") or {})["priceToBeat"])
        except (KeyError, TypeError, ValueError):
            price_to_beat = None

    closed = bool(raw.get("closed"))
    uma = (raw.get("umaResolutionStatus") or "").lower()
    result = ""
    settlement = None
    if closed and len(prices) >= 2:
        if prices[0] in ("1", "1.0", 1, 1.0):
            result, settlement = "yes", 1.0
        elif prices[1] in ("1", "1.0", 1, 1.0):
            result, settlement = "no", 0.0
        elif uma == "resolved" and _to_float(prices[0]) == 0.5 and _to_float(prices[1]) == 0.5:
            result, settlement = "", 0.5
        elif uma == "resolved":
            result = "yes" if yes_price >= 0.5 else "no"
            settlement = 1.0 if result == "yes" else 0.0

    status = "open"
    if closed:
        status = "settled" if (result or uma == "resolved") else "closed"
    elif not raw.get("active", True):
        status = "inactive"

    neg_risk = bool(raw.get("negRisk") or raw.get("neg_risk"))
    tick = _to_float(raw.get("orderPriceMinTickSize"), 0.01) or 0.01
    min_size = _to_float(raw.get("orderMinSize"), 5.0) or 5.0
    slug = raw.get("slug") or ""
    version = str(raw.get("version") or "")

    _meta[condition_id] = {
        "yes_token": yes_token,
        "no_token": no_token,
        "neg_risk": neg_risk,
        "tick_size": tick,
        "min_size": min_size,
        "slug": slug,
        "event_slug": event_slug,
        "question": raw.get("question") or "",
        "outcomes": outcomes,
        "version": version,
    }
    if len(_meta) > _META_MAX:
        for _k in list(_meta.keys())[: len(_meta) - _META_MAX]:
            _meta.pop(_k, None)

    return {
        "ticker": condition_id,
        "condition_id": condition_id,
        "event_ticker": event_slug,
        "series_ticker": "",
        "title": raw.get("question") or slug or condition_id,
        "yes_sub_title": outcomes[0] if outcomes else "",
        "category": category,
        "status": status,
        "close_time": raw.get("endDate") or raw.get("endDateIso") or "",
        "slug": slug,
        "neg_risk": neg_risk,
        "yes_token": yes_token,
        "no_token": no_token,
        "tick_size": tick,
        "version": version,
        "fee_schedule": _fee_schedule_from_gamma(raw),
        "volume_fp": _to_float(raw.get("volumeNum"), _to_float(raw.get("volume"))),
        "volume_24h_fp": _to_float(raw.get("volume24hr")),
        "open_interest_fp": _to_float(raw.get("liquidityNum"), _to_float(raw.get("liquidity"))),
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "last_price_dollars": yes_price,
        "no_price_dollars": no_price,
        "result": result,
        "settlement_value_dollars": settlement,
        "price_to_beat": price_to_beat,
    }


async def get_market_meta(ticker: str) -> dict | None:
    """Trading metadata for a conditionId (token ids, neg_risk, tick).
    Cached; fetches from Gamma on a miss."""
    m = _meta.get(ticker)
    if m and m.get("yes_token"):
        return m
    await fetch_market(ticker)
    return _meta.get(ticker)



async def fetch_markets(
    status: str = "open",
    limit: int = 500,
    cursor: str = "",
    series_ticker: str = "",
) -> tuple[list, str]:
    """Active, tradable markets (normalised). `cursor` is a numeric offset
    string (Gamma paginates via offset)."""
    params: dict = {
        "limit": min(int(limit), GAMMA_PAGE_MAX),
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        params["offset"] = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        params["offset"] = 0
    data = await _get(f"{GAMMA_BASE}/markets", params=params)
    if not isinstance(data, list):
        return [], ""
    out = [m for m in (_normalize_market(x) for x in data) if m]
    next_cursor = (
        str(params["offset"] + len(data)) if len(data) >= params["limit"] else ""
    )
    return out, next_cursor


MARKET_PAGE_CONCURRENCY = 4


async def fetch_all_open_markets(max_pages: int = 10) -> list:
    """Every open Gamma market, paged.

    Gamma's pagination is a fixed 100-row OFFSET window, so page N's offset is
    known in advance — the walk does not have to be serial. Pages are fetched
    in waves of `MARKET_PAGE_CONCURRENCY`, and the first SHORT page (Gamma
    returned fewer than a full page, i.e. no next cursor) ends the walk.

    This used to be a strictly serial walk with an unconditional
    `await asyncio.sleep(0.2)` between pages: ~2s of pure dead time plus ~10
    round-trips in series, all of it on the master trading loop. There is no
    per-host rate gate in this module for that sleep to have been standing in
    for — `_get` handles 429s with its own Retry-After ladder, and the shared
    client's pool is sized well above 4 — so the sleep bought nothing.

    The cost of concurrency is up to `MARKET_PAGE_CONCURRENCY - 1` speculative
    requests past the end of the universe on the wave that finds it. At one
    sync per `market_refresh_interval` (default 300s) that is noise next to the
    seconds of loop time it returns.
    """
    out: list = []
    pages = max(1, int(max_pages))
    start = 0
    while start < pages:
        wave = list(range(start, min(start + MARKET_PAGE_CONCURRENCY, pages)))
        results = await asyncio.gather(*[
            fetch_markets(
                status="open", limit=GAMMA_PAGE_MAX,
                cursor=str(i * GAMMA_PAGE_MAX),
            )
            for i in wave
        ])
        for markets, cursor in results:
            if not markets:
                return out
            out.extend(markets)
            if not cursor:
                return out
        start += MARKET_PAGE_CONCURRENCY
    return out


async def fetch_market(ticker: str) -> dict | None:
    """Look up a single market by conditionId (normalised).

    Gamma's /markets defaults to closed=false, so a plain condition_ids lookup
    returns NOTHING once a market has resolved — which silently stranded
    settled positions (we could never read their outcome). So if the default
    query is empty, retry including closed markets to pick up resolved ones."""
    if not ticker:
        return None
    data = await _get(f"{GAMMA_BASE}/markets", params={"condition_ids": ticker})
    if not (isinstance(data, list) and data):
        data = await _get(
            f"{GAMMA_BASE}/markets",
            params={"condition_ids": ticker, "closed": "true"},
        )
    if isinstance(data, list) and data:
        return _normalize_market(data[0])
    return None


async def fetch_markets_map(tickers, concurrency: int = 8) -> dict[str, dict]:
    """Fetch many markets concurrently → {ticker: market}, skipping misses.

    Replaces the serial `for t in tickers: await fetch_market(t)` chains in the
    resolution paths, where one hung endpoint could stall the whole pass
    (N tickers × the per-request timeout). A semaphore caps the in-flight burst
    so even a large unresolved backlog stays friendly to Gamma's rate limits
    (and fetch_market's own 429 backoff still applies per request)."""
    uniq = [t for t in dict.fromkeys(tickers) if t]
    if not uniq:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(tk: str):
        async with sem:
            try:
                return tk, await fetch_market(tk)
            except Exception:
                return tk, None

    pairs = await asyncio.gather(*[_one(t) for t in uniq])
    return {tk: m for tk, m in pairs if m}


_CRYPTO_INTERVAL_SEC = {"5m": 300, "15m": 900, "hourly": 3600, "4h": 14400}

_updown_cache: dict = {}
_UPDOWN_CACHE_TTL = 20.0
_UPDOWN_STALE_MAX = 90.0


async def fetch_crypto_updown(
    asset: str, interval: str = "15m", *, fast: bool = False
) -> list[dict]:
    """Active recurring "Up or Down" crypto markets for an asset.

    `fast=True` (used by the live snapshot) caps each Gamma lookup at one quick
    attempt so a slow/blocked Gamma degrades to "no market" inside the snapshot's
    per-asset budget instead of retrying ~40s and hard-timing-out the whole tab.

    Polymarket mints these on a rolling schedule with slugs like
    `btc-updown-15m-1782261900` where the trailing unix timestamp is the
    WINDOW START. We compute the current/adjacent window slugs directly
    (cheap, exact) and fall back to a crypto-tag scan if the naming differs.

    Each returned market is augmented with the REAL window timing derived
    from the slug — the Gamma `endDate`/close_time on these is a far-future
    placeholder and must NOT be used.
    """
    import time as _time
    sec = _CRYPTO_INTERVAL_SEC.get(interval, 900)
    asset_l = asset.lower()
    out: list[dict] = []
    seen: set[str] = set()
    base = (int(_time.time()) // sec) * sec
    ck = (asset_l, interval)
    cached = _updown_cache.get(ck)
    if (cached and cached["base"] == base
            and (_time.monotonic() - cached["at"]) < _UPDOWN_CACHE_TTL):
        return cached["markets"]
    g_timeout = 4.0 if fast else None
    g_retries = 1 if fast else None

    def _augment(norm: dict, ws: int) -> dict:
        from datetime import datetime, timezone
        norm = dict(norm)
        norm["window_start_epoch"] = ws
        norm["window_close_epoch"] = ws + sec
        norm["interval_sec"] = sec
        norm["close_time"] = datetime.fromtimestamp(
            ws + sec, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return norm

    if interval == "hourly":
        import binance_ref
        now = int(_time.time())
        base = (now // sec) * sec
        windows = (base, base + sec, base - sec)
        slugs = [(ws, binance_ref.hourly_slug(asset, ws)) for ws in windows]
        slugs = [(ws, s) for ws, s in slugs if s]
        datas = await asyncio.gather(*[
            _get(f"{GAMMA_BASE}/events", params={"slug": s},
                 timeout=g_timeout, max_retries=g_retries)
            for _ws, s in slugs
        ])
        for (ws, _s), data in zip(slugs, datas):
            if isinstance(data, list) and data and (data[0].get("markets") or []):
                norm = _normalize_market(data[0]["markets"][0])
                if norm and norm["ticker"] not in seen:
                    seen.add(norm["ticker"])
                    out.append(_augment(norm, ws))

    if interval in ("5m", "15m", "4h"):
        now = int(_time.time())
        base = (now // sec) * sec
        windows = (base, base + sec, base - sec, base + 2 * sec)
        datas = await asyncio.gather(*[
            _get(f"{GAMMA_BASE}/markets",
                 params={"slug": f"{asset_l}-updown-{interval}-{ws}"},
                 timeout=g_timeout, max_retries=g_retries)
            for ws in windows
        ])
        for ws, data in zip(windows, datas):
            if isinstance(data, list) and data:
                norm = _normalize_market(data[0])
                if norm and norm["ticker"] not in seen:
                    seen.add(norm["ticker"])
                    out.append(_augment(norm, ws))

    if not out:
        import re as _re
        pat = _re.compile(
            rf"^{asset_l}-updown-{interval}-(\d+)$", _re.IGNORECASE
        )
        data = await _get(
            f"{GAMMA_BASE}/markets",
            params={
                "tag_id": 21, "active": "true", "closed": "false",
                "limit": 500, "order": "startDate", "ascending": "false",
            },
            timeout=g_timeout, max_retries=g_retries,
        )
        for raw in (data if isinstance(data, list) else []):
            slug = raw.get("slug") or ""
            mobj = pat.match(slug)
            if not mobj:
                continue
            norm = _normalize_market(raw)
            if norm and norm["ticker"] not in seen:
                seen.add(norm["ticker"])
                out.append(_augment(norm, int(mobj.group(1))))

    if out:
        _updown_cache[ck] = {"at": _time.monotonic(), "base": base, "markets": out}
        return out
    if (cached and cached["base"] == base
            and (_time.monotonic() - cached["at"]) < _UPDOWN_STALE_MAX):
        return cached["markets"]
    return out


async def fetch_events(
    status: str = "open", limit: int = 200, cursor: str = ""
) -> tuple[list, str]:
    params: dict = {
        "limit": min(int(limit), 200),
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        params["offset"] = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        params["offset"] = 0
    data = await _get(f"{GAMMA_BASE}/events", params=params)
    if not isinstance(data, list):
        return [], ""
    out = []
    for e in data:
        out.append({
            "event_ticker": e.get("slug") or e.get("ticker") or str(e.get("id") or ""),
            "series_ticker": "",
            "title": e.get("title") or "",
            "sub_title": "",
            "category": _category_from_tags(e.get("tags") or []),
            "status": "open",
        })
    next_cursor = str(params["offset"] + len(data)) if len(data) >= params["limit"] else ""
    return out, next_cursor


async def fetch_series(series_ticker: str) -> dict | None:
    """Polymarket has no series concept; category comes from event tags
    on each market. Kept as a stub for call-site compatibility."""
    return None



def normalize_trade(t: dict) -> Optional[dict]:
    """Normalise ONE raw Polymarket trade record into the internal shape the
    scanner reads. For each trade we resolve the direction the taker went
    LONG and express its cost as yes/no fractions.

    BUY of outcome i  → long that outcome at `price`.
    SELL of outcome i → long the OPPOSITE outcome at `1 - price`.

    ONE parser for BOTH sources. The Data-API `/trades` rows and the RTDS
    `activity` websocket frames carry the same record (proxyWallet, side,
    asset, conditionId, size, price, outcome/outcomeIndex, slug, eventSlug,
    transactionHash), so routing both through this function is what makes a
    socket-seen trade produce field-for-field — above all `trade_id`-for-
    `trade_id` — what the 120s REST poll would have produced for it. The id is
    SYNTHESISED (Polymarket's trade rows carry no id of their own), so it only
    dedupes across sources if both sides synthesise it identically; that is why
    the socket path goes through here instead of building its own dict.

    Returns None if the record can't be parsed.
    """
    if not isinstance(t, dict):
        return None
    try:
        price = _to_float(t.get("price"))
        size = _to_float(t.get("size"))
        idx = outcome_index_of(t)
        if idx is None:
            return None
        side = str(t.get("side") or "BUY").upper()
        if side == "BUY":
            long_idx = idx
            long_price = price
        else:
            long_idx = 1 - idx
            long_price = 1.0 - price
        taker_side = "yes" if long_idx == 0 else "no"
        yes_price = long_price if long_idx == 0 else (1.0 - long_price)
        no_price = 1.0 - yes_price
        tx = t.get("transactionHash") or ""
        asset = t.get("asset") or ""
        trade_id = f"{tx}-{asset}-{int(round(price*1000))}-{int(round(size))}"
        return {
            "trade_id": trade_id,
            "ticker": t.get("conditionId") or "",
            "event_ticker": t.get("eventSlug") or "",
            "slug": t.get("slug") or "",
            "title": t.get("title") or "",
            "yes_sub_title": t.get("outcome") or "",
            "count_fp": size,
            "yes_price_dollars": yes_price,
            "no_price_dollars": no_price,
            "taker_side": taker_side,
            "created_time": "",
            "timestamp": int(t.get("timestamp") or 0),
        }
    except Exception:
        return None


def outcome_index_of(t: dict) -> Optional[int]:
    """The 0/1 index of the outcome a Data API row traded.

    The API documents `outcomeIndex: 999` as "the outcome could not be
    labeled", and serves it on ordinary binary markets now and then (45 of
    500 /trades rows at one point on 2026-10-11). Mapping it like any other
    index counted every such trade as a NO, both directions. The row's token
    id (`asset`) still names the outcome, so resolve it against the cached
    market meta (clobTokenIds order); None when it can't be resolved. A row
    with no index at all keeps the old reading, 0."""
    raw = t.get("outcomeIndex")
    if raw is None or raw == "":
        return 0
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        idx = -1
    if idx in (0, 1):
        return idx
    asset = str(t.get("asset") or "")
    meta = _meta.get(str(t.get("conditionId") or ""))
    if meta and asset:
        if asset == str(meta.get("yes_token") or ""):
            return 0
        if asset == str(meta.get("no_token") or ""):
            return 1
    return None


_V2_RENAME = {
    "token_id": "asset",
    "opposite_token_id": "oppositeAsset",
    "current_size": "size",
    "current_price": "curPrice",
    "entry_cost_usdc": "initialValue",
    "total_cost_usdc": "grossInitialValue",
    "unrealized_pnl": "cashPnl",
    "total_size": "totalBought",
}


def _snake_to_camel(k: str) -> str:
    head, *rest = k.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def v2_row_to_v1(row: Any) -> Any:
    """One Data API v2 row in the v1 camelCase shape: the renames that are
    real (token_id → asset, current_size → size, entry_cost_usdc →
    initialValue, unrealized_pnl → cashPnl, …), camelCase for the rest. v2's
    extra fields (status, firstEntryAt, entryFeesUsdc, …) come along."""
    if not isinstance(row, dict):
        return row
    return {_V2_RENAME.get(k, _snake_to_camel(k)): v for k, v in row.items()}


async def _data_v2_page(path: str, params: dict) -> tuple[Any, Optional[str]]:
    """One v2 read: (payload translated to the v1 shape, next cursor).
    (None, None) on any failure — like _get. A documented miss is `data:
    null` or `[]`, never an error; null comes back as None too, so callers
    that must tell "no such user" from "failed" check the raw body."""
    body = await _get(f"{DATA_V2}{path}", params=params)
    if not isinstance(body, dict) or "data" not in body:
        return None, None
    data = body.get("data")
    cursor = (body.get("pagination") or {}).get("next_cursor") or None
    if isinstance(data, list):
        return [v2_row_to_v1(r) for r in data], cursor
    return v2_row_to_v1(data), cursor


class PositionsTooLargeError(RuntimeError):
    """A cursor walk hit its page cap with rows still to come. Raised instead
    of returning the partial list: the copy engine would read every row past
    the cap as a position the wallet had sold."""


async def _data_v2_rows(
    path: str, params: dict, *, max_pages: int, want: int | None = None,
) -> Optional[list]:
    """Walk a v2 cursor to the end (or `want` rows): every row, in the v1
    shape. None if ANY page fails — a silently truncated walk reads as
    "those rows are gone", which the copy engine treats as the whale
    selling. Raises when the walk is longer than `max_pages`, for the same
    reason: a capped walk is not a complete one."""
    out: list = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        page_params = dict(params)
        if cursor:
            page_params.pop("limit", None)
            page_params["cursor"] = cursor
        data, cursor = await _data_v2_page(path, page_params)
        if not isinstance(data, list):
            return None
        out.extend(data)
        if not cursor or not data or (want is not None and len(out) >= want):
            return out
    raise PositionsTooLargeError(
        f"{path}: more than {max_pages} pages — refusing a partial read")


def normalize_activity_trade(payload) -> Optional[dict]:
    """Socket entry point: one RTDS `activity`/trades payload → the internal
    trade shape, or None to ignore the frame.

    Stricter than the REST path on purpose. `trade_id` is synthesised from
    (transactionHash, asset, price, size); a frame missing either id-bearing
    field would synthesise an id the REST poll will NOT reproduce, i.e. the
    same trade twice in `whale_trades`. The websocket is a strict accelerator,
    so an un-identifiable frame is dropped and the 120s REST poll — still the
    authority — picks the trade up on its own cadence.
    """
    if not isinstance(payload, dict):
        return None
    if not (payload.get("transactionHash") and payload.get("asset")):
        return None
    row = normalize_trade(payload)
    if not row or not row.get("ticker"):
        return None
    return row


async def fetch_recent_trades(limit: int = 1000, *, min_size: float = 0.0) -> list:
    """Recent public taker trades, normalised via `normalize_trade`.

    The page is served through a CDN with max-age=300 (measured 2026-10-11),
    so it can be up to five minutes old, and unfiltered it holds only the last
    ~30-50 seconds of trading. Every row keeps the trade's own `timestamp`;
    whatever is built from it should carry that time, not the time of the poll.

    `min_size` > 0 asks only for trades of at least that many shares
    (filter_type=TOKENS). The whale backstop passes its dollar threshold:
    a whale's dollars are its shares times a price under $1, so the share
    floor drops no whale, and a page then spans the last hour or so of
    whale-sized trades instead of the last minute of everything. A CASH
    floor would drop whales: a SELL is sized at the side the taker ends up
    long, so a big sell of a cheap outcome is a big position with a small
    cash leg."""
    params = {"taker_only": "true", "limit": min(int(limit), 1000)}
    if min_size and min_size > 0:
        params["filter_type"] = "TOKENS"
        params["filter_amount"] = min_size
    data, _ = await _data_v2_page("/trades", params)
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for t in data:
        row = normalize_trade(t)
        if row is not None:
            out.append(row)
    return out



async def web_market_url(
    *, event_ticker: str = "", ticker: str = "", env: str = "mainnet",
) -> str:
    """polymarket.com URL for a market/event."""
    slug = (event_ticker or "").strip()
    if not slug and ticker:
        meta = _meta.get(ticker) or await get_market_meta(ticker)
        if meta:
            slug = meta.get("event_slug") or meta.get("slug") or ""
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return "https://polymarket.com/"



async def _clob_price(token_id: str, side: str) -> Optional[float]:
    """Best price for a token+side. CLOB semantics VERIFIED LIVE 2026-07-16:
    side=BUY → best BID (what buy orders rest at), side=SELL → best ASK.
    The old docstring had this BACKWARDS, and get_quote/updown_arb_edge
    inherited the inversion — every REST-fallback quote on the live order
    paths swapped bid/ask (Gate 1/1b/2 repricing, copy exits), the recorder
    wrote 966 bid-as-ask tick rows, and the arb detector fired on bid+bid<1."""
    if not token_id:
        return None
    data = await _get(
        f"{CLOB_BASE}/price", params={"token_id": token_id, "side": side.upper()}
    )
    if isinstance(data, dict) and data.get("price") not in (None, ""):
        try:
            return float(data["price"])
        except (TypeError, ValueError):
            return None
    return None


async def get_quote(ticker: str, side: str) -> dict:
    """{'bid_cents', 'ask_cents'} for the (yes/no) token of a market."""
    meta = await get_market_meta(ticker)
    if not meta:
        return {"bid_cents": None, "ask_cents": None}
    token = meta["yes_token"] if side.lower() == "yes" else meta["no_token"]
    bid, ask = await asyncio.gather(
        _clob_price(token, "BUY"), _clob_price(token, "SELL"),
    )
    return {
        "bid_cents": int(round(bid * 100)) if bid is not None else None,
        "ask_cents": int(round(ask * 100)) if ask is not None else None,
    }


async def updown_arb_edge(ticker: str) -> Optional[dict]:
    """Up + Down ≠ $1 arbitrage check for a binary market.

    Owning one Up token AND one Down token pays exactly $1 at resolution (one
    side settles to $1, the other to $0). So if the two best ASKS sum to LESS
    than $1, you can buy both for under a dollar and lock the difference — a
    market-neutral edge. Returns {upAsk, downAsk, sum, edgeCents} (all 0..1
    except edgeCents; edgeCents > 0 = a buyable arb, GROSS of fees), or None if
    either side has no ask. Fees still apply, so a real edge must clear them —
    this only DETECTS the gap.
    """
    meta = await get_market_meta(ticker)
    if not meta:
        return None
    yt, nt = meta.get("yes_token"), meta.get("no_token")
    if not yt or not nt:
        return None
    up, down = await asyncio.gather(_clob_price(yt, "SELL"), _clob_price(nt, "SELL"))
    if up is None or down is None or up <= 0 or down <= 0:
        return None
    s = up + down
    return {
        "upAsk": round(up, 4), "downAsk": round(down, 4),
        "sum": round(s, 4), "edgeCents": round((1.0 - s) * 100, 1),
    }


async def book_imbalance(token_id: str, levels: int = 3) -> Optional[float]:
    """Top-of-book order-flow imbalance for a token over the best `levels`
    price levels: (bidVol − askVol) / (bidVol + askVol), in [-1, 1]. +1 = all
    resting buy interest (buying pressure), −1 = all sell interest, 0 = balanced.
    None if the book has no two-sided depth."""
    if not token_id:
        return None
    data = await _get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    if not isinstance(data, dict):
        return None
    n = max(1, int(levels or 1))

    def _top_vol(rows, *, best_high: bool) -> float:
        parsed: list[tuple[float, float]] = []
        for r in (rows or []):
            try:
                parsed.append((float(r.get("price", 0)), float(r.get("size", 0))))
            except (TypeError, ValueError):
                continue
        parsed.sort(key=lambda x: x[0], reverse=best_high)
        return sum(sz for _, sz in parsed[:n])

    bid_v = _top_vol(data.get("bids"), best_high=True)
    ask_v = _top_vol(data.get("asks"), best_high=False)
    tot = bid_v + ask_v
    if tot <= 0:
        return None
    return round((bid_v - ask_v) / tot, 4)


async def get_orderbook(ticker: str) -> dict:
    """Normalised {'yes':[[cents,size],...], 'no':[...]} built from
    the two token books' bids. Best-effort."""
    meta = await get_market_meta(ticker)
    out: dict = {"yes": [], "no": []}
    if not meta:
        return out
    for side, token in (("yes", meta["yes_token"]), ("no", meta["no_token"])):
        if not token:
            continue
        data = await _get(f"{CLOB_BASE}/book", params={"token_id": token})
        if isinstance(data, dict):
            for lvl in data.get("bids") or []:
                try:
                    out[side].append(
                        [int(round(float(lvl["price"]) * 100)), float(lvl["size"])]
                    )
                except (TypeError, ValueError, KeyError):
                    continue
    return out



def _is_stale_creds_error(body: Any) -> bool:
    """True if a CLOB error body indicates the L2 API creds are stale/invalid for
    the current signer — recoverable by re-deriving. Most importantly the
    post-key-change "the order signer address has to be the address of the api
    key", plus generic api-key/unauthorized rejections."""
    s = (body if isinstance(body, str) else str(body)).lower()
    return (
        "signer address has to be the address of the api key" in s
        or "invalid api key" in s
        or "api key" in s and ("unauthorized" in s or "not found" in s or "invalid" in s)
        or "unauthorized" in s
    )


def _is_signer_mismatch_error(body: Any) -> bool:
    """The CLOB's 'order signer address has to be the address of the api key'
    rejection specifically. Unlike generic stale creds, re-deriving CANNOT fix
    this: it means the imported key isn't the authorized signer of the configured
    deposit wallet, so the api key (derived from that key) will never match the
    order's funder/signer. A user-config problem, not a transient one."""
    s = (body if isinstance(body, str) else str(body)).lower()
    return "signer address has to be the address of the api key" in s


def _is_maker_not_allowed_error(body: Any) -> bool:
    """The CLOB's 'maker address not allowed, please use the deposit wallet flow'
    rejection: the order's `maker` is a plain EOA. Polymarket only matches orders
    whose maker is a Polymarket deposit (proxy) wallet."""
    s = (body if isinstance(body, str) else str(body)).lower()
    return "maker address not allowed" in s


_SIGNER_MISMATCH_HELP = (
    "Order rejected: your imported key isn't the authorized signer for the "
    "configured deposit wallet. In API Keys, paste your Polymarket Profile/"
    "deposit address AND import the key that controls it."
)

_NO_DEPOSIT_WALLET_HELP = (
    "No deposit wallet set, so orders are signed with your raw wallet as maker "
    "and Polymarket rejects them. In API Keys, paste your Polymarket Profile/"
    "deposit address (no need to re-import your key)."
)


async def _reset_api_creds() -> None:
    """Drop the cached/derived L2 creds and re-derive them for the CURRENT signer."""
    global _creds_derived, _allowance_synced
    _creds_derived = False
    _allowance_synced = False
    try:
        auth.clear_api_creds()
    except Exception:
        pass
    await ensure_api_creds()


async def _authed_request(
    method: str, path: str, *, json_body: dict | list | None = None,
    params: dict | None = None, idempotent: bool = True,
) -> Any:
    """Authenticated CLOB request. Serialises the body once, signs that exact
    string (HMAC L2), and sends it as raw content. On a stale/invalid-creds
    rejection it re-derives the L2 creds once and retries, so a wallet/key change
    self-heals instead of the user having to hit Wallet → Test.

    `idempotent=False` (used for POST /order) disables the automatic retry on an
    AMBIGUOUS failure — a network timeout or a 5xx — because the request may have
    already mutated server state (the order could have reached the CLOB before the
    response was lost). Re-firing it would risk a double fill, so we surface the
    error and let the order poller reconcile what actually landed. 429s and
    auth-refresh retries are still safe (the order was rejected before matching)."""
    await ensure_api_creds()
    method = method.upper()
    body_str = ""
    if json_body is not None:
        body_str = json.dumps(json_body, separators=(",", ":"))
    last_exc: Optional[Exception] = None
    creds_refreshed = False
    for attempt in range(1, MAX_RETRIES + 1):
        headers = auth.l2_headers(method, path, body_str)
        headers["Content-Type"] = "application/json"
        client = await _get_client()
        try:
            resp = await client.request(
                method, f"{CLOB_BASE}{path}",
                headers=headers,
                content=body_str if body_str else None,
                params=params,
                follow_redirects=False,
            )
            _note_net_ok()
        except httpx.PoolTimeout as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            if idempotent and attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            await _note_net_fail()
            raise
        if resp.status_code == 429:
            await asyncio.sleep(min(float(resp.headers.get("retry-after", 2)), 10))
            continue
        if resp.status_code == 425:
            try:
                ra = float(resp.headers.get("retry-after")) if resp.headers.get("retry-after") else None
            except (TypeError, ValueError):
                ra = None
            wait = _note_restart(ra)
            if idempotent and attempt < MAX_RETRIES and wait <= 10:
                await asyncio.sleep(wait)
                continue
            raise EngineRestartError(425, trading_restriction()["message"]
                                     if trading_restriction() else "matching engine restarting")
        if 500 <= resp.status_code < 600 and idempotent and attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if (
            not creds_refreshed
            and (resp.status_code == 401
                 or (resp.status_code in (400, 403) and _is_stale_creds_error(body)))
        ):
            creds_refreshed = True
            logger.warning("CLOB rejected creds — re-deriving for the current signer and retrying")
            try:
                await _reset_api_creds()
            except Exception as e:
                logger.warning(f"creds re-derive failed: {e}")
            continue
        if resp.status_code >= 400:
            if _is_signer_mismatch_error(body):
                logger.warning(f"CLOB signer/api-key mismatch (raw): {body}")
                raise PolymarketAPIError(resp.status_code, _SIGNER_MISMATCH_HELP)
            if _is_maker_not_allowed_error(body):
                logger.warning(f"CLOB rejected the order's EOA maker (raw): {body}")
                raise PolymarketAPIError(resp.status_code, _NO_DEPOSIT_WALLET_HELP)
            if resp.status_code == 503 and _is_post_only_mode(body):
                _note_post_only()
                raise PostOnlyModeError(503, trading_restriction()["message"]
                                        if trading_restriction() else "post-only mode")
            if "restricted in your region" in str(body).lower():
                global _geoblocked_at
                _geoblocked_at = time.time()
                logger.warning(
                    "Polymarket CLOB geoblock: trading is restricted from this "
                    "IP/region — orders will keep failing from this connection"
                )
            raise PolymarketAPIError(resp.status_code, body)
        return body
    if last_exc:
        raise last_exc
    raise RuntimeError("exhausted retries without response")


_creds_derived = False
_last_nokey_log_t = 0.0


async def _sync_clock_offset() -> None:
    """Fetch the CLOB server time and record the local↔server offset (via
    auth.set_time_offset) so signed L1/L2 timestamps compensate for a wrong system
    clock. Polymarket validates request-timestamp freshness, so a skewed clock
    otherwise rejects every signed request as stale — an opaque 'stale creds' loop
    the re-derive can't fix. Best-effort: on any failure the offset is unchanged."""
    try:
        client = await _get_client()
        resp = await client.get(f"{CLOB_BASE}/time", timeout=5.0)
        if resp.status_code >= 400:
            return
        server_ts = float((resp.text or "").strip().strip('"'))
        offset = server_ts - time.time()
        auth.set_time_offset(offset)
        if abs(offset) > 30:
            logger.warning(
                f"system clock is {offset:+.0f}s off the Polymarket server — "
                "compensating for signing; enable automatic time sync on this machine"
            )
    except Exception as e:
        logger.debug(f"clock offset sync failed (non-fatal): {e}")


_allowance_synced = False


async def refresh_balance_allowance(force: bool = False) -> bool:
    """Ask the CLOB to refresh its server-side cached balance/allowance for
    this account (GET /balance-allowance/update, L2-authed). The operator
    validates orders against that cache, and a stale zero after a deposit,
    redemption, or fresh session is the most common cause of silent
    'not enough balance / allowance' V2 rejections. Fired once per session
    (re-armed on a creds reset); `force=True` refreshes again on demand.
    Best-effort — a failure never blocks the caller.

    COLLATERAL covers BUYS only. A sell is validated against the CONDITIONAL
    allowance for the specific outcome token being sold, which this cannot
    refresh because it does not know which token — see refresh_token_allowance,
    called from the sell path itself."""
    global _allowance_synced
    if _allowance_synced and not force:
        return True
    try:
        await _authed_request(
            "GET", "/balance-allowance/update",
            params={
                "asset_type": "COLLATERAL",
                "signature_type": str(auth.get_signature_type()),
            },
        )
        _allowance_synced = True
        logger.info("CLOB balance/allowance server cache refreshed")
        return True
    except Exception as e:
        logger.debug(f"balance-allowance refresh failed (non-fatal): {e}")
        return False


async def refresh_token_allowance(token_id: str) -> bool:
    """Refresh the CLOB's cached CONDITIONAL balance/allowance for ONE outcome
    token, before selling it.

    The collateral refresh above is what stops a stale server-side cache
    rejecting a BUY. Sells had no equivalent: they are validated against the
    CONDITIONAL allowance for the token being sold, so the exact failure that
    refresh exists to prevent — a spurious "not enough balance / allowance" —
    could still reject an EXIT. An exit failing is worse than an entry failing:
    you are already in the position, and a stop loss that cannot sell is not a
    stop.

    Best-effort and non-fatal: on failure we still attempt the order, because
    refusing to sell because a *cache refresh* failed would be its own way of
    trapping somebody in a position.
    """
    if not token_id:
        return False
    try:
        await _authed_request(
            "GET", "/balance-allowance/update",
            params={
                "asset_type": "CONDITIONAL",
                "token_id": str(token_id),
                "signature_type": str(auth.get_signature_type()),
            },
        )
        return True
    except Exception as e:
        logger.debug(f"conditional allowance refresh failed (non-fatal): {e}")
        return False


async def ensure_api_creds() -> None:
    """Derive (or create) L2 API credentials from the wallet key if we don't
    have them yet. Idempotent. On the first pass of a session it also nudges
    the CLOB to refresh its cached balance/allowance (see
    refresh_balance_allowance) so the first order isn't rejected on a stale
    server-side zero."""
    global _creds_derived
    if _creds_derived or auth.has_api_creds():
        first_pass = not _creds_derived
        _creds_derived = True
        if first_pass:
            await refresh_balance_allowance()
        return
    await _sync_clock_offset()
    client = await _get_client()
    global _last_nokey_log_t
    for path, verb in (("/auth/derive-api-key", "GET"), ("/auth/api-key", "POST")):
        try:
            headers = auth.l1_headers(nonce=0)
            resp = await client.request(
                verb, f"{CLOB_BASE}{path}", headers=headers, follow_redirects=False,
            )
            data = resp.json() if resp.status_code < 400 else None
        except Exception as e:
            if "not configured" in str(e):
                if time.time() - _last_nokey_log_t >= 600:
                    _last_nokey_log_t = time.time()
                    logger.warning(
                        f"api-cred {verb} {path} failed: {e} "
                        "(connect a wallet in Settings; muted for 10min)"
                    )
            else:
                logger.warning(f"api-cred {verb} {path} failed: {e}")
            continue
        if isinstance(data, dict) and data.get("apiKey"):
            auth.set_api_creds({
                "apiKey": data["apiKey"],
                "secret": data.get("secret") or data.get("apiSecret") or "",
                "passphrase": data.get("passphrase") or data.get("apiPassphrase") or "",
            })
            _creds_derived = True
            logger.info("Polymarket API credentials ready")
            await refresh_balance_allowance()
            return
    raise RuntimeError("could not derive Polymarket API credentials from wallet key")


_BALANCE_READ_TIMEOUT = 8.0


async def _erc20_balance_usd(token: str, address: str) -> Optional[float]:
    """On-chain 6-decimal ERC-20 balanceOf(address) at `token` → dollars.

    Races ALL public Polygon RPC fallbacks CONCURRENTLY and takes the first
    valid result, under a hard overall timeout. Parallel (not sequential) is the
    point: the read is as fast as the QUICKEST healthy RPC (~1-2s), and adding
    fallbacks only HELPS. A sequential loop over slow/dead endpoints took
    len(RPCs) × per-timeout (tens of seconds) — which blew the 30s `crypto15m:
    status` RPC (it reads the balance for the sizing preview). None only if every
    endpoint fails within the timeout."""
    addr = (address or "").strip()
    tok = (token or "").strip()
    if not addr.startswith("0x") or len(addr) != 42 or not tok.startswith("0x"):
        return None
    data = "0x70a08231" + "0" * 24 + addr[2:].lower()
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": tok, "data": data}, "latest"],
    }
    client = await _get_client()

    async def _one(rpc: str) -> Optional[float]:
        resp = await client.post(rpc, json=body, timeout=_BALANCE_READ_TIMEOUT)
        res = resp.json().get("result")
        if isinstance(res, str) and res.startswith("0x"):
            return int(res, 16) / 1e6
        return None

    tasks = [asyncio.create_task(_one(rpc)) for rpc in POLYGON_RPCS]
    result: Optional[float] = None
    try:
        for fut in asyncio.as_completed(tasks, timeout=_BALANCE_READ_TIMEOUT):
            try:
                val = await fut
            except Exception as e:
                logger.debug(f"polygon rpc failed: {e}")
                continue
            if val is not None:
                result = val
                break
    except asyncio.TimeoutError:
        logger.debug("polygon balance read timed out (all RPCs slow/unreachable)")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if result is not None:
        _note_net_ok()
    else:
        await _note_net_fail()
    return result


async def _pusd_balance_usd(address: str) -> Optional[float]:
    """On-chain pUSD balanceOf(address) → dollars. None if every RPC fails."""
    return await _erc20_balance_usd(auth.COLLATERAL_PUSD_ADDRESS, address)


async def _rpc_first(method: str, params: list) -> Optional[str]:
    """One JSON-RPC call, racing all Polygon fallbacks, first valid result wins."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    client = await _get_client()

    async def _one(rpc: str) -> Optional[str]:
        resp = await client.post(rpc, json=body, timeout=_BALANCE_READ_TIMEOUT)
        res = resp.json().get("result")
        return res if isinstance(res, str) else None

    tasks = [asyncio.create_task(_one(rpc)) for rpc in POLYGON_RPCS]
    try:
        for fut in asyncio.as_completed(tasks, timeout=_BALANCE_READ_TIMEOUT):
            try:
                val = await fut
            except Exception:
                continue
            if val is not None:
                return val
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None


_EIP1167_MARKER = "363d3d373d3d3d363d73"
_EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
_SIG_ISVALIDSIGNATURE = "1626ba7e"
_SEL_GET_OWNERS = "0xa0e67e2b"


async def _proxy_impl_address(funder: str, code: str) -> str:
    """The implementation address behind `funder`, or "" if it isn't a proxy we
    recognise. Handles both the inline EIP-1167 form and the EIP-1967 storage
    slot that Polymarket's own deposit wallets use."""
    if _EIP1167_MARKER in code:
        i = code.index(_EIP1167_MARKER) + len(_EIP1167_MARKER)
        return "0x" + code[i:i + 40]
    try:
        raw = await _rpc_first(
            "eth_getStorageAt", [funder, _EIP1967_IMPL_SLOT, "latest"])
        if raw and int(raw, 16) != 0:
            return "0x" + raw[-40:]
    except Exception:
        pass
    return ""


async def detect_wallet_signature_type() -> Optional[int]:
    """Best-effort on-chain detection of the deposit wallet's Polymarket
    signature scheme, so orders are signed the way the CLOB expects instead of
    blindly assuming POLY_1271.

    Polymarket hands different account types different deposit wallets:
      • Gmail/email  → an EIP-1967-slot proxy over a 1271 impl → POLY_1271 (3)
      • MetaMask     → a Gnosis Safe                           → POLY_GNOSIS_SAFE (2)
      • some proxies → an impl with no EIP-1271 entrypoint     → POLY_PROXY (1)
      • raw key      → the EOA itself (no funder)              → EOA (0)

    The overwhelmingly common case is the Gmail/email signup we tell users to
    make, and it is type 3 — do not "correct" it. Returns a SignatureType int
    ONLY on positive evidence and None otherwise, so the caller keeps its stored
    default (3) whenever the chain doesn't clearly say something else. Read only;
    places no orders and moves no funds."""
    funder = (auth.get_funder() or "").strip()
    if not funder:
        return auth.SIGNATURE_TYPE_EOA
    try:
        eoa = (auth.get_address() or "").strip()
    except Exception:
        eoa = ""
    if eoa and funder.lower() == eoa.lower():
        return auth.SIGNATURE_TYPE_EOA
    code = (await _rpc_first("eth_getCode", [funder, "latest"]) or "").lower()
    if len(code) <= 2:
        return None
    if eoa:
        owners_res = (await _rpc_first(
            "eth_call", [{"to": funder, "data": _SEL_GET_OWNERS}, "latest"]) or "")
        if len(owners_res) > 2:
            try:
                b = owners_res[2:]
                cnt = int(b[64:128], 16)
                owners = {"0x" + b[128 + i * 64 + 24: 128 + (i + 1) * 64].lower()
                          for i in range(cnt)}
                if eoa.lower() in owners:
                    return auth.SIGNATURE_TYPE_POLY_GNOSIS_SAFE
            except Exception:
                pass
    impl_code = code
    impl = await _proxy_impl_address(funder, code)
    if impl:
        impl_code = (await _rpc_first("eth_getCode", [impl, "latest"]) or "").lower()
        if len(impl_code) <= 2:
            return None
    if _SIG_ISVALIDSIGNATURE in (impl_code or ""):
        return auth.SIGNATURE_TYPE_POLY_1271
    if impl:
        return auth.SIGNATURE_TYPE_POLY_PROXY
    return None


_SEL_OWNER = "0x8da5cb5b"
_SEL_SESSION_SIGNER_UNTIL = "0xb9ac71d6"


async def check_deposit_wallet_authorization() -> dict:
    """Is the imported key allowed to sign for the configured deposit wallet?

    The current-generation deposit wallet exposes `owner()`, so we can answer this
    with one eth_call — offline, free, no order placed, no balance required. The
    legacy EIP-1167 proxy exposes no owner getter, so we abstain there rather than
    guess (the live 1¢ probe remains the fallback).

    Returns {ok, tested, reason, owner}. ok=False ONLY on positive proof of a
    mismatch — a failed read is always {ok: True, tested: False}, never a reject.
    """
    out = {"ok": True, "tested": False, "reason": "", "owner": ""}
    try:
        funder = auth.get_funder()
        signer = (auth.get_address() or "").strip()
    except Exception as e:
        out["reason"] = f"inconclusive ({type(e).__name__})"
        return out
    if not funder or not signer:
        return out
    raw = await _rpc_first("eth_call", [{"to": funder, "data": _SEL_OWNER}, "latest"])
    if not raw or len(raw) < 42:
        out["reason"] = "wallet exposes no owner() — not checked"
        return out
    owner = "0x" + raw[-40:]
    if int(owner, 16) == 0:
        out["reason"] = "owner() returned the zero address — not checked"
        return out
    out["owner"] = owner
    out["tested"] = True
    if owner.lower() == signer.lower():
        out["reason"] = "imported key owns this deposit wallet"
        auth.set_signer_role("OWNER", signer=signer, funder=funder)
        return out
    auth.set_signer_role("SESSION_KEY", signer=signer, funder=funder)
    until = 0
    sess = await _rpc_first("eth_call", [
        {"to": funder, "data": _SEL_SESSION_SIGNER_UNTIL + signer[2:].lower().rjust(64, "0")},
        "latest",
    ])
    if sess is None:
        out["tested"] = False
        out["reason"] = "session-signer status unreadable — not checked"
        return out
    try:
        until = int(sess, 16) if sess and sess != "0x" else 0
    except (TypeError, ValueError):
        until = 0
    if until > time.time():
        out["reason"] = "imported key is an authorized session signer"
        out["sessionKeyValidUntil"] = until
        return out
    out["ok"] = False
    out["reason"] = (
        f"Deposit wallet {funder[:6]}…{funder[-4:]} is owned by {owner[:6]}…{owner[-4:]}, "
        f"not by your imported key {signer[:6]}…{signer[-4:]}. Import the key for THAT "
        "wallet, or paste the deposit wallet that belongs to this key."
    )
    return out


_SIGNER_ROLE_RECHECK_MIN_S = 30.0
_last_signer_role_recheck: Optional[float] = None


def _is_signature_rejection(body: Any) -> bool:
    """A CLOB rejection that may mean the order was signed in the wrong form —
    "invalid signature", or the signer/api-key mismatch. Deliberately broad: all
    it triggers is a throttled, read-only re-check of the signer role, and an
    order is re-signed only if the wallet's answer actually changes the form."""
    s = (body if isinstance(body, str) else str(body)).lower()
    return "signature" in s or "signer" in s


async def _signer_role_changed_after_reject(body: Any, signed_as: str) -> bool:
    """After a POLY_1271 order signed as `signed_as` was rejected, ask the wallet
    again whether the imported key is its owner or a session signer. True only
    when the next order would now be signed in a DIFFERENT form — i.e. the
    rejected one carried the wrong form and is worth re-signing once. Never
    raises."""
    global _last_signer_role_recheck
    if not signed_as or not _is_signature_rejection(body):
        return False
    try:
        now = time.monotonic()
        if (_last_signer_role_recheck is None
                or now - _last_signer_role_recheck >= _SIGNER_ROLE_RECHECK_MIN_S):
            _last_signer_role_recheck = now
            await check_deposit_wallet_authorization()
        now_role = auth.current_signer_role()
    except Exception as e:
        logger.debug(f"[wallet] signer-role re-check after reject skipped: {e}")
        return False
    if now_role and now_role != signed_as:
        logger.warning(
            f"[wallet] order signed as {signed_as} was rejected and the deposit wallet "
            f"reports the imported key as {now_role} — re-signing once"
        )
        return True
    return False


async def get_balance() -> dict:
    """{'balance': cash_cents, 'portfolio_value': portfolio_cents}.

    Cash = collateral balance. For a deposit wallet (signatureType 3) this is
    the pUSD held on-chain by the funder address; for a raw EOA it comes from
    the CLOB balance-allowance endpoint (1e6 units → cents). Portfolio = sum of
    position market value from the Data API /value endpoint, keyed by whichever
    address actually holds the positions.

    The two legs are independent network reads (chain/CLOB vs Data API) and
    used to run back-to-back, so every account snapshot, every fill and both
    engines' sizing paid the sum of both latencies. They are gathered now; the
    failure semantics of each leg are unchanged (cash raises, value degrades to
    None) and `_rpc_first` still races the RPCs inside the cash leg.
    """
    funder = auth.get_funder()

    async def _cash_leg() -> int:
        if funder:
            usd = await _pusd_balance_usd(funder)
            if usd is None:
                raise RuntimeError("deposit-wallet balance read failed (all Polygon RPCs unreachable)")
            return int(round(usd * 100))
        try:
            data = await _authed_request(
                "GET", "/balance-allowance",
                params={"asset_type": "COLLATERAL", "signature_type": "0"},
            )
        except Exception as e:
            raise RuntimeError(f"raw-EOA balance-allowance read failed: {e}")
        bal = data.get("balance") if isinstance(data, dict) else None
        if bal is None:
            raise RuntimeError("raw-EOA balance-allowance response missing 'balance'")
        return int(round(float(bal) / 1e6 * 100))

    async def _value_leg() -> Optional[int]:
        try:
            addr = funder or auth.get_address()
            body = await _get(f"{DATA_V2}/value", params={"user": addr})
            if isinstance(body, dict) and "data" in body:
                data = body.get("data")
                if data is None:
                    return 0
                return int(round(float(data.get("value") or 0) * 100))
            logger.debug(f"/v2/value unexpected shape: {type(body).__name__}")
        except Exception as e:
            logger.debug(f"/value failed: {e}")
        return None

    cash_res, port_res = await asyncio.gather(
        _cash_leg(), _value_leg(), return_exceptions=True
    )
    if isinstance(cash_res, BaseException):
        raise cash_res
    port_value: Optional[int] = port_res if isinstance(port_res, int) else None
    return {"balance": cash_res, "portfolio_value": port_value}


def _v2_approvals(data) -> tuple[Optional[bool], list[str]]:
    """(approved, unapproved exchanges) from a CLOB /balance-allowance reply.

    The reply's `allowances` maps each spender to the collateral allowance it
    holds. Only the V2 exchanges the app signs orders for count: an allowance
    to anything else (the retired V1 exchanges) authorises nothing we send, so
    taking the largest value of the map, as this once did, could read a V1
    approval as ready. (None, []) when the reply names neither V2 exchange:
    unknown, not approved."""
    allow = data.get("allowances") if isinstance(data, dict) else None
    if not isinstance(allow, dict):
        return None, []
    names = {auth.EXCHANGE_ADDRESS.lower(): "exchange",
             auth.NEG_RISK_EXCHANGE_ADDRESS.lower(): "neg-risk exchange"}
    held: dict[str, float] = {}
    for spender, amount in allow.items():
        name = names.get(str(spender).lower())
        if name:
            held[name] = _to_float(amount)
    if not held:
        return None, []
    missing = [n for n, v in held.items() if v <= 0]
    return not missing, missing


_signer_check_cache: dict[tuple[str, str], dict] = {}


async def verify_order_signer_live() -> dict:
    """Prove the imported key is the authorized signer of the configured deposit
    wallet by submitting ONE tiny, non-crossing FAK order and reading the CLOB's
    verdict. Returns {ok, tested, reason}.

    The supported setup is a Polymarket Gmail/email account, signing as
    signatureType 3 (POLY_1271) with the funder = the Profile/deposit address and
    the api key derived from the exported EOA. That pairing can't be proven
    offline (the CLOB validates the key↔deposit-wallet authorization on-chain), so
    `check_trading_ready`'s "approvals look OK" can pass while EVERY real order is
    rejected with "the order signer address has to be the address of the API KEY"
    — which means THIS wallet's imported key isn't the signer of THIS deposit
    address (a mismatched pairing). This live probe catches that in one click.

    Safety: a BUY at 1¢ crosses nothing (nothing sells at 1¢) and FAK kills the
    unfilled remainder instantly — nothing rests, nothing fills, no cancel needed.
    The CLOB only reaches "no liquidity to match" AFTER it has accepted (i.e.
    signature/signer-validated) the order, so a non-crossing FAK is a reliable
    probe: a bad pairing is rejected at validation; a good one returns "unmatched".

    ok=False is returned ONLY on a proven mismatch, or (with regionRefused=True)
    when Polymarket refused the order for this connection's region. ok=True with tested=False
    means no conclusive probe ran (no market / transient / too little balance) —
    callers must NOT treat that as a failure."""
    if auth.get_signature_type() == auth.SIGNATURE_TYPE_EOA:
        return {"ok": False, "tested": True, "reason": _NO_DEPOSIT_WALLET_HELP}
    signer = (auth.get_address() or "").strip().lower()
    funder = (auth.get_funder() or "").strip().lower()
    cached = _signer_check_cache.get((signer, funder))
    if cached is not None:
        return {**cached, "tested": True}
    try:
        markets, _ = await fetch_markets(limit=50)
    except Exception as e:
        return {"ok": True, "tested": False, "reason": f"no test market ({type(e).__name__})"}
    target = next(
        (m for m in markets
         if m.get("yes_token") and m.get("status") == "open"
         and 0.10 <= _to_float(m.get("yes_bid_dollars")) <= 0.90),
        None,
    )
    if not target:
        return {"ok": True, "tested": False, "reason": "no suitable test market"}
    meta = await get_market_meta(target["ticker"]) or {}
    count = max(int(math.ceil(_to_float(meta.get("min_size"), 5.0))), 110)
    try:
        await place_limit_order(
            ticker=target["ticker"], side="yes", action="buy",
            count=count, price_cents=1, order_type="FAK",
        )
    except PolymarketAPIError as e:
        if _is_signer_mismatch_error(e.body) or str(e.body) == _SIGNER_MISMATCH_HELP:
            verdict = {"ok": False, "reason": _SIGNER_MISMATCH_HELP}
            _signer_check_cache[(signer, funder)] = verdict
            return {**verdict, "tested": True}
        if _is_region_refusal(e.status, e.body):
            return {"ok": False, "tested": True, "regionRefused": True,
                    "reason": REGION_REFUSED_MSG}
        return {"ok": True, "tested": False, "reason": f"inconclusive (CLOB {e.status})"}
    except Exception as e:
        return {"ok": True, "tested": False, "reason": f"inconclusive ({type(e).__name__})"}
    verdict = {"ok": True, "reason": "signer verified"}
    _signer_check_cache[(signer, funder)] = verdict
    return {**verdict, "tested": True}


async def check_trading_ready() -> dict:
    """Preflight before live trading: confirm the wallet can actually place
    CLOB orders. Derives API creds, checks USDC collateral balance, and
    best-effort checks that the on-chain trading approvals (allowances) are
    set. Returns a dict for the UI/logs; never raises.

    `approvalsOk` is True/False when the CLOB reports an allowance, or None
    when it can't be determined (then a real order is the only sure test).

    `region` is Polymarket's region verdict for this connection (see
    `region_status`), included for every wallet type. `fatalIssues` is the
    subset of `issues` under which no order can work at all (blocked or
    close-only region, no pUSD, allowance 0, signer mismatch, no deposit
    wallet); the rest are cautions."""
    issues: list[str] = []
    fatal: list[str] = []

    def _fatal(msg: str) -> None:
        issues.append(msg)
        fatal.append(msg)

    address = ""
    balance_usd = 0.0
    approvals_ok: Optional[bool] = None
    region = await region_status()
    if region.get("status") == "blocked":
        _fatal(REGION_BLOCKED_MSG)
    elif region.get("status") == "close_only":
        _fatal(REGION_CLOSE_ONLY_MSG)
    try:
        funder = auth.get_funder()
        sig_type = auth.get_signature_type()
    except auth.WalletMetaError as e:
        msg = f"deposit-wallet setting unreadable ({e}) — re-save it in API Keys"
        return {
            "ok": False, "address": "", "balanceUsd": 0.0, "approvalsOk": None,
            "issues": [msg, *issues], "fatalIssues": [msg, *fatal], "region": region,
        }
    try:
        await ensure_api_creds()
        address = auth.trading_address()
    except Exception as e:
        msg = f"could not derive Polymarket API credentials: {e}"
        return {
            "ok": False, "address": "", "balanceUsd": 0.0, "approvalsOk": None,
            "issues": [msg, *issues], "fatalIssues": [msg, *fatal], "region": region,
        }

    signer_addr = ""
    try:
        signer_addr = auth.get_address()
    except Exception:
        pass
    if funder and signer_addr and funder.strip().lower() == signer_addr.strip().lower():
        _fatal(
            "deposit wallet equals your signer (wallet) address — paste your "
            "Polymarket Profile/deposit address there instead, not your wallet's "
            "own address (otherwise every order is rejected: signer ≠ api key)"
        )
    elif not funder:
        _fatal(
            "no deposit wallet configured — Polymarket rejects orders signed by a "
            "raw wallet address, so no order can fill. Paste your Polymarket "
            "Profile/deposit address into the Deposit wallet field."
        )
    if funder:
        try:
            detected = await detect_wallet_signature_type()
            if detected == auth.SIGNATURE_TYPE_EOA:
                detected = None
            if detected is not None and int(detected) != int(sig_type):
                auth.set_wallet_meta(funder=funder, signature_type=int(detected))
                _names = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE", 3: "POLY_1271"}
                logger.info(
                    f"[wallet] corrected signatureType {sig_type}→{detected} "
                    f"({_names.get(int(detected), detected)}) for deposit {funder}"
                )
                sig_type = int(detected)
        except Exception as e:
            logger.debug(f"[wallet] signature-type detection skipped: {e}")
    balance_read_failed = False
    if funder:
        try:
            authz = await check_deposit_wallet_authorization()
            if authz.get("tested") and not authz.get("ok"):
                _fatal(authz["reason"])
        except Exception as e:
            logger.debug(f"[wallet] deposit-wallet owner check skipped: {e}")

    try:
        bal = await get_balance()
        balance_usd = int(bal.get("balance") or 0) / 100.0
    except Exception as e:
        balance_read_failed = True
        issues.append(f"could not read balance: {e}")
    if balance_usd <= 0 and not balance_read_failed:
        usdc_usd = 0.0
        for _tok in (USDC_E_ADDRESS, NATIVE_USDC_ADDRESS):
            try:
                v = await _erc20_balance_usd(_tok, address)
                if v:
                    usdc_usd = max(usdc_usd, v)
            except Exception:
                pass
        short = f"{address[:6]}…{address[-4:]}" if address else "your wallet"
        if usdc_usd > 0:
            _fatal(
                f"found ${usdc_usd:.2f} USDC (not pUSD) at {short} — Polymarket trades in pUSD, "
                "so convert/deposit it to pUSD on Polymarket before the bot can trade"
            )
        elif funder:
            _fatal(
                f"deposit wallet {short} holds no pUSD — confirm it's the Polygon (0x) address "
                "from your Polymarket deposit screen (not the Solana one) and that it's funded"
            )
        else:
            _fatal(
                "no pUSD collateral on this raw wallet — if your funds are in a Polymarket "
                "account (email/Google login), paste your deposit-wallet (Polygon 0x) address "
                "into the Deposit wallet field, then reconnect"
            )
    MIN_ORDER_USD = 1.00
    DEFAULT_MIN_SHARES = 5.0
    ANY_PRICE_USD = DEFAULT_MIN_SHARES * 0.99
    PROBE_MIN_USD = 1.5
    if 0 < balance_usd < MIN_ORDER_USD:
        _fatal(
            f"balance is ${balance_usd:.2f} — below Polymarket's ${MIN_ORDER_USD:.2f} "
            "minimum order, so no order can be placed at any price. Add funds."
        )
    elif 0 < balance_usd < PROBE_MIN_USD:
        issues.append(
            f"balance is ${balance_usd:.2f} — enough for a minimum order, but too "
            f"low to run the live order test that verifies this wallet is paired "
            f"correctly. Add a little (${PROBE_MIN_USD:.2f}+) to fully verify it."
        )
    elif 0 < balance_usd < ANY_PRICE_USD:
        max_cents = int(balance_usd / DEFAULT_MIN_SHARES * 100)
        issues.append(
            f"balance is ${balance_usd:.2f} — enough to trade markets priced up to "
            f"~{max_cents}c (Polymarket's {int(DEFAULT_MIN_SHARES)}-share minimum "
            f"order). Add ~${ANY_PRICE_USD:.0f} total to trade at any price; until "
            "then the engines will skip markets that cost more than your balance."
        )
    try:
        data = await _authed_request(
            "GET", "/balance-allowance",
            params={"asset_type": "COLLATERAL", "signature_type": str(int(sig_type))},
        )
        approvals_ok, missing = _v2_approvals(data)
        if missing:
            _fatal(
                f"collateral allowance is 0 for Polymarket's {' and '.join(missing)} — "
                "enable trading for this wallet on polymarket.com before going live "
                "(orders there are rejected until it is approved)"
            )
    except Exception as e:
        logger.debug(f"allowance check failed: {e}")

    signer_ok: Optional[bool] = None
    if (
        sig_type == auth.SIGNATURE_TYPE_POLY_1271
        and balance_usd >= 1.5
        and region.get("status") not in ("blocked", "close_only")
        and not any("deposit wallet equals your signer" in i for i in issues)
    ):
        try:
            probe_sig = await verify_order_signer_live()
            if probe_sig.get("regionRefused"):
                _fatal(probe_sig.get("reason") or REGION_REFUSED_MSG)
                region = {**region, "refusedOrder": True}
            elif probe_sig.get("tested"):
                signer_ok = bool(probe_sig.get("ok"))
                if not signer_ok:
                    _fatal(probe_sig.get("reason") or _SIGNER_MISMATCH_HELP)
        except Exception as e:
            logger.debug(f"live signer probe failed: {e}")

    try:
        probe = await _get(
            f"{GAMMA_BASE}/markets", params={"limit": 1},
            timeout=4.0, max_retries=1,
        )
    except Exception:
        probe = None
    if not probe:
        issues.append(
            "Polymarket market data (Gamma API) is unreachable from your network "
            "— the Crypto markets may be unavailable in your region"
        )

    return {
        "ok": len(issues) == 0, "address": address, "balanceUsd": balance_usd,
        "approvalsOk": approvals_ok, "signerOk": signer_ok, "issues": issues,
        "fatalIssues": fatal, "region": region,
    }


async def get_positions(
    limit: int = 1000, *, settlement_status: str | None = None,
    paginate: bool = True, user: str | None = None, strict: bool = False,
) -> list[dict]:
    """Wallet positions from the Data API, mapped to the internal shape
    the reconciler reads (signed `position_fp`, `market_exposure_dollars`).

    `user` overrides whose wallet to read — used by the copy-trading engine to
    poll a FOLLOWED address. Default (None) reads our own wallet: the funder
    (deposit wallet) when one is configured, else the EOA.

    `strict` raises instead of returning [] when OUR address cannot be read.
    For callers where "no wallet" and "no positions" must not look the same:
    the standing-instruction tick retires every armed stop on an empty read."""
    if user:
        addr = user
    else:
        try:
            addr = auth.trading_address()
        except Exception:
            if strict:
                raise
            return []
    page_size = min(500, max(1, int(limit)))
    params = {"user": addr, "limit": page_size}
    try:
        if paginate:
            raw = await _data_v2_rows("/positions", params, max_pages=40)
        else:
            raw, _ = await _data_v2_page("/positions", params)
    except RuntimeError as e:
        raise type(e)(f"positions fetch for {addr or 'own wallet'}: {e}") from e
    if not isinstance(raw, list):
        raise RuntimeError(f"positions fetch failed for {addr or 'own wallet'}")
    out: list[dict] = []
    for p in raw:
        try:
            size = _to_float(p.get("size"))
            if size == 0:
                continue
            idx = int(p.get("outcomeIndex") or 0)
            signed = size if idx == 0 else -size
            avg = _to_float(p.get("avgPrice"))
            out.append({
                "ticker": p.get("conditionId") or "",
                "event_ticker": p.get("eventSlug") or "",
                "title": p.get("title") or "",
                "category": "",
                "position_fp": signed,
                "market_exposure_dollars": abs(size * avg),
                "fees_paid_dollars": 0.0,
                "redeemable": bool(p.get("redeemable")),
                "cur_price": _to_float(p.get("curPrice")),
                "_raw": p,
            })
        except Exception:
            continue
    return out


async def get_settled_positions(limit: int = 1000) -> list[dict]:
    pos = await get_positions(limit=limit)
    return [p for p in pos if p.get("redeemable")]


async def get_activity(limit: int = 500, *, user: str | None = None) -> list[dict]:
    """Recent on-chain activity (TRADE / REDEEM / SPLIT / MERGE) for a wallet,
    from the public Data API. Used to detect positions the user exited DIRECTLY
    on Polymarket — a SELL or REDEEM the order/position reconcile can't see,
    because the position simply vanishes from /positions. Each event carries
    `type`, `side` (BUY/SELL), `conditionId`, `outcomeIndex`, `usdcSize` (cash
    leg), `size` (shares), and `timestamp`. Default reads our own trading
    wallet (deposit-wallet funder when set, else the EOA)."""
    if user:
        addr = user
    else:
        try:
            addr = auth.trading_address()
        except Exception:
            return []
    page = min(1000, max(1, int(limit)))
    try:
        rows = await _data_v2_rows(
            "/activity", {"user": addr, "limit": page},
            max_pages=10, want=int(limit),
        )
    except RuntimeError:
        rows = None
    return (rows or [])[: max(1, int(limit))]



async def get_order(order_id: str) -> dict:
    """Single open order by id → {'order': {...}}."""
    data = await _authed_request("GET", f"/data/order/{order_id}")
    return {"order": data if isinstance(data, dict) else {}}


_ORDER_FILL_MAX_PAGES = 50


def _order_legs(t: dict, order_id: str) -> list[dict]:
    """Our order's leg(s) of one /data/trades match → [{asset_id, price,
    size, side, role}], or [] when the order took no part in it.

    `/data/trades` reports every match from the TAKER's side: the top-level
    `asset_id`, `price`, `size` and `side` are the taker order's. When our
    order rested and was hit, its leg is its own `maker_orders` entry, at its
    own price and `matched_amount`, possibly on the other token and in the
    other direction. Same rule as terminal_trade._our_leg, keyed by the order
    id we hold instead of by wallet address."""
    if not isinstance(t, dict):
        return []
    if str(t.get("status") or "").upper().replace("TRADE_STATUS_", "") == "FAILED":
        return []
    if str(t.get("taker_order_id") or "") == order_id:
        return [{"asset_id": str(t.get("asset_id") or ""), "price": _to_float(t.get("price")),
                 "size": _to_float(t.get("size")), "side": str(t.get("side") or ""),
                 "role": "taker"}]
    return [{"asset_id": str(m.get("asset_id") or ""), "price": _to_float(m.get("price")),
             "size": _to_float(m.get("matched_amount")), "side": str(m.get("side") or ""),
             "role": "maker"}
            for m in (t.get("maker_orders") or [])
            if isinstance(m, dict) and str(m.get("order_id") or "") == order_id]


async def get_fills_for_order(order_id: str, limit: int = 200, *, market: str = "",
                              asset_id: str = "", after: Optional[int] = None) -> list[dict]:
    """Trades for one of our orders, as OUR leg of each match, mapped to
    {count, action, price fields}.

    The `/data/trades` endpoint is L2-authenticated, so it already returns ONLY
    the authed account's trades — as MAKER or TAKER. We must NOT add a
    `maker_address` filter: that restricts the response to trades where we were
    the maker and silently drops every TAKER fill (a marketable/crossing order,
    or the crypto15m maker→taker escalation). That dropped-taker bug made filled
    orders look unfilled, so they were written off as 'canceled' while the
    contracts sat in the wallet. We match our order_id client-side against both
    the taker_order_id and the maker_orders entries instead (see _order_legs).

    `market` (the condition id), `asset_id` and `after` (unix seconds) narrow
    the venue's walk; the cursor is followed to the end (`LTE=`). The callers
    rewrite a position's filled size and cost from this list, so a walk that
    fails part-way, or never ends, returns [] ("unknown") rather than a short
    list that would shrink a real position."""
    oid = str(order_id or "")
    if not oid:
        return []
    base: dict = {}
    if market:
        base["market"] = market
    if asset_id:
        base["asset_id"] = asset_id
    if after:
        base["after"] = int(after)
    out: list[dict] = []
    cursor = ""
    seen: set[str] = set()
    for _ in range(_ORDER_FILL_MAX_PAGES):
        params = {**base, "next_cursor": cursor} if cursor else dict(base)
        try:
            data = await _authed_request("GET", "/data/trades", params=params or None)
        except PolymarketAPIError:
            return []
        if isinstance(data, dict):
            rows, nxt = data.get("data") or [], str(data.get("next_cursor") or "")
        elif isinstance(data, list):
            rows, nxt = data, ""
        else:
            return []
        for t in rows:
            for leg in _order_legs(t, oid):
                price = leg["price"]
                out.append({
                    "order_id": oid,
                    "action": "sell" if leg["side"].upper() == "SELL" else "buy",
                    "count_fp": leg["size"],
                    "price_cents": int(round(price * 100)),
                    "yes_price_dollars": price,
                    "no_price_dollars": 1.0 - price,
                    "side": "",
                    "role": leg["role"],
                    "asset_id": leg["asset_id"],
                    "trade_id": str(t.get("id") or ""),
                })
        if not rows or not nxt or nxt == "LTE=" or nxt in seen:
            return out
        seen.add(nxt)
        cursor = nxt
    logger.warning(f"fills for order {oid[:12]}…: no end after "
                   f"{_ORDER_FILL_MAX_PAGES} pages; treating as unknown")
    return []


async def get_fills_since(after_ts_unix: int, limit: int = 200) -> list[dict]:
    return []


def _deposit_wallet_issue() -> str:
    """Non-empty when the configured wallet would sign orders with an EOA `maker`
    — i.e. no funder, or a funder that is just the signer's own address. Both
    make every order a guaranteed CLOB reject. Returns the user-facing fix."""
    try:
        funder = auth.get_funder()
    except auth.WalletMetaError as e:
        return f"Deposit-wallet setting unreadable ({e}). Re-save it in API Keys."
    if not funder:
        return _NO_DEPOSIT_WALLET_HELP
    try:
        signer = (auth.get_address() or "").strip()
    except Exception:
        return ""
    if signer and funder.strip().lower() == signer.lower():
        return _NO_DEPOSIT_WALLET_HELP
    return ""


def _assert_maker_is_deposit_wallet() -> None:
    issue = _deposit_wallet_issue()
    if issue:
        raise PolymarketAPIError(400, issue)


def _snap_price_to_tick(price: float, tick: float) -> float:
    """Round a 0..1 price to the market's tick size and clamp to
    [tick, 1-tick]. Polymarket rejects orders whose price isn't an exact
    multiple of the market's tick (0.1 / 0.01 / 0.001 / 0.0001)."""
    if not tick or tick <= 0:
        tick = 0.01
    dp = max(0, -int(round(math.log10(tick))))
    snapped = round(round(price / tick) * tick, dp)
    return min(round(1 - tick, dp), max(tick, snapped))


_VALID_ORDER_TYPES = ("GTC", "FOK", "FAK")


def _post_outcome_ambiguous(e: BaseException) -> bool:
    """True when a failed POST /order may still have placed the order: the
    body could have reached the CLOB before the reply was lost. A refused
    connection never sent anything, and a 4xx or a post-only refusal is the
    CLOB's own definitive "no"."""
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return False
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, PolymarketAPIError) and not isinstance(e, PostOnlyModeError):
        return 500 <= int(e.status or 0) < 600
    return False


_AMBIGUOUS_LOOKUPS = 2
_AMBIGUOUS_LOOKUP_GAP_SEC = 1.0
_AMBIGUOUS_LOOKUP_TIMEOUT_SEC = 5.0


async def _settle_ambiguous_post(order: dict, *, neg_risk: bool, order_type: str,
                                 err: Exception) -> dict:
    """After an ambiguous POST /order failure (see _post_outcome_ambiguous),
    find out what became of the order by its own hash.

    Found → the venue's real id and status, the same shape a clean placement
    returns, so every caller tracks it as placed. Not found, or the lookup
    failed too → OrderStatusUnknownError carrying the id, never a clean
    "failed": a resting order the app forgot about is exactly what this
    prevents. No automatic cancel here: an order the lookup cannot see cannot
    be cancelled with any confidence either, and the callers that keep the
    id already poll it (and cancel on their own terms)."""
    try:
        oid = auth.order_hash(order, neg_risk=neg_risk)
    except Exception as he:  # noqa: BLE001 — no id means nothing to look up
        logger.warning(f"[order] ambiguous send ({err}); order hash unavailable: {he}")
        raise err
    found: Optional[dict] = None
    for attempt in range(_AMBIGUOUS_LOOKUPS):
        try:
            data = await asyncio.wait_for(_authed_request("GET", f"/data/order/{oid}"),
                                          timeout=_AMBIGUOUS_LOOKUP_TIMEOUT_SEC)
            if isinstance(data, dict) and (data.get("id") or data.get("status")):
                found = data
                break
        except Exception as le:  # noqa: BLE001 — a failed lookup is "not found yet"
            logger.debug(f"[order] lookup {oid[:12]}… after ambiguous send: {le}")
        if attempt + 1 < _AMBIGUOUS_LOOKUPS:
            await asyncio.sleep(_AMBIGUOUS_LOOKUP_GAP_SEC)
    if found is None:
        logger.warning(f"[order] ambiguous send ({type(err).__name__}: {err}); "
                       f"order {oid} not found — status unknown")
        raise OrderStatusUnknownError(oid, err) from err
    status = str(found.get("status") or "").lower().replace("order_status_", "")
    logger.warning(f"[order] ambiguous send ({type(err).__name__}: {err}) but the "
                   f"CLOB has order {oid}: status={status}")
    raw = {**found, "recovered": True}
    if (order_type in ("FOK", "FAK") and _to_float(found.get("size_matched")) <= 0
            and status in ("unmatched", "killed", "canceled", "cancelled")):
        return {"order": {"order_id": "", "status": "unmatched"}, "raw": raw}
    return {"order": {"order_id": str(found.get("id") or oid), "status": status}, "raw": raw}


def _is_no_match_error(msg: str) -> bool:
    """True for the CLOB's 'marketable order found no liquidity' rejection — a
    FOK/FAK that crossed nothing. That's a clean zero-fill, not a real failure."""
    return "no orders found to match" in (msg or "").lower()


async def place_limit_order(
    *,
    ticker: str,
    side: str,
    action: str,
    count: int,
    price_cents: float,
    client_order_id: Optional[str] = None,
    order_type: str = "GTC",
) -> dict:
    """Sign + submit a CLOB order for the (yes/no) outcome token.

    The main engine only opens positions (`action='buy'`); the crypto
    executor also sells to stop out (`action='sell'`). A "no" position is a
    trade of the NO token — buying NO to open, selling NO to close.

    `order_type` (CLOB V2): "GTC" (default) rests on the book; "FAK"/"FOK" are
    marketable — they cross and fill immediately against resting liquidity at a
    price at-or-better than `price_cents` (the slippage cap) and KILL the
    unfilled remainder instead of resting it. Marketable orders carry no
    `postOnly` (the CLOB rejects post-only FOK/FAK). The signed order struct is
    identical across types — only the submission `orderType` differs.

    The requested `price_cents` may be fractional (a 0.001-tick market quotes
    3.5¢); it is snapped to the market's tick size and the
    `count` is raised to the market's minimum order size (for buys) so the
    CLOB doesn't reject the order with INVALID_ORDER_MIN_TICK_SIZE /
    INVALID_ORDER_MIN_SIZE.

    `client_order_id` is LOCAL correlation only — it is stored on our position
    row and never sent to the exchange, because the CLOB V2 /order body has no
    client-id field (order/owner/orderType/deferExec/postOnly; `metadata` is a
    signed bytes32 on the order struct, not an idempotency key). So there is NO
    server-side dedupe: if a response is lost after the CLOB accepted the order,
    a blind retry opens a SECOND position. That is why every caller must record
    a terminal row on failure (and why `idempotent=False` below never re-fires
    a placement) — the order poller/reconcile is what resolves the ambiguity."""
    side = side.lower()
    action = (action or "buy").lower()
    order_type = (order_type or "GTC").upper()
    if order_type not in _VALID_ORDER_TYPES:
        raise ValueError(f"order_type must be one of {_VALID_ORDER_TYPES}, got {order_type}")
    if side not in ("yes", "no"):
        raise ValueError(f"side must be yes|no, got {side}")
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be buy|sell, got {action}")
    if not (1 <= price_cents <= 99):
        raise ValueError(f"price_cents must be 1..99, got {price_cents}")
    if count <= 0:
        raise ValueError(f"count must be positive, got {count}")

    _assert_maker_is_deposit_wallet()

    meta = await get_market_meta(ticker)
    if not meta:
        raise PolymarketAPIError(404, f"no market meta for {ticker}")
    token_id = meta["yes_token"] if side == "yes" else meta["no_token"]
    if not token_id:
        raise PolymarketAPIError(404, f"no {side} token for {ticker}")
    version = str(meta.get("version") or "").strip().lower()
    if version and version not in TRADABLE_MARKET_VERSIONS:
        raise UnsupportedMarketVersionError(
            400,
            f"This market trades on Polymarket Protocol {version.upper()}, which "
            f"this version of the app can't sign orders for yet. No order was sent.",
        )

    tick = float(meta.get("tick_size") or 0.01)
    min_size = float(meta.get("min_size") or 0.0)
    price = _snap_price_to_tick(price_cents / 100.0, tick)
    size = int(count)
    if action == "buy" and min_size > 0 and size < min_size:
        raise PolymarketAPIError(
            400,
            f"requested size {size} is below this market's minimum of "
            f"{int(math.ceil(min_size))} shares — raise your order size to trade "
            f"here (not auto-inflating, to protect your risk limits)",
        )

    if action == "sell" and min_size > 0 and size < min_size:
        logger.warning(
            "[min-size] selling %s shares of %s is below this market's minimum "
            "of %d - the CLOB may reject it, in which case the position can "
            "only be closed by letting it settle",
            size, ticker, int(math.ceil(min_size)),
        )

    if action == "sell":
        await refresh_token_allowance(token_id)

    restriction = trading_restriction()
    post_only = False
    if restriction is not None:
        if restriction["kind"] == "restarting":
            raise EngineRestartError(425, restriction["message"])
        if order_type in ("FOK", "FAK"):
            raise PostOnlyModeError(503, restriction["message"])
        post_only = True

    resigned = False
    while True:
        signed_as = auth.current_signer_role()
        order = auth.create_signed_order(
            token_id=token_id,
            side="BUY" if action == "buy" else "SELL",
            price=price,
            size=size,
            neg_risk=bool(meta.get("neg_risk")),
        )
        await ensure_api_creds()
        creds = auth.get_api_creds() or {}
        body = {
            "order": order,
            "owner": creds.get("apiKey", ""),
            "orderType": order_type,
            "deferExec": False,
            "postOnly": post_only,
        }
        try:
            resp = await _authed_request("POST", "/order", json_body=body, idempotent=False)
        except PolymarketAPIError as e:
            if order_type in ("FOK", "FAK") and _is_no_match_error(str(e)):
                return {"order": {"order_id": "", "status": "unmatched"}, "raw": {"errorMsg": str(e)}}
            if (not resigned and 400 <= int(getattr(e, "status", 0) or 0) < 500
                    and await _signer_role_changed_after_reject(e.body, signed_as)):
                resigned = True
                continue
            if "invalid order payload" in str(e).lower():
                logger.warning(
                    f"[order-payload] rejected: {ticker} {side}/{action} "
                    f"type={order_type} price={price} size={size} tick={tick} "
                    f"neg_risk={bool(meta.get('neg_risk'))} token={token_id} "
                    f"sig_type={auth.get_signature_type()}"
                )
            if _post_outcome_ambiguous(e):
                return await _settle_ambiguous_post(
                    order, neg_risk=bool(meta.get("neg_risk")), order_type=order_type, err=e)
            raise
        except httpx.TransportError as e:
            if _post_outcome_ambiguous(e):
                return await _settle_ambiguous_post(
                    order, neg_risk=bool(meta.get("neg_risk")), order_type=order_type, err=e)
            raise
        if (not resigned and isinstance(resp, dict)
                and (resp.get("success") is False or resp.get("errorMsg") or resp.get("error"))
                and await _signer_role_changed_after_reject(
                    resp.get("errorMsg") or resp.get("error") or "", signed_as)):
            resigned = True
            continue
        break
    oid = ""
    status = ""
    if isinstance(resp, dict):
        oid = resp.get("orderID") or resp.get("orderId") or ""
        status = resp.get("status") or ""
        err = resp.get("errorMsg") or resp.get("error") or ""
        if resp.get("success") is False or err:
            if order_type in ("FOK", "FAK") and _is_no_match_error(err):
                return {"order": {"order_id": "", "status": "unmatched"}, "raw": resp}
            raise PolymarketAPIError(400, err or f"order rejected: {str(resp)[:160]}")
    if order_type in ("FOK", "FAK") and str(status).lower() in (
        "unmatched", "killed", "canceled", "cancelled"
    ):
        oid = ""
    return {"order": {"order_id": oid, "status": status}, "raw": resp}


async def cancel_order(order_id: str) -> dict:
    return await _authed_request("DELETE", "/order", json_body={"orderID": order_id})


_CANCEL_BATCH = 500


async def cancel_orders(order_ids: list[str]) -> dict:
    """Cancel many orders in as few requests as possible. Returns
    {"canceled": set[str], "not_canceled": {id: reason}}. A chunk whose
    request fails outright lands in not_canceled with the error, so the
    caller treats it exactly like a refused cancel (row left for reconcile)."""
    ids = [str(i) for i in dict.fromkeys(order_ids) if i]
    canceled: set[str] = set()
    not_canceled: dict[str, str] = {}
    for i in range(0, len(ids), _CANCEL_BATCH):
        chunk = ids[i:i + _CANCEL_BATCH]
        try:
            resp = await _authed_request("DELETE", "/orders", json_body=chunk)
        except Exception as e:
            for oid in chunk:
                not_canceled[oid] = str(e)
            continue
        resp = resp if isinstance(resp, dict) else {}
        for oid in resp.get("canceled") or []:
            canceled.add(str(oid))
        nc = resp.get("not_canceled") or {}
        if isinstance(nc, dict):
            for oid, why in nc.items():
                not_canceled[str(oid)] = str(why)
        for oid in chunk:
            if oid not in canceled and oid not in not_canceled:
                not_canceled[oid] = "not acknowledged"
    return {"canceled": canceled, "not_canceled": not_canceled}
