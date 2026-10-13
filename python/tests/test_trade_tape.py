"""The REST trade tape: what the whale backstop asks for, and how old it says
its data is.

Measured 2026-10-11: /v2/trades is served through a CDN with max-age=300, so
a poll can read a page up to five minutes old, and an unfiltered 1,000-row
page holds only ~30-50 seconds of trading. The whale backstop therefore asks
for whale-sized rows only, and everything built from the tape carries the
trade's own time, so a late find is never traded or shown as a fresh one.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

import db
import polymarket_api as api
import scanner

CONDITION_ID = "0x" + "cd" * 32


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "tape.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile



def _capture_get(monkeypatch):
    seen: dict = {}

    async def _get(url, params=None):
        seen["params"] = dict(params or {})
        return {"data": [], "pagination": {"next_cursor": None}}

    monkeypatch.setattr(api, "_get", _get)
    return seen


def test_whale_sized_page_filters_on_shares(monkeypatch):
    """TOKENS, not CASH: a whale is sized at the price of the side the taker
    ends up long, so a SELL of a cheap outcome is a whale whose own cash leg
    is small (40,000 shares sold at 5c is $2,000 of cash and a $38,000 long).
    Shares bound every whale from above (dollars = shares x a price under $1),
    so a share floor at the dollar threshold drops no whale."""
    seen = _capture_get(monkeypatch)
    asyncio.run(api.fetch_recent_trades(limit=1000, min_size=2500))
    assert seen["params"]["filter_type"] == "TOKENS"
    assert seen["params"]["filter_amount"] == 2500


def test_unfiltered_page_by_default(monkeypatch):
    seen = _capture_get(monkeypatch)
    asyncio.run(api.fetch_recent_trades(limit=1000))
    assert "filter_type" not in seen["params"]
    assert "filter_amount" not in seen["params"]


def test_scan_whales_asks_for_whale_sized_rows(monkeypatch):
    seen: dict = {}

    async def _fetch(limit=1000, *, min_size=0.0):
        seen["min_size"] = min_size
        return []

    monkeypatch.setattr(api, "fetch_recent_trades", _fetch)
    asyncio.run(scanner.scan_whales({"min_whale_usd": 4000}))
    assert seen["min_size"] == 4000



def _whale(ts: int, tx: str) -> dict:
    return api.normalize_trade({
        "proxyWallet": "0x" + "aa" * 20, "side": "BUY", "asset": "tok-1",
        "conditionId": CONDITION_ID, "size": 10_000.0, "price": 0.70,
        "outcome": "Yes", "outcomeIndex": 0, "slug": "will-it-2026",
        "eventSlug": "it-2026", "title": "Will it?", "transactionHash": tx,
        "timestamp": ts,
    })


def _no_gamma(monkeypatch):
    async def _none(*_a, **_k):
        return {}
    monkeypatch.setattr(api, "fetch_markets_map", _none)

    async def _cat(*_a, **_k):
        return "politics"
    monkeypatch.setattr(scanner, "_resolve_category", _cat)


def test_a_late_found_whale_keeps_its_own_time(fresh_db, monkeypatch):
    """The backstop finds a whale the socket missed five minutes after it
    traded. Stamped with the time it was FOUND, it would read as fresh and be
    followed for the next 120s; stamped with its own time, it ages out."""
    _no_gamma(monkeypatch)
    now = int(time.time())
    cfg = {"min_whale_usd": 2500, "min_entry_price_frac": 0.5}
    _n, rows = asyncio.run(scanner.process_whale_trades(
        cfg, [_whale(now - 300, "0xold"), _whale(now - 5, "0xnew")]))
    assert len(rows) == 2
    with db.get_db() as conn:
        live = db.fetch_tradeable_whale_signals(
            conn, min_confidence=0.0, max_age_sec=120, seen_ids=set())
    assert [r["trade_id"].split("-")[0] for r in live] == ["0xnew"]
    stamped = {r["trade_id"].split("-")[0]: r["created_at"] for r in rows}
    assert stamped["0xold"] == datetime.fromtimestamp(
        now - 300, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_an_unreadable_trade_time_falls_back_to_now(fresh_db, monkeypatch):
    _no_gamma(monkeypatch)
    cfg = {"min_whale_usd": 2500, "min_entry_price_frac": 0.5}
    future = int(time.time()) + 3600
    _n, rows = asyncio.run(scanner.process_whale_trades(
        cfg, [_whale(0, "0xzero"), _whale(future, "0xfuture")]))
    with db.get_db() as conn:
        live = db.fetch_tradeable_whale_signals(
            conn, min_confidence=0.0, max_age_sec=120, seen_ids=set())
    assert len(rows) == 2 and len(live) == 2


def test_a_fast_pc_clock_does_not_age_a_fresh_whale(fresh_db, monkeypatch):
    """The age gate runs on this machine's clock and the trade time is the
    server's. With the PC ten minutes fast, a whale from five seconds ago must
    still be followable."""
    _no_gamma(monkeypatch)
    monkeypatch.setattr(api.auth, "_time_offset", -600.0)
    server_now = int(time.time()) - 600
    cfg = {"min_whale_usd": 2500, "min_entry_price_frac": 0.5}
    asyncio.run(scanner.process_whale_trades(cfg, [_whale(server_now - 5, "0xskew")]))
    with db.get_db() as conn:
        live = db.fetch_tradeable_whale_signals(
            conn, min_confidence=0.0, max_age_sec=120, seen_ids=set())
    assert len(live) == 1


def test_a_trade_cluster_from_a_stale_page_carries_the_page_time(fresh_db, monkeypatch):
    """A cluster read off a page the CDN cached four minutes ago is a cluster
    from four minutes ago: the alert says so, and the 120s age gate skips it."""
    now = int(time.time())
    with db.get_db() as conn:
        db.upsert_market(conn, {
            "ticker": CONDITION_ID, "title": "Will it?", "status": "open",
            "close_time": "2099-01-01T00:00:00Z", "volume": 5000.0,
            "volume_24h": 900.0, "open_interest": 100.0,
            "yes_bid": 0.30, "yes_ask": 0.30, "last_price": 0.30,
        })
    page = [
        dict(_whale(now - 240 - i, f"0xc{i}"), count_fp=200.0)
        for i in range(6)
    ]

    async def _page(limit=1000, *, min_size=0.0):
        return page

    monkeypatch.setattr(api, "fetch_recent_trades", _page)
    cfg = {"allowed_momentum_signal_types": ["trade_cluster"], "contrarian_only": False}
    _n, rows = asyncio.run(scanner.scan_momentum(cfg))
    assert [r["signal_type"] for r in rows] == ["trade_cluster"]
    assert rows[0]["created_at"] == datetime.fromtimestamp(
        now - 240, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_db() as conn:
        live = db.fetch_tradeable_momentum_signals(
            conn, min_confidence=0.0, max_age_sec=120,
            allowed_types=["trade_cluster"], seen_ids=set())
    assert live == []
