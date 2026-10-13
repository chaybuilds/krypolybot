"""A followed wallet with more open positions than one /positions walk reads.

get_positions stops at its page cap rather than hand back a partial list,
because the copy engine reads "not in their holdings" as "they sold it". These
pin what happens next: the wallet is named as too large to mirror (on the Copy
page, not just in a log line), nothing is decided off a partial read, it is not
re-walked 40 pages deep on every 5s tick, and its pushed BUYs are not copied
either, since its exits could never be followed.
"""
from __future__ import annotations

import asyncio

import pytest

import copy_trader
import db
import polymarket_api
import trader
from config import merge_with_defaults

BIG = "0x" + "b" * 40


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(copy_trader, "_wallet_pos_cache", {}, raising=False)
    monkeypatch.setattr(copy_trader, "_wallet_too_large", {}, raising=False)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "copy-big.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    db.init_db()
    return dbfile


def _cfg() -> dict:
    c = merge_with_defaults({"account_mode": "live"})
    c.update(network="mainnet", copy_enabled=True, copy_wallets=[BIG])
    return c


def _too_large(monkeypatch) -> dict:
    calls = {"n": 0}

    async def _gp(limit=500, user=None, **_kw):
        calls["n"] += 1
        raise polymarket_api.PositionsTooLargeError(
            f"positions fetch for {user}: /positions: more than 40 pages")

    monkeypatch.setattr(polymarket_api, "get_positions", _gp)
    return calls


def test_a_walk_past_the_page_cap_raises_too_large(monkeypatch):
    async def _page(_path, _params):
        return [{"conditionId": "0xc", "size": 1}], "more"

    monkeypatch.setattr(polymarket_api, "_data_v2_page", _page)
    with pytest.raises(polymarket_api.PositionsTooLargeError) as ei:
        asyncio.run(polymarket_api.get_positions(limit=500, user=BIG))
    assert BIG in str(ei.value)
    assert isinstance(ei.value, RuntimeError)


def test_too_large_is_never_a_holdings_list_and_is_not_rewalked(monkeypatch):
    calls = _too_large(monkeypatch)
    assert asyncio.run(copy_trader._read_wallet_positions(BIG)) == (None, False)
    assert asyncio.run(copy_trader._read_wallet_positions(BIG)) == (None, False)
    assert calls["n"] == 1


def test_no_exit_is_decided_while_a_too_large_wallet_is_followed(fresh_db, monkeypatch):
    _too_large(monkeypatch)
    with db.get_db() as conn:
        db.insert_bot_position(conn, {
            "signal_source": "copy", "signal_id": 1, "ticker": "T-A",
            "direction": "yes", "target_contracts": 20, "limit_price_cents": 50,
            "filled_contracts": 20, "cost_usd": 10.0, "client_order_id": "copy-T-A",
            "status": "filled", "network": "mainnet",
        })
    sent = []

    async def _place(**kw):
        sent.append(kw)
        return {"order": {"order_id": "x", "status": "matched"}}

    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    asyncio.run(copy_trader.run_tick(_cfg(), authed=True))
    assert sent == []
    with db.get_db() as conn:
        row = conn.execute("SELECT resolved FROM bot_positions").fetchone()
    assert row[0] == 0


def test_the_copy_page_says_the_wallet_is_too_large(fresh_db, monkeypatch):
    calls = _too_large(monkeypatch)
    asyncio.run(copy_trader._read_wallet_positions(BIG))
    st = asyncio.run(copy_trader.status(_cfg(), authed=True))
    (w,) = st["wallets"]
    assert w["error"].startswith("Too large to mirror")
    assert "paused" in w["error"]
    assert calls["n"] == 1


def test_pushed_buys_from_a_too_large_wallet_are_not_copied(monkeypatch):
    _too_large(monkeypatch)
    asyncio.run(copy_trader._read_wallet_positions(BIG))
    copy_trader._remember_push_ctx(_cfg(), True)
    assert BIG not in copy_trader._push_ctx["wallets"]


def test_a_wallet_that_shrinks_is_mirrored_again(monkeypatch):
    calls = _too_large(monkeypatch)
    asyncio.run(copy_trader._read_wallet_positions(BIG))
    copy_trader._wallet_too_large[BIG] -= copy_trader._TOO_LARGE_RECHECK_SEC + 1

    async def _gp(limit=500, user=None, **_kw):
        calls["n"] += 1
        return []

    monkeypatch.setattr(polymarket_api, "get_positions", _gp)
    assert asyncio.run(copy_trader._read_wallet_positions(BIG)) == ([], True)
    assert BIG not in copy_trader._wallet_too_large
