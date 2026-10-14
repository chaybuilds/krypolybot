"""Native official Polymarket US adapter, isolated from Polygon/CLOB exchange code.

LIVE ORDERS ARE OFF BY DEFAULT. Real execution requires a credentialed account,
deployment flag, temporary session approval, fresh public market quote and limits.
All execution requests use a durable UUID journal. A failed POST is NEVER retried.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_MARKET = re.compile(r"^[a-z0-9-]{5,140}$")
_ORDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_ORDER = Decimal("5")
MAX_SESSION = Decimal("15")
MAX_DAY = Decimal("30")
MAX_SLIPPAGE = Decimal("0.02")
_ARM_TIME = 600
_armed_until = 0.0
_session_spend = Decimal("0")
_lock = threading.RLock()


class USExchangeError(ValueError):
    """Sanitized user-facing error. Never include auth secret in message."""


def _client(key_id=None, secret_key=None):
    try:
        from polymarket_us import PolymarketUS
    except ImportError as exc:
        raise USExchangeError("Official polymarket-us SDK is not installed") from exc
    kw = {"timeout": 8.0, "max_retries": 0}
    if key_id and secret_key:
        kw.update(key_id=key_id, secret_key=secret_key)
    return PolymarketUS(**kw)


def _profile() -> Path:
    return Path(os.environ.get("KRYPT_POLYBOT_USERDATA") or Path.home() / ".oracle-us")


def _service() -> str:
    name = str(_profile().resolve()).casefold()
    return "ORACLE.PolymarketUS." + hashlib.sha256(name.encode()).hexdigest()[:20]


def _credentials() -> tuple[str, str] | None:
    import keyring
    key_id = keyring.get_password(_service(), "key_id")
    secret = keyring.get_password(_service(), "secret_key")
    return (key_id, secret) if key_id and secret else None


def _authenticated():
    pair = _credentials()
    if not pair:
        raise USExchangeError("No Polymarket US developer API keys connected")
    return _client(*pair)


def set_credentials(key_id: str, secret_key: str) -> dict:
    import keyring
    try:
        uuid.UUID(key_id)
    except (TypeError, ValueError) as exc:
        raise USExchangeError("The US API key ID must be a UUID") from exc
    if not isinstance(secret_key, str) or not 32 <= len(secret_key) <= 500 or any(c.isspace() for c in secret_key):
        raise USExchangeError("Invalid secret key format")
    c = _client(key_id, secret_key)
    try:
        c.account.balances()
    except Exception as exc:
        raise USExchangeError("US authentication failed; key not stored") from exc
    finally:
        c.close()
    with _lock:
        disarm()
        keyring.set_password(_service(), "key_id", key_id)
        keyring.set_password(_service(), "secret_key", secret_key)
    return {"connected": True, "keyIdPreview": key_id[:8] + "..."}


def clear_credentials() -> dict:
    import keyring
    with _lock:
        disarm()
        for name in ("key_id", "secret_key"):
            try:
                keyring.delete_password(_service(), name)
            except keyring.errors.PasswordDeleteError:
                pass
    return {"connected": False}


def status() -> dict:
    pair = _credentials()
    with _lock:
        armed = time.monotonic() < _armed_until
        return {
            "venue": "polymarket.us", "connected": bool(pair),
            "keyIdPreview": pair[0][:8] + "..." if pair else None,
            "liveConfigured": os.environ.get("ORACLE_US_ALLOW_LIVE") == "1",
            "armed": armed, "armedSecondsRemaining": max(0, int(_armed_until - time.monotonic())) if armed else 0,
            "maxOrderUsd": str(MAX_ORDER), "maxSessionUsd": str(MAX_SESSION),
            "maxDailyUsd": str(MAX_DAY), "sessionGrossUsd": str(_session_spend),
            "autoTrading": False, "mode": "LIVE_ARMED" if armed else "READ_ONLY",
        }


def disarm() -> dict:
    global _armed_until
    with _lock:
        _armed_until = 0.0
    return {"armed": False}


def arm(phrase: str) -> dict:
    global _armed_until
    if phrase != "I AUTHORIZE REAL MONEY US ORDERS":
        raise USExchangeError("Exact live session confirmation required")
    if os.environ.get("ORACLE_US_ALLOW_LIVE") != "1":
        raise USExchangeError("US live execution is disabled by deployment configuration")
    c = _authenticated()
    try:
        c.account.balances()
    except Exception as exc:
        raise USExchangeError("Account verification failed; not armed") from exc
    finally:
        c.close()
    with _lock:
        _armed_until = time.monotonic() + _ARM_TIME
    return status()


def _slug(value: Any) -> str:
    if not isinstance(value, str) or not _MARKET.fullmatch(value):
        raise USExchangeError("Invalid Polymarket US market slug")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise USExchangeError(name + " must be numeric")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise USExchangeError(name + " must be numeric") from exc
    if not n.is_finite() or n <= 0:
        raise USExchangeError(name + " must be positive and finite")
    return n


def markets(query: str = "bitcoin") -> dict:
    if not isinstance(query, str) or len(query) > 80:
        raise USExchangeError("Invalid search query")
    c = _client()
    try:
        return c.search.query({"query": query.strip() or "bitcoin"})
    finally:
        c.close()


def market(slug: str) -> dict:
    c = _client()
    try:
        return c.markets.retrieve_by_slug(_slug(slug))
    finally:
        c.close()


def book(slug: str) -> dict:
    c = _client()
    try:
        return c.markets.book(_slug(slug))
    finally:
        c.close()


def account() -> dict:
    c = _authenticated()
    try:
        return {"balances": c.account.balances(),
                "positions": c.portfolio.positions(),
                "orders": c.orders.list()}
    finally:
        c.close()


def activities() -> dict:
    c = _authenticated()
    try:
        return c.portfolio.activities()
    finally:
        c.close()


def _db():
    folder = _profile() / "data"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "oracle_us_orders.sqlite", timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS orders(
      request_id TEXT PRIMARY KEY, market_slug TEXT NOT NULL, intent TEXT NOT NULL,
      quantity INTEGER NOT NULL, price TEXT NOT NULL, gross TEXT NOT NULL,
      state TEXT NOT NULL, exchange_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    return conn


def journal() -> dict:
    with _db() as c:
        items = c.execute(
            "SELECT request_id,market_slug,intent,quantity,price,gross,state,exchange_id,created_at,updated_at "
            "FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
    fields = ("requestId","marketSlug","intent","quantity","price","grossUsd","state","orderId","createdAt","updatedAt")
    return {"orders": [dict(zip(fields, row)) for row in items]}


def _preflight(c, raw: dict) -> tuple[dict, dict]:
    if not isinstance(raw, dict):
        raise USExchangeError("Order payload must be an object")
    market_slug = _slug(raw.get("marketSlug"))
    intent = str(raw.get("intent") or "").upper()
    if intent not in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_LONG"):
        raise USExchangeError("Only BUY_LONG and SELL_LONG are supported")
    quantity = _decimal(raw.get("quantity"), "quantity")
    if quantity != quantity.to_integral_value() or quantity > 10000:
        raise USExchangeError("Quantity must be a whole number <= 10000")
    price = _decimal(raw.get("price"), "limit price")
    if price >= 1:
        raise USExchangeError("Limit price must be below 1.00 USD")
    gross = quantity * price
    if gross > MAX_ORDER:
        raise USExchangeError("Order exceeds the configured 5 USD pilot cap")
    raw_market = c.markets.retrieve_by_slug(market_slug).get("market") or {}
    if not raw_market or not raw_market.get("active") or raw_market.get("closed"):
        raise USExchangeError("US market is inactive or closed")
    if raw_market.get("status") not in (None, "MARKET_STATUS_OPEN"):
        raise USExchangeError("US market does not report OPEN status")
    snap = c.markets.bbo(market_slug).get("marketData") or {}
    if snap.get("state") != "MARKET_STATE_OPEN":
        raise USExchangeError("US live market data unavailable or market closed")
    field = "bestAsk" if intent == "ORDER_INTENT_BUY_LONG" else "bestBid"
    best_raw = (snap.get(field) or {}).get("value")
    best = _decimal(best_raw, field)
    if intent == "ORDER_INTENT_BUY_LONG" and price > best + MAX_SLIPPAGE:
        raise USExchangeError("Buy limit exceeds current ask by more than 2 cents")
    if intent == "ORDER_INTENT_SELL_LONG" and price < best - MAX_SLIPPAGE:
        raise USExchangeError("Sell limit is too far below the current bid")
    tick = raw_market.get("orderPriceMinTickSize")
    if tick and price % _decimal(tick, "tick size") != 0:
        raise USExchangeError("Limit price violates exchange tick size")
    if intent == "ORDER_INTENT_SELL_LONG":
        # Explicit inventory verification prevents naked/short exposure via this adapter.
        held = c.portfolio.positions({"market": market_slug}).get("positions") or {}
        available = Decimal(0)
        for key, position in held.items():
            meta = position.get("marketMetadata") or {}
            if meta.get("slug") == market_slug or key == market_slug:
                try:
                    available += _decimal(position.get("qtyAvailableDecimal") or
                                          position.get("qtyAvailable"), "available")
                except USExchangeError:
                    continue
        if available < quantity:
            raise USExchangeError("Insufficient confirmed long inventory for sell")
    params = {
        "marketSlug": market_slug, "intent": intent,
        "type": "ORDER_TYPE_LIMIT", "quantity": int(quantity),
        "price": {"value": str(price), "currency": "USD"},
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL",
    }
    snapshot = {
        "marketSlug": market_slug, "intent": intent,
        "price": str(price), "quantity": int(quantity), "grossUsd": str(gross),
        "bestQuote": str(best), "quoteReadAt": datetime.now(timezone.utc).isoformat(),
        "feeAdjustedProfitUnknown": True, "orderBookObserved": True,
    }
    return params, snapshot


def preview(raw: dict) -> dict:
    has_keys = _credentials() is not None
    c = _authenticated() if has_keys else _client()
    try:
        params, result = _preflight(c, raw)
        result["exchangeValidated"] = False
        if has_keys:
            # Official account-authenticated eligibility preview; never an order.
            try:
                result["venuePreview"] = c.orders.preview({"request": params})
                result["exchangeValidated"] = True
            except Exception as exc:
                raise USExchangeError("US exchange rejected order preview") from exc
        return result
    finally:
        c.close()


def place(raw: dict) -> dict:
    """Post once only, under a process+database lock; never auto-retry on errors."""
    global _session_spend
    if os.environ.get("ORACLE_US_ALLOW_LIVE") != "1":
        raise USExchangeError("Deployment has not allowed live US orders")
    with _lock:
        if time.monotonic() >= _armed_until:
            raise USExchangeError("Live session is disarmed or expired")
        if not isinstance(raw, dict):
            raise USExchangeError("Order request must be a dictionary")
        try:
            request_id = str(uuid.UUID(str(raw.get("requestId"))))
        except (TypeError, ValueError) as exc:
            raise USExchangeError("Live order requires a unique UUID requestId") from exc
        c = _authenticated()
        try:
            order, snapshot = _preflight(c, raw)
            # Account-specific compliance/availability check immediately before
            # reserving and submitting a real-money order.
            try:
                c.orders.preview({"request": order})
            except Exception as exc:
                raise USExchangeError("US exchange eligibility preview rejected order") from exc
            gross = Decimal(snapshot["grossUsd"])
            is_buy = order["intent"] == "ORDER_INTENT_BUY_LONG"
            if is_buy and _session_spend + gross > MAX_SESSION:
                raise USExchangeError("Session gross execution cap reached")
            with _db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT 1 FROM orders WHERE request_id=?", (request_id,)
                ).fetchone()
                if existing:
                    raise USExchangeError("Duplicate requestId; never resend this order")
                today = datetime.now(timezone.utc).date().isoformat()
                rows = conn.execute(
                    "SELECT gross FROM orders WHERE created_at>=? AND "
                    "intent='ORDER_INTENT_BUY_LONG'",
                    (today,)
                ).fetchall()
                day_spend = sum((Decimal(row[0]) for row in rows), Decimal(0))
                if is_buy and day_spend + gross > MAX_DAY:
                    raise USExchangeError("Daily gross execution cap reached")
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO orders(request_id,market_slug,intent,quantity,price,"
                    "gross,state,exchange_id,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (request_id, order["marketSlug"], order["intent"],
                     order["quantity"], order["price"]["value"], str(gross),
                     "pending", None, now, now)
                )
            if is_buy:
                _session_spend += gross
            try:
                result = c.orders.create(order)
            except Exception as exc:
                with _db() as conn:
                    conn.execute(
                        "UPDATE orders SET state='uncertain',updated_at=? WHERE request_id=?",
                        (datetime.now(timezone.utc).isoformat(), request_id)
                    )
                raise USExchangeError(
                    "Submission outcome uncertain or rejected; reconcile before taking further action"
                ) from exc
            order_id = str(result.get("id") or result.get("orderId") or "")
            with _db() as conn:
                conn.execute(
                    "UPDATE orders SET state='submitted',exchange_id=?,updated_at=? "
                    "WHERE request_id=?",
                    (order_id, datetime.now(timezone.utc).isoformat(), request_id)
                )
            return {
                "requestId": request_id, "orderId": order_id,
                "exchangeResponse": result, "submissionAccepted": True,
                "fillConfirmed": False,
                "notice": "Use account activity and order state to verify actual fills"
            }
        finally:
            c.close()


def cancel(order_id: str, market_slug: str) -> dict:
    # Cancels remain available when buying has been disarmed.
    if not isinstance(order_id, str) or not _ORDER_ID.fullmatch(order_id):
        raise USExchangeError("Invalid order ID")
    slug = _slug(market_slug)
    c = _authenticated()
    try:
        result = c.orders.cancel(order_id, {"marketSlug": slug})
        with _db() as conn:
            conn.execute(
                "UPDATE orders SET state='cancel-requested',updated_at=? "
                "WHERE exchange_id=? AND market_slug=?",
                (datetime.now(timezone.utc).isoformat(), order_id, slug)
            )
        return {"cancelRequested": True, "exchangeResponse": result,
                "fillConfirmed": False, "notice": "Check remote order state"}
    finally:
        c.close()


def reconcile() -> dict:
    c = _authenticated()
    try:
        order_list = c.orders.list()
        positions = c.portfolio.positions()
        activity = c.portfolio.activities()
        remote_details = []
        # Only an explicit exchange order detail can mark an order filled.
        with _db() as conn:
            rows = conn.execute(
                "SELECT request_id,exchange_id FROM orders "
                "WHERE exchange_id IS NOT NULL AND exchange_id != '' "
                "AND state NOT IN ('filled','canceled','rejected','expired') "
                "ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        state_map = {
            "ORDER_STATE_FILLED": "filled",
            "ORDER_STATE_PARTIALLY_FILLED": "partially-filled",
            "ORDER_STATE_CANCELED": "canceled",
            "ORDER_STATE_REJECTED": "rejected",
            "ORDER_STATE_EXPIRED": "expired",
            "ORDER_STATE_NEW": "submitted",
            "ORDER_STATE_PENDING_NEW": "submitted",
            "ORDER_STATE_PENDING_CANCEL": "cancel-requested",
            "ORDER_STATE_PENDING_REPLACE": "submitted",
            "ORDER_STATE_PENDING_RISK": "submitted",
        }
        for request_id, exchange_id in rows:
            try:
                detail = c.orders.retrieve(exchange_id).get("order") or {}
                remote_state = detail.get("state")
                if remote_state not in state_map:
                    continue
                state = state_map[remote_state]
                with _db() as conn:
                    conn.execute(
                        "UPDATE orders SET state=?,updated_at=? WHERE request_id=?",
                        (state, datetime.now(timezone.utc).isoformat(), request_id)
                    )
                remote_details.append({
                    "requestId": request_id, "exchangeId": exchange_id,
                    "venueState": remote_state, "recordedState": state,
                    "filledQuantity": detail.get("cumQuantity"),
                    "remainingQuantity": detail.get("leavesQuantity"),
                    "averageFillPrice": detail.get("avgPx"),
                    "commissionCollected": detail.get("commissionNotionalTotalCollected")
                })
            except Exception:
                # No evidence means unresolved; never manufacture an outcome.
                continue
        return {
            "remoteOrders": order_list,
            "positions": positions, "activities": activity,
            "reconciledOrders": remote_details,
            "localOrders": journal(),
            "notice": "Only verified exchange order states count as fills; unknown requests remain unresolved"
        }
    finally:
        c.close()
