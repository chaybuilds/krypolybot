"""15-minute crypto market monitor (READ-ONLY).

Powers the "15m Crypto" tab. Tracks Polymarket's seven 15-minute crypto
series and surfaces a momentum entry *signal* for each: in the last
`time_delay` minutes of a quarter, when one side is a strong favorite
(>= entry threshold) and the underlying has moved at least
`min_delta_pct` from the quarter's opening spot price.

This module NEVER places, modifies, or cancels an order — it only reads
public Polymarket market data and a public crypto spot feed. The spot price
(needed to compute delta) comes from CoinGecko because Polymarket's API only
exposes contract probabilities, not the live underlying price.

Every threshold is user-configurable from the 15m Crypto tab and
overridable via `crypto15m_*` config keys. The shipped defaults
(8-minute window, 85c favorite, 40c stop, 0.02 markup) are just one
common favorite-follow baseline — treat them as a starting point, not a
proven edge.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import httpx

import clob_ws
import indicators
import polymarket_api
import binance_ref
import rtds_ws
import spot_ws

logger = logging.getLogger(__name__)


SERIES: list[dict[str, str]] = [
    {"asset": "BTC",  "series": "BTC-UPDOWN",  "cg": "bitcoin"},
    {"asset": "ETH",  "series": "ETH-UPDOWN",  "cg": "ethereum"},
    {"asset": "SOL",  "series": "SOL-UPDOWN",  "cg": "solana"},
    {"asset": "XRP",  "series": "XRP-UPDOWN",  "cg": "ripple"},
    {"asset": "DOGE", "series": "DOGE-UPDOWN", "cg": "dogecoin"},
    {"asset": "HYPE", "series": "HYPE-UPDOWN", "cg": "hyperliquid"},
    {"asset": "BNB",  "series": "BNB-UPDOWN",  "cg": "binancecoin"},
    {"asset": "ZEC",  "series": "ZEC-UPDOWN",  "cg": "zcash"},
]

ALL_ASSETS = [s["asset"] for s in SERIES]


def asset_enabled(cfg: dict, asset: str) -> bool:
    """True if the executor may open NEW positions on `asset`. None/missing
    `crypto15m_assets` = all enabled; a list restricts to those symbols (an
    empty list disables every asset)."""
    raw = (cfg or {}).get("crypto15m_assets")
    if isinstance(raw, list):
        return asset.upper() in {str(a).upper() for a in raw}
    return True

_DEFAULTS: dict[str, float] = {
    "time_delay_min": 8.0,
    "entry_threshold": 0.95,
    "exit_threshold": 0.40,
    "take_profit": 0.0,
    "stop_loss_pct": 0.0,
    "take_profit_pct": 0.0,
    "entry_max": 0.98,
    "min_delta_pct": 0.0,
    "entry_diff": 0.02,
}

_CRYPTOCOMPARE_URL = "https://min-api.cryptocompare.com/data/pricemulti"
_COINBASE_URL = "https://api.coinbase.com/v2/prices/{sym}-USD/spot"
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
_SPOT_CACHE_TTL = 5.0
_INTERVAL_SEC = {"5m": 300, "15m": 900, "hourly": 3600, "4h": 14400}


def _interval(cfg: dict) -> str:
    iv = str(cfg.get("crypto15m_interval", "15m"))
    return iv if iv in _INTERVAL_SEC else "15m"

_spot_client: Optional[httpx.AsyncClient] = None

_spot_cache: dict = {"at": 0.0, "spots": {}, "source": "none"}

_window_open: dict[tuple[str, int], tuple[float, str]] = {}


def _get_spot_client() -> httpx.AsyncClient:
    global _spot_client
    if _spot_client is None or _spot_client.is_closed:
        _spot_client = httpx.AsyncClient(
            timeout=8.0,
            headers={"Accept": "application/json", "User-Agent": "KryptPolyBot/1.0"},
        )
    return _spot_client


async def close_clients() -> None:
    global _spot_client
    if _spot_client and not _spot_client.is_closed:
        try:
            await _spot_client.aclose()
        except Exception:
            pass
    _spot_client = None


polymarket_api.register_recycle_hook(close_clients)


def _const(cfg: dict, key: str) -> float:
    try:
        return float(cfg.get(f"crypto15m_{key}", _DEFAULTS[key]))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _price_dollars(m: dict, key: str) -> float:
    """Polymarket ships both integer-cents (`yes_bid`) and dollar
    (`yes_bid_dollars`) fields for prices; prefer the dollar form and
    fall back to cents/100."""
    d = m.get(f"{key}_dollars")
    if d is not None:
        return _to_float(d)
    return _to_float(m.get(key)) / 100.0


def _parse_close_epoch(close_time: str) -> Optional[float]:
    if not close_time:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ct = datetime.strptime(close_time, fmt)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            return ct.timestamp()
        except ValueError:
            continue
    return None


async def _spots_cryptocompare(client: httpx.AsyncClient) -> dict[str, float]:
    """One call covers every tracked asset (incl. HYPE/BNB)."""
    syms = ",".join(s["asset"] for s in SERIES)
    resp = await client.get(_CRYPTOCOMPARE_URL, params={"fsyms": syms, "tsyms": "USD"})
    resp.raise_for_status()
    data = resp.json() or {}
    if isinstance(data, dict) and data.get("Response") == "Error":
        raise RuntimeError(str(data.get("Message") or "cryptocompare error"))
    out: dict[str, float] = {}
    for s in SERIES:
        px = (data.get(s["asset"]) or {}).get("USD")
        if px is not None:
            out[s["asset"]] = float(px)
    return out


async def _spots_coinbase(client: httpx.AsyncClient) -> dict[str, float]:
    """Per-asset spot fallback. Coinbase now lists every tracked coin."""
    async def one(s: dict) -> tuple[str, Optional[float]]:
        try:
            r = await client.get(_COINBASE_URL.format(sym=s["asset"]))
            r.raise_for_status()
            amt = ((r.json() or {}).get("data") or {}).get("amount")
            return s["asset"], (float(amt) if amt is not None else None)
        except Exception:
            return s["asset"], None

    results = await asyncio.gather(*[one(s) for s in SERIES])
    return {a: px for a, px in results if px is not None}


async def _spots_coingecko(client: httpx.AsyncClient) -> dict[str, float]:
    """Original source, now last-resort (free tier rate-limits hard)."""
    ids = ",".join(s["cg"] for s in SERIES)
    resp = await client.get(_COINGECKO_URL, params={"ids": ids, "vs_currencies": "usd"})
    resp.raise_for_status()
    data = resp.json()
    out: dict[str, float] = {}
    for s in SERIES:
        px = (data.get(s["cg"]) or {}).get("usd")
        if px is not None:
            out[s["asset"]] = float(px)
    return out


_SPOT_SOURCES = [
    ("cryptocompare", _spots_cryptocompare),
    ("coinbase", _spots_coinbase),
    ("coingecko", _spots_coingecko),
]


async def fetch_spots() -> tuple[dict[str, float], str]:
    """Every tracked asset's USD spot, with the source that supplied it.

    Returns ``({asset: usd}, source)``. Tries each feed in turn and caches
    the first non-empty result for `_SPOT_CACHE_TTL` seconds so the three
    callers (monitor, executor, recorder) share one upstream request. If
    every feed fails it serves the last good cache (marked "(stale)") so
    Spot/Open/Δ don't all blank out during a transient outage."""
    spots, source, _stale = await _fetch_spots()
    return spots, source


async def _fetch_spots() -> tuple[dict[str, float], str, set[str]]:
    """`fetch_spots`, plus the assets whose price is that stale last-good
    cache. The source tag alone can't say which: live push feeds still
    overlay the assets they cover during a REST outage."""
    loop = asyncio.get_event_loop()
    now = loop.time()

    def _overlay(spots: dict, source: str,
                 stale: bool = False) -> tuple[dict[str, float], str, set[str]]:
        cb = spot_ws.fresh_spots()
        ch = rtds_ws.fresh_spots()
        rest_only = {a for a in spots if a not in cb and a not in ch}
        stale_assets = rest_only if stale else set()
        if not cb and not ch:
            return spots, source, stale_assets
        merged = {**spots, **cb, **ch}
        parts = []
        if ch:
            parts.append("rtds-ws")
        if any(a in cb and a not in ch for a in merged):
            parts.append("coinbase-ws")
        if rest_only:
            parts.append(source)
        return merged, "+".join(parts), stale_assets

    if _spot_cache["spots"] and (now - _spot_cache["at"]) < _SPOT_CACHE_TTL:
        return _overlay(dict(_spot_cache["spots"]), _spot_cache["source"])

    client = _get_spot_client()
    for name, fn in _SPOT_SOURCES:
        try:
            spots = await fn(client)
        except Exception as e:
            logger.debug(f"crypto15m spot source {name} failed: {e}")
            continue
        if spots:
            _spot_cache.update(at=now, spots=dict(spots), source=name)
            return _overlay(dict(spots), name)

    if _spot_cache["spots"]:
        return _overlay(dict(_spot_cache["spots"]), f"{_spot_cache['source']} (stale)",
                        stale=True)
    return _overlay({}, "unavailable")


_SPOT_HIST_KEEP_S = 360.0
_spot_hist: dict[str, "deque[tuple[float, float]]"] = {}


def _note_spot(asset: str, spot: Optional[float]) -> None:
    if spot is None:
        return
    dq = _spot_hist.setdefault(asset, deque())
    now = time.time()
    dq.append((now, float(spot)))
    while dq and now - dq[0][0] > _SPOT_HIST_KEEP_S:
        dq.popleft()


def live_velocity_pct(
    asset: str, spot: Optional[float],
    horizon_s: float = 60.0, tol_s: float = 35.0,
) -> Optional[float]:
    """(spot now / spot ~60s ago − 1)·100 from the rolling live-spot history.
    Reference = the newest sample at least `horizon_s` old, accepted up to
    horizon_s+tol_s stale. None until history covers the horizon (callers
    then fall back to the candle-derived velocity)."""
    if spot is None:
        return None
    dq = _spot_hist.get(asset)
    if not dq:
        return None
    now = time.time()
    ref_t = ref_p = None
    for t, p in reversed(dq):
        if now - t >= horizon_s:
            ref_t, ref_p = t, p
            break
    if ref_t is None or (now - ref_t) > horizon_s + tol_s or not ref_p or ref_p <= 0:
        return None
    return (float(spot) / ref_p - 1.0) * 100.0


_HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
_INDICATOR_LOOKBACK_MIN = 90
_INDICATOR_CACHE_TTL = 60.0
_indicator_cache: dict[str, dict] = {}


async def _fetch_closes(
    asset: str, client: httpx.AsyncClient, lookback_min: int
) -> tuple[list[float], list[float]]:
    """The last `lookback_min` one-minute (closes, volumes) for `asset` from
    Hyperliquid, oldest→newest. Volumes feed the VWAP field (TWAP fallback when
    absent). Empty lists on any error (caller degrades to None fields)."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": asset,
            "interval": "1m",
            "startTime": now_ms - int(lookback_min) * 60_000,
            "endTime": now_ms,
        },
    }
    resp = await client.post(_HYPERLIQUID_URL, json=body)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise RuntimeError("unexpected candleSnapshot shape")
    closes: list[float] = []
    volumes: list[float] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = [r for r in rows
            if not (isinstance(r, dict) and r.get("T") is not None
                    and float(r["T"]) > cutoff_ms)]
    for r in rows:
        c = r.get("c") if isinstance(r, dict) else None
        if not c:
            continue
        try:
            closes.append(float(c))
        except (TypeError, ValueError):
            continue
        try:
            v = r.get("v")
            volumes.append(float(v) if v is not None else 0.0)
        except (TypeError, ValueError):
            volumes.append(0.0)
    return closes, volumes


async def asset_indicators(asset: str) -> dict:
    """MACD/RSI bundle for one asset's underlying, cached ~60s and shared by
    the monitor poll, executor tick and recorder. All-None on any failure or
    until there's enough candle history."""
    loop = asyncio.get_event_loop()
    now = loop.time()
    cached = _indicator_cache.get(asset)
    if cached and (now - cached["at"]) < _INDICATOR_CACHE_TTL:
        return cached["data"]
    try:
        closes, volumes = await _fetch_closes(asset, _get_spot_client(), _INDICATOR_LOOKBACK_MIN)
        data = indicators.compute(closes, volumes)
    except Exception as e:
        logger.debug(f"crypto15m indicators {asset} failed: {e}")
        data = indicators.compute([])
    _indicator_cache[asset] = {"at": now, "data": data}
    return data


def _blank_asset(entry: dict, spot: Optional[float], error: Optional[str] = None) -> dict:
    return {
        "asset": entry["asset"], "series": entry["series"],
        "spotUsd": spot, "open15mUsd": None, "deltaUsd": None, "deltaPct": None,
        "hasMarket": False, "ticker": None, "closeTime": None, "minsLeft": None,
        "upProb": None, "downProb": None, "favorite": None,
        "favoritePrice": None, "entryCost": None, "yesBid": None, "yesAsk": None,
        "inWindow": False, "signal": False, "openMarketCount": 0, "error": error,
        "hourUtc": None, "peersAgree": None, "marketBias": None,
        "macd": None, "macdSignal": None, "macdHist": None,
        "macdCross": None, "rsi": None,
        "vwap1h": None, "ema12": None, "sma20": None, "sma50": None,
        "priceVsVwapPct": None, "ema12VsSma20Pct": None, "ema1VsSma5Pct": None,
        "velocity1mPct": None, "change5mPct": None, "change15mPct": None,
        "wsBid": None, "wsAsk": None,
        "priceSource": "gamma",
    }


def _friendly_market_error(e) -> str:
    """User-facing label for a failed/timed-out market fetch.

    The raw exception name ('TimeoutError') reads like an app bug, but the cause
    is almost always that Polymarket's Gamma API is unreachable or slow from the
    user's network/region (it's Cloudflare-fronted and geo-restricted) — while
    the SPOT price, a DIFFERENT host, still loads. Point the user at the real fix
    instead of a cryptic type name."""
    name = type(e).__name__
    msg = str(e).lower()
    if ("403" in msg or "451" in msg or "forbidden" in msg
            or "blocked" in msg or "unavailable for legal" in msg):
        return "Polymarket markets unavailable on your network/region"
    if (name in ("TimeoutError", "TimeoutException", "ConnectTimeout",
                 "ReadTimeout", "PoolTimeout", "ConnectError", "NetworkError",
                 "ConnectionError", "ReadError")
            or "timeout" in msg or "connect" in msg or "network" in msg
            or "unreachable" in msg or "temporarily unavailable" in msg):
        return "Polymarket API unreachable — check your connection"
    return f"Market data unavailable ({name})"


def hours_ok(cfg: dict, hour: Optional[int] = None) -> bool:
    """Is the (UTC) hour inside the configured trading window?

    [start, end) in UTC hours; start == end (or the full 0..24) means
    always-on; start > end wraps overnight (e.g. 22 -> 6)."""
    try:
        start = int(cfg.get("crypto15m_hours_start_utc", 0) or 0)
        end = int(cfg.get("crypto15m_hours_end_utc", 24) or 24)
    except (TypeError, ValueError):
        return True
    start, end = start % 24, (end % 24 if end != 24 else 24)
    if start == end or (start == 0 and end == 24):
        return True
    if hour is None:
        hour = datetime.now(timezone.utc).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end



def hour_override(cfg: dict, hour: Optional[int] = None) -> Optional[dict]:
    """The effective config for `hour` under cfg["crypto15m_hour_configs"].

    No schedule set (key absent/None) -> cfg unchanged. Schedule set and the
    hour seated -> a merged copy (override wins). Schedule set but the hour
    unseated -> None (= don't trade this hour). Never mutates cfg.

    An EMPTY schedule dict is "set, nothing seated" -> None for every hour, NOT
    "no schedule". Conflating the two fell through to the base config while the
    caller still stamped the rows as parlay, which exempts them from every exit."""
    hcs = cfg.get("crypto15m_hour_configs")
    if hcs is None or not isinstance(hcs, dict):
        return cfg
    if hour is None:
        hour = datetime.now(timezone.utc).hour
    ov = hcs.get(str(int(hour)))
    if not isinstance(ov, dict):
        return None
    merged = dict(cfg)
    merged.update(ov)
    return merged


def resignal_asset(asset: dict, cfg: dict) -> dict:
    """Recompute the config-derived snapshot fields (inWindow, signal) under a
    DIFFERENT config than the one the snapshot was built with — the per-hour
    override. The snapshot bakes the base config's window/threshold into
    `signal`, so an overlay that only re-gates should_enter would still be
    reading the OLD signal. Mirrors the snapshot/replay signal expression."""
    a = dict(asset)
    ml = a.get("minsLeft")
    in_window = ml is not None and float(ml) <= _const(cfg, "time_delay_min")
    fav_price = a.get("favoritePrice")
    entry_cost = a.get("entryCost")
    dp = a.get("deltaPct")
    a["inWindow"] = bool(in_window)
    a["signal"] = bool(
        in_window
        and fav_price is not None
        and float(fav_price) >= _const(cfg, "entry_threshold")
        and (entry_cost is None or float(entry_cost) <= _const(cfg, "entry_max"))
        and (dp is None or float(dp) >= _const(cfg, "min_delta_pct"))
        and hours_ok(cfg, hour=a.get("hourUtc"))
    )
    return a


SETTLE_TWAP_SEC = 60.0
STRIKE_SOURCES_OK = ("twap", "spot-avg", "binance")


def settle_twap_sec(interval: str) -> float:
    """Length of the settlement average per series: the 5m/15m/4h windows
    settle on Chainlink's 60s TWAP; the hourly series on a Binance candle's
    close — a single print."""
    return 0.0 if interval == "hourly" else SETTLE_TWAP_SEC


TWAP_SETTLEMENT_SINCE = {"5m": "2026-08-14", "15m": "2026-08-07", "4h": "2026-08-07"}


def twap_settlement_since(interval: str) -> Optional[str]:
    """'YYYY-MM-DD' the series switched to TWAP settlement, or None."""
    return TWAP_SETTLEMENT_SINCE.get(str(interval or "15m"))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


FEE_RATE_CRYPTO = 0.07
FEE_EXPONENT_CRYPTO = 1.0


def _fee_cents(price_cents: float, schedule: Optional[dict] = None) -> float:
    """Taker fee in cents/share at `price_cents`, using the market's own fee
    schedule when Gamma provided one (else the crypto-category curve)."""
    if schedule is not None and not schedule.get("enabled", True):
        return 0.0
    rate, exponent = FEE_RATE_CRYPTO, FEE_EXPONENT_CRYPTO
    if schedule is not None:
        if schedule.get("rate") is not None:
            rate = float(schedule["rate"])
        if schedule.get("exponent") is not None:
            exponent = float(schedule["exponent"])
    p = max(0.0, min(1.0, float(price_cents) / 100.0))
    return rate * ((p * (1.0 - p)) ** exponent) * 100.0 * (1.0 - TAKER_REBATE)


TAKER_REBATE = 0.0


def set_taker_rebate(pct) -> None:
    global TAKER_REBATE
    try:
        TAKER_REBATE = max(0.0, min(50.0, float(pct or 0.0))) / 100.0
    except (TypeError, ValueError):
        TAKER_REBATE = 0.0


def model_up_prob(
    spot: Optional[float], strike: Optional[float],
    sigma_1m: Optional[float], mins_left: Optional[float],
    *, known_sum: float = 0.0, known_count: int = 0,
    settle_twap_sec: float = SETTLE_TWAP_SEC,
) -> Optional[float]:
    """P(up): probability the settlement value (the `settle_twap_sec` TWAP at
    the close; a single print when 0) is ≥ the strike. `known_sum` /
    `known_count` are the live 1-second prints already observed inside the
    settlement window — ignored outside it. See the block comment above."""
    if spot is None or strike is None or sigma_1m is None or mins_left is None:
        return None
    if spot <= 0 or strike <= 0 or sigma_1m <= 0:
        return None
    L = float(settle_twap_sec or 0.0)
    if L <= 0:
        t = max(0.05, float(mins_left))
        sd_abs = sigma_1m * math.sqrt(t) * spot
        if sd_abs <= 0:
            return None
        return max(0.0, min(1.0, _norm_cdf((spot - strike) / sd_abs)))
    r = max(0.0, float(mins_left) * 60.0)
    var_s = (sigma_1m * spot) ** 2 / 60.0
    if r >= L:
        mean = spot
        var = var_s * (r - L + L / 3.0)
    else:
        elapsed = L - r
        known_avg = (known_sum / known_count) if known_count > 0 else spot
        mean = (elapsed * known_avg + r * spot) / L
        var = var_s * r ** 3 / (3.0 * L * L)
    var = max(var, var_s / (3.0 * L * L))
    sd_abs = math.sqrt(var)
    if sd_abs <= 0:
        return None
    return max(0.0, min(1.0, _norm_cdf((mean - strike) / sd_abs)))


def model_edge_net_cents(
    up_prob: Optional[float], yes_ask: Optional[float], no_ask: Optional[float],
    fee_schedule: Optional[dict] = None,
) -> Optional[float]:
    """Best fee-adjusted cents of edge the model sees on EITHER side: buy-up
    edge = P(up)·100 − upAsk − fee, buy-down mirrored. Positive = the model
    thinks a side is underpriced net of the taker fee (each side's fee is
    evaluated at its own ask on the fee curve); which side is implied by
    upProb vs the asks. None when the model or both asks are unavailable."""
    if up_prob is None:
        return None
    edges = []
    if yes_ask and 0 < yes_ask < 1:
        ask_c = yes_ask * 100.0
        edges.append(up_prob * 100.0 - ask_c - _fee_cents(ask_c, fee_schedule))
    if no_ask and 0 < no_ask < 1:
        ask_c = no_ask * 100.0
        edges.append((1.0 - up_prob) * 100.0 - ask_c - _fee_cents(ask_c, fee_schedule))
    if not edges:
        return None
    return round(max(edges), 2)


_STRIKE_GRACE_SEC = 12.0


def _ring_avg(asset: str, end_epoch: float,
              min_count: int = 45) -> Optional[float]:
    """Our own average of live 1-second spot prints over (end − span, end],
    from the Chainlink/Pyth ring, else the Coinbase ring. None when too
    sparse to stand in for the official TWAP."""
    for mod in (rtds_ws, spot_ws):
        try:
            total, count = mod.window_partial(asset, end_epoch, now=end_epoch)
        except Exception:
            continue
        if count >= min_count:
            return total / count
    return None


def _track_window_open(
    asset: str, window_start: int, spot: Optional[float],
    now_epoch: Optional[float] = None,
) -> Optional[float]:
    """Record a window's strike (the price to beat) and return it.

    Priority:
      1. the official Chainlink 60s TWAP point stamped at the open second;
      2. our own 60s average of live spot ending at the open ("spot-avg");
      3. the boundary print from a ring, or the current spot within
         _STRIKE_GRACE_SEC of the open ("spot" — approximate under TWAP
         settlement; model entries refuse it);
      4. otherwise None for the window's whole life — a mid-window restart
         (rings empty, grace blown) sits out rather than fabricate a strike.
    A strike recorded from a fallback is upgraded the moment the official
    TWAP point shows up (it can arrive in PolyBolt's replay a little later)."""
    key = (asset, window_start)
    cur = _window_open.get(key)
    if cur is not None and cur[1] == "twap":
        return cur[0]
    official = rtds_ws.twap_at(asset, window_start)
    if official is not None:
        _window_open[key] = (official, "twap")
    elif cur is None:
        avg = _ring_avg(asset, window_start)
        if avg is not None:
            _window_open[key] = (avg, "spot-avg")
        else:
            strike = rtds_ws.sample_at(asset, window_start)
            if strike is None:
                strike = spot_ws.sample_at(asset, window_start)
            if strike is None:
                if now_epoch is not None and (now_epoch - window_start) > _STRIKE_GRACE_SEC:
                    return None
                strike = spot
            if strike is None:
                return None
            _window_open[key] = (strike, "spot")
    if len(_window_open) > 200:
        for old in sorted(_window_open, key=lambda k: k[1])[:50]:
            _window_open.pop(old, None)
    return _window_open[key][0]


def strike_source(asset: str, window_start: int) -> Optional[str]:
    rec = _window_open.get((asset, window_start))
    return rec[1] if rec else None


def known_partial(asset: str, close_epoch: float, now_epoch: float) -> tuple[float, int]:
    """Live 1-second prints already inside the settlement average
    (close − 60s, now] — Chainlink/Pyth ring first, else Coinbase."""
    for mod in (rtds_ws, spot_ws):
        try:
            total, count = mod.window_partial(asset, close_epoch, now=now_epoch)
        except Exception:
            continue
        if count > 0:
            return total, count
    return 0.0, 0


def _ws_mid(q: Optional[dict]) -> Optional[float]:
    """Sane best-bid/ask mid (0..1) from a live WS quote, or None if the book is
    one-sided / crossed / out of range (so the caller falls back to Gamma)."""
    if not q:
        return None
    b, a = q.get("bid_cents"), q.get("ask_cents")
    if b is None or a is None or not (0 < b <= a <= 100):
        return None
    return (b + a) / 200.0


def _ws_up_price(yes_q: Optional[dict], no_q: Optional[dict]) -> Optional[float]:
    """Real 'up' probability (0..1) from the live CLOB book, or None if unusable.
    Averages the YES mid and (1 − NO mid) when both are sane for a tighter, less
    one-sided estimate; else uses whichever side is sane."""
    ym, nm = _ws_mid(yes_q), _ws_mid(no_q)
    if ym is not None and nm is not None:
        return max(0.0, min(1.0, (ym + (1.0 - nm)) / 2.0))
    if ym is not None:
        return ym
    if nm is not None:
        return max(0.0, min(1.0, 1.0 - nm))
    return None


def _ws_fav_ask(favorite: str, yes_q: Optional[dict], no_q: Optional[dict]) -> Optional[float]:
    """Real cost to BUY the favorite side = its best ASK (fraction), or None."""
    q = yes_q if favorite == "up" else no_q
    a = (q or {}).get("ask_cents")
    if a is None or not (0 < a <= 100):
        return None
    return a / 100.0


async def _asset_snapshot(entry: dict, spot: Optional[float], cfg: dict, now_epoch: float,
                          *, spot_stale: bool = False) -> dict:
    asset, series = entry["asset"], entry["series"]
    interval = _interval(cfg)
    model_spot_ok = not spot_stale
    if interval == "hourly":
        try:
            bspot = (await binance_ref.spots([asset])).get(asset)
        except Exception:
            bspot = None
        if bspot:
            spot = bspot
            spot_stale = False
        model_spot_ok = bool(bspot)
    _note_spot(asset, spot)
    try:
        markets = await polymarket_api.fetch_crypto_updown(asset, interval, fast=True)
    except Exception as e:
        return _blank_asset(entry, spot, _friendly_market_error(e))

    candidates: list[tuple[float, dict]] = []
    for m in markets:
        ce = float(m.get("window_close_epoch") or 0)
        if ce <= now_epoch:
            continue
        candidates.append((ce, m))

    out = _blank_asset(entry, spot)
    out["openMarketCount"] = len(candidates)
    if not candidates:
        return out

    candidates.sort(key=lambda x: x[0])
    close_epoch, m = candidates[0]

    window_start = int(m.get("window_start_epoch") or (close_epoch - _INTERVAL_SEC[interval]))
    if interval == "hourly":
        open15m = await binance_ref.hour_open(asset, window_start)
        if open15m is not None:
            src = "binance-spot-proxy" if asset in binance_ref.FUTURES_SETTLED else "binance"
            _window_open[(asset, window_start)] = (open15m, src)
    else:
        open15m = _track_window_open(asset, window_start, spot, now_epoch)
    delta = abs(open15m - spot) if (open15m is not None and spot is not None) else None
    delta_pct = (delta / open15m) if (delta is not None and open15m) else None

    yt, nt = m.get("yes_token"), m.get("no_token")
    ws_yes = ws_no = None
    if cfg.get("crypto15m_ws_book", True):
        try:
            clob_ws.feed.observe(yt, nt)
            clob_ws.feed.start()
            ws_yes = clob_ws.feed.get_quote_cents(yt)
            ws_no = clob_ws.feed.get_quote_cents(nt)
        except Exception as e:
            logger.debug(f"crypto15m ws book {asset}: {e}")

    up_ws = _ws_up_price(ws_yes, ws_no)
    if up_ws is not None:
        up = up_ws
        price_source = "ws"
        yes_bid = (ws_yes.get("bid_cents") / 100.0) if (ws_yes and ws_yes.get("bid_cents")) else up
        yes_ask = (ws_yes.get("ask_cents") / 100.0) if (ws_yes and ws_yes.get("ask_cents")) else up
    else:
        up = max(0.0, min(1.0, _price_dollars(m, "last_price")
                          or _price_dollars(m, "yes_bid")))
        price_source = "gamma"
        yes_bid = up
        yes_ask = up
    down = 1.0 - up
    favorite = "up" if up >= down else "down"
    fav_price = up if favorite == "up" else down

    entry_cost = _ws_fav_ask(favorite, ws_yes, ws_no)
    if entry_cost is None:
        entry_cost = max(0.0, min(1.0, fav_price))
    fav_q = ws_yes if favorite == "up" else ws_no
    if fav_q:
        out["wsBid"] = fav_q.get("bid_cents")
        out["wsAsk"] = fav_q.get("ask_cents")
    out["priceSource"] = price_source

    mins_left = (close_epoch - now_epoch) / 60.0
    hour_utc = datetime.fromtimestamp(now_epoch, timezone.utc).hour
    in_window = mins_left <= _const(cfg, "time_delay_min")
    signal = (
        in_window
        and hours_ok(cfg)
        and fav_price >= _const(cfg, "entry_threshold")
        and entry_cost <= _const(cfg, "entry_max")
        and (delta_pct is None or delta_pct >= _const(cfg, "min_delta_pct"))
    )

    delta_signed_pct = (
        ((spot - open15m) / open15m)
        if (spot is not None and open15m) else None
    )

    out.update({
        "open15mUsd": open15m, "deltaUsd": delta, "deltaPct": delta_pct,
        "deltaSignedPct": round(delta_signed_pct, 6) if delta_signed_pct is not None else None,
        "strikeUsd": open15m,
        "strikeSource": strike_source(asset, window_start),
        "settleTwapUsd": rtds_ws.twap(asset) if interval != "hourly" else None,
        "hasMarket": True, "ticker": m.get("ticker"),
        "closeTime": m.get("close_time"), "minsLeft": round(mins_left, 2),
        "upProb": round(up, 4), "downProb": round(down, 4),
        "favorite": favorite, "favoritePrice": round(fav_price, 4),
        "entryCost": round(entry_cost, 4),
        "yesBid": round(yes_bid, 4) if yes_bid else None,
        "yesAsk": round(yes_ask, 4) if yes_ask else None,
        "inWindow": in_window, "signal": signal, "hourUtc": hour_utc,
    })

    _up_ask_c = (ws_yes or {}).get("ask_cents")
    _dn_ask_c = (ws_no or {}).get("ask_cents")
    if _up_ask_c:
        out["upAsk"] = round(_up_ask_c / 100.0, 4)
    if _dn_ask_c:
        out["downAsk"] = round(_dn_ask_c / 100.0, 4)

    if cfg.get("crypto15m_arb_detect", True):
        thresh = float(cfg.get("crypto15m_arb_min_edge_cents", 1.0) or 0.0)
        if _up_ask_c and _dn_ask_c:
            edge_c = round(100.0 - (_up_ask_c + _dn_ask_c), 1)
            out["arbEdgeCents"] = edge_c
            out["arbSignal"] = edge_c >= thresh
        else:
            try:
                arb = await polymarket_api.updown_arb_edge(m.get("ticker"))
            except Exception:
                arb = None
            if arb:
                out["upAsk"] = arb["upAsk"]
                out["downAsk"] = arb["downAsk"]
                out["arbEdgeCents"] = arb["edgeCents"]
                out["arbSignal"] = arb["edgeCents"] >= thresh

    if cfg.get("crypto15m_imbalance_detect", True):
        try:
            imb = await polymarket_api.book_imbalance(
                m.get("yes_token"), int(cfg.get("crypto15m_imbalance_levels", 3) or 3)
            )
        except Exception:
            imb = None
        out["bookImbalance"] = imb

    if cfg.get("crypto15m_indicator_detect", True):
        try:
            ind = await asyncio.wait_for(asset_indicators(asset), 4.0)
        except Exception:
            ind = {}
        out["macd"] = ind.get("macd")
        out["macdSignal"] = ind.get("macdSignal")
        out["macdHist"] = ind.get("macdHist")
        out["macdCross"] = ind.get("macdCross")
        out["rsi"] = ind.get("rsi")
        out["sigma1m"] = ind.get("sigma1m")
        for _k in ("vwap1h", "ema12", "sma20", "sma50", "priceVsVwapPct",
                   "ema12VsSma20Pct", "ema1VsSma5Pct", "velocity1mPct",
                   "change5mPct", "change15mPct"):
            out[_k] = ind.get(_k)
        _lv = live_velocity_pct(asset, spot)
        if _lv is not None:
            out["velocity1mPct"] = round(_lv, 4)
        out["spotLive"] = (
            interval != "hourly"
            and (rtds_ws.spot(asset) is not None or spot_ws.spot(asset) is not None)
        )
        _L = settle_twap_sec(interval)
        _ks, _kc = known_partial(asset, close_epoch, now_epoch) if _L > 0 else (0.0, 0)
        out["spotStale"] = spot_stale
        mp = model_up_prob(spot if model_spot_ok else None, open15m, ind.get("sigma1m"),
                           mins_left, known_sum=_ks, known_count=_kc, settle_twap_sec=_L)
        out["settleKnownSec"] = _kc
        out["modelProb"] = round(mp, 4) if mp is not None else None
        out["feeSchedule"] = m.get("fee_schedule")
        out["edgeNetCents"] = model_edge_net_cents(
            mp, out.get("upAsk"), out.get("downAsk"), m.get("fee_schedule"),
        )
    return out


_INTERVAL_MINUTES = {"5m": 5.0, "15m": 15.0, "1h": 60.0, "hourly": 60.0, "4h": 240.0}


def derive_script_fields(a: dict, interval: str = "15m") -> None:
    """Add the convenience fields scripts read, IN PLACE.

    Called from BOTH the live snapshot and the backtest's tick_to_asset, so
    these can never drift between the two — which is the whole reason they
    live here instead of being recomputed at each call site. Every field is a
    pure function of values already present in the dict (no per-window memory,
    no clock), so live and replay produce identical results from identical
    inputs. Anything missing stays None: a script comparing a None it didn't
    check crashes and gets disabled, so silence is never an option.
    """
    yb, ya = a.get("yesBid"), a.get("yesAsk")
    a["spreadCents"] = (
        round((float(ya) - float(yb)) * 100.0, 2)
        if yb is not None and ya is not None else None
    )

    mp, up = a.get("modelProb"), a.get("upProb")
    a["modelEdgePts"] = (
        round((float(mp) - float(up)) * 100.0, 2)
        if mp is not None and up is not None else None
    )

    fav = a.get("favorite")
    fav_ask = a.get("upAsk") if fav == "up" else (
        a.get("downAsk") if fav == "down" else None)
    a["favoriteAskCents"] = (
        round(float(fav_ask) * 100.0, 1) if fav_ask is not None else None)

    ml = a.get("minsLeft")
    span = _INTERVAL_MINUTES.get(str(interval or "15m"))
    a["timeFracLeft"] = (
        round(max(0.0, min(1.0, float(ml) / span)), 4)
        if ml is not None and span else None
    )


_SPOT_FETCH_TIMEOUT = 6.0
_ASSET_SNAPSHOT_TIMEOUT = 10.0


def _apply_cross_asset(assets: list[dict], now_epoch: float) -> None:
    """Compute timing + cross-asset correlation fields across the whole snapshot
    and inject them per-asset (so the rule builder can gate on them):

      hourUtc     current UTC hour 0-23 (also stamped on blank/errored assets)
      marketBias  market-wide directional lean = (#up - #down) / #withFavorite,
                  range -1..1; same for every asset (a breadth gauge)
      peersAgree  fraction of the OTHER favorited assets whose favorite matches
                  this asset's, range 0..1 — the correlation gate: high = the
                  pack agrees (herding/momentum), low = this coin diverges.

    These are OPTIONAL rule fields, never forced gates — assets can and do
    diverge, so the user decides whether agreement helps or hurts their edge."""
    hour_utc = datetime.fromtimestamp(now_epoch, timezone.utc).hour
    favs = [a.get("favorite") for a in assets
            if a.get("hasMarket") and a.get("favorite") in ("up", "down")]
    n_up = favs.count("up")
    n_down = favs.count("down")
    total = n_up + n_down
    market_bias = round((n_up - n_down) / total, 4) if total else None

    for a in assets:
        a["hourUtc"] = hour_utc
        a["marketBias"] = market_bias
        a["peersAgree"] = None
        fav = a.get("favorite")
        if not a.get("hasMarket") or fav not in ("up", "down") or total <= 1:
            continue
        peers = total - 1
        same = (n_up - 1) if fav == "up" else (n_down - 1)
        a["peersAgree"] = round(same / peers, 4) if peers else None


_snapshot_cache: dict = {"at": 0.0, "data": None, "interval": None}
_SNAPSHOT_TTL = 3.0


async def snapshot(cfg: dict) -> dict:
    """Build the full monitor snapshot for all seven series. Resilient:
    a failure OR a timeout on the spot feed or any single series degrades that
    field rather than failing/hanging the whole call.

    Cached for ~3s so the 4-second executor poll and the open Crypto tab SHARE
    one snapshot instead of each firing the full ~7-calls-per-series fan-out —
    halving the API load (a 429 hazard). 3s is well inside the executor's poll
    interval and a 15-min window, so the slight staleness is immaterial."""
    now_epoch = datetime.now(timezone.utc).timestamp()
    cached = _snapshot_cache.get("data")
    if (
        cached is not None
        and _snapshot_cache.get("interval") == _interval(cfg)
        and (now_epoch - _snapshot_cache.get("at", 0.0)) < _SNAPSHOT_TTL
    ):
        return cached
    try:
        spots, spot_source, stale = await asyncio.wait_for(_fetch_spots(), _SPOT_FETCH_TIMEOUT)
    except Exception as e:
        logger.debug(f"crypto15m spot fetch failed/timeout: {e}")
        spots = dict(_spot_cache.get("spots") or {})
        spot_source = f"{_spot_cache.get('source', '?')} (stale)" if spots else "unavailable"
        stale = set(spots)
    spot_ok = bool(spots)

    async def _bounded(entry: dict) -> dict:
        try:
            return await asyncio.wait_for(
                _asset_snapshot(entry, spots.get(entry["asset"]), cfg, now_epoch,
                                spot_stale=entry["asset"] in stale),
                _ASSET_SNAPSHOT_TIMEOUT,
            )
        except Exception as e:
            return _blank_asset(entry, spots.get(entry["asset"]), _friendly_market_error(e))

    results = await asyncio.gather(*[_bounded(s) for s in SERIES])
    assets: list[dict] = []
    for s, r in zip(SERIES, results):
        a = r if not isinstance(r, Exception) else _blank_asset(s, spots.get(s["asset"]), _friendly_market_error(r))
        a["enabled"] = asset_enabled(cfg, s["asset"])
        assets.append(a)

    _apply_cross_asset(assets, now_epoch)
    for a in assets:
        derive_script_fields(a, _interval(cfg))

    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "spotOk": spot_ok,
        "spotSource": spot_source,
        "hoursOk": hours_ok(cfg),
        "constants": {
            "timeDelayMin": _const(cfg, "time_delay_min"),
            "entryThreshold": _const(cfg, "entry_threshold"),
            "exitThreshold": _const(cfg, "exit_threshold"),
            "entryMax": _const(cfg, "entry_max"),
            "minDeltaPct": _const(cfg, "min_delta_pct"),
            "entryDiff": _const(cfg, "entry_diff"),
            "directionMode": str(cfg.get("crypto15m_direction_mode", "favorite")),
            "entryStyle": str(cfg.get("crypto15m_entry_style", "maker")),
            "hoursStartUtc": int(cfg.get("crypto15m_hours_start_utc", 0) or 0),
            "hoursEndUtc": int(cfg.get("crypto15m_hours_end_utc", 24) or 24),
        },
        "assets": assets,
    }
    _snapshot_cache["at"] = now_epoch
    _snapshot_cache["data"] = result
    _snapshot_cache["interval"] = _interval(cfg)
    return result
