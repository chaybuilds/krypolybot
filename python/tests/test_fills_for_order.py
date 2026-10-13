"""`polymarket_api.get_fills_for_order`: the fills of ONE of our orders, as
OUR leg of each match.

`/data/trades` reports every match from the TAKER's side. When our order
rested and was hit, its leg is our entry in `maker_orders` — its own size
(`matched_amount`), its own price, possibly the other token and the other
direction. The old reader matched maker entries with `str(dict)` (never
equal to an order id, so maker fills were invisible), booked the taker's
price and size, called everything a buy, and read only the newest page of
the whole account's history. No network: `_authed_request` is stubbed.
"""
from __future__ import annotations

import asyncio

import pytest

import crypto15m_trader
import polymarket_api as api

OURS = "0xour-order"
CID = "0xcondition"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _stub_trades(monkeypatch, pages, calls):
    """`pages` is a list of (rows, next_cursor), served in cursor order."""
    async def _authed(method, path, *, params=None, **_kw):
        assert (method, path) == ("GET", "/data/trades")
        calls.append(dict(params or {}))
        i = len(calls) - 1
        if isinstance(pages[i], Exception):
            raise pages[i]
        rows, nxt = pages[i]
        return {"data": rows, "next_cursor": nxt}
    monkeypatch.setattr(api, "_authed_request", _authed)


def _maker_trade(**over):
    """Someone's taker BUY of 10 NO at 70c filled against our resting BUY of
    4 YES at 30c (a cross-token match) plus another maker's order."""
    t = {
        "id": "t-maker", "market": CID, "asset_id": "tokNO", "side": "BUY",
        "price": "0.70", "size": "10", "status": "CONFIRMED",
        "taker_order_id": "0xsomeone-else", "trader_side": "MAKER",
        "maker_orders": [
            {"order_id": "0xother-maker", "asset_id": "tokNO", "side": "SELL",
             "price": "0.70", "matched_amount": "6", "maker_address": "0xother"},
            {"order_id": OURS, "asset_id": "tokYES", "side": "BUY",
             "price": "0.30", "matched_amount": "4", "maker_address": "0xus"},
        ],
    }
    t.update(over)
    return t


def _taker_trade(**over):
    """Our marketable SELL of 5 YES at 62c, taken against one resting bid."""
    t = {
        "id": "t-taker", "market": CID, "asset_id": "tokYES", "side": "SELL",
        "price": "0.62", "size": "5", "status": "MATCHED",
        "taker_order_id": OURS, "trader_side": "TAKER",
        "maker_orders": [{"order_id": "0xm", "asset_id": "tokYES", "side": "BUY",
                          "price": "0.62", "matched_amount": "5"}],
    }
    t.update(over)
    return t


def test_a_maker_fill_is_found_and_booked_at_our_own_size_and_price(monkeypatch):
    calls: list = []
    _stub_trades(monkeypatch, [([_maker_trade()], "LTE=")], calls)
    fills = _run(api.get_fills_for_order(OURS))
    assert len(fills) == 1, fills
    f = fills[0]
    assert f["order_id"] == OURS
    assert f["count_fp"] == 4.0
    assert f["price_cents"] == 30
    assert f["action"] == "buy"
    assert f["role"] == "maker"
    assert f["asset_id"] == "tokYES"


def test_a_taker_fill_uses_the_top_level_leg_and_its_real_direction(monkeypatch):
    calls: list = []
    _stub_trades(monkeypatch, [([_taker_trade()], "LTE=")], calls)
    [f] = _run(api.get_fills_for_order(OURS))
    assert (f["count_fp"], f["price_cents"], f["action"], f["role"]) == (5.0, 62, "sell", "taker")


def test_the_walk_follows_the_cursor_and_filters_by_market(monkeypatch):
    calls: list = []
    other = _maker_trade(id="t-x", maker_orders=[], taker_order_id="0xnot-ours")
    _stub_trades(monkeypatch, [
        ([other], "Y3Vyc29yMQ=="),
        ([_maker_trade()], "LTE="),
    ], calls)
    fills = _run(api.get_fills_for_order(OURS, market=CID, after=1_760_000_000))
    assert [f["count_fp"] for f in fills] == [4.0]
    assert len(calls) == 2
    assert calls[0] == {"market": CID, "after": 1_760_000_000}
    assert calls[1] == {"market": CID, "after": 1_760_000_000, "next_cursor": "Y3Vyc29yMQ=="}


def test_a_failed_trade_settled_nothing_and_is_not_a_fill(monkeypatch):
    calls: list = []
    _stub_trades(monkeypatch, [([_taker_trade(status="TRADE_STATUS_FAILED"),
                                  _maker_trade(status="FAILED")], "LTE=")], calls)
    assert _run(api.get_fills_for_order(OURS)) == []


def test_a_page_that_fails_mid_walk_returns_nothing_not_a_short_list(monkeypatch):
    calls: list = []
    _stub_trades(monkeypatch, [
        ([_taker_trade()], "c1"),
        api.PolymarketAPIError(500, "boom"),
    ], calls)
    assert _run(api.get_fills_for_order(OURS)) == []


def test_a_walk_that_never_ends_returns_nothing(monkeypatch):
    calls: list = []
    pages = [([_taker_trade(id=f"t{i}")], f"c{i}") for i in range(500)]
    _stub_trades(monkeypatch, pages, calls)
    assert _run(api.get_fills_for_order(OURS)) == []
    assert len(calls) < 500


def test_the_crypto_engine_asks_for_its_own_market_only(monkeypatch):
    seen = {}

    async def _fills(order_id, **kw):
        seen.update(kw, order_id=order_id)
        return [{"order_id": order_id, "action": "buy", "count_fp": 4.0,
                 "price_cents": 30, "side": ""}]
    monkeypatch.setattr(api, "get_fills_for_order", _fills)
    pos = {"ticker": CID, "created_at": "2026-10-11 12:00:00"}
    avg, cost = _run(crypto15m_trader._real_fill_cents(OURS, 31.0, 4, pos=pos))
    assert (avg, cost) == (30.0, 1.2)
    assert seen["market"] == CID
    assert seen["after"] == 1_791_720_000 - 3600
