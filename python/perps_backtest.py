from __future__ import annotations

import asyncio
import bisect
import collections
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

import db
import perps_api
import perps_board
import perps_maker

logger = logging.getLogger("perps")

DAY_MS = 86_400_000
MAX_DAYS = 30
_PAGE_CONCURRENCY = 3
_MAX_RETRIES = 8
_sem: Optional[asyncio.Semaphore] = None
_cooldown_until = 0.0
_MIN_SPACING = 0.2
_next_slot = 0.0
_MAX_PAGES_PER_MARKET = 800
FEE_TIERS = [
    (0.000400, 0.000125), (0.000370, 0.000100), (0.000350, 0.000080),
    (0.000300, 0.000050), (0.000270, 0.000020), (0.000250, 0.0), (0.000200, -0.000050),
]
_TOUCH_FRESH_MS = 60_000
_TICK_FRESH_MS = 15_000
_EQUITY_POINTS = 240


def missing_ranges(cov: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    out = []
    cur = start
    for a, b in sorted(cov):
        if b < cur:
            continue
        if a > end:
            break
        if a > cur:
            out.append((cur, min(end, a - 1)))
        cur = max(cur, b + 1)
        if cur > end:
            break
    if cur <= end:
        out.append((cur, end))
    return out


def _pipe() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(_PAGE_CONCURRENCY)
    return _sem


async def _get_paced(params: dict, path: str = "/v1/info/trades") -> Optional[dict]:
    global _cooldown_until, _next_slot
    for attempt in range(_MAX_RETRIES):
        wait = _cooldown_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with _pipe():
                now = time.monotonic()
                slot = max(now, _next_slot)
                _next_slot = slot + _MIN_SPACING
                if slot > now:
                    await asyncio.sleep(slot - now)
                return await perps_api._get(path, params)
        except perps_api.PerpsAPIError as e:
            if e.status == 429 or e.status >= 500 or e.error == "network":
                back = e.retry_after or min(16.0, 0.5 * 2 ** attempt)
                _cooldown_until = max(_cooldown_until, time.monotonic() + back)
                continue
            raise
    return None


async def _fetch_window(iid: int, a: int, b: int, budget: list[int]) -> tuple[list[dict], bool]:
    rows: list[dict] = []
    end = b
    while True:
        if budget[0] <= 0:
            return rows, False
        budget[0] -= 1
        data = await _get_paced({"instrument_id": iid, "start_timestamp": a, "end_timestamp": end})
        if data is None:
            return rows, False
        page = (data or {}).get("data") or [] if isinstance(data, dict) else []
        if not page:
            return rows, True
        oldest = end
        for t in page:
            try:
                ts = int(t["timestamp"])
                oldest = min(oldest, ts)
                if t.get("settlement"):
                    continue
                rows.append({"trade_id": int(t["trade_id"]), "iid": iid, "side": t["side"],
                             "price": float(t["price"]), "qty": float(t["quantity"]), "ts": ts})
            except (KeyError, TypeError, ValueError):
                continue
        if not (isinstance(data, dict) and data.get("more")):
            return rows, True
        if oldest - 1 < a or oldest - 1 >= end:
            return rows, True
        end = oldest - 1


async def ensure_history(iid: int, start: int, end: int) -> dict:
    with db.get_db() as conn:
        gaps = missing_ranges(db.perps_coverage(conn, iid), start, end)
    chunks = []
    for a, b in gaps:
        s = a
        while s <= b:
            e = min(b, s + DAY_MS - 1)
            chunks.append((s, e))
            s = e + 1
    budget = [_MAX_PAGES_PER_MARKET]
    chunks.sort(key=lambda c: -c[1])
    results = await asyncio.gather(*(_fetch_window(iid, a, b, budget) for a, b in chunks),
                                   return_exceptions=True)
    fetched = 0
    complete = True
    with db.get_db() as conn:
        for (a, b), res in zip(chunks, results):
            if isinstance(res, BaseException):
                complete = False
                logger.info(f"perps backtest backfill {iid} [{a},{b}]: {res}")
                continue
            rows, ok = res
            fetched += db.insert_perps_trades(conn, rows) if rows else 0
            if ok:
                db.add_perps_coverage(conn, iid, a, b)
            else:
                complete = False
        cov = db.perps_coverage(conn, iid)
    covered_from = start
    for a, b in cov:
        if a <= end and b >= end - 60_000:
            covered_from = max(start, a)
    return {"fetched": fetched, "complete": complete, "coveredFrom": covered_from}


async def funding_history(iid: int, start: int, end: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    cursor_end = end
    for _ in range(20):
        try:
            data = await _get_paced({"instrument_id": iid, "start_timestamp": start,
                                     "end_timestamp": cursor_end}, "/v1/info/funding")
        except perps_api.PerpsAPIError:
            break
        page = (data or {}).get("data") or [] if isinstance(data, dict) else []
        if not page:
            break
        for r in page:
            try:
                out.append((int(r["timestamp"]), float(r["funding_rate"])))
            except (KeyError, TypeError, ValueError):
                continue
        oldest = min(int(r.get("timestamp") or cursor_end) for r in page)
        if not (isinstance(data, dict) and data.get("more")) or oldest - 1 < start:
            break
        cursor_end = oldest - 1
    return sorted(set(out))


class _Order:
    __slots__ = ("buy", "px", "qty", "rem", "live_at", "prio", "price", "level")

    def __init__(self, q: perps_maker.Quote, live_at: int, prio: float):
        self.buy = q.buy
        self.px = q.px
        self.price = q.price
        self.qty = float(q.qty)
        self.rem = float(q.qty)
        self.live_at = live_at
        self.prio = prio
        self.level = q.level


def _day(ts_ms: int) -> int:
    return ts_ms - ts_ms % DAY_MS


def simulate(trades: list[tuple], *, ticks: list[tuple] = (), funding: list[tuple] = (),
             inst: dict, cfg: dict, start_ms: int, end_ms: int,
             maker_fee: float = FEE_TIERS[0][1], queue_share: float = 0.5,
             latency_ms: int = 250, comp_score: Optional[float] = None,
             pool_usd: float = 0.0) -> dict:
    p = perps_maker.params(cfg)
    requote = float(cfg.get("perps_requote_bps") or 1.5)
    realtime = bool(cfg.get("perps_realtime", True))
    decide_every = 200 if realtime else int(float(cfg.get("perps_poll_sec") or 2) * 1000)
    vol_win = float(cfg.get("perps_vol_window_sec") or 10)
    pull_bps = float(cfg.get("perps_vol_pull_bps") or 0)
    cooldown_ms = int(float(cfg.get("perps_pull_cooldown_sec") or 20) * 1000)
    kill_bps = float(cfg.get("perps_markout_kill_bps") or 0)
    kill_n = int(cfg.get("perps_markout_min_fills") or 20)
    day_lim = float(cfg.get("perps_daily_loss_limit") or 0)
    vol_target = float(cfg.get("perps_daily_volume_target_usd") or 0)

    tick_ts = [t[0] for t in ticks]
    fund = list(funding)
    fi = 0

    pos = 0.0
    avg = 0.0
    realized = 0.0
    fees = 0.0
    fund_pnl = 0.0
    volume = 0.0
    n_fills = 0
    orders: list[_Order] = []
    last_long: Optional[tuple[int, float]] = None
    last_short: Optional[tuple[int, float]] = None
    spreads: collections.deque = collections.deque(maxlen=200)
    mids: collections.deque = collections.deque(maxlen=400)
    last_decision = -10**18
    pulled_until = 0
    halted_day = -1
    killed_day = -1
    markouts: collections.deque = collections.deque(maxlen=max(kill_n, 50))
    all_markouts: list[float] = []
    pending_mo: collections.deque = collections.deque()
    day_start_equity: dict[int, float] = {}
    day_volume: dict[int, float] = collections.defaultdict(float)
    max_inv = 0.0
    quoting_ms = 0
    score_ms = 0.0
    last_q_ts: Optional[int] = None
    last_q_score = 0.0
    equity_pts: list[tuple[int, float]] = []
    peak = 0.0
    max_dd = 0.0
    eq_step = max(60_000, (end_ms - start_ms) // _EQUITY_POINTS)
    next_eq = start_ms
    real_touch_events = 0
    touch_events = 0
    mid = None

    def touch_at(ts: int) -> Optional[tuple[float, float, bool]]:
        nonlocal real_touch_events, touch_events
        touch_events += 1
        if tick_ts:
            i = bisect.bisect_right(tick_ts, ts) - 1
            if i >= 0 and ts - tick_ts[i] <= _TICK_FRESH_MS:
                real_touch_events += 1
                return ticks[i][1], ticks[i][2], True
        if last_long and last_short and ts - last_long[0] <= _TOUCH_FRESH_MS \
                and ts - last_short[0] <= _TOUCH_FRESH_MS and last_short[1] < last_long[1]:
            return last_short[1], last_long[1], True
        ref = max((x for x in (last_long, last_short) if x), key=lambda x: x[0], default=None)
        if ref is None:
            return None
        half = (sorted(spreads)[len(spreads) // 2] / 2) if spreads else ref[1] * 1e-4
        return ref[1] - half, ref[1] + half, False

    def equity(m: float) -> float:
        return realized - fees + fund_pnl + (pos * (m - avg) if pos else 0.0)

    def fill(o: _Order, qty: float, ts: int) -> None:
        nonlocal pos, avg, realized, fees, volume, n_fills, max_inv
        signed = qty if o.buy else -qty
        notional = qty * o.px
        fees += notional * maker_fee
        volume += notional
        day_volume[_day(ts)] += notional
        n_fills += 1
        if pos == 0 or (pos > 0) == (signed > 0):
            avg = (avg * abs(pos) + o.px * qty) / (abs(pos) + qty)
            pos += signed
        else:
            closing = min(abs(pos), qty)
            realized += closing * (o.px - avg) * (1 if pos > 0 else -1)
            pos += signed
            if abs(pos) < 1e-12:
                pos, avg = 0.0, 0.0
            elif (pos > 0) == (signed > 0):
                avg = o.px
        max_inv = max(max_inv, abs(pos) * o.px)
        pending_mo.append((ts + 60_000, o.px, "long" if o.buy else "short"))

    def accrue_quote_time(ts: int) -> None:
        nonlocal quoting_ms, score_ms, last_q_ts
        if last_q_ts is not None and last_q_score > 0:
            dt = max(0, ts - last_q_ts)
            quoting_ms += dt
            score_ms += dt * last_q_score
        last_q_ts = ts

    def our_score(m: float) -> float:
        bids = [(o.px, o.rem) for o in orders if o.buy]
        asks = [(o.px, o.rem) for o in orders if not o.buy]
        return perps_board.harmonic(perps_board.side_score(bids, m), perps_board.side_score(asks, m))

    for ts, side, px, qty in trades:
        if ts < start_ms or ts > end_ms:
            continue
        while fi < len(fund) and fund[fi][0] <= ts:
            if mid and pos:
                fund_pnl -= pos * mid * fund[fi][1]
            fi += 1
        while pending_mo and pending_mo[0][0] <= ts and mid:
            _due, fpx, fside = pending_mo.popleft()
            mo = perps_maker.markout_bps(fside, fpx, mid)
            markouts.append(mo)
            all_markouts.append(mo)
        q_left = qty
        if side == "short":
            book = sorted((o for o in orders if o.buy and o.live_at <= ts and o.px >= px),
                          key=lambda o: -o.px)
        else:
            book = sorted((o for o in orders if not o.buy and o.live_at <= ts and o.px <= px),
                          key=lambda o: o.px)
        for o in book:
            if q_left <= 0:
                break
            through = (o.px > px) if o.buy else (o.px < px)
            take = min(o.rem, q_left if through else q_left * o.prio)
            if take <= 0:
                continue
            fill(o, take, ts)
            o.rem -= take
            q_left -= take
        orders = [o for o in orders if o.rem * o.px >= 1e-6]
        if side == "long":
            last_long = (ts, px)
        else:
            last_short = (ts, px)
        if last_long and last_short and abs(last_long[0] - last_short[0]) <= _TOUCH_FRESH_MS \
                and last_short[1] < last_long[1]:
            spreads.append(last_long[1] - last_short[1])
        tb = touch_at(ts)
        if tb is None:
            continue
        bid, ask, firm = tb
        mid = (bid + ask) / 2
        if firm:
            mids.append((ts / 1000.0, mid))
        eq = equity(mid)
        d = _day(ts)
        day_start_equity.setdefault(d, eq)
        while next_eq <= ts:
            equity_pts.append((next_eq, eq))
            next_eq += eq_step
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

        if ts - last_decision < decide_every:
            continue
        last_decision = ts
        accrue_quote_time(ts)
        stop = False
        if day_lim < 0 and eq - day_start_equity[d] <= day_lim:
            halted_day = d
        if vol_target > 0 and day_volume[d] >= vol_target:
            halted_day = d
        if halted_day == d:
            stop = True
        if kill_bps > 0 and killed_day != d and len(markouts) >= kill_n:
            recent = list(markouts)[-kill_n:]
            if sum(recent) / kill_n < -kill_bps:
                killed_day = d
                markouts.clear()
        if killed_day == d:
            stop = True
        if pull_bps > 0 and perps_maker.moved_bps(mids, ts / 1000.0, vol_win) > pull_bps:
            pulled_until = ts + cooldown_ms
        if ts < pulled_until:
            stop = True
        if stop:
            orders = []
            last_q_score = 0.0
            continue
        desired, _note = perps_maker.plan_quotes(best_bid=bid, best_ask=ask, mid=mid,
                                                 position_qty=pos, inst=inst, p=p)
        live = [{"order_id": i, "buy": o.buy, "price": o.px, "resting_quantity": o.rem}
                for i, o in enumerate(orders)]
        drop, place = perps_maker.diff_orders(live, desired, requote)
        dropped = set(drop)
        orders = [o for i, o in enumerate(orders) if i not in dropped]
        for q in place:
            improves = (q.px > bid) if q.buy else (q.px < ask)
            orders.append(_Order(q, ts + latency_ms, 1.0 if improves else queue_share))
        last_q_score = our_score(mid) if (any(o.buy for o in orders)
                                          and any(not o.buy for o in orders)) else 0.0

    if last_q_ts is not None:
        accrue_quote_time(end_ms if trades else last_q_ts)
    while fi < len(fund) and fund[fi][0] <= end_ms:
        if mid and pos:
            fund_pnl -= pos * mid * fund[fi][1]
        fi += 1
    final_mid = mid or 0.0
    unreal = pos * (final_mid - avg) if pos else 0.0
    net = realized - fees + fund_pnl + unreal
    span_ms = max(1, end_ms - start_ms)
    days = span_ms / DAY_MS
    uptime = quoting_ms / span_ms
    avg_score = (score_ms / quoting_ms) if quoting_ms else 0.0
    share = perps_board.est_share(avg_score, comp_score) if comp_score is not None else None
    reward_day = (pool_usd * uptime * share) if share is not None else None
    return {
        "fills": n_fills,
        "volumeUsd": volume,
        "volumePerDayUsd": volume / days,
        "feesUsd": fees,
        "realizedPnlUsd": realized,
        "fundingUsd": fund_pnl,
        "unrealizedPnlUsd": unreal,
        "netPnlUsd": net,
        "netPerDayUsd": net / days,
        "endPositionQty": pos,
        "maxInventoryUsd": max_inv,
        "maxDrawdownUsd": max_dd,
        "avgMarkoutBps": (sum(all_markouts) / len(all_markouts)) if all_markouts else None,
        "uptimePct": uptime * 100,
        "avgQuoteScore": avg_score,
        "estRewardPerDayUsd": reward_day,
        "estRewardSharePct": share * 100 if share is not None else None,
        "realTouchPct": (100.0 * real_touch_events / touch_events) if touch_events else 0.0,
        "days": days,
        "equity": [[t, round(v, 4)] for t, v in equity_pts],
    }


GRID_KEYS = {
    "perpsQuoteBps": (0.0, 50.0), "perpsSizeUsd": (10.0, 100_000.0),
    "perpsSkewBps": (0.0, 50.0), "perpsMaxInventoryUsd": (10.0, 1_000_000.0),
    "perpsRequoteBps": (0.1, 20.0), "perpsLevels": (1, 3),
    "perpsVolPullBps": (0.0, 200.0), "perpsMarkoutKillBps": (0.0, 200.0),
}
MAX_VARIANTS = 36


def grid_variants(grid: dict) -> list[dict]:
    import itertools
    axes = []
    for k, vals in (grid or {}).items():
        if k not in GRID_KEYS or not isinstance(vals, list):
            continue
        lo, hi = GRID_KEYS[k]
        clean = []
        for v in vals:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if lo <= f <= hi and f not in clean:
                clean.append(int(f) if k == "perpsLevels" else f)
        if clean:
            axes.append((k, clean[:8]))
    if not axes:
        return []
    out = []
    for combo in itertools.product(*(vals for _k, vals in axes)):
        patch = {k: v for (k, _vals), v in zip(axes, combo)}
        name = " · ".join(f"{_short(k)} {v:g}" for k, v in patch.items())
        out.append({"name": name, "config": patch})
        if len(out) >= MAX_VARIANTS:
            break
    return out


def _short(k: str) -> str:
    return {"perpsQuoteBps": "dist", "perpsSizeUsd": "size $", "perpsSkewBps": "skew",
            "perpsMaxInventoryUsd": "inv $", "perpsRequoteBps": "requote",
            "perpsLevels": "levels", "perpsVolPullBps": "pull",
            "perpsMarkoutKillBps": "kill"}.get(k, k)


def _merge(base: dict, patch: dict) -> dict:
    import config as _config
    camel = {**{k: v for k, v in base.items()}}
    for k, v in (patch or {}).items():
        camel[k] = v
    return _config.merge_with_defaults(camel)


async def run(cfg: dict, *, days: float = 7, markets: Optional[list[str]] = None,
              variants: Optional[list[dict]] = None, grid: Optional[dict] = None,
              fee_tier: int = 0, queue_share: float = 0.5, latency_ms: int = 250) -> dict:
    t0 = time.time()
    days = max(0.25, min(float(days), MAX_DAYS))
    end = int(time.time() * 1000)
    start = end - int(days * DAY_MS)
    board = await perps_board.snapshot(cfg)
    rows = {str(r["symbol"]).upper(): r for r in board.get("markets") or []}
    if markets:
        picks = [rows[s.upper()] for s in markets if s.upper() in rows]
    else:
        picks = perps_board.pick_markets(board, cfg)
    if not picks:
        return {"ok": False, "message": "No markets to backtest — pick some on the Perps tab."}
    tier = max(0, min(len(FEE_TIERS) - 1, int(fee_tier)))
    maker_fee = FEE_TIERS[tier][1]
    variants = list(variants or []) + grid_variants(grid or {})
    variants = variants[:MAX_VARIANTS]
    queue_share = max(0.0, min(1.0, float(queue_share)))
    latency_ms = max(0, min(10_000, int(latency_ms)))
    for r in picks:
        if r.get("competingScore") is None:
            try:
                bk = await perps_api.book(int(r["instrumentId"]), depth=100)
                r["competingScore"] = perps_board.note_book(
                    int(r["instrumentId"]), bk.get("bids") or [], bk.get("asks") or []).get("comp")
            except Exception:
                pass
    runs = [{"name": "Current settings", "config": cfg, "patch": {}}] + [
        {"name": str(v.get("name") or f"Variant {i + 1}"),
         "config": _merge(cfg, v.get("config") or {}), "patch": dict(v.get("config") or {})}
        for i, v in enumerate(variants)
    ]

    hist = await asyncio.gather(*(ensure_history(int(r["instrumentId"]), start, end) for r in picks),
                                return_exceptions=True)
    funds = await asyncio.gather(*(funding_history(int(r["instrumentId"]), start, end) for r in picks))

    data = []
    with db.get_db() as conn:
        for r, h, f in zip(picks, hist, funds):
            iid = int(r["instrumentId"])
            data.append({
                "row": r, "hist": h if not isinstance(h, BaseException) else {"error": str(h)},
                "trades": db.perps_trades_range(conn, iid, start, end),
                "ticks": db.perps_ticks_range(conn, iid, start, end),
                "funding": f,
            })

    out_runs = []
    for run_ in runs:
        per_market = []
        for d in data:
            r = d["row"]
            h = d["hist"]
            m_start = max(start, int(h.get("coveredFrom") or start)) if isinstance(h, dict) else start
            res = await asyncio.to_thread(
                simulate, d["trades"], ticks=d["ticks"], funding=d["funding"], inst=r,
                cfg=run_["config"], start_ms=m_start, end_ms=end, maker_fee=maker_fee,
                queue_share=queue_share, latency_ms=latency_ms,
                comp_score=r.get("competingScore"), pool_usd=float(r.get("poolUsd") or 0))
            res.update(symbol=r["symbol"], instrumentId=r["instrumentId"],
                       trades=len(d["trades"]), ticks=len(d["ticks"]),
                       dataComplete=bool(isinstance(h, dict) and h.get("complete")),
                       dataError=h.get("error") if isinstance(h, dict) else None)
            per_market.append(res)
        tot_days = max((m["days"] for m in per_market), default=days)
        totals = {k: sum(m[k] or 0 for m in per_market) for k in (
            "fills", "volumeUsd", "feesUsd", "realizedPnlUsd", "fundingUsd",
            "unrealizedPnlUsd", "netPnlUsd", "maxInventoryUsd")}
        rewards = [m["estRewardPerDayUsd"] for m in per_market if m["estRewardPerDayUsd"] is not None]
        totals["volumePerDayUsd"] = totals["volumeUsd"] / max(tot_days, 1e-9)
        totals["netPerDayUsd"] = totals["netPnlUsd"] / max(tot_days, 1e-9)
        totals["estRewardPerDayUsd"] = sum(rewards) if rewards else None
        mos = [(m["avgMarkoutBps"], m["fills"]) for m in per_market if m["avgMarkoutBps"] is not None]
        wsum = sum(n for _, n in mos)
        totals["avgMarkoutBps"] = (sum(a * n for a, n in mos) / wsum) if wsum else None
        out_runs.append({"name": run_["name"], "config": run_["patch"],
                         "totals": totals, "markets": per_market})

    return {
        "ok": True,
        "startMs": start, "endMs": end, "days": days,
        "feeTier": tier, "makerFeeBps": maker_fee * 1e4,
        "queueShare": queue_share, "latencyMs": latency_ms,
        "markets": [r["symbol"] for r in picks],
        "runs": out_runs,
        "elapsedSec": round(time.time() - t0, 1),
        "assumptions": [
            "Our orders never move the market: real fills can only be fewer and worse.",
            "At-price prints fill us fully only if our quote improved the touch; otherwise by the queue share.",
            "Index-gap pulls and post-only rejects are not modelled.",
            "Rewards assume today's competing depth and that you meet the 1% maker-share bar.",
        ],
    }
