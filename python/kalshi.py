from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import terminal as term
from terminal import _fetch, _now_iso, _sentinel_free, num

logger = logging.getLogger("krypt.kalshi")

TTL_MARKET = 20.0
TTL_CANDLES = 120.0



def _from_cents(v: Any) -> Optional[float]:
    n = num(v)
    if n is None:
        return None
    return _sentinel_free(round(n, 2))


def _from_dollars(v: Any) -> Optional[float]:
    n = num(v)
    if n is None:
        return None
    return _sentinel_free(round(n * 100.0, 2))


def _cents(v: Any) -> Optional[float]:
    n = num(v)
    if n is None:
        return None
    written_decimal = isinstance(v, str) and "." in v
    if abs(n) <= 1.5 and (written_decimal or not float(n).is_integer()):
        n *= 100.0
    return _sentinel_free(round(n, 2))


def _iso(v: Any) -> Optional[str]:
    return v if (v and isinstance(v, str)) else None


def _hours_to(close_iso: Optional[str]) -> Optional[float]:
    return term._hours_to(close_iso)


def _strike(v: Any) -> Optional[float]:
    return num(v)


def market_row(m: dict, observed_at: Optional[str] = None,
               event: Optional[dict] = None) -> Optional[dict]:
    if not isinstance(m, dict):
        return None
    ticker = str(m.get("ticker") or "").strip()
    if not ticker:
        return None
    ev = event if isinstance(event, dict) else {}

    yes_bid = _from_cents(m.get("yes_bid"))
    if yes_bid is None:
        yes_bid = _from_dollars(m.get("yes_bid_dollars"))
    yes_ask = _from_cents(m.get("yes_ask"))
    if yes_ask is None:
        yes_ask = _from_dollars(m.get("yes_ask_dollars"))
    last = _from_cents(m.get("last_price"))
    if last is None:
        last = _from_dollars(m.get("last_price_dollars"))

    no_bid = _from_cents(m.get("no_bid")) or _from_dollars(m.get("no_bid_dollars"))
    no_ask = _from_cents(m.get("no_ask")) or _from_dollars(m.get("no_ask_dollars"))
    if yes_ask is None and no_bid is not None:
        yes_ask = _sentinel_free(round(100.0 - no_bid, 2))
    if yes_bid is None and no_ask is not None:
        yes_bid = _sentinel_free(round(100.0 - no_ask, 2))

    spread = (round(yes_ask - yes_bid, 2)
              if (yes_bid is not None and yes_ask is not None) else None)
    mid = (round((yes_ask + yes_bid) / 2.0, 2)
           if spread is not None else None)
    prob = mid if mid is not None else last

    close_time = _iso(m.get("close_time")) or _iso(m.get("expiration_time"))
    status = str(m.get("status") or "").lower()
    if status in ("finalized", "settled"):
        norm_status = "settled"
    elif status in ("closed",):
        norm_status = "closed"
    elif status in ("active", "open", "initialized"):
        norm_status = "open"
    else:
        norm_status = "unknown"

    result = str(m.get("result") or "").lower()
    resolved = "yes" if result == "yes" else ("no" if result == "no" else None)

    vol24 = num(m.get("volume_24h"))
    if vol24 is None:
        vol24 = num(m.get("volume_24h_fp"))
    vol = num(m.get("volume")) or num(m.get("volume_fp"))
    liq = num(m.get("liquidity_dollars"))
    if liq is None:
        lc = num(m.get("liquidity"))
        liq = round(lc / 100.0, 2) if lc is not None else None

    obs = observed_at or _now_iso()
    stamp = {"source": "kalshi", "observedAt": obs, "ageSec": term._age_sec(obs)}
    sources = {}
    for field, val in (("probability", prob), ("bid", yes_bid), ("ask", yes_ask),
                       ("spread", spread), ("mid", mid), ("volume24h", vol24),
                       ("liquidity", liq), ("closeTime", close_time),
                       ("status", norm_status)):
        if val is not None:
            sources[field] = dict(stamp)

    unknowns = []
    if prob is None:
        unknowns.append({
            "field": "probability",
            "reason": ("Kalshi has no quote on this market — neither side of "
                       "the book has a resting order."),
        })

    title = (str(m.get("title") or "").strip()
             or str(m.get("subtitle") or "").strip() or ticker)
    sub = str(m.get("yes_sub_title") or m.get("subtitle") or "").strip()

    event_ticker = str(m.get("event_ticker") or ev.get("event_ticker") or "")
    event_title = str(ev.get("title") or "").strip()
    event_sub = str(ev.get("sub_title") or "").strip()

    resolves_at = (_iso(ev.get("strike_date"))
                   or _iso(m.get("expected_expiration_time"))
                   or _iso(m.get("expiration_time"))
                   or close_time)

    parts = [p for p in (event_title, event_sub, title, sub) if p]
    seen: set[str] = set()
    match_bits: list[str] = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        match_bits.append(p)

    return {
        "conditionId": ticker,
        "venue": "kalshi",
        "title": title,
        "subtitle": sub or None,
        "noSubtitle": str(m.get("no_sub_title") or "").strip() or None,
        "matchText": " ".join(match_bits).strip(),
        "eventTicker": event_ticker or None,
        "eventTitle": event_title or None,
        "eventSubtitle": event_sub or None,
        "strikeType": str(m.get("strike_type") or "").lower() or None,
        "floorStrike": _strike(m.get("floor_strike")),
        "capStrike": _strike(m.get("cap_strike")),
        "strikeDate": _iso(ev.get("strike_date")),
        "resolvesAt": resolves_at,
        "slug": ticker,
        "eventSlug": event_ticker or None,
        "seriesTicker": str(m.get("series_ticker")
                            or ev.get("series_ticker") or "") or None,
        "category": str(m.get("category") or "").lower() or None,
        "status": norm_status,
        "outcomes": ["Yes", "No"],
        "probability": prob,
        "bid": yes_bid, "ask": yes_ask, "spread": spread, "mid": mid,
        "volume24hUsd": vol24,
        "volumeTotalUsd": vol,
        "liquidityUsd": liq,
        "priceChange24h": None,
        "priceChange1h": None,
        "closeTime": close_time,
        "hoursToClose": _hours_to(close_time),
        "negRisk": None,
        "tickSize": None,
        "minOrderSize": None,
        "yesTokenId": None, "noTokenId": None,
        "resolvedOutcome": resolved,
        "sources": sources,
        "unknowns": unknowns,
    }


async def market(ticker: str) -> Optional[dict]:
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    data, observed = await _fetch("kalshi", f"/markets/{t}", None, ttl=TTL_MARKET)
    if not isinstance(data, dict):
        return None
    raw = data.get("market") if isinstance(data.get("market"), dict) else data
    return market_row(raw, observed)


CATEGORIES = [
    "Economics", "Politics", "Financials", "Crypto", "Climate and Weather",
    "Companies", "Sports", "Entertainment", "Health",
    "Science and Technology", "World",
]

TTL_SERIES = 1800.0


async def series_index() -> tuple[list[dict], bool]:
    rows: list[dict] = []
    ok_any = False
    for cat in CATEGORIES:
        data, _ = await _fetch(
            "kalshi", "/series", {"category": cat}, ttl=TTL_SERIES, timeout=20.0)
        if not isinstance(data, dict):
            continue
        ok_any = True
        for sx in data.get("series") or []:
            if not isinstance(sx, dict):
                continue
            ticker = str(sx.get("ticker") or "").strip()
            title = str(sx.get("title") or "").strip()
            if not ticker or not title:
                continue
            rows.append({
                "ticker": ticker,
                "title": title,
                "category": (str(sx.get("category") or cat)).strip(),
                "settlementSources": [
                    str(x.get("name") or x.get("url") or "")
                    for x in (sx.get("settlement_sources") or [])
                    if isinstance(x, dict)
                ],
                "contractUrl": str(sx.get("contract_url") or "") or None,
            })
    return rows, ok_any


TTL_EVENTS = 300.0


async def events_in_series(series_ticker: str, limit: int = 200) -> list[dict]:
    st = str(series_ticker or "").strip().upper()
    if not st:
        return []
    data, observed = await _fetch(
        "kalshi", "/events",
        {"series_ticker": st, "status": "open",
         "with_nested_markets": "true", "limit": int(limit)},
        ttl=TTL_EVENTS, timeout=20.0)
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue
        for raw in ev.get("markets") or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("mve_collection_ticker"):
                continue
            row = market_row(raw, observed, ev)
            if row:
                row["seriesTicker"] = row["seriesTicker"] or st
                out.append(row)
    return out


async def markets_in_series(series_ticker: str, limit: int = 200) -> list[dict]:
    st = str(series_ticker or "").strip().upper()
    if not st:
        return []
    data, observed = await _fetch(
        "kalshi", "/markets",
        {"series_ticker": st, "status": "open", "limit": int(limit)},
        ttl=TTL_MARKET, timeout=20.0)
    if not isinstance(data, dict):
        return []
    out = []
    for raw in data.get("markets") or []:
        if isinstance(raw, dict) and raw.get("mve_collection_ticker"):
            continue
        row = market_row(raw, observed)
        if row:
            row["seriesTicker"] = row["seriesTicker"] or st
            out.append(row)
    return out


def _candle_price(c: dict, group: str, key: str) -> Optional[float]:
    d = c.get(group)
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None:
        return _from_dollars(d.get(f"{key}_dollars"))
    return _cents(v)


def normalize_candles(raw: list) -> list[dict]:
    by_ts: dict[int, float] = {}
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        ts = num(c.get("end_period_ts"))
        if ts is None or ts <= 0:
            continue
        p = _candle_price(c, "price", "close")
        if p is None:
            p = _candle_price(c, "price", "mean")
        if p is None:
            bid = _candle_price(c, "yes_bid", "close")
            ask = _candle_price(c, "yes_ask", "close")
            if bid is not None and ask is not None:
                p = round((bid + ask) / 2.0, 2)
        if p is None:
            continue
        by_ts[int(ts)] = p
    return [{"t": t, "p": by_ts[t]} for t in sorted(by_ts)]


async def candles(ticker: str, *, start_ts: int, end_ts: int,
                  period_interval: int = 60,
                  series_ticker: str = "") -> list[dict]:
    t = str(ticker or "").strip().upper()
    if not t:
        return []
    series = (series_ticker or "").strip().upper() or t.split("-")[0]
    params = {
        "start_ts": int(start_ts), "end_ts": int(end_ts),
        "period_interval": int(period_interval),
    }
    for path in (f"/series/{series}/markets/{t}/candlesticks",
                 f"/historical/markets/{t}/candlesticks"):
        data, _ = await _fetch("kalshi", path, params, ttl=TTL_CANDLES,
                               timeout=20.0)
        if isinstance(data, dict) and isinstance(data.get("candlesticks"), list):
            pts = normalize_candles(data["candlesticks"])
            if pts:
                return pts
    return []


def web_url(ticker: str, event_ticker: Optional[str] = None) -> str:
    ev = (event_ticker or str(ticker or "").split("-")[0]).strip().lower()
    return f"https://kalshi.com/markets/{ev}" if ev else "https://kalshi.com"


VENUE_NOTE = (
    "Kalshi is a CFTC-regulated US exchange; Polymarket is not the same thing "
    "legally. Fees, settlement guarantees and the resolution source are each "
    "venue's own, so two markets asking the same question can still settle "
    "differently."
)

SPREAD_CAVEATS = [
    "The two markets may resolve on different sources, or on different dates.",
    "This app does not trade on Kalshi. These figures are reference data — a "
    "second opinion on the same question, nothing more.",
    "Kalshi quotes are exchange prices in cents; Polymarket quotes are on-chain "
    "book prices. Neither is a fill until somebody takes it.",
]
