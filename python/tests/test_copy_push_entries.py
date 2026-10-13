"""Copy entries built from the activity-websocket payload.

The trade tape delivers a followed wallet's fill ~1s after it executes; the
Data API needs several more seconds to reflect it. The tape used to be a
WAKEUP only — the tick it triggered still re-read /positions — so a real copy
entry landed 5-12s behind the whale. `copy_trader.on_activity_trade` now builds
the entry from the frame and `drain_pushed_entries` runs it through
`_entry_pass`, the SAME pipeline the poll uses.

What these tests pin is the safety half of that: the push may only make an
entry the poll would have made anyway, sooner. So a frame that is not a BUY by
a followed wallet at a real size, or that we have already acted on, is dropped;
every entry gate still runs; and a pushed entry followed by the poll seeing the
same position never opens a second one.
"""
from __future__ import annotations

import asyncio

import pytest

import copy_trader
import db
import polymarket_api
import trader
from config import merge_with_defaults

WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
COND = "0xcond-1"


def run_async(coro):
    return asyncio.run(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "copy-push.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


@pytest.fixture
def env_net(monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    return "mainnet"


@pytest.fixture(autouse=True)
def _reset_push_state(monkeypatch):
    copy_trader._pushed.clear()
    copy_trader._pushed_ids.clear()
    copy_trader._pushed_changed.clear()
    copy_trader._copy_cooldown.clear()
    copy_trader._exit_retry_at.clear()
    copy_trader._category_cache.clear()
    copy_trader._push_ctx.update({"on": False, "wallets": set(), "min_usd": 0.0})
    copy_trader._own_addr_cache = (0.0, set())
    async def _warm(_pairs, **_kw):
        return 0
    monkeypatch.setattr(trader, "warm_books", _warm)
    yield
    copy_trader._pushed.clear()
    copy_trader._pushed_ids.clear()
    copy_trader._pushed_changed.clear()


def _cfg(**over) -> dict:
    c = merge_with_defaults({"account_mode": "live"})
    c.update({
        "network": "mainnet", "copy_enabled": True, "copy_wallets": [WALLET],
        "copy_activity_ws": True, "copy_sizing_mode": "fixed",
        "copy_fixed_usd": 10.0, "copy_min_trade_usd": 25.0,
        "start_bankroll_usd": 100.0,
        "copy_only_new_entries": False,
    })
    c.update(over)
    return c


def _frame(**over) -> dict:
    """One `activity_ws.drain_hits()` hit: a 100-share BUY of outcome 0 at 50c."""
    f = {"wallet": WALLET, "side": "BUY", "size": 100.0, "price": 0.5,
         "conditionId": COND, "outcomeIndex": 0, "outcome": "Yes",
         "slug": "who-wins", "eventSlug": "who-wins", "title": "Who wins?",
         "transactionHash": "0xtx-1", "timestamp": 1700000000, "at": 0.0}
    f.update(over)
    return f


def _patch_live(monkeypatch, ask=50):
    calls = {"place": []}

    async def _place(**kw):
        calls["place"].append(kw)
        return {"order": {"order_id": f"ord-{len(calls['place'])}"}}

    async def _quote(_t, _s):
        return {"ask_cents": ask, "bid_cents": ask - 2}

    async def _market(ticker):
        return {"ticker": ticker, "category": "politics"}

    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "fetch_market", _market)
    return calls


def _arm(cfg):
    """What every copy tick does before the socket callback can fire."""
    copy_trader._remember_push_ctx(cfg, True)


def _copies():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM bot_positions WHERE signal_source='copy'"
        ).fetchall()]



def test_pushed_buy_opens_exactly_one_position(fresh_db, env_net, monkeypatch):
    cfg = _cfg()
    _arm(cfg)
    calls = _patch_live(monkeypatch, ask=50)
    copy_trader.on_activity_trade(_frame())
    assert copy_trader.pending_pushes() == 1
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 1
    rows = _copies()
    assert len(rows) == 1
    r = rows[0]
    assert (r["ticker"], r["direction"]) == (COND, "yes")
    assert r["status"] == "submitted"
    assert r["target_contracts"] == 20
    assert r["title"] == "Who wins?" and r["event_ticker"] == "who-wins"
    assert len(calls["place"]) == 1 and calls["place"][0]["action"] == "buy"
    assert copy_trader.pending_pushes() == 0


def test_raw_tape_payload_is_accepted(fresh_db, env_net, monkeypatch):
    """service.py hands the frame over RAW, straight off the socket, so the
    tape's own field names have to work as well as the drain_hits shape."""
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade({
        "proxyWallet": WALLET.upper(), "side": "BUY", "size": 100.0,
        "price": 0.5, "conditionId": COND, "outcomeIndex": 0, "outcome": "Yes",
        "eventSlug": "who-wins", "name": "Who wins?",
        "transactionHash": "0xtx-raw", "timestamp": 1700000000,
    })
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 1
    rows = _copies()
    assert len(rows) == 1 and rows[0]["ticker"] == COND


def test_outcome_index_one_is_the_no_side(fresh_db, env_net, monkeypatch):
    """Side comes from outcomeIndex, the same field /positions derives its own
    side from — never from the `outcome` label, which is per-market prose."""
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame(outcomeIndex=1, outcome="Down"))
    run_async(copy_trader.drain_pushed_entries(cfg))
    assert [r["direction"] for r in _copies()] == ["no"]


def test_pushed_rows_ride_out_on_the_next_run_tick(fresh_db, env_net, monkeypatch):
    """The service emits position events from run_tick's return value; a pushed
    entry has to appear there or the UI never learns about it."""
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)

    async def _positions(limit=500, user=None, **kw):
        return [_their_pos(COND)] if user else []

    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    copy_trader.on_activity_trade(_frame())
    run_async(copy_trader.drain_pushed_entries(cfg))
    changed = run_async(copy_trader.run_tick(cfg, authed=True))
    assert [r["ticker"] for r in changed] == [COND]
    assert copy_trader._pushed_changed == []



def test_unfollowed_wallet_is_ignored(fresh_db, env_net, monkeypatch):
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame(wallet=OTHER))
    assert copy_trader.pending_pushes() == 0
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert _copies() == []


def test_sell_is_ignored(fresh_db, env_net, monkeypatch):
    """A SELL is an exit: it needs OUR position, not their print, so the exit
    pass owns it — on the very tick this frame accelerates."""
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame(side="SELL"))
    assert copy_trader.pending_pushes() == 0
    assert _copies() == []


def test_sub_minimum_size_is_ignored(fresh_db, env_net, monkeypatch):
    """copy_min_trade_usd is $25; a 10-share buy at 50c is $5 of their dust."""
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame(size=10.0))
    assert copy_trader.pending_pushes() == 0
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert _copies() == []


def test_duplicate_trade_id_opens_one_position(fresh_db, env_net, monkeypatch):
    """The tape re-delivers on reconnect and replays history bundles."""
    cfg = _cfg()
    _arm(cfg)
    calls = _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame())
    copy_trader.on_activity_trade(_frame())
    assert copy_trader.pending_pushes() == 1
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 1
    assert len(_copies()) == 1 and len(calls["place"]) == 1
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert len(_copies()) == 1


def test_duplicate_without_a_hash_still_dedupes(fresh_db, env_net, monkeypatch):
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame(transactionHash=""))
    copy_trader.on_activity_trade(_frame(transactionHash=""))
    assert len(copy_trader._pushed_ids) == 1


@pytest.mark.parametrize("bad", [
    None, "not a dict", 42, {},
    {"wallet": WALLET, "side": "BUY"},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND,
     "outcomeIndex": 0},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND,
     "outcomeIndex": 7, "size": 100, "price": 0.5},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND,
     "outcomeIndex": "yes", "size": 100, "price": 0.5},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND,
     "outcomeIndex": 0, "size": "lots", "price": 0.5},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND,
     "outcomeIndex": 0, "size": -100, "price": 0.5},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND,
     "outcomeIndex": 0, "size": 100, "price": 0.0},
    {"wallet": WALLET, "side": "BUY", "conditionId": COND,
     "outcomeIndex": 0, "size": 100, "price": 3.5},
])
def test_garbled_frames_are_dropped_silently(fresh_db, env_net, bad):
    _arm(_cfg())
    copy_trader.on_activity_trade(bad)
    assert copy_trader.pending_pushes() == 0


def test_push_is_inert_until_a_tick_arms_it(fresh_db, env_net):
    """`on_activity_trade` is cfg-less (it runs on the socket's read loop). It
    stays closed until a copy tick has told it what we follow."""
    copy_trader.on_activity_trade(_frame())
    assert copy_trader.pending_pushes() == 0


def test_push_disabled_by_config_drops_the_queue(fresh_db, env_net, monkeypatch):
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame())
    assert copy_trader.pending_pushes() == 1
    off = _cfg(copy_activity_ws=False)
    assert run_async(copy_trader.drain_pushed_entries(off)) == 0
    assert _copies() == [] and copy_trader.pending_pushes() == 0


def test_unauthed_drain_places_nothing(fresh_db, env_net, monkeypatch):
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg, authed=False)) == 0
    assert _copies() == []


def test_stale_queued_push_is_not_entered(fresh_db, env_net, monkeypatch):
    """Past the stale window the poll has had several chances at it; entering
    on a minutes-old print is chasing, not copying."""
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame())
    copy_trader._pushed[(COND, "yes")]["at"] -= (copy_trader._PUSHED_STALE_SEC + 5)
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert _copies() == []



def test_entry_band_rejects_a_pushed_buy_above_the_cap(fresh_db, env_net, monkeypatch):
    cfg = _cfg(copy_entry_max_cents=60)
    _arm(cfg)
    calls = _patch_live(monkeypatch, ask=80)
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert calls["place"] == []


def test_only_new_entries_baselines_an_unseen_wallet(fresh_db, env_net, monkeypatch):
    """Same rule as the poll: a wallet we have not baselined yet is RECORDED,
    not copied — otherwise following a wallet back-fills its run-up positions."""
    cfg = _cfg(copy_only_new_entries=True)
    _arm(cfg)
    _patch_live(monkeypatch)
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert _copies() == []
    assert f"{WALLET}|{COND}|yes" in copy_trader._load_seen("mainnet")


def test_concurrency_cap_applies_to_pushed_entries(fresh_db, env_net, monkeypatch):
    cfg = _cfg(copy_max_concurrent=1)
    _arm(cfg)
    _patch_live(monkeypatch)
    with db.get_db() as conn:
        db.insert_bot_position(conn, {
            "signal_source": "copy", "signal_id": 9, "ticker": "0xother",
            "direction": "yes", "target_contracts": 5, "limit_price_cents": 50,
            "filled_contracts": 5, "cost_usd": 2.5, "client_order_id": "c-9",
            "status": "filled", "network": "mainnet",
        })
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert len(_copies()) == 1


def test_market_another_engine_holds_is_off_limits(fresh_db, env_net, monkeypatch):
    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    with db.get_db() as conn:
        db.insert_bot_position(conn, {
            "signal_source": "whale", "signal_id": 11, "ticker": COND,
            "direction": "yes", "target_contracts": 5, "limit_price_cents": 50,
            "filled_contracts": 5, "cost_usd": 2.5, "client_order_id": "w-11",
            "status": "filled", "network": "mainnet",
        })
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 0
    assert _copies() == []



def _their_pos(ticker, qty=1000.0, cur_price=0.5, cost=500.0):
    return {"ticker": ticker, "position_fp": qty,
            "market_exposure_dollars": cost, "cur_price": cur_price,
            "title": "Who wins?", "event_ticker": "who-wins"}


def test_poll_after_a_push_does_not_open_a_second_position(
    fresh_db, env_net, monkeypatch,
):
    """The dedupe key is the poll's own (ticker, side) open-copy key, so the
    Data-API read that arrives seconds later finds the position already there."""
    cfg = _cfg()
    _arm(cfg)
    calls = _patch_live(monkeypatch)

    async def _positions(limit=500, user=None, **kw):
        return [_their_pos(COND)] if user else []

    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 1
    run_async(copy_trader.run_tick(cfg, authed=True))
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert len(_copies()) == 1
    assert len(calls["place"]) == 1


def test_pushed_entry_leaves_the_size_anchor_to_the_poll(
    fresh_db, env_net, monkeypatch,
):
    """A frame carries the TRADE's size, not the wallet's position. Anchoring
    to it would make the next poll read their pre-existing shares as a fresh
    scale-in and buy again; leaving it unset lets the poll record their real
    total without buying."""
    cfg = _cfg(copy_allow_reentries=True)
    _arm(cfg)
    calls = _patch_live(monkeypatch)

    async def _positions(limit=500, user=None, **kw):
        return [_their_pos(COND, qty=1000.0)] if user else []

    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    copy_trader.on_activity_trade(_frame())
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 1
    assert "|".join((COND, "yes")) not in copy_trader._load_qty_base("mainnet")
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert copy_trader._load_qty_base("mainnet")[f"{COND}|yes"] == 1000.0
    assert len(_copies()) == 1 and len(calls["place"]) == 1


def test_a_real_tape_frame_reaches_the_entry_queue(fresh_db, env_net, monkeypatch):
    """End to end from the socket's own parser: activity_ws.handle_message ->
    the every-trade hook service.py registers -> on_activity_trade. Guards the
    field names across the two modules, which is exactly what a wakeup-only
    integration never had to get right."""
    import activity_ws

    cfg = _cfg()
    _arm(cfg)
    _patch_live(monkeypatch)
    client = activity_ws._Client()
    client.set_watch([WALLET])
    client.set_trade_hook(copy_trader.on_activity_trade)
    client.handle_message({"topic": "activity", "type": "trades", "payload": {
        "proxyWallet": WALLET, "side": "BUY", "size": 100.0, "price": 0.5,
        "conditionId": COND, "slug": "who-wins", "eventSlug": "who-wins",
        "outcome": "Yes", "outcomeIndex": 0, "name": "Who wins?",
        "transactionHash": "0xtx-live", "timestamp": 1700000000,
    }})
    assert copy_trader.pending_pushes() == 1
    hit = client.drain_hits()[0]
    assert hit["outcomeIndex"] == 0 and hit["transactionHash"] == "0xtx-live"
    assert run_async(copy_trader.drain_pushed_entries(cfg)) == 1
