from __future__ import annotations

import time

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
