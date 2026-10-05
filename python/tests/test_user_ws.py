from __future__ import annotations

import asyncio

import user_ws


def _feed():
    f = user_ws.UserFeed()
    f.wake = asyncio.Event()
    f._api_key = "KEY"
    return f


def test_order_events_are_tracked_and_wake():
    f = _feed()
    assert f.handle({"event_type": "order", "id": "O1", "type": "UPDATE", "status": "MATCHED",
                     "size_matched": "5", "original_size": "10", "price": "0.52"})
    assert f.wake.is_set()
    assert f.order("O1")["sizeMatched"] == 5.0 and f.order("O1")["status"] == "MATCHED"


def test_trade_events_wake_only_for_our_orders():
    f = _feed()
    theirs = {"event_type": "trade", "id": "T1", "taker_order_id": "X", "trade_owner": "OTHER",
              "maker_orders": [{"order_id": "Y", "owner": "OTHER"}]}
    assert f.handle(theirs) is False and not f.wake.is_set()
    ours_as_maker = {"event_type": "trade", "id": "T2", "taker_order_id": "X", "trade_owner": "OTHER",
                     "status": "MATCHED", "size": "3", "price": "0.4",
                     "maker_orders": [{"order_id": "M1", "owner": "KEY", "matched_amount": "3"}]}
    assert f.handle(ours_as_maker) is True and f.wake.is_set()
    assert f.trades[-1]["orders"] == ["M1"]


def test_junk_and_pongs_are_ignored():
    f = _feed()
    for junk in ("PONG", 5, None, {"event_type": "price_change"}, [{"foo": 1}]):
        assert f.handle(junk) is False
    assert f.events == 0 and not f.wake.is_set()
