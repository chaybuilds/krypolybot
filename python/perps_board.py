from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import perps_api

logger = logging.getLogger("perps")

DAILY_REWARD_POOL_USD = 75_000.0
TIER_CAP_USD = 100_000.0
TIERS = ((5.0, 1.0), (10.0, 0.25), (20.0, 0.10))
MAKER_SHARE_MIN = 0.01

_SNAPSHOT_TTL = 3.0
_BOOK_TTL = 30.0
_BOOKS_PER_REFRESH = 8
_VOLUME_TTL = 1800.0
_VOLUMES_PER_REFRESH = 10

_books: dict[int, dict] = {}
_volumes: dict[int, dict] = {}
_snap: dict = {"at": 0.0, "data": None}
_lock = asyncio.Lock()


def tier_weight(dist_bps: float) -> float:
    for edge, w in TIERS:
        if dist_bps <= edge + 1e-9:
            return w
    return 0.0


def side_score(levels: list[tuple[float, float]], mid: float, *, cap: bool = True) -> float:
    tiers = [0.0, 0.0, 0.0]
    for px, qty in levels:
        if mid <= 0 or px <= 0 or qty <= 0:
            continue
        d = abs(px - mid) / mid * 1e4
        for i, (edge, _w) in enumerate(TIERS):
            if d <= edge + 1e-9:
                tiers[i] += px * qty
                break
    lim = TIER_CAP_USD if cap else float("inf")
    return sum(min(n, lim) * w for n, (_e, w) in zip(tiers, TIERS))


def harmonic(a: float, b: float) -> float:
    return 2 * a * b / (a + b) if a > 0 and b > 0 else 0.0


def book_depth(bids: list, asks: list, own: Optional[list[tuple[bool, float, float]]] = None) -> dict:
    b = [(float(p), float(q)) for p, q in bids]
    a = [(float(p), float(q)) for p, q in asks]
    if not b or not a:
        return {"bestBid": b[0][0] if b else None, "bestAsk": a[0][0] if a else None,
                "mid": None, "compBid": 0.0, "compAsk": 0.0, "comp": 0.0}
    mid = (b[0][0] + a[0][0]) / 2
    if own:
        b = _subtract(b, [(px, q) for buy, px, q in own if buy])
        a = _subtract(a, [(px, q) for buy, px, q in own if not buy])
    cb, ca = side_score(b, mid, cap=False), side_score(a, mid, cap=False)
    return {"bestBid": float(bids[0][0]), "bestAsk": float(asks[0][0]), "mid": mid,
            "compBid": cb, "compAsk": ca, "comp": harmonic(cb, ca)}


def _subtract(levels: list[tuple[float, float]], mine: list[tuple[float, float]]) -> list:
    out = []
    for px, q in levels:
        for mpx, mq in mine:
            if abs(mpx - px) <= max(1e-12, abs(px) * 1e-9):
                q -= mq
        if q > 1e-12:
            out.append((px, q))
    return out


def quote_score(size_usd: float, quote_bps: float, levels: int, step_bps: float) -> float:
    tiers = [0.0, 0.0, 0.0]
    for k in range(max(1, levels)):
        d = quote_bps + k * step_bps
        for i, (edge, _w) in enumerate(TIERS):
            if d <= edge + 1e-9:
                tiers[i] += size_usd
                break
    return sum(min(n, TIER_CAP_USD) * w for n, (_e, w) in zip(tiers, TIERS))


def est_share(mine: float, competing: float) -> float:
    if mine <= 0:
        return 0.0
    if competing <= 0:
        return 1.0
    a, c = mine ** 0.65, competing ** 0.65
    return a / (a + c)


async def _refresh_book(iid: int) -> None:
    try:
        bk = await perps_api.book(iid, depth=100)
    except Exception as e:
        logger.debug(f"perps board book {iid}: {e}")
        return
    note_book(iid, bk.get("bids") or [], bk.get("asks") or [])


def note_book(iid: int, bids: list, asks: list,
              own: Optional[list[tuple[bool, float, float]]] = None) -> dict:
    d = book_depth(bids, asks, own)
    d["at"] = time.time()
    _books[iid] = d
    return d


async def _refresh_volume(iid: int) -> None:
    now = int(time.time() * 1000)
    try:
        kl = await perps_api.klines(iid, "1d", now - 7 * 86_400_000)
    except Exception as e:
        logger.debug(f"perps board klines {iid}: {e}")
        return
    vol = 0.0
    for k in kl:
        try:
            vol += float(k[5]) * float(k[4])
        except (TypeError, ValueError, IndexError):
            continue
    _volumes[iid] = {"at": time.time(), "vol7": vol}


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def snapshot(cfg: dict, *, priority: tuple[int, ...] = (), force: bool = False) -> dict:
    now = time.time()
    if not force and _snap["data"] is not None and now - _snap["at"] < _SNAPSHOT_TTL:
        return _snap["data"]
    async with _lock:
        if not force and _snap["data"] is not None and time.time() - _snap["at"] < _SNAPSHOT_TTL:
            return _snap["data"]
        try:
            inst, ticks = await asyncio.gather(perps_api.instruments(), perps_api.tickers())
        except Exception as e:
            data = {"ok": False, "error": str(e), "markets": [], "fetchedAt": int(now * 1000)}
            if _snap["data"]:
                data = {**_snap["data"], "ok": False, "error": str(e)}
            return data
        by_tick = {int(t.get("instrument_id")): t for t in ticks if t.get("instrument_id") is not None}

        live = [int(i["instrument_id"]) for i in inst if not i.get("close_only")]
        stale_books = sorted(live, key=lambda i: _books.get(i, {}).get("at", 0.0))
        want = [i for i in priority if i in by_tick]
        want += [i for i in stale_books if i not in want
                 and now - _books.get(i, {}).get("at", 0.0) > _BOOK_TTL][:_BOOKS_PER_REFRESH]
        stale_vol = [i for i in sorted(live, key=lambda i: _volumes.get(i, {}).get("at", 0.0))
                     if now - _volumes.get(i, {}).get("at", 0.0) > _VOLUME_TTL][:_VOLUMES_PER_REFRESH]
        await asyncio.gather(*(_refresh_book(i) for i in want),
                             *(_refresh_volume(i) for i in stale_vol))

        data = build(cfg, inst, by_tick)
        data["fetchedAt"] = int(time.time() * 1000)
        _snap.update(at=time.time(), data=data)
        return data


def build(cfg: dict, inst: list[dict], by_tick: dict[int, dict]) -> dict:
    active = [i for i in inst if not i.get("close_only")]
    pool = DAILY_REWARD_POOL_USD / max(1, len(active))
    size = float(cfg.get("perps_size_usd") or 100.0)
    qbps = float(cfg.get("perps_quote_bps") or 0.0)
    levels = int(cfg.get("perps_levels") or 1)
    step = float(cfg.get("perps_level_step_bps") or 3.0)
    mine = quote_score(size, qbps, levels, step)
    rows = []
    for i in inst:
        iid = int(i["instrument_id"])
        t = by_tick.get(iid, {})
        bk = _books.get(iid)
        vol = _volumes.get(iid)
        mark = _f(t.get("mark_price"))
        index = _f(t.get("index_price"))
        mid = _f(t.get("mid_price")) or (bk or {}).get("mid")
        oi = _f(t.get("open_interest")) or 0.0
        fund = _f(t.get("funding_rate"))
        spread = None
        if bk and bk.get("bestBid") and bk.get("bestAsk") and bk.get("mid"):
            spread = (bk["bestAsk"] - bk["bestBid"]) / bk["mid"] * 1e4
        comp = bk.get("comp") if bk else None
        share = est_share(mine, comp) if comp is not None else None
        rows.append({
            "instrumentId": iid,
            "symbol": i.get("symbol"),
            "displaySymbol": i.get("display_symbol") or i.get("symbol"),
            "base": i.get("base_asset"),
            "category": i.get("category"),
            "closeOnly": bool(i.get("close_only")),
            "isolatedOnly": bool(i.get("isolated_only")),
            "maxLeverage": int(i.get("max_leverage") or 1),
            "minNotional": _f(i.get("min_notional")) or 10.0,
            "priceDecimals": int(i.get("price_decimals") or 0),
            "quantityDecimals": int(i.get("quantity_decimals") or 0),
            "markPrice": mark, "indexPrice": index, "midPrice": mid,
            "indexDevBps": ((mid - index) / index * 1e4) if (mid and index) else None,
            "spreadBps": spread,
            "fundingRate": fund,
            "fundingAprPct": (fund * 24 * 365 * 100) if fund is not None else None,
            "openInterestUsd": oi * (mark or 0.0),
            "vol7dUsd": vol["vol7"] if vol else None,
            "avgDailyVolUsd": (vol["vol7"] / 7.0) if vol else None,
            "competingScore": comp,
            "competingBidScore": bk.get("compBid") if bk else None,
            "competingAskScore": bk.get("compAsk") if bk else None,
            "depthAgeSec": (time.time() - bk["at"]) if bk else None,
            "poolUsd": 0.0 if i.get("close_only") else pool,
            "estSharePct": share * 100 if share is not None else None,
            "estRewardUsd": (share * pool) if (share is not None and not i.get("close_only")) else None,
        })
    total7 = sum(r["vol7dUsd"] or 0.0 for r in rows)
    covered = sum(1 for r in rows if r["vol7dUsd"] is not None)
    return {
        "ok": True,
        "markets": rows,
        "poolPerMarketUsd": pool,
        "dailyPoolUsd": DAILY_REWARD_POOL_USD,
        "activeMarkets": len(active),
        "exchangeVol7dUsd": total7,
        "volumeCoverage": covered / max(1, len(rows)),
        "makerVol7dNeededUsd": total7 * MAKER_SHARE_MIN,
        "myQuoteScore": mine,
    }


def pick_markets(board: dict, cfg: dict) -> list[dict]:
    rows = [r for r in board.get("markets") or [] if not r.get("closeOnly")]
    size = float(cfg.get("perps_size_usd") or 100.0)
    rows = [r for r in rows if (r.get("minNotional") or 10.0) <= size]
    mode = cfg.get("perps_select_mode") or "rewards"
    if mode == "manual":
        want = [s.upper() for s in cfg.get("perps_markets") or []]
        by_sym = {str(r.get("symbol") or "").upper(): r for r in rows}
        return [by_sym[s] for s in want if s in by_sym]
    cats = set(cfg.get("perps_categories") or [])
    if cats:
        rows = [r for r in rows if r.get("category") in cats]
    n = int(cfg.get("perps_market_count") or 4)
    if mode == "volume":
        rows.sort(key=lambda r: -(r.get("avgDailyVolUsd") or 0.0))
    else:
        rows.sort(key=lambda r: (r.get("estRewardUsd") is None,
                                 -(r.get("estRewardUsd") or 0.0),
                                 -(r.get("avgDailyVolUsd") or 0.0)))
    return rows[:n]


def reset_cache() -> None:
    _books.clear()
    _volumes.clear()
    _snap.update(at=0.0, data=None)
