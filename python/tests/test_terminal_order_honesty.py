"""What an order actually DID, said in the right tense.

Every case here is one the owner hit in a live test on 2026-09-03, in order:

  1. `buy 5 YES @ 14c GTC` against a 15c ask came back "Order sent … fill
     details appear once it reconciles". It never filled. Nothing in the app
     said it was still on the book, so it read as "bought".
  2. The attempt to sell that non-existent position came back
     "not enough balance / allowance: … balance: 0, order amount: 5000000",
     rendered raw on the ticket.
  3. Three buys costing $3.35, $5 and $7 were reviewed and sent against a
     $2.94 wallet. Nothing checked, and the exchange's refusal was again the
     raw text.

Pure logic — no network, no clock, no sleeps.
"""
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


@pytest.fixture
def no_dollar_floor(monkeypatch):
    """For tests about how a result is WORDED, written around the live
    `buy 5 @ 14c` case. Polymarket's $1.00 marketable floor (tested on its own
    below) would refuse those small orders before the wording is reached."""
    monkeypatch.setattr(tt, "MIN_MARKETABLE_BUY_USD", 0.0)


def _market(monkeypatch, *, asks=(), bids=(), available=True, tick=0.01):
    """A fee-free open market whose book holds exactly these levels."""
    async def _m(cid, include_raw=False):
        m = {"conditionId": cid, "title": "Elon tweets?", "category": "politics",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 15.0,
             "_raw": {"orderPriceMinTickSize": tick, "orderMinSize": 1,
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
    """Pin what the account can spend. `None` = we could not read it."""
    async def _avail(paper):
        return usd
    monkeypatch.setattr(tt, "_available_cash_usd", _avail)



def test_a_resting_buy_is_not_reported_as_a_fill(fresh_db, monkeypatch):
    """The exact live case: buy 5 YES at 14c with the ask at 15c. The venue
    accepts it and it sits there. The message must name the reference price
    and refuse to call it a position."""
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
    """The 51c sell on the 5-minute window. A sell rests against the BID, so
    quoting the ask back at the user would explain nothing."""
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


def test_a_filled_order_keeps_the_reconcile_sentence(fresh_db, monkeypatch, no_dollar_floor):
    """"Fill details appear once it reconciles" is TRUE of a fill, and the
    only case it should survive."""
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
        fresh_db, monkeypatch, no_dollar_floor):
    """A marketable order that crossed 3 of 5 killed the other 2. Reported
    only because the VENUE said so — we never derive a fill from the book."""
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
        fresh_db, monkeypatch, no_dollar_floor):
    """The other half of a partial: a limit order's remainder RESTS, so the
    sentence about killing it would be false."""
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
    """`makingAmount` on a buy is collateral, not contracts. Reading it as a
    size would report a fill that never happened."""
    assert tt._matched_size({"raw": {"makingAmount": "5000000"}}) is None
    assert tt._matched_size({"raw": {"makingAmount": "0.7"}}, "buy", 5) is None
    assert tt._matched_size({"raw": {"takingAmount": "0.7"}}, "sell", 5) is None
    assert tt._matched_size({"raw": {}}) is None
    assert tt._matched_size(None) is None
    assert tt._matched_size({"raw": {"size_matched": "3"}}) == 3.0


@pytest.mark.parametrize("action,raw,want", [
    ("buy", {"takingAmount": "3", "makingAmount": "0.42"}, 3.0),
    ("sell", {"makingAmount": "3", "takingAmount": "1.53"}, 3.0),
    ("buy", {"takingAmount": "3000000", "makingAmount": "420000"}, 3.0),
    ("buy", {"takingAmount": "", "makingAmount": ""}, None),
    ("buy", {"takingAmount": "9000000000"}, None),
])
def test_the_shares_the_order_took_are_read_from_the_right_leg(action, raw, want):
    assert tt._matched_size({"raw": raw}, action, 5) == want


def test_a_delayed_cross_is_matching_not_resting(fresh_db, monkeypatch, no_dollar_floor):
    """A marketable GTC on a market with a matching delay comes back
    `delayed`: it crossed and is being matched. Calling it "Resting —
    nothing has filled" sent the user to cancel an order that was filling."""
    _market(monkeypatch, asks=[(14.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0xd1", "status": "delayed"},
                "raw": {"status": "delayed", "takingAmount": "", "makingAmount": ""}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["ok"] is True
    assert "Resting" not in r["message"], r["message"]
    assert "nothing has filled" not in r["message"], r["message"]
    assert "matching delay" in r["message"], r["message"]
    assert "not a position until it fills" in r["message"]


def test_a_matched_buy_reports_the_shares_it_took(fresh_db, monkeypatch, no_dollar_floor):
    _market(monkeypatch, asks=[(14.0, 3)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return {"order": {"order_id": "0xd2", "status": "matched"},
                "raw": {"status": "matched", "takingAmount": "3", "makingAmount": "0.42"}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["filledSize"] == 3.0
    assert "Filled 3 of 5" in r["message"], r["message"]


@pytest.mark.parametrize("asked", [3.5, 2.5, 97.5])
def test_a_sub_cent_price_goes_out_at_the_price_that_was_confirmed(
        fresh_db, monkeypatch, asked):
    """Near the extremes a market trades in 0.1c ticks. The confirmation, the
    ceilings and the agent's edge check all ran on the snapped price, and the
    order then went out rounded to whole cents: 3.5c as 4c, 97.5c as 98c, and
    (banker's rounding) 2.5c as 2c."""
    _market(monkeypatch, asks=[(99.9, 5000)], bids=[(0.1, 5000)], tick=0.001)
    _balance(monkeypatch, 5000.0)
    sent = []

    async def _place(**kw):
        sent.append(kw)
        return {"order": {"order_id": "0xsub", "status": "live"}, "raw": {}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    req = {"conditionId": "0xabc", "side": "yes", "action": "buy",
           "priceCents": asked, "size": 1000, "orderType": "GTC"}
    pv = _run(tt.preview(req))
    assert pv["priceCents"] == asked and not pv["blockers"], pv
    r = _run(tt.place({**req, "confirmUsd": pv["costUsd"]}))
    assert r["ok"], r["message"]
    assert sent[0]["price_cents"] == asked, \
        f"confirmed {asked}c, sent {sent[0]['price_cents']}c"
    assert f"Resting at {asked:g}¢" in r["message"], r["message"]
    log = tt.order_log()["orders"][0]
    assert log["sentPriceCents"] == asked


def test_the_venue_call_signs_a_sub_cent_price_unrounded(monkeypatch):
    """place_limit_order snaps to the market's own tick, so a fractional cent
    on a 0.001-tick market reaches the signer as is."""
    import polymarket_api as api
    signed = []

    async def _meta(ticker):
        return {"yes_token": "TOKYES", "no_token": "TOKNO",
                "tick_size": 0.001, "min_size": 5, "neg_risk": False}

    async def _stop(*a, **kw):
        raise RuntimeError("stop before submit")

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "_assert_maker_is_deposit_wallet", lambda: None)
    monkeypatch.setattr(api.auth, "create_signed_order",
                        lambda **kw: signed.append(kw) or {"signed": True})
    monkeypatch.setattr(api, "ensure_api_creds", _stop)
    with pytest.raises(RuntimeError):
        _run(api.place_limit_order(
            ticker="0xabc", side="yes", action="buy", count=1000,
            price_cents=3.5, order_type="GTC"))
    assert signed[0]["price"] == 0.035



def test_a_buy_over_the_balance_is_blocked_before_it_is_sent(monkeypatch):
    """$2.94 in the wallet, a $3.35 order. The exchange refuses this — the
    point is to say so before the user confirms it, in English."""
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 2.94)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 67, "size": 5,
                          "orderType": "FAK"}))
    assert pv["costUsd"] == 3.35
    assert pv["availableUsd"] == 2.94
    assert any("holds $2.94 of USDC" in b and "costs $3.35" in b
               for b in pv["blockers"]), pv["blockers"]


def test_a_buy_inside_the_balance_is_not_blocked(monkeypatch, no_dollar_floor):
    _market(monkeypatch, asks=[(14.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 2.94)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 14, "size": 5,
                          "orderType": "FAK"}))
    assert pv["costUsd"] == 0.70
    assert not pv["blockers"], pv["blockers"]


def test_an_unknown_balance_blocks_nothing_and_says_so(monkeypatch):
    """Refusing an order because a wallet READ failed is the same lie in the
    other direction. It warns instead."""
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
    """A sell brings money IN. Gating it on the cash balance would trap a
    position in exactly the wallet that most needs to close it."""
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
    """A sentence we cannot translate is shown as-is. Replacing it with
    something vaguer would destroy the only evidence the user has."""
    assert tt.translate_venue_error("some new CLOB error", action="buy") is None


def test_the_translated_message_reaches_the_ticket_and_the_raw_text_the_log(
        fresh_db, monkeypatch):
    """Both halves of the contract in one test: the user reads English, the
    order log keeps the venue's own words."""
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


def test_a_protocol_v2_market_is_refused_in_plain_words(fresh_db, monkeypatch):
    """The real order choke point, not a stub of it: a Protocol V2 market never
    reaches the signer, and the ticket says why without an "HTTP 400" the venue
    never sent."""
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _meta(_cid):
        return {"yes_token": "111", "no_token": "222", "tick_size": 0.01,
                "min_size": 1, "neg_risk": False, "version": "v2"}

    def _no_sign(**_kw):
        raise AssertionError("signed an order for a Protocol V2 market")

    monkeypatch.setattr(tt.api, "get_market_meta", _meta)
    monkeypatch.setattr(tt.api.auth, "create_signed_order", _no_sign)
    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["ok"] is False
    assert r["message"].startswith("This market trades on Polymarket Protocol V2"), \
        r["message"]


def _passthrough_preview(monkeypatch):
    """`preview` with its balance BLOCKER downgraded, so a test can drive the
    placement path the exchange's own refusal takes."""
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
    """What the Market page's Working row prints: 0 of 5 filled, best ask
    15¢. Both come from the venue; neither is derived from our own request."""
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
    """Track's list must not pay for a book read per order. No condition id,
    no touch — and null there means "not read", not "no touch"."""
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



def _order_row(oid, status, reconciled=0):
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO terminal_orders (condition_id, title, side, action, "
            "order_type, requested_size, requested_price_cents, order_id, "
            "status, origin, network, reconciled) "
            "VALUES ('0xabc','T','yes','buy','GTC',10,50,?,?,'manual','mainnet',?)",
            (oid, status, reconciled))


def _row_status(oid):
    with db.get_db() as conn:
        return conn.execute("SELECT status FROM terminal_orders WHERE order_id=?",
                            (oid,)).fetchone()["status"]


def _venue_cancel(monkeypatch, reply):
    async def _cancel(oid):
        return reply
    monkeypatch.setattr(tt.api, "cancel_order", _cancel)


def test_a_refused_cancel_is_not_reported_as_canceled(fresh_db, monkeypatch):
    """DELETE /order answers 200 either way; an order that already filled
    comes back under `not_canceled`. It used to read "Canceled order …" and
    overwrite the reconciled 'filled' row with 'canceled', for good — and the
    Reprice button sends its replacement only after a cancel that says ok."""
    _order_row("0xfilled", "filled", reconciled=1)
    _venue_cancel(monkeypatch, {"canceled": [],
                                "not_canceled": {"0xfilled": "order already matched"}})
    r = _run(tt.cancel("0xfilled"))
    assert r["ok"] is False, r
    assert "order already matched" in r["message"]
    assert r["notCanceled"] == "order already matched"
    assert _row_status("0xfilled") == "filled"


def test_a_cancel_the_venue_did_not_acknowledge_is_not_a_cancel(fresh_db, monkeypatch):
    _order_row("0xlive", "placed")
    _venue_cancel(monkeypatch, {"canceled": [], "not_canceled": {}})
    r = _run(tt.cancel("0xlive"))
    assert r["ok"] is False
    assert _row_status("0xlive") == "placed"


def test_a_confirmed_cancel_marks_the_row(fresh_db, monkeypatch):
    _order_row("0xlive", "placed")
    _venue_cancel(monkeypatch, {"canceled": ["0xlive"], "not_canceled": {}})
    r = _run(tt.cancel("0xlive"))
    assert r["ok"] is True
    assert _row_status("0xlive") == "canceled"


def test_cancelling_a_partly_filled_remainder_keeps_the_fill(fresh_db, monkeypatch):
    """Reconcile marks an order filled on its first fill. Cancelling what is
    left of it takes the remainder off the book; it does not undo the fill."""
    _order_row("0xpart", "filled", reconciled=1)
    _venue_cancel(monkeypatch, {"canceled": ["0xpart"], "not_canceled": {}})
    assert _run(tt.cancel("0xpart"))["ok"] is True
    assert _row_status("0xpart") == "filled"



def _pv(**over):
    req = {"conditionId": "0xabc", "side": "yes", "action": "buy",
           "size": 5, "priceCents": 18.0, "orderType": "FAK"}
    req.update(over)
    return _run(tt.preview(req))


def test_a_marketable_buy_under_a_dollar_is_blocked_with_the_size_that_works(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)])
    _balance(monkeypatch, 50.0)
    pv = _pv()
    assert any("minimum for a marketable buy is $1.00" in b and "at least 6 contracts" in b
               for b in pv["blockers"]), pv["blockers"]


def test_a_crossing_gtc_under_a_dollar_is_blocked_but_a_resting_one_is_not(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)])
    _balance(monkeypatch, 50.0)
    crossing = _pv(orderType="GTC", priceCents=19.0)
    assert any("marketable buy" in b for b in crossing["blockers"])
    resting = _pv(orderType="GTC", priceCents=9.0, size=12)
    assert not any("marketable buy" in b for b in resting["blockers"])


def test_a_dollar_or_more_passes(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)])
    _balance(monkeypatch, 50.0)
    assert not any("marketable buy" in b for b in _pv(size=6)["blockers"])
    assert not any("marketable buy" in b for b in _pv(action="sell")["blockers"])
