"""Krypt PolyBot backend service.

Spawned as a child process by the Electron main process. Communicates
exclusively via JSON-line stdin/stdout (no HTTP server, no shared
filesystem state besides the SQLite DB and the credential files in
<userData>).

Wire protocol — one JSON object per line:

  Command         {"type": "rpc", "id": "<uuid>", "method": "...", "params": {...}}
  Response (ok)   {"type": "rpc", "id": "<uuid>", "ok": true, "result": {...}}
  Response (err)  {"type": "rpc", "id": "<uuid>", "ok": false, "error": "..."}
  Event           {"type": "event", "name": "...", "data": {...}}
  Log             {"type": "log", "level": "INFO", "source": "...", "msg": "..."}

Methods (called from Electron main):
  ping
  setConfig          {config: {...}}
  setCredentials     {privateKey, apiCreds?}
  clearCredentials
  credentialStatus
  testCredentials
  account
  pnlSeries          {sinceHours?}
  positions          {filter?}
  signals            {filter?}
  scannerStats
  cancelAllOpen
  flatten
  runOnce            {action}                 -- syncMarkets|pollOrders|resolveAll|reconcilePositions|syncPositions|recomputePnl|reconcileFills|auditPnl
  pause              {paused: bool}
  shutdown
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __name__ == "__main__" and "--mcp" in sys.argv:
    import mcp_stdio
    sys.exit(mcp_stdio.main())

if __name__ == "__main__" and "--backtest-child" in sys.argv:
    import script_backtest_child
    sys.exit(script_backtest_child.main())

if __name__ == "__main__":
    sys.modules.setdefault("service", sys.modules[__name__])


_log_q: asyncio.Queue | None = None


class _StdoutHandler(logging.Handler):
    """Forward every log record up to the Electron parent as a JSON line."""

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
    try:
        import logscrub
        logscrub.install(root)
    except Exception:  # noqa: BLE001 — a missing scrubber is a selftest failure
        pass
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)



_setup_logging()
logger = logging.getLogger("service")


import db  # noqa: E402
import polymarket_api  # noqa: E402
import polymarket_auth  # noqa: E402
import scanner  # noqa: E402
import crypto15m  # noqa: E402
import trader  # noqa: E402
import crypto15m_trader  # noqa: E402
import crypto15m_record  # noqa: E402
import perps_api  # noqa: E402
import perps_board  # noqa: E402
import perps_maker  # noqa: E402
import perps_ws  # noqa: E402
import user_ws  # noqa: E402
import data_v2  # noqa: E402
import activity_ws  # noqa: E402
import copy_trader  # noqa: E402
import script_engine  # noqa: E402
import script_sandbox  # noqa: E402
import rtds_ws  # noqa: E402
import spot_ws  # noqa: E402
from config import DEFAULT_CONFIG, merge_with_defaults  # noqa: E402


def _iso_utc(s: Any) -> Any:
    """Normalise a SQLite naive-UTC timestamp ("YYYY-MM-DD HH:MM:SS") into
    a real ISO 8601 UTC string ("YYYY-MM-DDTHH:MM:SSZ").

    Why: SQLite's `datetime('now')` writes naive UTC strings without a Z.
    JavaScript's Date parser then interprets those strings as LOCAL time
    (Chrome behaviour), which made every "Started at" / "Resolved at"
    field show up 7 hours off for users in PT. Tagging with Z makes JS
    parse them correctly so `toLocaleString()` formats in the user's
    real local zone."""
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



def _balance_persistable() -> bool:
    """Whether this account snapshot may be written down as a balance point
    (the P&L chart, the daily stop's input, transfer detection, the run row).
    Not while /value keeps failing: that total is today's cash plus a stale
    portfolio, and once persisted it becomes the reading the daily stop acts
    on and the baseline the next real reading is measured against. The
    snapshot still goes to the UI."""
    return bool(STATE.auth_ok) and not trader.portfolio_stale()


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
        "tradingRegion": polymarket_api.region_snapshot(refresh=bool(STATE.auth_ok)),
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
    """Unrealized P&L for an OPEN filled position: held contracts valued at
    the live mark price minus cost. None for resolved rows (use realized
    pnl), unfilled rows, or rows without a mark yet."""
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
    """Run a coroutine as a detached background task so a slow call can never
    stall the trading loop (the balance refresh after a fill goes through a
    retry ladder that can take seconds — exactly when open positions need
    their management cadence). Errors are swallowed; a strong reference is
    kept so the task can't be garbage-collected mid-flight."""
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
    """Whale/momentum collection gate: scanning runs when the user collects
    main-engine research data OR trading is on (the engine can't follow signals
    it never sees) OR a user script consumes signals."""
    return (
        bool(cfg.get("main_record_signals", True))
        or bool(cfg.get("enable_trading"))
        or script_engine.needs_signal_feed()
    )


async def _emit_whale_batch(rows: list[dict]) -> None:
    """Ship freshly-inserted whale rows to the renderer as ONE `signal:batch`.

    Shared by the REST poll and the socket push so the renderer needs no idea
    which one found the whale (a batch of one is fine)."""
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
    """Detect + insert + emit ONE socket-seen whale. Detached (fire-and-forget)
    so a Gamma market lookup can never sit on the websocket read loop."""
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
    """Nudge the copy loop out of its cadence wait. Safe from the socket's read
    loop (Event.set is sync and never raises for a set Event)."""
    ev = _copy_wake
    if ev is None:
        return
    try:
        ev.set()
    except Exception as e:      # pragma: no cover - defensive
        logger.debug(f"copy wake ({reason}) failed: {e}")


def _wake_copy_if_followed(cfg: dict, raw: dict) -> None:
    """Wake the copy loop when `raw` is a trade by a wallet we follow. Cheap:
    a lowercase compare against the (small) follow list, on the socket's read
    loop, for every frame on the tape."""
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
    except Exception as e:      # pragma: no cover - defensive
        logger.debug(f"copy wake check: {e}")


def _on_activity_trade(raw: dict) -> None:
    """Sync sink for EVERY trade on the RTDS tape. Runs on the socket's read
    loop, so it does only cheap work and hands anything awaitable to a task."""
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
    """Start a background market sync. False if one is already in flight."""
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
    """Time + ceiling one master-loop block. Swallows the ceiling timeout."""
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
    """The crypto15m engine hit its overall take-profit target and turned itself
    off (it already flipped crypto15m_enabled in STATE.cfg, which it shares by
    reference). Tell the host (Electron) so it PERSISTS crypto15m_enabled=False
    and reflects it in the UI toggle, and push a fresh snapshot so the dashboard
    updates at once."""
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
    """Single coroutine that runs every periodic task on its own cadence."""
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
    last_forecast = 0.0
    last_autopilot = 0.0

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
                    if _balance_persistable():
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
                    from config import account_live
                    res = await terminal_trade.standing_tick(
                        allow_trading=True, paper=not account_live(cfg))
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

        try:
            async with _block("forecasts"):
                if now - last_forecast >= 300:
                    last_forecast = now
                    import forecast_ledger
                    await forecast_ledger.resolve_pending()
        except Exception as e:
            logger.debug(f"forecast resolve error: {e}")

        try:
            async with _block("autopilot"):
                if now - last_autopilot >= 30:
                    last_autopilot = now
                    acfg = merge_with_defaults(dict(STATE.cfg or {}))
                    if acfg.get("autopilot_enabled"):
                        import autopilot
                        if autopilot.due(acfg) and autopilot.blocked_reason(acfg) is None:
                            t = asyncio.create_task(autopilot.run_once(acfg, "schedule"))
                            _bg_tasks.add(t)
                            t.add_done_callback(_bg_tasks.discard)
        except Exception as e:
            logger.debug(f"autopilot schedule error: {e}")

        await asyncio.sleep(1)



async def _engine_supervisor(name: str, body, heartbeat_attr: str) -> None:
    """Run `body()` forever, restarting it on any crash with exponential
    backoff. A trading engine that dies silently is the failure mode this file
    is written against, so a crash is CRITICAL-logged; the heartbeat is
    re-stamped across the backoff sleep so the watchdog doesn't also fire while
    we are deliberately waiting."""
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
    """Copy trading on its own cadence, woken directly by the trade tape.

    Cadence (unchanged from when this rode the master loop, minus the 0-10s it
    used to spend queued behind the other blocks):
      - activity_ws PUSH: a followed wallet's BUY/SELL sets `_copy_wake` from
        the socket's read loop, so the tick starts within milliseconds
        (debounced to 1s so a burst of frames is one tick),
      - FAST (copy_fast_poll_sec) whenever we HOLD open copies (their sell has
        to be mirrored quickly) or the hot window after a push is live,
      - BASE (copy_poll_sec) otherwise - idle entry discovery.
    The socket's LIFECYCLE stays on the master loop (it is shared with whale
    detection); this loop only consumes it."""
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
    """Sleep, but return immediately when the trade tape wakes copy."""
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
    """The 15-minute crypto executor on its own 4s cadence.

    Places REAL orders (no paper mode). Gated on auth, NOT on
    crypto15m_enabled: run_tick cheaply no-ops when the engine is off AND
    nothing is open, but still MANAGES open positions to settlement after the
    engine is turned off - a manual toggle or the overall take-profit
    kill-switch must never strand an open bet. New entries are opened only when
    crypto15m_enabled is on. Independent of the main bot's enable_trading."""
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
    """The Perps market maker on its own cadence (`perps_poll_sec`). Gated on a
    linked Perps account, not on the CLOB wallet session: once the proxy
    credential exists the engine needs nothing else. With the engine off it
    still ticks — to cancel any quotes it left behind and to keep the tab's
    preview fresh — but never places an order."""
    await _engine_supervisor("perps", _perps_loop_body, "_perps_heartbeat")


async def _perps_loop_body() -> None:
    """A full tick (account + orders re-synced with the exchange) every
    `perps_poll_sec`; between them, the live price feed wakes the loop the
    moment a quoted market's mid moves and only those markets are requoted
    from cached state."""
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
    """Start every engine loop (master + copy + crypto15m + perps) and the
    watchdog that supervises them. `_loop_stop` is shared: one Event stops
    every loop, which is what factory-reset / clear-history / shutdown want."""
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
    """Stop the watchdog and every engine loop. Each is given the shared stop
    Event first (so it exits its own `while` cleanly), then a bounded wait,
    then a hard cancel — a task still pending at process exit is what produces
    'Task was destroyed but it is pending!' on stderr."""
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
    """Cancel (if needed) and re-spawn ONE engine loop.

    A restart is SAFE here: DB writes are synchronous, so a cancel can only land
    on a network await (never mid-write), and any order that reached the exchange
    is re-imported by the wallet-authoritative reconcile when the loop comes back
    up. A false restart (e.g. a legitimately slow iteration during a network
    outage) is harmless - it just re-enters the same idempotent loop."""
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
    """0.0 if `label` may be restarted right now, else the seconds still to
    wait. Exponential backoff so a loop that dies on every single start cannot
    spin the watchdog restarting it forever; the ladder resets once a loop has
    been up for a minute."""
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
    """Force-restart any engine loop whose heartbeat goes stale (or whose task
    ended), so open real-money positions never silently stop being managed if
    some await hangs.

    Watches every loop - master, copy, crypto15m, perps - independently: a
    stalled copy loop restarts the copy loop only, leaving the master loop's
    reconcile/settlement passes and the crypto engine untouched."""
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
    if p.get("pruneAgentTokens"):
        import mcp_server
        gone = mcp_server.prune_tokens(cfg)
        if gone:
            logger.info(f"mcp: removed {len(gone)} deleted agent token(s)")
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
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mcp sync failed: {e}")
    return {"ok": True}


async def _h_setCredentials(p: dict) -> dict:
    """Save the Polygon wallet private key (and optional pre-derived API
    creds). The CLOB API credentials are derived on demand from the wallet
    key, so normally only `privateKey` is needed."""
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
        polymarket_api._creds_derived = False  # type: ignore[attr-defined]
    except Exception:
        pass

    active_env = env or polymarket_auth.get_env()
    global _sig_type_reconciled
    _sig_type_reconciled = False
    try:
        trader._balance_cache.pop(active_env, None)  # type: ignore[attr-defined]
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
    """Re-detect the deposit wallet's signing scheme once per launch and correct
    a wrong stored value.

    A shipped version of `detect_wallet_signature_type` concluded POLY_PROXY (1)
    from the mere ABSENCE of an EIP-1271 selector, and `check_trading_ready`
    persisted that — silently downgrading the current-generation deposit wallet
    (the email/Google default) from POLY_1271 (3). Every order it then signed was
    rejected with "the order signer address has to be the address of the api key".

    Detection alone doesn't rescue those users: it only runs from the manual
    Test/Save button. Someone who installs the fix and simply reopens the app
    would keep the corrupt type and stay unable to trade. Run it on connect so the
    repair lands with zero user action. Detection abstains (returns None) unless
    the chain positively proves a type, so this can never introduce a wrong value.
    """
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


async def _reconcile_signer_role() -> None:
    """Ask the deposit wallet, on connect, whether the imported key is its owner
    or a session signer — the form every POLY_1271 order must be signed in.

    This used to be learned only by the manual Test button and held only in
    memory, so after every restart a session key signed in the owner form and
    every order (exits included) was rejected until the user clicked Test.
    Read-only (two eth_calls at most); once the wallet has answered for this
    key + wallet it isn't asked again this process. When the chain can't be read
    the role saved by an earlier run is used instead, and a signature rejection
    re-asks (polymarket_api.place_limit_order). Never raises.
    """
    try:
        funder = polymarket_auth.get_funder()
        if (not funder
                or polymarket_auth.get_signature_type() != polymarket_auth.SIGNATURE_TYPE_POLY_1271):
            return
        signer = polymarket_auth.get_address()
        if polymarket_auth.signer_role_for(signer, funder)[1] == "chain":
            return
        res = await polymarket_api.check_deposit_wallet_authorization()
        role, source = polymarket_auth.signer_role_for(signer, funder)
        if source == "chain":
            if role == "SESSION_KEY":
                logger.info(
                    f"[wallet] imported key is a session signer of deposit {funder}; "
                    "orders carry the session envelope"
                )
            if res.get("tested") and not res.get("ok"):
                logger.warning(f"[wallet] {res.get('reason')}")
        else:
            logger.info(
                f"[wallet] could not confirm the signer role on-chain "
                f"({res.get('reason') or 'no answer'}); signing as {role} ({source})"
            )
    except Exception as e:
        logger.debug(f"[wallet] signer-role check skipped: {e}")


async def _establish_auth(retries: int = 3) -> bool:
    """Verify the wallet is connected by deriving CLOB API credentials — the
    real 'can this wallet authenticate' signal — with retries so a single
    transient blip (RPC timeout, region 403, rate-limit) can't latch auth as
    'disconnected'.

    The balance read is best-effort and never decides the verdict: it hits
    flaky public Polygon RPCs, and letting it fail the check was the cause of
    spurious 'saved wallet could not connect to Polymarket' on the dashboard
    (where the user then had to click Wallet → Test to clear it)."""
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

        async def _warm_balance() -> None:
            try:
                await polymarket_api.get_balance()
            except Exception as be:
                logger.debug(f"auth verify: balance warm failed (non-fatal): {be}")

        await asyncio.gather(_reconcile_signer_role(), _warm_balance(),
                             return_exceptions=True)
        return True
    logger.warning(f"auth verify failed after {retries} attempts: {last_exc}")
    return False


async def _h_testCredentials(p: dict) -> dict:
    """Verify the wallet by deriving CLOB API credentials from it and
    fetching the account balance. Single network (mainnet)."""
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
        "fatalIssues": ready.get("fatalIssues", []),
        "region": ready.get("region"),
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
    """Pause the engine loop. Pausing also stops the AI agents and Autopilot
    (trade mode off, persisted like the phone's /pause): they run on the MCP
    server and Autopilot's own task, not this loop, so a pause that only froze
    the loop let them keep trading — with approvals off, unseen. Unpausing
    does NOT turn them back on; that is the AI Agents page's call."""
    STATE.paused = bool((p or {}).get("paused", False))
    out: dict = {"paused": STATE.paused}
    if STATE.paused:
        try:
            import remote
            was = await remote.stop_agents()
            out["agentsStopped"] = True
            out["message"] = "Engines paused. " + remote.agents_stopped_text(was)
        except Exception as e:  # noqa: BLE001
            logger.warning("pause: could not stop the AI agents: %s", e)
            out["agentsStopped"] = False
            out["message"] = ("Engines paused, but the AI agents could NOT be "
                              f"stopped ({type(e).__name__}). Turn them off on "
                              "the AI Agents page.")
    return out


def _run_row_to_js(r: dict) -> dict:
    """Serialise a `bot_runs` row for the renderer."""
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
    """Return the most recent bot_runs rows for the History → Run
    History tab. Optional params: env, limit."""
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
    """Wipe ALL trade history, bot runs, P&L snapshots, signals, and
    scanner state from the local DB. Public Polymarket market cache is
    preserved (no PII, just public data).

    The trader loop is STOPPED first — without this, a reconcile
    cycle running mid-wipe will re-INSERT the live Polymarket positions
    we just deleted, which is exactly what made earlier reset
    attempts look like nothing happened. The active bot run (if any)
    is closed; in-memory snapshot state is cleared so the dashboard
    returns to a true zero. The loop is restarted and the next
    reconcile re-imports live positions as fresh `external` rows.
    """
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
    """Wipe trade history / P&L / sessions ONLY — keep all settings, the
    wallet credentials, and the live signal feed. This is the targeted
    "reset my stats" button on the History page (vs. `factoryReset`, which
    also drops scanner/signal state).

    Same loop-pause discipline as factory reset: a reconcile cycle running
    mid-wipe would re-INSERT live Polymarket positions, so the loop is
    stopped, the active run closed, the history cleared, then the loop and a
    fresh session run are restarted with the user's current balance as the
    new baseline. Open Polymarket positions are re-imported as `external`
    rows on the next tick — only the resolved *history* is actually gone.
    """
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
    """Every Perps market with reward/competition/volume stats (public data;
    works without a linked account)."""
    return await perps_board.snapshot(STATE.cfg)


async def _h_perps_status(_p: dict) -> dict:
    if not perps_api.creds():
        try:
            await perps_maker.preview(STATE.cfg)
        except Exception as e:
            logger.debug(f"perps preview: {e}")
    return perps_maker.status(STATE.cfg)


async def _h_perps_link(_p: dict) -> dict:
    """Authorise a Perps proxy signer with the saved wallet key (one wallet
    signature) and store it."""
    try:
        link = await perps_api.link_account()
    except perps_api.PerpsAPIError as e:
        return {"ok": False, "message": str(e)}
    except FileNotFoundError:
        return {"ok": False, "message": "Connect your Polymarket wallet first (Wallet page)."}
    return {"ok": True, "message": "Perps account linked.", "link": link}


async def _h_perps_unlink(_p: dict) -> dict:
    """Forget the proxy credential. Our resting quotes are cancelled first —
    without the credential nothing could cancel them afterwards."""
    c = perps_api.creds()
    if c:
        try:
            await perps_maker.cancel_ours(c)
        except Exception:
            pass
    perps_api.unlink_account()
    return {"ok": True, "message": "Perps account unlinked."}



async def _h_data_user_pnl(p: dict) -> dict:
    """Polymarket's own P&L for the connected wallet over a window —
    including the rebates and rewards it pays out separately."""
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
    """Replay the Perps market maker over the exchange's trade history (REST
    backfill + the live recorder's tape). One at a time: a run can page
    hundreds of history requests and shares the per-IP rate limit with the
    live engine."""
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
    """Resolved 15m trades (with strategy attribution) for the History page."""
    limit = min(1000, max(1, int((p or {}).get("limit") or 200)))
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        rows = db.recent_crypto15m_resolved(conn, env, limit=limit)
    return {"rows": [crypto15m_trader._pos_to_js(r) for r in rows]}


async def _h_c15_backtest(p: dict) -> dict:
    """Replay the CURRENT (or supplied) 15m config over recorded ticks using
    the live entry gates. Read-heavy — run off the event loop."""
    import replay
    live = STATE.cfg or {}
    cfg = dict(live)
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or polymarket_auth.get_env())
    return await asyncio.to_thread(
        replay.replay, cfg, env=env, since_days=since,
        recording_interval=crypto15m._interval(live),
        recording_on=bool(live.get("crypto15m_record_signals", True)),
    )


async def _h_c15_parlay_generate(p: dict) -> dict:
    """Run the per-hour parlay generator over the recorded corpus. Saves the
    resulting schedule to the durable kv store (UNARMED — arming is a
    separate explicit call) unless save=False."""
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
    """The stored parlay schedule + armed flag (fresh read, not the cache),
    and why it should be regenerated when it is stale."""
    armed, sched = crypto15m_trader.parlay_state(force=True)
    return {"armed": armed, "schedule": sched,
            "staleReason": crypto15m_trader.parlay_stale_reason(sched)}


async def _h_c15_parlay_arm(p: dict) -> dict:
    """Arm/disarm the stored schedule for live trading. While armed, the
    parlay schedule OWNS crypto15m entries (seated hours trade their
    generated config; unseated hours don't trade at all)."""
    import parlay_generator
    try:
        parlay_generator.set_armed(bool((p or {}).get("armed")))
    except ValueError as e:
        armed, sched = crypto15m_trader.parlay_state(force=True)
        return {"armed": armed, "hasSchedule": bool(sched), "error": str(e)}
    armed, sched = crypto15m_trader.parlay_state(force=True)
    return {"armed": armed, "hasSchedule": bool(sched),
            "staleReason": crypto15m_trader.parlay_stale_reason(sched)}


async def _h_main_backtest(p: dict) -> dict:
    """Replay recorded whale/momentum signals through the live should_trade
    gates with follower economics."""
    import replay
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    return await asyncio.to_thread(replay.replay_main, cfg, since_days=since)


async def _h_collection_stats(_p: dict) -> dict:
    """Inventory of the passively collected research data — what the Backtest
    page's "view data" area shows. Counts + spans + a recent sample per
    stream, all cheap indexed queries."""
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
            by_iv = conn.execute(
                """SELECT COALESCE(interval, '15m') iv, COUNT(*) n,
                          SUM(CASE WHEN resolved = 1 AND up_won IS NOT NULL
                                   THEN 1 ELSE 0 END) r
                   FROM crypto15m_signals WHERE network=?
                   GROUP BY iv""", (env,)
            ).fetchall()
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
                "byInterval": {
                    str(r["iv"]): {"windows": int(r["n"] or 0),
                                   "resolved": int(r["r"] or 0)}
                    for r in by_iv
                },
                "recordingInterval": crypto15m._interval(STATE.cfg or {}),
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
    """Dump the collected research data as CSVs the user can analyze anywhere.
    Written under data/exports/<stamp>/; the renderer reveals the folder."""
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
    """Ordered gate checklist for both engines — the "why isn't it trading"
    panel. Each row: {id, label, state: ok|blocked|off, reason}. The first
    blocked row is the answer."""
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
    from config import account_live as _account_live
    gate("account", "Account is Live", _account_live(cfg),
         "the account is in Paper — engines send nothing until you switch it to Live")
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
    """Scrub script text arriving from the renderer.

    Text pasted out of a browser, PDF or chat window can carry LONE SURROGATES
    (\\udc80-\\udcff) — what a smart quote turns into when it loses its encoding
    on the way in. Python stores them happily and SQLite accepts them, but the
    moment the row is serialized back to the renderer `.encode("utf-8")` raises
    `UnicodeEncodeError: surrogates not allowed`, killing the RPC. The renderer
    only sees `error invoking remote method "scripts:validate"`, which points
    nowhere near the real cause.

    Replacing them here means the worst case is an ordinary syntax error with a
    line number, which the editor can actually show the user.
    """
    return str(raw or "").encode("utf-8", "replace").decode("utf-8", "replace")


def _script_status_js(r: dict) -> dict:
    """The one thing the Scripts page needs and never had: whether this script
    is ACTUALLY running, and if not, which gate is holding it.

    The page used to show `enabled` plus shadow/live, neither of which answers
    the question. A script can read ENABLED and be doing nothing because the
    wallet is disconnected, because it's armed while the master switch is off,
    because it's past the concurrency cap, or because its code failed to
    compile — and the user's only clue was an empty ledger. Every gate is
    evaluated here, in the process that applies them, so the line can't drift
    from the truth the way a re-derivation in the renderer would.
    """
    import script_engine
    sid = str(r.get("id") or "")
    st = script_engine.status_for(sid)
    cfg = STATE.cfg or {}
    enabled = bool(r.get("enabled"))
    shadow = bool(r.get("dry_run", 1))

    if r.get("needs_review"):
        state, detail = ("off",
                         "Written by an AI agent and not yet reviewed. Read the "
                         "code, then flip Enabled to approve it and start it in "
                         "shadow.")
    elif not enabled:
        state, detail = "off", "Not enabled — flip Enabled to start it."
    elif not STATE.auth_ok:
        state, detail = ("blocked",
                         "No wallet connected — connect one on the Dashboard.")
    elif not shadow and not __import__("config").account_live(cfg):
        state, detail = ("blocked",
                         "Armed for real orders, but the account is in Paper — "
                         "it is not running. Switch the account to Live, or set "
                         "this script back to Shadow.")
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
        "author": str(r.get("author") or "") or "user",
        "needsReview": bool(r.get("needs_review")),
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


async def _h_script_set_dry_run(p: dict, *, agent: str | None = None) -> dict:
    """Arm a script for real orders (dryRun=False) or return it to shadow.

    Arming is the moment a script can spend money, so it re-validates first
    and refuses code that doesn't pass — the same gate as enabling.

    Only the USER arms. `agent` is set by the MCP tools (never by the RPC
    dispatcher, which passes params only): an agent may return a script to
    shadow, never arm one. A script an agent wrote must be reviewed (enabled
    by the user) before it can be armed."""
    sid = str(p.get("id") or "")
    dry = bool(p.get("dryRun"))
    if agent and not dry:
        raise ValueError("Only the user can arm a script for real orders, from "
                         "the Scripts page.")
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        if not dry:
            if row.get("needs_review"):
                raise ValueError("An AI agent wrote this script's code and it has not "
                                 "been reviewed. Read it, flip Enabled to approve it, "
                                 "then arm it.")
            errors = script_sandbox.validate(str(row.get("code") or ""))
            if errors:
                raise ValueError("script does not validate: " + errors[0])
            errors = _agent_code_errors(row)
            if errors:
                raise ValueError("this agent-written script breaks the agent-script "
                                 "rules: " + errors[0])
        db.update_user_script(conn, sid, dry_run=1 if dry else 0)
        row = db.get_user_script(conn, sid)
        stats = db.script_live_stats(conn, polymarket_auth.get_env())
        shadow = db.script_shadow_stats(conn, polymarket_auth.get_env())
    logger.warning(
        f"[scripts] {row.get('name')} ({sid[:8]}) is now "
        f"{'SHADOW (no real orders)' if dry else 'ARMED FOR REAL ORDERS'}")
    return {"script": _script_row_js(row or {}, stats, shadow)}


async def _h_script_shadow_orders(p: dict) -> dict:
    """The shadow ledger for one script — what it would have traded."""
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


def _is_agent_author(row: dict | None) -> bool:
    return str((row or {}).get("author") or "").startswith("agent")


def _agent_code_errors(row: dict | None) -> list[str]:
    """The agent-script allowlist errors for a row an AI agent wrote ([] for
    the user's own code, which is full Python by design)."""
    if not _is_agent_author(row):
        return []
    import script_audit
    return script_audit.validate_agent(_sanitize_script_code((row or {}).get("code")))


async def _push_script_off(sid: str, reason: str) -> None:
    """Tell the engine and the Scripts page that a script stopped: the
    compiled module is dropped (a re-enable compiles the row fresh) and the
    page hears it at once instead of on its next poll."""
    try:
        import script_engine
        script_engine.forget(sid)
    except Exception:  # noqa: BLE001
        pass
    try:
        await emit_event("script:status", {"id": sid, "enabled": False, "lastError": reason})
    except Exception:  # noqa: BLE001
        pass


async def _h_script_save(p: dict, *, agent: str | None = None) -> dict:
    """Create or update a script. Validation runs in the script's declared
    trust mode; an invalid script still SAVES (so work isn't lost) but comes
    back with the errors and is force-disabled.

    `agent` names the AI agent saving it (set by mcp_tools, never by the RPC
    dispatcher, which passes params only - the renderer's save is always the
    user's). Code an agent writes or changes must pass the agent allowlist
    (script_audit.validate_agent) and lands DISABLED, in SHADOW and marked
    needs_review: it runs nowhere in this process until the user enables it
    from the Scripts page, and only the user can arm it again."""
    sid = str(p.get("id") or "").strip() or os.urandom(8).hex()
    code = _sanitize_script_code(p.get("code"))
    meta = script_sandbox.parse_header(code)
    name = str(p.get("name") or "").strip() or meta.get("name") or "Untitled script"
    desc = str(p.get("description") or "").strip() or meta.get("description") or ""
    import script_audit
    if agent:
        bad = script_audit.validate_agent(code)
        if bad:
            raise ValueError("Scripts written by an AI agent may only use plain "
                             "computation (see get_script_docs). Refused: "
                             + "; ".join(bad[:5]))
    with db.get_db() as conn:
        existing = db.get_user_script(conn, sid)
        errors = script_sandbox.validate(code)
        audit = script_audit.audit(code)
        code_changed = bool(existing) and str(existing.get("code") or "") != code
        force_off = (code_changed and bool(existing.get("enabled"))
                     and not audit["ok"])
        agent_code = bool(agent) and (existing is None or code_changed)
        was_armed = bool(existing) and not bool(existing.get("dry_run", 1))
        was_enabled = bool(existing) and bool(existing.get("enabled"))
        db.upsert_user_script(conn, {
            "id": sid, "name": name, "description": desc, "code": code,
            "notes": str(p.get("notes") or ""),
        })
        if agent_code:
            db.update_user_script(conn, sid, enabled=0, dry_run=1, needs_review=1,
                                  author=f"agent:{str(agent)[:60]}")
        elif code_changed and not agent:
            db.update_user_script(conn, sid, author="user", needs_review=0)
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
    disarmed = force_off or (agent_code and (was_armed or was_enabled))
    if agent_code and existing is not None:
        logger.warning(
            f"[scripts] AI agent {agent} rewrote {name} ({sid[:8]}): disabled, "
            f"back in SHADOW, waiting for your review"
            f"{' (it was ARMED for real orders)' if was_armed else ''}")
        await _push_script_off(sid, "Rewritten by an AI agent: disabled and in "
                                    "shadow until you review it.")
    elif force_off:
        await _push_script_off(sid, "Code changed with critical audit findings.")
    return {"script": _script_row_js(row or {}), "errors": errors,
            "warnings": _script_warnings(code), "audit": audit,
            "disarmed": disarmed}


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


async def _h_script_set_enabled(p: dict, *, agent: str | None = None) -> dict:
    """Enable / disable a script. The USER enabling a script an AI agent
    wrote is the review step: it clears needs_review (they flipped it on in
    the Scripts page, with the code in front of them). An agent (`agent` set
    by mcp_tools, never by the RPC dispatcher) can never enable code that is
    still waiting for that review."""
    sid = str(p.get("id") or "")
    enabled = bool(p.get("enabled"))
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        if enabled:
            if agent and row.get("needs_review"):
                raise ValueError("An AI agent wrote this script and the user has not "
                                 "reviewed it yet. Only the user can enable it, from "
                                 "the Scripts page.")
            errors = script_sandbox.validate(str(row.get("code") or ""))
            if errors:
                raise ValueError("script does not validate: " + errors[0])
            errors = _agent_code_errors(row)
            if errors:
                raise ValueError("this agent-written script breaks the agent-script "
                                 "rules (edit it to make it yours, or delete it): "
                                 + errors[0])
            if agent:
                db.update_user_script(conn, sid, enabled=1, last_error=None)
            else:
                db.update_user_script(conn, sid, enabled=1, last_error=None,
                                      needs_review=0)
        else:
            db.update_user_script(conn, sid, enabled=0)
        row = db.get_user_script(conn, sid)
    return {"script": _script_row_js(row or {})}


async def _h_script_set_assets(p: dict) -> dict:
    """Set a script's OWN coin scope (null/[] = every coin).

    This replaces the crypto15m_assets coupling: which coins a script sees is
    now its own property, so the 15m Crypto tab's asset checkboxes (and any
    preset applied there) can never silently starve a script again."""
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
    """Validate code (editor button + the AI-output import path). Returns
    parsed metadata so the import flow can prefill name/description."""
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


async def _h_script_backtest(p: dict, *, agent: str | None = None) -> dict:
    """Backtest a script (by id, or raw code from the editor) over the
    recorded tick corpus. Never places orders — script_backtest is
    structurally order-free.

    Where the code came from decides where it runs. The user's own code runs
    here, in-process, as it always has. Code an AI agent wrote — passed by
    an agent (`agent`, set by mcp_tools only) or a saved row an agent last
    changed, even when the USER clicks Backtest on it — runs in the
    isolated child, and agent code must pass the agent allowlist first."""
    import script_audit
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
    isolate = bool((p or {}).get("isolate")) or bool(agent)
    row = None
    if p.get("id"):
        with db.get_db() as conn:
            row = db.get_user_script(conn, str(p.get("id") or ""))
    if not code:
        if not row:
            raise ValueError("script not found")
        code = _sanitize_script_code(row.get("code"))
        if assets is None:
            assets = row.get("assets")
    agent_code = bool(agent and p.get("code")) or (
        _is_agent_author(row) and code == _sanitize_script_code((row or {}).get("code")))
    if agent_code:
        isolate = True
        bad = script_audit.validate_agent(code)
        if bad:
            raise ValueError("Scripts written by an AI agent may only use plain "
                             "computation (see get_script_docs). Refused: "
                             + "; ".join(bad[:5]))
    if isolate:
        return await _script_backtest_isolated(cfg, str(code), assets, env, since)
    return await asyncio.to_thread(
        script_backtest.run, cfg, str(code),
        assets=assets, env=env, since_days=since,
    )


_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.I)
_PROFILE_ENV = ("KRYPT_POLYBOT_USERDATA", "KRYPT_POLYBOT_VAULT", "APPDATA",
                "LOCALAPPDATA", "USERPROFILE", "HOME")


def _backtest_child_env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    for k in _PROFILE_ENV:
        d = os.path.join(tmp, "profile", k.lower())
        os.makedirs(d, exist_ok=True)
        env[k] = d
    env["PYTHONIOENCODING"] = "utf-8"
    if not getattr(sys, "frozen", False):
        paths = [str(Path(__file__).resolve().parent)]
        paths += [p for p in sys.path if p and "site-packages" in p]
        env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _backtest_child_cmd() -> list[str]:
    """The child's command line. The frozen backend is one process (a
    PyInstaller onedir build runs Python in-process). A Windows venv's
    python.exe is a LAUNCHER that starts the base interpreter as a second
    process, which the child's job object (one process, no children) refuses,
    so dev and tests start the base interpreter directly."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--backtest-child"]
    exe = sys.executable
    base = getattr(sys, "_base_executable", None)
    if sys.platform == "win32" and base and sys.prefix != sys.base_prefix:
        exe = base
    return [exe, str(Path(__file__).resolve()), "--backtest-child"]


async def _script_backtest_isolated(
    cfg: dict, code: str, assets, env: str, since: int, *, db_file: str | None = None,
) -> dict:
    """Run a script backtest in a child backend process with no credentials
    (script_backtest_child.py): how an AI agent's backtest_script runs the
    code it wrote. Killed if it outlives the backtest's own wall-clock cap.

    The child never sees the real database: `db_file` (default: the app's
    DB) is only the SOURCE of a read-only extract built here, in the child's
    own temporary directory. On Windows the child runs in a Job Object that
    forbids it starting any process."""
    import shutil
    import subprocess
    import tempfile

    import script_backtest
    import script_backtest_child
    cmd = _backtest_child_cmd()
    tmp = tempfile.mkdtemp(prefix="kp-backtest-")
    job = None
    try:
        src = Path(db_file) if db_file else db.db_path()
        extract = Path(tmp) / "data" / "backtest-extract.db"
        await asyncio.to_thread(
            script_backtest_child.build_extract, src, extract, int(since), str(env),
            src.with_name(db.RESEARCH_ARCHIVE_NAME))
        child_env = _backtest_child_env(tmp)
        safe_cfg = {k: v for k, v in cfg.items()
                    if not k.startswith(("mcp_", "remote_", "ai_", "autopilot_"))}
        payload = json.dumps({
            "db": str(extract), "cfg": safe_cfg, "code": code,
            "assets": assets, "env": env, "sinceDays": since,
        }, default=str).encode("utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=child_env, cwd=tmp,
            creationflags=flags,
        )
        try:
            job = script_backtest_child.contain(proc.pid)
        except OSError as e:
            logger.warning(f"[scripts] backtest child not contained in a job object: {e}")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(payload), timeout=script_backtest.MAX_RUN_SECS + 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"scriptError": "the backtest ran past its time limit and was stopped",
                    "n": 0, "trades": [], "caveats": ["Stopped: over the time limit."]}
        try:
            return json.loads(out.decode("utf-8"))
        except ValueError:
            tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
            return {"scriptError": "the isolated backtest exited without a result"
                                   + (f": {' | '.join(tail)}" if tail else ""),
                    "n": 0, "trades": [], "caveats": []}
    finally:
        script_backtest_child.release(job)
        shutil.rmtree(tmp, ignore_errors=True)


async def _h_script_context_pack(_p: dict) -> dict:
    import script_docs
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    env = polymarket_auth.get_env()
    text = await asyncio.to_thread(script_docs.build_context_pack, cfg, env)
    return {"text": text}


async def _h_script_api_docs(_p: dict) -> dict:
    """Structured script-API reference for the in-app docs panel. Rendered
    FROM the live modules (same sources as the context pack) so the panel can
    never drift from what the engine/backtester actually do."""
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
    """Compare this Polymarket market with its Kalshi counterpart.

    Read-only on both venues. The first call warms Kalshi's series index
    (~11,800 rows, a few seconds) and later ones are nearly free — which is
    why this is opt-in from the market page rather than part of any poll.
    """
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
    """Send a hand-placed order.

    Gated on the SAME live-trading switch as every engine. A terminal that
    would place an order while the app says PAUSED is lying about its own
    state, and the user would have no way to stop it.
    """
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
    """Sell every open position in the wallet — or, with ``preview``, price it.

    The preview half is read-only, so it is not gated: showing a user what
    they hold and what it would fetch spends nothing. The half that SENDS is
    gated exactly like `_h_terminal_place` — paper mode, the live-trading
    switch, a connected wallet — because one button that closes everything is
    still one button that signs orders.
    """
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
    """Price a one-click exit out of one position. Read-only, so ungated:
    showing a user what a position would fetch spends nothing.

    The `paper` flag is forced from the CONFIG, never trusted from the
    renderer — the panel must price the same book the Sell button will send
    against, and a renderer that disagreed with Settings about which mode it
    is in would show paper numbers over a live sell.
    """
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    req = dict(p.get("order") or {})
    req["paper"] = bool(cfg.get("terminal_paper_mode"))
    return await terminal_trade.sell_position_preview(req)


async def _h_terminal_sell_position(p: dict) -> dict:
    """Send the one-click exit. Gated exactly like `_h_terminal_place`.

    Paper mode is not a refusal here — it is a ROUTE: the panel's button says
    "Sell paper" and the order goes to the simulator. What must never happen
    is the two disagreeing, so the mode is read from the config in main and
    the renderer's flag is ignored.
    """
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
    """Simulate a hand-placed order. Needs no wallet and no live-trading
    switch: the entire point is to practise before funding anything."""
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
    """Resting orders. With a `conditionId` the list is narrowed to that
    market and each row carries the touch it would have to reach to fill —
    what the Market page's working-order rows show. Without one it is the
    Track-wide list, unchanged and with no extra book reads."""
    import terminal_trade
    cid = str((p or {}).get("conditionId") or "").strip() or None
    return await terminal_trade.resting_orders(cid)


async def _h_terminal_portfolio(_p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.portfolio()


async def _h_terminal_settled(_p: dict) -> dict:
    """The settled RECORD from the fill ledger — deliberately its own call.

    Kept off `terminalPortfolio` so the 5 s positions poll stays a wallet read
    and never pays for the ledger walk; this one runs when the user opens the
    settled table.
    """
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
    """Start or stop each bot to match config. Idempotent — safe to call on
    every config change and on a timer."""
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
    """Verify a token BEFORE storing it, so a typo fails on the setup screen
    rather than as a bot that silently never connects."""
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
    """Add an id by hand — the Discord path, where the user can copy their own
    user id out of the client in two clicks."""
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
    import mcp_rails
    import mcp_server
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    st = mcp_server.status(cfg)
    mode = mcp_rails.trade_mode(cfg)
    if mode in ("paper", "live"):
        try:
            loss = await asyncio.wait_for(mcp_rails.day_loss(mode, cfg), 8.0)
            st["lossTodayUsd"] = (None if loss.get("positionsUnreadable")
                                  else loss.get("lossUsd"))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"mcp status: loss read failed: {e}")
    return st


async def _h_mcp_sync(_p: dict) -> dict:
    return await _mcp_sync()


def _agent_id_arg(p: dict) -> str:
    """An agent id from the renderer, validated, never defaulted to another
    agent by accident: absent means Default, malformed is refused."""
    import mcp_agents
    aid = str((p or {}).get("agentId") or mcp_agents.DEFAULT_ID)
    if aid != mcp_agents.DEFAULT_ID and not mcp_agents.valid_id(aid):
        raise ValueError("bad agent id")
    return aid


async def _h_mcp_rotate_token(p: dict) -> dict:
    import mcp_agents
    import mcp_server
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    aid = _agent_id_arg(p)
    if mcp_agents.get(cfg, aid) is None:
        raise ValueError("no such agent")
    mcp_server.rotate_token(aid)
    return mcp_server.status(cfg)


async def _h_mcp_http_snippet(p: dict) -> dict:
    """curl + Python for the HTTP API, carrying one agent's token. The ONLY
    RPC whose answer holds a token: Electron main puts the text on the
    clipboard and hands the renderer {ok} — the renderer never sees it."""
    import mcp_agents
    import mcp_server
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    aid = _agent_id_arg(p)
    agent = mcp_agents.get(cfg, aid)
    if agent is None:
        raise ValueError("no such agent")
    token = await asyncio.to_thread(mcp_server.ensure_token, aid)
    return {"text": mcp_server.http_snippet(int(cfg.get("mcp_port") or 47615), token,
                                            agent["name"])}


async def _h_mcp_seen(_p: dict) -> dict:
    """Agent clients seen this session. Memory only: no disk, no network, so
    onboarding may poll it."""
    import mcp_server
    return mcp_server.seen()


async def _h_mcp_agent_close_paper(p: dict) -> dict:
    """The USER closes one agent's paper positions at the bid (e.g. after
    deleting or switching it off). Not an MCP tool: no agent can reach it."""
    import mcp_rails
    return await mcp_rails.close_agent_paper(_agent_id_arg(p))


async def _h_paper_status(_p: dict) -> dict:
    """The ACCOUNT's paper book (the terminal's 'manual' book) for the
    Paper | Live panel."""
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    with terminal_paper.using_book("manual"):
        pf = await terminal_paper.portfolio(cfg)
        resting = sum(1 for o in terminal_paper.orders(500)["orders"]
                      if o["status"] in ("resting", "partial"))
    try:
        has_wallet = bool(polymarket_auth.credentials_present())
    except Exception:  # noqa: BLE001
        has_wallet = False
    return {
        "accountMode": cfg.get("account_mode"),
        "bankrollUsd": pf.get("bankrollUsd"),
        "nextBankrollUsd": cfg.get("terminal_paper_bankroll_usd"),
        "cashUsd": pf.get("cashUsd"),
        "equityUsd": pf.get("equityUsd"),
        "realizedPnlUsd": pf.get("realizedPnlUsd"),
        "openPositions": len(pf.get("positions") or []),
        "restingOrders": resting,
        "hasWalletKey": has_wallet,
        "fetchedAt": pf.get("fetchedAt"),
    }


async def _h_paper_reset(_p: dict) -> dict:
    """Wipe the account's paper book back to its bankroll. Agents' own paper
    books are untouched (mcpPaper resets those)."""
    import terminal_paper
    with terminal_paper.using_book("manual"):
        res = terminal_paper.reset()
    return {"ok": True, **res}



async def _health_wallet(deep: bool, cfg: dict) -> dict:
    import health
    from config import account_live
    st = await asyncio.to_thread(polymarket_auth.credentials_status)
    live = account_live(cfg)
    if not (deep and st.get("hasWalletKey")):
        return health.check_wallet(st, STATE.auth_ok, account_live=live)
    try:
        await asyncio.wait_for(_h_testCredentials({}), 45.0)
    except Exception as e:  # noqa: BLE001
        return health.check_wallet(st, STATE.auth_ok, account_live=live, probed=True,
                                   probe_ok=False, probe_error=e)
    return health.check_wallet(st, STATE.auth_ok, account_live=live, probed=True,
                               probe_ok=True)


async def _health_ai(deep: bool, cfg: dict) -> list:
    """Guarded throughout: the provider check is ai_analysis.check_provider
    (Settings' "Test connection", free endpoints only) when this build has
    it, else ai_providers.check, else no probe at all. Only the SELECTED
    provider is ever contacted."""
    import health
    try:
        import ai_providers as prov_mod  # type: ignore
    except Exception:  # noqa: BLE001
        prov_mod = None
    import ai_analysis
    import ai_secrets
    providers = list(getattr(prov_mod, "PROVIDERS", None) or getattr(ai_analysis, "PROVIDERS", ()))
    active = str(cfg.get("ai_provider") or (providers[0] if providers else "anthropic"))
    check = None
    _cp = (getattr(ai_analysis, "check_provider", None)
           or getattr(prov_mod, "check_provider", None))
    if callable(_cp):

        def check(p, c):  # noqa: F811
            return _cp(p, c)
    elif prov_mod is not None and callable(getattr(prov_mod, "check", None)):
        _ck = prov_mod.check

        def check(p, c):  # noqa: F811
            return _ck(p, ai_secrets.load_key(p) if ai_secrets.has_key(p) else None,
                       str(c.get("ai_model") or "") or None)
    can_probe = check is not None

    def _is_local(p: str) -> bool:
        nk = getattr(ai_analysis, "needs_key", None) or getattr(prov_mod, "needs_key", None)
        if callable(nk):
            return not nk(p)
        caps = getattr(prov_mod, "capabilities", None)
        if callable(caps):
            return not caps(p).get("needsKey", True)
        return False
    out = []
    for prov in providers:
        try:
            local = _is_local(prov)
        except Exception:  # noqa: BLE001
            local = False
        if local and prov != active:
            continue
        try:
            has = True if local else bool(ai_secrets.has_key(prov))
        except Exception:  # noqa: BLE001
            has = False
        needed = prov == active and bool(cfg.get("autopilot_enabled"))
        if not (deep and has and can_probe):
            out.append(health.check_ai(prov, active=prov == active, has_key=has,
                                       can_probe=can_probe, local=local, needed=needed))
            continue
        if prov != active:
            out.append(health.check_ai(prov, active=False, has_key=True, can_probe=True,
                                       local=local, idle=True))
            continue
        try:
            import inspect
            if inspect.iscoroutinefunction(check):
                res = await asyncio.wait_for(check(prov, cfg), 30.0)
            else:
                res = await asyncio.wait_for(asyncio.to_thread(check, prov, cfg), 30.0)
            out.append(health.check_ai(prov, active=True, has_key=True, can_probe=True,
                                       probed=True, local=local,
                                       probe=res if isinstance(res, dict) else {}))
        except Exception as e:  # noqa: BLE001
            out.append(health.check_ai(prov, active=True, has_key=True, can_probe=True,
                                       probed=True, probe_error=e, local=local))
    return out


async def _health_agents(deep: bool, cfg: dict) -> list:
    import health
    import mcp_agents
    import mcp_server
    st = await asyncio.to_thread(mcp_server.status, cfg)
    seen = mcp_server.seen().get("clients") or []
    st["lastSeen"] = {"client": seen[0]["client"], "at": seen[0]["at"]} if seen else None
    rows = [health.check_mcp(st)]
    probe = None
    if deep and st.get("enabled") and st.get("httpEnabled") and st.get("running"):
        token = None
        for a in mcp_agents.agents(cfg):
            if a.get("enabled", True):
                token = await asyncio.to_thread(mcp_server.load_token, a["id"])
                if token:
                    break
        probe = (await health.probe_http(int(st["port"]), token) if token
                 else {"ok": False, "status": 401, "error": "no token"})
    rows.append(health.check_http(st, probed=probe is not None, probe=probe))
    return rows


def _clob_feed_stats() -> dict | None:
    try:
        import clob_ws
        f = clob_ws.feed
        task = getattr(f, "_task", None)
        running = task is not None and not task.done()
        now = time.monotonic()
        ages = [now - float(b.get("ts") or 0.0) for b in list(getattr(f, "_books", {}).values())]
        fresh = sum(1 for a in ages if a < 60)
        return {"connected": running, "books": fresh,
                "lastMsgAgeSec": round(min(ages), 1) if ages and running else None}
    except Exception:  # noqa: BLE001
        return None


def _health_local(cfg: dict) -> list:
    import health
    import remote
    from config import engine_on
    rows = []
    try:
        import autopilot  # type: ignore
        ap = autopilot.status(cfg)
    except Exception:  # noqa: BLE001
        ap = None
    rows.append(health.check_autopilot(ap))
    rs = remote.status()
    for which in ("discord", "telegram"):
        d = rs.get(which) or {}
        rows.append(health.check_remote(
            which, enabled=bool(cfg.get("remote_enabled")) and bool(cfg.get(f"remote_{which}_enabled")),
            has_token=bool(d.get("hasToken")), paired=bool(d.get("paired")),
            bot={"connected": bool(d.get("connected")), "running": bool(d.get("connected")),
                 "lastError": d.get("lastError"),
                 "botName": d.get("botUsername")}))
    needed = any(engine_on(cfg, k) for k in ("enable_trading", "crypto15m_enabled",
                                             "copy_enabled", "scripts_live_enabled"))
    rows.append(health.check_feed(_clob_feed_stats(), needed=needed))
    try:
        import spot_ws
        spot = spot_ws.stats()
    except Exception:  # noqa: BLE001
        spot = None
    rows.append(health.check_feed(spot, needed=engine_on(cfg, "crypto15m_enabled"),
                                  rid="spot", label="Crypto spot feed"))
    return rows


async def _h_health_check(p: dict) -> dict:
    import health
    deep = (p or {}).get("deep") is True
    cfg = merge_with_defaults(dict(STATE.cfg or {}))

    async def _guard(label: str, coro) -> list:
        try:
            r = await coro
            return r if isinstance(r, list) else [r]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[health] {label} check failed: {type(e).__name__}: {e}")
            return [health.row(label.lower().replace(" ", "-"), label, health.FAIL,
                               f"The check itself failed: {type(e).__name__}.",
                               "Copy diagnostics from the Logs page if this persists.")]

    async def _local() -> list:
        return await asyncio.to_thread(_health_local, cfg)

    parts = await asyncio.gather(
        _guard("Wallet", _health_wallet(deep, cfg)),
        _guard("AI providers", _health_ai(deep, cfg)),
        _guard("AI agents", _health_agents(deep, cfg)),
        _guard("Local services", _local()),
    )
    rows = [r for part in parts for r in part]
    if deep:
        bad = [r["id"] for r in rows if r["status"] in (health.FAIL, health.WARN)]
        logger.info(f"[health] checks run: {len(rows)} rows, attention: {bad or 'none'}")
    return {"checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "deep": deep, "rows": rows, "counts": health.summarize(rows)}


async def _h_mcp_paper(p: dict) -> dict:
    """One agent's paper account, for the AI Agents page."""
    import mcp_agents
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    with terminal_paper.using_book(mcp_agents.paper_book(_agent_id_arg(p))):
        if (p or {}).get("reset"):
            terminal_paper.reset()
        return {"portfolio": await terminal_paper.portfolio(cfg),
                "orders": terminal_paper.orders(int((p or {}).get("limit") or 50))}


async def _h_mcp_decide(p: dict) -> dict:
    """The user's answer to a live agent order waiting for approval."""
    import mcp_rails
    try:
        rid = int((p or {}).get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "message": "No such agent order."}
    return await mcp_rails.decide(rid, bool((p or {}).get("approve")), via="app")


async def _h_mcp_settle_unknown(p: dict) -> dict:
    """The user settles one live agent order booked 'unknown': Polymarket is
    asked what became of it, and it is booked at that (mcp_rails.settle_unknown)."""
    import mcp_rails
    return await mcp_rails.settle_unknown((p or {}).get("id"))


async def _h_mcp_activity(p: dict) -> dict:
    """Every agent order (paper and live, sent, refused, queued) and the
    approvals waiting, newest first."""
    import mcp_rails
    limit = max(1, min(500, int((p or {}).get("limit") or 100)))
    return {"orders": mcp_rails.activity(limit), "pending": mcp_rails.pending()}


async def _h_ai_scoreboard(p: dict) -> dict:
    """The agents' forecast record against the market's own price."""
    import forecast_ledger
    return forecast_ledger.scoreboard(recent=max(1, min(200, int((p or {}).get("recent") or 40))))


async def _h_remote_test(_p: dict) -> dict:
    """Push a line to every paired account, so the user can confirm the round
    trip works before relying on it."""
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
    return ai_analysis.status(merge_with_defaults(dict(STATE.cfg or {})))


async def _h_ai_analyze(p: dict) -> dict:
    """Analyse one market with the configured provider.

    On-demand only — there is no scheduler, no tick and no engine path that
    reaches this, because every call spends the user's own provider credit.

    The ai_enabled gate lives in ai_analysis.analyze, next to the code that
    would do the sending, rather than being re-asserted here.
    """
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
    """Verify a key BEFORE storing it, so a typo fails on the setup screen
    rather than as a feature that silently never works. Same rule as the
    chat-bot tokens."""
    import ai_analysis
    import ai_secrets
    provider = str(p.get("provider") or "").strip().lower()
    key = str(p.get("key") or "").strip()
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "provider": provider, "hasKey": False,
                "message": f"Unknown provider {provider!r}."}
    if not ai_secrets.stores_key(provider):
        return {"ok": False, "provider": provider, "hasKey": False,
                "message": f"{provider} runs on this machine and takes no key."}
    if not key:
        ai_secrets.clear_key(provider)
        logger.info(f"[ai] {provider} API key cleared")
        return {"ok": True, "provider": provider, "hasKey": False,
                "message": "Key cleared.", "models": [], "masked": None}

    previous = ai_secrets.load_key(provider)
    ai_secrets.save_key(provider, key)
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    res = await asyncio.to_thread(ai_analysis.check_provider, provider, cfg)
    if not res.get("ok") and res.get("reason") != "model":
        if previous:
            ai_secrets.save_key(provider, previous)
        else:
            ai_secrets.clear_key(provider)
        return {"ok": False, "provider": provider,
                "hasKey": ai_secrets.has_key(provider),
                "message": res.get("message") or f"{provider} rejected that key."}
    logger.info(f"[ai] {provider} API key saved")
    models = res.get("models") or []
    return {
        "ok": True,
        "provider": provider,
        "hasKey": True,
        "message": f"Key verified. {len(models)} models available.",
        "models": models,
        "masked": ai_secrets.masked(provider),
    }


async def _h_ai_list_models(p: dict) -> dict:
    """{ok, provider, models: [id], modelInfo: [{id, tools, context,
    maxContext, loaded}], message?}. Free listing endpoints only; local
    providers say honestly that their server is not running."""
    import ai_analysis
    import ai_providers
    provider = str(p.get("provider") or "").strip().lower()
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "provider": provider,
                "message": f"Unknown provider {provider!r}.", "models": [], "modelInfo": []}
    try:
        models = await ai_analysis.list_models(provider)
    except Exception as e:  # noqa: BLE001
        reason = getattr(e, "reason", None) or f"{type(e).__name__}: {e}"
        return {"ok": False, "provider": provider, "message": reason,
                "models": [], "modelInfo": []}
    info = [m.public() for m in ai_providers.cached_models(provider) if m.id in models]
    return {"ok": True, "provider": provider, "models": models, "modelInfo": info}


async def _h_ai_check_provider(p: dict) -> dict:
    """Settings' "Test connection". The renderer names a provider id; the
    host, the key and the endpoint are all chosen here. Free endpoints only
    (model listings), so pressing it never bills the user."""
    import ai_analysis
    provider = str((p or {}).get("provider") or "").strip().lower()
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "provider": provider,
                "message": f"Unknown AI provider: {provider or '—'}"}
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    res = await asyncio.to_thread(ai_analysis.check_provider, provider, cfg)
    logger.info(f"[ai] connection check {provider}: {'ok' if res.get('ok') else 'failed'}")
    return res


async def _h_autopilot_status(_p: dict) -> dict:
    """kryptrader's AutopilotStatus: the run state, budgets, today's usage,
    the agent it runs as, and the last 30 runs."""
    import autopilot
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    st = await asyncio.to_thread(autopilot.status, cfg)
    st["runs"] = await asyncio.to_thread(autopilot.runs, 30)
    return st


async def _h_autopilot_run_now(_p: dict) -> dict:
    """A run on the user's click. Started in the background: the renderer gets
    an answer now and watches the run on the status poll. Every refusal —
    deleted/disabled agent, missing key, a budget, a model that cannot call
    tools — comes back HERE as the reason, before anything is recorded."""
    import autopilot
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    if autopilot.STATE.running:
        return {"ok": False, "message": "Autopilot is already running."}
    why = autopilot.blocked_reason(cfg)
    if not why:
        why = await asyncio.to_thread(autopilot.tool_refusal, cfg)
    if why:
        autopilot.STATE.last_error = why
        return {"ok": False, "message": why}
    t = asyncio.create_task(autopilot.run_once(cfg, "manual"))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return {"ok": True, "message": "Autopilot run started."}


_HANDLERS = {
    "ping": _h_ping,
    "aiStatus": _h_ai_status,
    "aiAnalyze": _h_ai_analyze,
    "aiSetKey": _h_ai_set_key,
    "aiListModels": _h_ai_list_models,
    "aiCheckProvider": _h_ai_check_provider,
    "autopilotStatus": _h_autopilot_status,
    "autopilotRunNow": _h_autopilot_run_now,
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
    "mcpHttpSnippet": _h_mcp_http_snippet,
    "mcpSeen": _h_mcp_seen,
    "mcpAgentClosePaper": _h_mcp_agent_close_paper,
    "paperStatus": _h_paper_status,
    "paperReset": _h_paper_reset,
    "healthCheck": _h_health_check,
    "mcpPaper": _h_mcp_paper,
    "mcpDecide": _h_mcp_decide,
    "mcpSettleUnknown": _h_mcp_settle_unknown,
    "mcpActivity": _h_mcp_activity,
    "aiScoreboard": _h_ai_scoreboard,
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
    """Push the user's offline-mode switch into the terminal provider layer.

    Done on dispatch rather than only on setConfig so the switch is honoured
    even if the backend restarted, or the config arrived by some path that did
    not go through setConfig. Cheap: a dict read and a bool assignment.
    """
    try:
        import terminal
        cfg = STATE.cfg or {}
        terminal.set_offline(bool(cfg.get("terminal_offline_mode", False)))
    except Exception:
        pass


_SECRET_CHANGING_RPCS = frozenset({
    "setCredentials", "clearCredentials", "testCredentials", "aiSetKey",
    "remoteSetToken", "mcpRotateToken", "mcpHttpSnippet", "perpsLink",
    "perpsUnlink", "factoryReset",
})


def _refresh_log_scrubber() -> None:
    try:
        import logscrub
        logscrub.refresh_known_secrets()
    except Exception:  # noqa: BLE001 — never let scrubber upkeep break an RPC
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
        if method in _SECRET_CHANGING_RPCS:
            _refresh_log_scrubber()
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
    """The authed startup round trips, in the fewest serial hops that are safe.

    Order is NOT free here:
      • cancel-all-resting FIRST, alone. Cancelling a resting GTC both takes it
        off the book and releases its collateral, so the two calls after it
        depend on it having finished — the reconcile would otherwise inherit a
        still-resting stale order as an unmanaged external position, and the
        balance would report collateral that is about to come back as cash.
      • reconcile + refresh_balance are independent of EACH OTHER (one reads
        /portfolio/positions and writes local rows, the other reads /balance +
        /value; neither consumes the other's result), so they go out together
        — four serial round trips at startup become three.

    Returns the two gathered results (an exception instance instead of a value
    for whichever failed); the caller keeps each one's own error handling, so a
    failed balance can't swallow the reconcile's events or vice versa.
    """
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
    import mcp_server as _mcp_server
    _mcp_server.set_event_callback(emit_event)
    _refresh_log_scrubber()
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
    """Frozen-build smoke test: confirm the core deps AND the deposit-wallet
    order signer are importable + functional in THIS build. Prints a short
    report; exit 0 = all good, 1 = something's missing (e.g. the PyInstaller
    bundle dropped py_clob_client_v2 → the app couldn't sign a single order).
    Run:  krypt-polybot-backend(.exe) --selftest"""
    import shutil
    import tempfile
    iso = tempfile.mkdtemp(prefix="kp-selftest-profile-")
    os.environ["KRYPT_POLYBOT_USERDATA"] = os.path.join(iso, "userdata")
    os.environ["KRYPT_POLYBOT_VAULT"] = os.path.join(iso, "vault")
    try:
        return _selftest_checks()
    finally:
        try:
            db.close_thread_connection()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(iso, ignore_errors=True)


def _selftest_checks() -> int:
    checks: list[tuple[str, bool, str]] = []

    def _try(name: str, fn) -> None:
        try:
            fn()
            checks.append((name, True, ""))
        except Exception as e:  # noqa: BLE001 — report every failure mode
            checks.append((name, False, f"{type(e).__name__}: {e}"))

    _try("import eth_account", lambda: __import__("eth_account"))
    _try("import httpx", lambda: __import__("httpx"))
    _try("import cryptography", lambda: __import__("cryptography"))
    _try("sign EOA + POLY_1271 orders = known vectors (offline)", polymarket_auth.verify_signer)

    def _session_envelope_check() -> None:
        w = polymarket_auth.wrap_session_signer_signature("0x" + "ab" * 20, "0x" + "11" * 65)
        if not w.endswith(polymarket_auth.SESSION_SIGNER_MAGIC.hex()):
            raise RuntimeError("session envelope lacks the 0x6492 magic")
    _try("session-key order envelope (eth_abi)", _session_envelope_check)
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
        """The gate that decides whether a stranger can move your money.

        An unpaired sender must reach NO command, and remote trading must
        require BOTH switches. If either regresses the build stops here — this
        is not something to discover from a user's chat log.
        """
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
        """The guard that stops a fictional arbitrage.

        Caught live: "Fed INCREASE rates by 25bps" matched Kalshi's "CUT
        25bps" at high confidence and printed a 33c spread between two
        mutually exclusive markets. If the polarity rule regresses, that comes
        straight back — so the build stops here.
        """
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
    _try("import ai_providers", lambda: __import__("ai_providers"))
    _try("import autopilot", lambda: __import__("autopilot"))
    _try("import anthropic SDK", lambda: __import__("anthropic"))
    _try("import openai SDK", lambda: __import__("openai"))

    def _ai_no_estimate_check() -> None:
        """The rule the AI panel rests on, proven in THIS build.

        The model must never return a probability or a fair value, and the
        guarantee is structural: a closed schema with no numeric field, so
        the provider's own structured-output layer refuses one. If a numeric
        property is ever added, a language model's made-up price starts
        rendering next to real quotes — so the build stops here.
        """
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
        assert ai._conform({"lean": "cheap", "probability": "high"}),             "a reply with an extra field passed the schema check"
        assert ai._conform({"reasoning": ["x"], "lean": "cheap", "confidence": "low",
                            "summary": 0.7}), "a numeric field passed the schema check"
        assert ai._conform({"drivers": ["a", 70]}), "a number in a list passed the schema check"
        assert ai._conform({"summary": "s", "lean": "fair"}) is None,             "a clean reply failed the schema check"
        assert set(ai.PROVIDERS) == {"anthropic", "openai", "openrouter", "gemini",
                                     "ollama", "lmstudio"}, "AI provider set changed"
        gs = __import__("ai_providers").gemini_schema(ai._SCHEMA)
        for name, spec in gs["properties"].items():
            assert spec.get("type") not in ("number", "integer"),                 f"Gemini response schema exposes a numeric field: {name}"

    _try("AI analysis: no-estimate + no-account-leak", _ai_no_estimate_check)

    def _terminal_honesty_check() -> None:
        """The honest-null rule, proven in THIS build.

        These are the two behaviours the whole terminal rests on, and both
        have failed silently before in other products: a missing metric
        coerced to zero, and a filter that hides the row it could not
        evaluate. If either regresses, the build stops here.
        """
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
        """The AI-agent server's safety rails, proven in THIS build.

        A frozen build that dropped mcp_tools would answer every agent with
        "unknown tool"; one where the loopback checks regressed would let a
        web page in the browser reach the wallet.
        """
        srv = __import__("mcp_server")
        tools = __import__("mcp_tools")
        rails = __import__("mcp_rails")
        agents = __import__("mcp_agents")
        __import__("mcp_stdio")
        __import__("forecast_ledger")
        listed = {t["name"]: t for t in tools.list_tools()}
        for name in ("get_status", "get_my_agent", "record_forecast",
                     "place_order", "backtest_crypto15m", "save_script",
                     "pause_all_engines", "update_config", "research_data",
                     "close_position"):
            assert name in listed, f"MCP tool missing: {name}"
        assert len(listed) <= 40, "over 40 MCP tools — some clients drop the rest"
        assert "forecastId" in listed["place_order"]["inputSchema"]["properties"], \
            "place_order lost its forecast citation"
        assert srv._host_ok("127.0.0.1:47615") and srv._host_ok("localhost")
        assert not srv._host_ok("evil.example:47615"), "DNS-rebinding guard regressed"
        cfg = {"mcp_allow_live_switches": False, "mcp_scripts_enabled": False}
        for gate in ("switches", "scripts"):
            try:
                tools._require(cfg, gate)
            except tools.Refused:
                continue
            raise AssertionError(f"MCP '{gate}' gate is open with its switch off")
        a = agents.default_agent("live")
        assert agents.effective_mode("paper", a) == "paper"
        assert rails.trade_mode({"mcp_trade_mode": "live",
                                 "terminal_paper_mode": True}) == "paper"
        loose = {**a, "rules": agents.clean_rules({"minEdgeCents": 0, "maxOrderUsd": 1e6})}
        er = agents.effective_rails({"maxOrderUsd": 25.0, "dailySpendUsd": 100.0,
                                     "minEdgeCents": 3.0}, loose)
        assert er["maxOrderUsd"] == 25.0 and er["minEdgeCents"] == 3.0, \
            "an agent's rules widened a global rail"

    _try("MCP server: tools, loopback guard, gates", _mcp_check)

    def _agent_doors_check() -> None:
        """The HTTP API, the tool-call stream, the log scrubber, health and
        the account gate, proven in THIS build. Each module is lazy-imported,
        so a bundle that dropped one would fail only when a user reached it."""
        srv = __import__("mcp_server")
        ls = __import__("logscrub")
        hl = __import__("health")
        tools = __import__("mcp_tools")
        cfgmod = __import__("config")
        code, body, _ = asyncio.run(srv.handle_api("GET", "/api/v1/tools", b"", {}, "default"))
        assert code == 403 and body.get("ok") is False, "HTTP API answered while switched off"
        marker = "selftest-free-text-must-not-leak"
        ev = srv.build_call_event(
            name="record_forecast",
            args={"conditionId": marker, "rationale": marker, "model": marker + " kp_" + "Q7x" * 15,
                  "fairValueCents": 42},
            session={"client": "selftest", "agent": "default"}, res={"ok": True},
            outcome="ok", reason=None, started=time.monotonic(), mode="paper")
        assert marker not in json.dumps(ev), "tool-call stream forwarded a raw argument"
        assert ev["conditionId"] is None, "a non-conditionId passed the shape check"
        ev = srv.build_call_event(
            name="search_markets", args={"query": marker}, session={"client": "selftest"},
            res={"markets": []}, outcome="ok", reason=None, started=time.monotonic(), mode=None)
        assert marker not in json.dumps(ev), "search query reached the stream"
        tok = "kp_" + "Zx9" * 14
        assert tok not in ls.scrub(f"Authorization: Bearer {tok}"), "bearer token logged"
        assert ls.looks_secret(tok.upper()), "case-folded token not recognised"
        assert ls.scrub("conditionId 0x" + "ab" * 32) == "conditionId 0x" + "ab" * 32, \
            "the scrubber ate a conditionId"
        assert tools.classify("account_mode") == "protected"
        assert tools.classify("terminal_paper_mode") == "protected"
        unclassified = [k for k in cfgmod.DEFAULT_CONFIG if tools.classify(k) == "unknown"]
        assert not unclassified, f"unclassified config keys: {unclassified[:5]}"
        paper = cfgmod.merge_with_defaults({"account_mode": "paper", "enable_trading": True})
        assert paper["terminal_paper_mode"] is True and not cfgmod.engine_on(paper, "enable_trading")
        assert hl.check_http({"enabled": True, "httpEnabled": False})["status"] == hl.OFF

    _try("agent doors: HTTP API off, stream whitelist, log scrub, account gate",
         _agent_doors_check)
    _try("import health", lambda: __import__("health"))
    _try("import logscrub", lambda: __import__("logscrub"))

    def _isolated_backtest_check() -> None:
        db.init_db()
        db.close_thread_connection()
        f = str(db.db_path())
        code = "\n".join([
            "def on_start(state):",
            "    import socket",
            "    socket.create_connection(('example.com', 80))",
            "",
            "def decide(ctx):",
            "    return None",
            "",
        ])
        res = asyncio.run(_script_backtest_isolated(
            merge_with_defaults({}), code, None, "mainnet", 7, db_file=f))
        err = str(res.get("scriptError") or "")
        assert "network is off" in err, f"child did not refuse the network: {err or res}"

    _try("agent backtest runs isolated (child process, no network)", _isolated_backtest_check)

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
