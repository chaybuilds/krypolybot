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
_spot_cache: dict = {"at": 0.0, "px": {}}
_SPOT_TTL = 2.0
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
    y = utc_dt.year
    start = _nth_sunday(y, 3, 2).replace(hour=7, tzinfo=timezone.utc)
    end = _nth_sunday(y, 11, 1).replace(hour=6, tzinfo=timezone.utc)
    return -4 if start <= utc_dt < end else -5


def hourly_slug(asset: str, window_start_epoch: int) -> Optional[str]:
    name = HOURLY_SLUG_NAME.get(asset.upper())
    if not name:
        return None
    utc = datetime.fromtimestamp(window_start_epoch, tz=timezone.utc)
    et = utc + timedelta(hours=eastern_offset_hours(utc))
    h12 = et.hour % 12 or 12
    ampm = "am" if et.hour < 12 else "pm"
    return f"{name}-up-or-down-{_MONTHS[et.month - 1]}-{et.day}-{et.year}-{h12}{ampm}-et"


async def spots(assets) -> dict[str, float]:
    now = time.monotonic()
    wanted = [a.upper() for a in assets]
    if now - _spot_cache["at"] < _SPOT_TTL and all(a in _spot_cache["px"] for a in wanted):
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
    return {a: _spot_cache["px"][a] for a in wanted if a in _spot_cache["px"]}


async def hour_open(asset: str, window_start_epoch: int) -> Optional[float]:
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
