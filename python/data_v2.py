from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("data_v2")

BASE = "https://data-api.polymarket.com/v2"
_client: Optional[httpx.AsyncClient] = None
_cache: dict[tuple, tuple[float, Any]] = {}

PNL_INTERVALS = ("1d", "1w", "1m", "max")
_FIDELITY = {"1d": "1h", "1w": "12h", "1m": "1d", "max": "1d"}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=BASE, timeout=10.0,
                                    headers={"User-Agent": "KryptPolyBot/1.0"})
    return _client


async def close_clients() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None


async def _get(path: str, params: dict, *, ttl: float) -> Any:
    key = (path, tuple(sorted((k, str(v)) for k, v in params.items() if v is not None)))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    for attempt in range(3):
        resp = await _get_client().get(path, params={k: v for k, v in params.items() if v is not None})
        if resp.status_code == 429:
            try:
                wait = float(resp.headers.get("Retry-After") or 1)
            except ValueError:
                wait = 1.0
            await asyncio.sleep(min(5.0, wait))
            continue
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else body
        _cache[key] = (time.monotonic(), data)
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
        return data
    raise RuntimeError("data API rate-limited")


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_PNL_FIELDS = (
    "economic_pnl", "realized_pnl", "unrealized_pnl", "trade_pnl", "settled_pnl",
    "fees_paid", "fees_refunded", "maker_rebate", "taker_rebate", "reward_income",
    "referral_income", "volume_usdc", "trade_count",
)


def summarize_pnl(points: list[dict]) -> dict:
    pts = [p for p in points or [] if isinstance(p, dict)]
    if not pts:
        return {"points": 0}
    pts.sort(key=lambda p: int(p.get("timestamp") or 0))
    first, last = pts[0], pts[-1]
    out: dict[str, Any] = {"points": len(pts),
                           "from": int(first.get("timestamp") or 0),
                           "to": int(last.get("timestamp") or 0)}
    for k in _PNL_FIELDS:
        a, b = _f(first.get(k)), _f(last.get(k))
        if b is None:
            continue
        out[k] = b if (k == "unrealized_pnl" or a is None) else b - a
    out["rebatesAndRewards"] = sum(out.get(k) or 0.0 for k in (
        "maker_rebate", "taker_rebate", "reward_income", "referral_income", "fees_refunded"))
    base = _f(first.get("economic_pnl")) or 0.0
    out["series"] = [[int(p.get("timestamp") or 0), round((_f(p.get("economic_pnl")) or 0.0) - base, 4)]
                     for p in pts]
    return out


async def user_pnl(user: str, interval: str = "1w") -> dict:
    iv = interval if interval in PNL_INTERVALS else "1w"
    data = await _get("/user-pnl", {"user": user, "interval": iv,
                                    "fidelity": _FIDELITY[iv]}, ttl=60.0)
    points = (data or {}).get("points") or [] if isinstance(data, dict) else []
    return {"interval": iv, **summarize_pnl(points)}


def _resolution_row(r: dict) -> dict:
    return {
        "status": r.get("status"),
        "expectedSettlementTime": r.get("expected_settlement_time"),
        "settlementTimeBasis": r.get("settlement_time_basis"),
        "extendedReview": bool(r.get("extended_review")),
        "wasDisputed": bool(r.get("was_disputed")),
        "proposedPrice": r.get("proposed_price"),
    }


async def resolutions(condition_ids: list[str]) -> dict[str, dict]:
    ids = [c for c in dict.fromkeys(condition_ids or []) if isinstance(c, str) and c.startswith("0x")]
    out: dict[str, dict] = {}
    chunks = [ids[i:i + 20] for i in range(0, len(ids), 20)]
    results = await asyncio.gather(*(
        _get("/resolutions", {"condition": ",".join(ch)}, ttl=60.0) for ch in chunks
    ), return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException) or not isinstance(res, list):
            continue
        for r in res:
            cid = r.get("condition_id")
            if cid:
                out[cid] = _resolution_row(r)
    return out


async def holders(condition_id: str, limit: int = 10) -> list[dict]:
    data = await _get("/holders", {"condition": condition_id, "limit": max(1, min(100, limit)),
                                   "include_pnl": "true"}, ttl=60.0)
    out = []
    for tok in data or []:
        for h in tok.get("holders") or []:
            out.append({
                "wallet": h.get("proxy_wallet"),
                "name": h.get("name") or h.get("pseudonym") or "",
                "outcomeIndex": h.get("outcome_index"),
                "shares": _f(h.get("amount")),
                "avgPrice": _f(h.get("avg_price")),
                "currentValue": _f(h.get("current_value")),
                "totalPnl": _f(h.get("total_pnl")),
                "realizedPnl": _f(h.get("realized_pnl")),
                "unrealizedPnl": _f(h.get("unrealized_pnl")),
            })
    return out


async def biggest_winners(period: str = "week", category: str = "overall",
                          limit: int = 20) -> list[dict]:
    p = period if period in ("day", "week", "month", "all") else "week"
    data = await _get("/biggest-winners", {"time_period": p, "category": category or "overall",
                                           "limit": max(1, min(50, limit))}, ttl=300.0)
    return [{
        "rank": r.get("win_rank"), "wallet": r.get("user_id"), "name": r.get("user_name") or "",
        "pnl": _f(r.get("pnl")), "initialValue": _f(r.get("initial_value")),
        "eventTitle": r.get("event_title") or "", "eventSlug": r.get("event_slug") or "",
        "resolvedAt": r.get("resolved_at"),
    } for r in (data or []) if isinstance(r, dict)]
