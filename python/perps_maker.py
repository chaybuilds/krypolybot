from __future__ import annotations

import asyncio
import collections
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import db
import perps_api
import perps_board
import perps_ws

logger = logging.getLogger("perps")

_COID_PREFIX = "0b0710"
_FILL_SYNC_SEC = 5.0
_ACCOUNT_SLOW_SEC = 300.0
_AUTOCANCEL_EVERY_SEC = 15.0
_RESELECT_SEC = 60.0
_MARKOUT_DELAY_MS = 60_000
_FIRST_IMPORT_LOOKBACK_MS = 7 * 86_400_000
_FLATTEN_SLIP = 0.005


def new_coid() -> str:
    return _COID_PREFIX + secrets.token_hex(13)


def is_ours(order: dict) -> bool:
    return str(order.get("client_order_id") or order.get("coid") or "").startswith(_COID_PREFIX)


@dataclass
class Quote:
    buy: bool
    price: str
    qty: str
    px: float
    level: int


def plan_quotes(*, best_bid: Optional[float], best_ask: Optional[float], mid: Optional[float],
                position_qty: float, inst: dict, p: dict) -> tuple[list[Quote], str]:
    if not mid or mid <= 0 or not best_bid or not best_ask:
        return [], "no two-sided book"
    pdec = int(inst.get("priceDecimals", inst.get("price_decimals", 0)) or 0)
    qdec = int(inst.get("quantityDecimals", inst.get("quantity_decimals", 0)) or 0)
    min_notional = float(inst.get("minNotional", inst.get("min_notional", 10)) or 10)
    inv_usd = position_qty * mid
    max_inv = max(1.0, float(p["max_inv_usd"]))
    ratio = max(-1.0, min(1.0, inv_usd / max_inv))
    center = mid * (1 - ratio * float(p["skew_bps"]) / 1e4)
    allow_bid = inv_usd < max_inv
    allow_ask = inv_usd > -max_inv
    note = ""
    if not allow_bid:
        note = "long inventory cap — bids paused"
    elif not allow_ask:
        note = "short inventory cap — asks paused"
    out: list[Quote] = []
    for k in range(max(1, int(p["levels"]))):
        dist = (float(p["quote_bps"]) + k * float(p["step_bps"])) / 1e4
        for buy in (True, False):
            if (buy and not allow_bid) or (not buy and not allow_ask):
                continue
            target = center * (1 - dist) if buy else center * (1 + dist)
            step = perps_api.price_step(target, pdec)
            if buy:
                target = min(target, best_ask - step)
            else:
                target = max(target, best_bid + step)
            if target <= 0:
                continue
            price = perps_api.fmt_price(target, pdec, side="buy" if buy else "sell")
            px = float(price)
            if buy and px >= center:
                price = perps_api.fmt_price(px - step, pdec, side="buy")
            elif not buy and px <= center:
                price = perps_api.fmt_price(px + step, pdec, side="sell")
            px = float(price)
            qty = perps_api.fmt_qty(float(p["size_usd"]) / px, qdec)
            if float(qty) * px < min_notional:
                note = note or f"quote size below the ${min_notional:g} minimum"
                continue
            out.append(Quote(buy=buy, price=price, qty=qty, px=px, level=k))
    bids = [q.px for q in out if q.buy]
    asks = [q.px for q in out if not q.buy]
    if bids and asks and max(bids) >= min(asks):
        return [], "quotes would cross"
    return out, note


def diff_orders(live: list[dict], desired: list[Quote], requote_bps: float
                ) -> tuple[list[int], list[Quote]]:
    remaining = list(live)
    place: list[Quote] = []
    for q in sorted(desired, key=lambda q: q.level):
        match = None
        for o in remaining:
            if bool(o["buy"]) != q.buy:
                continue
            opx = float(o["price"])
            if abs(opx - q.px) / q.px * 1e4 > requote_bps + 1e-9:
                continue
            rest = float(o.get("resting_quantity") or o.get("quantity") or 0)
            if rest < 0.5 * float(q.qty) or rest > 1.5 * float(q.qty):
                continue
            match = o
            break
        if match is not None:
            remaining.remove(match)
        else:
            place.append(q)
    return [int(o["order_id"]) for o in remaining], place


def moved_bps(mids: collections.deque, now: float, window: float) -> float:
    if not mids:
        return 0.0
    latest = mids[-1][1]
    worst = 0.0
    for t, m in mids:
        if now - t <= window and m > 0:
            worst = max(worst, abs(latest - m) / m * 1e4)
    return worst


def markout_bps(side: str, fill_px: float, later_mid: float) -> float:
    if fill_px <= 0:
        return 0.0
    move = (later_mid - fill_px) / fill_px * 1e4
    return move if side == "long" else -move


def utc_day_start_ms(now: Optional[float] = None) -> int:
    d = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def params(cfg: dict) -> dict:
    return {
        "quote_bps": float(cfg.get("perps_quote_bps", 3.0)),
        "levels": int(cfg.get("perps_levels", 1)),
        "step_bps": float(cfg.get("perps_level_step_bps", 3.0)),
        "size_usd": float(cfg.get("perps_size_usd", 100.0)),
        "max_inv_usd": float(cfg.get("perps_max_inventory_usd", 300.0)),
        "skew_bps": float(cfg.get("perps_skew_bps", 4.0)),
    }


@dataclass
class MarketState:
    iid: int
    symbol: str
    mids: collections.deque = field(default_factory=lambda: collections.deque(maxlen=120))
    pulled_until: float = 0.0
    pull_reason: str = ""
    killed_day: int = 0
    killed_reason: str = ""
    leverage_set: bool = False
    markouts: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500))
    state: str = "idle"
    note: str = ""
    book: dict = field(default_factory=dict)
    book_at: float = 0.0
    top: dict = field(default_factory=dict)
    desired: list = field(default_factory=list)


_markets: dict[int, MarketState] = {}
_selected: list[int] = []
_selected_at = 0.0
_selected_key: tuple = ()
_pending_markouts: list[dict] = []
_last_fill_sync = 0.0
_last_slow = 0.0
_last_autocancel = 0.0
_autocancel_armed = False
_halt_reason = ""
_last_error = ""
_last_error_at = 0.0
_account: dict = {}
_stats: dict = {}
_rewards: list = []
_inst: dict[int, dict] = {}
_tick_by_iid: dict[int, dict] = {}
_positions: dict[int, dict] = {}
_our_orders: dict[int, list[dict]] = {}
_tick_lock = asyncio.Lock()


def _ms() -> int:
    return int(time.time() * 1000)


def _set_error(msg: str) -> None:
    global _last_error, _last_error_at
    _last_error = msg
    _last_error_at = time.time()
    logger.warning(f"perps: {msg}")


def _mstate(iid: int) -> MarketState:
    ms = _markets.get(iid)
    if ms is None:
        sym = str(_inst.get(iid, {}).get("symbol") or iid)
        ms = _markets[iid] = MarketState(iid=iid, symbol=sym)
    return ms


async def _cancel_ids(c: dict, ids: list[int]) -> None:
    if not ids:
        return
    try:
        res = await perps_api.cancel_orders(c, ids)
    except perps_api.PerpsAPIError as e:
        _set_error(f"cancel failed: {e}")
        return
    for r in res:
        if r.get("status") == "err" and r.get("error") not in (
                "order_not_in_orderbook", "order_already_terminal", "order_not_found",
                "order_not_pending_engine"):
            _set_error(f"cancel {r.get('oid')}: {r.get('error')}")


async def cancel_ours(c: dict, iids: Optional[set[int]] = None) -> int:
    try:
        orders = await perps_api.open_orders(c)
    except perps_api.PerpsAPIError as e:
        _set_error(f"open orders: {e}")
        return 0
    ids = [int(o["order_id"]) for o in orders
           if is_ours(o) and (iids is None or int(o.get("instrument_id", -1)) in iids)]
    await _cancel_ids(c, ids)
    return len(ids)


async def _sync_fills(c: dict) -> int:
    global _last_fill_sync
    _last_fill_sync = time.time()
    with db.get_db() as conn:
        last = db.perps_last_fill_ts(conn)
    start = last + 1 if last else _ms() - _FIRST_IMPORT_LOOKBACK_MS
    try:
        rows = await perps_api.fills(c, start)
    except perps_api.PerpsAPIError as e:
        _set_error(f"fills: {e}")
        return 0
    new = 0
    with db.get_db() as conn:
        for f in rows:
            try:
                iid = int(f["instrument_id"])
                px = float(f["price"])
                qty = float(f["quantity"])
                ms = _markets.get(iid)
                mid = ms.mids[-1][1] if ms and ms.mids else None
                row = {
                    "trade_id": int(f["trade_id"]), "order_id": f.get("order_id"),
                    "instrument_id": iid,
                    "symbol": _inst.get(iid, {}).get("symbol") or "",
                    "side": f.get("side") or "long", "price": px, "quantity": qty,
                    "notional": abs(px * qty), "taker": bool(f.get("taker")),
                    "fee": float(f.get("fee") or 0), "pnl": float(f.get("pnl") or 0),
                    "liquidation": bool(f.get("liquidation")), "mid_at_fill": mid,
                    "ts": int(f.get("timestamp") or 0),
                }
            except (KeyError, TypeError, ValueError):
                continue
            if db.insert_perps_fill(conn, row):
                new += 1
                if not row["taker"] and row["ts"] > _ms() - 5 * _MARKOUT_DELAY_MS:
                    _pending_markouts.append({
                        "trade_id": row["trade_id"], "iid": iid, "side": row["side"],
                        "px": px, "due": row["ts"] + _MARKOUT_DELAY_MS,
                    })
    return new


def _settle_markouts() -> None:
    now = _ms()
    keep = []
    with db.get_db() as conn:
        for pm in _pending_markouts:
            if pm["due"] > now:
                keep.append(pm)
                continue
            ms = _markets.get(pm["iid"])
            if not ms or not ms.mids or now - pm["due"] > 5 * _MARKOUT_DELAY_MS:
                continue
            mo = markout_bps(pm["side"], pm["px"], ms.mids[-1][1])
            db.set_perps_markout(conn, pm["trade_id"], mo)
            ms.markouts.append(mo)
    _pending_markouts[:] = keep


def _check_kill(ms: MarketState, cfg: dict, day: int) -> None:
    kill = float(cfg.get("perps_markout_kill_bps") or 0)
    n = int(cfg.get("perps_markout_min_fills") or 20)
    if kill <= 0 or ms.killed_day == day or len(ms.markouts) < n:
        return
    recent = list(ms.markouts)[-n:]
    avg = sum(recent) / len(recent)
    if avg < -kill:
        ms.killed_day = day
        ms.killed_reason = (f"last {n} maker fills averaged {avg:.1f} bps a minute later "
                            f"(limit −{kill:g}) — off for the rest of the UTC day")
        ms.markouts.clear()
        logger.warning(f"perps {ms.symbol}: markout kill — {ms.killed_reason}")


def _unrealized() -> float:
    return sum(float(p.get("unrealized_pnl") or 0) for p in _positions.values())


def breakers(cfg: dict) -> tuple[str, dict]:
    day = utc_day_start_ms()
    with db.get_db() as conn:
        today = db.perps_fill_totals(conn, day)
        life = db.perps_fill_totals(conn, 0)
    upnl = _unrealized()
    today_pnl = today["realizedPnlUsd"] - today["feesUsd"] + upnl
    life_pnl = life["realizedPnlUsd"] - life["feesUsd"] + upnl
    today.update(netPnlUsd=today_pnl, unrealizedPnlUsd=upnl)
    lim = float(cfg.get("perps_daily_loss_limit") or 0)
    if lim < 0 and today_pnl <= lim:
        return (f"daily loss limit: today is {today_pnl:+.2f} (limit {lim:.2f}). "
                "Quoting resumes at 00:00 UTC, or raise the limit."), today
    life_lim = float(cfg.get("perps_lifetime_loss_limit_usd") or 0)
    if life_lim > 0 and life_pnl <= -life_lim:
        return (f"lifetime loss limit: {life_pnl:+.2f} across all Perps fills "
                f"(limit −{life_lim:.2f}). Raise or clear it to resume."), today
    target = float(cfg.get("perps_daily_volume_target_usd") or 0)
    if target > 0 and today["volumeUsd"] >= target:
        return (f"daily volume target reached (${today['volumeUsd']:,.0f} ≥ "
                f"${target:,.0f}) — quoting resumes at 00:00 UTC."), today
    return "", today


async def run_tick(cfg: dict) -> bool:
    async with _tick_lock:
        return await _run_tick(cfg)


async def _run_tick(cfg: dict) -> bool:
    global _halt_reason, _last_slow, _account, _stats, _rewards
    c = perps_api.creds()
    enabled = bool(cfg.get("perps_enabled"))
    if not c:
        _halt_reason = ""
        return False

    try:
        inst_rows, ticks, pf, orders = await asyncio.gather(
            perps_api.instruments(), perps_api.tickers(),
            perps_api.portfolio(c), perps_api.open_orders(c))
    except perps_api.PerpsAPIError as e:
        _set_error(str(e))
        return False
    _inst.clear()
    _inst.update({int(i["instrument_id"]): i for i in inst_rows})
    _tick_by_iid.clear()
    _tick_by_iid.update({int(t["instrument_id"]): t for t in ticks if "instrument_id" in t})
    _account = pf
    _positions.clear()
    for p in pf.get("positions") or []:
        try:
            if float(p.get("size") or 0) != 0:
                _positions[int(p["instrument_id"])] = p
        except (TypeError, ValueError, KeyError):
            continue
    _our_orders.clear()
    for o in orders:
        if is_ours(o):
            _our_orders.setdefault(int(o["instrument_id"]), []).append(o)

    now = time.time()
    for iid, t in _tick_by_iid.items():
        if iid in _markets and iid not in _selected:
            try:
                _markets[iid].mids.append((now, float(t.get("mid_price") or 0)))
            except (TypeError, ValueError):
                pass

    if now - _last_fill_sync >= _FILL_SYNC_SEC:
        await _sync_fills(c)
    _settle_markouts()
    if now - _last_slow >= _ACCOUNT_SLOW_SEC:
        _last_slow = now
        st, rw = await perps_api.gather_quiet(perps_api.stats(c), perps_api.rewards(c))
        _stats = st or _stats
        _rewards = rw if rw is not None else _rewards

    record_tape()
    halt, _today = breakers(cfg)
    _halt_reason = halt
    if not enabled or halt:
        perps_ws.feed.set_instruments([])
        changed = False
        if _our_orders:
            await _cancel_ids(c, [int(o["order_id"]) for os_ in _our_orders.values() for o in os_])
            changed = True
        await _disarm_autocancel(c)
        for ms in _markets.values():
            ms.state, ms.note = ("halted", halt) if halt else ("off", "")
        if not halt:
            await preview(cfg)
        return changed
    await _select(cfg)
    perps_ws.feed.set_instruments(_selected if cfg.get("perps_realtime", True) else [])
    return await _quote(cfg, c)


async def run_fast(cfg: dict, iids) -> bool:
    async with _tick_lock:
        c = perps_api.creds()
        if not c or not cfg.get("perps_enabled") or _halt_reason:
            return False
        only = {int(i) for i in iids} & set(_selected)
        if not only:
            return False
        return await _quote(cfg, c, only=only, fast=True)


def record_tape() -> int:
    trades, samples = perps_ws.feed.drain()
    if not trades and not samples:
        return 0
    try:
        with db.get_db() as conn:
            return db.insert_perps_trades(conn, trades) + db.insert_perps_ticks(conn, samples)
    except Exception as e:
        logger.debug(f"perps tape record: {e}")
        return 0


async def _select(cfg: dict) -> None:
    global _selected, _selected_at, _selected_key
    key = (cfg.get("perps_select_mode"), tuple(cfg.get("perps_markets") or ()),
           int(cfg.get("perps_market_count") or 4), tuple(cfg.get("perps_categories") or ()),
           float(cfg.get("perps_size_usd") or 0), float(cfg.get("perps_quote_bps") or 0))
    if key == _selected_key and _selected and time.time() - _selected_at < _RESELECT_SEC:
        return
    try:
        board = await perps_board.snapshot(cfg, priority=tuple(_selected))
    except Exception as e:
        _set_error(f"market board: {e}")
        return
    n = int(cfg.get("perps_market_count") or 4)
    if cfg.get("perps_select_mode") == "manual":
        sel = [int(r["instrumentId"]) for r in perps_board.pick_markets(board, cfg)]
    else:
        ranked = [int(r["instrumentId"]) for r in
                  perps_board.pick_markets(board, {**cfg, "perps_market_count": n + 2})]
        sel = [i for i in _selected if i in ranked][:n]
        sel += [i for i in ranked if i not in sel][: max(0, n - len(sel))]
    for iid in _positions:
        if iid not in sel and iid in _markets and _markets[iid].state in ("quoting", "pulled"):
            sel.append(iid)
    _selected = sel
    _selected_at = time.time()
    _selected_key = key


async def _quote(cfg: dict, c: dict, *, only: Optional[set] = None, fast: bool = False) -> bool:
    p = params(cfg)
    day = utc_day_start_ms()
    now = time.time()
    sel = list(_selected)
    targets = sel if only is None else [i for i in sel if i in only]
    realtime = bool(cfg.get("perps_realtime", True))
    refresh = float(cfg.get("perps_book_refresh_sec") or 10)
    need_book = [] if fast else [
        i for i in targets
        if not realtime or perps_ws.feed.bbo(i) is None
        or now - _mstate(i).book_at >= refresh
    ]
    fetched = await perps_api.gather_quiet(*(perps_api.book(i, depth=100) for i in need_book))
    books = dict(zip(need_book, fetched))

    cancels: list[int] = []
    creates: list[list] = []
    if only is None:
        for iid, os_ in _our_orders.items():
            if iid not in sel:
                cancels += [int(o["order_id"]) for o in os_]
        for iid, ms in _markets.items():
            if iid not in sel:
                ms.state, ms.note, ms.desired = "idle", "", []

    for iid in targets:
        ms = _mstate(iid)
        inst = _inst.get(iid) or {}
        live = _our_orders.get(iid, [])
        bk = books.get(iid)
        if bk is not None:
            own = [(bool(o["buy"]), float(o["price"]), float(o.get("resting_quantity") or 0))
                   for o in live]
            ms.book = perps_board.note_book(iid, bk.get("bids") or [], bk.get("asks") or [], own)
            ms.book_at = now
        q = perps_ws.feed.bbo(iid) if realtime else None
        if q is not None:
            best_bid, best_ask, mid, src = q["bid"], q["ask"], q["mid"], "live"
        elif bk is not None and ms.book.get("mid"):
            best_bid, best_ask, mid, src = (ms.book.get("bestBid"), ms.book.get("bestAsk"),
                                            ms.book.get("mid"), "rest")
        else:
            if not fast:
                ms.state, ms.note = "error", "book unavailable"
            continue
        ms.top = {"bestBid": best_bid, "bestAsk": best_ask, "mid": mid, "source": src}
        ms.mids.append((now, mid))
        _check_kill(ms, cfg, day)
        if ms.killed_day == day:
            ms.state, ms.note, ms.desired = "killed", ms.killed_reason, []
            cancels += [int(o["order_id"]) for o in live]
            continue

        if not ms.leverage_set and not fast:
            await _set_leverage(c, cfg, iid, inst)
            ms.leverage_set = True

        moved = moved_bps(ms.mids, now, float(cfg.get("perps_vol_window_sec") or 10))
        pull_at = float(cfg.get("perps_vol_pull_bps") or 0)
        if pull_at > 0 and moved > pull_at:
            ms.pulled_until = now + float(cfg.get("perps_pull_cooldown_sec") or 20)
            ms.pull_reason = f"mid moved {moved:.1f} bps in {cfg.get('perps_vol_window_sec')}s"
        index = _f(_tick_by_iid.get(iid, {}).get("index_price"))
        dev_lim = float(cfg.get("perps_max_index_dev_bps") or 0)
        if dev_lim > 0 and mid and index:
            dev = (mid - index) / index * 1e4
            if abs(dev) > dev_lim:
                ms.pulled_until = max(ms.pulled_until, now + 5)
                ms.pull_reason = f"book mid {dev:+.1f} bps off the index"
        if now < ms.pulled_until:
            ms.state, ms.note, ms.desired = "pulled", ms.pull_reason, []
            cancels += [int(o["order_id"]) for o in live]
            continue

        pos = float(_positions.get(iid, {}).get("size") or 0)
        desired, note = plan_quotes(best_bid=best_bid, best_ask=best_ask,
                                    mid=mid, position_qty=pos, inst=inst, p=p)
        ms.desired = desired
        ms.state = "quoting" if desired else "blocked"
        ms.note = note
        drop, place = diff_orders(live, desired, float(cfg.get("perps_requote_bps") or 1.5))
        cancels += drop
        for qt in place:
            creates.append(perps_api.order_row(iid, qt.buy, qt.price, qt.qty,
                                               tif="gtc", post_only=True, coid=new_coid()))

    changed = False
    if cancels:
        await _cancel_ids(c, cancels)
        gone = set(cancels)
        for iid in list(_our_orders):
            _our_orders[iid] = [o for o in _our_orders[iid] if int(o["order_id"]) not in gone]
        changed = True
    if creates:
        try:
            acks = await perps_api.create_orders(c, creates)
        except perps_api.PerpsAPIError as e:
            _set_error(f"place: {e}")
            acks = []
        for row, ack in zip(creates, acks):
            if ack.get("status") == "ok" and ack.get("oid") is not None:
                _our_orders.setdefault(row[0], []).append({
                    "order_id": int(ack["oid"]), "instrument_id": row[0], "buy": row[1],
                    "price": row[2], "resting_quantity": row[3], "quantity": row[3],
                    "client_order_id": row[7],
                })
        errs = [a.get("error") for a in acks if a.get("status") == "err"]
        real = [e for e in errs if e and "post" not in str(e)]
        if real:
            _set_error(f"orders rejected: {', '.join(sorted(set(map(str, real))))}")
        changed = True
    if sel and not fast:
        await _arm_autocancel(c, cfg)
    return changed


async def _set_leverage(c: dict, cfg: dict, iid: int, inst: dict) -> None:
    lev = max(1, min(int(cfg.get("perps_leverage") or 3), int(inst.get("max_leverage") or 1)))
    cross = bool(cfg.get("perps_cross_margin")) and not inst.get("isolated_only")
    try:
        await perps_api.update_leverage(c, iid, lev, cross)
    except perps_api.PerpsAPIError as e:
        logger.info(f"perps leverage {inst.get('symbol')}: {e}")


async def _arm_autocancel(c: dict, cfg: dict) -> None:
    global _last_autocancel, _autocancel_armed
    if time.time() - _last_autocancel < _AUTOCANCEL_EVERY_SEC:
        return
    _last_autocancel = time.time()
    lead = max(45.0, 10.0 * float(cfg.get("perps_poll_sec") or 2))
    try:
        await perps_api.auto_cancel(c, _ms() + int(lead * 1000))
        _autocancel_armed = True
    except perps_api.PerpsAPIError as e:
        _set_error(f"auto-cancel: {e}")


async def _disarm_autocancel(c: dict) -> None:
    global _autocancel_armed, _last_autocancel
    if not _autocancel_armed:
        return
    try:
        await perps_api.auto_cancel(c, 0)
        _autocancel_armed = False
        _last_autocancel = 0.0
    except perps_api.PerpsAPIError as e:
        _set_error(f"auto-cancel disarm: {e}")


async def preview(cfg: dict) -> None:
    global _selected
    try:
        board = await perps_board.snapshot(cfg)
    except Exception:
        return
    picks = perps_board.pick_markets(board, cfg)
    _selected = [int(r["instrumentId"]) for r in picks]
    p = params(cfg)
    for r in picks:
        ms = _mstate(int(r["instrumentId"]))
        ms.symbol = str(r.get("symbol") or ms.symbol)
        bk = perps_board._books.get(ms.iid) or {}
        mid = r.get("midPrice") or bk.get("mid")
        bb, ba = bk.get("bestBid"), bk.get("bestAsk")
        if not (mid and bb and ba and bb <= mid <= ba):
            bb, ba = (mid * (1 - 1e-6), mid * (1 + 1e-6)) if mid else (None, None)
        pos = float(_positions.get(ms.iid, {}).get("size") or 0)
        ms.desired, note = plan_quotes(best_bid=bb, best_ask=ba,
                                       mid=mid, position_qty=pos, inst=r, p=p)
        ms.book = {**bk, "mid": mid}
        ms.state, ms.note = "off", note


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def flatten_all() -> dict:
    c = perps_api.creds()
    if not c:
        return {"ok": False, "message": "Perps account not linked."}
    cancelled = await cancel_ours(c)
    try:
        pf = await perps_api.portfolio(c)
        inst = {int(i["instrument_id"]): i for i in await perps_api.instruments()}
    except perps_api.PerpsAPIError as e:
        return {"ok": False, "message": str(e)}
    rows = []
    for p in pf.get("positions") or []:
        try:
            iid = int(p["instrument_id"])
            size = float(p.get("size") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if size == 0:
            continue
        i = inst.get(iid) or {}
        try:
            bk = await perps_api.book(iid, depth=10)
        except perps_api.PerpsAPIError:
            continue
        buy = size < 0
        side = bk.get("asks") if buy else bk.get("bids")
        if not side:
            continue
        touch = float(side[0][0])
        px = touch * (1 + _FLATTEN_SLIP) if buy else touch * (1 - _FLATTEN_SLIP)
        pdec = int(i.get("price_decimals") or 0)
        qdec = int(i.get("quantity_decimals") or 0)
        rows.append(perps_api.order_row(
            iid, buy, perps_api.fmt_price(px, pdec, side="sell" if buy else "buy"),
            perps_api.fmt_qty(abs(size), qdec), tif="ioc", post_only=False,
            reduce_only=True, coid=new_coid()))
    if not rows:
        return {"ok": True, "message": f"Cancelled {cancelled} quote(s); no open positions."}
    try:
        acks = await perps_api.create_orders(c, rows)
    except perps_api.PerpsAPIError as e:
        return {"ok": False, "message": f"Flatten failed: {e}"}
    bad = [a.get("error") for a in acks if a.get("status") == "err"]
    msg = f"Cancelled {cancelled} quote(s); sent {len(rows) - len(bad)} close order(s)."
    if bad:
        msg += f" Rejected: {', '.join(map(str, bad))}."
    return {"ok": not bad, "message": msg}


async def shutdown() -> None:
    c = perps_api.creds()
    if not c:
        return
    try:
        await asyncio.wait_for(cancel_ours(c), timeout=5)
        await asyncio.wait_for(_disarm_autocancel(c), timeout=3)
    except Exception:
        pass


def _q(q: Quote) -> dict:
    return {"buy": q.buy, "price": q.px, "qty": float(q.qty), "level": q.level}


def status(cfg: dict) -> dict:
    link = perps_api.creds_status()
    halt, today = ("", {})
    try:
        halt, today = breakers(cfg)
    except Exception as e:
        logger.debug(f"perps status breakers: {e}")
    day = utc_day_start_ms()
    with db.get_db() as conn:
        by_market = db.perps_market_totals(conn, day)
        recent = db.recent_perps_fills(conn, 30)
        week = db.perps_fill_totals(conn, _ms() - 7 * 86_400_000)
    markets = []
    iids = list(dict.fromkeys(list(_selected) + list(_positions)))
    for iid in iids:
        ms = _markets.get(iid) or MarketState(iid=iid, symbol=str(_inst.get(iid, {}).get("symbol") or iid))
        pos = _positions.get(iid) or {}
        live = _our_orders.get(iid, [])
        mt = by_market.get(iid, {})
        mo = list(ms.markouts)
        markets.append({
            "instrumentId": iid,
            "symbol": ms.symbol,
            "selected": iid in _selected,
            "state": ms.state,
            "note": ms.note,
            "bestBid": ms.top.get("bestBid", ms.book.get("bestBid")),
            "bestAsk": ms.top.get("bestAsk", ms.book.get("bestAsk")),
            "mid": ms.mids[-1][1] if ms.mids else ms.book.get("mid"),
            "priceSource": ms.top.get("source"),
            "competingScore": ms.book.get("comp"),
            "desired": [_q(q) for q in ms.desired],
            "resting": [{"buy": bool(o["buy"]), "price": float(o["price"]),
                         "qty": float(o.get("resting_quantity") or 0)} for o in live],
            "positionQty": float(pos.get("size") or 0),
            "positionEntry": _f(pos.get("entry_price")),
            "positionValueUsd": _f(pos.get("position_value")),
            "unrealizedPnlUsd": _f(pos.get("unrealized_pnl")),
            "liquidationPrice": _f(pos.get("liquidation_price")),
            "todayFills": mt.get("fills", 0),
            "todayVolumeUsd": mt.get("volumeUsd", 0.0),
            "todayPnlUsd": mt.get("realizedPnlUsd", 0.0) - mt.get("feesUsd", 0.0),
            "avgMarkoutBps": (sum(mo[-50:]) / len(mo[-50:])) if mo else None,
            "markoutN": len(mo),
        })
    margin = _account.get("margin") or {}
    st = _stats or {}
    return {
        "enabled": bool(cfg.get("perps_enabled")),
        "link": link,
        "trading": bool(cfg.get("perps_enabled")) and link["linked"] and not halt,
        "haltReason": halt,
        "lastError": _last_error if time.time() - _last_error_at < 300 else "",
        "account": {
            "equityUsd": _f(margin.get("total_account_value")),
            "availableMarginUsd": _f(margin.get("available_order_margin")),
            "withdrawableUsd": _f(_account.get("withdrawable")),
            "positionValueUsd": _f(margin.get("total_position_value")),
            "inLiquidation": bool(_account.get("in_liquidation")),
            "feeTier": _account.get("fee_tier"),
        } if _account else None,
        "stats7d": {
            "volumeUsd": _f(st.get("volume_7d")),
            "makerVolumeUsd": _f(st.get("maker_volume_7d")),
            "makerShare": _f(st.get("account_maker_share_7d")),
            "entityMakerShare": _f(st.get("entity_maker_share_7d")),
        } if st else None,
        "localWeek": week,
        "rewards": _rewards[-14:] if isinstance(_rewards, list) else [],
        "today": today,
        "markets": markets,
        "recentFills": recent,
        "feed": perps_ws.feed.status(),
    }


def reset_state() -> None:
    global _selected, _selected_at, _selected_key, _last_fill_sync, _last_slow
    global _last_autocancel, _autocancel_armed, _halt_reason, _last_error, _last_error_at
    global _account, _stats, _rewards
    _markets.clear()
    _selected = []
    _selected_at = 0.0
    _selected_key = ()
    _pending_markouts.clear()
    _last_fill_sync = _last_slow = _last_autocancel = 0.0
    _autocancel_armed = False
    _halt_reason = _last_error = ""
    _last_error_at = 0.0
    _account, _stats, _rewards = {}, {}, []
    _inst.clear()
    _tick_by_iid.clear()
    _positions.clear()
    _our_orders.clear()
