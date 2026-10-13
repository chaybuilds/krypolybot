"""Copy-trading engine (Phase 3).

Mirror one or more Polymarket wallets. Each tick:

  1. Pull the CURRENT holdings of every followed wallet (Data API
     /positions) and union them into a set of (ticker, side) they're in,
     filtered by `copy_min_trade_usd` (ignore their dust).
  2. ENTER any (ticker, side) they hold that we don't have an open copy of
     (sized by our own rules, inside the
     `copy_entry_min_cents`..`copy_entry_max_cents` band).
  3. EXIT any open copy of ours whose (ticker, side) NONE of the followed
     wallets hold anymore — i.e. they all closed it, so we follow them out.

This is deliberately STATELESS: "enter if they hold and we don't / exit if
we hold and nobody does" needs no persisted last-seen snapshot, so a restart
mid-session can't desync. Copies are ordinary `bot_positions` rows tagged
`signal_source='copy'`, so the existing poll/reconcile/settlement/live-P&L
machinery manages them for free — this module only opens and closes.

Fully INDEPENDENT of the main bot and crypto engines (its own enable + live
switches), exactly like crypto15m_trader.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

import db
import polymarket_api
import polymarket_auth
import trader

logger = logging.getLogger("krypt.copy")

_last_halt_log_t = 0.0

_copy_cooldown: dict = {}
_COPY_COOLDOWN_SEC = 300
_COPY_MIN_CONTRACTS = 5

_exit_retry_at: dict = {}
_EXIT_RETRY_SEC = 20

_wallet_pos_cache: dict[str, tuple[float, list]] = {}
_WALLET_CACHE_STALE_SEC = 90.0
_WALLET_READ_CONCURRENCY = 8
_WALLET_CACHE_UI_SEC = 10.0

_wallet_too_large: dict[str, float] = {}
_TOO_LARGE_RECHECK_SEC = 600.0

last_open_count = 0


_pushed: dict[tuple[str, str], dict] = {}
_PUSHED_MAX = 200
_pushed_ids: dict[str, float] = {}
_PUSHED_ID_TTL = 900.0
_PUSHED_STALE_SEC = 120.0
_push_ctx: dict = {"on": False, "wallets": set(), "min_usd": 0.0}
_pushed_changed: list[dict] = []


def _own_addresses() -> set:
    """Our own wallet addresses (EOA + funder) — never copy ourselves, which in
    LIVE would just double our own exposure."""
    out = set()
    for fn in (polymarket_auth.trading_address, polymarket_auth.get_address,
               polymarket_auth.get_funder):
        try:
            a = (fn() or "").strip().lower()
            if a:
                out.add(a)
        except Exception:
            pass
    return out


def _short_addr(a: str) -> str:
    a = a or ""
    return f"{a[:6]}…{a[-4:]}" if len(a) >= 12 else a


async def _bankroll_usd(cfg: dict, authed: bool) -> float:
    """Balance to size against — live cash when connected. A CONFIRMED zero
    is a real 0.0 (skip entries; don't spend the pinned bankroll that isn't
    there); only an unknown balance (read failed) falls back to
    `start_bankroll_usd`."""
    if authed:
        try:
            cents, _port = await trader.refresh_balance(cfg, force=False)
            if cents > 0:
                return cents / 100.0
            if trader.last_balance_read_ok():
                return 0.0
        except Exception:
            pass
    return max(0.0, float(cfg.get("start_bankroll_usd", 0.0) or 0.0))


def _today_copy_pnl(env: str) -> float:
    """Realized copy-trade P&L today (UTC). Robust to a mid-day history wipe:
    folds in any today-P&L banked when clear_trade_history/factory_reset removed
    today's resolved copy rows, so wiping stats can't re-arm trading past the
    daily loss limit."""
    with db.get_db() as conn:
        return db.engine_today_pnl(conn, "copy", env)


def _lifetime_loss_tripped(cfg: dict, env: str) -> tuple[bool, str]:
    """Durable cumulative realized-loss guard for the copy engine (mirrors
    crypto15m's). The daily breaker resets every UTC midnight, so a net-negative
    copy engine could bleed the daily cap forever; this caps TOTAL drawdown using
    a counter that SURVIVES a stats wipe. Trips once cumulative realized copy loss
    reaches the absolute-$ override (if > 0) or the configured fraction of the
    starting bankroll. Returns (tripped, human reason)."""
    limit_usd = float(cfg.get("copy_lifetime_loss_limit_usd") or 0.0)
    try:
        limit_pct = max(0.0, min(1.0, float(cfg.get("copy_lifetime_loss_limit_pct", 0.0) or 0.0)))
    except (TypeError, ValueError):
        limit_pct = 0.0
    if limit_usd <= 0 and limit_pct <= 0:
        return False, ""
    with db.get_db() as conn:
        lifetime = db.lifetime_realized_pnl(conn, "copy", env)
        if limit_usd > 0:
            cap = limit_usd
        else:
            bankroll = db.effective_start_bankroll(
                conn, env, cfg.get("start_bankroll_usd", 0.0))
            if bankroll <= 0:
                return False, ""
            cap = bankroll * limit_pct
    loss = -lifetime
    if cap > 0 and loss >= cap:
        return True, (
            f"lifetime loss limit reached — engine paused; adjust settings to "
            f"resume (cumulative ${lifetime:+.2f}, loss ${loss:.2f} >= "
            f"cap ${cap:.2f})"
        )
    return False, ""


def _our_open_copies(env: str) -> list[dict]:
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM bot_positions
               WHERE signal_source='copy' AND network=? AND resolved=0
                 AND status IN ('submitted','partial','filled')""",
            (env,),
        ).fetchall()
    return [dict(r) for r in rows]


def _seen_key(env: str) -> str:
    return f"copy_seen:{env}"


_SEEN_MAX = 5000


def _load_seen(env: str) -> dict:
    """Baseline keys in stored (insertion) order — a dict used as an ordered
    set (key -> None), so the cap can evict oldest-first."""
    try:
        with db.get_db() as conn:
            raw = db.kv_get(conn, _seen_key(env))
        if raw:
            return dict.fromkeys(json.loads(raw))
    except Exception:
        pass
    return {}


def _save_seen(env: str, seen: dict) -> None:
    try:
        with db.get_db() as conn:
            db.kv_set(conn, _seen_key(env), json.dumps(list(seen)))
    except Exception:
        pass


def _qty_base_key(env: str) -> str:
    return f"copy_qty_base:{env}"


def _load_qty_base(env: str) -> dict:
    try:
        with db.get_db() as conn:
            raw = db.kv_get(conn, _qty_base_key(env))
        if raw:
            m = json.loads(raw)
            if isinstance(m, dict):
                return {str(k): float(v) for k, v in m.items()}
    except Exception:
        pass
    return {}


def _save_qty_base(env: str, m: dict) -> None:
    try:
        with db.get_db() as conn:
            db.kv_set(conn, _qty_base_key(env), json.dumps(m))
    except Exception:
        pass


def _filter_new_entries(followed: dict, env: str, cfg: dict) -> dict:
    """Return only the followed holdings that are a NEW entry for an
    already-baselined wallet; baseline everything else. Persists the seen-set
    only when it actually changed (it used to rewrite the kv row every tick)."""
    seen = _load_seen(env)
    changed = False
    cur_wallets = {w.strip().lower() for w in (cfg.get("copy_wallets") or [])
                   if isinstance(w, str)}
    stale = [k for k in seen if k.split("|", 1)[0].strip().lower() not in cur_wallets]
    for k in stale:
        del seen[k]
        changed = True
    seen_wallets = {k.split("|", 1)[0] for k in seen}
    eligible: dict = {}
    for key, h in followed.items():
        ticker, side = h["ticker"], h["side"]
        is_new = False
        for w in h.get("wallets", []):
            hk = f"{w}|{ticker}|{side}"
            if w in seen_wallets and hk not in seen:
                is_new = True
            elif hk not in seen:
                seen[hk] = None
                changed = True
        if is_new:
            eligible[key] = h
    if len(seen) > _SEEN_MAX:
        for k in list(seen)[: len(seen) - _SEEN_MAX]:
            del seen[k]
        changed = True
    if changed:
        _save_seen(env, seen)
    return eligible


def _compute_copy_contracts(
    cfg: dict, *, price_cents: int, balance_usd: float, cap_usd: Optional[float] = None
) -> int:
    """Contracts to buy for one copy, given the entry price. `fixed` spends
    `copy_fixed_usd`; `balance_pct` spends a fraction of balance. Either way
    clamped to what the balance can actually collateralize (a buy-only binary
    locks contracts × price). `cap_usd` further caps spend to the shared-wallet
    risk room (cash reserve / total-exposure cap) computed by the caller. Returns
    0 when even one contract is unaffordable so the caller skips instead of
    eating a 'not enough balance' rejection."""
    price = max(0.01, int(price_cents) / 100.0)
    bal = max(0.0, float(balance_usd or 0.0))
    mode = (cfg.get("copy_sizing_mode") or "fixed").lower()
    if mode == "balance_pct" and bal > 0:
        pct = max(0.0, min(1.0, float(cfg.get("copy_balance_pct", 0.02) or 0.0)))
        budget = bal * pct
    else:
        budget = float(cfg.get("copy_fixed_usd", 10.0) or 0.0)
    if bal > 0:
        budget = min(budget, bal * 0.98)
    if cap_usd is not None:
        budget = min(budget, max(0.0, cap_usd))
    return max(0, int(budget // price))


async def _read_wallet_positions(w: str) -> tuple[Optional[list], bool]:
    """One followed wallet's /positions read plus its last-good-cache fallback.

    Returns (positions, read_ok). `positions is None` (with read_ok False) means
    we have never had a good read for this wallet this session, so exits can't
    be decided safely — same meaning the old `all_ok = False; continue` branch
    had.

    Split out of `_followed_holdings` purely so the per-wallet reads can be
    gathered: the loop used to be N serial Data-API round-trips before ANY exit
    or entry decision, on every 5 s tick. Every fallback semantic below is
    unchanged."""
    wl = w.strip().lower()
    flagged = _wallet_too_large.get(wl)
    if flagged is not None and time.monotonic() - flagged < _TOO_LARGE_RECHECK_SEC:
        return _too_large_fallback(wl)
    try:
        positions = await polymarket_api.get_positions(limit=500, user=w)
        _wallet_pos_cache[wl] = (time.time(), positions)
        _wallet_too_large.pop(wl, None)
        return positions, True
    except polymarket_api.PositionsTooLargeError as e:
        _wallet_too_large[wl] = time.monotonic()
        logger.warning(
            f"[copy] {_short_addr(w)} is too large to mirror ({e}); not copying "
            f"its trades, re-checking in {_TOO_LARGE_RECHECK_SEC / 60:.0f} min"
        )
        return _too_large_fallback(wl)
    except Exception as e:
        cached = _wallet_pos_cache.get(wl)
        age = (time.time() - cached[0]) if cached else None
        if cached and age <= _WALLET_CACHE_STALE_SEC:
            logger.debug(
                f"[copy] fetch positions for {_short_addr(w)} failed ({e}); "
                f"using {age:.0f}s-old cached read"
            )
            return cached[1], True
        if cached:
            logger.warning(
                f"[copy] {_short_addr(w)} read failing for {age:.0f}s; using "
                "last-good holdings so exits keep running (a real sale by this "
                "wallet will be mirrored once its read recovers)"
            )
            return cached[1], True
        logger.warning(f"[copy] fetch positions for {_short_addr(w)} failed: {e}")
        return None, False


def _too_large_fallback(wl: str) -> tuple[Optional[list], bool]:
    """A too-large wallet is read like any wallet whose read keeps failing:
    its last complete read if this session ever had one, else unknown, which
    holds the exit pass for everyone. A partial list is never an option."""
    cached = _wallet_pos_cache.get(wl)
    return (cached[1], True) if cached else (None, False)


def _too_large_message(wl: str) -> Optional[str]:
    """What the Copy page shows for a wallet flagged too large, else None."""
    if wl not in _wallet_too_large:
        return None
    msg = ("Too large to mirror: this wallet holds more open positions than the "
           "app can read in full, so its trades aren't copied. Stop following it.")
    if wl not in _wallet_pos_cache:
        msg += (" Until you do, copy exits are paused for every wallet: the app "
                "never saw this one's full holdings, so it can't tell what it sold.")
    return msg


async def _followed_holdings(cfg: dict) -> tuple[dict, bool]:
    """Union of all followed wallets' current holdings, keyed by
    (ticker, side). Value carries the live price, max cost across wallets,
    title, and which wallets hold it. Dust below `copy_min_trade_usd` is
    dropped so we don't mirror their rounding noise.

    Returns (holdings, all_reads_ok). A wallet whose /positions read FAILED
    is absent from the map — which downstream is indistinguishable from that
    wallet having sold everything. Exits must therefore only run when every
    read succeeded; entries can safely use the partial map (worst case a
    missed entry, retried next tick)."""
    own = _own_addresses()
    wallets = [w for w in (cfg.get("copy_wallets") or [])
               if isinstance(w, str) and w.strip().lower() not in own]
    min_cost = float(cfg.get("copy_min_trade_usd") or 0.0)
    out: dict[tuple[str, str], dict] = {}
    all_ok = True

    sem = asyncio.Semaphore(_WALLET_READ_CONCURRENCY)

    async def _bounded(addr: str) -> tuple[Optional[list], bool]:
        async with sem:
            return await _read_wallet_positions(addr)

    reads = await asyncio.gather(*[_bounded(w) for w in wallets])

    for w, (positions, read_ok) in zip(wallets, reads):
        if not read_ok or positions is None:
            all_ok = False
            continue
        for p in positions:
            try:
                qty = float(p.get("position_fp") or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty == 0:
                continue
            ticker = p.get("ticker") or ""
            if not ticker:
                continue
            side = "yes" if qty > 0 else "no"
            cost = float(p.get("market_exposure_dollars") or 0.0)
            if cost < min_cost:
                continue
            cur = p.get("cur_price")
            try:
                price_cents = int(round(float(cur) * 100)) if cur else None
            except (TypeError, ValueError):
                price_cents = None
            key = (ticker, side)
            entry = out.get(key)
            if entry is None:
                out[key] = {
                    "ticker": ticker, "side": side,
                    "price_cents": price_cents,
                    "cost": cost,
                    "qty": abs(qty),
                    "title": p.get("title") or ticker,
                    "event_ticker": p.get("event_ticker") or "",
                    "wallets": [w],
                }
            else:
                entry["wallets"].append(w)
                entry["cost"] = max(entry["cost"], cost)
                entry["qty"] = float(entry.get("qty") or 0.0) + abs(qty)
                if entry.get("price_cents") is None and price_cents is not None:
                    entry["price_cents"] = price_cents
    return out, all_ok


_category_cache: dict[str, str] = {}
_CATEGORY_CACHE_MAX = 500


async def _market_category(ticker: str) -> str:
    """The followed market's Polymarket category, or "" when it can't be read.

    This is a MONEY field, not a label: the fee-v2 taker rate is per category
    (crypto 0.07, sports/economics/culture/weather/other 0.05, finance /
    politics / tech / mentions 0.04, geopolitics 0.0) and copy rows used to be
    written with a hard-coded "" — which missed the table and billed every
    copied position, sports and politics included, at the CRYPTO rate. That
    fee lands in pnl_usd and in the daily/lifetime loss breakers.

    Local `markets` row first (free, and the scanner keeps it fresh), Gamma
    only on a miss, and the answer is cached per ticker either way."""
    if not ticker:
        return ""
    cached = _category_cache.get(ticker)
    if cached is not None:
        return cached
    cat = ""
    try:
        with db.get_db() as conn:
            row = db.get_market(conn, ticker)
        cat = str((row or {}).get("category") or "").strip()
    except Exception as e:
        logger.debug(f"[copy] local category lookup {ticker}: {e}")
    if not cat:
        try:
            m = await polymarket_api.fetch_market(ticker)
            cat = str((m or {}).get("category") or "").strip()
        except Exception as e:
            logger.debug(f"[copy] category fetch {ticker}: {e}")
    if len(_category_cache) >= _CATEGORY_CACHE_MAX:
        _category_cache.clear()
    _category_cache[ticker] = cat
    return cat


async def _enter_copy(
    h: dict, cfg: dict, env: str, balance_usd: float, available_usd: Optional[float] = None
) -> Optional[dict]:
    """Open one copy: a taker buy crossing to the current ask (cap-bounded).
    Records a `bot_positions` row; the poll/reconcile loop then syncs the
    fill, mark price, and eventual settlement just like any other position.
    Always places a REAL order (the caller only runs when the wallet is
    connected — there is no paper mode).

    Serialised against the main / crypto15m / script engines by
    `trader.order_gate`. Copy computes `available_usd` ONCE per tick (a
    wallet-wide exposure + reserve snapshot), so the gate also re-reads that
    headroom from the DB immediately before placing — without it, copy and
    crypto ticking on their own tasks could both size against the same
    headroom and both place."""
    async with trader.order_gate("copy"):
        return await _enter_copy_locked(h, cfg, env, balance_usd, available_usd)


async def _enter_copy_locked(
    h: dict, cfg: dict, env: str, balance_usd: float, available_usd: Optional[float] = None
) -> Optional[dict]:
    ticker, side = h["ticker"], h["side"]
    max_cents = int(cfg.get("copy_entry_max_cents", 95) or 95)
    min_cents = int(cfg.get("copy_entry_min_cents", 0) or 0)

    ask = None
    try:
        q = trader.ws_quote_cents(ticker, side) or await polymarket_api.get_quote(ticker, side)
        ask = q.get("ask_cents")
    except Exception as e:
        logger.debug(f"[copy] entry quote {ticker}: {e}")
    limit_cents = int(ask) if ask else (h.get("price_cents") or max_cents)
    if limit_cents > max_cents:
        logger.info(
            f"[copy] skip {_short_addr(h['wallets'][0])} {ticker} {side}: "
            f"price {limit_cents}c > entry_max {max_cents}c"
        )
        return None
    if min_cents > 0 and limit_cents < min_cents:
        logger.info(
            f"[copy] skip {_short_addr(h['wallets'][0])} {ticker} {side}: "
            f"price {limit_cents}c < entry_min {min_cents}c"
        )
        return None
    limit_cents = max(1, min(99, limit_cents))

    contracts = _compute_copy_contracts(
        cfg, price_cents=limit_cents, balance_usd=balance_usd, cap_usd=available_usd
    )
    if contracts < _COPY_MIN_CONTRACTS:
        logger.info(
            f"[copy] skip {ticker} {side}: budget funds {contracts} contract(s) at "
            f"{limit_cents}c, below the {_COPY_MIN_CONTRACTS}-contract minimum"
        )
        return None

    _why = trader.headroom_blocks(
        cfg, env, balance_usd, contracts * limit_cents / 100.0
    )
    if _why:
        logger.info(f"[copy] skip {ticker} {side}: {_why}")
        return None

    coid = f"krypt-copy-{uuid.uuid4().hex[:10]}"
    row = {
        "signal_source": "copy",
        "signal_id": abs(hash((ticker, side, coid))) % 2_000_000_000,
        "ticker": ticker, "event_ticker": h.get("event_ticker") or "",
        "title": h.get("title") or ticker,
        "category": await _market_category(ticker),
        "direction": side, "action": "buy",
        "target_contracts": contracts, "limit_price_cents": limit_cents,
        "client_order_id": coid, "confidence": 0.0, "edge_pts": 0.0,
        "signal_price": float(limit_cents), "network": env,
    }

    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=side, action="buy",
            count=contracts, price_cents=limit_cents, client_order_id=coid,
        )
    except polymarket_api.OrderStatusUnknownError as e:
        logger.warning(f"[copy] enter {ticker}: {e}")
        resp = e.response
    except Exception as e:
        row.update({"status": "error", "error": str(e)[:200]})
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            db.update_bot_position(conn, pid, resolved=1)
            conn.execute("UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?", (pid,))
            logger.error(f"[copy] enter order failed {ticker}: {e}")
            return db.fetch_position_by_id(conn, pid)

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    oid = order.get("order_id") if isinstance(order, dict) else None
    if not oid:
        row.update({"status": "error", "error": "no order_id from exchange"})
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            db.update_bot_position(conn, pid, resolved=1)
            conn.execute("UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?", (pid,))
            return db.fetch_position_by_id(conn, pid)
    row.update({"status": "submitted", "order_id": oid})
    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, row)
        logger.info(
            f"[copy] enter {ticker} {side} x{contracts} @ {limit_cents}c "
            f"(following {_short_addr(h['wallets'][0])})"
        )
        return db.fetch_position_by_id(conn, pid)


async def _book_gone_copy(pos: dict) -> Optional[dict]:
    """Book a copy whose shares the wallet NO LONGER HOLDS (our exit already
    filled, or it was closed on Polymarket) as CLOSED — with real proceeds/P&L
    from the wallet's /activity. Returns the closed row, or None when there's no
    SELL/REDEEM evidence yet (left for the reconcile/external-exit sweep; we still
    place NO order). This is what ENDS the 'not enough balance: balance 0' retry
    loop where the engine kept trying to sell a position it no longer held."""
    pid = pos["id"]
    cost = float(pos.get("cost_usd") or 0.0)
    try:
        activity = await polymarket_api.get_activity(limit=1000)
        info = trader._exit_proceeds(pos, activity)
    except Exception as e:
        logger.debug(f"[copy] gone-copy activity read failed: {e}")
        return None
    if info is None:
        return None
    prior_proceeds = float(pos.get("settlement_usd") or 0.0)
    prior_pnl = float(pos.get("pnl_usd") or 0.0)
    sell_gross = float(info.get("sell_gross") or 0.0)
    prior_fee = (float(info.get("sell_fee") or 0.0) * min(1.0, prior_proceeds / sell_gross)
                 if sell_gross > 0 else 0.0)
    remaining_proceeds = max(0.0, round(float(info["proceeds"]) + prior_fee - prior_proceeds, 4))
    total_proceeds = round(prior_proceeds + remaining_proceeds, 4)
    fee = trader._position_fee_usd(pos)
    pnl = round(prior_pnl + (remaining_proceeds - cost - fee), 4)
    with db.get_db() as conn:
        db.update_bot_position(
            conn, pid, resolved=1, status="filled",
            outcome_correct=(1 if pnl > 0.05 else (0 if pnl < -0.05 else None)),
            settlement_usd=total_proceeds, pnl_usd=pnl,
        )
        conn.execute(
            "UPDATE bot_positions SET resolved_at=datetime('now'), closed_early=1 WHERE id=?",
            (pid,),
        )
        db.log_event(conn, pid, "copy-exit",
                     note=f"position no longer held ({info['kind']}); "
                          f"proceeds=${total_proceeds:.2f} pnl=${pnl:+.2f}")
        logger.info(f"[copy] exit {pos['ticker']} booked (no longer held) pnl=${pnl:+.2f}")
        return db.fetch_position_by_id(conn, pid)


async def _exit_copy(pos: dict, cfg: dict, held_qty: dict, holdings_ok: bool,
                     sell_qty: Optional[int] = None,
                     reason: str = "followed wallets exited") -> Optional[dict]:
    """Close (or REDUCE) one copy. Full exit when every followed wallet left
    the market; `sell_qty` sells only that slice when they materially reduced
    (mirroring their scale-out) and keeps the rest open. Crosses DOWN to the
    current bid with a FAK (fill-and-kill) sell so it NEVER rests on the book —
    a resting GTC exit was what locked the shares and made the next tick fail
    with 'not enough balance (active orders)'. Always real — the caller only
    runs when authed.

    `held_qty`/`holdings_ok` are the live wallet holdings: if we no longer hold
    the shares the position is booked closed instead of re-selling nothing."""
    ticker, direction = pos["ticker"], pos["direction"]
    qty = int(pos.get("filled_contracts") or 0)
    cost = float(pos.get("cost_usd") or 0.0)
    pid = pos["id"]

    if pos.get("status") in ("submitted", "partial") and qty <= 0:
        oid = pos.get("order_id")
        if oid:
            try:
                await polymarket_api.cancel_order(oid)
            except Exception as e:
                logger.debug(f"[copy] cancel {ticker}: {e}")
        held_now = int(held_qty.get((ticker, direction)) or 0) if holdings_ok else 0
        if held_now > 0:
            qty = held_now
            if cost <= 0:
                cost = qty * float(pos.get("limit_price_cents") or 0) / 100.0
        elif holdings_ok:
            with db.get_db() as conn:
                db.update_bot_position(conn, pid, status="canceled", resolved=1,
                                       error="followed wallet exited before fill")
                conn.execute("UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?", (pid,))
                return db.fetch_position_by_id(conn, pid)
        else:
            return None

    if qty <= 0:
        return None

    if holdings_ok:
        held = held_qty.get((ticker, direction)) or 0
        if held <= 0:
            return await _book_gone_copy(pos)
        if held < qty:
            qty = int(held)

    sell_request = qty if sell_qty is None else max(0, min(int(sell_qty), qty))
    if sell_request <= 0:
        return None

    rkey = (ticker, direction)
    if time.time() - _exit_retry_at.get(rkey, 0.0) < _EXIT_RETRY_SEC:
        return None

    try:
        q = trader.ws_quote_cents(ticker, direction) or await polymarket_api.get_quote(ticker, direction)
        bid = q.get("bid_cents")
    except Exception:
        bid = None
    sell_px = max(1, min(99, int(bid) if bid else 1))
    if sell_qty is not None and sell_request * sell_px < 100:
        return None
    coid = f"krypt-copyx-{pid}-{uuid.uuid4().hex[:6]}"
    _exit_retry_at[rkey] = time.time()
    trader.cap_dict_size(_exit_retry_at)
    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=direction, action="sell",
            count=sell_request, price_cents=sell_px, client_order_id=coid,
            order_type="FAK",
        )
    except Exception as e:
        logger.warning(f"[copy] exit sell {ticker} failed: {e}")
        return None
    order = (resp.get("order") if isinstance(resp, dict) else None) or {}
    sold, avg_cents = await trader._confirm_sell(order.get("order_id"), sell_request, sell_px)
    if sold <= 0:
        logger.warning(f"[copy] exit {ticker}: sell didn't fill; still holding {qty}")
        return None

    proceeds = sold * avg_cents / 100.0
    prior_pnl = float(pos.get("pnl_usd") or 0.0)
    prior_proceeds = float(pos.get("settlement_usd") or 0.0)
    if sold < qty:
        remaining = qty - sold
        sold_cost = cost * (sold / qty)
        slice_pnl = proceeds - sold_cost - trader._sell_fee_usd(pos, sold, avg_cents)
        label = "copy-reduce" if sell_qty is not None else "copy-exit-partial"
        with db.get_db() as conn:
            db.update_bot_position(
                conn, pid, filled_contracts=remaining,
                cost_usd=round(cost - sold_cost, 4),
                pnl_usd=round(prior_pnl + slice_pnl, 4),
                settlement_usd=round(prior_proceeds + proceeds, 4),
            )
            db.log_event(conn, pid, label,
                         note=f"{reason}; sold {sold}/{qty} @ ~{avg_cents:.0f}c; "
                              f"{remaining} left; slice pnl=${slice_pnl:+.2f}")
            logger.info(f"[copy] {label} {ticker} sold {sold}/{qty} @ ~{avg_cents:.0f}c "
                        f"(slice pnl ${slice_pnl:+.2f})")
            return db.fetch_position_by_id(conn, pid)

    pnl = (prior_pnl + (proceeds - cost)
           - trader._sell_fee_usd(pos, sold, avg_cents)
           - trader._position_fee_usd(pos))
    total_proceeds = prior_proceeds + proceeds
    with db.get_db() as conn:
        db.update_bot_position(
            conn, pid, resolved=1, status="filled",
            outcome_correct=(1 if pnl > 0.05 else (0 if pnl < -0.05 else None)),
            settlement_usd=round(total_proceeds, 4), pnl_usd=round(pnl, 4),
        )
        conn.execute("UPDATE bot_positions SET resolved_at=datetime('now'), closed_early=1 WHERE id=?", (pid,))
        db.log_event(conn, pid, "copy-exit",
                     note=f"{reason}; sold {sold} @ ~{avg_cents:.0f}c pnl=${pnl:+.2f}")
        logger.info(f"[copy] exit {ticker} sold {sold} @ ~{avg_cents:.0f}c pnl=${pnl:+.2f}")
        return db.fetch_position_by_id(conn, pid)


async def _entry_pass(
    cfg: dict, env: str, entry_map: dict, *,
    follows_ok: bool, anchor_qty_base: bool = True, source: str = "poll",
) -> list[dict]:
    """THE copy-entry pipeline. One implementation, two callers:

      * `run_tick` — the polled pass; `entry_map` is every followed wallet's
        current Data-API holdings.
      * `drain_pushed_entries` — the websocket pass; `entry_map` is holdings
        SYNTHESISED from activity-tape trades that just executed.

    Both run exactly the same gates in the same order: the account-wide daily
    risk halt, copy's own daily realized-loss cap, the lifetime loss breaker,
    balance, the shared cash-reserve / total-exposure caps, the wallet
    mutual-exclusion lock, `copy_only_new_entries`, the per-market cooldown,
    the open-copy / other-engine dedupe, `copy_allow_reentries`, and the entry
    band + sizing + exchange minimum inside `_enter_copy`. Nothing can enter by
    the push route that the poll route would have refused — that is the whole
    contract of building entries from the socket.

    `follows_ok` gates the baseline PRUNE: only a COMPLETE holdings read may
    conclude a wallet no longer holds something, and a pushed map is by
    definition a fragment.

    `anchor_qty_base` writes the "their size when we copied" anchor. The pushed
    path deliberately leaves it UNSET: a trade frame carries its own size, not
    the wallet's total, so anchoring to the trade size would make the very next
    poll read the difference (their pre-existing shares) as a fresh scale-in and
    buy again. Left absent, the next poll takes the `base is None` branch below
    — record their real total, do not retro-buy — which is exactly the
    reconcile we want.
    """
    changed: list[dict] = []
    now = time.time()
    global _last_halt_log_t
    blocked, why = trader._is_blocked_by_daily_risk(cfg, env)
    if blocked:
        if now - _last_halt_log_t >= 60:
            _last_halt_log_t = now
            logger.warning(f"[copy] {why}; no new copies")
        return changed
    loss_limit = float(cfg.get("copy_daily_loss_limit") or 0.0)
    if loss_limit < 0 and _today_copy_pnl(env) <= loss_limit:
        if now - _last_halt_log_t >= 60:
            _last_halt_log_t = now
            logger.warning(f"[copy] daily loss limit hit (today <= ${loss_limit:.0f}); no new copies")
        return changed
    _lt_trip, _lt_why = _lifetime_loss_tripped(cfg, env)
    if _lt_trip:
        if now - _last_halt_log_t >= 60:
            _last_halt_log_t = now
            logger.warning(f"[copy] {_lt_why}; no new copies")
        return changed

    open_copies = _our_open_copies(env)
    open_keys = {(p["ticker"], p["direction"]) for p in open_copies}
    qty_base: dict = _load_qty_base(env)
    qb_changed = False

    max_conc = int(cfg.get("copy_max_concurrent", 10) or 10)
    open_count = len(open_keys)
    balance_usd = await _bankroll_usd(cfg, True)
    if balance_usd <= 0.0 and trader.last_balance_read_ok():
        return changed

    with db.get_db() as conn:
        exposure = db.current_total_exposure_usd(conn, env)
        filled_exposure = db.current_filled_exposure_usd(conn, env)
        held_rows = conn.execute(
            """SELECT ticker, direction FROM bot_positions
               WHERE resolved=0 AND status IN ('submitted','partial','filled')
                 AND network=?""",
            (env,),
        ).fetchall()
    held_keys = {(r["ticker"], r["direction"]) for r in held_rows}
    pending_notional = max(0.0, exposure - filled_exposure)
    total_bankroll = max(0.0, balance_usd) + max(0.0, filled_exposure)
    reserve = total_bankroll * float(cfg.get("min_cash_reserve_fraction", 0.0) or 0.0)
    max_exposure = total_bankroll * float(cfg.get("max_total_exposure_fraction", 1.0) or 1.0)
    spendable_cash = max(0.0, balance_usd) - pending_notional
    available_usd = min(max(0.0, spendable_cash - reserve), max(0.0, max_exposure - exposure))

    entry_set = _filter_new_entries(entry_map, env, cfg) if cfg.get("copy_only_new_entries", True) else entry_map

    _ok_open, _lock_why = trader.can_open_new_entries(env)
    if not _ok_open:
        logger.info(f"[copy] {_lock_why}")
        return changed

    allow_reentries = bool(cfg.get("copy_allow_reentries"))
    if follows_ok:
        for k in [k for k in qty_base if tuple(k.rsplit("|", 1)) not in entry_map]:
            del qty_base[k]
            qb_changed = True

    for key, h in entry_set.items():
        kstr = f"{key[0]}|{key[1]}"
        addon = False
        if key in open_keys or key in held_keys:
            if not allow_reentries or key not in open_keys:
                continue
            cur_qty = float(h.get("qty") or 0.0)
            base = qty_base.get(kstr)
            if base is None:
                if anchor_qty_base:
                    qty_base[kstr] = cur_qty
                    qb_changed = True
                continue
            px = float(h.get("price_cents") or 50) / 100.0
            added_usd = max(0.0, cur_qty - float(base)) * px
            if added_usd < max(1.0, float(cfg.get("copy_min_trade_usd") or 0.0)):
                continue
            addon = True
        if now - _copy_cooldown.get(key, 0.0) < _COPY_COOLDOWN_SEC:
            continue
        if not addon and open_count >= max_conc:
            continue
        try:
            row = await _enter_copy(h, cfg, env, balance_usd, available_usd)
            if row is None or row.get("status") == "error":
                _copy_cooldown[key] = now
                trader.cap_dict_size(_copy_cooldown)
            if row:
                changed.append(row)
                if not addon:
                    open_count += 1
                    open_keys.add(key)
                if row.get("status") in ("submitted", "filled", "partial"):
                    if anchor_qty_base:
                        qty_base[kstr] = float(h.get("qty") or 0.0)
                        qb_changed = True
                    committed = float(row.get("cost_usd") or 0.0)
                    if committed <= 0:
                        committed = (int(row.get("target_contracts") or 0)
                                     * int(row.get("limit_price_cents") or 0) / 100.0)
                    balance_usd = max(0.0, balance_usd - committed)
                    available_usd = max(0.0, available_usd - committed)
        except Exception as e:
            _copy_cooldown[key] = now
            trader.cap_dict_size(_copy_cooldown)
            logger.warning(f"[copy] enter {h.get('ticker')} failed ({source}): {e}")

    if qb_changed:
        _save_qty_base(env, qty_base)
    return changed


async def run_tick(cfg: dict, *, authed: bool) -> list[dict]:
    """One copy-trading cycle. Returns the rows that changed (opened/closed)
    so the caller can emit position events. No-op when disabled, no wallets, or
    the wallet isn't connected (there is no paper mode — copies are real)."""
    _remember_push_ctx(cfg, authed)
    from config import engine_on
    if not engine_on(cfg, "copy_enabled") or not (cfg.get("copy_wallets") or []):
        return []
    if not authed:
        return []
    env = trader.get_env()
    now = time.time()
    changed: list[dict] = []
    if _pushed_changed:
        changed.extend(_pushed_changed)
        _pushed_changed.clear()

    followed, follows_ok = await _followed_holdings(cfg)
    open_copies = _our_open_copies(env)
    open_keys = {(p["ticker"], p["direction"]) for p in open_copies}
    global last_open_count
    last_open_count = len(open_keys)
    await trader.warm_books(open_keys | set(followed))
    qty_base: dict = _load_qty_base(env)
    qb_changed = False

    if not follows_ok:
        logger.warning("[copy] skipping exit pass — one or more wallet reads failed")
    to_exit = [] if not follows_ok else [
        p for p in open_copies if (p["ticker"], p["direction"]) not in followed
    ]
    reduce_jobs: list[tuple[dict, int, str, float]] = []
    if follows_ok and bool(cfg.get("copy_mirror_reductions", True)):
        try:
            thr = float(cfg.get("copy_reduce_threshold", 0.25) or 0.25)
        except (TypeError, ValueError):
            thr = 0.25
        thr = max(0.05, min(0.95, thr))
        for p in open_copies:
            key = (p["ticker"], p["direction"])
            if key not in followed:
                continue
            if p.get("status") != "filled" or int(p.get("filled_contracts") or 0) <= 0:
                continue
            kstr = f"{key[0]}|{key[1]}"
            cur_qty = float(followed[key].get("qty") or 0.0)
            base = qty_base.get(kstr)
            if base is None or float(base) <= 0:
                qty_base[kstr] = cur_qty
                qb_changed = True
                continue
            base = float(base)
            if cur_qty < base * (1.0 - thr):
                frac_gone = 1.0 - (cur_qty / base)
                sell = int(round(int(p.get("filled_contracts") or 0) * frac_gone))
                if sell >= 1:
                    reduce_jobs.append((p, sell, kstr, cur_qty))
    held_qty: dict = {}
    holdings_ok = False
    if to_exit or reduce_jobs:
        try:
            for p in await polymarket_api.get_positions():
                q = float(p.get("position_fp") or 0.0)
                if q:
                    held_qty[(p.get("ticker") or "", "yes" if q > 0 else "no")] = abs(q)
            holdings_ok = True
        except Exception as e:
            logger.debug(f"[copy] holdings fetch for exits failed: {e}")
    for pos in to_exit:
        key = (pos["ticker"], pos["direction"])
        try:
            row = await _exit_copy(pos, cfg, held_qty, holdings_ok)
            if row and row.get("resolved"):
                changed.append(row)
                open_keys.discard(key)
                _copy_cooldown[key] = now
                trader.cap_dict_size(_copy_cooldown)
            elif row:
                changed.append(row)
        except Exception as e:
            logger.warning(f"[copy] exit {pos['ticker']} failed: {e}")
    for pos, sell, kstr, cur_qty in reduce_jobs:
        try:
            row = await _exit_copy(
                pos, cfg, held_qty, holdings_ok, sell_qty=sell,
                reason=f"followed wallets cut to {cur_qty:.0f} shares",
            )
            if row:
                changed.append(row)
                qty_base[kstr] = cur_qty
                qb_changed = True
                if row.get("resolved"):
                    open_keys.discard((pos["ticker"], pos["direction"]))
        except Exception as e:
            logger.warning(f"[copy] reduce {pos['ticker']} failed: {e}")
    if qb_changed:
        _save_qty_base(env, qty_base)

    changed.extend(await _entry_pass(cfg, env, followed, follows_ok=follows_ok))
    return changed


_own_addr_cache: tuple[float, set] = (0.0, set())
_OWN_ADDR_TTL = 60.0


def _remember_push_ctx(cfg: dict, authed: bool) -> None:
    """Snapshot what the sync socket callback needs from cfg.

    Called once per LOOP PASS (drain_pushed_entries runs unconditionally), not
    once per copy tick, so it stays allocation-cheap: `_own_addresses` reads the
    wallet sidecar off disk, which is fine per tick and wasteful per second."""
    try:
        on = (__import__("config").engine_on(cfg, "copy_enabled") and bool(authed)
              and bool(cfg.get("copy_activity_ws", True)))
        global _own_addr_cache
        if time.time() - _own_addr_cache[0] > _OWN_ADDR_TTL:
            _own_addr_cache = (time.time(), _own_addresses())
        own = _own_addr_cache[1]
        _push_ctx["on"] = on
        _push_ctx["wallets"] = {
            str(w).strip().lower() for w in (cfg.get("copy_wallets") or [])
            if isinstance(w, str) and str(w).strip()
        } - own - set(_wallet_too_large)
        _push_ctx["min_usd"] = float(cfg.get("copy_min_trade_usd") or 0.0)
    except Exception:
        _push_ctx["on"] = False


def _trade_id(t: dict, wallet: str, ticker: str, idx: int) -> str:
    """Stable identity for a pushed trade. The tape's transactionHash is one
    per transaction and a transaction can settle several outcomes, so the
    outcome index is part of the key; without a hash we fall back to the
    (wallet, market, outcome, size, price, timestamp) tuple, which the same
    print always reproduces and a genuinely separate fill practically never
    collides with."""
    tx = str(t.get("transactionHash") or t.get("hash") or "").strip()
    if tx:
        return f"{tx}|{idx}"
    return (f"{wallet}|{ticker}|{idx}|{t.get('size')}|{t.get('price')}|"
            f"{t.get('timestamp') or t.get('at') or ''}")


def on_activity_trade(trade: dict) -> None:
    """Ingest ONE trade frame from the activity websocket. Sync, cheap and
    silent — it runs on the socket's read loop, so it validates, filters,
    dedupes and enqueues, and does no I/O and never raises.

    Accepts either the hit shape `activity_ws.drain_hits()` produces or a raw
    tape payload (`proxyWallet`/`side`/…), so it doesn't care which side of
    that module it is wired to.

    Everything below is a REJECTION, not a decision: only a BUY, by a wallet
    we actually follow, in a market we can identify, at a sane size and price,
    that we have not already seen, is worth waking the entry pipeline for. The
    pipeline itself (drain_pushed_entries -> _entry_pass) decides whether to
    trade it, with the live config.
    """
    try:
        if not _push_ctx.get("on") or not isinstance(trade, dict):
            return
        wallet = str(trade.get("wallet") or trade.get("proxyWallet") or "").strip().lower()
        if not wallet or wallet not in _push_ctx.get("wallets", ()):
            return
        if str(trade.get("side") or "").upper() != "BUY":
            return
        ticker = str(trade.get("conditionId") or "").strip()
        if not ticker:
            return
        try:
            idx = int(trade.get("outcomeIndex"))
        except (TypeError, ValueError):
            return
        if idx not in (0, 1):
            return
        side = "yes" if idx == 0 else "no"
        try:
            size = float(trade.get("size"))
            price = float(trade.get("price"))
        except (TypeError, ValueError):
            return
        if not (size > 0) or not (0.0 < price < 1.0):
            return
        notional = size * price
        if notional < float(_push_ctx.get("min_usd") or 0.0):
            return
        now = time.time()
        tid = _trade_id(trade, wallet, ticker, idx)
        if tid in _pushed_ids:
            return
        _pushed_ids[tid] = now
        trader.cap_dict_size(_pushed_ids, 2000)

        key = (ticker, side)
        cur = _pushed.get(key)
        if cur is None:
            if len(_pushed) >= _PUSHED_MAX:
                return
            _pushed[key] = {
                "ticker": ticker, "side": side,
                "price_cents": max(1, min(99, int(round(price * 100)))),
                "cost": notional,
                "qty": size,
                "title": str(trade.get("title") or trade.get("name") or ticker),
                "event_ticker": str(trade.get("eventSlug") or trade.get("slug") or ""),
                "wallets": [wallet],
                "at": now,
            }
        else:
            cur["qty"] = float(cur.get("qty") or 0.0) + size
            cur["cost"] = float(cur.get("cost") or 0.0) + notional
            cur["price_cents"] = max(1, min(99, int(round(price * 100))))
            cur["at"] = now
            if wallet not in cur["wallets"]:
                cur["wallets"].append(wallet)
    except Exception as e:   # pragma: no cover - defensive; the socket must live
        logger.debug(f"[copy] pushed trade ignored: {e}")


async def drain_pushed_entries(cfg: dict, *, authed: bool = True) -> int:
    """Run the entries queued by `on_activity_trade` through the normal copy
    entry pipeline. Returns how many positions it opened.

    Call it each tick BEFORE the Data-API read: that is the entire latency win
    (the tape has the trade ~1s after it executes; /positions needs several
    seconds more). Everything it opens still goes through `_entry_pass`, so
    every gate — daily/lifetime loss, exposure and reserve caps, concurrency,
    the wallet lock, cooldowns, `copy_only_new_entries`, the entry band, sizing
    and the exchange minimum — applies exactly as on the polled route, and the
    open-copy/other-engine dedupe is the same (ticker, side) key, so a pushed
    entry and the poll that later sees the same position cannot both open one.

    Opened rows are handed to the next `run_tick` return value so the service
    emits position events for them without extra wiring."""
    _remember_push_ctx(cfg, authed)
    if not _push_ctx.get("on") or not (cfg.get("copy_wallets") or []):
        _pushed.clear()
        return 0
    if not _pushed:
        return 0
    now = time.time()
    for _tid in [t for t, ts in _pushed_ids.items() if now - ts > _PUSHED_ID_TTL]:
        _pushed_ids.pop(_tid, None)
    pushed = dict(_pushed)
    _pushed.clear()
    entry_map = {
        k: h for k, h in pushed.items()
        if now - float(h.get("at") or 0.0) <= _PUSHED_STALE_SEC
    }
    if not entry_map:
        return 0
    try:
        rows = await _entry_pass(
            cfg, trader.get_env(), entry_map,
            follows_ok=False, anchor_qty_base=False, source="push",
        )
    except Exception as e:
        logger.warning(f"[copy] pushed-entry pass failed: {e}")
        return 0
    opened = [r for r in rows if (r.get("status") or "") in ("submitted", "filled", "partial")]
    if rows:
        _pushed_changed.extend(rows)
        del _pushed_changed[:-200]
    if opened:
        logger.info(
            f"[copy] activity push opened {len(opened)} entr"
            f"{'y' if len(opened) == 1 else 'ies'} ahead of the Data-API read"
        )
    await trader.warm_books(entry_map.keys())
    return len(opened)


def pending_pushes() -> int:
    """Queued pushed trades — for status/diagnostics only."""
    return len(_pushed)


async def status(cfg: dict, *, authed: bool = False) -> dict:
    """Snapshot for the Copy Trading page: per-wallet holding counts, our open
    copies, today's copy P&L, and the enabled/connected flags."""
    env = trader.get_env()
    wallets = cfg.get("copy_wallets") or []

    wallet_info = []
    for w in wallets:
        n, value = 0, 0.0
        wl = (w or "").strip().lower()
        error = _too_large_message(wl)
        try:
            cached = _wallet_pos_cache.get(wl)
            if error:
                positions = cached[1] if cached else []
            elif cached and time.time() - cached[0] <= _WALLET_CACHE_UI_SEC:
                positions = cached[1]
            else:
                positions = await polymarket_api.get_positions(limit=500, user=w)
                _wallet_pos_cache[wl] = (time.time(), positions)
            for p in positions:
                if float(p.get("position_fp") or 0.0) != 0.0:
                    n += 1
                    value += float(p.get("market_exposure_dollars") or 0.0)
        except Exception as e:
            logger.debug(f"[copy] status fetch {_short_addr(w)}: {e}")
        wallet_info.append({
            "address": w, "short": _short_addr(w),
            "positions": n, "valueUsd": round(value, 2),
            "error": error,
        })

    open_copies = _our_open_copies(env)
    loss_limit = float(cfg.get("copy_daily_loss_limit") or 0.0)
    today_pnl = _today_copy_pnl(env)

    return {
        "enabled": bool(cfg.get("copy_enabled")),
        "authed": bool(authed),
        "trading": __import__("config").engine_on(cfg, "copy_enabled") and bool(authed),
        "wallets": wallet_info,
        "openCopies": len(open_copies),
        "todayPnlUsd": round(today_pnl, 2),
        "lossLimitHit": loss_limit < 0 and today_pnl <= loss_limit,
        "sizing": {
            "mode": (cfg.get("copy_sizing_mode") or "fixed"),
            "fixedUsd": float(cfg.get("copy_fixed_usd") or 0.0),
            "balancePct": float(cfg.get("copy_balance_pct") or 0.0),
        },
        "maxConcurrent": int(cfg.get("copy_max_concurrent") or 0),
        "entryMaxCents": int(cfg.get("copy_entry_max_cents") or 0),
        "entryMinCents": int(cfg.get("copy_entry_min_cents") or 0),
    }
