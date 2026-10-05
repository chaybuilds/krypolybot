from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import backtest
import clob_ws
import db
import instance_lock
import rules as rules_engine
from polymarket_api import (
    PolymarketAPIError, cancel_order, cancel_orders, fetch_market, fetch_markets_map,
    get_activity, get_balance,
    get_fills_for_order, get_market_meta, get_order, get_quote, get_positions,
    place_limit_order,
)
from polymarket_auth import get_env, trading_address

logger = logging.getLogger(__name__)


_balance_cache: dict[str, dict] = {}


def last_balance_read_ok(env: str | None = None) -> bool:
    cached = _balance_cache.get(env or get_env())
    return bool(cached and cached.get("ok"))
_balance_fail_log_at: dict[str, float] = {}
_BALANCE_FAIL_LOG_EVERY = 300.0


async def refresh_balance(cfg: dict, force: bool = False) -> tuple[int, int]:
    env = get_env()
    interval = float(cfg.get("balance_poll_interval", 60))
    loop = asyncio.get_event_loop()
    now = loop.time()
    cached = _balance_cache.get(env)
    if not force and cached and (now - cached["at"]) < interval:
        return cached["cents"], cached["portfolio_cents"]
    try:
        data = await get_balance()
        cents = int(data.get("balance", 0))
        port_raw = data.get("portfolio_value")
        if port_raw is None:
            port = cached["portfolio_cents"] if cached else 0
        else:
            port = int(port_raw)
        _balance_cache[env] = {"cents": cents, "portfolio_cents": port, "at": now, "ok": True}
        return cents, port
    except Exception as e:
        prev_cents = cached["cents"] if cached else 0
        prev_port = cached["portfolio_cents"] if cached else 0
        _balance_cache[env] = {"cents": prev_cents, "portfolio_cents": prev_port, "at": now, "ok": False}
        wt = time.time()
        if wt - _balance_fail_log_at.get(env, 0.0) >= _BALANCE_FAIL_LOG_EVERY:
            _balance_fail_log_at[env] = wt
            logger.warning(f"balance fetch failed: {e}")
        return prev_cents, prev_port


def _edge_fraction(edge_pts: float, cfg: dict) -> float:
    lo_e = float(cfg["sizing_base_edge"])
    hi_e = float(cfg["sizing_max_edge"])
    if edge_pts <= lo_e:
        return 0.0
    if edge_pts >= hi_e or hi_e <= lo_e:
        return 1.0
    return (edge_pts - lo_e) / (hi_e - lo_e)


def _compute_position_usd(balance_usd: float, edge_pts: float, cfg: dict) -> float:
    lo_f = float(cfg["min_size_fraction"])
    hi_f = float(cfg["max_size_fraction"])
    frac = lo_f + _edge_fraction(edge_pts, cfg) * (hi_f - lo_f)
    return min(balance_usd * frac, float(cfg["hard_max_position_usd"]))


def _compute_target_usd(
    balance_usd: float, edge_pts: float, limit_cents: int, cfg: dict
) -> float:
    if (cfg.get("sizing_mode") or "percent").lower() == "contracts":
        min_c = max(1, int(cfg.get("min_contracts", 5) or 5))
        max_c = max(min_c, int(cfg.get("max_contracts", min_c) or min_c))
        contracts = round(min_c + _edge_fraction(edge_pts, cfg) * (max_c - min_c))
        usd = contracts * max(1, int(limit_cents)) / 100.0
        return min(usd, float(cfg["hard_max_position_usd"]))
    return _compute_position_usd(balance_usd, edge_pts, cfg)


_WS_QUOTES_OFF = os.environ.get("KRYPT_WS_QUOTES", "1").strip().lower() in (
    "0", "off", "false", "no",
)

_ws_token: dict[tuple[str, str], str] = {}
_WS_TOKEN_MAX = 400
_WS_WARM_FETCH_MAX = 8


def ws_quote_cents(ticker: str, direction: str) -> Optional[dict]:
    if _WS_QUOTES_OFF:
        return None
    try:
        token = _ws_token.get((ticker, (direction or "").lower()))
        if not token:
            return None
        q = clob_ws.quote_cents_if_fresh(token)
        if q is None:
            return None
        return {"bid_cents": q[0], "ask_cents": q[1]}
    except Exception:
        return None


async def warm_books(pairs, *, limit: int = 120) -> int:
    if _WS_QUOTES_OFF:
        return 0
    tokens: list[str] = []
    to_fetch: list[tuple[str, str]] = []
    try:
        for ticker, direction in list(pairs)[:limit]:
            key = (ticker, (direction or "").lower())
            if not ticker or key[1] not in ("yes", "no"):
                continue
            tok = _ws_token.get(key)
            if tok:
                tokens.append(tok)
            elif len(to_fetch) < _WS_WARM_FETCH_MAX:
                to_fetch.append(key)
        if to_fetch:
            metas = await asyncio.gather(
                *[get_market_meta(t) for t, _d in to_fetch], return_exceptions=True,
            )
            for (ticker, direction), meta in zip(to_fetch, metas):
                if not isinstance(meta, dict):
                    continue
                tok = meta.get("yes_token") if direction == "yes" else meta.get("no_token")
                if tok:
                    _ws_token[(ticker, direction)] = str(tok)
                    tokens.append(str(tok))
            cap_dict_size(_ws_token, _WS_TOKEN_MAX)
        if tokens:
            clob_ws.warm(*tokens)
    except Exception as e:
        logger.debug(f"warm_books: {e}")
    return len(tokens)


async def _compute_limit_price_cents(
    ticker: str, direction: str, signal_price_cents: int, cfg: dict,
    book: Optional[dict] = None,
) -> int:
    direction = direction.lower()
    style = cfg.get("order_style", "limit_cross")
    if style == "market":
        return max(1, min(99, int(cfg.get("max_entry_price_cents", 99) or 99)))
    try:
        q = ws_quote_cents(ticker, direction) or await get_quote(ticker, direction)
        ask = q.get("ask_cents")
        bid = q.get("bid_cents")
        if book is not None:
            book["ask_cents"] = ask
            book["bid_cents"] = bid
        if style == "limit_mid" and ask is not None and bid is not None:
            return max(1, min(99, (ask + bid) // 2))
        if ask is not None and 1 <= ask <= 99:
            cap = int(cfg.get("max_entry_price_cents", 99) or 99)
            if ask > cap:
                return ask
            margin = int(cfg.get("cross_spread_fallback_offset", 2) or 0)
            return max(1, min(99, cap, ask + margin))
    except Exception as e:
        logger.warning(f"quote fetch failed for {ticker}: {e}")
    fallback = signal_price_cents + int(cfg.get("cross_spread_fallback_offset", 2))
    return max(1, min(99, fallback))


def _is_maker_limit(limit_cents: int, best_ask_cents) -> int:
    try:
        if best_ask_cents is None:
            return 0
        return 1 if int(limit_cents) < int(best_ask_cents) else 0
    except (TypeError, ValueError):
        return 0


def _signal_cost_cents(signal: dict, source: str) -> tuple[str, int]:
    if source == "whale":
        direction = (signal.get("taker_side") or "yes").lower()
        price_frac = float(signal.get("price") or 0.0)
        cents = max(1, min(99, int(round(price_frac * 100))))
        return direction, cents
    if source == "convergence":
        direction = (signal.get("direction") or "yes").lower()
        price_frac = float(signal.get("price") or 0.5)
        cents = max(1, min(99, int(round(price_frac * 100))))
        return direction, cents
    direction = (signal.get("direction") or "yes").lower()
    yes_frac = float(signal.get("price") or 0.0)
    yes_cents = max(1, min(99, int(round(yes_frac * 100))))
    if direction == "yes":
        return direction, yes_cents
    return direction, max(1, min(99, 100 - yes_cents))


def _compute_edge(signal: dict, source: str) -> float:
    conf = float(signal.get("confidence") or 0.0)
    if source == "whale":
        implied = float(signal.get("price") or 0.0) * 100
    elif source == "convergence":
        implied = float(signal.get("price") or 0.5) * 100
    else:
        direction = (signal.get("direction") or "yes").lower()
        yes = float(signal.get("price") or 0.0)
        implied = (yes if direction == "yes" else (1.0 - yes)) * 100
    implied = max(5.0, min(implied, 95.0))
    return conf - implied


def _signal_rule_values(signal: dict, source: str) -> dict:
    _, cost_cents = _signal_cost_cents(signal, source)
    return {
        "confidence": float(signal.get("confidence") or 0.0),
        "edge": _compute_edge(signal, source),
        "costCents": cost_cents,
    }


def _days_until_close(close_time: str) -> Optional[float]:
    if not close_time:
        return None
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ct = datetime.strptime(close_time, fmt)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            return (ct - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except ValueError:
            continue
    return None


def _enrich_close_time(conn, sig: dict) -> None:
    if sig.get("close_time"):
        return
    m = db.get_market(conn, sig.get("ticker") or "")
    if m:
        sig["close_time"] = m.get("close_time") or ""


def should_trade(signal: dict, source: str, cfg: dict) -> tuple[bool, str]:
    conf = float(signal.get("confidence") or 0.0)
    edge = _compute_edge(signal, source)
    use_rules = bool(cfg.get("use_rules"))

    if source == "whale":
        if not cfg.get("trade_whales", False):
            return False, "whales disabled"
        if not use_rules:
            if conf < cfg["min_confidence_whale"]:
                return False, f"conf {conf:.1f} < {cfg['min_confidence_whale']}"
            if edge < cfg["min_edge_pts_whale"]:
                return False, f"edge {edge:.1f} < {cfg['min_edge_pts_whale']}"
    elif source == "momentum":
        if not cfg.get("trade_momentum", False):
            return False, "momentum disabled"
        if not use_rules:
            if conf < cfg["min_confidence_momentum"]:
                return False, f"conf {conf:.1f} < {cfg['min_confidence_momentum']}"
            if edge < cfg["min_edge_pts_momentum"]:
                return False, f"edge {edge:.1f} < {cfg['min_edge_pts_momentum']}"
        sig_type = (signal.get("signal_type") or "")
        allowed = set(cfg.get("allowed_momentum_signal_types", []))
        if sig_type not in allowed:
            return False, f"signal_type {sig_type!r} not allowed"
    elif source == "convergence":
        if not cfg.get("trade_convergence", False):
            return False, "convergence disabled"
        if not use_rules and conf < cfg["min_confidence_whale"]:
            return False, f"conf {conf:.1f} < {cfg['min_confidence_whale']}"

    cat = (signal.get("category") or "").lower()
    allowed_cats = cfg.get("allowed_categories")
    if allowed_cats is not None:
        if not allowed_cats:
            return False, "no categories enabled"
        if cat not in {c.lower() for c in allowed_cats}:
            return False, f"category {cat!r} not in allowed set"

    src_key = {
        "whale": "allowed_whale_categories",
        "convergence": "allowed_whale_categories",
        "momentum": "allowed_momentum_categories",
    }.get(source)
    src_cats = cfg.get(src_key) if src_key else None
    if src_cats is not None:
        if not src_cats:
            return False, f"no {source} categories enabled"
        if cat not in {c.lower() for c in src_cats}:
            return False, f"category {cat!r} not in {source} set"

    max_res_days = int(cfg.get("max_resolution_days", 0) or 0)
    if max_res_days > 0:
        days = _days_until_close(signal.get("close_time") or "")
        if days is not None and days > max_res_days:
            return False, f"resolves in ~{days:.0f}d > max {max_res_days}d"

    if use_rules:
        return rules_engine.evaluate_rules(
            _signal_rule_values(signal, source), cfg.get("rules") or [])

    _, cost_cents = _signal_cost_cents(signal, source)
    if cost_cents < cfg["min_entry_price_cents"]:
        return False, f"entry {cost_cents}c < {cfg['min_entry_price_cents']}c"
    if cost_cents > cfg["max_entry_price_cents"]:
        return False, f"entry {cost_cents}c > {cfg['max_entry_price_cents']}c"
    return True, "ok"


def _today_pnl_balance_delta(env: str) -> float | None:
    with db.get_db() as conn:
        first_today = db.first_snapshot_of_today(conn, env)
        if not first_today:
            return None
        latest = db.latest_snapshot(conn, env)
        transfers = db.transfer_adjustment_today(conn, env)
    if not latest:
        return None
    today_baseline = float(first_today["total_usd"] or 0.0)
    today_total = float(latest["total_usd"] or 0.0)
    return today_total - today_baseline - transfers


_DAY_RISK_PERSIST_SEC = 180.0
_day_risk_breach: dict = {}


def _breach_persists(env: str, kind: str, breached: bool) -> bool:
    now = time.monotonic()
    key = (env, kind)
    if not breached:
        _day_risk_breach[key] = None
        return False
    if _day_risk_breach.get(key) is None:
        _day_risk_breach[key] = now
    return (now - _day_risk_breach[key]) >= _DAY_RISK_PERSIST_SEC


def _is_blocked_by_daily_risk(cfg: dict, env: str) -> tuple[bool, str]:
    pnl = _today_pnl_balance_delta(env)
    if pnl is None:
        return False, ""
    sl = float(cfg.get("stop_loss_on_day", 0))
    tp = float(cfg.get("take_profit_on_day", 0))
    sl_hit = _breach_persists(env, "sl", sl < 0 and pnl <= sl)
    tp_hit = _breach_persists(env, "tp", tp > 0 and pnl >= tp)
    if sl_hit:
        return True, f"daily stop-loss hit (today pnl=${pnl:+.2f}, limit=${sl:+.2f})"
    if tp_hit:
        return True, f"daily take-profit hit (today pnl=${pnl:+.2f}, target=${tp:+.2f})"
    return False, ""


def _lifetime_loss_tripped_main(cfg: dict, env: str) -> tuple[bool, str]:
    limit_usd = float(cfg.get("lifetime_loss_limit_usd") or 0.0)
    try:
        limit_pct = max(0.0, min(1.0, float(cfg.get("lifetime_loss_limit_pct", 0.0) or 0.0)))
    except (TypeError, ValueError):
        limit_pct = 0.0
    if limit_usd <= 0 and limit_pct <= 0:
        return False, ""
    with db.get_db() as conn:
        lifetime = db.lifetime_realized_pnl(conn, "main", env)
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
            f"main-engine lifetime loss limit reached — new entries paused; "
            f"adjust settings to resume (cumulative ${lifetime:+.2f}, "
            f"loss ${loss:.2f} >= cap ${cap:.2f})"
        )
    return False, ""


async def _maybe_flatten_on_daily_stop(cfg: dict, env: str) -> None:
    if not cfg.get("flatten_on_daily_stop") or not cfg.get("enable_trading"):
        return
    pnl = _today_pnl_balance_delta(env)
    if pnl is None:
        return
    sl = float(cfg.get("stop_loss_on_day", 0))
    if not _breach_persists(env, "sl", sl < 0 and pnl <= sl):
        return
    with db.get_db() as conn:
        n = conn.execute(
            """SELECT COUNT(*) FROM bot_positions
               WHERE resolved=0 AND status IN ('submitted','partial','filled')
                 AND network=?""",
            (env,),
        ).fetchone()[0]
    if not n:
        return
    logger.warning(
        f"[daily-stop] flatten_on_daily_stop ON and loss limit hit "
        f"(today ${pnl:+.2f} <= ${sl:+.2f}) — closing {n} open position(s)"
    )
    try:
        res = await flatten_open_positions(cfg)
        logger.warning(f"[daily-stop] flatten result: {res}")
    except Exception as e:
        logger.error(f"[daily-stop] flatten failed: {e}", exc_info=True)


_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _is_blocked_by_trading_hours(cfg: dict) -> tuple[bool, str]:
    if not cfg.get("trading_hours_enabled"):
        return False, ""
    from datetime import datetime, timedelta, timezone
    offset_min = int(cfg.get("trading_timezone_offset_min", 0) or 0)
    local_now = datetime.now(timezone.utc) + timedelta(minutes=offset_min)
    days = [d.lower() for d in (cfg.get("trading_days") or [])]
    weekday_short = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][local_now.weekday()]
    if weekday_short not in days:
        return True, f"trading-hours: {weekday_short} not in active days"
    try:
        s_h, s_m = (int(x) for x in str(cfg.get("trading_hours_start", "00:00")).split(":"))
        e_h, e_m = (int(x) for x in str(cfg.get("trading_hours_end", "23:59")).split(":"))
    except (ValueError, AttributeError):
        return False, ""
    cur_min = local_now.hour * 60 + local_now.minute
    start_min = s_h * 60 + s_m
    end_min = e_h * 60 + e_m
    in_window = (
        (start_min <= end_min and start_min <= cur_min <= end_min)
        or (start_min > end_min and (cur_min >= start_min or cur_min <= end_min))
    )
    if not in_window:
        return True, (
            f"trading-hours: outside window "
            f"({local_now.strftime('%H:%M')} not in "
            f"{s_h:02d}:{s_m:02d}-{e_h:02d}:{e_m:02d})"
        )
    return False, ""


async def execute_signal(
    signal: dict, source: str, cfg: dict, balance_usd: float
) -> dict | None:
    async with order_gate("main"):
        return await _execute_signal_locked(signal, source, cfg, balance_usd)


async def _execute_signal_locked(
    signal: dict, source: str, cfg: dict, balance_usd: float
) -> dict | None:
    direction, signal_cost_cents = _signal_cost_cents(signal, source)
    edge_pts = _compute_edge(signal, source)
    env = get_env()

    with db.get_db() as conn:
        open_count = db.count_open_bot_positions(conn, env)
        if open_count >= cfg["max_open_positions"]:
            logger.info(
                f"[skip] {signal['ticker']}: MAX_OPEN_POSITIONS "
                f"({open_count}/{cfg['max_open_positions']} open)"
            )
            return None
        if not cfg.get("unlimited_daily_new_positions"):
            today_count = db.count_new_positions_today(conn, env)
            daily_cap = int(cfg["max_daily_new_positions"])
            if today_count >= daily_cap:
                logger.info(
                    f"[skip] {signal['ticker']}: MAX_DAILY_NEW_POSITIONS "
                    f"({today_count}/{daily_cap} today). "
                    f"Toggle 'Unlimited daily new positions' in Settings → "
                    f"Concurrency to disable this cap."
                )
                return None
        per_event_cap = int(cfg.get("max_positions_per_event", 1) or 1)
        if db.count_positions_in_event(
            conn, signal.get("event_ticker") or "", env
        ) >= per_event_cap:
            logger.info(
                f"[skip] {signal['ticker']}: event already at its "
                f"max ({per_event_cap}) position(s)"
            )
            return None
        if db.exists_position_in_market(conn, signal["ticker"], direction, env):
            logger.info(f"[skip] {signal['ticker']}: market/side already open")
            return None
        exposure = db.current_total_exposure_usd(conn, env)
        filled_exposure = db.current_filled_exposure_usd(conn, env)

    submit_book: dict = {}
    limit_cents = await _compute_limit_price_cents(
        signal["ticker"], direction, signal_cost_cents, cfg, submit_book
    )
    hi = int(cfg["max_entry_price_cents"])
    lo = 1 if bool(cfg.get("use_rules")) else int(cfg["min_entry_price_cents"])
    if limit_cents > hi:
        logger.info(
            f"[skip] {signal['ticker']}: order price {limit_cents}c above your "
            f"max-entry cap {hi}c (favorite ran past the cap since the signal)"
        )
        return None
    if limit_cents < lo:
        logger.info(
            f"[skip] {signal['ticker']}: order price {limit_cents}c below your "
            f"min-entry {lo}c (book moved since signal)"
        )
        return None

    pending_notional = max(0.0, exposure - filled_exposure)
    total_bankroll = max(0.0, balance_usd) + max(0.0, filled_exposure)
    target_usd = _compute_target_usd(total_bankroll, edge_pts, limit_cents, cfg)
    max_exposure = total_bankroll * float(cfg["max_total_exposure_fraction"])
    target_usd = min(target_usd, max(0.0, max_exposure - exposure))
    reserve = total_bankroll * float(cfg["min_cash_reserve_fraction"])
    spendable_cash = max(0.0, balance_usd) - pending_notional
    target_usd = min(target_usd, max(0.0, spendable_cash - reserve))
    risk_ceiling_usd = min(
        float(cfg["hard_max_position_usd"]),
        max(0.0, max_exposure - exposure),
        max(0.0, spendable_cash - reserve),
    )

    if target_usd < 1.0:
        logger.info(f"[skip] {signal['ticker']}: size ${target_usd:.2f} < $1")
        return None

    contracts = max(1, int(target_usd * 100 // limit_cents))

    min_size = 0
    try:
        meta = await get_market_meta(signal["ticker"])
        if meta:
            min_size = int(math.ceil(float(meta.get("min_size") or 0)))
    except Exception as e:
        logger.debug(f"min_size lookup failed for {signal['ticker']}: {e}")
    if min_size > contracts:
        bumped_cost = min_size * limit_cents / 100.0
        if bumped_cost <= risk_ceiling_usd + 1e-9:
            logger.info(
                f"[{source}] {signal['ticker']}: sizing {contracts}->{min_size} "
                f"to meet market minimum (${bumped_cost:.2f})"
            )
            contracts = min_size
        else:
            logger.info(
                f"[skip] {signal['ticker']}: market minimum {min_size} @ "
                f"{limit_cents}c = ${bumped_cost:.2f} exceeds risk budget "
                f"${risk_ceiling_usd:.2f}"
            )
            return None

    expected_cost_usd = contracts * limit_cents / 100.0
    client_order_id = f"krypt-{source}-{signal['id']}-{uuid.uuid4().hex[:8]}"

    logger.info(
        f"[{source}] {signal['ticker']} {direction} x{contracts} @ {limit_cents}c "
        f"= ${expected_cost_usd:.2f} conf={signal.get('confidence',0):.1f} edge={edge_pts:.1f}"
    )

    row = {
        "signal_source": source,
        "signal_id": signal["id"],
        "ticker": signal["ticker"],
        "event_ticker": signal.get("event_ticker", ""),
        "title": signal.get("title", ""),
        "category": signal.get("category", ""),
        "direction": direction,
        "action": "buy",
        "target_contracts": contracts,
        "limit_price_cents": limit_cents,
        "filled_contracts": 0,
        "cost_usd": 0.0,
        "client_order_id": client_order_id,
        "order_id": None,
        "status": "submitted",
        "confidence": signal.get("confidence", 0.0),
        "edge_pts": edge_pts,
        "signal_price": (signal.get("price") or 0.0) * 100,
        "balance_before_usd": balance_usd,
        "network": env,
        "entry_maker": _is_maker_limit(limit_cents, submit_book.get("ask_cents")),
    }

    if not cfg.get("enable_trading"):
        return None

    try:
        resp = await place_limit_order(
            ticker=signal["ticker"],
            side=direction,
            action="buy",
            count=contracts,
            price_cents=limit_cents,
            client_order_id=client_order_id,
        )
    except PolymarketAPIError as e:
        row["status"] = "error"
        row["error"] = f"HTTP {e.status}: {str(e.body)[:200]}"
        logger.error(f"[ORDER-FAIL] {signal['ticker']}: {row['error']}")
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            db.log_event(conn, pid, "error", note=row["error"])
            return db.fetch_position_by_id(conn, pid)
    except Exception as e:
        row["status"] = "error"
        row["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"[ORDER-EXC] {signal['ticker']}: {row['error']}")
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            db.log_event(conn, pid, "error", note=row["error"])
            return db.fetch_position_by_id(conn, pid)

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    order_id = order.get("order_id") if isinstance(order, dict) else None
    row["order_id"] = order_id
    if str((order.get("status") if isinstance(order, dict) else "") or "").lower() == "matched":
        row["entry_maker"] = 0

    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, row)
        db.log_event(
            conn, pid, "placed", order_status=order.get("status"),
            note=f"order_id={order_id}",
        )
        return db.fetch_position_by_id(conn, pid)


ORDER_LOCK = asyncio.Lock()

ORDER_LOCK_TIMEOUT_SEC = 30.0


@contextlib.asynccontextmanager
async def order_gate(engine: str = ""):
    global ORDER_LOCK
    lock = ORDER_LOCK
    try:
        await asyncio.wait_for(lock.acquire(), timeout=ORDER_LOCK_TIMEOUT_SEC)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            f"[order-gate] {engine or 'engine'}: waited "
            f"{ORDER_LOCK_TIMEOUT_SEC:.0f}s for the cross-engine order lock — "
            "proceeding unserialised"
        )
        yield False
        return
    except RuntimeError:
        logger.debug("[order-gate] lock bound to a stale event loop — rebuilding")
        ORDER_LOCK = lock = asyncio.Lock()
        await lock.acquire()
    try:
        yield True
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


def shared_headroom_usd(cfg: dict, env: str, balance_usd: float) -> float:
    with db.get_db() as conn:
        exposure = db.current_total_exposure_usd(conn, env)
        filled_exposure = db.current_filled_exposure_usd(conn, env)
    pending_notional = max(0.0, exposure - filled_exposure)
    total_bankroll = max(0.0, balance_usd) + max(0.0, filled_exposure)
    reserve = total_bankroll * float(cfg.get("min_cash_reserve_fraction", 0.0) or 0.0)
    max_exposure = total_bankroll * float(
        cfg.get("max_total_exposure_fraction", 1.0) or 1.0
    )
    spendable_cash = max(0.0, balance_usd) - pending_notional
    return min(
        max(0.0, spendable_cash - reserve),
        max(0.0, max_exposure - exposure),
    )


def headroom_blocks(cfg: dict, env: str, balance_usd: float, cost_usd: float) -> str:
    if balance_usd <= 0 or cost_usd <= 0:
        return ""
    try:
        headroom = shared_headroom_usd(cfg, env, balance_usd)
    except Exception as e:
        logger.debug(f"[order-gate] headroom re-check failed: {e}")
        return ""
    if cost_usd <= headroom + 1e-9:
        return ""
    return (
        f"wallet-wide cap: ${cost_usd:.2f} exceeds the ${headroom:.2f} still "
        f"available across all engines (exposure/reserve caps)"
    )


def can_open_new_entries(env: str) -> tuple[bool, str]:
    try:
        addr = trading_address(env)
    except Exception:
        return True, ""
    if not addr:
        return True, ""
    if instance_lock.claim(addr):
        return True, ""
    other = instance_lock.foreign_holder(addr)
    return False, (
        f"wallet {addr[:6]}…{addr[-4:]} is already being traded by another "
        f"account/instance (pid {other}); pausing new entries to avoid double orders."
    )


async def scan_for_trades(cfg: dict) -> list[dict]:
    global _last_scan_skip_log
    now_ts = time.time()

    def _skip_log(reason: str) -> None:
        last_cycle["skipReason"] = reason
        last_cycle["at"] = time.time()
        key = reason.split(" (", 1)[0]
        last = _last_scan_skip_log.get(key, 0)
        if now_ts - last < 60:
            return
        _last_scan_skip_log[key] = now_ts
        logger.info(f"[skip-cycle] {reason}")

    if not cfg.get("enable_trading"):
        _skip_log("enable_trading=false (main-bot trading off in Settings)")
        return []

    env = get_env()
    blocked, reason = _is_blocked_by_daily_risk(cfg, env)
    if blocked:
        await _maybe_flatten_on_daily_stop(cfg, env)
        _skip_log(reason)
        return []
    tripped, reason = _lifetime_loss_tripped_main(cfg, env)
    if tripped:
        _skip_log(reason)
        return []
    ok, reason = can_open_new_entries(env)
    if not ok:
        _skip_log(reason)
        return []
    blocked, reason = _is_blocked_by_trading_hours(cfg)
    if blocked:
        _skip_log(reason)
        return []

    cents, _ = await refresh_balance(cfg)
    balance_usd = cents / 100.0
    if balance_usd < 5.0:
        _skip_log(f"balance ${balance_usd:.2f} too low")
        return []

    candidates: list[tuple[dict, str]] = []
    fetched_w = fetched_m = 0
    gate_resolution = int(cfg.get("max_resolution_days", 0) or 0) > 0
    with db.get_db() as conn:
        use_rules = bool(cfg.get("use_rules"))
        if cfg.get("trade_whales"):
            seen = db.already_traded_signal_ids(conn, "whale", env)
            for sig in db.fetch_tradeable_whale_signals(
                conn,
                min_confidence=0.0 if use_rules else float(cfg["min_confidence_whale"]),
                max_age_sec=int(cfg["max_signal_age_sec"]),
                seen_ids=seen,
            ):
                fetched_w += 1
                if gate_resolution:
                    _enrich_close_time(conn, sig)
                candidates.append((sig, "whale"))
        if cfg.get("trade_momentum"):
            seen = db.already_traded_signal_ids(conn, "momentum", env)
            for sig in db.fetch_tradeable_momentum_signals(
                conn,
                min_confidence=0.0 if use_rules else float(cfg["min_confidence_momentum"]),
                max_age_sec=int(cfg["max_signal_age_sec"]),
                allowed_types=list(cfg.get("allowed_momentum_signal_types", [])),
                seen_ids=seen,
            ):
                fetched_m += 1
                if gate_resolution:
                    _enrich_close_time(conn, sig)
                candidates.append((sig, "momentum"))

    if not candidates:
        bits = []
        if not cfg.get("trade_whales"):
            bits.append("whales OFF")
        if not cfg.get("trade_momentum"):
            bits.append("momentum OFF")
        if not bits:
            bits.append(
                f"no fresh signals match thresholds "
                f"(whale min_conf={cfg.get('min_confidence_whale')}, "
                f"momentum min_conf={cfg.get('min_confidence_momentum')}, "
                f"max_age={cfg.get('max_signal_age_sec')}s)"
            )
        _skip_log("no candidates: " + ", ".join(bits))
        return []

    candidates.sort(key=lambda c: _compute_edge(c[0], c[1]), reverse=True)
    await warm_books(
        (sig["ticker"], _signal_cost_cents(sig, src)[0]) for sig, src in candidates
    )
    inserted: list[dict] = []
    filter_counts: dict[str, int] = {}
    entries_placed = 0
    for sig, src in candidates:
        ok, why = should_trade(sig, src, cfg)
        if not ok:
            key = (int(sig.get("id") or 0), src)
            last = _last_filter_log.get(key, 0.0)
            if (now_ts - last) >= float(cfg.get("max_signal_age_sec", 120)):
                logger.info(f"[filter] {sig['ticker']} {src}: {why}")
                _last_filter_log[key] = now_ts
                cap_dict_size(_last_filter_log)
            filter_counts[why] = filter_counts.get(why, 0) + 1
            continue
        try:
            row = await execute_signal(sig, src, cfg, balance_usd)
            if row:
                inserted.append(row)
        except Exception as e:
            row = None
            logger.error(f"[exec-fail] {sig['ticker']} {src}: {e}", exc_info=True)
        if row and (row.get("status") or "") in ("submitted", "filled", "partial"):
            committed = float(row.get("cost_usd") or 0.0)
            if committed <= 0:
                committed = (int(row.get("target_contracts") or 0)
                             * int(row.get("limit_price_cents") or 0) / 100.0)
            balance_usd = max(0.0, balance_usd - committed)
            entries_placed += 1
        if balance_usd < 5.0:
            logger.info("[halt-cycle] balance now below $5")
            break

    if entries_placed:
        await refresh_balance(cfg, force=True)

    failed = sum(1 for r in inserted if (r.get("status") or "") == "error")
    placed = len(inserted) - failed
    last_cycle.update({
        "skipReason": None, "filterCounts": dict(filter_counts),
        "candidates": len(candidates), "placed": placed, "at": time.time(),
    })

    if candidates:
        rejected = sum(filter_counts.values())
        logger.info(
            f"[trade-cycle] candidates={len(candidates)} "
            f"(whale={fetched_w}, momentum={fetched_m}) "
            f"placed={placed} failed={failed} filtered={rejected}"
        )

    return inserted


_last_scan_skip_log: dict[str, float] = {}


def cap_dict_size(d: dict, cap: int = 2000) -> None:
    if len(d) > cap:
        for _k in list(d.keys())[: len(d) - cap]:
            d.pop(_k, None)

last_cycle: dict = {"skipReason": None, "filterCounts": {}, "candidates": 0, "placed": 0, "at": None}

_last_filter_log: dict[tuple[int, str], float] = {}

_last_skip_import_log: dict[tuple[str, str], float] = {}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_polymarket_order(order: dict) -> dict:
    filled = int(round(_f(order.get("size_matched"))))
    place_count = int(round(_f(order.get("original_size"))))
    price = _f(order.get("price"))
    cost_cents = int(round(filled * price * 100))
    raw_status = (order.get("status") or "").lower()
    if "match" in raw_status:
        status = "executed"
    elif "cancel" in raw_status or "invalid" in raw_status:
        status = "canceled"
    elif "live" in raw_status:
        status = "resting"
    else:
        status = raw_status or "resting"
    remaining = max(0, place_count - filled)
    avg_cents = (cost_cents / filled) if filled else None
    return {
        "filled": filled,
        "cost_cents": cost_cents,
        "avg_cents": avg_cents,
        "status": status,
        "place_count": place_count,
        "remaining": remaining,
    }


def _parse_polymarket_fill(f: dict, default_side: str = "") -> dict:
    count = int(round(_f(f.get("count_fp") if f.get("count") is None else f.get("count"))))
    side = str(f.get("side") or default_side).lower()
    cents = f.get("price_cents")
    if cents in (None, ""):
        frac = f.get("yes_price_dollars") if side == "yes" else f.get("no_price_dollars")
        cents = int(round(_f(frac) * 100)) if frac is not None else None
    try:
        price_cents: Optional[int] = int(cents) if cents not in (None, "") else None
    except (TypeError, ValueError):
        price_cents = None
    if price_cents is not None and not (1 <= price_cents <= 99):
        price_cents = None
    return {"count": count, "side": side, "price_cents": price_cents}


def _parse_polymarket_position(p: dict) -> dict:
    try:
        qty_signed = float(p.get("position_fp") or 0)
    except (TypeError, ValueError):
        qty_signed = 0.0
    filled = int(abs(round(qty_signed)))
    side = "yes" if qty_signed > 0 else ("no" if qty_signed < 0 else "")
    try:
        exposure_usd = float(p.get("market_exposure_dollars") or 0)
    except (TypeError, ValueError):
        exposure_usd = 0.0
    try:
        fees_usd = float(p.get("fees_paid_dollars") or 0)
    except (TypeError, ValueError):
        fees_usd = 0.0
    cost_cents = int(round(exposure_usd * 100))
    avg_cents = (cost_cents / filled) if filled else None
    return {
        "filled": filled,
        "cost_cents": cost_cents,
        "avg_cents": avg_cents,
        "fees_usd": fees_usd,
        "side": side,
        "status": "executed",
        "place_count": filled,
        "remaining": 0,
    }


def _db_status_from_order(parsed: dict, target: int) -> str:
    s = parsed["status"]
    filled = parsed["filled"]
    if s == "executed" or (filled >= target and target > 0):
        return "filled"
    if s in ("canceled", "cancelled"):
        return "canceled" if filled == 0 else "partial"
    if filled > 0:
        return "partial"
    if s == "resting":
        return "submitted"
    return "submitted"


_poll_failures: dict[int, int] = {}
_POLL_FAILURE_THRESHOLD = 6


async def poll_open_orders(cfg: dict) -> list[dict]:
    with db.get_db() as conn:
        pending = db.get_pending_bot_positions(conn)
    if not pending:
        return []

    pos_by_key: dict[tuple[str, str], dict] = {}
    try:
        live_positions = await get_positions(limit=1000)
        for lp in live_positions:
            qty = 0.0
            for k in ("position_fp", "position"):
                v = lp.get(k)
                if v in (None, "", 0):
                    continue
                try:
                    f = float(v)
                    if f != 0:
                        qty = f
                        break
                except (TypeError, ValueError):
                    pass
            if qty == 0:
                continue
            tkr = lp.get("ticker") or ""
            side = "yes" if qty > 0 else "no"
            pos_by_key[(tkr, side)] = lp
    except Exception as e:
        logger.debug(f"poll: get_positions for rescue failed: {e}")

    updated: list[dict] = []

    def _bump_failure(pid: int) -> int:
        n = _poll_failures.get(pid, 0) + 1
        _poll_failures[pid] = n
        cap_dict_size(_poll_failures)
        return n

    def _clear_failure(pid: int) -> None:
        _poll_failures.pop(pid, None)

    def _try_rescue_from_position_aggregate(pos: dict, conn) -> dict | None:
        live_p = pos_by_key.get((pos["ticker"], pos["direction"]))
        if not live_p:
            return None
        siblings = conn.execute(
            """SELECT id FROM bot_positions
                WHERE ticker = ? AND direction = ?
                  AND network = ?
                  AND resolved = 0
                  AND status NOT IN ('gone','canceled','expired','error','dry_run')""",
            (pos["ticker"], pos["direction"], pos["network"]),
        ).fetchall()
        if len(siblings) != 1:
            return None
        qty = 0.0
        for k in ("position_fp", "position"):
            v = live_p.get(k)
            if v in (None, "", 0):
                continue
            try:
                f = float(v)
                if f != 0:
                    qty = abs(f)
                    break
            except (TypeError, ValueError):
                pass
        cost_cents = 0.0
        v = live_p.get("market_exposure")
        if v not in (None, "", 0):
            try:
                cost_cents = abs(float(v))
            except (TypeError, ValueError):
                pass
        if cost_cents == 0:
            v = live_p.get("market_exposure_dollars")
            if v not in (None, ""):
                try:
                    cost_cents = abs(float(v) * 100.0)
                except (TypeError, ValueError):
                    pass
        if qty <= 0:
            return None
        local_cost_usd = float(pos.get("cost_usd") or 0.0)
        local_avg_cents = pos.get("avg_fill_price_cents")
        new_cost_usd = (
            cost_cents / 100.0 if cost_cents > 0 else local_cost_usd
        )
        new_avg_cents = (
            (cost_cents / qty) if (qty and cost_cents > 0)
            else local_avg_cents
        )
        new_filled = int(round(qty))

        cur_filled = int(pos.get("filled_contracts") or 0)
        cur_cost_usd = float(pos.get("cost_usd") or 0.0)
        if (
            pos.get("status") == "filled"
            and cur_filled == new_filled
            and abs(cur_cost_usd - new_cost_usd) < 0.005
        ):
            return None

        db.update_bot_position(
            conn, pos["id"], status="filled",
            filled_contracts=new_filled,
            cost_usd=new_cost_usd,
            avg_fill_price_cents=new_avg_cents,
        )
        db.log_event(
            conn, pos["id"], "poll",
            note="rescued via /portfolio/positions",
        )
        row = db.fetch_position_by_id(conn, pos["id"])
        conn.commit()
        return row

    _order_sem = asyncio.Semaphore(8)

    async def _poll_one(pid, kid: str):
        async with _order_sem:
            try:
                return pid, await get_order(kid)
            except Exception as e:
                return pid, e

    _to_poll = [
        (p["id"], p.get("order_id")) for p in pending
        if p.get("order_id") and p["status"] not in ("dry_run", "filled")
    ]
    order_resps: dict = dict(
        await asyncio.gather(*[_poll_one(pid, kid) for pid, kid in _to_poll])
    )

    expired: list[tuple[int, str, float]] = []
    with db.get_db() as conn:
        for pos in pending:
            kid = pos.get("order_id")
            if pos["status"] == "dry_run":
                continue

            if pos["status"] == "filled":
                row = _try_rescue_from_position_aggregate(pos, conn)
                if row:
                    _clear_failure(pos["id"])
                    updated.append(row)
                else:
                    _clear_failure(pos["id"])
                continue

            if not kid:
                row = _try_rescue_from_position_aggregate(pos, conn)
                if row:
                    _clear_failure(pos["id"])
                    updated.append(row)
                    continue
                n = _bump_failure(pos["id"])
                if n >= _POLL_FAILURE_THRESHOLD:
                    db.update_bot_position(
                        conn, pos["id"], status="gone",
                        error=f"missing order_id ({n} cycles)",
                    )
                    db.log_event(conn, pos["id"], "poll", note=f"no id × {n} -> gone")
                    row = db.fetch_position_by_id(conn, pos["id"])
                    conn.commit()
                    if row:
                        updated.append(row)
                    _clear_failure(pos["id"])
                continue

            try:
                if pos["id"] in order_resps:
                    resp = order_resps[pos["id"]]
                    if isinstance(resp, Exception):
                        raise resp
                else:
                    logger.debug(f"order poll {kid}: not prefetched, retrying next cycle")
                    continue
                order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
                parsed = _parse_polymarket_order(order)
            except PolymarketAPIError as e:
                row = _try_rescue_from_position_aggregate(pos, conn)
                if row:
                    _clear_failure(pos["id"])
                    updated.append(row)
                    continue
                n = _bump_failure(pos["id"])
                logger.debug(
                    f"order poll {kid}: HTTP {e.status} (failure #{n})"
                )
                if e.status == 404 and n >= _POLL_FAILURE_THRESHOLD:
                    db.update_bot_position(
                        conn, pos["id"], status="gone",
                        error=f"order 404 × {n}",
                    )
                    db.log_event(conn, pos["id"], "poll", note=f"404 × {n} -> gone")
                    row = db.fetch_position_by_id(conn, pos["id"])
                    conn.commit()
                    if row:
                        updated.append(row)
                    _clear_failure(pos["id"])
                continue
            except Exception as e:
                _bump_failure(pos["id"])
                logger.warning(f"order poll {kid}: {e}")
                continue

            _clear_failure(pos["id"])

            db_status = _db_status_from_order(parsed, pos["target_contracts"])
            if db_status == "filled" and parsed["filled"] == 0:
                rescued = _try_rescue_from_position_aggregate(pos, conn)
                if rescued:
                    updated.append(rescued)
                    continue
                db_status = "gone"

            fields: dict = {
                "status": db_status,
                "filled_contracts": parsed["filled"],
            }
            if db_status == "partial" and parsed["status"] in ("canceled", "cancelled"):
                fields["target_contracts"] = parsed["filled"]
            if parsed["avg_cents"] is not None:
                fields["avg_fill_price_cents"] = parsed["avg_cents"]
            if parsed["cost_cents"]:
                fields["cost_usd"] = parsed["cost_cents"] / 100.0
            if parsed.get("fees_usd"):
                fields["fees_usd"] = parsed["fees_usd"]
            elif parsed["filled"] and parsed["avg_cents"] is not None:
                fields["fees_usd"] = _position_fee_usd({
                    **pos,
                    "filled_contracts": parsed["filled"],
                    "avg_fill_price_cents": parsed["avg_cents"],
                    "fees_usd": 0.0,
                })
            db.update_bot_position(conn, pos["id"], **fields)
            db.log_event(
                conn, pos["id"], "poll",
                order_status=parsed["status"],
                filled_contracts=parsed["filled"],
                fill_cost_cents=parsed["cost_cents"] or None,
            )
            row = db.fetch_position_by_id(conn, pos["id"])
            conn.commit()
            if row:
                updated.append(row)

            if (
                db_status == "submitted"
                and cfg.get("order_expiration_sec") is not None
                and parsed["filled"] == 0
                and kid
            ):
                age = conn.execute(
                    "SELECT (julianday('now')-julianday(created_at))*86400 "
                    "FROM bot_positions WHERE id=?", (pos["id"],),
                ).fetchone()
                age_sec = float(age[0]) if age and age[0] is not None else 0.0
                if age_sec > float(cfg["order_expiration_sec"]):
                    expired.append((int(pos["id"]), str(kid), age_sec))

    for pid, kid, age_sec in expired:
        try:
            await cancel_order(kid)
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, pid, status="canceled",
                    error=f"auto-canceled after {age_sec:.0f}s",
                )
                db.log_event(conn, pid, "cancel", note=f"age {age_sec:.0f}s")
                r = db.fetch_position_by_id(conn, pid)
            if r:
                updated.append(r)
        except PolymarketAPIError as e:
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, pid, status="gone",
                    error=f"cancel 404: {str(e.body)[:100]}",
                )
                db.log_event(conn, pid, "cancel", note="404 -> gone")
                r = db.fetch_position_by_id(conn, pid)
            if r:
                updated.append(r)
        except Exception as e:
            logger.warning(f"cancel exception {kid}: {e}")

    return updated


def _market_yes_payout(market: dict | None) -> Optional[float]:
    if not market:
        return None
    result = (market.get("result") or "").lower()
    if result == "yes":
        return 1.0
    if result == "no":
        return 0.0
    status = (market.get("status") or "").lower()
    if status not in ("settled", "finalized", "determined"):
        return None
    sv = market.get("settlement_value_dollars")
    if sv is None:
        sv = market.get("settlement_value")
    if sv is None:
        return None
    try:
        f = float(sv)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= f <= 1.0):
        logger.warning(f"[settle] ignoring out-of-range settlement_value={sv!r}")
        return None
    return f


_resolve_retry_at: dict[int, float] = {}
_RESOLVE_SLOW_AGE_SEC = 7 * 86400
_RESOLVE_SLOW_RETRY_SEC = 6 * 3600


def _row_age_sec(created_at) -> float:
    try:
        dt = datetime.fromisoformat(str(created_at)).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _fee_coeff_for(pos: dict) -> Optional[float]:
    cat = (pos.get("category") or "").strip().lower()
    if cat in backtest.CATEGORY_TAKER_FEE:
        return None
    return backtest.CATEGORY_TAKER_FEE["other"]


def _position_fee_usd(pos: dict) -> float:
    if pos.get("entry_maker"):
        return 0.0
    reported = float(pos.get("fees_usd") or 0.0)
    if reported > 0:
        return reported
    filled = int(pos.get("filled_contracts") or 0)
    if filled <= 0:
        return 0.0
    avg_c = pos.get("avg_fill_price_cents")
    cost = float(pos.get("cost_usd") or 0.0)
    price = (float(avg_c) / 100.0) if avg_c else (cost / filled if cost > 0 else 0.0)
    if not (0.0 < price < 1.0):
        return 0.0
    return round(
        backtest.polymarket_fee_per_contract(
            price, _fee_coeff_for(pos),
            category=pos.get("category") or "") * filled, 6)


def _sell_fee_usd(pos: dict, sold: int, price_cents: float | None) -> float:
    if not sold or not price_cents:
        return 0.0
    p = max(0.0, min(1.0, float(price_cents) / 100.0))
    return round(
        backtest.polymarket_fee_per_contract(
            p, _fee_coeff_for(pos),
            category=pos.get("category") or "") * int(sold), 6)


async def mark_resolved_positions(cfg: dict) -> list[dict]:
    with db.get_db() as conn:
        unresolved = db.get_unresolved_bot_positions(conn)
    if not unresolved:
        return []

    env = get_env()
    by_env = [p for p in unresolved if p["network"] == env]
    if not by_env:
        return []

    updated: list[dict] = []
    needs_market: list[dict] = []
    for pos in by_env:
        filled = int(pos["filled_contracts"] or 0)
        if filled <= 0:
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, pos["id"], resolved=1, outcome_correct=None,
                    pnl_usd=0.0, settlement_usd=0.0,
                )
                conn.execute(
                    "UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?",
                    (pos["id"],),
                )
                db.log_event(conn, pos["id"], "resolve", note="no fill -> closed")
                r = db.fetch_position_by_id(conn, pos["id"])
            if r:
                updated.append(r)
            continue
        if time.monotonic() < _resolve_retry_at.get(pos["id"], 0.0):
            continue
        needs_market.append(pos)

    if not needs_market:
        return updated

    market_cache = await fetch_markets_map(list({p["ticker"] for p in needs_market}))
    if market_cache:
        try:
            with db.get_db() as conn:
                for ticker, m in market_cache.items():
                    db.upsert_market(
                        conn,
                        {
                            "ticker": ticker,
                            "event_ticker": m.get("event_ticker", ""),
                            "title": m.get("title", ""),
                            "yes_sub_title": m.get("yes_sub_title", ""),
                            "status": m.get("status", ""),
                            "close_time": m.get("close_time", ""),
                            "volume": m.get("volume_fp", 0),
                            "volume_24h": m.get("volume_24h_fp", 0),
                            "open_interest": m.get("open_interest_fp", 0),
                            "yes_bid": m.get("yes_bid_dollars", 0),
                            "yes_ask": m.get("yes_ask_dollars", 0),
                            "last_price": m.get("last_price_dollars", 0),
                            "result": m.get("result", ""),
                            "settlement_value": m.get("settlement_value_dollars"),
                        },
                    )
        except Exception:
            pass

    for pos in needs_market:
        market = market_cache.get(pos["ticker"])
        yes_payout = _market_yes_payout(market)
        if yes_payout is None:
            if _row_age_sec(pos.get("created_at")) > _RESOLVE_SLOW_AGE_SEC:
                _resolve_retry_at[pos["id"]] = time.monotonic() + _RESOLVE_SLOW_RETRY_SEC
                cap_dict_size(_resolve_retry_at)
            continue

        direction = pos["direction"]
        filled = int(pos["filled_contracts"] or 0)
        cost_usd = float(pos["cost_usd"] or 0.0)
        fee_usd = _position_fee_usd(pos)
        our_payout = yes_payout if direction == "yes" else (1.0 - yes_payout)
        settlement_usd = filled * our_payout
        pnl_usd = settlement_usd - cost_usd - fee_usd

        max_settlement = float(filled)
        max_pnl = max_settlement - cost_usd - fee_usd
        min_pnl = -cost_usd - fee_usd
        if pnl_usd > max_pnl + 0.01 or pnl_usd < min_pnl - 0.01:
            logger.warning(
                f"[resolve-clamp] {pos['ticker']} pnl=${pnl_usd:+.2f} "
                f"outside physical bounds (filled={filled}, cost=${cost_usd:.2f}, "
                f"settlement=${settlement_usd:.2f}); clamping. "
                f"This usually means filled_contracts or cost_usd is corrupt — "
                f"run Reconcile Fills."
            )
            pnl_usd = max(min_pnl, min(max_pnl, pnl_usd))
            settlement_usd = pnl_usd + cost_usd + fee_usd

        if our_payout >= 0.99:
            correct: Optional[int] = 1
        elif our_payout <= 0.01:
            correct = 0
        elif pnl_usd > 0.05:
            correct = 1
        elif pnl_usd < -0.05:
            correct = 0
        else:
            correct = None

        with db.get_db() as conn:
            fields = dict(resolved=1, outcome_correct=correct,
                          settlement_usd=settlement_usd, pnl_usd=pnl_usd)
            if fee_usd > 0 and not float(pos.get("fees_usd") or 0.0):
                fields["fees_usd"] = fee_usd
            db.update_bot_position(conn, pos["id"], **fields)
            conn.execute(
                "UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?",
                (pos["id"],),
            )
            label = "WIN" if correct == 1 else ("LOSS" if correct == 0 else "CLOSED")
            db.log_event(
                conn, pos["id"], "resolve",
                note=f"{label} pnl=${pnl_usd:+.2f} ({filled}c @ {our_payout:.2f})",
            )
            r = db.fetch_position_by_id(conn, pos["id"])
        if r:
            updated.append(r)
            logger.info(
                f"[resolve] {pos['ticker']} {direction}: {label} "
                f"pnl=${pnl_usd:+.2f} ({filled}c @ {our_payout:.2f}, "
                f"cost=${cost_usd:.2f})"
            )

    return updated


async def reconcile_fills_from_polymarket() -> dict:
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, order_id, ticker, direction, status
               FROM bot_positions
               WHERE network=? AND order_id IS NOT NULL
                 AND status NOT IN ('dry_run','submitted')""",
            (env,),
        ).fetchall()
        targets = [dict(r) for r in rows]

    fills_reconciled = 0
    for pos in targets:
        kid = pos["order_id"]
        try:
            fills = await get_fills_for_order(kid)
        except Exception as e:
            logger.debug(f"fills fetch failed for {kid}: {e}")
            continue

        ours = [f for f in fills if str(f.get("order_id") or "") == str(kid)]
        if not ours and fills:
            logger.warning(
                f"[reconcile] /portfolio/fills returned {len(fills)} fills "
                f"but none matched order_id={kid} — Polymarket may have rotated "
                f"old fills out. Skipping this position."
            )
            continue

        ours = [
            f for f in ours
            if str(f.get("action") or "buy").lower() == "buy"
        ]

        total_filled = 0
        total_cost_cents = 0
        for f in ours:
            fp = _parse_polymarket_fill(f, default_side=pos["direction"])
            n = fp["count"]
            if n <= 0:
                continue
            price_cents = fp["price_cents"]
            if price_cents is None:
                logger.warning(
                    f"[reconcile] unreadable price on fill order={kid} "
                    f"side={fp['side']}; skipping fill"
                )
                continue
            total_filled += n
            total_cost_cents += n * price_cents

        if total_filled == 0 and total_cost_cents == 0:
            continue

        avg_cents = (total_cost_cents / total_filled) if total_filled else None
        with db.get_db() as conn:
            db.update_bot_position(
                conn, pos["id"],
                filled_contracts=total_filled,
                cost_usd=total_cost_cents / 100.0,
                avg_fill_price_cents=avg_cents,
            )
            db.log_event(
                conn, pos["id"], "reconcile-fills",
                filled_contracts=total_filled,
                fill_cost_cents=total_cost_cents,
            )
        fills_reconciled += 1

    with db.get_db() as conn:
        cleared = conn.execute(
            """UPDATE bot_positions
               SET resolved=0, outcome_correct=NULL,
                   pnl_usd=NULL, settlement_usd=NULL,
                   resolved_at=NULL
               WHERE network=?
                 AND status IN ('filled','partial','expired','canceled','gone','error')
                 AND resolved=1
                 AND COALESCE(closed_early,0)=0""",
            (env,),
        ).rowcount
    resolved = await mark_resolved_positions({})
    return {
        "fills_reconciled": fills_reconciled,
        "pnl_cleared": int(cleared or 0),
        "pnl_recomputed": len(resolved),
    }


async def audit_pnl(limit: int = 200) -> dict:
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, direction, filled_contracts, cost_usd,
                      pnl_usd, settlement_usd, outcome_correct,
                      category, fees_usd, avg_fill_price_cents, entry_maker
               FROM bot_positions
               WHERE network=? AND resolved=1
               ORDER BY resolved_at DESC LIMIT ?""",
            (env, int(limit)),
        ).fetchall()
        rows = [dict(r) for r in rows]

    flagged: list[dict] = []
    sum_stored = 0.0
    sum_recompute = 0.0
    for r in rows:
        market = await fetch_market(r["ticker"])
        yes_payout = _market_yes_payout(market)
        if yes_payout is None:
            continue
        direction = r["direction"]
        filled = int(r["filled_contracts"] or 0)
        cost = float(r["cost_usd"] or 0)
        our = yes_payout if direction == "yes" else (1.0 - yes_payout)
        settle = filled * our
        fee = _position_fee_usd(r)
        pnl_fresh = settle - cost - fee
        pnl_fresh = max(-cost - fee, min(filled - cost - fee, pnl_fresh))
        pnl_stored = float(r["pnl_usd"] or 0)
        sum_stored += pnl_stored
        sum_recompute += pnl_fresh
        if abs(pnl_stored - pnl_fresh) > 0.05:
            flagged.append({
                "id": r["id"],
                "ticker": r["ticker"],
                "direction": direction,
                "filled": filled,
                "cost": round(cost, 2),
                "settlement_payout": round(our, 2),
                "stored_pnl": round(pnl_stored, 2),
                "fresh_pnl": round(pnl_fresh, 2),
                "delta": round(pnl_fresh - pnl_stored, 2),
            })
    return {
        "checked": len(rows),
        "flagged": len(flagged),
        "sum_stored_pnl": round(sum_stored, 2),
        "sum_recompute_pnl": round(sum_recompute, 2),
        "delta": round(sum_recompute - sum_stored, 2),
        "samples": flagged[:20],
    }


async def recompute_pnl_from_polymarket() -> dict:
    env = get_env()
    with db.get_db() as conn:
        cleared = conn.execute(
            """UPDATE bot_positions
               SET resolved=0, outcome_correct=NULL,
                   pnl_usd=NULL, settlement_usd=NULL,
                   resolved_at=NULL
               WHERE network=?
                 AND status IN ('filled','partial','expired','canceled','gone','error')
                 AND resolved=1
                 AND COALESCE(closed_early,0)=0""",
            (env,),
        ).rowcount
    rows = await mark_resolved_positions({})
    return {"cleared": int(cleared or 0), "recomputed": len(rows)}


async def reconcile_positions_with_polymarket() -> tuple[dict, list[dict]]:
    summary = {
        "closed_orphans": 0,
        "imported_unknowns": 0,
        "rescued": 0,
        "resurrected": 0,
    }
    changed: list[dict] = []
    env = get_env()

    try:
        live = await get_positions(limit=1000)
    except Exception as e:
        logger.warning(f"reconcile: get_positions failed: {e}")
        return summary, changed

    def _signed_qty(p: dict) -> float:
        for k in ("position_fp", "position"):
            v = p.get(k)
            if v in (None, "", 0):
                continue
            try:
                f = float(v)
                if f != 0:
                    return f
            except (TypeError, ValueError):
                pass
        return 0.0

    def _cost_cents(p: dict) -> float:
        v = p.get("market_exposure")
        if v not in (None, "", 0):
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                pass
        v = p.get("market_exposure_dollars")
        if v not in (None, ""):
            try:
                return abs(float(v) * 100.0)
            except (TypeError, ValueError):
                pass
        return 0.0

    def _cur_price_cents(p: dict) -> Optional[float]:
        try:
            f = float(p.get("cur_price"))
        except (TypeError, ValueError):
            return None
        if not (0.0 < f <= 1.0):
            return None
        return round(f * 100.0, 2)

    nonzero_count = sum(1 for p in live if _signed_qty(p) != 0)
    if live and nonzero_count == 0:
        sample_keys = list(live[0].keys())
        logger.warning(
            f"reconcile: Polymarket returned {len(live)} positions but ZERO "
            f"have non-zero qty — likely a field-name change. "
            f"sample keys = {sample_keys}"
        )
    elif live:
        logger.debug(
            f"reconcile: Polymarket returned {len(live)} positions, "
            f"{nonzero_count} with non-zero qty"
        )

    live_by_key: dict[tuple[str, str], dict] = {}
    for p in live:
        qty = _signed_qty(p)
        if qty == 0:
            continue
        side = "yes" if qty > 0 else "no"
        live_by_key[(p.get("ticker") or "", side)] = p

    with db.get_db() as conn:
        all_local = conn.execute(
            "SELECT * FROM bot_positions WHERE network = ?", (env,),
        ).fetchall()
        crypto15m_keys = db.crypto15m_owned_keys(conn, env)
    local_by_key: dict[tuple[str, str], list] = {}
    for r in all_local:
        pos = dict(r)
        if pos.get("resolved"):
            continue
        if pos.get("status") == "dry_run":
            continue
        local_by_key.setdefault(
            (pos["ticker"], pos["direction"]), [],
        ).append(pos)

    for (ticker, side), live_p in live_by_key.items():
        rows = local_by_key.get((ticker, side), [])
        qty = abs(_signed_qty(live_p))
        cost_cents = _cost_cents(live_p)
        if qty <= 0:
            continue

        siblings = len(rows) + (1 if (ticker, side) in crypto15m_keys else 0)
        if siblings > 1:
            key = (ticker, side)
            last = _last_skip_import_log.get(key, 0.0)
            if (time.time() - last) >= 600:
                logger.info(
                    f"[reconcile] skip-merge {ticker} {side}: {siblings} local rows "
                    f"share this market (multi-engine) — leaving each to self-manage"
                )
                _last_skip_import_log[key] = time.time()
                cap_dict_size(_last_skip_import_log)
            continue

        if rows:
            active = next(
                (r for r in rows if r["status"] in ("submitted", "partial", "filled")),
                None,
            )
            target = active or rows[0]
            was_terminal = target["status"] in (
                "gone", "canceled", "expired", "error",
            )
            local_cost_usd = float(target.get("cost_usd") or 0.0)
            local_avg_cents = target.get("avg_fill_price_cents")
            new_cost_usd = (
                cost_cents / 100.0 if cost_cents > 0 else local_cost_usd
            )
            new_avg_cents = (
                (cost_cents / qty) if (qty and cost_cents > 0)
                else local_avg_cents
            )
            new_filled = int(round(qty))

            cur_price = _cur_price_cents(live_p)
            cur_mark = target.get("mark_price_cents")
            mark_moved = (
                cur_price is not None
                and (cur_mark is None or abs(float(cur_mark) - cur_price) >= 1.0)
            )

            cur_filled = int(target.get("filled_contracts") or 0)
            cur_cost_usd = float(target.get("cost_usd") or 0.0)
            if (
                not was_terminal
                and target.get("status") == "filled"
                and cur_filled == new_filled
                and abs(cur_cost_usd - new_cost_usd) < 0.005
                and not mark_moved
            ):
                continue

            with db.get_db() as conn:
                db.update_bot_position(
                    conn, target["id"], status="filled",
                    filled_contracts=new_filled,
                    cost_usd=new_cost_usd,
                    avg_fill_price_cents=new_avg_cents,
                    error=None if was_terminal else target.get("error"),
                    **({"mark_price_cents": cur_price}
                       if cur_price is not None else {}),
                )
                db.log_event(
                    conn, target["id"], "reconcile",
                    note=(
                        "resurrected from "
                        f"{target['status']} via /portfolio/positions"
                    ) if was_terminal else "rescued via /portfolio/positions",
                )
                row = db.fetch_position_by_id(conn, target["id"])
            if row:
                changed.append(row)
            if was_terminal:
                summary["resurrected"] += 1
                logger.info(
                    f"[reconcile] RESURRECTED #{target['id']} "
                    f"({ticker} {side}, was {target['status']}) "
                    f"qty={qty:.0f} cost=${cost_cents / 100:.2f}"
                )
            else:
                summary["rescued"] += 1
        else:
            if (ticker, side) in crypto15m_keys:
                key = (ticker, side)
                last = _last_skip_import_log.get(key, 0.0)
                if (time.time() - last) >= 600:
                    logger.info(
                        f"[reconcile] skip-import {ticker} {side}: owned by the "
                        f"crypto15m engine (not a main-bot position)"
                    )
                    _last_skip_import_log[key] = time.time()
                    cap_dict_size(_last_skip_import_log)
                continue
            with db.get_db() as conn:
                _mrow = db.get_market(conn, ticker)
            settled = _mrow is not None and _market_yes_payout(_mrow) is not None
            if not settled:
                try:
                    _mkt = await fetch_market(ticker)
                    settled = _market_yes_payout(_mkt) is not None
                except Exception:
                    continue
            if settled:
                key = (ticker, side)
                last = _last_skip_import_log.get(key, 0.0)
                if (time.time() - last) >= 600:
                    logger.info(
                        f"[reconcile] skip-import {ticker} {side}: market "
                        f"already settled (residual holding, not a live bet)"
                    )
                    _last_skip_import_log[key] = time.time()
                    cap_dict_size(_last_skip_import_log)
                continue
            with db.get_db() as conn:
                if db.recent_resolved_position_exists(
                    conn, ticker, side, env,
                ):
                    key = (ticker, side)
                    last = _last_skip_import_log.get(key, 0.0)
                    if (time.time() - last) >= 600:
                        logger.info(
                            f"[reconcile] skip-import {ticker} {side}: "
                            f"already resolved within 24h "
                            f"(Polymarket cash-settlement still pending)"
                        )
                        _last_skip_import_log[key] = time.time()
                        cap_dict_size(_last_skip_import_log)
                    continue
            now_ms = int(time.time() * 1000)
            signal_id = (
                abs(hash((ticker, side, now_ms))) % 2_000_000_000
            )
            client_id = f"ext-{ticker}-{side}-{now_ms}"
            try:
                with db.get_db() as conn:
                    new_id = db.insert_bot_position(conn, {
                        "signal_source": "external",
                        "signal_id": signal_id,
                        "ticker": ticker,
                        "event_ticker": live_p.get("event_ticker") or "",
                        "title": live_p.get("title") or ticker,
                        "category": live_p.get("category") or "",
                        "direction": side,
                        "action": "buy",
                        "target_contracts": int(round(qty)),
                        "limit_price_cents": (
                            int(round(cost_cents / qty)) if qty else 0
                        ),
                        "filled_contracts": int(round(qty)),
                        "avg_fill_price_cents": (
                            (cost_cents / qty) if qty else None
                        ),
                        "cost_usd": cost_cents / 100.0,
                        "client_order_id": client_id,
                        "order_id": None,
                        "status": "filled",
                        "confidence": 0.0,
                        "edge_pts": 0.0,
                        "signal_price": 0.0,
                        "network": env,
                    })
                    _mark = _cur_price_cents(live_p)
                    if _mark is not None:
                        db.update_bot_position(
                            conn, new_id, mark_price_cents=_mark,
                            _stamp_last_updated=False,
                        )
                    row = db.fetch_position_by_id(conn, new_id)
                if row:
                    changed.append(row)
                summary["imported_unknowns"] += 1
                logger.info(
                    f"[reconcile] imported external #{new_id} "
                    f"{ticker} {side} qty={qty:.0f} "
                    f"cost=${cost_cents / 100:.2f}"
                )
            except Exception as e:
                logger.warning(
                    f"[reconcile] import failed for {ticker} {side}: {e}",
                )

    return summary, changed


def _created_epoch(ts) -> float:
    if not ts:
        return 0.0
    s = str(ts).replace("T", " ").split(".")[0].replace("Z", "").strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _exit_proceeds(pos: dict, activity: list[dict]) -> Optional[dict]:
    ticker = pos.get("ticker")
    want_index = 0 if pos.get("direction") == "yes" else 1
    since = _created_epoch(pos.get("created_at")) - 3600.0
    sell_usdc = 0.0
    redeem_usdc = 0.0
    redeemed_ours = False
    for ev in activity:
        if (ev.get("conditionId") or "") != ticker:
            continue
        try:
            ts = float(ev.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts < since:
            continue
        idx = ev.get("outcomeIndex")
        try:
            idx = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx = None
        if idx is not None and idx != want_index:
            continue
        etype = (ev.get("type") or "").upper()
        try:
            usdc = float(ev.get("usdcSize") or 0.0)
        except (TypeError, ValueError):
            usdc = 0.0
        if etype == "TRADE" and (ev.get("side") or "").upper() == "SELL":
            sell_usdc += usdc
        elif etype == "REDEEM":
            redeem_usdc += usdc
            if idx == want_index:
                redeemed_ours = True
    total = sell_usdc + redeem_usdc
    if total <= 0:
        if redeemed_ours:
            return {"proceeds": 0.0, "kind": "redeemed"}
        return None
    return {"proceeds": total, "kind": "redeemed" if redeem_usdc >= sell_usdc else "sold"}


def _exit_was_sell(info) -> bool:
    return str((info or {}).get("kind") or "").lower() in ("sold", "sell")


async def detect_external_exits(cfg: dict) -> list[dict]:
    env = get_env()
    try:
        live = await get_positions()
    except Exception as e:
        logger.debug(f"[exits] positions fetch failed: {e}")
        return []
    held_keys: set = set()
    for p in live:
        try:
            qty = float(p.get("position_fp") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            continue
        held_keys.add((p.get("ticker") or "", "yes" if qty > 0 else "no"))
    if not held_keys:
        logger.debug("[exits] no live holdings returned — skipping exit sweep (guard)")
        return []

    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bot_positions WHERE network=? AND resolved=0 "
            "AND status IN ('filled','partial')",
            (env,),
        ).fetchall()]
        crypto15m_keys = db.crypto15m_owned_keys(conn, env)

    by_key: dict[tuple[str, str], list] = {}
    for r in rows:
        by_key.setdefault((r["ticker"], r["direction"]), []).append(r)

    candidates: list[dict] = []
    for key, group in by_key.items():
        if key in held_keys or key in crypto15m_keys:
            continue
        if len(group) != 1:
            logger.info(
                f"[exits] skip {key[0]} {key[1]}: {len(group)} local rows share this "
                f"market — can't attribute one wallet exit; review manually"
            )
            continue
        candidates.append(group[0])
    if not candidates:
        return []

    try:
        activity = await get_activity(limit=1000)
    except Exception as e:
        logger.debug(f"[exits] activity fetch failed: {e}")
        return []

    changed: list[dict] = []
    for pos in candidates:
        info = _exit_proceeds(pos, activity)
        if info is None:
            logger.info(
                f"[exits] {pos['ticker']} {pos['direction']} no longer held but no "
                f"SELL/REDEEM found in recent activity — leaving open for review"
            )
            continue
        cost = float(pos.get("cost_usd") or 0.0)
        proceeds = round(info["proceeds"], 4)
        filled = int(pos.get("filled_contracts") or 0)
        fee = _position_fee_usd(pos)
        if filled > 0 and _exit_was_sell(info):
            fee += _sell_fee_usd(pos, filled, proceeds / filled * 100.0)
        pnl = round(proceeds - cost - fee, 4)
        correct = 1 if pnl > 0.05 else (0 if pnl < -0.05 else None)
        with db.get_db() as conn:
            fields = dict(resolved=1, outcome_correct=correct,
                          settlement_usd=proceeds, pnl_usd=pnl)
            if fee > 0 and not float(pos.get("fees_usd") or 0.0):
                fields["fees_usd"] = round(fee, 6)
            db.update_bot_position(conn, pos["id"], **fields)
            conn.execute(
                "UPDATE bot_positions SET resolved_at=datetime('now'), closed_early=1 "
                "WHERE id=?",
                (pos["id"],),
            )
            db.log_event(
                conn, pos["id"], "external_exit",
                note=f"{info['kind']} on Polymarket: proceeds=${proceeds:.2f} pnl=${pnl:+.2f}",
            )
            row = db.fetch_position_by_id(conn, pos["id"])
        if row:
            changed.append(row)
        logger.info(
            f"[exits] booked #{pos['id']} {pos['ticker']} {pos['direction']} as "
            f"{info['kind']} (proceeds=${proceeds:.2f}, pnl=${pnl:+.2f})"
        )
    return changed


async def cancel_all_resting_orders(reason: str = "cancel-all") -> int:
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, order_id FROM bot_positions
               WHERE status IN ('submitted','partial')
                 AND resolved=0 AND network=?""",
            (get_env(),),
        ).fetchall()
    with db.get_db() as conn:
        c15_rows = conn.execute(
            """SELECT id, order_id, filled_contracts FROM crypto15m_positions
               WHERE status IN ('submitted','partial') AND resolved=0
                 AND order_id IS NOT NULL AND network=?""",
            (get_env(),),
        ).fetchall()
    ids = [r["order_id"] for r in rows if r["order_id"]] + [r["order_id"] for r in c15_rows]
    if not ids:
        return 0
    res = await cancel_orders(ids)
    done = res["canceled"]
    for oid, why in res["not_canceled"].items():
        logger.warning(f"cancel_all_resting ({reason}): {oid}: {why}")
    canceled = 0
    with db.get_db() as conn:
        for r in rows:
            if r["order_id"] and str(r["order_id"]) in done:
                db.update_bot_position(conn, r["id"], status="canceled", error=reason)
                db.log_event(conn, r["id"], "cancel", note=reason)
                canceled += 1
        for r in c15_rows:
            if str(r["order_id"]) not in done:
                continue
            if not int(r["filled_contracts"] or 0):
                conn.execute(
                    """UPDATE crypto15m_positions
                          SET status='canceled', error=?, last_updated=datetime('now')
                        WHERE id=? AND COALESCE(filled_contracts,0)=0""",
                    (reason, r["id"]),
                )
            canceled += 1
    return canceled


async def cancel_all_open() -> int:
    return await cancel_all_resting_orders("user cancel-all")


async def _confirm_sell(oid: str | None, qty: int, px: int) -> tuple[int, float]:
    if not oid:
        return 0, 0.0
    for _ in range(4):
        try:
            resp = await get_order(oid)
            order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
            parsed = _parse_polymarket_order(order)
            f = int(parsed.get("filled") or 0)
            if f > 0:
                return f, float(parsed.get("avg_cents") or px)
            if parsed.get("status") in ("canceled", "cancelled"):
                return 0, 0.0
        except PolymarketAPIError as e:
            if e.status == 404:
                return qty, float(px)
        except Exception:
            pass
        await asyncio.sleep(0.6)
    return 0, 0.0


async def flatten_open_positions(cfg: dict) -> dict:
    env = get_env()
    canceled = await cancel_all_open()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, direction, filled_contracts, cost_usd
               FROM bot_positions
               WHERE status='filled' AND resolved=0 AND network=? AND filled_contracts > 0""",
            (env,),
        ).fetchall()
        open_filled = [dict(r) for r in rows]

    sold_total = 0
    proceeds_total = 0.0
    for pos in open_filled:
        sold, proceeds = await _liquidate_position(pos, cfg, reason="flatten")
        sold_total += sold
        proceeds_total += proceeds
    return {"canceled": canceled, "sold": sold_total, "proceedsUsd": round(proceeds_total, 2)}


async def take_profit_sweep(cfg: dict) -> list[dict]:
    try:
        pct = float(cfg.get("take_profit_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(1.0, pct))
    if pct <= 0:
        return []
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, direction, filled_contracts, cost_usd
               FROM bot_positions
               WHERE status='filled' AND resolved=0 AND network=? AND filled_contracts > 0""",
            (env,),
        ).fetchall()
        open_filled = [dict(r) for r in rows]
    closed: list[dict] = []
    for pos in open_filled:
        qty = int(pos["filled_contracts"] or 0)
        cost = float(pos["cost_usd"] or 0.0)
        if qty <= 0 or cost <= 0:
            continue
        try:
            q = await get_quote(pos["ticker"], pos["direction"])
            bid = q.get("bid_cents")
        except Exception:
            bid = None
        if not bid or int(bid) <= 0:
            continue
        cur_value = qty * (int(bid) / 100.0)
        if (cur_value - cost) / cost < pct:
            continue
        sold, _proceeds = await _liquidate_position(pos, cfg, reason="take_profit")
        if sold > 0:
            with db.get_db() as conn:
                row = db.fetch_position_by_id(conn, pos["id"])
            if row:
                closed.append(row)
    return closed


async def _liquidate_position(pos: dict, cfg: dict, *, reason: str) -> tuple[int, float]:
    ticker, direction = pos["ticker"], pos["direction"]
    qty = int(pos["filled_contracts"] or 0)
    cost = float(pos["cost_usd"] or 0.0)
    if qty <= 0:
        return 0, 0.0
    remaining = qty
    pos_sold = 0
    pos_proceeds = 0.0
    for _attempt in range(6):
        if remaining <= 0:
            break
        try:
            q = await get_quote(ticker, direction)
            bid = q.get("bid_cents")
        except Exception:
            bid = None
        sell_px = max(1, min(99, int(bid) if bid else 1))
        coid = f"krypt-{reason[:4]}-{pos['id']}-{uuid.uuid4().hex[:6]}"
        try:
            resp = await place_limit_order(
                ticker=ticker, side=direction, action="sell",
                count=remaining, price_cents=sell_px, client_order_id=coid,
            )
        except Exception as e:
            logger.warning(f"[{reason}] sell {ticker} failed: {e}")
            break
        order = (resp.get("order") if isinstance(resp, dict) else None) or {}
        oid = order.get("order_id")
        sold, avg_cents = await _confirm_sell(oid, remaining, sell_px)
        if sold <= 0:
            if oid:
                try:
                    await cancel_order(oid)
                except Exception:
                    pass
            logger.warning(f"[{reason}] {ticker}: sell didn't fill; still holding {remaining}")
            break
        pos_sold += sold
        pos_proceeds += sold * avg_cents / 100.0
        remaining -= sold
        if remaining > 0 and oid:
            try:
                await cancel_order(oid)
            except Exception:
                pass

    if pos_sold <= 0:
        return 0, 0.0
    avg_c = pos_proceeds * 100.0 / pos_sold
    if remaining <= 0:
        fee = _position_fee_usd(pos) + _sell_fee_usd(pos, pos_sold, avg_c)
        pnl = pos_proceeds - cost - fee
        correct = 1 if pnl > 0.05 else (0 if pnl < -0.05 else None)
        with db.get_db() as conn:
            fields = dict(resolved=1, outcome_correct=correct,
                          settlement_usd=pos_proceeds, pnl_usd=pnl,
                          exit_reason=reason)
            if fee > 0 and not float(pos.get("fees_usd") or 0.0):
                fields["fees_usd"] = round(fee, 6)
            db.update_bot_position(conn, pos["id"], **fields)
            conn.execute(
                "UPDATE bot_positions SET resolved_at=datetime('now'), closed_early=1 WHERE id=?",
                (pos["id"],),
            )
            db.log_event(conn, pos["id"], reason,
                         note=f"sold {pos_sold} @ ~{avg_c:.0f}c pnl=${pnl:+.2f}")
        logger.info(f"[{reason}] {ticker} sold {pos_sold} @ ~{avg_c:.0f}c pnl=${pnl:+.2f}")
    else:
        sold_cost = cost * (pos_sold / qty)
        with db.get_db() as conn:
            db.update_bot_position(
                conn, pos["id"],
                filled_contracts=remaining,
                cost_usd=round(max(0.0, cost - sold_cost), 6),
            )
            db.log_event(conn, pos["id"], f"{reason}_partial",
                         note=f"sold {pos_sold}/{qty} @ ~{avg_c:.0f}c; holding {remaining}")
        logger.warning(
            f"[{reason}] {ticker} PARTIAL sold {pos_sold}/{qty} @ ~{avg_c:.0f}c; still holding {remaining}"
        )
    return pos_sold, pos_proceeds
