"""Single SQLite store for the Krypt PolyBot backend.

Combines the tracker schema (markets, alerts, whale_trades, snapshots,
trades) with the live-trading schema (bot_positions, order_events,
pnl_snapshots, daily_stats) into one DB so that the Electron app has a
single backing file under <userData>/data/krypt-polybot.db.

This is a deliberate consolidation of the two separate DBs the
standalone scripts used to ship with — keeping one DB makes backup,
migration, and "wipe my data" all one button on the UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    base = os.environ.get("KRYPT_POLYBOT_USERDATA")
    if base:
        d = Path(base) / "data"
    else:
        d = Path(__file__).resolve().parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _vault_dir() -> Path | None:
    """Survival vault OUTSIDE the roaming userData dir, so recorded research
    data outlives anything that removes %APPDATA%/Krypt PolyBot — an
    uninstall with the "delete app data" box ticked, a reinstall gone wrong,
    a userData reset. Uninstallers never touch this path. One subdir per
    account (multi-account repoints userData at accounts/<name>)."""
    try:
        override = os.environ.get("KRYPT_POLYBOT_VAULT")
        if override:
            base = Path(override)
        else:
            local = os.environ.get("LOCALAPPDATA")
            base = (
                Path(local) / "Krypt PolyBot Vault"
                if local else Path.home() / ".krypt-polybot-vault"
            )
        tag = "default"
        ud = os.environ.get("KRYPT_POLYBOT_USERDATA")
        if ud:
            p = Path(ud)
            if p.parent.name.lower() == "accounts" and p.name:
                tag = p.name
        d = base / tag
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


_restore_checked = False


def _restore_latest_backup(dest: Path) -> None:
    """The DB file is gone (fresh install over a wiped userData, or the user
    deleted it): salvage the newest snapshot instead of silently starting
    from zero. Order: local data/backups (newest first) → the vault mirror."""
    candidates: list[Path] = []
    bdir = dest.parent / "backups"
    if bdir.is_dir():
        candidates += sorted(bdir.glob("research-*.db"), reverse=True)
    v = _vault_dir()
    if v is not None:
        vf = v / "research-latest.db"
        if vf.exists():
            candidates.append(vf)
    for c in candidates:
        try:
            if c.stat().st_size <= 0:
                continue
            for side in (".db-wal", ".db-shm"):
                sp = dest.with_name(dest.stem + side)
                if sp.exists():
                    sp.unlink()
            shutil.copy2(c, dest)
            logger.warning(f"database missing — restored {c.name} from {c.parent}")
            return
        except OSError:
            continue


def db_path() -> Path:
    global _restore_checked
    p = _data_dir() / "krypt-polybot.db"
    if not _restore_checked:
        _restore_checked = True
        if not p.exists():
            _restore_latest_backup(p)
    return p


def _open_conn(path: str) -> sqlite3.Connection:
    """A fresh connection with the four PRAGMAs this DB always runs under."""
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
    except BaseException:
        conn.close()
        raise
    return conn


_local = threading.local()


def _reuse_ok() -> bool:
    """True on threads where no coroutine can interleave with a `with` block."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def close_thread_connection() -> None:
    """Drop THIS thread's cached connection (no-op if it has none).

    Call before anything that needs no other handle on the file open: VACUUM's
    exclusive lock, the wal_checkpoint(TRUNCATE) after a wipe, and the corrupt-
    file quarantine, whose rename/unlink fail with WinError 32 on Windows while
    a handle is open. It can only reach the calling thread's connection.
    """
    conn = getattr(_local, "conn", None)
    _local.conn = None
    _local.path = None
    _local.busy = False
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def get_db():
    path = str(db_path())
    conn = None
    private = True
    busy = getattr(_local, "busy", False)
    if _reuse_ok() and not busy:
        conn = getattr(_local, "conn", None)
        if conn is not None and getattr(_local, "path", None) != path:
            close_thread_connection()
            conn = None
        if conn is None:
            conn = _open_conn(path)
            _local.conn = conn
            _local.path = path
        _local.busy = True
        private = False
    elif not busy and getattr(_local, "conn", None) is not None:
        close_thread_connection()
    if conn is None:
        conn = _open_conn(path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if private:
            conn.close()
        else:
            _local.busy = False



SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT DEFAULT '',
    series_ticker TEXT DEFAULT '',
    slug TEXT DEFAULT '',
    title TEXT DEFAULT '',
    yes_sub_title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    close_time TEXT DEFAULT '',
    volume REAL DEFAULT 0,
    volume_24h REAL DEFAULT 0,
    open_interest REAL DEFAULT 0,
    yes_bid REAL DEFAULT 0,
    yes_ask REAL DEFAULT 0,
    last_price REAL DEFAULT 0,
    prev_yes_bid REAL DEFAULT 0,
    prev_price REAL DEFAULT 0,
    result TEXT DEFAULT '',
    settlement_value REAL DEFAULT NULL,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_ticker TEXT PRIMARY KEY,
    series_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    sub_title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    count_fp REAL DEFAULT 0,
    yes_price REAL DEFAULT 0,
    no_price REAL DEFAULT 0,
    taker_side TEXT DEFAULT '',
    dollar_value REAL DEFAULT 0,
    category TEXT DEFAULT '',
    created_time TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    signal_type TEXT DEFAULT '',
    direction TEXT DEFAULT '',
    volume_24h REAL DEFAULT 0,
    price REAL DEFAULT 0,
    price_change REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    discord_sent INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    outcome_correct INTEGER DEFAULT NULL,
    resolved_price REAL DEFAULT NULL,
    pnl_estimate REAL DEFAULT NULL,
    resolved_at TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS whale_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    yes_sub_title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    taker_side TEXT DEFAULT '',
    count_fp REAL DEFAULT 0,
    price REAL DEFAULT 0,
    dollar_value REAL DEFAULT 0,
    market_volume REAL DEFAULT 0,
    open_interest REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    discord_sent INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    outcome_correct INTEGER DEFAULT NULL,
    resolved_price REAL DEFAULT NULL,
    pnl_estimate REAL DEFAULT NULL,
    resolved_at TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    volume REAL DEFAULT 0,
    volume_24h REAL DEFAULT 0,
    open_interest REAL DEFAULT 0,
    yes_bid REAL DEFAULT 0,
    last_price REAL DEFAULT 0,
    snapshot_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bot_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_source TEXT NOT NULL,
    signal_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT DEFAULT '',
    title TEXT DEFAULT '',
    category TEXT DEFAULT '',
    direction TEXT NOT NULL,
    action TEXT DEFAULT 'buy',
    target_contracts INTEGER NOT NULL,
    limit_price_cents INTEGER NOT NULL,
    filled_contracts INTEGER DEFAULT 0,
    avg_fill_price_cents REAL DEFAULT NULL,
    cost_usd REAL DEFAULT 0,
    fees_usd REAL DEFAULT 0,
    client_order_id TEXT UNIQUE NOT NULL,
    order_id TEXT DEFAULT NULL,
    status TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    edge_pts REAL DEFAULT 0,
    signal_price REAL DEFAULT 0,
    error TEXT DEFAULT NULL,
    resolved INTEGER DEFAULT 0,
    outcome_correct INTEGER DEFAULT NULL,
    settlement_usd REAL DEFAULT NULL,
    pnl_usd REAL DEFAULT NULL,
    closed_early INTEGER DEFAULT 0,
    exit_reason TEXT DEFAULT NULL,
    balance_before_usd REAL DEFAULT NULL,
    mark_price_cents REAL DEFAULT NULL,
    network TEXT DEFAULT 'mainnet',
    created_at TEXT DEFAULT (datetime('now')),
    last_updated TEXT DEFAULT (datetime('now')),
    resolved_at TEXT DEFAULT NULL,
    UNIQUE(signal_source, signal_id, network)
);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    order_status TEXT DEFAULT NULL,
    filled_contracts INTEGER DEFAULT NULL,
    fill_cost_cents INTEGER DEFAULT NULL,
    note TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT NOT NULL,
    network TEXT NOT NULL,
    opened INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    realized_pnl_usd REAL DEFAULT 0,
    ending_balance_usd REAL DEFAULT 0,
    last_updated TEXT DEFAULT (datetime('now')),
    PRIMARY KEY(day, network)
);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT DEFAULT (datetime('now')),
    network TEXT DEFAULT 'mainnet',
    cash_usd REAL NOT NULL,
    portfolio_usd REAL NOT NULL,
    total_usd REAL NOT NULL,
    realized_pnl_usd REAL DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    open_positions INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS crypto15m_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    series TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,                 -- 'up' | 'down' (display)
    direction TEXT NOT NULL,            -- 'yes' | 'no' (Polymarket side bought)
    target_contracts INTEGER NOT NULL,
    filled_contracts INTEGER DEFAULT 0,
    entry_limit_cents INTEGER NOT NULL,
    avg_entry_cents REAL DEFAULT NULL,
    cost_usd REAL DEFAULT 0,
    client_order_id TEXT UNIQUE NOT NULL,
    order_id TEXT DEFAULT NULL,
    -- stop-loss exit leg
    exit_client_order_id TEXT DEFAULT NULL,
    exit_order_id TEXT DEFAULT NULL,
    exit_limit_cents INTEGER DEFAULT NULL,
    exit_filled_contracts INTEGER DEFAULT 0,
    proceeds_usd REAL DEFAULT NULL,
    -- lifecycle
    status TEXT NOT NULL,               -- dry_run|submitted|filled|exiting|exited|settled|canceled|error
    exit_reason TEXT DEFAULT NULL,      -- 'stop_loss' | 'settlement' | 'unfilled_expired'
    close_time TEXT DEFAULT '',
    confidence REAL DEFAULT 0,          -- favorite prob at entry (×100)
    entry_delta_usd REAL DEFAULT NULL,
    outcome_correct INTEGER DEFAULT NULL,
    settlement_usd REAL DEFAULT NULL,
    pnl_usd REAL DEFAULT NULL,
    resolved INTEGER DEFAULT 0,
    network TEXT DEFAULT 'mainnet',
    dry_run INTEGER DEFAULT 0,
    error TEXT DEFAULT NULL,
    strategy TEXT DEFAULT NULL,         -- which strategy opened it (favorite|contrarian|rules)
    fees_usd REAL DEFAULT 0,            -- venue-reported entry fees (0 if none reported)
    exit_fees_usd REAL DEFAULT 0,       -- venue-reported exit fees
    created_at TEXT DEFAULT (datetime('now')),
    last_updated TEXT DEFAULT (datetime('now')),
    resolved_at TEXT DEFAULT NULL
);

-- crypto15m_signals: a passive research log for the 15-min crypto
-- strategy. One row per market (quarter window): a decision-point
-- snapshot captured live (favorite side + price + underlying delta a few
-- minutes before close), then the settled outcome filled in afterward.
-- This is what makes the 15m strategy backtestable — independent of
-- whether the user ever enables the executor. No orders, no money.
CREATE TABLE IF NOT EXISTS crypto15m_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    series TEXT DEFAULT '',
    close_time TEXT DEFAULT '',
    observed_at TEXT DEFAULT (datetime('now')),
    mins_left REAL,                    -- minutes to close at the observation
    favorite TEXT,                     -- 'up' | 'down' at the decision point
    favorite_price REAL,               -- favorite mid probability (fraction)
    entry_cost REAL,                   -- cost to BUY the favorite (fraction)
    up_prob REAL,                      -- yes/up mid probability (fraction)
    delta_pct REAL,                    -- abs(open-live)/open underlying move
    open_spot REAL,
    obs_spot REAL,
    book_imbalance REAL,               -- (bid-ask)/(bid+ask) vol on the up token, [-1,1]
    macd REAL,                         -- underlying MACD line (1-min closes)
    macd_signal REAL,                  -- MACD signal line
    macd_hist REAL,                    -- MACD histogram = macd - signal
    macd_cross INTEGER,                -- +1 bullish / -1 bearish / 0 no cross on this bar
    rsi REAL,                          -- Wilder RSI(14), 0..100
    resolved INTEGER DEFAULT 0,
    up_won INTEGER DEFAULT NULL,       -- 1 if the up/yes side settled true
    settled_at TEXT DEFAULT NULL,
    strike REAL,                       -- window-open reference at the decision point (USD)
    model_prob REAL,                   -- terminal-spot model P(up) at the decision point
    edge_net_cents REAL,               -- model's fee-net edge at the decision point (cents)
    network TEXT DEFAULT 'mainnet'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_c15sig_ticker ON crypto15m_signals(ticker);
CREATE INDEX IF NOT EXISTS idx_c15sig_resolved ON crypto15m_signals(resolved, close_time);

-- crypto15m_ticks: high-frequency companion to crypto15m_signals. One
-- row per active 15-min market every ~25s across the WHOLE window (not
-- just the decision point), so strategies can be tested for entry
-- timing, quote staleness vs spot, and maker-fill behaviour. Outcomes
-- come from joining crypto15m_signals on ticker. Moved to the research
-- archive by cleanup_old_data after `_C15_TICKS_KEEP_DAYS`.
CREATE TABLE IF NOT EXISTS crypto15m_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    observed_at TEXT DEFAULT (datetime('now')),
    mins_left REAL,
    yes_bid REAL,                      -- fraction 0..1
    yes_ask REAL,                      -- fraction 0..1
    up_prob REAL,                      -- mid, fraction 0..1
    spot REAL,                         -- live underlying USD
    open_spot REAL,                    -- quarter-open underlying USD
    delta_pct REAL,                    -- abs(open-live)/open
    book_imbalance REAL,               -- (bid-ask)/(bid+ask) vol on the up token, [-1,1]
    macd REAL,                         -- underlying MACD line (1-min closes)
    macd_signal REAL,                  -- MACD signal line
    macd_hist REAL,                    -- MACD histogram = macd - signal
    macd_cross INTEGER,                -- +1 bullish / -1 bearish / 0 no cross on this bar
    rsi REAL,                          -- Wilder RSI(14), 0..100
    ws_bid REAL,                       -- REAL CLOB best bid (favorite side), cents — vs the Gamma mid
    ws_ask REAL,                       -- REAL CLOB best ask (favorite side), cents
    strike REAL,                       -- window-open reference the model measures against (USD)
    delta_signed_pct REAL,             -- (spot-open)/open, SIGNED
    sigma1m REAL,                      -- realized 1-min vol (fraction/√min)
    model_prob REAL,                   -- terminal-spot model P(up), 0..1
    edge_net_cents REAL,               -- model's best fee-net edge (cents)
    no_ask REAL,                       -- down/no token best ask, fraction 0..1
    spot_source TEXT,                  -- which feed supplied `spot` (coinbase-ws/rest/…)
    up_ask REAL,                       -- up/yes token best ask, REAL book only (NULL when WS cold), fraction 0..1
    -- Trend / VWAP fields (Turbine strategy library). Backfillable from
    -- Hyperliquid 1-min candles for rows recorded before they existed.
    vwap1h REAL,                       -- 1-hour volume-weighted avg price (TWAP fallback), USD
    ema12 REAL,                        -- EMA(12) of 1-min closes, USD
    sma20 REAL,                        -- SMA(20) of 1-min closes, USD
    sma50 REAL,                        -- SMA(50) of 1-min closes, USD
    price_vs_vwap_pct REAL,            -- (spot - vwap1h)/vwap1h, percent (>0 = above VWAP)
    ema12_vs_sma20_pct REAL,           -- (ema12 - sma20)/sma20, percent
    ema1_vs_sma5_pct REAL,             -- (ema1 - sma5)/sma5, percent
    velocity1m_pct REAL,               -- 1-min percent change
    change5m_pct REAL,                 -- 5-min percent change
    change15m_pct REAL,                -- 15-min percent change
    network TEXT DEFAULT 'mainnet'
);
CREATE INDEX IF NOT EXISTS idx_c15tick_ticker ON crypto15m_ticks(ticker, observed_at);
CREATE INDEX IF NOT EXISTS idx_c15tick_time ON crypto15m_ticks(observed_at);

-- clob_trades: trade prints from the market WS (last_trade_price events the
-- feed used to DISCARD — 2026-07-16). The raw tape for maker/queue/adverse-
-- selection research: source_ts_ms is the exchange stamp, ingested_at ours.
-- Recorded by the same service loop as crypto15m_ticks; pruned with them.
CREATE TABLE IF NOT EXISTS clob_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL,
    market TEXT,                       -- condition id
    price REAL NOT NULL,               -- fraction 0..1
    size REAL,
    side TEXT,                         -- taker side: BUY / SELL
    source_ts_ms INTEGER,              -- exchange timestamp (ms epoch)
    ingested_at TEXT DEFAULT (datetime('now')),
    tx_hash TEXT,
    network TEXT DEFAULT 'mainnet'
);
CREATE INDEX IF NOT EXISTS idx_clobtr_asset ON clob_trades(asset_id, source_ts_ms);
CREATE INDEX IF NOT EXISTS idx_clobtr_time ON clob_trades(ingested_at);

-- bot_runs: each row is a single launch of the bot (start → stop).
-- This is what powers the user-facing "session P&L" model — every
-- restart starts a fresh run, and `start_balance` is what we benchmark
-- against. Per-run aggregates (P&L, trades opened/won/lost) get
-- updated periodically and finalised on shutdown.
CREATE TABLE IF NOT EXISTS bot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network TEXT NOT NULL DEFAULT 'mainnet',
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at TEXT,
    start_cash_usd REAL NOT NULL DEFAULT 0,
    start_portfolio_usd REAL NOT NULL DEFAULT 0,
    start_total_usd REAL NOT NULL DEFAULT 0,
    end_cash_usd REAL,
    end_portfolio_usd REAL,
    end_total_usd REAL,
    pnl_usd REAL DEFAULT 0,
    trades_opened INTEGER DEFAULT 0,
    trades_won INTEGER DEFAULT 0,
    trades_lost INTEGER DEFAULT 0,
    -- Lifetime counters captured at run start. The run's per-session
    -- trade/W/L counts are computed as `current_lifetime - start_lifetime`
    -- on every heartbeat. Without this baseline the heartbeat just
    -- stored lifetime totals into every run, which is why every row in
    -- the History → Run history table looked the same.
    start_trades_opened INTEGER DEFAULT 0,
    start_trades_won INTEGER DEFAULT 0,
    start_trades_lost INTEGER DEFAULT 0,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_env ON bot_runs(network, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_open ON bot_runs(ended_at, network);

-- Small durable key/value store for engine state that must survive a backend
-- restart but isn't user config (e.g. the crypto15m overall take-profit
-- baseline — the realized P&L snapshot taken when the target was enabled).
CREATE TABLE IF NOT EXISTS app_kv (
    k TEXT PRIMARY KEY,
    v TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- user_scripts: user-authored strategy scripts (the Scripts tab). Code is
-- stored verbatim and runs as full Python (see script_sandbox); `state_json`
-- is the script's persistent `state` dict snapshotted by the engine so
-- restarts don't fully reset cross-tick memory; `assets` is the script's own
-- coin scope (JSON list, NULL = all). A table (not app_kv/settings) so
-- per-script live P&L joins cleanly against crypto15m_positions.script_id.
-- `trusted` is VESTIGIAL: it used to gate the language sandbox, which no
-- longer exists. Kept so an older DB still opens; nothing reads it.
CREATE TABLE IF NOT EXISTS user_scripts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    code TEXT NOT NULL,
    enabled INTEGER DEFAULT 0,
    trusted INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    state_json TEXT DEFAULT '{}',
    last_error TEXT DEFAULT NULL,
    last_error_at TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- script_shadow_orders: what a script in SHADOW (dry-run) mode WOULD have
-- traded. Deliberately its OWN table rather than a dry_run flag on the live
-- position tables: fake rows there would be seen by reconcile, the position
-- caps and realized P&L, and crypto15m's manage pass
-- retires any dry_run row as a legacy "paper_removed" leftover. Settled
-- passively from the recorded window/signal outcome, so a shadow script
-- builds a real track record without ever touching the exchange.
CREATE TABLE IF NOT EXISTS script_shadow_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'crypto',   -- 'crypto' (window) | 'signal'
    signal_source TEXT DEFAULT '',           -- 'whale' | 'momentum' | 'market' when source='signal'
    signal_id INTEGER DEFAULT NULL,
    asset TEXT DEFAULT '',
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,                      -- up|down (crypto), yes|no (signal)
    contracts INTEGER NOT NULL,              -- 0 = refused by a rail
    entry_cents INTEGER NOT NULL,
    order_type TEXT DEFAULT 'FAK',
    reason TEXT DEFAULT '',
    refused INTEGER DEFAULT 0,
    note TEXT DEFAULT '',                    -- refusal reason / engine note
    tp_pct REAL DEFAULT NULL,
    sl_cents INTEGER DEFAULT NULL,
    close_time TEXT DEFAULT '',
    network TEXT DEFAULT 'mainnet',
    resolved INTEGER DEFAULT 0,
    outcome_correct INTEGER DEFAULT NULL,
    pnl_usd REAL DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_script ON script_shadow_orders(script_id, network, resolved);
CREATE INDEX IF NOT EXISTS idx_shadow_ticker ON script_shadow_orders(script_id, ticker);
CREATE INDEX IF NOT EXISTS idx_shadow_open ON script_shadow_orders(resolved, source);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(created_time DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker, direction, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_whale_trade_id ON whale_trades(trade_id);
-- The Signals page reads whale_trades newest-first with no category filter,
-- which had no index to walk: a full scan plus a temp B-tree sort on every
-- poll.
CREATE INDEX IF NOT EXISTS idx_whale_time ON whale_trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_whale_cat ON whale_trades(category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_whale_convergence ON whale_trades(ticker, taker_side, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots ON market_snapshots(ticker, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_markets_vol ON markets(volume DESC);
CREATE INDEX IF NOT EXISTS idx_bp_status ON bot_positions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bp_ticker ON bot_positions(ticker, direction, status);
CREATE INDEX IF NOT EXISTS idx_bp_resolved ON bot_positions(resolved, status);
CREATE INDEX IF NOT EXISTS idx_bp_created ON bot_positions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_oe_pos ON order_events(position_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pnl_at ON pnl_snapshots(at DESC);
CREATE INDEX IF NOT EXISTS idx_pnl_env_at ON pnl_snapshots(network, at);
CREATE INDEX IF NOT EXISTS idx_c15_open ON crypto15m_positions(resolved, status);
CREATE INDEX IF NOT EXISTS idx_c15_asset ON crypto15m_positions(asset, network, resolved);
CREATE INDEX IF NOT EXISTS idx_c15_env_status ON crypto15m_positions(network, status);
CREATE INDEX IF NOT EXISTS idx_c15_env_exit ON crypto15m_positions(network, exit_reason);

-- ── terminal (manual trading) ─────────────────────────────────────────
-- Deliberately its OWN tables, not bot_positions. bot_positions is the
-- ENGINE's ledger: every row is keyed to a signal (UNIQUE(signal_source,
-- signal_id, network)) and is swept by reconcile, the position caps and the
-- engine P&L roll-ups. A hand-placed order has no signal and must never be
-- counted as an engine trade, or the bot's own win-rate becomes a mix of
-- what it decided and what you decided.

-- terminal_orders: an order YOU placed from the terminal, or a standing
-- instruction of yours that fired. `origin` is always 'manual' today; the
-- column exists so a future automated writer can never be mistaken for you.
CREATE TABLE IF NOT EXISTS terminal_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    side TEXT NOT NULL,                      -- yes|no
    action TEXT NOT NULL,                    -- buy|sell
    order_type TEXT NOT NULL DEFAULT 'GTC',  -- GTC|FAK|FOK
    requested_size REAL NOT NULL,
    requested_price_cents REAL NOT NULL,
    -- what the venue actually accepted, after tick-snap / min-size
    sent_size REAL DEFAULT NULL,
    sent_price_cents REAL DEFAULT NULL,
    order_id TEXT DEFAULT NULL,
    status TEXT NOT NULL,                    -- placed|unmatched|error|canceled
    error TEXT DEFAULT NULL,
    -- Filled from the venue's OWN ledger by reconcile_fills(), never from
    -- what we asked for. NULL until that reconcile lands.
    filled_size REAL DEFAULT NULL,
    avg_fill_price_cents REAL DEFAULT NULL,
    fees_usd REAL DEFAULT NULL,
    cost_usd REAL DEFAULT NULL,
    reconciled INTEGER DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'manual',
    standing_id TEXT DEFAULT NULL,           -- set when a stop/TP fired it
    network TEXT DEFAULT 'mainnet',
    created_at TEXT DEFAULT (datetime('now')),
    reconciled_at TEXT DEFAULT NULL
);

-- terminal_fills: the venue's own record of what actually happened, keyed by
-- its trade id so re-running reconcile is idempotent. This — not the order
-- row — is the source of truth for cost basis. The naive number (what we
-- ASKED for) is wrong in the user's favour on every single row.
CREATE TABLE IF NOT EXISTS terminal_fills (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT DEFAULT NULL,
    condition_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    side TEXT NOT NULL,                      -- yes|no
    action TEXT NOT NULL,                    -- buy|sell
    size REAL NOT NULL,
    price_cents REAL NOT NULL,
    fees_usd REAL DEFAULT NULL,
    cost_usd REAL DEFAULT NULL,
    origin TEXT NOT NULL DEFAULT 'unknown',
    network TEXT DEFAULT 'mainnet',
    filled_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_term_ord_cid ON terminal_orders(condition_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_term_ord_open ON terminal_orders(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_term_fill_cid ON terminal_fills(condition_id, filled_at DESC);
CREATE INDEX IF NOT EXISTS idx_term_fill_at ON terminal_fills(filled_at DESC);

-- terminal_paper_orders: SIMULATED manual orders (paper trading).
--
-- Its own table, for the same reason script_shadow_orders has one: a fake row
-- in terminal_orders/terminal_fills would be picked up by reconcile, by the
-- cost-basis walk, and by every P&L figure on the Track screen — silently
-- mixing money you did not spend into money you did. Nothing outside
-- terminal_paper.py reads this table.
--
-- One row per order. A SETTLEMENT is written as a synthetic sell at 100c or
-- 0c with order_type='SETTLE', so the average-cost and cash maths have a
-- single code path instead of two that can disagree.
CREATE TABLE IF NOT EXISTS terminal_paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    side TEXT NOT NULL,                      -- yes|no
    action TEXT NOT NULL,                    -- buy|sell
    order_type TEXT NOT NULL DEFAULT 'GTC',  -- GTC|FAK|FOK|SETTLE
    requested_size REAL NOT NULL,
    requested_price_cents REAL NOT NULL,
    -- What the REAL book could actually have given us. Never the requested
    -- size at the requested price just because we asked: that is the classic
    -- paper-trading lie and it is the whole reason this is worth building.
    filled_size REAL DEFAULT 0,
    avg_fill_price_cents REAL DEFAULT NULL,
    -- Size still working on the book (GTC that did not fully cross).
    resting_size REAL DEFAULT 0,
    fees_usd REAL DEFAULT NULL,              -- NULL = fee rate was unknown
    cost_usd REAL DEFAULT NULL,              -- signed: buys negative cash
    status TEXT NOT NULL,                    -- filled|partial|resting|unfilled|canceled|settled
    note TEXT DEFAULT '',
    -- Which paper account this row belongs to: 'manual' (you, from the
    -- ticket) or 'ai' (an AI agent over MCP). Two bankrolls, two totals —
    -- an agent's paper P&L blended into yours would answer neither "am I
    -- any good?" nor "is the agent?".
    book TEXT DEFAULT 'manual',
    network TEXT DEFAULT 'mainnet',
    created_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_cid ON terminal_paper_orders(condition_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_resting ON terminal_paper_orders(status, created_at DESC);

-- ai_forecasts: every fair value an AI AGENT committed to (record_forecast),
-- with the market's own quote at that moment, scored once the market
-- resolves (forecast_ledger.py). These are the agents' numbers, never the
-- app's — the analysis panel produces none. A buy by an agent must cite one.
CREATE TABLE IF NOT EXISTS ai_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now')),
    network TEXT DEFAULT 'mainnet',
    condition_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'mcp',
    model TEXT DEFAULT '',
    client TEXT DEFAULT '',
    prob_yes REAL NOT NULL,                  -- 0.01..0.99
    market_mid_cents REAL DEFAULT NULL,      -- YES mid at that moment; NULL = no two-sided quote
    yes_bid_cents REAL DEFAULT NULL,
    yes_ask_cents REAL DEFAULT NULL,
    close_time TEXT DEFAULT NULL,
    rationale TEXT DEFAULT '',
    outcome REAL DEFAULT NULL,               -- YES payout 0..1; NULL = unresolved
    resolved_at TEXT DEFAULT NULL,
    last_checked_at TEXT DEFAULT NULL,
    agent_id TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_aif_pending ON ai_forecasts(outcome, condition_id);
CREATE INDEX IF NOT EXISTS idx_aif_agent ON ai_forecasts(agent_id, id);

-- mcp_orders: every order an AI agent asked for — paper or live, sent,
-- refused, queued for the user's approval. Every per-agent rail is computed
-- from here: today's spend (pending approvals included), its positions, and
-- "an agent sells only what IT opened" (by the fills of its own order ids in
-- terminal_fills, never by what it asked for). The live order itself also
-- lands in terminal_orders with origin='ai', like any other ticket order.
CREATE TABLE IF NOT EXISTS mcp_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now')),
    network TEXT DEFAULT 'mainnet',
    mode TEXT NOT NULL,                      -- paper | live
    client TEXT DEFAULT '',
    agent_id TEXT NOT NULL DEFAULT 'default',
    condition_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    side TEXT NOT NULL,                      -- yes|no
    action TEXT NOT NULL,                    -- buy|sell
    order_type TEXT NOT NULL DEFAULT 'GTC',
    size REAL NOT NULL,
    price_cents REAL NOT NULL,
    forecast_id INTEGER DEFAULT NULL,
    committed_usd REAL DEFAULT NULL,         -- worst-case outlay of a buy, fees in
    ok INTEGER NOT NULL DEFAULT 0,
    order_id TEXT DEFAULT NULL,              -- venue order id (live)
    paper_local_id INTEGER DEFAULT NULL,     -- terminal_paper_orders.id (paper)
    filled REAL DEFAULT NULL,
    avg_fill_cents REAL DEFAULT NULL,
    message TEXT DEFAULT '',
    -- NULL for orders that went straight through; pending | deciding |
    -- approved | rejected (by the user) | expired | failed | unknown (truly
    -- ambiguous) | cancelled | refused (the venue placed nothing) | filled |
    -- partial | unmatched (FAK/FOK with a known fill) | void (an unknown the
    -- user settled: the venue never had it). See mcp_rails.py's docstring.
    status TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_orders_day ON mcp_orders(mode, created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_orders_cid ON mcp_orders(condition_id, side);

-- autopilot_runs: every Autopilot run (autopilot.py) — the app running one
-- of the user's named agents on a timer with the user's own AI key. Today's
-- rows ARE the budgets: runs per UTC day and tokens per UTC day are summed
-- from here before every run and every model call. Tokens a provider did not
-- report are counted as an estimate (tokens_estimated=1), never as zero.
CREATE TABLE IF NOT EXISTS autopilot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,                -- UTC, YYYY-MM-DDTHH:MM:SS
    finished_at TEXT DEFAULT NULL,
    trigger TEXT DEFAULT 'schedule',         -- schedule | manual
    agent_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    -- running | ok | steps | budget | context | stopped | error
    status TEXT DEFAULT 'running',
    steps INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    tokens_estimated INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT NULL,              -- NULL where no price is published
    summary TEXT DEFAULT '',
    tool_log TEXT DEFAULT '[]',
    error TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_autopilot_started ON autopilot_runs(started_at);

-- perps_fills: Polymarket Perps executions, imported from the exchange's own
-- fill feed (it is the authority — this is a local copy for the Perps tab's
-- P&L, the loss breakers and the per-market markout kill switch). `pnl` is the
-- realized P&L the exchange booked on the fill, before `fee`. `mid_at_fill`
-- and `markout_bps` are ours: the book mid when the fill was seen and the
-- signed move one minute later, from the maker's side (negative = we were
-- picked off). Not wiped with trade history: it re-imports from the exchange.
CREATE TABLE IF NOT EXISTS perps_fills (
    trade_id INTEGER PRIMARY KEY,
    order_id INTEGER DEFAULT NULL,
    instrument_id INTEGER NOT NULL,
    symbol TEXT DEFAULT '',
    side TEXT NOT NULL,                      -- long|short (our side of the fill)
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    notional REAL NOT NULL,
    taker INTEGER NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    pnl REAL NOT NULL DEFAULT 0,
    liquidation INTEGER NOT NULL DEFAULT 0,
    mid_at_fill REAL DEFAULT NULL,
    markout_bps REAL DEFAULT NULL,
    ts INTEGER NOT NULL,                     -- exchange timestamp, ms
    network TEXT DEFAULT 'mainnet'
);

CREATE INDEX IF NOT EXISTS idx_perps_fills_ts ON perps_fills(ts DESC);
CREATE INDEX IF NOT EXISTS idx_perps_fills_iid ON perps_fills(instrument_id, ts DESC);

-- perps_trades: the PUBLIC Perps trade tape (every market participant), the
-- raw input of the Perps backtester. Filled two ways: the live WebSocket feed
-- records markets the engine watches, and the backtester backfills any window
-- it needs from the exchange's REST history (≈30 days deep). `side` is the
-- TAKER's side (verified: longs print at the ask, shorts at the bid).
CREATE TABLE IF NOT EXISTS perps_trades (
    trade_id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    qty REAL NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perps_trades_iid_ts ON perps_trades(instrument_id, ts);

-- perps_ticks: best bid/ask samples (~5s per market) from the live feed.
-- Gives the backtester a real quoted spread where it has them; elsewhere it
-- infers the touch from the trade tape.
-- perps_tape_coverage: windows of perps_trades known to be COMPLETE because
-- the backtester backfilled them from REST. Live-recorded trades don't add
-- coverage (the app may have been closed for part of the time).
CREATE TABLE IF NOT EXISTS perps_tape_coverage (
    instrument_id INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perps_cov ON perps_tape_coverage(instrument_id);

CREATE TABLE IF NOT EXISTS perps_ticks (
    instrument_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    bid REAL NOT NULL,
    ask REAL NOT NULL,
    PRIMARY KEY (instrument_id, ts)
);
"""


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _quarantine_db() -> None:
    """Move a corrupt DB (and its -wal/-shm sidecars) aside so a fresh one can be
    created. Best-effort: on rename failure, delete, so init can still proceed."""
    import time as _time
    close_thread_connection()
    p = db_path()
    suffix = _time.strftime("%Y%m%d-%H%M%S")
    for ext in ("", "-wal", "-shm"):
        src = Path(str(p) + ext)
        if not src.exists():
            continue
        try:
            src.replace(Path(str(p) + f".corrupt-{suffix}" + ext))
            logger.warning(f"quarantined corrupt DB file: {src.name}")
        except Exception:
            try:
                src.unlink()
                logger.warning(f"deleted corrupt DB file (rename failed): {src.name}")
            except Exception as e2:
                logger.error(f"could not quarantine/delete {src.name}: {e2}")


def init_db() -> None:
    """Create/upgrade the schema. Resilient to a corrupt/unreadable DB file: a
    power-loss / AV-lock / disk error can leave the sqlite file malformed, and an
    UNGUARDED failure here crash-loops the frozen backend into the packaged app's
    5-strike give-up — bricking the app on EVERY launch with no developer present.
    The on-chain wallet is the source of truth (reconcile re-imports open
    positions), so on unrecoverable corruption we quarantine the bad file and
    start fresh rather than leave the user with a permanently dead app."""
    try:
        _init_db_schema()
        return
    except sqlite3.DatabaseError as e:
        msg = str(e).lower()
        corrupt = (
            "malformed" in msg
            or "not a database" in msg
            or "file is encrypted" in msg
            or "disk image" in msg
        )
        if not corrupt:
            raise
        logger.error(f"database appears corrupt ({e}) — quarantining and recreating")
    _quarantine_db()
    global _restore_checked
    _restore_checked = False
    _init_db_schema()


def _init_db_schema() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)
        for migration in [
            "ALTER TABLE bot_positions ADD COLUMN closed_early INTEGER DEFAULT 0",
            "ALTER TABLE terminal_paper_orders ADD COLUMN book TEXT DEFAULT 'manual'",
            "ALTER TABLE alerts ADD COLUMN yes_sub_title TEXT DEFAULT ''",
            "ALTER TABLE bot_runs ADD COLUMN start_trades_opened INTEGER DEFAULT 0",
            "ALTER TABLE bot_runs ADD COLUMN start_trades_won INTEGER DEFAULT 0",
            "ALTER TABLE bot_runs ADD COLUMN start_trades_lost INTEGER DEFAULT 0",
            "ALTER TABLE markets ADD COLUMN slug TEXT DEFAULT ''",
            "ALTER TABLE crypto15m_signals ADD COLUMN book_imbalance REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN book_imbalance REAL",
            "ALTER TABLE bot_positions ADD COLUMN mark_price_cents REAL DEFAULT NULL",
            "ALTER TABLE bot_positions ADD COLUMN exit_reason TEXT DEFAULT NULL",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd_signal REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd_hist REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN macd_cross INTEGER",
            "ALTER TABLE crypto15m_signals ADD COLUMN rsi REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd_signal REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd_hist REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN macd_cross INTEGER",
            "ALTER TABLE crypto15m_ticks ADD COLUMN rsi REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN ws_bid REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN ws_ask REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN strike_source TEXT",
            "ALTER TABLE crypto15m_ticks ADD COLUMN strike_source TEXT",
            "ALTER TABLE crypto15m_ticks ADD COLUMN settle_twap REAL",
            "ALTER TABLE crypto15m_positions ADD COLUMN avg_entry_cents REAL DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN exit_client_order_id TEXT DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN exit_order_id TEXT DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN exit_limit_cents INTEGER DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN exit_filled_contracts INTEGER DEFAULT 0",
            "ALTER TABLE crypto15m_positions ADD COLUMN proceeds_usd REAL DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN exit_reason TEXT DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN entry_delta_usd REAL DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN outcome_correct INTEGER DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN settlement_usd REAL DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN pnl_usd REAL DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN dry_run INTEGER DEFAULT 0",
            "ALTER TABLE crypto15m_positions ADD COLUMN error TEXT DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN strategy TEXT DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN fees_usd REAL DEFAULT 0",
            "ALTER TABLE crypto15m_positions ADD COLUMN exit_fees_usd REAL DEFAULT 0",
            "ALTER TABLE crypto15m_positions ADD COLUMN entry_order_type TEXT DEFAULT NULL",
            "ALTER TABLE bot_positions ADD COLUMN entry_maker INTEGER DEFAULT NULL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN strike REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN delta_signed_pct REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN sigma1m REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN model_prob REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN edge_net_cents REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN no_ask REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN spot_source TEXT",
            "ALTER TABLE crypto15m_signals ADD COLUMN strike REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN model_prob REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN edge_net_cents REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN up_ask REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN vwap1h REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN ema12 REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN sma20 REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN sma50 REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN price_vs_vwap_pct REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN ema12_vs_sma20_pct REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN ema1_vs_sma5_pct REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN velocity1m_pct REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN change5m_pct REAL",
            "ALTER TABLE crypto15m_ticks ADD COLUMN change15m_pct REAL",
            "ALTER TABLE crypto15m_signals ADD COLUMN interval TEXT DEFAULT '15m'",
            "ALTER TABLE crypto15m_ticks ADD COLUMN interval TEXT DEFAULT '15m'",
            "ALTER TABLE crypto15m_positions ADD COLUMN script_id TEXT DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN tp_pct REAL DEFAULT NULL",
            "ALTER TABLE crypto15m_positions ADD COLUMN sl_cents INTEGER DEFAULT NULL",
            "ALTER TABLE bot_positions ADD COLUMN script_id TEXT DEFAULT NULL",
            "ALTER TABLE user_scripts ADD COLUMN dry_run INTEGER DEFAULT 1",
            "ALTER TABLE user_scripts ADD COLUMN assets TEXT DEFAULT NULL",
            "ALTER TABLE user_scripts ADD COLUMN author TEXT DEFAULT NULL",
            "ALTER TABLE user_scripts ADD COLUMN needs_review INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute(
                "UPDATE user_scripts SET author='agent:earlier', needs_review=1, enabled=0 "
                "WHERE author IS NULL AND COALESCE(dry_run, 1)=1 "
                "AND notes='Written by an AI agent over MCP.'")
            conn.execute(
                "UPDATE user_scripts SET author='agent:earlier' "
                "WHERE author IS NULL AND notes='Written by an AI agent over MCP.'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("UPDATE crypto15m_signals SET interval='15m' WHERE interval IS NULL")
            conn.execute("UPDATE crypto15m_ticks SET interval='15m' WHERE interval IS NULL")
        except sqlite3.OperationalError:
            pass


def factory_reset(*, wipe_markets: bool = False) -> dict:
    """Wipe ALL trading-related tables. Returns row counts deleted.

    Tables ALWAYS wiped (PII / per-user state):
        bot_positions, bot_runs, pnl_snapshots, daily_stats,
        order_events, alerts, whale_trades

    Tables wiped only if `wipe_markets=True` (public Polymarket cache —
    re-syncing all 1000+ markets takes a minute, so usually keep):
        markets, events, trades, market_snapshots

    The research archive (research-archive.db, the rows retention has aged
    out) is a separate file this never touches.

    Each DELETE is run in its OWN short transaction so a single
    locked-table error can't roll the whole thing back. If the user's
    main loop happens to hold a write lock on one table, only that
    one fails (caller can retry). VACUUM is run on a fresh,
    autocommit connection because it cannot be inside a transaction.
    A WAL checkpoint runs first so future readers see the deletion.
    """
    backup_research()
    try:
        with get_db() as conn:
            bank_realized_pnl_before_wipe(conn)
    except sqlite3.OperationalError:
        pass
    targets = [
        "bot_positions",
        "crypto15m_positions",
        "bot_runs",
        "pnl_snapshots",
        "daily_stats",
        "order_events",
        "alerts",
        "whale_trades",
        "terminal_orders",
        "terminal_fills",
        "terminal_paper_orders",
        "script_shadow_orders",
        "mcp_orders",
        "ai_forecasts",
        "autopilot_runs",
        "app_kv",
    ]
    if wipe_markets:
        targets.extend(["markets", "events", "trades", "market_snapshots"])

    summary: dict[str, int] = {}
    errors: dict[str, str] = {}

    for t in targets:
        try:
            with get_db() as conn:
                cur = conn.execute(f"SELECT COUNT(*) FROM {t}")
                count = int(cur.fetchone()[0])
                if t == "app_kv":
                    conn.execute(
                        "DELETE FROM app_kv WHERE k NOT LIKE ? AND k NOT LIKE ?",
                        (f"{_LIFETIME_PNL_PREFIX}%", f"{_DAILY_PNL_PREFIX}%"),
                    )
                else:
                    conn.execute(f"DELETE FROM {t}")
                summary[t] = count
        except sqlite3.OperationalError as e:
            errors[t] = str(e)
            summary[t] = -1

    try:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('bot_positions','bot_runs','pnl_snapshots',"
                "'daily_stats','order_events','alerts','whale_trades',"
                "'markets','events','trades','market_snapshots',"
                "'terminal_orders','terminal_paper_orders',"
                "'script_shadow_orders','mcp_orders','ai_forecasts',"
                "'autopilot_runs')",
            )
    except sqlite3.OperationalError:
        pass

    try:
        with get_db() as conn:
            set_terminal_reconcile_floor(conn)
    except sqlite3.OperationalError:
        pass

    try:
        close_thread_connection()
        conn = sqlite3.connect(str(db_path()), timeout=30)
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        conn.close()
    except sqlite3.OperationalError:
        pass

    if errors:
        summary["_errors"] = errors  # type: ignore[assignment]
    return summary


def clear_trade_history() -> dict:
    """Wipe TRADE HISTORY / P&L / sessions only — keep settings, wallet
    credentials, and the live signal feed (markets/events/trades cache,
    whale_trades, alerts). This is the targeted "start my stats fresh"
    button; `factory_reset` is the nuke-everything one.

    Tables wiped:
        bot_positions, crypto15m_positions, bot_runs, pnl_snapshots,
        daily_stats, order_events

    Note `factory_reset` misses `crypto15m_positions`; this includes it so
    crypto trade history is actually cleared. Same one-transaction-per-table
    approach so a single locked table can't roll back the rest.
    """
    try:
        with get_db() as conn:
            bank_realized_pnl_before_wipe(conn)
    except sqlite3.OperationalError:
        pass
    targets = [
        "bot_positions",
        "crypto15m_positions",
        "bot_runs",
        "pnl_snapshots",
        "daily_stats",
        "order_events",
        "terminal_orders",
        "terminal_fills",
        "terminal_paper_orders",
        "script_shadow_orders",
    ]

    summary: dict[str, int] = {}
    errors: dict[str, str] = {}

    for t in targets:
        try:
            with get_db() as conn:
                cur = conn.execute(f"SELECT COUNT(*) FROM {t}")
                count = int(cur.fetchone()[0])
                conn.execute(f"DELETE FROM {t}")
                summary[t] = count
        except sqlite3.OperationalError as e:
            errors[t] = str(e)
            summary[t] = -1

    try:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('bot_positions','crypto15m_positions','bot_runs',"
                "'pnl_snapshots','daily_stats','order_events',"
                "'terminal_orders','terminal_paper_orders',"
                "'script_shadow_orders')",
            )
    except sqlite3.OperationalError:
        pass

    try:
        with get_db() as conn:
            set_terminal_reconcile_floor(conn)
    except sqlite3.OperationalError:
        pass

    try:
        close_thread_connection()
        conn = sqlite3.connect(str(db_path()), timeout=30)
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        conn.close()
    except sqlite3.OperationalError:
        pass

    if errors:
        summary["_errors"] = errors  # type: ignore[assignment]
    return summary



def upsert_market(conn, market: dict) -> None:
    conn.execute(
        """
        INSERT INTO markets (ticker, event_ticker, series_ticker, slug, title, yes_sub_title,
            category, status, close_time, volume, volume_24h, open_interest,
            yes_bid, yes_ask, last_price, prev_yes_bid, prev_price,
            result, settlement_value, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            event_ticker=excluded.event_ticker,
            series_ticker=excluded.series_ticker,
            slug=COALESCE(NULLIF(excluded.slug,''), slug),
            title=COALESCE(excluded.title, title),
            yes_sub_title=COALESCE(excluded.yes_sub_title, yes_sub_title),
            category=COALESCE(NULLIF(excluded.category,''), category),
            status=excluded.status,
            close_time=excluded.close_time,
            prev_yes_bid=markets.yes_bid,
            prev_price=markets.last_price,
            volume=excluded.volume,
            volume_24h=excluded.volume_24h,
            open_interest=excluded.open_interest,
            yes_bid=excluded.yes_bid,
            yes_ask=excluded.yes_ask,
            last_price=excluded.last_price,
            result=excluded.result,
            settlement_value=excluded.settlement_value,
            last_updated=excluded.last_updated
        """,
        (
            market.get("ticker", ""),
            market.get("event_ticker", ""),
            market.get("series_ticker", ""),
            market.get("slug", ""),
            market.get("title", ""),
            market.get("yes_sub_title", ""),
            market.get("category", ""),
            market.get("status", "open"),
            market.get("close_time", ""),
            _to_float(market.get("volume", 0)),
            _to_float(market.get("volume_24h", 0)),
            _to_float(market.get("open_interest", 0)),
            _to_float(market.get("yes_bid", 0)),
            _to_float(market.get("yes_ask", 0)),
            _to_float(market.get("last_price", 0)),
            0,
            0,
            market.get("result", ""),
            _to_float(market.get("settlement_value")) if market.get("settlement_value") is not None else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def get_market(conn, ticker: str) -> dict | None:
    row = conn.execute("SELECT * FROM markets WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row) if row else None


def get_active_markets(conn, min_volume: float = 0, limit: int = 500) -> list:
    rows = conn.execute(
        "SELECT * FROM markets WHERE status IN ('active','open') AND volume >= ? "
        "ORDER BY volume DESC LIMIT ?",
        (min_volume, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_event(conn, event: dict) -> None:
    conn.execute(
        """
        INSERT INTO events (event_ticker, series_ticker, title, sub_title, category, status, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_ticker) DO UPDATE SET
            series_ticker=excluded.series_ticker,
            title=COALESCE(excluded.title, title),
            sub_title=COALESCE(excluded.sub_title, sub_title),
            category=COALESCE(NULLIF(excluded.category,''), category),
            status=excluded.status,
            last_updated=excluded.last_updated
        """,
        (
            event.get("event_ticker", ""),
            event.get("series_ticker", ""),
            event.get("title", ""),
            event.get("sub_title", ""),
            event.get("category", ""),
            event.get("status", "open"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def trade_exists(conn, trade_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
        is not None
    )


def insert_trade(conn, trade: dict) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO trades (trade_id, ticker, event_ticker, count_fp, yes_price,
                no_price, taker_side, dollar_value, category, created_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.get("trade_id", ""),
                trade.get("ticker", ""),
                trade.get("event_ticker", ""),
                _to_float(trade.get("count_fp", 0)),
                _to_float(trade.get("yes_price", 0)),
                _to_float(trade.get("no_price", 0)),
                trade.get("taker_side", ""),
                _to_float(trade.get("dollar_value", 0)),
                trade.get("category", ""),
                trade.get("created_time", ""),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False



def insert_alert(conn, alert: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO alerts (ticker, event_ticker, title, yes_sub_title, category, signal_type,
            direction, volume_24h, price, price_change, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
        """,
        (
            alert.get("ticker", ""),
            alert.get("event_ticker", ""),
            alert.get("title", ""),
            alert.get("yes_sub_title", ""),
            alert.get("category", ""),
            alert.get("signal_type", ""),
            alert.get("direction", ""),
            alert.get("volume_24h", 0),
            alert.get("price", 0),
            alert.get("price_change", 0),
            alert.get("confidence", 0),
            alert.get("created_at"),
        ),
    )
    return cur.lastrowid


def mark_alert_discord_sent(conn, alert_id: int) -> None:
    conn.execute("UPDATE alerts SET discord_sent = 1 WHERE id = ?", (alert_id,))


def recent_alert_exists(
    conn, ticker: str, signal_type: str, direction: str, cooldown_minutes: int = 30
) -> bool:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    ).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        """
        SELECT 1 FROM alerts
        WHERE ticker = ? AND signal_type = ? AND direction = ? AND created_at >= ?
        """,
        (ticker, signal_type, direction, cutoff),
    ).fetchone()
    return row is not None


def fetch_tradeable_momentum_signals(
    conn,
    *,
    min_confidence: float,
    max_age_sec: int,
    allowed_types: list[str],
    seen_ids: set[int],
    limit: int = 50,
) -> list[dict]:
    if not allowed_types:
        return []
    placeholders = ",".join("?" for _ in allowed_types)
    rows = conn.execute(
        f"""SELECT a.* FROM alerts a
            WHERE a.confidence >= ?
              AND a.resolved = 0
              AND (julianday('now') - julianday(a.created_at)) * 86400 <= ?
              AND a.signal_type IN ({placeholders})
            ORDER BY a.created_at DESC
            LIMIT ?""",
        [min_confidence, max_age_sec, *allowed_types, limit * 2],
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if int(d["id"]) in seen_ids:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out



def whale_trade_exists(conn, trade_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM whale_trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        is not None
    )


def insert_whale_trade(conn, trade: dict) -> int:
    tid = str(trade.get("trade_id") or "")
    if tid and conn.execute(
        "SELECT 1 FROM whale_trades WHERE trade_id=? LIMIT 1", (tid,)
    ).fetchone():
        return 0
    try:
        cur = conn.execute(
            """
            INSERT INTO whale_trades
                (trade_id, ticker, event_ticker, title, yes_sub_title, category, taker_side,
                 count_fp, price, dollar_value, market_volume, open_interest, confidence,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            """,
            (
                trade.get("trade_id", ""),
                trade.get("ticker", ""),
                trade.get("event_ticker", ""),
                trade.get("title", ""),
                trade.get("yes_sub_title", ""),
                trade.get("category", ""),
                trade.get("taker_side", ""),
                _to_float(trade.get("count_fp", 0)),
                _to_float(trade.get("price", 0)),
                _to_float(trade.get("dollar_value", 0)),
                _to_float(trade.get("market_volume", 0)),
                _to_float(trade.get("open_interest", 0)),
                trade.get("confidence", 0),
                trade.get("created_at"),
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return 0


def mark_whale_discord_sent(conn, whale_id: int) -> None:
    conn.execute("UPDATE whale_trades SET discord_sent = 1 WHERE id = ?", (whale_id,))


def fetch_tradeable_whale_signals(
    conn,
    *,
    min_confidence: float,
    max_age_sec: int,
    seen_ids: set[int],
    limit: int = 50,
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM whale_trades
           WHERE confidence >= ?
             AND resolved = 0
             AND (julianday('now') - julianday(created_at)) * 86400 <= ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (min_confidence, max_age_sec, limit * 2),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if int(d["id"]) in seen_ids:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def get_recent_whales_for_convergence(conn, hours: int = 2) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """
        SELECT ticker, taker_side, dollar_value, confidence, price,
               count_fp, title, category, created_at
        FROM whale_trades WHERE created_at >= ?
        ORDER BY created_at DESC
        """,
        (cutoff,),
    ).fetchall()
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r[0], r[1])
        groups.setdefault(key, []).append(
            {
                "dollar_value": r[2],
                "confidence": r[3],
                "price": r[4],
                "count_fp": r[5],
                "title": r[6],
                "category": r[7],
                "created_at": r[8],
            }
        )
    return groups



def save_snapshot(conn, ticker: str, market: dict) -> None:
    conn.execute(
        """INSERT INTO market_snapshots (ticker, volume, volume_24h, open_interest, yes_bid, last_price)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            ticker,
            _to_float(market.get("volume", 0)),
            _to_float(market.get("volume_24h", 0)),
            _to_float(market.get("open_interest", 0)),
            _to_float(market.get("yes_bid", 0)),
            _to_float(market.get("last_price", 0)),
        ),
    )


def get_previous_snapshot(conn, ticker: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM market_snapshots WHERE ticker = ?
           ORDER BY snapshot_at DESC LIMIT 1 OFFSET 1""",
        (ticker,),
    ).fetchone()
    return dict(row) if row else None


def get_previous_snapshots_bulk(conn, tickers) -> dict:
    """Second-most-recent snapshot per ticker (same as get_previous_snapshot's
    `LIMIT 1 OFFSET 1`, i.e. rn=2) for many tickers in ONE query — replaces a
    ~500× N+1 in the momentum scan. Returns {ticker: {volume_24h, yes_bid,
    last_price}}. Chunked under SQLite's 999-variable limit."""
    out: dict[str, dict] = {}
    uniq = [t for t in dict.fromkeys(tickers) if t]
    CHUNK = 400
    for i in range(0, len(uniq), CHUNK):
        chunk = uniq[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT ticker, volume_24h, yes_bid, last_price FROM (
                   SELECT ticker, volume_24h, yes_bid, last_price,
                          ROW_NUMBER() OVER (
                              PARTITION BY ticker ORDER BY snapshot_at DESC, id DESC
                          ) AS rn
                   FROM market_snapshots
                   WHERE ticker IN ({placeholders})
               ) WHERE rn = 2""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["ticker"]] = dict(r)
    return out



def get_unresolved_alerts(conn, days: int = 30) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """SELECT id, ticker, direction, price, created_at
           FROM alerts WHERE resolved = 0 AND created_at >= ?""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_unresolved_whale_trades(conn, days: int = 30) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """SELECT id, ticker, taker_side, price, dollar_value, created_at
           FROM whale_trades WHERE resolved = 0 AND created_at >= ?""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_alert_resolved(
    conn, alert_id: int, correct: bool, resolved_price: float, pnl_est: float
) -> None:
    conn.execute(
        """UPDATE alerts SET resolved = 1, outcome_correct = ?, resolved_price = ?,
              pnl_estimate = ?, resolved_at = ? WHERE id = ?""",
        (
            1 if correct else 0,
            resolved_price,
            pnl_est,
            datetime.now(timezone.utc).isoformat(),
            alert_id,
        ),
    )


def mark_whale_resolved(
    conn, trade_id: int, correct: bool, resolved_price: float, pnl_est: float
) -> None:
    conn.execute(
        """UPDATE whale_trades SET resolved = 1, outcome_correct = ?, resolved_price = ?,
              pnl_estimate = ?, resolved_at = ? WHERE id = ?""",
        (
            1 if correct else 0,
            resolved_price,
            pnl_est,
            datetime.now(timezone.utc).isoformat(),
            trade_id,
        ),
    )



def insert_bot_position(conn, row: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO bot_positions (
            signal_source, signal_id, ticker, event_ticker, title, category,
            direction, action, target_contracts, limit_price_cents,
            filled_contracts, avg_fill_price_cents, cost_usd,
            client_order_id, order_id, status,
            confidence, edge_pts, signal_price, error,
            balance_before_usd, network, script_id, entry_maker
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["signal_source"],
            row["signal_id"],
            row["ticker"],
            row.get("event_ticker", ""),
            row.get("title", ""),
            row.get("category", ""),
            row["direction"],
            row.get("action", "buy"),
            int(row["target_contracts"]),
            int(row["limit_price_cents"]),
            int(row.get("filled_contracts", 0)),
            row.get("avg_fill_price_cents"),
            float(row.get("cost_usd", 0.0)),
            row["client_order_id"],
            row.get("order_id"),
            row["status"],
            float(row.get("confidence", 0.0)),
            float(row.get("edge_pts", 0.0)),
            float(row.get("signal_price", 0.0)),
            row.get("error"),
            row.get("balance_before_usd"),
            row.get("network", "mainnet"),
            row.get("script_id"),
            row.get("entry_maker"),
        ),
    )
    return cur.lastrowid


def update_bot_position(conn, bot_id: int, **fields) -> None:
    if not fields:
        return
    stamp_last = fields.pop("_stamp_last_updated", True)
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    if stamp_last:
        cols.append("last_updated=datetime('now')")
    vals.append(bot_id)
    conn.execute(
        f"UPDATE bot_positions SET {', '.join(cols)} WHERE id=?", vals
    )


def log_event(
    conn,
    position_id: int,
    kind: str,
    *,
    order_status: str | None = None,
    filled_contracts: int | None = None,
    fill_cost_cents: int | None = None,
    note: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO order_events
              (position_id, kind, order_status, filled_contracts,
               fill_cost_cents, note)
           VALUES (?,?,?,?,?,?)""",
        (position_id, kind, order_status, filled_contracts, fill_cost_cents, note),
    )


def count_open_bot_positions(conn, env: str | None = None) -> int:
    sql = (
        "SELECT COUNT(*) FROM bot_positions "
        "WHERE status IN ('submitted','partial','filled') AND resolved=0"
    )
    args: tuple = ()
    if env:
        sql += " AND network = ?"
        args = (env,)
    return conn.execute(sql, args).fetchone()[0]


def count_new_positions_today(conn, env: str | None = None) -> int:
    """Number of NEW bot-initiated positions opened today.

    Two filters that matter for the daily quota check:

      - `status IN ('submitted','partial','filled')` — count ONLY rows
        that actually became (or are still working toward) a real
        position, matching count_open_bot_positions. Terminal
        non-position rows ('canceled','error','gone','expired') must
        NOT burn a daily slot: a run of unfilled auto-cancels or CLOB
        order rejections would otherwise saturate
        max_daily_new_positions while zero contracts are actually held,
        silently halting all new entries until the day boundary ("runs
        for hours then stops trading, empty portfolio, full balance,
        works next day"). No resolved=0 filter on purpose: a position
        opened AND resolved today keeps status='filled', so it still
        counts as taken-today, which is the cap's intent.
      - `signal_source != 'external'` — reconcile-imported rows from
        Polymarket (positions that already existed externally, or
        positions the bot resolved locally that Polymarket hasn't
        cash-settled yet) are NOT new bot trades. Counting them was
        why setting the daily limit to 300 still produced
        `MAX_DAILY_NEW_POSITIONS` skips after only a few real trades:
        the resolve→reconcile loop kept inserting fresh `external`
        rows for the same (ticker, side) pairs and burning the quota.
    """
    sql = (
        "SELECT COUNT(*) FROM bot_positions "
        "WHERE date(created_at)=date('now') "
        "AND status IN ('submitted','partial','filled') "
        "AND COALESCE(signal_source,'') != 'external'"
    )
    args: tuple = ()
    if env:
        sql += " AND network = ?"
        args = (env,)
    return conn.execute(sql, args).fetchone()[0]


def recent_resolved_position_exists(
    conn,
    ticker: str,
    direction: str,
    env: str,
    within_hours: Optional[int] = None,
) -> bool:
    """Did we already resolve a position for this (ticker, direction, env)?

    Used by the reconciler to break the
    *resolve → reimport-as-external → resolve → reimport* loop when
    Polymarket's `/portfolio/positions` still lists a market we already
    settled locally (Polymarket pays out asynchronously and the position
    can hang on the portfolio for several minutes after settlement).

    Guards on ANY prior resolution by default (`within_hours=None`).
    Polymarket market tickers are UNIQUE per market — a 15-minute up/down
    ticker encodes its exact window and resolves exactly once, ever — so a
    ticker we've resolved can never legitimately need a fresh position again.
    The old 24h bound let markets that resolved >24h ago get re-imported as
    brand-new `external` rows and re-booked as a second full loss on the same
    settled market: 163 imports for 127 distinct tickers, ~$333 of phantom
    double-counted loss (2026-07-07). Pass an explicit `within_hours` only if
    a caller genuinely reuses a ticker across resolutions (none do today).
    """
    sql = (
        "SELECT 1 FROM bot_positions "
        "WHERE ticker = ? AND direction = ? AND network = ? "
        "AND resolved = 1 AND resolved_at IS NOT NULL"
    )
    args: tuple = (ticker, direction, env)
    if within_hours is not None:
        sql += " AND resolved_at >= datetime('now', ?)"
        args += (f"-{int(within_hours)} hours",)
    row = conn.execute(sql + " LIMIT 1", args).fetchone()
    return row is not None


def exists_position_in_event(conn, event_ticker: str, env: str) -> bool:
    if not event_ticker:
        return False
    row = conn.execute(
        """SELECT 1 FROM bot_positions
           WHERE event_ticker=? AND resolved=0 AND network=?
             AND status IN ('submitted','partial','filled')
           LIMIT 1""",
        (event_ticker, env),
    ).fetchone()
    return row is not None


def count_positions_in_event(conn, event_ticker: str, env: str) -> int:
    """Open positions in an event — so `max_positions_per_event` can enforce any
    N, not just 1 (the old `==1` gate became UNLIMITED for any value > 1)."""
    if not event_ticker:
        return 0
    return int(conn.execute(
        """SELECT COUNT(*) FROM bot_positions
           WHERE event_ticker=? AND resolved=0 AND network=?
             AND status IN ('submitted','partial','filled')""",
        (event_ticker, env),
    ).fetchone()[0])


def exists_position_in_market(
    conn, ticker: str, direction: str, env: str
) -> bool:
    row = conn.execute(
        """SELECT 1 FROM bot_positions
           WHERE ticker=? AND direction=? AND resolved=0 AND network=?
             AND status IN ('submitted','partial','filled')
           LIMIT 1""",
        (ticker, direction, env),
    ).fetchone()
    return row is not None


def current_total_exposure_usd(conn, env: str) -> float:
    """$ committed across open positions. A just-placed order sits 'submitted'
    with cost_usd=0 until a poll fills in its cost ~30s later — so summing only
    cost_usd let a burst of placements in one scan each see ~0 exposure and blow
    straight through max_total_exposure_fraction / the cash reserve. Count the
    COMMITTED notional (target_contracts × limit price) for orders that haven't
    booked a real cost yet, and the actual cost once they have."""
    row = conn.execute(
        """SELECT COALESCE(SUM(
               CASE WHEN cost_usd > 0 THEN cost_usd
                    ELSE target_contracts * limit_price_cents / 100.0 END
           ), 0) FROM bot_positions
           WHERE resolved=0 AND status IN ('submitted','partial','filled') AND network=?""",
        (env,),
    ).fetchone()
    return float(row[0] or 0.0)


def current_filled_exposure_usd(conn, env: str) -> float:
    """$ that has actually LEFT the wallet — the booked cost of positions that
    filled. Unlike current_total_exposure_usd this EXCLUDES the notional of still-
    resting ('submitted', cost_usd=0) orders. On Polymarket a resting limit order
    does NOT lock wallet USDC (the CLOB matches off-chain; funds move only on
    execution), so that money is still sitting in the cash balance. The bankroll
    denominator must therefore add only THIS to cash — adding resting notional too
    would double-count the same dollars and let a burst of unfilled orders ratchet
    the exposure cap up to f/(1-f)x the account (3x at 0.75, ~5.7x at 0.85)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(cost_usd), 0) FROM bot_positions
           WHERE resolved=0 AND status IN ('submitted','partial','filled')
             AND cost_usd > 0 AND network=?""",
        (env,),
    ).fetchone()
    return float(row[0] or 0.0)


def get_pending_bot_positions(conn) -> list[dict]:
    """Return rows the order-poller should chase down. Critical filter:
    only rows with a order_id are eligible. Externally-imported
    positions (signal_source='external', order_id IS NULL) are
    NEVER pending — they're already-filled adoptions from
    /portfolio/positions, not orders we placed. Including them here
    caused the dashboard's "Open Positions" tile to flicker between 0
    and 9, because the poller would fetch their cost from Polymarket (which
    sometimes omits market_exposure), set cost back to zero, and the
    next aggregate_stats roll-up would treat them as no-fill noise."""
    rows = conn.execute(
        """SELECT * FROM bot_positions
           WHERE resolved=0
             AND signal_source != 'external'
             AND order_id IS NOT NULL
             AND (
               status IN ('submitted','partial')
               OR (status='filled' AND (cost_usd IS NULL OR cost_usd=0))
               OR (status IN ('canceled','expired','gone','error')
                   AND (cost_usd IS NULL OR cost_usd=0)
                   AND (julianday('now')-julianday(created_at))*86400 < 86400)
             )
           ORDER BY created_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_bot_positions(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM bot_positions
           WHERE status IN ('submitted','partial','filled') AND resolved=0
           ORDER BY created_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_unresolved_bot_positions(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM bot_positions
           WHERE resolved=0 AND status IN ('filled','partial','expired','canceled','gone')
           ORDER BY created_at ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def already_traded_signal_ids(conn, source: str, env: str) -> set[int]:
    rows = conn.execute(
        """SELECT signal_id FROM bot_positions
           WHERE signal_source=? AND network=?
             AND created_at >= datetime('now','-45 days')""",
        (source, env),
    ).fetchall()
    return {int(r["signal_id"]) for r in rows}


def fetch_position_by_id(conn, pos_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bot_positions WHERE id=?", (pos_id,)
    ).fetchone()
    return dict(row) if row else None



def insert_crypto15m_position(conn, row: dict) -> int:
    cur = conn.execute(
        """INSERT INTO crypto15m_positions (
              asset, series, ticker, side, direction, target_contracts,
              filled_contracts, entry_limit_cents, avg_entry_cents, cost_usd,
              client_order_id, order_id, status, exit_reason, close_time,
              confidence, entry_delta_usd, network, dry_run, error, strategy,
              script_id, tp_pct, sl_cents, entry_order_type
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["asset"], row["series"], row["ticker"], row["side"],
            row["direction"], int(row["target_contracts"]),
            int(row.get("filled_contracts", 0)),
            int(row["entry_limit_cents"]),
            row.get("avg_entry_cents"),
            float(row.get("cost_usd", 0.0)),
            row["client_order_id"], row.get("order_id"),
            row["status"], row.get("exit_reason"), row.get("close_time", ""),
            float(row.get("confidence", 0.0)),
            row.get("entry_delta_usd"),
            row.get("network", "mainnet"),
            1 if row.get("dry_run") else 0,
            row.get("error"),
            row.get("strategy"),
            row.get("script_id"),
            row.get("tp_pct"),
            row.get("sl_cents"),
            row.get("entry_order_type"),
        ),
    )
    return cur.lastrowid


def update_crypto15m_position(conn, pid: int, **fields) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("last_updated=datetime('now')")
    vals.append(pid)
    conn.execute(
        f"UPDATE crypto15m_positions SET {', '.join(cols)} WHERE id=?", vals
    )


def fetch_crypto15m_by_id(conn, pid: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM crypto15m_positions WHERE id=?", (pid,)
    ).fetchone()
    return dict(row) if row else None


def get_open_crypto15m(conn, env: str) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE resolved=0 AND network=?
           ORDER BY created_at DESC""",
        (env,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_crypto15m_by_asset(conn, asset: str, env: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE resolved=0 AND asset=? AND network=?
           ORDER BY created_at DESC LIMIT 1""",
        (asset, env),
    ).fetchone()
    return dict(row) if row else None


def count_open_crypto15m(conn, env: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM crypto15m_positions WHERE resolved=0 AND network=?",
        (env,),
    ).fetchone()[0]



def list_user_scripts(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM user_scripts ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_user_script(conn, sid: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM user_scripts WHERE id=?", (sid,)
    ).fetchone()
    return dict(row) if row else None


def upsert_user_script(conn, row: dict) -> None:
    conn.execute(
        """INSERT INTO user_scripts (id, name, description, code, enabled,
              trusted, notes, state_json)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, description=excluded.description,
              code=excluded.code, notes=excluded.notes,
              updated_at=datetime('now')""",
        (
            str(row["id"]), str(row.get("name") or "Untitled script"),
            str(row.get("description") or ""), str(row.get("code") or ""),
            1 if row.get("enabled") else 0,
            1 if row.get("trusted") else 0,
            str(row.get("notes") or ""),
            str(row.get("state_json") or "{}"),
        ),
    )


def update_user_script(conn, sid: str, **fields) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if k not in ("name", "description", "code", "enabled", "trusted",
                     "notes", "state_json", "last_error", "last_error_at",
                     "dry_run", "assets", "author", "needs_review"):
            continue
        cols.append(f"{k}=?")
        vals.append(v)
    if "last_error" in fields and fields["last_error"]:
        cols.append("last_error_at=datetime('now')")
    cols.append("updated_at=datetime('now')")
    vals.append(sid)
    conn.execute(f"UPDATE user_scripts SET {', '.join(cols)} WHERE id=?", vals)


def delete_user_script(conn, sid: str) -> None:
    conn.execute("DELETE FROM user_scripts WHERE id=?", (sid,))


def script_live_stats(conn, env: str) -> dict[str, dict]:
    """Per-script live P&L across BOTH position tables (crypto windows +
    main-market signal copies): {script_id: {n, open, wins, losses, pnlUsd}}
    — the Scripts list's at-a-glance numbers."""
    agg = """SELECT script_id,
                    COUNT(*) AS n,
                    SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS open_n,
                    SUM(CASE WHEN resolved=1 AND COALESCE(pnl_usd,0) > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN resolved=1 AND COALESCE(pnl_usd,0) < 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usd ELSE 0 END), 0) AS pnl
             FROM {table}
             WHERE script_id IS NOT NULL AND network=? AND target_contracts > 0
             GROUP BY script_id"""
    out: dict[str, dict] = {}
    for table in ("crypto15m_positions", "bot_positions"):
        for r in conn.execute(agg.format(table=table), (env,)).fetchall():
            s = out.setdefault(str(r["script_id"]), {
                "n": 0, "open": 0, "wins": 0, "losses": 0, "pnlUsd": 0.0})
            s["n"] += int(r["n"] or 0)
            s["open"] += int(r["open_n"] or 0)
            s["wins"] += int(r["wins"] or 0)
            s["losses"] += int(r["losses"] or 0)
            s["pnlUsd"] = round(s["pnlUsd"] + float(r["pnl"] or 0.0), 2)
    return out



_SHADOW_COLS = (
    "script_id", "source", "signal_source", "signal_id", "asset", "ticker",
    "side", "contracts", "entry_cents", "order_type", "reason", "refused",
    "note", "tp_pct", "sl_cents", "close_time", "network",
)


def insert_script_shadow(conn, row: dict) -> int:
    cols = ", ".join(_SHADOW_COLS)
    marks = ", ".join("?" for _ in _SHADOW_COLS)
    cur = conn.execute(
        f"INSERT INTO script_shadow_orders ({cols}) VALUES ({marks})",
        tuple(row.get(c) for c in _SHADOW_COLS),
    )
    return int(cur.lastrowid)


def shadow_already_attempted(conn, sid: str, ticker: str) -> bool:
    """Shadow mirror of the live one-entry-per-window rule, so a script
    behaves identically in both modes."""
    return conn.execute(
        "SELECT 1 FROM script_shadow_orders WHERE script_id=? AND ticker=? LIMIT 1",
        (sid, ticker),
    ).fetchone() is not None


SHADOW_STALE_DAYS = 7


def count_open_script_shadow(conn, sid: str, env: str) -> int:
    """Open (still settleable) shadow orders for a script. Stale rows are
    excluded so an unsettleable one can't permanently eat the cap.

    ONE definition of stale, shared with resolve_script_shadow(): a row stops
    holding a cap slot when that function can no longer settle it. It retires
    everything `SHADOW_STALE_DAYS` past its close time, so a row WITH a close
    time keeps its slot until it is actually resolved — the only rows this
    clause has to age out itself are the ones with no close time at all, which
    resolve can never reach.

    This used to age out every row `SHADOW_STALE_DAYS` after CREATION, which
    is a different question entirely: a shadow order on a market three weeks
    out silently stopped counting a fortnight before it could possibly settle,
    and the per-script open cap could be exceeded without limit.
    """
    return int(conn.execute(
        f"""SELECT COUNT(*) FROM script_shadow_orders
            WHERE script_id=? AND network=? AND resolved=0 AND contracts > 0
              AND (close_time != ''
                   OR created_at >= datetime('now', '-{SHADOW_STALE_DAYS} days'))""",
        (sid, env),
    ).fetchone()[0] or 0)


def script_shadow_daily_pnl(conn, sid: str, env: str) -> float:
    """Today's simulated realized P&L — drives the same daily-loss breaker as
    live, so a shadow run shows you when the script WOULD have been cut off."""
    row = conn.execute(
        """SELECT COALESCE(SUM(pnl_usd), 0) FROM script_shadow_orders
           WHERE script_id=? AND network=? AND resolved=1
             AND date(COALESCE(resolved_at, created_at)) = date('now')""",
        (sid, env),
    ).fetchone()
    return float(row[0] or 0.0)


def list_script_shadow(conn, sid: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM script_shadow_orders WHERE script_id=?
           ORDER BY id DESC LIMIT ?""",
        (sid, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def script_shadow_stats(conn, env: str) -> dict[str, dict]:
    """{script_id: {n, open, wins, losses, pnlUsd}} for shadow orders — the
    same shape as script_live_stats so the UI renders one component."""
    out: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT script_id,
                  COUNT(*) AS n,
                  SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS open_n,
                  SUM(CASE WHEN resolved=1 AND COALESCE(pnl_usd,0) > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(CASE WHEN resolved=1 AND COALESCE(pnl_usd,0) < 0 THEN 1 ELSE 0 END) AS losses,
                  COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usd ELSE 0 END), 0) AS pnl
           FROM script_shadow_orders
           WHERE network=? AND contracts > 0
           GROUP BY script_id""",
        (env,),
    ).fetchall():
        out[str(r["script_id"])] = {
            "n": int(r["n"] or 0), "open": int(r["open_n"] or 0),
            "wins": int(r["wins"] or 0), "losses": int(r["losses"] or 0),
            "pnlUsd": round(float(r["pnl"] or 0.0), 2),
        }
    return out


def resolve_script_shadow(conn, fee_fn, market_results: dict | None = None,
                          stale_days: int = SHADOW_STALE_DAYS) -> int:
    """Settle shadow orders whose market/signal outcome is now recorded.

    `fee_fn(cost_fraction, category)` supplies the taker fee per contract so
    the simulated P&L is charged the same fee the live path pays.

    `market_results` is {ticker: 'yes'|'no'} fetched from the LIVE API by the
    caller, and it is the only thing that settles decide_market() rows. The
    local `markets` table cannot: it never stores an outcome (verified on a
    live DB 2026-08-03 — 0 of 2,282 rows had `result`, `settlement_value` or
    `status='resolved'`), and closed markets drop out of it entirely when
    `sync_markets` refreshes from the active-markets endpoint, so the row a
    shadow order needs is GONE by the time it could settle. The working
    resolution paths (resolve_alerts_from_markets / resolve_whales_from_markets)
    have always fetched from the API for exactly this reason; this one read the
    table and therefore never fired.

    `stale_days` is the backstop: a shadow order still unresolvable that long
    after its close time is closed out with an UNKNOWN outcome (resolved=1,
    outcome_correct=NULL, pnl NULL) so it stops counting against the script's
    open-position cap. That cap gates EVERY hook, so five stuck rows silently
    retired a whole script while the UI still showed "0W / 0L / $0.00" —
    indistinguishable from "found no setups". Unknown rows are excluded from
    win/loss and P&L rather than guessed at.

    Returns the number of rows settled.
    """
    settled = 0
    market_results = {str(k): str(v).lower()
                      for k, v in (market_results or {}).items()}
    open_rows = conn.execute(
        "SELECT * FROM script_shadow_orders WHERE resolved=0 AND contracts > 0"
    ).fetchall()
    for r in open_rows:
        r = dict(r)
        won: int | None = None
        if str(r.get("source")) == "crypto":
            hit = conn.execute(
                """SELECT up_won FROM crypto15m_signals
                   WHERE ticker=? AND resolved=1 AND up_won IS NOT NULL
                   LIMIT 1""",
                (r.get("ticker"),),
            ).fetchone()
            if hit is not None:
                up_won = bool(hit[0])
                won = int(up_won if r.get("side") == "up" else not up_won)
        elif str(r.get("signal_source")) == "market":
            res = market_results.get(str(r.get("ticker") or ""))
            if res not in ("yes", "no"):
                hit = conn.execute(
                    """SELECT result, settlement_value FROM markets
                       WHERE ticker=? AND (result IN ('yes', 'no')
                                           OR settlement_value IS NOT NULL)""",
                    (r.get("ticker"),),
                ).fetchone()
                if hit is not None:
                    res = str(hit[0] or "").lower()
                    if res not in ("yes", "no") and hit[1] is not None:
                        res = "yes" if float(hit[1]) >= 0.5 else "no"
            if res in ("yes", "no"):
                won = int(res == str(r.get("side") or "").lower())
        else:
            table = ("whale_trades" if r.get("signal_source") == "whale"
                     else "alerts")
            hit = conn.execute(
                f"""SELECT outcome_correct FROM {table}
                    WHERE id=? AND resolved=1 AND outcome_correct IS NOT NULL""",
                (r.get("signal_id"),),
            ).fetchone()
            if hit is not None:
                won = int(bool(hit[0]))
        if won is None:
            close_iso = str(r.get("close_time") or "")
            if close_iso and stale_days > 0:
                stale = conn.execute(
                    "SELECT ? < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                    (close_iso, f"-{int(stale_days)} days"),
                ).fetchone()[0]
                if stale:
                    conn.execute(
                        """UPDATE script_shadow_orders
                           SET resolved=1, outcome_correct=NULL, pnl_usd=NULL,
                               note=?, resolved_at=datetime('now')
                           WHERE id=?""",
                        (f"outcome never became available ({stale_days}d after "
                         "close) — excluded from the record", r["id"]),
                    )
                    settled += 1
            continue
        cost = float(r.get("entry_cents") or 0) / 100.0
        category = ("crypto" if str(r.get("source")) == "crypto"
                    else str(r.get("asset") or ""))
        try:
            fee = float(fee_fn(cost, category))
        except Exception:
            fee = 0.0
        per_ct = (1.0 - cost - fee) if won else (-cost - fee)
        conn.execute(
            """UPDATE script_shadow_orders
               SET resolved=1, outcome_correct=?, pnl_usd=?,
                   resolved_at=datetime('now')
               WHERE id=?""",
            (won, round(per_ct * int(r.get("contracts") or 0), 4), r["id"]),
        )
        settled += 1
    return settled


def crypto15m_owned_keys(conn, env: str, recent_lookback_sql: str = "-6 hours") -> set:
    """(ticker, direction) of crypto15m positions the main-bot reconcile must NOT
    re-import as duplicate `external` bot_positions rows (which would double-count
    win/loss, realized P&L, and exposure). Covers TWO cases:

      1. OPEN rows the 15m engine is working/holding
         (submitted/partial/filled/exiting).
      2. Canceled/error rows with 0 fills — the exact rows the fast 15m endpoints
         mis-mark as dead while the buy ACTUALLY filled on-chain.
         crypto15m_reconcile_candidates is about to promote them to `filled`;
         without shielding them here the main 30s reconcile runs first, sees the
         held token, and imports the SAME on-chain position as an external row →
         double exposure + double settlement P&L that nothing ever removes.

    The lookback was 45 minutes, which let a mis-marked fill that the wallet
    reconcile couldn't promote in time (e.g. the ambiguity skip) age out of the
    shield and get double-imported as external — the reported bug. It's now 6h:
    an updown ticker (`btc-updown-15m-<ts>`) is UNIQUE to the crypto engine and
    never traded by the main bot, so any live on-chain position on it is the
    crypto engine's — shielding it for the full settlement window is safe, and
    genuinely-unfilled rows have no on-chain holding so shielding them is a
    harmless no-op regardless of age.

    Rule: shield (ticker, direction) for ANY crypto15m row created within the
    lookback, regardless of status/resolved. The previous two-case query
    (open-OR-recently-canceled) left a gap the old code didn't cover — a row the
    crypto engine had already SETTLED (resolved=1, filled>0) matched neither
    case, so once it booked its fill the shield dropped; if on-chain redemption
    then lagged, the main reconcile imported the same position as an external row
    (the reported `settled -4.47 AND external -4.50` double-count). Because an
    updown ticker is unique to this engine and never traded by the main bot, a
    single "any recent crypto row on this ticker" rule is both simpler and
    strictly safer. Unresolved rows are shielded at ANY age (a still-open/holding
    position can't lose its shield just because a market stalled past the
    window); resolved rows — settled-but-redemption-lagging, or canceled/error
    0-fill mis-marks — are shielded for the lookback, long enough for the
    on-chain position to clear or the wallet reconcile to reclaim it."""
    rows = conn.execute(
        """SELECT DISTINCT ticker, direction FROM crypto15m_positions
           WHERE network=? AND (
                 resolved=0
              OR created_at >= datetime('now', ?)
           )""",
        (env, recent_lookback_sql),
    ).fetchall()
    return {(r["ticker"], r["direction"]) for r in rows}


def crypto15m_errored_tickers(conn, env: str) -> set:
    """Tickers whose ENTRY order errored (e.g. exchange paused). Error rows
    resolve immediately so they don't pin a slot — this set is what stops
    the executor re-attempting the same market every tick instead. Age-capped:
    it only guards re-entry within the CURRENT 15-minute window (a ticker's
    conditionId is unique per window), so old rows never match anyway."""
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM crypto15m_positions
           WHERE network=? AND status='error' AND ticker IS NOT NULL
             AND created_at >= datetime('now','-6 hours')""",
        (env,),
    ).fetchall()
    return {r["ticker"] for r in rows}


def crypto15m_today_pnl(conn, env: str) -> float:
    """Realized crypto15m P&L for today (UTC) on this env — drives the daily
    loss circuit-breaker. Robust to a mid-day history wipe: folds in any today
    P&L banked when clear_trade_history/factory_reset removed today's resolved
    rows, so wiping stats can't re-arm trading past the daily limit."""
    return engine_today_pnl(conn, "crypto15m", env)


def crypto15m_today_open_cost(conn, env: str) -> float:
    """Committed cost of crypto15m positions OPENED today that are still open —
    the extra realized loss they would book if each settled worthless (a
    buy-only binary can lose its full cost). Added to today's realized figure so
    the daily breaker halts BEFORE a settlement wave of up-to-max_concurrent open
    bets overshoots the cap. Resting orders (cost_usd still 0) are valued at
    committed notional (contracts × limit)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(
               CASE WHEN cost_usd > 0 THEN cost_usd
                    ELSE target_contracts * entry_limit_cents / 100.0 END
           ), 0) FROM crypto15m_positions
           WHERE network=? AND resolved=0
             AND status IN ('submitted','partial','filled')
             AND date(created_at)=date('now')""",
        (env,),
    ).fetchone()
    return float(row[0] or 0.0)


def kv_get(conn, key: str) -> str | None:
    """Read a value from the durable app_kv store. None if unset."""
    row = conn.execute("SELECT v FROM app_kv WHERE k=?", (key,)).fetchone()
    return None if row is None else row[0]


def kv_set(conn, key: str, value: str) -> None:
    """Upsert a value into the durable app_kv store."""
    conn.execute(
        """INSERT INTO app_kv (k, v, updated_at)
           VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
           ON CONFLICT(k) DO UPDATE SET
               v=excluded.v, updated_at=excluded.updated_at""",
        (key, str(value)),
    )


def kv_delete(conn, key: str) -> None:
    """Remove a key from the durable app_kv store (no-op if absent)."""
    conn.execute("DELETE FROM app_kv WHERE k=?", (key,))


TERMINAL_RECONCILE_FLOOR_KEY = "terminal:reconcile_floor"


def set_terminal_reconcile_floor(conn, when: str | None = None) -> str:
    """Stamp 'reconcile nothing older than this'. Returns the floor written."""
    iso = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kv_set(conn, TERMINAL_RECONCILE_FLOOR_KEY, iso)
    return iso


def terminal_reconcile_floor(conn) -> str | None:
    """The wipe floor, or None when no wipe has happened."""
    v = kv_get(conn, TERMINAL_RECONCILE_FLOOR_KEY)
    return v or None


_LIFETIME_PNL_PREFIX = "lifetime_pnl_wiped:"
_DAILY_PNL_PREFIX = "daily_pnl_wiped:"


def _engine_pnl_source(engine: str) -> tuple[str, str]:
    """(table, extra_where) locating an engine's resolved-P&L rows."""
    if engine == "crypto15m":
        return "crypto15m_positions", "AND dry_run=0"
    if engine == "copy":
        return "bot_positions", "AND signal_source='copy'"
    if engine == "main":
        return "bot_positions", "AND signal_source IN ('whale','momentum')"
    raise ValueError(f"unknown engine {engine!r}")


def _engine_resolved_pnl_sum(
    conn, engine: str, env: str, *, today_only: bool = False
) -> float:
    table, extra = _engine_pnl_source(engine)
    sql = (
        f"SELECT COALESCE(SUM(pnl_usd),0) FROM {table} "
        f"WHERE network=? AND resolved=1 AND pnl_usd IS NOT NULL {extra}"
    )
    if today_only:
        sql += " AND date(resolved_at)=date('now')"
    return float(conn.execute(sql, (env,)).fetchone()[0] or 0.0)


def _daily_wiped_pnl(conn, engine: str, env: str) -> float:
    """Today's realized P&L that a history wipe folded away (0 if the stored
    figure is from a previous UTC day — the new day starts clean)."""
    import json
    raw = kv_get(conn, f"{_DAILY_PNL_PREFIX}{engine}:{env}")
    if not raw:
        return 0.0
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return 0.0
    if d.get("date") != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return 0.0
    return _to_float(d.get("pnl"))


def lifetime_realized_pnl(conn, engine: str, env: str) -> float:
    """Cumulative realized P&L for an engine, INCLUDING rows a history wipe
    removed (their P&L is banked into a durable counter first). Negative = net
    loss. Drives the lifetime drawdown circuit-breaker."""
    wiped = _to_float(kv_get(conn, f"{_LIFETIME_PNL_PREFIX}{engine}:{env}"))
    return wiped + _engine_resolved_pnl_sum(conn, engine, env)


def engine_today_pnl(conn, engine: str, env: str) -> float:
    """Realized P&L today (UTC) for an engine, robust to a mid-day history wipe
    (rows still present + any today-P&L banked when a wipe folded them away).
    Drives the daily loss circuit-breaker."""
    return _daily_wiped_pnl(conn, engine, env) + _engine_resolved_pnl_sum(
        conn, engine, env, today_only=True
    )


def _known_envs(conn) -> set:
    """Every network tag seen in the two engine position tables (plus the
    default), so the wipe can bank each env's counter."""
    envs = {"mainnet"}
    for tbl in ("crypto15m_positions", "bot_positions"):
        try:
            for r in conn.execute(f"SELECT DISTINCT network FROM {tbl}"):
                if r[0]:
                    envs.add(r[0])
        except sqlite3.OperationalError:
            pass
    return envs


def bank_realized_pnl_before_wipe(conn) -> None:
    """Fold the resolved P&L of rows ABOUT to be wiped into the durable lifetime
    + today loss-guard counters, so clear_trade_history / factory_reset cannot
    disarm the drawdown circuit-breakers by erasing the stats they read. MUST be
    called BEFORE the position rows are deleted."""
    import json
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for engine in ("crypto15m", "copy", "main"):
        for env in _known_envs(conn):
            all_sum = _engine_resolved_pnl_sum(conn, engine, env)
            if abs(all_sum) > 1e-9:
                key = f"{_LIFETIME_PNL_PREFIX}{engine}:{env}"
                kv_set(conn, key, repr(_to_float(kv_get(conn, key)) + all_sum))
            today_sum = _engine_resolved_pnl_sum(conn, engine, env, today_only=True)
            if abs(today_sum) > 1e-9:
                banked = _daily_wiped_pnl(conn, engine, env)
                kv_set(
                    conn, f"{_DAILY_PNL_PREFIX}{engine}:{env}",
                    json.dumps({"date": today, "pnl": banked + today_sum}),
                )


def effective_start_bankroll(conn, env: str, configured: float = 0.0) -> float:
    """Bankroll baseline for percent-of-bankroll risk limits: the user's pinned
    start_bankroll_usd when > 0, else the auto-detected first recorded total for
    env. 0 when neither is known (callers treat 0 as 'cannot size the % cap')."""
    try:
        v = float(configured or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    if v > 0:
        return v
    auto = earliest_pnl_total(conn, env)
    try:
        return float(auto) if auto and float(auto) > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def crypto15m_attempted_tickers(conn, env: str) -> set:
    """Every market (ticker) we've ALREADY placed an entry on. 15-minute markets
    have a unique conditionId per window, so "has any row" == "attempted this
    window" — re-using this as a skip set enforces ONE entry attempt per market.
    Without it, a maker order that cancels unfilled (resolved=1) frees the asset
    and the executor re-enters the SAME market next tick, spamming the book with
    orders that cancel immediately — especially in the final minute where each
    re-placed order is already inside its own cancel lead.

    Age-capped: the guard only matters within the CURRENT window (a fresh
    window is a fresh conditionId/ticker), so don't full-scan the ever-growing
    table every 4s tick — rows older than 6 hours can't collide."""
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM crypto15m_positions
           WHERE network=? AND ticker IS NOT NULL
             AND created_at >= datetime('now','-6 hours')""",
        (env,),
    ).fetchall()
    return {r["ticker"] for r in rows}


def crypto15m_reconcile_candidates(conn, env: str, lookback_sql: str = "-6 hours") -> list[dict]:
    """Entries the engine gave up on (canceled/error, 0 fills) within the lookback
    window. The fast 15m order/trades endpoints lag, so some of these ACTUALLY
    filled on-chain and were mis-marked — the wallet reconcile re-checks each
    against live holdings and promotes the ones we really hold. `lookback_sql` is
    a SQLite datetime modifier (e.g. '-6 hours', '-2 days' for a backfill).

    The default was 45 minutes, which matched the old (too-narrow) shield window
    in crypto15m_owned_keys and let a mis-marked fill stop being reclaimed before
    it was ever booked under crypto — surfacing later as an unexplained on-chain
    loss. 6h gives the wallet reconcile the full settlement window to promote
    it, so the loss lands in the crypto stats where it belongs."""
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE network=? AND COALESCE(filled_contracts,0)=0
             AND status IN ('canceled','error')
             AND created_at >= datetime('now', ?)
           ORDER BY id DESC""",
        (env, lookback_sql),
    ).fetchall()
    return [dict(r) for r in rows]


def crypto15m_ticker_counts(conn, ticker: str, env: str) -> dict:
    """How many rows exist for a ticker, and how many already record a real fill.
    Used by the wallet reconcile to refuse promoting a phantom row when the held
    contracts could belong to another row on the same market (avoids
    double-counting a single wallet position across rows)."""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN COALESCE(filled_contracts,0)>0 THEN 1 ELSE 0 END) AS filled_rows
           FROM crypto15m_positions WHERE network=? AND ticker=?""",
        (env, ticker),
    ).fetchone()
    return {"total": int(row["total"] or 0), "filled_rows": int(row["filled_rows"] or 0)}


def recent_crypto15m(conn, env: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE network=?
           ORDER BY created_at DESC LIMIT ?""",
        (env, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def crypto15m_strategy_stats(conn, env: str) -> list[dict]:
    """P&L per strategy (favorite/contrarian/rules). Only real fills; the
    attribution column is stamped at entry. `fees_usd` sums whatever the venue
    actually reported for entry+exit fills (0 when the CLOB charges none)."""
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(strategy, ''), 'directional') strategy,
                  COUNT(*) n,
                  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
                  SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) losses,
                  ROUND(SUM(pnl_usd), 4) pnl_usd,
                  ROUND(SUM(COALESCE(fees_usd,0) + COALESCE(exit_fees_usd,0)), 4) fees_usd
           FROM crypto15m_positions
           WHERE network=? AND resolved=1 AND filled_contracts>0
             AND dry_run=0 AND pnl_usd IS NOT NULL
           GROUP BY 1 ORDER BY SUM(pnl_usd) DESC""",
        (env,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_crypto15m_resolved(conn, env: str, limit: int = 200) -> list[dict]:
    """Resolved 15m trades for the History page (newest first)."""
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE network=? AND resolved=1 AND filled_contracts>0 AND dry_run=0
           ORDER BY resolved_at DESC, id DESC LIMIT ?""",
        (env, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def crypto15m_stats(conn, env: str) -> dict:
    """Headline 15m counters. `dry_run=0` to match its two siblings
    (crypto15m_strategy_stats, recent_crypto15m_resolved) — without it a
    simulated row counted as a win, a loss and realized P&L, and this figure
    also drives the overall take-profit kill-switch."""
    row = conn.execute(
        """SELECT
              SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS open_count,
              SUM(CASE WHEN resolved=1 AND outcome_correct=1 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN resolved=1 AND outcome_correct=0 THEN 1 ELSE 0 END) AS losses,
              COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usd END),0) AS realized_pnl,
              COUNT(*) AS total
           FROM crypto15m_positions WHERE network=? AND dry_run=0""",
        (env,),
    ).fetchone()
    return {
        "openCount": int(row["open_count"] or 0),
        "wins": int(row["wins"] or 0),
        "losses": int(row["losses"] or 0),
        "realizedPnlUsd": float(row["realized_pnl"] or 0.0),
        "total": int(row["total"] or 0),
    }



def insert_crypto15m_signal(conn, row: dict) -> bool:
    """Record one decision-point signal. UNIQUE(ticker) — returns True if a
    new row was inserted, False if this market was already logged."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO crypto15m_signals (
              ticker, asset, series, close_time, mins_left, favorite,
              favorite_price, entry_cost, up_prob, delta_pct, open_spot,
              obs_spot, book_imbalance, macd, macd_signal, macd_hist,
              macd_cross, rsi, strike, model_prob, edge_net_cents, network,
              interval, strike_source
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ticker"], row["asset"], row.get("series", ""),
            row.get("close_time", ""), row.get("mins_left"),
            row.get("favorite"), row.get("favorite_price"),
            row.get("entry_cost"), row.get("up_prob"), row.get("delta_pct"),
            row.get("open_spot"), row.get("obs_spot"),
            row.get("book_imbalance"),
            row.get("macd"), row.get("macd_signal"), row.get("macd_hist"),
            row.get("macd_cross"), row.get("rsi"),
            row.get("strike"), row.get("model_prob"), row.get("edge_net_cents"),
            row.get("network", "mainnet"),
            row.get("interval", "15m"),
            row.get("strike_source"),
        ),
    )
    return (cur.rowcount or 0) > 0


def unresolved_crypto15m_signals(conn, limit: int = 50) -> list[dict]:
    """Logged signals still awaiting their settled outcome, oldest first."""
    rows = conn.execute(
        """SELECT * FROM crypto15m_signals
           WHERE resolved=0
           ORDER BY close_time ASC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_crypto15m_signal(conn, ticker: str, up_won: int) -> None:
    conn.execute(
        """UPDATE crypto15m_signals
              SET resolved=1, up_won=?, settled_at=datetime('now')
            WHERE ticker=?""",
        (int(up_won), ticker),
    )


def fetch_resolved_crypto15m_signals(conn) -> list[dict]:
    """All settled signals — the dataset the 15m backtest replays."""
    rows = conn.execute(
        """SELECT * FROM crypto15m_signals
           WHERE resolved=1 AND up_won IS NOT NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


def crypto15m_signal_counts(conn) -> dict:
    row = conn.execute(
        """SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
              SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS pending
           FROM crypto15m_signals"""
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "resolved": int(row["resolved"] or 0),
        "pending": int(row["pending"] or 0),
    }


_C15_TICKS_KEEP_DAYS = 60


def insert_clob_trades(conn, rows: list[dict], network: str = "mainnet") -> int:
    """Bulk-append trade prints drained from the market WS feed."""
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO clob_trades
              (asset_id, market, price, size, side, source_ts_ms, tx_hash, network)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(r.get("asset_id"), r.get("market"), r.get("price"), r.get("size"),
          r.get("side"), r.get("source_ts_ms"), r.get("tx"), network)
         for r in rows if r.get("asset_id") and r.get("price") is not None],
    )
    return len(rows)


def insert_crypto15m_tick(conn, row: dict) -> None:
    """Append one whole-window observation of an active 15m market."""
    conn.execute(
        """INSERT INTO crypto15m_ticks (
              ticker, asset, mins_left, yes_bid, yes_ask, up_prob,
              spot, open_spot, delta_pct, book_imbalance, macd, macd_signal,
              macd_hist, macd_cross, rsi, ws_bid, ws_ask,
              strike, delta_signed_pct, sigma1m, model_prob, edge_net_cents,
              no_ask, spot_source, up_ask,
              vwap1h, ema12, sma20, sma50, price_vs_vwap_pct,
              ema12_vs_sma20_pct, ema1_vs_sma5_pct, velocity1m_pct,
              change5m_pct, change15m_pct,
              network, interval, strike_source, settle_twap
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                     ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ticker"], row["asset"], row.get("mins_left"),
            row.get("yes_bid"), row.get("yes_ask"), row.get("up_prob"),
            row.get("spot"), row.get("open_spot"), row.get("delta_pct"),
            row.get("book_imbalance"),
            row.get("macd"), row.get("macd_signal"), row.get("macd_hist"),
            row.get("macd_cross"), row.get("rsi"),
            row.get("ws_bid"), row.get("ws_ask"),
            row.get("strike"), row.get("delta_signed_pct"), row.get("sigma1m"),
            row.get("model_prob"), row.get("edge_net_cents"),
            row.get("no_ask"), row.get("spot_source"), row.get("up_ask"),
            row.get("vwap1h"), row.get("ema12"), row.get("sma20"),
            row.get("sma50"), row.get("price_vs_vwap_pct"),
            row.get("ema12_vs_sma20_pct"), row.get("ema1_vs_sma5_pct"),
            row.get("velocity1m_pct"), row.get("change5m_pct"),
            row.get("change15m_pct"),
            row.get("network", "mainnet"),
            row.get("interval", "15m"),
            row.get("strike_source"), row.get("settle_twap"),
        ),
    )


def crypto15m_tick_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM crypto15m_ticks").fetchone()[0])



def insert_pnl_snapshot(
    conn,
    *,
    cash_usd: float,
    portfolio_usd: float,
    realized_pnl_usd: float,
    wins: int,
    losses: int,
    open_positions: int,
    env: str,
) -> None:
    conn.execute(
        """INSERT INTO pnl_snapshots
              (network, cash_usd, portfolio_usd, total_usd, realized_pnl_usd,
               wins, losses, open_positions)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            env,
            float(cash_usd),
            float(portfolio_usd),
            float(cash_usd) + float(portfolio_usd),
            float(realized_pnl_usd),
            int(wins),
            int(losses),
            int(open_positions),
        ),
    )
    pnl_baseline(conn, env)


_PNL_BASELINE_PREFIX = "pnl_baseline:"


def pnl_baseline(conn, env: str) -> dict | None:
    """{'total': float, 'at': 'YYYY-MM-DD HH:MM:SS'} — the first-ever
    recorded total for env, pinned the first time it's seen (from the
    oldest snapshot still on file, for a DB that predates the pin)."""
    raw = kv_get(conn, _PNL_BASELINE_PREFIX + env)
    if raw:
        try:
            v = json.loads(raw)
            return {"total": float(v["total"]), "at": str(v["at"])}
        except (ValueError, TypeError, KeyError):
            pass
    row = conn.execute(
        """SELECT total_usd, at FROM pnl_snapshots
           WHERE network = ?
           ORDER BY at ASC LIMIT 1""",
        (env,),
    ).fetchone()
    if row is None:
        return None
    try:
        base = {"total": float(row[0]), "at": str(row[1])}
    except (TypeError, ValueError):
        return None
    kv_set(conn, _PNL_BASELINE_PREFIX + env, json.dumps(base))
    return base


def earliest_pnl_total(conn, env: str) -> float | None:
    """First-ever recorded total balance for this env. Used as the
    auto-detected bankroll baseline when the user hasn't pinned a
    starting bankroll explicitly. Returns None if no snapshot exists."""
    base = pnl_baseline(conn, env)
    return base["total"] if base else None


def first_snapshot_of_today(conn, env: str) -> dict | None:
    """First snapshot recorded today (UTC) for env. Used as the baseline for
    "today P&L = total_now - today_baseline". Returns the full snapshot row
    or None if none yet today. The predicate is a SARGABLE range served by
    idx_pnl_env_at — the old `date(at) = date('now')` form full-scanned the
    snapshot history on every daily-risk check and account build."""
    row = conn.execute(
        """SELECT * FROM pnl_snapshots
           WHERE network = ? AND at >= strftime('%Y-%m-%d 00:00:00', 'now')
           ORDER BY at ASC LIMIT 1""",
        (env,),
    ).fetchone()
    return dict(row) if row else None


def latest_snapshot(conn, env: str) -> dict | None:
    """Most recent snapshot for env, or None if none exist yet."""
    row = conn.execute(
        """SELECT * FROM pnl_snapshots
           WHERE network = ?
           ORDER BY at DESC LIMIT 1""",
        (env,),
    ).fetchone()
    return dict(row) if row else None


_TRANSFER_MIN_JUMP_USD = 25.0


def transfer_adjustment_today(conn, env: str) -> float:
    """Cumulative net transfers detected today, UTC (deposits +, withdrawals −).
    Subtract from the raw balance delta to get trading-only day P&L."""
    row = conn.execute(
        "SELECT v FROM app_kv WHERE k = ?",
        (f"transfers:{env}:" + _utc_today(),),
    ).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def _utc_today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def transfer_adjustment_total(conn, env: str) -> float:
    """Cumulative net transfers ever recorded for this env (deposits +,
    withdrawals −), summed across every daily `transfers:` key. Subtract from
    the raw all-time balance delta so a withdrawal doesn't read as lifetime
    trading loss forever (the daily key already protects today's P&L; the
    all-time figure was left unadjusted and went permanently negative after
    any withdrawal). A history wipe deletes these keys AND the pnl_snapshots
    baseline together, so the adjustment stays consistent with its baseline."""
    base = pnl_baseline(conn, env)
    since = base["at"][:10] if base else ""
    total = 0.0
    for row in conn.execute(
        "SELECT k, v FROM app_kv WHERE k LIKE ?", (f"transfers:{env}:%",)
    ):
        if since and str(row[0]).rsplit(":", 1)[-1] < since:
            continue
        try:
            total += float(row[1])
        except (TypeError, ValueError):
            continue
    return total


def transfer_adjustment_run(conn, env: str, run_id: int) -> float:
    """Net transfers detected DURING a specific bot run (deposits +,
    withdrawals −). Subtract from the raw session balance delta so a mid-run
    withdrawal doesn't fake a deep-red session (and a mid-run deposit doesn't
    fake a green one)."""
    if not run_id:
        return 0.0
    row = conn.execute(
        "SELECT v FROM app_kv WHERE k = ?", (f"transfers_run:{env}:{int(run_id)}",)
    ).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def note_transfer_if_unexplainable(
    conn, env: str, new_total: float, run_id: int = 0
) -> float:
    """Called by the snapshot heartbeat BEFORE persisting a new snapshot.
    Returns the transfer amount recorded (0.0 when the jump is explainable
    as trading). Idempotent per heartbeat: it compares against the latest
    PERSISTED snapshot, and the caller persists right after. When `run_id`
    is given, the transfer is also accumulated per-run so session P&L can
    net it out."""
    prev = latest_snapshot(conn, env)
    if not prev:
        return 0.0
    try:
        jump = float(new_total) - float(prev["total_usd"] or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if abs(jump) < _TRANSFER_MIN_JUMP_USD:
        return 0.0
    since = str(prev.get("at") or "")
    open_cost = conn.execute(
        """SELECT COALESCE((SELECT SUM(cost_usd) FROM bot_positions
                             WHERE resolved=0 AND filled_contracts>0 AND network=?),0)
                + COALESCE((SELECT SUM(cost_usd) FROM crypto15m_positions
                             WHERE resolved=0 AND filled_contracts>0 AND network=?),0)""",
        (env, env),
    ).fetchone()[0] or 0.0
    recent = conn.execute(
        """SELECT COALESCE((SELECT SUM(ABS(COALESCE(pnl_usd,0)))+SUM(COALESCE(cost_usd,0))
                             FROM bot_positions
                             WHERE network=? AND last_updated >= ?),0)
                + COALESCE((SELECT SUM(ABS(COALESCE(pnl_usd,0)))+SUM(COALESCE(cost_usd,0))
                             FROM crypto15m_positions
                             WHERE network=? AND last_updated >= ?),0)""",
        (env, since, env, since),
    ).fetchone()[0] or 0.0
    explainable = float(open_cost) + float(recent) + 5.0
    if abs(jump) <= explainable:
        return 0.0
    key = f"transfers:{env}:" + _utc_today()
    row = conn.execute("SELECT v FROM app_kv WHERE k = ?", (key,)).fetchone()
    try:
        prior = float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        prior = 0.0
    kv_set(conn, key, repr(prior + jump))
    if run_id:
        rkey = f"transfers_run:{env}:{int(run_id)}"
        kv_set(conn, rkey, repr(transfer_adjustment_run(conn, env, run_id) + jump))
    return jump



def start_bot_run(
    conn, *, env: str, cash_usd: float, portfolio_usd: float,
    lifetime_trades: int = 0, lifetime_wins: int = 0, lifetime_losses: int = 0,
) -> int:
    """Open a new bot_runs row. Returns its id (the active run id).

    `lifetime_*` are the env-scoped lifetime counters AT THE MOMENT THIS
    RUN STARTS. Heartbeats compute per-run trade counts as deltas from
    these baselines so the History → Run History table shows trades
    opened **during this session**, not lifetime totals.

    Also closes any previous unfinished run for this env so we never
    have two open runs simultaneously (e.g. after a hard crash).
    """
    conn.execute(
        """UPDATE bot_runs
           SET ended_at = COALESCE(ended_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
               notes    = COALESCE(notes, '') || ' [auto-closed on next start]'
           WHERE network = ? AND ended_at IS NULL""",
        (env,),
    )
    total = float(cash_usd) + float(portfolio_usd)
    cur = conn.execute(
        """INSERT INTO bot_runs (
              network, start_cash_usd, start_portfolio_usd,
              start_total_usd, end_cash_usd, end_portfolio_usd,
              end_total_usd, pnl_usd,
              start_trades_opened, start_trades_won, start_trades_lost,
              trades_opened, trades_won, trades_lost
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, 0, 0)""",
        (
            env, float(cash_usd), float(portfolio_usd), total,
            float(cash_usd), float(portfolio_usd), total,
            int(lifetime_trades), int(lifetime_wins), int(lifetime_losses),
        ),
    )
    return int(cur.lastrowid or 0)


def heartbeat_bot_run(
    conn, run_id: int, *, cash_usd: float, portfolio_usd: float,
    lifetime_trades: int = 0, lifetime_wins: int = 0, lifetime_losses: int = 0,
) -> None:
    """Update the active run's running totals. Called on every account
    snapshot push so the run's end_balance/pnl is always current.

    `lifetime_*` are the CURRENT env-scoped lifetime counters. Per-run
    trade counts are computed in SQL as the difference vs the
    `start_trades_*` baseline captured by `start_bot_run`."""
    if not run_id:
        return
    total = float(cash_usd) + float(portfolio_usd)
    conn.execute(
        """UPDATE bot_runs
              SET end_cash_usd = ?,
                  end_portfolio_usd = ?,
                  end_total_usd = ?,
                  pnl_usd = ? - start_total_usd,
                  trades_opened = MAX(0, ? - start_trades_opened),
                  trades_won    = MAX(0, ? - start_trades_won),
                  trades_lost   = MAX(0, ? - start_trades_lost)
            WHERE id = ?""",
        (
            float(cash_usd), float(portfolio_usd), total, total,
            int(lifetime_trades), int(lifetime_wins), int(lifetime_losses),
            int(run_id),
        ),
    )


def end_bot_run(conn, run_id: int) -> None:
    """Mark the run as ended (now, ISO 8601 UTC). Idempotent."""
    if not run_id:
        return
    conn.execute(
        "UPDATE bot_runs SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ? AND ended_at IS NULL",
        (int(run_id),),
    )


def get_active_run(conn, env: str) -> dict | None:
    """Return the current open run for env (if any)."""
    row = conn.execute(
        """SELECT * FROM bot_runs
            WHERE network = ? AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1""",
        (env,),
    ).fetchone()
    return dict(row) if row else None


def get_recent_runs(conn, env: str | None = None, limit: int = 50) -> list[dict]:
    """List the most recent runs (newest first), optionally filtered by env."""
    if env:
        rows = conn.execute(
            """SELECT * FROM bot_runs
                WHERE network = ?
                ORDER BY started_at DESC LIMIT ?""",
            (env, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bot_runs ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pnl_snapshots(
    conn, *, since_hours: int = 168, env: str | None = None,
    max_points: int = 2000,
) -> list[dict]:
    since_hours = max(1, min(int(since_hours), 24 * 365))
    sql = "SELECT * FROM pnl_snapshots WHERE at >= datetime('now', ?)"
    args: list = [f"-{since_hours} hours"]
    if env:
        sql += " AND network = ?"
        args.append(env)
    sql += " ORDER BY at ASC"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    if max_points and len(rows) > max_points:
        stride = -(-len(rows) // max_points)
        sampled = rows[::stride]
        if sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        rows = sampled
    return rows


def recent_balance_transition(conn, env: str, within_sec: int = 180) -> bool:
    """True when a bot position was OPENED or RESOLVED within the last
    `within_sec` seconds — the window where the venue's cash/value reads and
    our position ledger can disagree (an entry's debit / a settlement's
    payout is in flight), so displayed totals transiently dip by ~one
    position or lag a win. Consumers show a 'syncing' hint instead of the
    scary number. Keys on created_at / resolved_at, NOT last_updated —
    reconcile passes touch last_updated constantly on open rows."""
    args = (env, f"-{int(within_sec)} seconds", f"-{int(within_sec)} seconds")
    for table, extra in (("bot_positions", ""),
                         ("crypto15m_positions", "AND dry_run = 0")):
        row = conn.execute(
            f"""SELECT 1 FROM {table}
                WHERE network = ? {extra}
                  AND ((resolved_at IS NOT NULL AND resolved_at >= datetime('now', ?))
                    OR (created_at >= datetime('now', ?)
                        AND status IN ('submitted', 'partial', 'filled')))
                LIMIT 1""",
            args,
        ).fetchone()
        if row:
            return True
    return False


def aggregate_stats(conn, env: str | None = None) -> dict:
    cond = ""
    args: list = []
    if env:
        cond = "AND network=?"
        args = [env]
    row = conn.execute(
        f"""SELECT
              SUM(CASE WHEN status IN ('submitted','partial') AND resolved=0 THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='filled' AND resolved=0 THEN 1 ELSE 0 END) AS open_filled,
              SUM(CASE WHEN resolved=1 AND outcome_correct=1 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN resolved=1 AND outcome_correct=0 THEN 1 ELSE 0 END) AS losses,
              COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usd END),0) AS realized_pnl,
              COALESCE(SUM(CASE WHEN resolved=1 AND date(resolved_at)=date('now') THEN pnl_usd END),0) AS today_pnl,
              SUM(CASE WHEN resolved=1 AND outcome_correct=1 AND date(resolved_at)=date('now') THEN 1 ELSE 0 END) AS today_wins,
              SUM(CASE WHEN resolved=1 AND outcome_correct=0 AND date(resolved_at)=date('now') THEN 1 ELSE 0 END) AS today_losses,
              COALESCE(SUM(CASE WHEN resolved=0 AND status IN ('filled','partial') THEN cost_usd END),0) AS open_cost,
              COALESCE(SUM(fees_usd),0) AS fees,
              SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved_count,
              COUNT(*) AS total_opened
           FROM bot_positions
           WHERE status!='dry_run' {cond}""",
        args,
    ).fetchone()
    return {
        "pending": int(row["pending"] or 0),
        "open_filled": int(row["open_filled"] or 0),
        "wins": int(row["wins"] or 0),
        "losses": int(row["losses"] or 0),
        "realized_pnl": float(row["realized_pnl"] or 0.0),
        "today_pnl": float(row["today_pnl"] or 0.0),
        "today_wins": int(row["today_wins"] or 0),
        "today_losses": int(row["today_losses"] or 0),
        "open_cost": float(row["open_cost"] or 0.0),
        "fees": float(row["fees"] or 0.0),
        "resolved_count": int(row["resolved_count"] or 0),
        "total_opened": int(row["total_opened"] or 0),
    }


_DELETE_BATCH = 50_000


def _delete_batched(
    where_sql: str, params: tuple, table: str, *, batch: int = _DELETE_BATCH
) -> int:
    """Delete `table` rows matching `where_sql`, one committed chunk at a
    time, so the write lock is released between batches and the live loop
    interleaves even when clearing millions of rows. Returns rows deleted."""
    sql = (
        f"DELETE FROM {table} WHERE rowid IN "
        f"(SELECT rowid FROM {table} WHERE {where_sql} LIMIT ?)"
    )
    total = 0
    while True:
        with get_db() as conn:
            n = conn.execute(sql, (*params, batch)).rowcount or 0
        total += n
        if n < batch:
            return total


RESEARCH_ARCHIVE_NAME = "research-archive.db"
_ARCHIVE = "research_archive"
_SIGNAL_KEEP_DAYS = 45
_C15_SIGNAL_KEEP_DAYS = 60
_RESEARCH_TABLES = {
    "crypto15m_signals": ("observed_at", _C15_SIGNAL_KEEP_DAYS),
    "crypto15m_ticks": ("observed_at", _C15_TICKS_KEEP_DAYS),
    "whale_trades": ("created_at", _SIGNAL_KEEP_DAYS),
    "alerts": ("created_at", _SIGNAL_KEEP_DAYS),
}
_ARCHIVE_BATCH = 10_000


def research_archive_path() -> Path:
    return db_path().with_name(RESEARCH_ARCHIVE_NAME)


@contextmanager
def _research_archive():
    """A private connection to the live DB with the archive ATTACHed, in
    autocommit mode so every statement below commits on its own."""
    conn = _open_conn(str(db_path()))
    try:
        conn.isolation_level = None
        conn.execute(f"ATTACH DATABASE ? AS {_ARCHIVE}", (str(research_archive_path()),))
        conn.execute(f"PRAGMA {_ARCHIVE}.journal_mode=WAL")
        yield conn
    finally:
        conn.close()


def _archive_columns(conn, table: str) -> list[str]:
    """Create (or extend) `table` in the archive so it carries every live
    column, and return the live column list. A migration that adds a column
    to the live table must not silently drop it from archived rows."""
    tcol = _RESEARCH_TABLES[table][0]
    cols = [(r[1], r[2] or "") for r in conn.execute(f"PRAGMA main.table_info({table})")]
    have = {r[1] for r in conn.execute(f"PRAGMA {_ARCHIVE}.table_info({table})")}
    if not have:
        defs = ", ".join(f"{n} {t}".strip() for n, t in cols)
        conn.execute(f"CREATE TABLE {_ARCHIVE}.{table} ({defs})")
        conn.execute(f"CREATE UNIQUE INDEX {_ARCHIVE}.ux_{table}_row ON {table}(id, {tcol})")
    else:
        for n, t in cols:
            if n not in have:
                conn.execute(f"ALTER TABLE {_ARCHIVE}.{table} ADD COLUMN {n} {t}")
    return [n for n, _ in cols]


def _archive_batched(
    conn, where_sql: str, params: tuple, table: str, *, batch: int = _ARCHIVE_BATCH
) -> int:
    """Move `table` rows matching `where_sql` into the research archive, one
    chunk at a time. Returns rows deleted from the live DB.

    The order is the safety: a chunk is committed to the archive FIRST, and the
    delete only removes rows the archive provably holds. A crash in between
    leaves the chunk in both files (the re-run's INSERT OR IGNORE skips it),
    never in neither."""
    tcol = _RESEARCH_TABLES[table][0]
    cols = ", ".join(_archive_columns(conn, table))
    total = 0
    while True:
        hi = conn.execute(
            f"SELECT MAX(rowid) FROM (SELECT rowid FROM main.{table} "
            f"WHERE {where_sql} ORDER BY rowid LIMIT ?)",
            (*params, batch),
        ).fetchone()[0]
        if hi is None:
            return total
        conn.execute(
            f"INSERT OR IGNORE INTO {_ARCHIVE}.{table} ({cols}) "
            f"SELECT {cols} FROM main.{table} WHERE rowid <= ? AND {where_sql}",
            (hi, *params),
        )
        n = conn.execute(
            f"DELETE FROM main.{table} WHERE rowid <= ? AND {where_sql} "
            f"AND EXISTS (SELECT 1 FROM {_ARCHIVE}.{table} a "
            f"WHERE a.id = main.{table}.id AND a.{tcol} = main.{table}.{tcol})",
            (hi, *params),
        ).rowcount or 0
        total += n
        if n == 0:
            return total


def research_source(conn, table: str, since_days: float | None = None) -> str:
    """What a reader should SELECT FROM to see `table` together with the rows
    retention moved into the research archive: a UNION ALL subquery, or the
    bare table name when no archived row can matter (no archive beside this
    DB, or a `since_days` window inside the table's retention).

    Works on any connection to a live DB file, backtest.py's read-only CLI
    one included: it ATTACHes the archive beside that file once. ATTACH can't
    run inside a transaction, so a connection in one just reads the live
    table."""
    tcol, keep_days = _RESEARCH_TABLES[table]
    if since_days is not None and since_days <= keep_days:
        return table
    files = {r[1]: r[2] for r in conn.execute("PRAGMA database_list")}
    if _ARCHIVE not in files:
        path = Path(files["main"]).with_name(RESEARCH_ARCHIVE_NAME) if files.get("main") else None
        if path is None or not path.exists() or conn.in_transaction:
            return table
        try:
            try:
                conn.execute(f"ATTACH DATABASE ? AS {_ARCHIVE}", (path.as_uri() + "?mode=ro",))
            except (sqlite3.Error, ValueError):
                conn.execute(f"ATTACH DATABASE ? AS {_ARCHIVE}", (str(path),))
        except sqlite3.Error:
            return table
    arch = {r[1] for r in conn.execute(f"PRAGMA {_ARCHIVE}.table_info({table})")}
    cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
    if not arch or not cols:
        return table
    old = ", ".join(f"a.{c}" if c in arch else f"NULL AS {c}" for c in cols)
    return (
        f"(SELECT {', '.join(cols)} FROM main.{table} UNION ALL "
        f"SELECT {old} FROM {_ARCHIVE}.{table} a WHERE NOT EXISTS "
        f"(SELECT 1 FROM main.{table} m WHERE m.id = a.id AND m.{tcol} = a.{tcol}))"
    )


def cleanup_old_data(
    conn=None, *, trade_hours: int = 48, alert_days: int = _SIGNAL_KEEP_DAYS,
    snapshot_hours: int = 6, pnl_days: int = 45,
    event_days: int = 30, c15_signal_days: int = _C15_SIGNAL_KEEP_DAYS,
) -> int:
    """Prune stale rows from the high-volume tables. Safe to call often;
    on a maintained DB each pass removes only the last interval's overflow.
    `conn` is accepted for backwards-compatibility but ignored — the
    batched deletes manage their own short transactions."""
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    trade_cutoff = (now - timedelta(hours=trade_hours)).strftime("%Y-%m-%d %H:%M:%S")
    alert_cutoff = (now - timedelta(days=alert_days)).strftime("%Y-%m-%d %H:%M:%S")
    snap_cutoff = (now - timedelta(hours=snapshot_hours)).strftime("%Y-%m-%d %H:%M:%S")
    pnl_cutoff = (now - timedelta(days=pnl_days)).strftime("%Y-%m-%d %H:%M:%S")
    settled_cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    event_cutoff = (now - timedelta(days=event_days)).strftime("%Y-%m-%d %H:%M:%S")
    c15sig_cutoff = (now - timedelta(days=c15_signal_days)).strftime("%Y-%m-%d %H:%M:%S")

    ticks_cutoff = (now - timedelta(days=_C15_TICKS_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    deleted = 0
    try:
        with _research_archive() as ac:
            deleted += _archive_batched(ac, "observed_at < ?", (ticks_cutoff,), "crypto15m_ticks")
            deleted += _archive_batched(
                ac, "resolved = 1 AND observed_at < ?", (c15sig_cutoff,), "crypto15m_signals")
            for t in ("alerts", "whale_trades"):
                deleted += _archive_batched(
                    ac, "resolved IN (0, 1) AND created_at < ?", (alert_cutoff,), t)
    except sqlite3.Error as e:
        logger.warning(f"research archive unavailable ({e}); research rows kept this pass")
    deleted += _delete_batched("created_time < ?", (trade_cutoff,), "trades")
    deleted += _delete_batched("snapshot_at < ?", (snap_cutoff,), "market_snapshots")
    perps_cutoff = int((now - timedelta(days=45)).timestamp() * 1000)
    deleted += _delete_batched("ts < ?", (perps_cutoff,), "perps_trades")
    deleted += _delete_batched("ts < ?", (perps_cutoff,), "perps_ticks")
    deleted += _delete_batched("end_ms < ?", (perps_cutoff,), "perps_tape_coverage")
    deleted += _delete_batched("ingested_at < ?", (ticks_cutoff,), "clob_trades")
    deleted += _delete_batched("created_at < ?", (event_cutoff,), "order_events")
    deleted += _delete_batched(
        "resolved = 1 AND created_at < ?", (c15sig_cutoff,), "script_shadow_orders")
    deleted += _delete_batched(
        "status NOT IN ('active','open') AND last_updated < ?", (settled_cutoff,), "markets")
    deleted += _delete_batched(
        "close_time != '' AND close_time < ? AND last_updated < ?",
        (now_iso, settled_cutoff), "markets")
    deleted += _delete_batched("volume < 10 AND volume_24h < 5", (), "markets")
    with get_db() as c:
        for (env_,) in c.execute("SELECT DISTINCT network FROM pnl_snapshots").fetchall():
            pnl_baseline(c, env_)
        deleted += c.execute(
            "DELETE FROM pnl_snapshots WHERE at < ?", (pnl_cutoff,)
        ).rowcount or 0
        deleted += c.execute(
            "DELETE FROM events WHERE last_updated < ?", (event_cutoff,)
        ).rowcount or 0
    return deleted


def _reclaimable_mb() -> float:
    """Megabytes of free (deleted-but-not-released) pages in the DB file."""
    with get_db() as conn:
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return fl * ps / 1e6


def is_trading_idle(conn=None) -> tuple[bool, str]:
    """Is there any live money state that a long exclusive lock could hurt?

    Returns (idle, reason). `idle` is True only when NOTHING is open or in
    flight anywhere: no live main-engine position, no live crypto15m/script
    position, no unreconciled resting terminal order.

    This exists because VACUUM takes an EXCLUSIVE lock for its whole run. Every
    write the trading loop attempts during it blocks on `busy_timeout=10000`
    and then RAISES — so on a large file the loop does not "keep going", it
    fails every write for the duration. Compaction is a nice-to-have; managing
    an open position is not, so the vacuum waits for a genuinely idle moment.
    """
    own = conn is None
    ctx = get_db() if own else None
    c = ctx.__enter__() if own else conn
    try:
        row = c.execute(
            "SELECT 1 FROM bot_positions "
            "WHERE resolved = 0 AND status IN ('submitted','partial','filled') "
            "LIMIT 1"
        ).fetchone()
        if row:
            return False, "an open main-engine position"
        row = c.execute(
            "SELECT 1 FROM crypto15m_positions "
            "WHERE resolved = 0 AND dry_run = 0 "
            "  AND status IN ('submitted','partial','filled','exiting') "
            "LIMIT 1"
        ).fetchone()
        if row:
            return False, "an open crypto15m/script position"
        row = c.execute(
            "SELECT 1 FROM terminal_orders "
            "WHERE status = 'placed' AND reconciled = 0 LIMIT 1"
        ).fetchone()
        if row:
            return False, "an unreconciled resting terminal order"
        return True, ""
    finally:
        if own:
            ctx.__exit__(None, None, None)


def vacuum() -> None:
    """Checkpoint the WAL and rewrite the file to reclaim free pages.
    Uses a dedicated autocommit connection (VACUUM can't run in a txn).

    Callers must gate this on `is_trading_idle()` — VACUUM holds an exclusive
    lock for its whole run and concurrent writes RAISE after busy_timeout."""
    close_thread_connection()
    conn = sqlite3.connect(str(db_path()), timeout=120)
    conn.isolation_level = None
    try:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        conn.execute("VACUUM")
    finally:
        conn.close()


def backup_research(keep: int = 7) -> str | None:
    """Nightly copy of the whole DB into data/backups (rotated). The research
    tables (ticks/signals/positions) are the irreplaceable evidence every
    strategy verdict came from — before this there was NO backup anywhere and
    factory_reset wiped them permanently. sqlite3's online backup API is safe
    against concurrent writers. Rows past retention are not in these copies:
    they live on in the research archive (see research_source)."""
    try:
        src = str(db_path())
        bdir = os.path.join(os.path.dirname(src), "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        dest = os.path.join(bdir, f"research-{stamp}.db")
        if os.path.exists(dest):
            _mirror_to_vault(dest)
            return dest
        with get_db() as conn:
            out = sqlite3.connect(dest)
            try:
                conn.backup(out)
            finally:
                out.close()
        snaps = sorted(
            f for f in os.listdir(bdir)
            if f.startswith("research-") and f.endswith(".db")
        )
        for old_f in snaps[:-keep]:
            try:
                os.remove(os.path.join(bdir, old_f))
            except OSError:
                pass
        _mirror_to_vault(dest)
        return dest
    except Exception:
        return None


def _mirror_to_vault(snapshot: str) -> None:
    """Keep ONE rolling copy of the newest nightly snapshot in the vault
    (outside userData) so the research data survives a full userData wipe.
    Skipped when the vault copy is already from the same or a newer day."""
    try:
        v = _vault_dir()
        if v is None:
            return
        dest = v / "research-latest.db"
        if dest.exists() and dest.stat().st_mtime >= os.path.getmtime(snapshot):
            return
        tmp = v / "research-latest.db.tmp"
        shutil.copy2(snapshot, tmp)
        os.replace(tmp, dest)
    except OSError:
        pass


def run_maintenance(*, vacuum_min_free_mb: float = 200.0, force_vacuum: bool = False) -> dict:
    """One maintenance pass: prune stale rows, then compact the file when it is
    both worth it (>= `vacuum_min_free_mb` reclaimable) and SAFE.

    Pruning is batched deletes and stays on the hourly timer regardless. The
    VACUUM does not: it takes an exclusive lock for its whole run, and every
    write the trading loop attempts meanwhile blocks on busy_timeout=10000 and
    then RAISES — on a multi-GB file that is minutes of a loop that cannot
    record a fill, a stop or a settlement. So it runs only at a genuinely idle
    moment (nothing open in any engine, no resting terminal order) and is
    otherwise skipped with a reason; there is always another hour.

    `force_vacuum` is the explicit-operator path (a user-triggered compaction)
    and still honours the idle gate — the lock is no less exclusive because a
    human asked for it. Returns a summary for logging.
    """
    deleted = cleanup_old_data()
    backed_up = backup_research()
    free_mb = _reclaimable_mb()
    vacuumed = False
    skipped = ""
    if force_vacuum or free_mb >= vacuum_min_free_mb:
        idle, why = is_trading_idle()
        if idle:
            vacuum()
            vacuumed = True
        else:
            skipped = why
            logger.info(
                f"db maintenance: skipping VACUUM ({round(free_mb, 1)}MB "
                f"reclaimable) — {why} is live; it would block every trading "
                "write until it finished. Retrying next pass."
            )
    return {"deleted": deleted, "reclaimable_mb": round(free_mb, 1), "vacuumed": vacuumed,
            "vacuumSkipped": skipped, "backup": backed_up}



def insert_perps_fill(conn, row: dict) -> bool:
    """Insert one Perps fill. Idempotent on trade_id; True when it was new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO perps_fills
           (trade_id, order_id, instrument_id, symbol, side, price, quantity,
            notional, taker, fee, pnl, liquidation, mid_at_fill, ts, network)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(row["trade_id"]), row.get("order_id"), int(row["instrument_id"]),
            row.get("symbol") or "", row["side"], float(row["price"]),
            float(row["quantity"]), float(row["notional"]), 1 if row.get("taker") else 0,
            float(row.get("fee") or 0), float(row.get("pnl") or 0),
            1 if row.get("liquidation") else 0, row.get("mid_at_fill"),
            int(row["ts"]), row.get("network") or "mainnet",
        ),
    )
    return cur.rowcount > 0


def set_perps_markout(conn, trade_id: int, markout_bps: float) -> None:
    conn.execute("UPDATE perps_fills SET markout_bps=? WHERE trade_id=?",
                 (float(markout_bps), int(trade_id)))


def perps_last_fill_ts(conn) -> int:
    row = conn.execute("SELECT MAX(ts) FROM perps_fills").fetchone()
    return int(row[0] or 0)


def perps_fill_totals(conn, since_ms: int = 0) -> dict:
    """Volume / fees / realized P&L over fills at or after `since_ms`."""
    row = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(notional),0),
                  COALESCE(SUM(CASE WHEN taker=0 THEN notional ELSE 0 END),0),
                  COALESCE(SUM(fee),0), COALESCE(SUM(pnl),0)
           FROM perps_fills WHERE ts >= ?""",
        (int(since_ms),),
    ).fetchone()
    return {
        "fills": int(row[0] or 0), "volumeUsd": float(row[1] or 0),
        "makerVolumeUsd": float(row[2] or 0), "feesUsd": float(row[3] or 0),
        "realizedPnlUsd": float(row[4] or 0),
    }


def perps_market_totals(conn, since_ms: int) -> dict[int, dict]:
    rows = conn.execute(
        """SELECT instrument_id, COUNT(*), COALESCE(SUM(notional),0),
                  COALESCE(SUM(fee),0), COALESCE(SUM(pnl),0),
                  AVG(CASE WHEN taker=0 THEN markout_bps END),
                  COUNT(CASE WHEN taker=0 AND markout_bps IS NOT NULL THEN 1 END)
           FROM perps_fills WHERE ts >= ? GROUP BY instrument_id""",
        (int(since_ms),),
    ).fetchall()
    return {
        int(r[0]): {"fills": int(r[1]), "volumeUsd": float(r[2]), "feesUsd": float(r[3]),
                    "realizedPnlUsd": float(r[4]),
                    "avgMarkoutBps": None if r[5] is None else float(r[5]),
                    "markoutN": int(r[6])}
        for r in rows
    }


def recent_perps_fills(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT trade_id, instrument_id, symbol, side, price, quantity, notional,
                  taker, fee, pnl, markout_bps, ts
           FROM perps_fills ORDER BY ts DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    keys = ("tradeId", "instrumentId", "symbol", "side", "price", "quantity",
            "notional", "taker", "fee", "pnl", "markoutBps", "ts")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["taker"] = bool(d["taker"])
        out.append(d)
    return out


def insert_perps_trades(conn, rows: list[dict]) -> int:
    """Insert public tape rows ({trade_id, iid|instrument_id, side, price, qty,
    ts}); idempotent on trade_id. Returns rows that were new."""
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO perps_trades (trade_id, instrument_id, side, price, qty, ts)
           VALUES (?,?,?,?,?,?)""",
        [(int(r["trade_id"]), int(r.get("iid", r.get("instrument_id"))), str(r["side"]),
          float(r["price"]), float(r["qty"]), int(r["ts"])) for r in rows],
    )
    return conn.total_changes - before


def insert_perps_ticks(conn, rows: list[dict]) -> int:
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO perps_ticks (instrument_id, ts, bid, ask) VALUES (?,?,?,?)",
        [(int(r["iid"]), int(r["ts"]), float(r["bid"]), float(r["ask"])) for r in rows],
    )
    return conn.total_changes - before


def perps_trades_range(conn, iid: int, start_ms: int, end_ms: int) -> list[tuple]:
    """(ts, side, price, qty) for one market, oldest first."""
    return [tuple(r) for r in conn.execute(
        """SELECT ts, side, price, qty FROM perps_trades
           WHERE instrument_id=? AND ts>=? AND ts<=? ORDER BY ts, trade_id""",
        (int(iid), int(start_ms), int(end_ms)),
    ).fetchall()]


def perps_ticks_range(conn, iid: int, start_ms: int, end_ms: int) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        """SELECT ts, bid, ask FROM perps_ticks
           WHERE instrument_id=? AND ts>=? AND ts<=? ORDER BY ts""",
        (int(iid), int(start_ms), int(end_ms)),
    ).fetchall()]


def perps_trade_span(conn, iid: int) -> tuple[int, int]:
    """(oldest ts, newest ts) recorded for a market, or (0, 0)."""
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM perps_trades WHERE instrument_id=?", (int(iid),),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)



def perps_coverage(conn, iid: int) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT start_ms, end_ms FROM perps_tape_coverage WHERE instrument_id=? ORDER BY start_ms",
        (int(iid),),
    ).fetchall()
    return [(int(a), int(b)) for a, b in rows]


def add_perps_coverage(conn, iid: int, start_ms: int, end_ms: int) -> None:
    """Record [start, end] as complete, merging with overlapping/adjacent
    windows so the table stays one row per contiguous span."""
    spans = perps_coverage(conn, iid) + [(int(start_ms), int(end_ms))]
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    conn.execute("DELETE FROM perps_tape_coverage WHERE instrument_id=?", (int(iid),))
    conn.executemany(
        "INSERT INTO perps_tape_coverage (instrument_id, start_ms, end_ms) VALUES (?,?,?)",
        [(int(iid), a, b) for a, b in merged],
    )
