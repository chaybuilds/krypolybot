"""Live-book (websocket) quotes on the order paths — the strict-accelerator
contract.

Both engines used to pay a REST quote immediately before `place_limit_order`:
a cached market-meta lookup plus TWO CLOB /price round-trips, for a number
`clob_ws.feed` already holds and stamped when we received it. These tests pin
the contract that lets us skip it:

  * a FRESH, two-sided, sentinel-free book is used and REST is not called;
  * anything less — stale, one-sided, an empty-book sentinel, an unwarmed
    market, the kill switch — falls back to the REST quote, unchanged;
  * `warm_books` is what turns the order-path read into pure dict work (no
    await, no network) by resolving the outcome token once.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import clob_ws
import copy_trader
import db
import polymarket_api
import trader
from config import merge_with_defaults


def run_async(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_feed(monkeypatch):
    """A private feed + token map per test — the module singletons are process
    wide and a leaked book would make another test's REST fallback disappear."""
    feed = clob_ws.ClobMarketFeed()
    monkeypatch.setattr(feed, "start", lambda: None)
    monkeypatch.setattr(clob_ws, "feed", feed)
    monkeypatch.setattr(trader, "_ws_token", {}, raising=False)
    monkeypatch.setattr(trader, "_WS_QUOTES_OFF", False, raising=False)
    return feed


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "wsq-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _warm(token="TOK", ticker="T-A", side="yes"):
    """Stand in for a completed `warm_books` pass."""
    trader._ws_token[(ticker, side)] = token


def _rest_spy(monkeypatch, bid=10, ask=11):
    """Count REST quotes. Returns a book deliberately unlike any ws book in
    these tests, so the assertion says WHICH source priced the order."""
    calls = {"n": 0}

    async def _q(_ticker, _side):
        calls["n"] += 1
        return {"bid_cents": bid, "ask_cents": ask}

    monkeypatch.setattr(polymarket_api, "get_quote", _q)
    monkeypatch.setattr(trader, "get_quote", _q, raising=False)
    return calls



def test_ws_quote_cents_reads_the_warmed_token(_clean_feed):
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    assert trader.ws_quote_cents("T-A", "yes") == {"bid_cents": 40, "ask_cents": 41}


def test_ws_quote_cents_none_for_unwarmed_market(_clean_feed):
    _clean_feed._set("TOK", 0.40, 0.41)
    assert trader.ws_quote_cents("T-A", "yes") is None


def test_ws_quote_cents_none_when_stale_or_sentinel(_clean_feed):
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    _clean_feed._books["TOK"]["ts"] = time.monotonic() - 100.0
    assert trader.ws_quote_cents("T-A", "yes") is None
    _clean_feed._set("TOK", 0.0, 1.0)
    assert trader.ws_quote_cents("T-A", "yes") is None


def test_ws_quote_cents_kill_switch(monkeypatch, _clean_feed):
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    monkeypatch.setattr(trader, "_WS_QUOTES_OFF", True)
    assert trader.ws_quote_cents("T-A", "yes") is None


def test_warm_books_resolves_the_token_once(monkeypatch, _clean_feed):
    fetches = {"n": 0}

    async def _meta(ticker):
        fetches["n"] += 1
        return {"yes_token": f"Y-{ticker}", "no_token": f"N-{ticker}"}

    monkeypatch.setattr(trader, "get_market_meta", _meta)
    assert run_async(trader.warm_books([("T-A", "yes"), ("T-B", "no")])) == 2
    assert fetches["n"] == 2
    assert trader._ws_token[("T-A", "yes")] == "Y-T-A"
    assert trader._ws_token[("T-B", "no")] == "N-T-B"
    assert run_async(trader.warm_books([("T-A", "yes"), ("T-B", "no")])) == 2
    assert fetches["n"] == 2
    assert set(_clean_feed._tokens) == {"Y-T-A", "N-T-B"}


def test_warm_books_bounds_new_meta_fetches(monkeypatch, _clean_feed):
    fetches = {"n": 0}

    async def _meta(ticker):
        fetches["n"] += 1
        return {"yes_token": f"Y-{ticker}", "no_token": f"N-{ticker}"}

    monkeypatch.setattr(trader, "get_market_meta", _meta)
    run_async(trader.warm_books([(f"T{i}", "yes") for i in range(50)]))
    assert fetches["n"] == trader._WS_WARM_FETCH_MAX


def test_warm_books_survives_a_failing_meta(monkeypatch, _clean_feed):
    async def _boom(_ticker):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(trader, "get_market_meta", _boom)
    assert run_async(trader.warm_books([("T-A", "yes")])) == 0



def test_main_engine_prices_off_the_feed_without_a_rest_quote(monkeypatch, _clean_feed):
    rest = _rest_spy(monkeypatch, bid=10, ask=11)
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    cfg = merge_with_defaults({})
    cfg.update({"order_style": "limit_cross", "max_entry_price_cents": 95,
                "cross_spread_fallback_offset": 2})
    px = run_async(trader._compute_limit_price_cents("T-A", "yes", 40, cfg))
    assert px == 43
    assert rest["n"] == 0


def test_main_engine_falls_back_to_rest_when_the_book_is_stale(monkeypatch, _clean_feed):
    rest = _rest_spy(monkeypatch, bid=10, ask=11)
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    _clean_feed._books["TOK"]["ts"] = time.monotonic() - 100.0
    cfg = merge_with_defaults({})
    cfg.update({"order_style": "limit_cross", "max_entry_price_cents": 95,
                "cross_spread_fallback_offset": 2})
    px = run_async(trader._compute_limit_price_cents("T-A", "yes", 40, cfg))
    assert px == 13
    assert rest["n"] == 1


def test_main_engine_falls_back_to_rest_on_an_empty_book(monkeypatch, _clean_feed):
    rest = _rest_spy(monkeypatch, bid=10, ask=11)
    _warm()
    _clean_feed._set("TOK", 0.0, 1.0)
    cfg = merge_with_defaults({})
    cfg.update({"order_style": "limit_cross", "max_entry_price_cents": 95,
                "cross_spread_fallback_offset": 2})
    px = run_async(trader._compute_limit_price_cents("T-A", "yes", 40, cfg))
    assert px == 13
    assert rest["n"] == 1


def test_feed_price_still_obeys_the_max_entry_cap(monkeypatch, _clean_feed):
    """The websocket changes WHERE the number comes from, never which numbers
    are allowed: the user's cap still bounds the order price."""
    _rest_spy(monkeypatch)
    _warm()
    _clean_feed._set("TOK", 0.80, 0.81)
    cfg = merge_with_defaults({})
    cfg.update({"order_style": "limit_cross", "max_entry_price_cents": 60,
                "cross_spread_fallback_offset": 2})
    px = run_async(trader._compute_limit_price_cents("T-A", "yes", 80, cfg))
    assert px == 81



def _copy_cfg(**over):
    c = merge_with_defaults({})
    c.update({"network": "mainnet", "copy_enabled": True,
              "copy_wallets": ["0x" + "a" * 40], "copy_sizing_mode": "fixed",
              "copy_fixed_usd": 10.0, "copy_min_trade_usd": 25.0,
              "start_bankroll_usd": 100.0, "copy_only_new_entries": False,
              "copy_entry_max_cents": 95})
    c.update(over)
    return c


def _patch_place(monkeypatch):
    calls = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "ord-1"}}

    async def _market(ticker):
        return {"ticker": ticker, "category": "politics"}

    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(polymarket_api, "fetch_market", _market)
    return calls


def test_copy_entry_prices_off_the_feed_without_a_rest_quote(
    fresh_db, monkeypatch, _clean_feed,
):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    rest = _rest_spy(monkeypatch, bid=10, ask=11)
    place = _patch_place(monkeypatch)
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    h = {"ticker": "T-A", "side": "yes", "price_cents": 50, "qty": 10.0,
         "title": "T-A", "event_ticker": "", "wallets": ["0x" + "a" * 40]}
    row = run_async(copy_trader._enter_copy(h, _copy_cfg(), "mainnet", 100.0, 100.0))
    assert row["limit_price_cents"] == 41
    assert rest["n"] == 0
    assert len(place) == 1


def test_copy_entry_falls_back_to_rest_when_stale(fresh_db, monkeypatch, _clean_feed):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    rest = _rest_spy(monkeypatch, bid=10, ask=11)
    _patch_place(monkeypatch)
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    _clean_feed._books["TOK"]["ts"] = time.monotonic() - 100.0
    h = {"ticker": "T-A", "side": "yes", "price_cents": 50, "qty": 10.0,
         "title": "T-A", "event_ticker": "", "wallets": ["0x" + "a" * 40]}
    row = run_async(copy_trader._enter_copy(h, _copy_cfg(), "mainnet", 100.0, 100.0))
    assert row["limit_price_cents"] == 11
    assert rest["n"] == 1


def test_copy_entry_band_still_applies_to_a_feed_price(
    fresh_db, monkeypatch, _clean_feed,
):
    """Nothing enters on websocket data that would not have entered on REST
    data — the band rejects an 81c live ask under a 60c cap exactly as it
    rejects an 81c REST ask."""
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    _rest_spy(monkeypatch, bid=10, ask=11)
    place = _patch_place(monkeypatch)
    _warm()
    _clean_feed._set("TOK", 0.80, 0.81)
    h = {"ticker": "T-A", "side": "yes", "price_cents": 50, "qty": 10.0,
         "title": "T-A", "event_ticker": "", "wallets": ["0x" + "a" * 40]}
    row = run_async(copy_trader._enter_copy(
        h, _copy_cfg(copy_entry_max_cents=60), "mainnet", 100.0, 100.0))
    assert row is None and place == []


def test_copy_exit_sells_at_the_feed_bid_without_a_rest_quote(
    fresh_db, monkeypatch, _clean_feed,
):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    rest = _rest_spy(monkeypatch, bid=10, ask=11)
    place = _patch_place(monkeypatch)

    async def _confirm(_oid, qty, px):
        return qty, float(px)

    monkeypatch.setattr(trader, "_confirm_sell", _confirm)
    _warm()
    _clean_feed._set("TOK", 0.40, 0.41)
    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, {
            "signal_source": "copy", "signal_id": 1, "ticker": "T-A",
            "direction": "yes", "target_contracts": 20, "limit_price_cents": 50,
            "filled_contracts": 20, "cost_usd": 10.0, "client_order_id": "c-1",
            "status": "filled", "network": "mainnet",
        })
        pos = db.fetch_position_by_id(conn, pid)
    run_async(copy_trader._exit_copy(
        pos, _copy_cfg(), {("T-A", "yes"): 20.0}, True))
    assert len(place) == 1
    assert place[0]["price_cents"] == 40
    assert rest["n"] == 0
