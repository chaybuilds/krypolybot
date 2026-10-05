from __future__ import annotations

import asyncio
import logging
import math
import secrets
import struct
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Optional

import httpx

import polymarket_auth as auth

logger = logging.getLogger("perps")

BASE_URL = "https://api.perpetuals.polymarket.com"
CHAIN_ID = 137
PROXY_TTL_MS = 7 * 24 * 3600 * 1000
PROXY_RENEW_BEFORE_MS = 12 * 3600 * 1000
SIG_FIGS = 5

_EIP712_DOMAIN = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
]


class PerpsAPIError(RuntimeError):

    def __init__(self, message: str, *, status: int = 0, error: str = "",
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.error = error or message
        self.retry_after = retry_after


def _pack(obj: Any) -> bytes:
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, int):
        if 0 <= obj <= 0x7F:
            return bytes([obj])
        if -32 <= obj < 0:
            return struct.pack(">b", obj)
        if obj >= 0:
            for fmt, tag, hi in ((">B", 0xCC, 0xFF), (">H", 0xCD, 0xFFFF),
                                 (">I", 0xCE, 0xFFFFFFFF), (">Q", 0xCF, 2**64 - 1)):
                if obj <= hi:
                    return bytes([tag]) + struct.pack(fmt, obj)
            raise ValueError("integer too large for msgpack")
        for fmt, tag, lo in ((">b", 0xD0, -(2**7)), (">h", 0xD1, -(2**15)),
                             (">i", 0xD2, -(2**31)), (">q", 0xD3, -(2**63))):
            if obj >= lo:
                return bytes([tag]) + struct.pack(fmt, obj)
        raise ValueError("integer too small for msgpack")
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        n = len(raw)
        if n <= 31:
            head = bytes([0xA0 | n])
        elif n <= 0xFF:
            head = b"\xd9" + struct.pack(">B", n)
        elif n <= 0xFFFF:
            head = b"\xda" + struct.pack(">H", n)
        else:
            head = b"\xdb" + struct.pack(">I", n)
        return head + raw
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        if n <= 15:
            head = bytes([0x90 | n])
        elif n <= 0xFFFF:
            head = b"\xdc" + struct.pack(">H", n)
        else:
            head = b"\xdd" + struct.pack(">I", n)
        return head + b"".join(_pack(x) for x in obj)
    raise TypeError(f"cannot msgpack {type(obj).__name__}")


def _compact(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_compact(v) for v in value if v is not None]
    return value


def hash_op(op: list) -> str:
    from eth_utils.crypto import keccak
    return "0x" + keccak(_pack(_compact(op))).hex()


def op_typed_data(op: list, salt: int, ts_ms: int) -> dict:
    return {
        "types": {
            "EIP712Domain": _EIP712_DOMAIN,
            "Op": [
                {"name": "data", "type": "bytes32"},
                {"name": "salt", "type": "uint64"},
                {"name": "ts", "type": "uint64"},
            ],
        },
        "primaryType": "Op",
        "domain": {"name": "Polymarket", "version": "1", "chainId": CHAIN_ID},
        "message": {"data": hash_op(op), "salt": salt, "ts": ts_ms},
    }


def sign_op(private_key: str, op: list) -> dict:
    from eth_account import Account
    salt = secrets.randbits(32)
    ts = int(time.time() * 1000)
    signed = Account.from_key(private_key).sign_typed_data(
        full_message=op_typed_data(op, salt, ts))
    sig = bytes(signed.signature).hex()
    return {"salt": salt, "sig": "0x" + sig, "ts": ts}


def order_row(iid: int, buy: bool, price: Optional[str], qty: str, *,
              tif: str = "gtc", post_only: bool = True, reduce_only: bool = False,
              coid: Optional[str] = None) -> list:
    return [iid, bool(buy), price, qty, tif, bool(post_only),
            True if reduce_only else None, coid, None]


def _order_body(row: list) -> dict:
    body: dict[str, Any] = {"iid": row[0], "buy": row[1], "po": row[5], "qty": row[3]}
    if row[4] is not None:
        body["tif"] = row[4]
    if row[6]:
        body["ro"] = row[6]
    if row[2] is not None:
        body["p"] = row[2]
    if row[7] is not None:
        body["c"] = row[7]
    return body


def body_op(op: list) -> dict:
    kind = op[0]
    if kind == "createOrders":
        return {"type": kind, "args": [_order_body(r) for r in op[1]]}
    if kind in ("cancelOrders", "cancelOrdersCOID"):
        return {"type": kind, "args": list(op[1])}
    if kind == "cancelAll":
        return {"type": kind, "args": {"iid": op[1][0]} if op[1] else {}}
    if kind == "autoCancel":
        return {"type": kind, "args": {"time": op[1][0]}}
    if kind == "updateLeverage":
        iid, lev, cross = op[1]
        return {"type": kind, "args": {"cross": cross, "iid": iid, "lev": lev}}
    raise ValueError(f"unsupported perps command {kind!r}")


def price_step(price: float, price_decimals: int) -> float:
    if price <= 0:
        return 10.0 ** -price_decimals
    mag = math.floor(math.log10(price))
    return max(10.0 ** -price_decimals, 10.0 ** (mag - SIG_FIGS + 1))


def fmt_price(price: float, price_decimals: int, *, side: str) -> str:
    step = Decimal(str(price_step(price, price_decimals)))
    q = Decimal(str(price)) / step
    q = q.to_integral_value(rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING)
    out = q * step
    return _plain(out)


def fmt_qty(qty: float, quantity_decimals: int) -> str:
    if qty <= 0:
        return "0"
    mag = math.floor(math.log10(qty))
    step = max(10.0 ** -quantity_decimals, 10.0 ** (mag - SIG_FIGS + 1))
    d = Decimal(str(step))
    out = (Decimal(str(qty)) / d).to_integral_value(rounding=ROUND_FLOOR) * d
    return _plain(out)


def _plain(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s if s not in ("-0",) else "0"


_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=8.0,
            headers={"Accept": "application/json", "User-Agent": "KryptPolyBot/1.0"},
        )
    return _client


async def close_clients() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None


def _raise_for(resp: httpx.Response) -> Any:
    try:
        data = resp.json()
    except Exception:
        data = None
    if resp.status_code >= 400:
        err = ""
        if isinstance(data, dict):
            err = str(data.get("error") or data.get("message") or "")
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            err = str(data[0].get("error") or "")
        msg = err or (resp.text or "")[:200] or f"HTTP {resp.status_code}"
        if resp.status_code in (401, 403) and not err:
            msg = ("Perps request refused (HTTP 403) — Perps trading is not available "
                   "from the US, Canada and some other regions.")
        ra = None
        try:
            ra = float(resp.headers.get("Retry-After")) if resp.headers.get("Retry-After") else None
        except (TypeError, ValueError):
            ra = None
        raise PerpsAPIError(msg, status=resp.status_code, error=err, retry_after=ra)
    return data


async def _get(path: str, params: Optional[dict] = None, *, creds: Optional[dict] = None) -> Any:
    headers = {}
    if creds:
        headers = {"polymarket-proxy": creds["proxy"], "polymarket-secret": creds["secret"]}
    q = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = await _get_client().get(path, params=q, headers=headers)
    except httpx.HTTPError as e:
        raise PerpsAPIError(f"Perps API unreachable: {type(e).__name__}", error="network") from e
    return _raise_for(resp)


async def _signed(method: str, path: str, op: list, creds: dict) -> Any:
    body = {"op": body_op(op), **sign_op(creds["privateKey"], op)}
    try:
        resp = await _get_client().request(method, path, json=body)
    except httpx.HTTPError as e:
        raise PerpsAPIError(f"Perps API unreachable: {type(e).__name__}", error="network") from e
    return _raise_for(resp)


_instruments_cache: dict = {"at": 0.0, "rows": []}
_INSTRUMENTS_TTL = 600.0


async def instruments(force: bool = False) -> list[dict]:
    now = time.monotonic()
    if (not force and _instruments_cache["rows"]
            and now - _instruments_cache["at"] < _INSTRUMENTS_TTL):
        return _instruments_cache["rows"]
    rows = await _get("/v1/info/instruments")
    if isinstance(rows, list):
        _instruments_cache.update(at=now, rows=rows)
    return _instruments_cache["rows"]


async def tickers() -> list[dict]:
    rows = await _get("/v1/info/tickers")
    return rows if isinstance(rows, list) else []


async def book(iid: int, depth: int = 100) -> dict:
    data = await _get("/v1/info/book", {"instrument_id": iid, "depth": depth})
    return data if isinstance(data, dict) else {}


async def klines(iid: int, interval: str, start_ms: int, end_ms: Optional[int] = None) -> list:
    data = await _get("/v1/info/klines", {
        "instrument_id": iid, "interval": interval,
        "start_timestamp": start_ms, "end_timestamp": end_ms,
    })
    return (data or {}).get("data") or [] if isinstance(data, dict) else []


def creds() -> Optional[dict]:
    c = auth.load_perps_creds()
    if not c or not all(c.get(k) for k in ("proxy", "privateKey", "secret")):
        return None
    if int(c.get("expiresAt") or 0) <= int(time.time() * 1000):
        return None
    return c


def creds_status() -> dict:
    c = auth.load_perps_creds()
    now = int(time.time() * 1000)
    exp = int((c or {}).get("expiresAt") or 0)
    return {
        "linked": bool(c) and exp > now,
        "expired": bool(c) and exp <= now,
        "expiresAt": exp or None,
        "proxy": (c or {}).get("proxy"),
        "owner": (c or {}).get("owner"),
        "walletPresent": auth.credentials_present(),
    }


async def link_account(label: str = "krypt-polybot") -> dict:
    from eth_account import Account
    if not auth.credentials_present():
        raise PerpsAPIError("Connect your Polymarket wallet first (Wallet page).",
                            error="no_wallet")
    owner = auth.get_address()
    proxy_key = "0x" + secrets.token_bytes(32).hex()
    proxy = Account.from_key(proxy_key).address
    ts = int(time.time() * 1000)
    exp = ts + PROXY_TTL_MS
    salt = secrets.randbits(32)
    typed = {
        "types": {
            "EIP712Domain": _EIP712_DOMAIN,
            "CreateProxy": [
                {"name": "addr", "type": "address"},
                {"name": "exp", "type": "uint64"},
                {"name": "salt", "type": "uint64"},
                {"name": "ts", "type": "uint64"},
            ],
        },
        "primaryType": "CreateProxy",
        "domain": {"name": "Polymarket", "version": "1", "chainId": CHAIN_ID},
        "message": {"addr": proxy, "exp": exp, "salt": salt, "ts": ts},
    }
    sig = auth.sign_typed_data(typed)
    body = {
        "op": {"type": "createProxy", "args": {"expiry": exp, "owner": owner, "proxy": proxy}},
        "salt": salt, "sig": sig, "ts": ts, "label": label,
    }
    try:
        resp = await _get_client().post("/v1/account/proxy", json=body)
    except httpx.HTTPError as e:
        raise PerpsAPIError(f"Perps API unreachable: {type(e).__name__}", error="network") from e
    data = _raise_for(resp)
    secret = data.get("secret") if isinstance(data, dict) else None
    if not secret:
        raise PerpsAPIError("Perps did not return a proxy secret.", error="bad_response")
    c = {"proxy": proxy, "privateKey": proxy_key, "secret": secret,
         "expiresAt": exp, "owner": owner}
    info = await _get("/v1/account/credentials", creds=c)
    addr = str((info or {}).get("address") or "") if isinstance(info, dict) else ""
    if addr and addr.lower() != owner.lower():
        raise PerpsAPIError("Perps linked a different account than this wallet.",
                            error="owner_mismatch")
    auth.save_perps_creds(c)
    return creds_status()


def unlink_account() -> None:
    auth.clear_perps_creds()


def needs_renewal(c: Optional[dict]) -> bool:
    if not c:
        return False
    return int(c.get("expiresAt") or 0) - int(time.time() * 1000) < PROXY_RENEW_BEFORE_MS


async def portfolio(c: dict) -> dict:
    data = await _get("/v1/account/portfolio", creds=c)
    return data if isinstance(data, dict) else {}


async def open_orders(c: dict, iid: Optional[int] = None) -> list[dict]:
    data = await _get("/v1/account/open-orders", {"instrument_id": iid}, creds=c)
    if isinstance(data, dict):
        data = data.get("data") or []
    return data if isinstance(data, list) else []


async def fills(c: dict, start_ms: int, end_ms: Optional[int] = None,
                max_pages: int = 20) -> list[dict]:
    out: list[dict] = []
    cursor = start_ms
    for _ in range(max_pages):
        data = await _get("/v1/account/fills", {
            "start_timestamp": cursor, "end_timestamp": end_ms}, creds=c)
        rows = (data or {}).get("data") or [] if isinstance(data, dict) else []
        if not rows:
            break
        rows = sorted(rows, key=lambda r: int(r.get("timestamp") or 0))
        out.extend(rows)
        if not (isinstance(data, dict) and data.get("more")):
            break
        nxt = int(rows[-1].get("timestamp") or 0) + 1
        if nxt <= cursor:
            break
        cursor = nxt
    return out


async def stats(c: dict) -> dict:
    data = await _get("/v1/account/stats", creds=c)
    return data if isinstance(data, dict) else {}


async def rewards(c: dict) -> list[dict]:
    data = await _get("/v1/account/rewards", creds=c)
    if isinstance(data, dict):
        data = data.get("data") or data.get("rewards") or []
    return data if isinstance(data, list) else []


async def create_orders(c: dict, rows: list[list]) -> list[dict]:
    if not rows:
        return []
    data = await _signed("POST", "/v1/trade/orders", ["createOrders", rows], c)
    return data if isinstance(data, list) else []


async def cancel_orders(c: dict, order_ids: list[int]) -> list[dict]:
    if not order_ids:
        return []
    data = await _signed("DELETE", "/v1/trade/orders", ["cancelOrders", list(order_ids)], c)
    return data if isinstance(data, list) else []


async def cancel_all(c: dict, iid: Optional[int] = None) -> None:
    await _signed("DELETE", "/v1/trade/orders/all",
                  ["cancelAll", [] if iid is None else [iid]], c)


async def auto_cancel(c: dict, at_ms: int) -> None:
    await _signed("PATCH", "/v1/trade/auto-cancel", ["autoCancel", [int(at_ms)]], c)


async def update_leverage(c: dict, iid: int, leverage: int, cross: bool) -> Any:
    return await _signed("PATCH", "/v1/trade/leverage",
                         ["updateLeverage", [int(iid), int(leverage), bool(cross)]], c)


async def gather_quiet(*aws):
    res = await asyncio.gather(*aws, return_exceptions=True)
    return [None if isinstance(r, BaseException) else r for r in res]
