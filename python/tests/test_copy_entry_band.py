"""The copy-trading entry-price band.

A max alone let the engine follow a whale into a 3c lottery ticket, which is a
different bet from following them into a 60c conviction trade — and the loss
rate on the cheap end is what quietly bleeds a copy book.
"""
from __future__ import annotations

import asyncio

import pytest

import config
import copy_trader


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cfg(**over):
    c = config.merge_with_defaults(dict(over))
    return c


def _holding(price_cents):
    return {"ticker": "0xabc", "side": "yes", "price_cents": price_cents,
            "wallets": ["0x1111111111111111111111111111111111111111"]}


def _wire(monkeypatch, ask_cents):
    placed = []

    async def _quote(ticker, side):
        return {"ask_cents": ask_cents, "bid_cents": ask_cents - 1}

    async def _place(**kw):
        placed.append(kw)
        return {"order": {"order_id": "OID", "status": "live"}}

    async def _market(_ticker):
        return {"ticker": _ticker, "category": "sports"}

    monkeypatch.setattr(copy_trader.polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(copy_trader.polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(copy_trader.polymarket_api, "fetch_market", _market)
    monkeypatch.setattr(copy_trader, "_category_cache", {}, raising=False)
    return placed



def test_a_copy_below_the_floor_is_skipped(monkeypatch):
    placed = _wire(monkeypatch, ask_cents=3)
    r = _run(copy_trader._enter_copy(
        _holding(3), _cfg(copy_entry_min_cents=20), "mainnet", 1000.0))
    assert r is None
    assert placed == [], "a 3c copy was placed under a 20c floor"


def test_a_copy_at_the_floor_is_allowed(monkeypatch):
    placed = _wire(monkeypatch, ask_cents=20)
    _run(copy_trader._enter_copy(
        _holding(20), _cfg(copy_entry_min_cents=20), "mainnet", 1000.0))
    assert placed, "a copy exactly at the floor was rejected"


def test_zero_means_no_floor(monkeypatch):
    placed = _wire(monkeypatch, ask_cents=2)
    _run(copy_trader._enter_copy(
        _holding(2), _cfg(copy_entry_min_cents=0), "mainnet", 1000.0))
    assert placed, "the floor applied while switched off"


def test_the_ceiling_still_applies(monkeypatch):
    placed = _wire(monkeypatch, ask_cents=97)
    r = _run(copy_trader._enter_copy(
        _holding(97), _cfg(copy_entry_min_cents=10, copy_entry_max_cents=95),
        "mainnet", 1000.0))
    assert r is None and placed == []


def test_a_price_inside_the_band_is_copied(monkeypatch):
    placed = _wire(monkeypatch, ask_cents=60)
    _run(copy_trader._enter_copy(
        _holding(60), _cfg(copy_entry_min_cents=10, copy_entry_max_cents=95),
        "mainnet", 1000.0))
    assert placed and placed[0]["price_cents"] == 60



def test_the_default_is_off():
    assert _cfg()["copy_entry_min_cents"] == 0


def test_a_floor_above_the_ceiling_is_clamped_not_obeyed():
    """Obeying it literally would reject EVERY copy while the UI still showed
    the engine as armed — a silent no-op is worse than a clamped setting."""
    c = _cfg(copy_entry_min_cents=99, copy_entry_max_cents=50)
    assert c["copy_entry_min_cents"] < c["copy_entry_max_cents"]
    assert c["copy_entry_min_cents"] == 49


def test_the_floor_is_clamped_into_range():
    assert _cfg(copy_entry_min_cents=-5)["copy_entry_min_cents"] == 0
    assert _cfg(copy_entry_min_cents=500)["copy_entry_min_cents"] <= 99


def test_the_status_reports_both_ends():
    import inspect
    src = inspect.getsource(copy_trader)
    assert '"entryMinCents"' in src and '"entryMaxCents"' in src
