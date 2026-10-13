"""Tests for the real-time CLOB WebSocket feed (clob_ws.py).

Pure message parsing + the in-memory best-bid/ask cache (book maintenance,
one-sided updates, staleness, subscription). No network — the live protocol is
validated separately against the public feed.
"""
from __future__ import annotations

import asyncio
import json
import time

import websockets

import clob_ws



def test_best_from_levels_bid_is_max_ask_is_min():
    bids = [{"price": "0.10", "size": "5"}, {"price": "0.55", "size": "5"},
            {"price": "0.30", "size": "5"}]
    asks = [{"price": "0.62", "size": "5"}, {"price": "0.56", "size": "5"}]
    assert clob_ws._best_from_levels(bids, is_bid=True) == 0.55
    assert clob_ws._best_from_levels(asks, is_bid=False) == 0.56


def test_best_from_levels_skips_zero_size_and_empty():
    assert clob_ws._best_from_levels([{"price": "0.9", "size": "0"}], is_bid=True) is None
    assert clob_ws._best_from_levels([], is_bid=True) is None
    assert clob_ws._best_from_levels(None, is_bid=False) is None


def test_parse_book():
    msg = {
        "event_type": "book", "asset_id": "TOK",
        "bids": [{"price": "0.18", "size": "100"}, {"price": "0.17", "size": "50"}],
        "asks": [{"price": "0.19", "size": "100"}, {"price": "0.20", "size": "50"}],
    }
    assert clob_ws.parse_book(msg) == ("TOK", 0.18, 0.19)


def test_parse_book_no_asset_id():
    assert clob_ws.parse_book({"bids": [], "asks": []}) is None


def test_parse_price_changes_reads_server_best():
    msg = {"event_type": "price_change", "price_changes": [
        {"asset_id": "YES", "best_bid": "0.55", "best_ask": "0.56"},
        {"asset_id": "NO", "best_bid": "0.44", "best_ask": "0.45"},
    ]}
    assert clob_ws.parse_price_changes(msg) == [
        ("YES", 0.55, 0.56), ("NO", 0.44, 0.45),
    ]


def test_parse_price_changes_handles_missing_side():
    msg = {"price_changes": [{"asset_id": "YES", "best_bid": "0.55"}]}
    assert clob_ws.parse_price_changes(msg) == [("YES", 0.55, None)]


def test_iter_messages_single_array_and_garbage():
    assert clob_ws._iter_messages('{"a":1}') == [{"a": 1}]
    assert clob_ws._iter_messages('[{"a":1},{"b":2}]') == [{"a": 1}, {"b": 2}]
    assert clob_ws._iter_messages("not json") == []



def test_dispatch_book_then_price_change_updates_quote():
    f = clob_ws.ClobMarketFeed()
    f._dispatch({"event_type": "book", "asset_id": "T",
                 "bids": [{"price": "0.18", "size": "1"}],
                 "asks": [{"price": "0.19", "size": "1"}]})
    assert f.get_quote_cents("T") == {"bid_cents": 18, "ask_cents": 19}
    f._dispatch({"event_type": "price_change", "price_changes": [
        {"asset_id": "T", "best_bid": "0.40", "best_ask": "0.41"}]})
    assert f.get_quote_cents("T") == {"bid_cents": 40, "ask_cents": 41}


def test_one_sided_update_keeps_other_side():
    f = clob_ws.ClobMarketFeed()
    f._set("T", 0.40, 0.41)
    f._set("T", None, 0.43)
    assert f.get_quote_cents("T") == {"bid_cents": 40, "ask_cents": 43}


def test_a_book_snapshot_with_an_empty_side_clears_it():
    """A `book` is the whole book. An empty side in it means nothing rests
    there now; keeping the old price would show a bid that was pulled as if
    it were fresh (price_change, a partial update, still keeps the other side)."""
    f = clob_ws.ClobMarketFeed()
    f._dispatch({"event_type": "book", "asset_id": "T",
                 "bids": [{"price": "0.40", "size": "5"}],
                 "asks": [{"price": "0.41", "size": "5"}]})
    f._dispatch({"event_type": "book", "asset_id": "T",
                 "bids": [], "asks": [{"price": "0.45", "size": "5"}]})
    assert f.get_quote_cents("T") == {"bid_cents": None, "ask_cents": 45}
    assert f.quote_cents_if_fresh("T") is None


def test_stale_book_returns_none():
    f = clob_ws.ClobMarketFeed()
    f._set("T", 0.40, 0.41)
    f._books["T"]["ts"] = time.monotonic() - 100.0
    assert f.get_quote_cents("T") is None


def test_unknown_token_and_none():
    f = clob_ws.ClobMarketFeed()
    assert f.get_quote_cents("nope") is None
    assert f.get_quote_cents(None) is None



def test_quote_cents_if_fresh_returns_bid_ask():
    f = clob_ws.ClobMarketFeed()
    f._set("T", 0.40, 0.41)
    assert f.quote_cents_if_fresh("T") == (40, 41)


def test_quote_cents_if_fresh_none_when_stale():
    f = clob_ws.ClobMarketFeed()
    f._set("T", 0.40, 0.41)
    f._books["T"]["ts"] = time.monotonic() - 100.0
    assert f.quote_cents_if_fresh("T") is None


def test_quote_cents_if_fresh_scrubs_empty_book_sentinels():
    """bestBid 0 / bestAsk 1 is the venue saying "nothing rests here", not a
    price — crossing to a 100c "ask" would pay the cap into an empty book."""
    f = clob_ws.ClobMarketFeed()
    f._set("EMPTY", 0.0, 1.0)
    assert f.quote_cents_if_fresh("EMPTY") is None
    f._set("NO_ASKS", 0.30, 1.0)
    assert f.quote_cents_if_fresh("NO_ASKS") is None
    f._set("NO_BIDS", 0.0, 0.31)
    assert f.quote_cents_if_fresh("NO_BIDS") is None


def test_quote_cents_if_fresh_none_when_one_sided_or_crossed():
    f = clob_ws.ClobMarketFeed()
    f._set("ONE", 0.40, None)
    assert f.quote_cents_if_fresh("ONE") is None
    f._set("CROSSED", 0.60, 0.55)
    assert f.quote_cents_if_fresh("CROSSED") is None


def test_quote_cents_if_fresh_unknown_token_and_module_helper():
    f = clob_ws.ClobMarketFeed()
    assert f.quote_cents_if_fresh("nope") is None
    assert f.quote_cents_if_fresh(None) is None
    assert clob_ws.quote_cents_if_fresh(None) is None


def test_module_quote_helper_never_raises(monkeypatch):
    class _Boom:
        def quote_cents_if_fresh(self, *_a, **_kw):
            raise RuntimeError("feed exploded")

    monkeypatch.setattr(clob_ws, "feed", _Boom())
    assert clob_ws.quote_cents_if_fresh("T") is None


def test_observe_adds_tokens_and_flags_resub():
    f = clob_ws.ClobMarketFeed()
    f.observe("A", "B", None)
    assert set(f._tokens) == {"A", "B"}
    assert f._resub.is_set()
    f._resub.clear()
    f.observe("A")
    assert not f._resub.is_set()
    f.observe("C")
    assert f._resub.is_set()


def test_observe_evicts_expired_window_tokens():
    """Recurring up/down windows mint ~4k tokens/day on the 5m series; tokens
    no longer observed must age out or the subscribe frame grows unbounded
    (a rejected oversized subscription silently kills live-book pricing)."""
    f = clob_ws.ClobMarketFeed()
    f.observe("OLD")
    f._tokens["OLD"] -= (f._TOKEN_TTL + 1)
    f._books["OLD"] = {"bid": 0.5, "ask": 0.51, "ts": 0.0}
    f._resub.clear()
    f.observe("NEW")
    assert "OLD" not in f._tokens
    assert "OLD" not in f._books
    assert "NEW" in f._tokens
    assert f._resub.is_set()
    f._resub.clear()
    f.observe("NEW")
    assert "NEW" in f._tokens and not f._resub.is_set()


def test_books_cap_holds_the_multi_engine_working_set():
    """The book cache is swept only when it outgrows _BOOKS_MAX, and only of
    tokens NOBODY observes. 64 was sized for crypto15m's two-tokens-per-window
    alone; the main bot and the copy engine now warm the markets they are about
    to price, so a 64 ceiling swept books they were still reading and silently
    pushed those orders back onto the REST quote."""
    assert clob_ws.ClobMarketFeed._BOOKS_MAX >= 256
    f = clob_ws.ClobMarketFeed()
    for i in range(200):
        f._set(f"T{i}", 0.40, 0.41)
    f.observe("NEW")
    assert len(f._books) == 200


def test_books_cap_sweeps_only_unobserved_tokens():
    f = clob_ws.ClobMarketFeed()
    f.observe("KEEP")
    f._set("KEEP", 0.40, 0.41)
    for i in range(clob_ws.ClobMarketFeed._BOOKS_MAX + 10):
        f._set(f"DEAD{i}", 0.40, 0.41)
    f.observe("NEW")
    assert f.quote_cents_if_fresh("KEEP") == (40, 41)
    assert "DEAD0" not in f._books



def _book(aid, bid="0.40", ask="0.41"):
    return json.dumps({"event_type": "book", "asset_id": aid,
                       "bids": [{"price": bid, "size": "5"}],
                       "asks": [{"price": ask, "size": "5"}]})


class _FakeWS:
    """Serves a script of inbound frames (a str, or a callable run at that
    point that returns one), records what the client sends, and stops the feed
    once the script runs out."""

    def __init__(self, feed, script, *, fail_ops=False):
        self.feed, self.script, self.fail_ops = feed, list(script), fail_ops
        self.sent: list[str] = []

    async def send(self, msg):
        if self.fail_ops and '"operation"' in msg:
            raise ConnectionError("socket went away")
        self.sent.append(msg)

    async def recv(self):
        await asyncio.sleep(0)
        if not self.script:
            self.feed._stop = True
            return "PONG"
        nxt = self.script.pop(0)
        return nxt() if callable(nxt) else nxt

    def frames(self):
        return [json.loads(s) for s in self.sent if s != "PING"]


def test_a_new_token_is_subscribed_on_the_open_socket(monkeypatch):
    f = clob_ws.ClobMarketFeed()
    f.observe("A")
    connects = []

    def _new_window():
        f.observe("B")
        return _book("A")

    ws = _FakeWS(f, [_new_window, _book("B", "0.60", "0.62")])

    class _Connect:
        def __init__(self, *_a, **_k):
            connects.append(1)

        async def __aenter__(self):
            return ws

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(websockets, "connect", _Connect)
    asyncio.run(f._run())
    assert len(connects) == 1
    assert ws.frames() == [
        {"assets_ids": ["A"], "type": "market", "custom_feature_enabled": True},
        {"assets_ids": ["B"], "operation": "subscribe", "custom_feature_enabled": True},
    ]
    assert f.quote_cents_if_fresh("B") == (60, 62)


def _subscribed_feed(*tokens):
    f = clob_ws.ClobMarketFeed()
    f.observe(*tokens)
    f._subscribed = set(tokens)
    f._resub.clear()
    return f


def test_an_expired_token_is_unsubscribed_on_the_open_socket():
    f = _subscribed_feed("A", "OLD")

    def _expire():
        f._tokens["OLD"] -= f._TOKEN_TTL + 1
        f.observe("A")
        return _book("A")

    ws = _FakeWS(f, [_expire, _book("A")])
    asyncio.run(f._recv_loop(ws))
    assert f._stop is True
    assert ws.frames() == [{"assets_ids": ["OLD"], "operation": "unsubscribe"}]


def test_a_subscribe_the_server_never_answers_falls_back_to_reconnect():
    f = _subscribed_feed("A")

    def _new():
        f.observe("B")
        return _book("A")

    def _long_silence_for_b():
        f._pending_subs["B"] -= clob_ws._SUB_CONFIRM_SEC + 1
        return _book("A")

    ws = _FakeWS(f, [_new, _long_silence_for_b, _book("A"), _book("A")])
    asyncio.run(f._recv_loop(ws))
    assert f._stop is False
    assert ws.script


def test_a_subscription_change_that_cannot_be_sent_falls_back_to_reconnect():
    f = _subscribed_feed("A")

    def _new():
        f.observe("B")
        return _book("A")

    ws = _FakeWS(f, [_new, _book("A"), _book("A")], fail_ops=True)
    asyncio.run(f._recv_loop(ws))
    assert f._stop is False and ws.script
