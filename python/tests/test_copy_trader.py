"""Copy-trading engine tests (paper mode, fully hermetic).

Pins the stateless mirror logic: ENTER a followed wallet's holding we don't
have, don't double-enter, and EXIT once no followed wallet holds it anymore.
"""
from __future__ import annotations

import asyncio

import pytest

import copy_trader
import db
import polymarket_api
import trader
from config import merge_with_defaults


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "copy-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


@pytest.fixture
def env_net(monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    return "mainnet"


@pytest.fixture(autouse=True)
def _reset_copy_state():
    copy_trader._copy_cooldown.clear()
    copy_trader._exit_retry_at.clear()
    copy_trader._category_cache.clear()
    copy_trader._last_halt_log_t = 0.0
    yield


def run_async(coro):
    return asyncio.run(coro)


def _cfg(**over) -> dict:
    c = merge_with_defaults({"account_mode": "live"})
    c["network"] = "mainnet"
    c["copy_enabled"] = True
    c["copy_wallets"] = ["0x" + "a" * 40]
    c["copy_sizing_mode"] = "fixed"
    c["copy_fixed_usd"] = 10.0
    c["copy_min_trade_usd"] = 25.0
    c["start_bankroll_usd"] = 100.0
    c["copy_only_new_entries"] = False
    c.update(over)
    return c


def _their_pos(ticker, qty=10.0, cur_price=0.5, cost=50.0):
    return {"ticker": ticker, "position_fp": qty,
            "market_exposure_dollars": cost, "cur_price": cur_price,
            "title": ticker, "event_ticker": ""}


def _open_copies():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM bot_positions WHERE signal_source='copy' AND resolved=0"
        ).fetchall()]


def _all_copies():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM bot_positions WHERE signal_source='copy'"
        ).fetchall()]


def _patch_positions(monkeypatch, holder):
    async def _gp(limit=500, user=None, **kw):
        return holder["v"]
    monkeypatch.setattr(polymarket_api, "get_positions", _gp)


def _patch_live(monkeypatch, ask=50, bid=48, category="politics"):
    """Mock the real order path (no paper mode): place_limit_order + get_quote,
    and a deterministic sell confirmation."""
    calls = {"place": []}

    async def _place(**kw):
        calls["place"].append(kw)
        return {"order": {"order_id": "ord-1", "status": "resting"}}

    async def _quote(_t, _s):
        return {"ask_cents": ask, "bid_cents": bid}

    async def _confirm(oid, qty, px):
        return qty, float(bid)

    async def _market(ticker):
        return {"ticker": ticker, "category": category}

    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "fetch_market", _market)
    monkeypatch.setattr(trader, "_confirm_sell", _confirm)
    return calls


def _seed_filled_copy(ticker="T-A", direction="yes", contracts=20, cost=10.0):
    with db.get_db() as conn:
        return db.insert_bot_position(conn, {
            "signal_source": "copy", "signal_id": abs(hash(ticker)) % 1_000_000,
            "ticker": ticker, "direction": direction,
            "target_contracts": contracts, "limit_price_cents": int(cost / contracts * 100),
            "filled_contracts": contracts, "cost_usd": cost,
            "client_order_id": f"copy-{ticker}", "status": "filled", "network": "mainnet",
        })


def test_copy_enters_followed_holding(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A", cur_price=0.5)]})
    calls = _patch_live(monkeypatch, ask=50)
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    rows = _open_copies()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "T-A" and rows[0]["direction"] == "yes"
    assert rows[0]["status"] == "submitted"
    assert rows[0]["target_contracts"] == 20
    assert len(calls["place"]) == 1 and calls["place"][0]["action"] == "buy"


def test_copy_only_new_baselines_then_copies_fresh(fresh_db, env_net, monkeypatch):
    holder = {"v": [_their_pos("T-A", cur_price=0.5)]}
    _patch_positions(monkeypatch, holder)
    _patch_live(monkeypatch, ask=50)
    cfg = _cfg(copy_only_new_entries=True)
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert _open_copies() == []
    holder["v"] = [_their_pos("T-A", cur_price=0.5), _their_pos("T-B", cur_price=0.4)]
    run_async(copy_trader.run_tick(cfg, authed=True))
    rows = _open_copies()
    assert len(rows) == 1 and rows[0]["ticker"] == "T-B"


def test_copy_halts_on_account_daily_stop(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A")]})
    _patch_live(monkeypatch, ask=50)
    monkeypatch.setattr(trader, "_is_blocked_by_daily_risk",
                        lambda cfg, env: (True, "daily stop-loss hit"))
    changed = run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert _open_copies() == [] and changed == []


def test_copy_no_side_for_negative_qty(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-B", qty=-8.0, cur_price=0.4)]})
    _patch_live(monkeypatch, ask=40)
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    rows = _open_copies()
    assert len(rows) == 1 and rows[0]["direction"] == "no"


def test_copy_does_not_double_enter(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A")]})
    _patch_live(monkeypatch)
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert len(_open_copies()) == 1


def test_copy_no_trade_without_auth(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A")]})
    calls = _patch_live(monkeypatch)
    changed = run_async(copy_trader.run_tick(_cfg(), authed=False))
    assert changed == [] and calls["place"] == []
    assert len(_open_copies()) == 0


def test_copy_books_gone_when_wallet_genuinely_empty(fresh_db, env_net, monkeypatch):
    _seed_filled_copy("T-A", contracts=20, cost=10.0)
    _patch_positions_split(monkeypatch, followed=[], ours=[])
    calls = _patch_live(monkeypatch, bid=60)

    async def _activity(limit=1000, **kw):
        return [{"conditionId": "T-A", "outcomeIndex": 0, "timestamp": 9_999_999_999}]
    monkeypatch.setattr(polymarket_api, "get_activity", _activity)
    monkeypatch.setattr(trader, "_exit_proceeds", lambda pos, act: {"proceeds": 12.0, "kind": "sold"})

    run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert calls["place"] == []
    assert len(_open_copies()) == 0
    closed = [r for r in _all_copies() if r["resolved"]]
    assert len(closed) == 1 and closed[0]["closed_early"] == 1
    assert closed[0]["pnl_usd"] == pytest.approx(1.75)


def _patch_positions_split(monkeypatch, *, followed, ours):
    """Followed-wallet reads pass user=<their addr>; our own holdings read passes
    user=None — return different lists so a position can be 'gone from our wallet'
    while the wallet still holds OTHER positions (the realistic case)."""
    async def _gp(limit=500, user=None, **kw):
        return ours if user is None else followed
    monkeypatch.setattr(polymarket_api, "get_positions", _gp)


def test_copy_books_gone_position_without_reselling(fresh_db, env_net, monkeypatch):
    _seed_filled_copy("T-A", contracts=20, cost=10.0)
    _patch_positions_split(monkeypatch, followed=[], ours=[_their_pos("OTHER", qty=5.0)])
    calls = _patch_live(monkeypatch, bid=60)

    async def _activity(limit=1000, **kw):
        return [{"conditionId": "T-A", "outcomeIndex": 0, "timestamp": 9_999_999_999}]
    monkeypatch.setattr(polymarket_api, "get_activity", _activity)
    monkeypatch.setattr(trader, "_exit_proceeds", lambda pos, act: {"proceeds": 12.0, "kind": "sold"})

    run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert calls["place"] == []
    closed = [r for r in _all_copies() if r["resolved"]]
    assert len(closed) == 1 and closed[0]["closed_early"] == 1
    assert closed[0]["pnl_usd"] == pytest.approx(1.75)


@pytest.mark.parametrize("rest, want_pnl", [
    ({"type": "TRADE", "side": "SELL", "size": 10, "price": 0.60, "usdcSize": 5.88}, 1.51),
    ({"type": "REDEEM", "size": 10, "price": 0, "usdcSize": 10.0}, 5.63),
])
def test_gone_copy_after_a_reduction_charges_each_exit_fee_once(
        fresh_db, env_net, monkeypatch, rest, want_pnl):
    pid = _seed_filled_copy("T-A", contracts=20, cost=10.0)
    slice_fee = 0.05 * 0.6 * 0.4 * 10
    with db.get_db() as conn:
        db.update_bot_position(conn, pid, filled_contracts=10, cost_usd=5.0,
                               fees_usd=0.25,
                               pnl_usd=round(6.0 - 5.0 - slice_fee, 4),
                               settlement_usd=6.0)
    _patch_positions_split(monkeypatch, followed=[], ours=[_their_pos("OTHER", qty=5.0)])
    calls = _patch_live(monkeypatch, bid=60)
    later = 9_999_999_999
    rows = [
        {"conditionId": "T-A", "outcomeIndex": 0, "timestamp": later, "type": "TRADE",
         "side": "SELL", "size": 10, "price": 0.60, "usdcSize": 5.88},
        {"conditionId": "T-A", "outcomeIndex": 0, "timestamp": later, **rest},
    ]

    async def _activity(limit=1000, **kw):
        return rows
    monkeypatch.setattr(polymarket_api, "get_activity", _activity)

    run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert calls["place"] == []
    closed = [r for r in _all_copies() if r["resolved"]]
    assert len(closed) == 1
    assert closed[0]["pnl_usd"] == pytest.approx(want_pnl, abs=1e-4)


def test_copy_gone_without_evidence_stays_open_no_order(fresh_db, env_net, monkeypatch):
    _seed_filled_copy("T-A", contracts=20, cost=10.0)
    _patch_positions_split(monkeypatch, followed=[], ours=[_their_pos("OTHER", qty=5.0)])
    calls = _patch_live(monkeypatch, bid=60)

    async def _activity(limit=1000, **kw):
        return []
    monkeypatch.setattr(polymarket_api, "get_activity", _activity)
    monkeypatch.setattr(trader, "_exit_proceeds", lambda pos, act: None)

    run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert calls["place"] == []
    assert len(_open_copies()) == 1


def test_copy_exit_uses_fak_when_still_held(fresh_db, env_net, monkeypatch):
    _seed_filled_copy("T-A", contracts=20, cost=10.0)
    _patch_positions_split(monkeypatch, followed=[], ours=[_their_pos("T-A", qty=20.0)])
    calls = _patch_live(monkeypatch, bid=60)
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert len(calls["place"]) == 1
    assert calls["place"][0]["order_type"] == "FAK"
    assert calls["place"][0]["action"] == "sell"


def test_copy_skips_dust_below_min(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A", cost=50.0)]})
    _patch_live(monkeypatch)
    run_async(copy_trader.run_tick(_cfg(copy_min_trade_usd=100.0), authed=True))
    assert len(_open_copies()) == 0


def test_copy_respects_max_concurrent(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [
        _their_pos("T-A"), _their_pos("T-B"), _their_pos("T-C"),
    ]})
    _patch_live(monkeypatch)
    run_async(copy_trader.run_tick(_cfg(copy_max_concurrent=2), authed=True))
    assert len(_open_copies()) == 2


def test_copy_noop_when_disabled(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A")]})
    _patch_live(monkeypatch)
    changed = run_async(copy_trader.run_tick(_cfg(copy_enabled=False), authed=True))
    assert changed == []
    assert len(_open_copies()) == 0


def test_copy_ignores_own_wallet(fresh_db, env_net, monkeypatch):
    own = "0x" + "a" * 40
    monkeypatch.setattr(copy_trader.polymarket_auth, "trading_address", lambda env=None: own)
    monkeypatch.setattr(copy_trader.polymarket_auth, "get_address", lambda env=None: own)
    monkeypatch.setattr(copy_trader.polymarket_auth, "get_funder", lambda env=None: "")
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A")]})
    _patch_live(monkeypatch)
    run_async(copy_trader.run_tick(_cfg(copy_wallets=[own]), authed=True))
    assert len(_open_copies()) == 0


def test_copy_reentry_copies_scale_in(fresh_db, env_net, monkeypatch):
    holder = {"v": [_their_pos("T-A", qty=10.0, cur_price=0.5)]}
    _patch_positions(monkeypatch, holder)
    calls = _patch_live(monkeypatch, ask=50)
    cfg = _cfg(copy_allow_reentries=True)
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert len(_open_copies()) == 1
    holder["v"] = [_their_pos("T-A", qty=60.0, cur_price=0.5)]
    run_async(copy_trader.run_tick(cfg, authed=True))
    rows = _open_copies()
    assert len(rows) == 2 and all(r["ticker"] == "T-A" for r in rows)
    assert len(calls["place"]) == 2 and all(c["action"] == "buy" for c in calls["place"])
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert len(_open_copies()) == 2


def test_copy_reentry_ignores_dust_add(fresh_db, env_net, monkeypatch):
    holder = {"v": [_their_pos("T-A", qty=10.0, cur_price=0.5)]}
    _patch_positions(monkeypatch, holder)
    _patch_live(monkeypatch, ask=50)
    cfg = _cfg(copy_allow_reentries=True)
    run_async(copy_trader.run_tick(cfg, authed=True))
    holder["v"] = [_their_pos("T-A", qty=12.0, cur_price=0.5)]
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert len(_open_copies()) == 1


def test_copy_reentry_off_never_adds(fresh_db, env_net, monkeypatch):
    holder = {"v": [_their_pos("T-A", qty=10.0, cur_price=0.5)]}
    _patch_positions(monkeypatch, holder)
    _patch_live(monkeypatch, ask=50)
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    holder["v"] = [_their_pos("T-A", qty=500.0, cur_price=0.5)]
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    assert len(_open_copies()) == 1


def test_copy_reentry_baselines_preexisting_open_copy(fresh_db, env_net, monkeypatch):
    holder = {"v": [_their_pos("T-A", qty=80.0, cur_price=0.5)]}
    _patch_positions(monkeypatch, holder)
    _patch_live(monkeypatch, ask=50)
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    cfg = _cfg(copy_allow_reentries=True)
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert len(_open_copies()) == 1
    holder["v"] = [_their_pos("T-A", qty=140.0, cur_price=0.5)]
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert len(_open_copies()) == 2


def test_copy_skips_below_min_size(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A", cur_price=0.90)]})
    calls = _patch_live(monkeypatch, ask=90)
    run_async(copy_trader.run_tick(_cfg(copy_fixed_usd=2.0), authed=True))
    assert calls["place"] == []
    assert len(_open_copies()) == 0



def test_a_copied_entry_records_the_markets_category(fresh_db, env_net, monkeypatch):
    _patch_positions(monkeypatch, {"v": [_their_pos("T-CAT", cur_price=0.5)]})
    _patch_live(monkeypatch, ask=50, category="sports")
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    rows = _open_copies()
    assert len(rows) == 1 and rows[0]["category"] == "sports"
    priced = dict(rows[0], filled_contracts=20, cost_usd=10.0,
                  avg_fill_price_cents=50)
    assert trader._position_fee_usd(priced) == pytest.approx(0.05 * 0.25 * 20)


def test_the_local_markets_row_answers_without_a_gamma_call(fresh_db, env_net,
                                                            monkeypatch):
    """The scanner already keeps `markets` fresh; only a miss costs a fetch."""
    with db.get_db() as conn:
        db.upsert_market(conn, {"ticker": "T-LOC", "title": "t", "slug": "t",
                                "status": "open", "category": "finance"})
    fetched: list[str] = []

    async def _boom(ticker):
        fetched.append(ticker)
        return None

    _patch_positions(monkeypatch, {"v": [_their_pos("T-LOC", cur_price=0.5)]})
    _patch_live(monkeypatch, ask=50)
    monkeypatch.setattr(polymarket_api, "fetch_market", _boom)
    run_async(copy_trader.run_tick(_cfg(), authed=True))
    rows = _open_copies()
    assert len(rows) == 1 and rows[0]["category"] == "finance"
    assert fetched == []


def test_an_unknown_category_is_rated_at_the_non_crypto_default(fresh_db):
    """Genuinely unreadable → "" is stored, and "" must NOT mean crypto:
    bot_positions never holds a 15m window, so the fallback is 'other'
    (0.05), the modal non-crypto rate."""
    import backtest
    pos = {"category": "", "filled_contracts": 10, "cost_usd": 5.0,
           "avg_fill_price_cents": 50}
    assert trader._fee_coeff_for(pos) == backtest.CATEGORY_TAKER_FEE["other"]
    assert trader._position_fee_usd(pos) == pytest.approx(0.05 * 0.25 * 10)
    assert trader._fee_coeff_for(dict(pos, category="crypto")) == \
        backtest.CATEGORY_TAKER_FEE["crypto"]
    assert trader._position_fee_usd(dict(pos, category="crypto")) == \
        pytest.approx(0.07 * 0.25 * 10)
    assert trader._sell_fee_usd(pos, 10, 50.0) == pytest.approx(0.05 * 0.25 * 10)


def test_copy_sends_nothing_from_a_paper_account(fresh_db, env_net, monkeypatch):
    """Account mode gate: the user's copy switch stays ON, the account is in
    Paper, and nothing goes to the venue (config.engine_on)."""
    _patch_positions(monkeypatch, {"v": [_their_pos("T-A", cur_price=0.5)]})
    calls = _patch_live(monkeypatch, ask=50)
    cfg = _cfg(account_mode="paper")
    assert cfg["copy_enabled"] is True
    run_async(copy_trader.run_tick(cfg, authed=True))
    assert calls["place"] == [] and _open_copies() == []
    assert run_async(copy_trader.status(cfg, authed=True))["trading"] is False
