"""Passive research logger for the recurring crypto up/down strategy.

Follows `crypto15m_interval` — whichever window series the Crypto tab
watches (5m / 15m / hourly) is the one recorded, and every row is stamped
with that interval so per-interval backtests never mix datasets.

Builds the dataset that makes the strategy backtestable: once per
window per asset it captures a *decision-point* snapshot (which side is
the favorite, its price, and the underlying delta a few minutes before
close), then fills in the settled outcome afterward. It NEVER places an
order or touches money — it only reads public market data, exactly like
the monitor. Runs in the background regardless of whether the executor is
enabled, so anyone accumulates data to test their own strategies against.

The captured features are raw (favorite price, delta %), so the backtest
can sweep ANY entry threshold / delta filter / favorite-vs-contrarian
choice after the fact without re-collecting data.

It also appends a `crypto15m_ticks` row for every active market on every
pass (~25s cadence, the whole window — bid/ask, mid, spot, delta), the
raw series needed to study entry timing, quote staleness vs spot, and
maker-fill behaviour. After 60 days db maintenance moves ticks (and resolved
signals) out of the live DB into data/research-archive.db, which is never
pruned; backtest.py and any replay window longer than 60 days read them there.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import crypto15m
import db
import polymarket_api
import polymarket_auth

logger = logging.getLogger(__name__)

_LOOKBACK_FRACTION = 8.0 / 15.0


def _capture_lookback_min(interval: str) -> float:
    sec = crypto15m._INTERVAL_SEC.get(interval, 900)
    return round((sec / 60.0) * _LOOKBACK_FRACTION, 2)
_RESOLVE_PER_TICK = 8
_ATTEMPTS_PER_TICK = 16
_GIVE_UP_AFTER_SEC = 3 * 24 * 3600.0


def _settled_up_won(m: dict) -> Optional[bool]:
    """Did the up/yes side win? None if the market isn't settled yet.

    Mirrors trader._market_yes_payout but local (no import cycle): reads
    `result`, else the settlement value (dollar or legacy cents)."""
    if not isinstance(m, dict):
        return None
    result = (m.get("result") or "").lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    status = (m.get("status") or "").lower()
    if status not in ("settled", "finalized", "closed", "determined"):
        return None
    sv = m.get("settlement_value_dollars")
    if sv is not None:
        try:
            return float(sv) >= 0.5
        except (TypeError, ValueError):
            return None
    sv = m.get("settlement_value")
    if sv is not None:
        try:
            return float(sv) >= 50.0
        except (TypeError, ValueError):
            return None
    return None


_STRIKE_CHECK_BPS = 2.0


def _check_strike(sig: dict, m: dict) -> Optional[float]:
    """Gap in bps between a resolved window's captured strike and the official
    one, logged when beyond `_STRIKE_CHECK_BPS`. None when either is missing."""
    try:
        ours, official = float(sig.get("strike")), float(m.get("price_to_beat"))
    except (TypeError, ValueError):
        return None
    if ours <= 0 or official <= 0:
        return None
    gap = (ours / official - 1.0) * 1e4
    if abs(gap) > _STRIKE_CHECK_BPS:
        logger.warning(
            f"crypto15m strike check {sig.get('asset')} {str(sig.get('ticker'))[:16]}…: "
            f"captured {ours:.8g} ({sig.get('strike_source') or 'unknown source'}) vs "
            f"official {official:.8g} ({gap:+.1f} bps)"
        )
    return gap


def _capture_ticks(snap: dict, env: str, interval: str) -> int:
    """Append a whole-window observation row for every active market —
    every recorder pass, not just the decision point. This is the raw
    series that lets entry TIMING, spot-vs-quote staleness, and maker
    fill behaviour be studied later (the per-market outcome joins in
    from crypto15m_signals on ticker)."""
    captured = 0
    max_mins = {"5m": 5, "15m": 15, "hourly": 60, "4h": 240}.get(interval, 15) + 2
    with db.get_db() as conn:
        for a in snap.get("assets", []):
            if not a.get("hasMarket") or not a.get("ticker"):
                continue
            ml = a.get("minsLeft")
            if ml is not None and float(ml) > max_mins:
                logger.warning(
                    f"[crypto15m] tick recorder: {a.get('asset')} latched a "
                    f"future window (minsLeft={float(ml):.0f} > {max_mins}) — skipped"
                )
                continue
            db.insert_crypto15m_tick(conn, {
                "ticker": a["ticker"],
                "asset": a["asset"],
                "mins_left": a.get("minsLeft"),
                "yes_bid": a.get("yesBid"),
                "yes_ask": a.get("yesAsk"),
                "up_prob": a.get("upProb"),
                "spot": a.get("spotUsd"),
                "open_spot": a.get("open15mUsd"),
                "delta_pct": a.get("deltaPct"),
                "book_imbalance": a.get("bookImbalance"),
                "macd": a.get("macd"),
                "macd_signal": a.get("macdSignal"),
                "macd_hist": a.get("macdHist"),
                "macd_cross": a.get("macdCross"),
                "rsi": a.get("rsi"),
                "ws_bid": a.get("wsBid"),
                "ws_ask": a.get("wsAsk"),
                "strike": a.get("strikeUsd"),
                "strike_source": a.get("strikeSource"),
                "settle_twap": a.get("settleTwapUsd"),
                "delta_signed_pct": a.get("deltaSignedPct"),
                "sigma1m": a.get("sigma1m"),
                "model_prob": a.get("modelProb"),
                "edge_net_cents": a.get("edgeNetCents"),
                "no_ask": a.get("downAsk"),
                "up_ask": a.get("upAsk"),
                "spot_source": (snap.get("spotSource") if a.get("spotLive") else "rest"),
                "vwap1h": a.get("vwap1h"),
                "ema12": a.get("ema12"),
                "sma20": a.get("sma20"),
                "sma50": a.get("sma50"),
                "price_vs_vwap_pct": a.get("priceVsVwapPct"),
                "ema12_vs_sma20_pct": a.get("ema12VsSma20Pct"),
                "ema1_vs_sma5_pct": a.get("ema1VsSma5Pct"),
                "velocity1m_pct": a.get("velocity1mPct"),
                "change5m_pct": a.get("change5mPct"),
                "change15m_pct": a.get("change15mPct"),
                "network": env,
                "interval": interval,
            })
            captured += 1
    return captured


def _capture(snap: dict, env: str, interval: str) -> int:
    captured = 0
    lookback = _capture_lookback_min(interval)
    for a in snap.get("assets", []):
        if not a.get("hasMarket") or not a.get("ticker"):
            continue
        ml = a.get("minsLeft")
        fav = a.get("favorite")
        if ml is None or ml > lookback or fav not in ("up", "down"):
            continue
        row = {
            "ticker": a["ticker"],
            "asset": a["asset"],
            "series": a.get("series", ""),
            "close_time": a.get("closeTime", ""),
            "mins_left": ml,
            "favorite": fav,
            "favorite_price": a.get("favoritePrice"),
            "entry_cost": a.get("entryCost"),
            "up_prob": a.get("upProb"),
            "delta_pct": a.get("deltaPct"),
            "open_spot": a.get("open15mUsd"),
            "obs_spot": a.get("spotUsd"),
            "book_imbalance": a.get("bookImbalance"),
            "macd": a.get("macd"),
            "macd_signal": a.get("macdSignal"),
            "macd_hist": a.get("macdHist"),
            "macd_cross": a.get("macdCross"),
            "rsi": a.get("rsi"),
            "strike": a.get("strikeUsd"),
            "strike_source": a.get("strikeSource"),
            "model_prob": a.get("modelProb"),
            "edge_net_cents": a.get("edgeNetCents"),
            "network": env,
            "interval": interval,
        }
        with db.get_db() as conn:
            if db.insert_crypto15m_signal(conn, row):
                captured += 1
    return captured


async def _resolve(now_epoch: float) -> int:
    with db.get_db() as conn:
        pending = db.unresolved_crypto15m_signals(conn, limit=200)
    resolved = 0
    attempts = 0
    for s in pending:
        if resolved >= _RESOLVE_PER_TICK or attempts >= _ATTEMPTS_PER_TICK:
            break
        ce = crypto15m._parse_close_epoch(s.get("close_time") or "")
        if ce is not None and ce > now_epoch:
            continue
        attempts += 1
        try:
            m = await polymarket_api.fetch_market(s["ticker"])
        except Exception:
            m = None
        up_won = _settled_up_won(m or {})
        if up_won is None:
            if ce is not None and (now_epoch - ce) > _GIVE_UP_AFTER_SEC:
                with db.get_db() as conn:
                    conn.execute(
                        """UPDATE crypto15m_signals
                              SET resolved=1, settled_at=datetime('now')
                            WHERE ticker=?""",
                        (s["ticker"],),
                    )
                logger.info(
                    f"crypto15m signal {s['ticker'][:16]}… gave up resolving "
                    "(no settled outcome 3 days after close) — parked without outcome"
                )
            continue
        _check_strike(s, m or {})
        with db.get_db() as conn:
            db.resolve_crypto15m_signal(conn, s["ticker"], 1 if up_won else 0)
        resolved += 1
    return resolved


async def record_tick(cfg: dict) -> dict:
    """One pass: log a tick for every active market, capture new decision
    points, resolve settled ones. Resilient — never raises into the loop."""
    env = polymarket_auth.get_env()
    interval = crypto15m._interval(cfg)
    now_epoch = datetime.now(timezone.utc).timestamp()
    captured = 0
    resolved = 0
    ticks = 0
    try:
        snap = await crypto15m.snapshot(cfg)
    except Exception as e:
        logger.debug(f"crypto15m snapshot failed: {e}")
        snap = None
    if snap:
        try:
            ticks = _capture_ticks(snap, env, interval)
        except Exception as e:
            logger.debug(f"crypto15m tick capture failed: {e}")
        try:
            captured = _capture(snap, env, interval)
        except Exception as e:
            logger.debug(f"crypto15m capture failed: {e}")
    try:
        resolved = await _resolve(now_epoch)
    except Exception as e:
        logger.debug(f"crypto15m resolve failed: {e}")
    prints = 0
    try:
        import clob_ws
        rows = clob_ws.feed.drain_trades()
        if rows:
            with db.get_db() as conn:
                prints = db.insert_clob_trades(conn, rows, network=env)
    except Exception as e:
        logger.debug(f"clob trade-print persist failed: {e}")
    if captured or resolved:
        logger.debug(f"crypto15m signals: +{captured} captured, {resolved} resolved")
    return {"captured": captured, "resolved": resolved, "ticks": ticks,
            "prints": prints}
