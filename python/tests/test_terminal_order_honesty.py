from __future__ import annotations

import asyncio

import pytest

import db
import terminal_trade as tt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "terminal.db")
    db.init_db()
    return tmp_path


def _market(monkeypatch, *, asks=(), bids=(), available=True):
    async def _m(cid, include_raw=False):
        m = {"conditionId": cid, "title": "Elon tweets?", "category": "politics",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 15.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "orderMinSize": 1,
                      "acceptingOrders": True, "feesEnabled": False}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _b(cid, side="yes"):
        if not available:
            return {"available": False, "bids": [], "asks": [],
                    "bestBid": None, "bestAsk": None,
                    "note": "The CLOB did not return a book for this token."}
        return {
            "available": True, "note": None,
            "asks": [{"priceCents": p, "size": s} for p, s in asks],
            "bids": [{"priceCents": p, "size": s} for p, s in bids],
            "bestAsk": asks[0][0] if asks else None,
            "bestBid": bids[0][0] if bids else None,
        }

    monkeypatch.setattr(tt.term, "market", _m)
    monkeypatch.setattr(tt.term, "book", _b)
    monkeypatch.setattr(tt.api, "_deposit_wallet_issue", lambda: "")
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    monkeypatch.setattr(tt, "_spawn", lambda coro: coro.close())


def _balance(monkeypatch, usd):
    async def _avail(paper):
        return usd
    monkeypatch.setattr(tt, "_available_cash_usd", _avail)


def test_a_resting_buy_is_not_reported_as_a_fill(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0xdead", "status": "live"}, "raw": {}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["ok"] is True
    assert "Resting at 14¢" in r["message"], r["message"]
    assert "the best ask is 15¢" in r["message"], r["message"]
    assert "not a position until it fills" in r["message"]
    assert "cancel or reprice" in r["message"]
    assert "reconciles" not in r["message"]


def test_a_resting_sell_quotes_the_bid_not_the_ask(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(55.0, 500)], bids=[(49.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0xbeef", "status": "live"}, "raw": {}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "sell",
                       "priceCents": 51, "size": 5, "orderType": "GTC",
                       "confirmUsd": 2.55}))
    assert "Resting at 51¢" in r["message"], r["message"]
    assert "the best bid is 49¢" in r["message"], r["message"]


def test_a_resting_order_with_an_unreadable_book_does_not_invent_a_touch(
        fresh_db, monkeypatch):
    _market(monkeypatch, available=False)
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0x1", "status": "live"}, "raw": {}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert "Resting at 14¢" in r["message"]
    assert "could not read the touch" in r["message"]
    assert "not a position until it fills" in r["message"]


def test_a_filled_order_keeps_the_reconcile_sentence(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(14.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0x2", "status": "matched"}, "raw": {}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert "Order sent" in r["message"]
    assert "reconciles" in r["message"]
    assert "Resting" not in r["message"]


def test_a_partial_fak_says_what_filled_and_what_was_killed(
        fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(14.0, 3)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0x3", "status": "matched"},
                "raw": {"size_matched": 3}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "FAK",
                       "confirmUsd": 0.42}))
    assert "Filled 3 of 5" in r["message"], r["message"]
    assert "remaining 2 was killed" in r["message"], r["message"]
    assert r["filledSize"] == 3


def test_a_partial_gtc_says_the_remainder_is_still_on_the_book(
        fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(14.0, 3)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0x4", "status": "matched"},
                "raw": {"size_matched": 3}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert "Filled 3 of 5" in r["message"], r["message"]
    assert "remaining 2 is RESTING" in r["message"], r["message"]
    assert "killed" not in r["message"]


def test_a_fill_size_the_venue_did_not_report_is_not_guessed():
    assert tt._matched_size({"raw": {"makingAmount": "5000000"}}) is None
    assert tt._matched_size({"raw": {}}) is None
    assert tt._matched_size(None) is None
    assert tt._matched_size({"raw": {"size_matched": "3"}}) == 3.0


def test_a_buy_over_the_balance_is_blocked_before_it_is_sent(monkeypatch):
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 2.94)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 67, "size": 5,
                          "orderType": "FAK"}))
    assert pv["costUsd"] == 3.35
    assert pv["availableUsd"] == 2.94
    assert any("holds $2.94 of USDC" in b and "costs $3.35" in b
               for b in pv["blockers"]), pv["blockers"]


def test_a_buy_inside_the_balance_is_not_blocked(monkeypatch):
    _market(monkeypatch, asks=[(14.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 2.94)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 14, "size": 5,
                          "orderType": "FAK"}))
    assert pv["costUsd"] == 0.70
    assert not pv["blockers"], pv["blockers"]


def test_an_unknown_balance_blocks_nothing_and_says_so(monkeypatch):
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, None)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 67, "size": 5,
                          "orderType": "FAK"}))
    assert pv["availableUsd"] is None
    assert not pv["blockers"], pv["blockers"]
    assert any("could not read this account's balance" in w
               for w in pv["warnings"]), pv["warnings"]


def test_a_sell_is_never_blocked_on_cash(monkeypatch):
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 0.0)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "sell", "priceCents": 66, "size": 5,
                          "orderType": "FAK"}))
    assert not pv["blockers"], pv["blockers"]


def test_the_preview_reports_the_touch_and_whether_it_crosses(monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    rests = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                             "action": "buy", "priceCents": 14, "size": 5,
                             "orderType": "GTC"}))
    assert rests["bestAskCents"] == 15.0
    assert rests["bestBidCents"] == 13.0
    assert rests["crossesNow"] is False

    crosses = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                               "action": "buy", "priceCents": 15, "size": 5,
                               "orderType": "GTC"}))
    assert crosses["crossesNow"] is True


def test_an_unreadable_book_leaves_crosses_now_unknown(monkeypatch):
    _market(monkeypatch, available=False)
    _balance(monkeypatch, 50.0)
    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 14, "size": 5,
                          "orderType": "GTC"}))
    assert pv["crossesNow"] is None, "an unread book claimed the order rests"
    assert pv["bestAskCents"] is None


def test_a_sell_rejection_explains_the_resting_buy():
    raw = ("not enough balance / allowance: the balance is not enough -> "
           "balance: 0, order amount: 5000000")
    msg = tt.translate_venue_error(raw, action="sell")
    assert msg is not None
    assert "holds 0 of that token" in msg
    assert "still resting" in msg
    assert "check Track" in msg


def test_a_buy_rejection_names_both_numbers_when_we_know_them():
    raw = "not enough balance / allowance: the balance is not enough"
    msg = tt.translate_venue_error(raw, action="buy", cost_usd=3.35,
                                   balance_usd=2.94)
    assert msg == ("Your wallet's USDC ($2.94) is below this order's cost "
                   "($3.35).")


def test_a_buy_rejection_with_no_known_balance_still_names_the_cost():
    raw = "not enough balance / allowance"
    msg = tt.translate_venue_error(raw, action="buy", cost_usd=3.35)
    assert "below this order's cost ($3.35)" in msg
    assert "$2.94" not in msg, "a balance we never read was invented"


def test_the_marketable_minimum_is_explained():
    raw = "invalid amount for a marketable BUY order ($0.05)"
    msg = tt.translate_venue_error(raw, action="buy")
    assert msg == ("Polymarket's minimum for a marketable buy is $1.00; this "
                   "order is $0.05.")


def test_an_unknown_rejection_is_passed_through_verbatim():
    assert tt.translate_venue_error("some new CLOB error", action="buy") is None


def test_the_translated_message_reaches_the_ticket_and_the_raw_text_the_log(
        fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 2.94)

    async def _place(**kw):
        raise tt.api.PolymarketAPIError(
            400, "not enough balance / allowance: the balance is not enough "
                 "-> balance: 294, order amount: 3350000")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    monkeypatch.setattr(tt, "preview", _passthrough_preview(monkeypatch))
    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 67, "size": 5, "orderType": "FAK",
                       "confirmUsd": 3.35}))
    assert r["ok"] is False
    assert r["message"] == ("Your wallet's USDC ($2.94) is below this order's "
                            "cost ($3.35).")
    log = tt.order_log(10)["orders"]
    assert log and "balance is not enough" in (log[0]["error"] or ""), \
        "the venue's own words were lost"


def _passthrough_preview(monkeypatch):
    real = tt.preview

    async def _pv(req):
        out = await real(req)
        out["blockers"] = [b for b in out["blockers"] if "of USDC" not in b]
        return out
    return _pv


def _clob_orders(monkeypatch, rows):
    async def _req(method, path, **kw):
        return {"data": rows}
    monkeypatch.setattr(tt.api, "_authed_request", _req)


def test_working_rows_carry_the_remainder_and_the_touch(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _clob_orders(monkeypatch, [{
        "id": "0xdead", "market": "0xabc", "side": "BUY", "price": "0.14",
        "original_size": "5", "size_matched": "0", "created_at": "1756900000",
    }])
    tt._record_order({
        "condition_id": "0xabc", "title": "Elon tweets?", "side": "yes",
        "action": "buy", "order_type": "GTC", "requested_size": 5,
        "requested_price_cents": 14, "sent_size": 5, "sent_price_cents": 14,
        "order_id": "0xdead", "status": "placed",
    })

    r = _run(tt.resting_orders("0xabc"))
    o = r["orders"][0]
    assert o["size"] == 5 and o["filledSize"] == 0
    assert o["remainingSize"] == 5
    assert o["priceCents"] == 14.0
    assert o["bestTouchCents"] == 15.0, "a buy's touch must be the best ASK"
    assert o["side"] == "yes" and o["origin"] == "manual"


def test_a_working_sells_touch_is_the_bid(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(55.0, 500)], bids=[(49.0, 500)])
    _clob_orders(monkeypatch, [{
        "id": "0xbeef", "market": "0xabc", "side": "SELL", "price": "0.51",
        "original_size": "5", "size_matched": "1",
    }])
    tt._record_order({
        "condition_id": "0xabc", "title": "", "side": "no", "action": "sell",
        "order_type": "GTC", "requested_size": 5, "requested_price_cents": 51,
        "sent_size": 5, "sent_price_cents": 51, "order_id": "0xbeef",
        "status": "placed",
    })

    o = _run(tt.resting_orders("0xabc"))["orders"][0]
    assert o["bestTouchCents"] == 49.0
    assert o["remainingSize"] == 4
    assert o["side"] == "no"


def test_other_markets_are_filtered_out(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _clob_orders(monkeypatch, [
        {"id": "a", "market": "0xabc", "side": "BUY", "price": "0.14",
         "original_size": "5", "size_matched": "0"},
        {"id": "b", "market": "0xother", "side": "BUY", "price": "0.60",
         "original_size": "5", "size_matched": "0"},
    ])
    r = _run(tt.resting_orders("0xabc"))
    assert [o["orderId"] for o in r["orders"]] == ["a"]


def test_the_wallet_wide_list_reads_no_books(fresh_db, monkeypatch):
    async def _boom(cid, side="yes"):
        raise AssertionError("the wallet-wide list read a book")
    monkeypatch.setattr(tt.term, "book", _boom)
    _clob_orders(monkeypatch, [
        {"id": "a", "market": "0xabc", "side": "BUY", "price": "0.14",
         "original_size": "5", "size_matched": "0"},
    ])
    r = _run(tt.resting_orders())
    assert r["orders"][0]["bestTouchCents"] is None


def test_an_unreadable_size_leaves_the_remainder_unknown(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _clob_orders(monkeypatch, [
        {"id": "a", "market": "0xabc", "side": "BUY", "price": "0.14",
         "original_size": None, "size_matched": "0"},
    ])
    o = _run(tt.resting_orders("0xabc"))["orders"][0]
    assert o["remainingSize"] is None, "an unknown size became a remainder of 0"


def test_an_empty_market_list_says_which_emptiness_it_means(
        fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _clob_orders(monkeypatch, [])
    assert _run(tt.resting_orders("0xabc"))["note"] == \
        "No resting orders in this market."
    assert _run(tt.resting_orders())["note"] == "No resting orders."
