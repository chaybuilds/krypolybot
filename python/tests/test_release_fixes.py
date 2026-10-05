from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

import crypto15m_trader as c15
import db
import polymarket_api
import terminal_trade as tt
import trader


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_place_refuses_a_real_order_while_paper_mode_is_on(monkeypatch):
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: True)

    async def _preview(req):
        raise AssertionError("preview ran while paper mode was on")
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place_limit_order(**kw):
        raise AssertionError("a real order was signed in paper mode")
    monkeypatch.setattr(tt.api, "place_limit_order", _place_limit_order)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "sell",
                       "priceCents": 40, "size": 10, "orderType": "FAK",
                       "confirmUsd": 4.0}))
    assert r["ok"] is False
    assert "Paper mode is on" in r["message"]


def test_paper_mode_is_read_from_the_live_config(monkeypatch):
    fake = types.SimpleNamespace(STATE=types.SimpleNamespace(cfg={"terminal_paper_mode": True}))
    monkeypatch.setitem(sys.modules, "service", fake)
    assert tt._paper_mode_on() is True
    fake.STATE.cfg = {"terminal_paper_mode": False}
    assert tt._paper_mode_on() is False
    fake.STATE.cfg = None
    assert tt._paper_mode_on() is False


def _standing(monkeypatch, *, positions, prob=30.0, placed=None):
    entries = [{
        "id": "st_1", "conditionId": "0xabc", "title": "T", "side": "yes",
        "kind": "stopLoss", "triggerCents": 40.0, "size": 100,
        "createdAt": "", "armed": True, "lastCheckedAt": None,
        "firedAt": None, "note": None,
    }]
    monkeypatch.setattr(tt, "standing_list", lambda: entries)
    monkeypatch.setattr(tt, "_standing_put", lambda e: None)

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": prob}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return positions
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _place(req, **kw):
        if placed is not None:
            placed.append(req)
        return {"ok": True, "message": "sold"}
    monkeypatch.setattr(tt, "place", _place)

    async def _preview(req):
        return {"costUsd": 1.0}
    monkeypatch.setattr(tt, "preview", _preview)
    return entries


def test_a_standing_stop_does_not_fire_in_paper_mode(monkeypatch):
    placed = []
    held = [{"ticker": "0xabc", "position_fp": 100.0}]
    entries = _standing(monkeypatch, positions=held, placed=placed)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    r = _run(tt.standing_tick(allow_trading=True, paper=True))
    assert placed == [], "a standing stop placed an order while paper mode was on"
    assert entries[0]["armed"] is False
    assert "paper mode is on" in entries[0]["note"]
    assert r["alerts"] and r["alerts"][0]["blocked"] is True


def test_the_standing_tick_reads_the_paper_switch_itself(monkeypatch):
    placed = []
    held = [{"ticker": "0xabc", "position_fp": 100.0}]
    _standing(monkeypatch, positions=held, placed=placed)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: True)
    _run(tt.standing_tick(allow_trading=True))
    assert placed == []


def test_a_standing_stop_still_fires_live(monkeypatch):
    placed = []
    held = [{"ticker": "0xabc", "position_fp": 100.0}]
    _standing(monkeypatch, positions=held, placed=placed)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    _run(tt.standing_tick(allow_trading=True, paper=False))
    assert len(placed) == 1 and placed[0]["action"] == "sell"


def test_exit_was_sell_matches_what_exit_proceeds_actually_returns():
    assert trader._exit_was_sell({"kind": "sold"}) is True
    assert trader._exit_was_sell({"kind": "SELL"}) is True
    assert trader._exit_was_sell({"kind": "redeemed"}) is False
    assert trader._exit_was_sell({}) is False
    assert trader._exit_was_sell(None) is False


def test_get_positions_strict_raises_when_our_address_cannot_be_read(monkeypatch):
    def _boom():
        raise RuntimeError("no saved wallet")
    monkeypatch.setattr(polymarket_api.auth, "trading_address", _boom)

    async def _get(*a, **kw):
        raise AssertionError("the network must not be hit without an address")
    monkeypatch.setattr(polymarket_api, "_get", _get)

    assert _run(polymarket_api.get_positions()) == []
    with pytest.raises(RuntimeError):
        _run(polymarket_api.get_positions(strict=True))


def test_the_standing_tick_holds_fire_when_the_wallet_address_is_missing(monkeypatch):
    placed = []
    entries = _standing(monkeypatch, positions=[], placed=placed)

    async def _positions(*a, **kw):
        assert kw.get("strict") is True, "the standing tick must read strictly"
        raise RuntimeError("no saved wallet")
    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)

    _run(tt.standing_tick(allow_trading=True))
    assert placed == []
    assert entries[0]["armed"] is True, "the stop was retired on an unreadable wallet"
    assert "could not be read" in entries[0]["note"]


@pytest.fixture
def c15_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "exit.db")
    db.init_db()
    c15._exit_fail_at.clear()
    c15._stop_retry_at.clear()
    return tmp_path


def _c15_pos(pid=1):
    return {"id": pid, "ticker": "0xabc", "direction": "up", "asset": "BTC",
            "filled_contracts": 10}


def _c15_wire(monkeypatch):
    calls = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "OID", "status": "live"}}

    monkeypatch.setattr(c15.polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(c15.db, "update_crypto15m_position", lambda *a, **k: None)
    monkeypatch.setattr(c15.db, "fetch_crypto15m_by_id",
                        lambda *a, **k: {"id": 1, "status": "exiting"})
    return calls


def test_the_stop_loss_is_exempt_from_the_shared_exit_cooldown(c15_fresh, monkeypatch):
    calls = _c15_wire(monkeypatch)
    c15._exit_fail_at[1] = time.time()

    _run(c15._place_exit_sell(_c15_pos(), {}, exit_cents=45, reason="take_profit"))
    assert calls == [], "take-profit should still honour its cooldown"

    _run(c15._place_exit_sell(_c15_pos(), {}, exit_cents=45, reason="stop_loss"))
    assert len(calls) == 1 and calls[0]["action"] == "sell", \
        "the stop-loss was swallowed by the take-profit's cooldown"


def test_paired_fee_is_the_fee_v2_curve_on_both_legs():
    assert abs(c15.paired_fee_cents(50.0, 50.0) - 3.5) < 1e-6
    assert c15.paired_fee_cents(90.0, 10.0) < c15.paired_fee_cents(50.0, 50.0)


def _paired_asset(up_ask: float, down_ask: float) -> dict:
    return {
        "hasMarket": True, "favorite": "up", "inWindow": True,
        "upAsk": up_ask, "downAsk": down_ask, "ticker": "KXBTC15M-P1",
        "modelProb": 0.62, "secsLeft": 600, "book": {},
    }


def test_a_refused_pair_names_the_taker_fees_it_would_have_paid(monkeypatch):
    cfg = {"crypto15m_enabled": True, "crypto15m_paired_mode": True,
           "crypto15m_direction_mode": "paired",
           "crypto15m_paired_max_combined_cents": 100.0,
           "crypto15m_max_concurrent": 7}
    ok, why = c15.should_enter(_paired_asset(0.52, 0.49), cfg,
                               has_open=False, open_count=0)
    assert not ok and "combined" in why and "taker fees" in why


import terminal_paper as tp
from test_terminal_paper import _mock_market, _order, paper_db


def test_two_resting_sells_cannot_both_be_written_against_one_holding(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    r0 = _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    assert r0["ok"]
    _mock_market(monkeypatch, bids=[(30.0, 100)])
    r1 = _run(tp.place(_order(action="sell", priceCents=45, size=10, orderType="GTC")))
    assert r1["ok"] and r1["status"] == "resting"
    r2 = _run(tp.place(_order(action="sell", priceCents=45, size=10, orderType="GTC")))
    assert r2["ok"] is False
    assert "naked short" in r2["message"].lower()


def test_a_sell_that_outran_the_holding_does_not_credit_phantom_cash(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    for _ in range(2):
        tp._record({
            "condition_id": "0xabc", "title": "T", "side": "yes",
            "action": "sell", "order_type": "FAK", "requested_size": 10,
            "requested_price_cents": 50, "filled_size": 10,
            "avg_fill_price_cents": 50.0, "resting_size": 0, "fees_usd": 0.0,
            "cost_usd": 5.0, "status": "filled", "note": "", "filled_at": tp._now_iso(),
        })
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert abs(pf["realizedPnlUsd"] - 1.0) < 1e-6
    assert abs(pf["cashUsd"] - 1001.0) < 1e-6, pf["cashUsd"]


def test_the_fill_sweep_cancels_a_resting_sell_once_the_position_is_gone(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    _mock_market(monkeypatch, bids=[(30.0, 100)])
    r1 = _run(tp.place(_order(action="sell", priceCents=45, size=10, orderType="GTC")))
    assert r1["status"] == "resting"
    _mock_market(monkeypatch, bids=[(44.0, 100)])
    tp._record({
        "condition_id": "0xabc", "title": "T", "side": "yes",
        "action": "sell", "order_type": "FAK", "requested_size": 10,
        "requested_price_cents": 44, "filled_size": 10,
        "avg_fill_price_cents": 44.0, "resting_size": 0, "fees_usd": 0.0,
        "cost_usd": 4.4, "status": "filled", "note": "", "filled_at": tp._now_iso(),
    })
    _mock_market(monkeypatch, bids=[(50.0, 100)])
    assert _run(tp._fill_resting()) == 0
    with tp._conn() as conn:
        row = conn.execute("SELECT status, resting_size, note FROM terminal_paper_orders "
                           "WHERE id=?", (r1["localId"],)).fetchone()
    assert row["status"] == "canceled" and float(row["resting_size"]) == 0.0
    assert "no longer held" in row["note"]


def test_per_outcome_redeem_rows_book_a_settled_loss_at_zero():
    import trader
    pos = {"ticker": "0xabc", "direction": "yes", "created_at": "2026-09-01 00:00:00"}
    later = 1_790_000_000
    lost = [
        {"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 0, "usdcSize": 0, "timestamp": later},
        {"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 1, "usdcSize": 25.0, "timestamp": later},
    ]
    assert trader._exit_proceeds(pos, lost) == {"proceeds": 0.0, "kind": "redeemed"}
    won = [{"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 0, "usdcSize": 25.0, "timestamp": later},
           {"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 1, "usdcSize": 0, "timestamp": later}]
    assert trader._exit_proceeds(pos, won) == {"proceeds": 25.0, "kind": "redeemed"}
    other = [{"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 1, "usdcSize": 0, "timestamp": later}]
    assert trader._exit_proceeds(pos, other) is None
