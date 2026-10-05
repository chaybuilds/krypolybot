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
    global _net_fail_streak, _last_net_ok
    _net_fail_streak = 0
    _last_net_ok = time.monotonic()


async def _note_net_fail() -> None:
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
    if fn not in _extra_recycle_hooks:
        _extra_recycle_hooks.append(fn)


async def _recycle_client() -> None:
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


_restart_until = 0.0
_post_only_until = 0.0
_RESTART_DEFAULT_SEC = 30.0
_POST_ONLY_SEC = 120.0


class EngineRestartError(PolymarketAPIError):
    pass


class PostOnlyModeError(PolymarketAPIError):
    pass


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


def _normalize_market(raw: dict) -> dict | None:
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

    closed = bool(raw.get("closed"))
    uma = (raw.get("umaResolutionStatus") or "").lower()
    result = ""
    settlement = None
    if closed and len(prices) >= 2:
        if prices[0] in ("1", "1.0", 1, 1.0):
            result, settlement = "yes", 1.0
        elif prices[1] in ("1", "1.0", 1, 1.0):
            result, settlement = "no", 0.0
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
        "fee_schedule": _fee_schedule_from_gamma(raw),
        "volume_fp": _to_float(raw.get("volumeNum"), _to_float(raw.get("volume"))),
        "volume_24h_fp": _to_float(raw.get("volume24hr")),
        "open_interest_fp": _to_float(raw.get("liquidityNum"), _to_float(raw.get("liquidity"))),
        "yes_bid_dollars": yes_price,
        "yes_ask_dollars": yes_price,
        "last_price_dollars": yes_price,
        "no_price_dollars": no_price,
        "result": result,
        "settlement_value_dollars": settlement,
    }


async def get_market_meta(ticker: str) -> dict | None:
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
    return None


def normalize_trade(t: dict) -> Optional[dict]:
    if not isinstance(t, dict):
        return None
    try:
        price = _to_float(t.get("price"))
        size = _to_float(t.get("size"))
        idx = int(t.get("outcomeIndex") or 0)
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


def normalize_activity_trade(payload) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    if not (payload.get("transactionHash") and payload.get("asset")):
        return None
    row = normalize_trade(payload)
    if not row or not row.get("ticker"):
        return None
    return row


async def fetch_recent_trades(limit: int = 1000) -> list:
    data = await _get(
        f"{DATA_BASE}/trades",
        params={"takerOnly": "true", "limit": min(int(limit), 1000)},
    )
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
    slug = (event_ticker or "").strip()
    if not slug and ticker:
        meta = _meta.get(ticker) or await get_market_meta(ticker)
        if meta:
            slug = meta.get("event_slug") or meta.get("slug") or ""
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return "https://polymarket.com/"


async def _clob_price(token_id: str, side: str) -> Optional[float]:
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
    s = (body if isinstance(body, str) else str(body)).lower()
    return (
        "signer address has to be the address of the api key" in s
        or "invalid api key" in s
        or "api key" in s and ("unauthorized" in s or "not found" in s or "invalid" in s)
        or "unauthorized" in s
    )


def _is_signer_mismatch_error(body: Any) -> bool:
    s = (body if isinstance(body, str) else str(body)).lower()
    return "signer address has to be the address of the api key" in s


def _is_maker_not_allowed_error(body: Any) -> bool:
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
                    "IP/region — orders will keep failing until the connection "
                    "exits the blocked region (e.g. VPN)"
                )
            raise PolymarketAPIError(resp.status_code, body)
        return body
    if last_exc:
        raise last_exc
    raise RuntimeError("exhausted retries without response")


_creds_derived = False
_last_nokey_log_t = 0.0


async def _sync_clock_offset() -> None:
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
    return await _erc20_balance_usd(auth.COLLATERAL_PUSD_ADDRESS, address)


async def _rpc_first(method: str, params: list) -> Optional[str]:
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
        auth.set_signer_role("OWNER")
        return out
    until = 0
    sess = await _rpc_first("eth_call", [
        {"to": funder, "data": _SEL_SESSION_SIGNER_UNTIL + signer[2:].lower().rjust(64, "0")},
        "latest",
    ])
    try:
        until = int(sess, 16) if sess and sess != "0x" else 0
    except (TypeError, ValueError):
        until = 0
    if until > time.time():
        auth.set_signer_role("SESSION_KEY")
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


async def get_balance() -> dict:
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
            data = await _get(f"{DATA_BASE}/value", params={"user": addr})
            if isinstance(data, list):
                return int(round(float(data[0].get("value") or 0) * 100)) if data else 0
            logger.debug(f"/value unexpected shape: {type(data).__name__}")
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


def _extract_allowance(data) -> Optional[float]:
    if not isinstance(data, dict):
        return None
    for k in ("allowance", "allowances"):
        v = data.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            try:
                return max((float(x) for x in v.values()), default=0.0)
            except (TypeError, ValueError):
                return None
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


_signer_check_cache: dict[tuple[str, str], dict] = {}


async def verify_order_signer_live() -> dict:
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
        return {"ok": True, "tested": False, "reason": f"inconclusive (CLOB {e.status})"}
    except Exception as e:
        return {"ok": True, "tested": False, "reason": f"inconclusive ({type(e).__name__})"}
    verdict = {"ok": True, "reason": "signer verified"}
    _signer_check_cache[(signer, funder)] = verdict
    return {**verdict, "tested": True}


async def check_trading_ready() -> dict:
    issues: list[str] = []
    address = ""
    balance_usd = 0.0
    approvals_ok: Optional[bool] = None
    try:
        funder = auth.get_funder()
        sig_type = auth.get_signature_type()
    except auth.WalletMetaError as e:
        return {
            "ok": False, "address": "", "balanceUsd": 0.0, "approvalsOk": None,
            "issues": [f"deposit-wallet setting unreadable ({e}) — re-save it in API Keys"],
        }
    try:
        await ensure_api_creds()
        address = auth.trading_address()
    except Exception as e:
        return {
            "ok": False, "address": "", "balanceUsd": 0.0, "approvalsOk": None,
            "issues": [f"could not derive Polymarket API credentials: {e}"],
        }

    signer_addr = ""
    try:
        signer_addr = auth.get_address()
    except Exception:
        pass
    if funder and signer_addr and funder.strip().lower() == signer_addr.strip().lower():
        issues.append(
            "deposit wallet equals your signer (wallet) address — paste your "
            "Polymarket Profile/deposit address there instead, not your wallet's "
            "own address (otherwise every order is rejected: signer ≠ api key)"
        )
    elif not funder:
        issues.append(
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
                issues.append(authz["reason"])
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
            issues.append(
                f"found ${usdc_usd:.2f} USDC (not pUSD) at {short} — Polymarket trades in pUSD, "
                "so convert/deposit it to pUSD on Polymarket before the bot can trade"
            )
        elif funder:
            issues.append(
                f"deposit wallet {short} holds no pUSD — confirm it's the Polygon (0x) address "
                "from your Polymarket deposit screen (not the Solana one) and that it's funded"
            )
        else:
            issues.append(
                "no pUSD collateral on this raw wallet — if your funds are in a Polymarket "
                "account (email/Google login), paste your deposit-wallet (Polygon 0x) address "
                "into the Deposit wallet field, then reconnect"
            )
    MIN_ORDER_USD = 1.00
    DEFAULT_MIN_SHARES = 5.0
    ANY_PRICE_USD = DEFAULT_MIN_SHARES * 0.99
    PROBE_MIN_USD = 1.5
    if 0 < balance_usd < MIN_ORDER_USD:
        issues.append(
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
    if funder or sig_type == auth.SIGNATURE_TYPE_POLY_1271:
        approvals_ok = True if balance_usd > 0 else None
    else:
        try:
            data = await _authed_request(
                "GET", "/balance-allowance",
                params={"asset_type": "COLLATERAL", "signature_type": "0"},
            )
            allow = _extract_allowance(data)
            if allow is not None:
                approvals_ok = allow > 0
                if not approvals_ok:
                    issues.append(
                        "collateral allowance is 0 — enable trading approvals for "
                        "this wallet on polymarket.com before going live"
                    )
        except Exception as e:
            logger.debug(f"allowance check failed: {e}")

    signer_ok: Optional[bool] = None
    if (
        sig_type == auth.SIGNATURE_TYPE_POLY_1271
        and balance_usd >= 1.5
        and not any("deposit wallet equals your signer" in i for i in issues)
    ):
        try:
            probe_sig = await verify_order_signer_live()
            if probe_sig.get("tested"):
                signer_ok = bool(probe_sig.get("ok"))
                if not signer_ok:
                    issues.append(probe_sig.get("reason") or _SIGNER_MISMATCH_HELP)
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
    }


async def get_positions(
    limit: int = 1000, *, settlement_status: str | None = None,
    paginate: bool = True, user: str | None = None, strict: bool = False,
) -> list[dict]:
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
    raw: list = []
    offset = 0
    for _ in range(20):
        data = await _get(
            f"{DATA_BASE}/positions",
            params={"user": addr, "limit": page_size, "offset": offset,
                    "sizeThreshold": 0.1},
        )
        if not isinstance(data, list):
            raise RuntimeError(
                f"positions fetch failed (offset {offset}) for {addr or 'own wallet'}"
            )
        if not data:
            break
        raw.extend(data)
        if not paginate or len(data) < page_size:
            break
        offset += page_size
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
    if user:
        addr = user
    else:
        try:
            addr = auth.trading_address()
        except Exception:
            return []
    out: list[dict] = []
    page = min(500, max(1, int(limit)))
    offset = 0
    for _ in range(10):
        data = await _get(
            f"{DATA_BASE}/activity",
            params={"user": addr, "limit": page, "offset": offset},
        )
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < page or len(out) >= limit:
            break
        offset += page
    return out


async def get_order(order_id: str) -> dict:
    data = await _authed_request("GET", f"/data/order/{order_id}")
    return {"order": data if isinstance(data, dict) else {}}


async def get_fills_for_order(order_id: str, limit: int = 200) -> list[dict]:
    try:
        data = await _authed_request("GET", "/data/trades")
    except PolymarketAPIError:
        return []
    rows = data.get("data") if isinstance(data, dict) else data
    out: list[dict] = []
    for t in rows or []:
        oid = str(t.get("taker_order_id") or "")
        maker_oids = [str(x) for x in (t.get("maker_orders") or [])]
        if str(order_id) not in ([oid] + maker_oids):
            continue
        price = _to_float(t.get("price"))
        out.append({
            "order_id": str(order_id),
            "action": "buy",
            "count_fp": _to_float(t.get("size")),
            "price_cents": int(round(price * 100)),
            "yes_price_dollars": price,
            "no_price_dollars": 1.0 - price,
            "side": "",
        })
    return out


async def get_fills_since(after_ts_unix: int, limit: int = 200) -> list[dict]:
    return []


def _deposit_wallet_issue() -> str:
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
    if not tick or tick <= 0:
        tick = 0.01
    dp = max(0, -int(round(math.log10(tick))))
    snapped = round(round(price / tick) * tick, dp)
    return min(round(1 - tick, dp), max(tick, snapped))


_VALID_ORDER_TYPES = ("GTC", "FOK", "FAK")


def _is_no_match_error(msg: str) -> bool:
    return "no orders found to match" in (msg or "").lower()


async def place_limit_order(
    *,
    ticker: str,
    side: str,
    action: str,
    count: int,
    price_cents: int,
    client_order_id: Optional[str] = None,
    order_type: str = "GTC",
) -> dict:
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
        if "invalid order payload" in str(e).lower():
            logger.warning(
                f"[order-payload] rejected: {ticker} {side}/{action} "
                f"type={order_type} price={price} size={size} tick={tick} "
                f"neg_risk={bool(meta.get('neg_risk'))} token={token_id} "
                f"sig_type={auth.get_signature_type()}"
            )
        raise
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
