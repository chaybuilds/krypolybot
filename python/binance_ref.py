"""Binance reference prices for Polymarket's HOURLY crypto up/down markets.

The hourly series ("Bitcoin Up or Down - October 5, 6AM ET", slug
`bitcoin-up-or-down-october-5-2026-6am-et` — the year is in the slug; the
year-less form is LAST year's closed market) is a different product from the
5m/15m/4h series: it resolves on the Binance <ASSET>/USDT 1-hour CANDLE —
Up when the candle's close ≥ its open (verified against the live market rules
2026-10-05). So its strike is the candle open and its spot is Binance's.

Reads go to `data-api.binance.vision`, Binance's public market-data host,
which serves the same candles without the regional blocks of api.binance.com.
Read-only, keyless, cached.

US Eastern time is computed here rather than via zoneinfo: Windows Pythons
ship without the tz database and the frozen app doesn't bundle `tzdata`.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

BASE = "https://data-api.binance.vision/api/v3"

HOURLY_SLUG_NAME = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp",
                    "DOGE": "dogecoin", "BNB": "bnb", "HYPE": "hype"}
FUTURES_SETTLED = {"HYPE"}
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")

_client: Optional[httpx.AsyncClient] = None
_spot_cache: dict = {"at": 0.0, "px": {}, "seen": {}}
_SPOT_TTL = 2.0
_SPOT_MAX_AGE = 10.0
_open_cache: dict[tuple[str, int], float] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "KryptPolyBot/1.0"})
    return _client


async def close_clients() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None


def symbol(asset: str) -> str:
    return f"{asset.upper()}USDT"



def _nth_sunday(year: int, month: int, n: int) -> datetime:
    d = datetime(year, month, 1)
    first = d + timedelta(days=(6 - d.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def eastern_offset_hours(utc_dt: datetime) -> int:
    """-4 during US daylight time, -5 otherwise."""
    y = utc_dt.year
    start = _nth_sunday(y, 3, 2).replace(hour=7, tzinfo=timezone.utc)
    end = _nth_sunday(y, 11, 1).replace(hour=6, tzinfo=timezone.utc)
    return -4 if start <= utc_dt < end else -5


def hourly_slug(asset: str, window_start_epoch: int) -> Optional[str]:
    """`bitcoin-up-or-down-october-5-6am-et` for the hour starting at
    `window_start_epoch` (UTC, on the hour), or None for unlisted assets."""
    name = HOURLY_SLUG_NAME.get(asset.upper())
    if not name:
        return None
    utc = datetime.fromtimestamp(window_start_epoch, tz=timezone.utc)
    et = utc + timedelta(hours=eastern_offset_hours(utc))
    h12 = et.hour % 12 or 12
    ampm = "am" if et.hour < 12 else "pm"
    return f"{name}-up-or-down-{_MONTHS[et.month - 1]}-{et.day}-{et.year}-{h12}{ampm}-et"



async def spots(assets) -> dict[str, float]:
    """Mid of Binance's best bid/ask per asset (USDT), cached ~2s. An asset
    whose last good read is older than `_SPOT_MAX_AGE` is left out."""
    now = time.monotonic()
    wanted = [a.upper() for a in assets]
    seen = _spot_cache.setdefault("seen", {})
    if all(now - seen.get(a, -1e18) < _SPOT_TTL for a in wanted):
        return {a: _spot_cache["px"][a] for a in wanted}
    syms = "[" + ",".join(f'"{symbol(a)}"' for a in wanted) + "]"
    try:
        r = await _get_client().get(f"{BASE}/ticker/bookTicker", params={"symbols": syms})
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        rows = []
    px = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            a = str(row["symbol"]).removesuffix("USDT")
            px[a] = (float(row["bidPrice"]) + float(row["askPrice"])) / 2
        except (KeyError, TypeError, ValueError):
            continue
    if px:
        _spot_cache.update(at=now, px={**_spot_cache["px"], **px})
        seen.update({a: now for a in px})
    return {a: _spot_cache["px"][a] for a in wanted
            if a in _spot_cache["px"] and now - seen.get(a, -1e18) <= _SPOT_MAX_AGE}


async def hour_open(asset: str, window_start_epoch: int) -> Optional[float]:
    """The open of the Binance 1h candle starting at `window_start_epoch` —
    the hourly market's strike. Cached once known (it never changes)."""
    key = (asset.upper(), int(window_start_epoch))
    if key in _open_cache:
        return _open_cache[key]
    try:
        r = await _get_client().get(f"{BASE}/klines", params={
            "symbol": symbol(asset), "interval": "1h",
            "startTime": int(window_start_epoch) * 1000, "limit": 1})
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        return None
    if not rows or int(rows[0][0]) != int(window_start_epoch) * 1000:
        return None
    try:
        px = float(rows[0][1])
    except (TypeError, ValueError, IndexError):
        return None
    _open_cache[key] = px
    if len(_open_cache) > 200:
        for k in sorted(_open_cache, key=lambda k: k[1])[:50]:
            _open_cache.pop(k, None)
    return px
