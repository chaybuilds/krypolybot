from __future__ import annotations

import asyncio

import pytest

import db
import terminal as t
import terminal_trade as tt


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "terminal.db")
    db.init_db()
    return tmp_path


def test_num_returns_none_for_unknowable_input():
    for bad in (None, "", "abc", [], {}, float("nan"), float("inf")):
        assert t.num(bad) is None, f"{bad!r} was coerced to a number"


def test_num_preserves_a_real_zero():
    assert t.num(0) == 0.0
    assert t.num("0") == 0.0
    assert t.num(0.0) == 0.0


def test_num_rejects_bools():
    assert t.num(True) is None
    assert t.num(False) is None


def test_cents_scales_and_preserves_none():
    assert t.cents(0.42) == 42.0
    assert t.cents(1) == 100.0
    assert t.cents(None) is None
    assert t.cents("") is None


def test_row_put_ignores_none():
    r = t._Row()
    r.put("probability", 42.0, "gamma", t.PRIO_BEST_SOURCE)
    r.put("probability", None, "clob-ws", t.PRIO_OWN_FEED)
    assert r.get("probability") == 42.0, "a None erased a real value"
    assert r.sources["probability"]["source"] == "gamma"


def test_row_put_prefers_better_priority():
    r = t._Row()
    r.put("bid", 40.0, "gamma", t.PRIO_FALLBACK)
    r.put("bid", 41.0, "clob-ws", t.PRIO_OWN_FEED)
    assert r.get("bid") == 41.0
    assert r.sources["bid"]["source"] == "clob-ws"


def test_row_put_does_not_downgrade():
    r = t._Row()
    r.put("bid", 41.0, "clob-ws", t.PRIO_OWN_FEED)
    r.put("bid", 40.0, "gamma", t.PRIO_FALLBACK)
    assert r.get("bid") == 41.0, "a worse provider overwrote a better one"


def test_unknown_reason_is_dropped_once_the_field_is_known():
    r = t._Row()
    r.unknown("liquidity", "Gamma reported nothing.")
    assert r.unknowns and r.unknowns[0]["field"] == "liquidity"
    r2 = t._Row()
    r2.put("liquidity", 10.0, "gamma", t.PRIO_BEST_SOURCE)
    r2.unknown("liquidity", "should not appear")
    assert not r2.unknowns


def test_empty_book_sentinels_are_not_prices():
    raw = {
        "conditionId": "0xabc", "question": "Just listed?",
        "active": True, "closed": False,
        "bestBid": 0, "bestAsk": 1, "outcomePrices": None,
    }
    m = t.summary_from_gamma(raw)
    assert m["bid"] is None, "0c was reported as a real bid"
    assert m["ask"] is None, "100c was reported as a real ask"
    assert m["spread"] is None, "an empty book produced a spread"
    assert any(u["field"] in ("bid", "ask") for u in m["unknowns"]),         "the missing side was not explained"


def test_real_quotes_survive_the_sentinel_filter():
    raw = {
        "conditionId": "0xabc", "question": "Live?",
        "active": True, "closed": False,
        "bestBid": 0.4, "bestAsk": 0.42,
        "outcomePrices": '["0.41", "0.59"]',
    }
    m = t.summary_from_gamma(raw)
    assert m["bid"] == 40.0 and m["ask"] == 42.0
    assert abs(m["spread"] - 2.0) < 1e-9
    assert abs(m["mid"] - 41.0) < 1e-9


def test_a_settled_market_reports_its_outcome():
    raw = {
        "conditionId": "0xabc", "question": "Done?",
        "active": False, "closed": True,
        "outcomePrices": '["1", "0"]', "umaResolutionStatus": "resolved",
    }
    m = t.summary_from_gamma(raw)
    assert m["resolvedOutcome"] == "yes"
    assert m["status"] == "settled"


ROWS = [
    {"title": "known-pass", "volume24hUsd": 5000.0, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": "crypto"},
    {"title": "known-fail", "volume24hUsd": 10.0, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": "crypto"},
    {"title": "unknown-vol", "volume24hUsd": None, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": "crypto"},
    {"title": "unknown-cat", "volume24hUsd": 5000.0, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": None},
]


def test_numeric_filter_skips_unknown_rather_than_failing_it():
    kept, skipped, excluded = t.apply_filters(ROWS, {"minVolume24hUsd": 1000})
    titles = {r["title"] for r in kept}
    assert titles == {"known-pass", "unknown-cat"}
    assert skipped == 2
    assert excluded == 0


def test_categorical_filter_excludes_unknown():
    kept, skipped, excluded = t.apply_filters(ROWS, {"categories": ["crypto"]})
    titles = {r["title"] for r in kept}
    assert titles == {"known-pass", "known-fail", "unknown-vol"}
    assert excluded == 1, "an uncategorised row survived a category filter"
    assert skipped == 0


def test_categorical_and_numeric_counters_do_not_double_count():
    kept, skipped, excluded = t.apply_filters(
        ROWS, {"categories": ["crypto"], "minVolume24hUsd": 1000})
    assert [r["title"] for r in kept] == ["known-pass"]
    assert excluded == 1
    assert skipped == 2
    assert len(kept) + skipped + excluded == len(ROWS)


def test_no_filters_keeps_everything():
    kept, skipped, excluded = t.apply_filters(ROWS, None)
    assert len(kept) == len(ROWS) and skipped == 0 and excluded == 0


def test_probability_bounds_skip_unpriced_rows():
    rows = [{"probability": None, "category": "x"}, {"probability": 80.0, "category": "x"}]
    kept, skipped, _ = t.apply_filters(rows, {"minProbability": 50})
    assert len(kept) == 1 and skipped == 1


def test_history_is_sorted_and_deduped():
    pts = t._dedupe_points([
        {"t": 3, "p": 30.0}, {"t": 1, "p": 10.0},
        {"t": 3, "p": 33.0}, {"t": 2, "p": 20.0},
    ])
    assert [p["t"] for p in pts] == [1, 2, 3]
    assert pts[-1]["p"] == 33.0, "the later duplicate should win"


def test_history_drops_unparseable_points_without_inventing_one():
    pts = t._dedupe_points(
        [{"t": 1, "p": None}, {"t": None, "p": 5.0}, {"t": 2, "p": 20.0}])
    assert pts == [{"t": 2, "p": 20.0}]


def test_book_levels_collapse_duplicate_prices_and_accumulate():
    levels = t._levels(
        [{"price": "0.51", "size": "10"},
         {"price": "0.51", "size": "5"},
         {"price": "0.50", "size": "20"}],
        descending=True)
    assert [l["priceCents"] for l in levels] == [51.0, 50.0]
    assert levels[0]["size"] == 15.0, "duplicate levels were not summed"
    assert levels[1]["cumulative"] == 35.0
    assert levels[0]["notionalUsd"] == 7.65


def test_book_levels_ignore_zero_and_malformed_rows():
    levels = t._levels(
        [{"price": "0.5", "size": "0"}, {"price": "x", "size": "1"},
         {"nope": 1}, {"price": "0.4", "size": "3"}],
        descending=True)
    assert len(levels) == 1 and levels[0]["priceCents"] == 40.0


def test_fee_uses_the_markets_own_schedule_over_the_category_table():
    fee = tt._fee_per_share(50.0, "crypto", {"enabled": True, "rate": 0.02, "exponent": 1})
    assert abs(fee - 0.02 * 0.25) < 1e-9


def test_fee_respects_an_explicitly_fee_free_market():
    assert tt._fee_per_share(50.0, "crypto", {"enabled": False}) == 0.0


def test_fee_is_none_when_we_cannot_determine_it():
    assert tt._fee_per_share(50.0, None, None) is None
    assert tt._fee_per_share(50.0, "not-a-real-category", None) is None


def test_fee_curve_matches_the_verified_v2_rate():
    fee = tt._fee_per_share(50.0, "crypto", None)
    assert abs(fee - 0.0175) < 1e-9


def test_fee_is_symmetric_and_vanishes_at_the_extremes():
    assert abs(tt._fee_per_share(20.0, "crypto", None)
               - tt._fee_per_share(80.0, "crypto", None)) < 1e-9
    assert tt._fee_per_share(1.0, "crypto", None) < 0.001


def test_walking_an_unreadable_book_reports_unknown_not_zero():
    assert tt._walk_book([], 10, 50, is_buy=True) == (None, None)


def test_walking_a_book_with_nothing_in_range_reports_zero():
    levels = [{"priceCents": 60.0, "size": 100.0}]
    fillable, avg = tt._walk_book(levels, 10, 50, is_buy=True)
    assert fillable == 0.0 and avg is None


def test_walk_stops_at_the_limit_price_when_buying():
    levels = [{"priceCents": 50.0, "size": 4.0},
              {"priceCents": 51.0, "size": 6.0},
              {"priceCents": 60.0, "size": 100.0}]
    fillable, avg = tt._walk_book(levels, 20, 51, is_buy=True)
    assert fillable == 10.0
    assert abs(avg - 50.6) < 1e-9


def test_walk_stops_at_the_limit_price_when_selling():
    levels = [{"priceCents": 50.0, "size": 4.0},
              {"priceCents": 49.0, "size": 6.0},
              {"priceCents": 40.0, "size": 100.0}]
    fillable, avg = tt._walk_book(levels, 20, 49, is_buy=False)
    assert fillable == 10.0
    assert abs(avg - 49.4) < 1e-9


def test_walk_never_reports_more_than_requested():
    levels = [{"priceCents": 50.0, "size": 1000.0}]
    fillable, avg = tt._walk_book(levels, 7, 50, is_buy=True)
    assert fillable == 7.0 and avg == 50.0


def _seed_fills(rows):
    with db.get_db() as conn:
        conn.execute("DELETE FROM terminal_fills")
        for i, r in enumerate(rows):
            conn.execute(
                """INSERT INTO terminal_fills
                   (trade_id, order_id, condition_id, title, side, action,
                    size, price_cents, fees_usd, cost_usd, origin, network,
                    filled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"t{i}", None, "0xdead", "T", r.get("side", "yes"),
                 r["action"], r["size"], r["price"], r.get("fee", 0.0),
                 r["size"] * r["price"] / 100.0, "manual", "mainnet",
                 f"2026-01-0{i + 1}T00:00:00Z"))


def test_average_cost_across_two_buys(fresh_db):
    _seed_fills([
        {"action": "buy", "size": 100, "price": 40.0},
        {"action": "buy", "size": 100, "price": 60.0},
    ])
    st = tt._ledger_basis()[("0xdead", "yes")]
    assert st["size"] == 200
    assert abs(st["cost"] / st["size"] * 100 - 50.0) < 1e-9


def test_a_sell_reduces_size_and_leaves_the_average_alone(fresh_db):
    _seed_fills([
        {"action": "buy", "size": 100, "price": 40.0},
        {"action": "buy", "size": 100, "price": 60.0},
        {"action": "sell", "size": 100, "price": 90.0},
    ])
    st = tt._ledger_basis()[("0xdead", "yes")]
    assert st["size"] == 100
    assert abs(st["cost"] / st["size"] * 100 - 50.0) < 1e-6, "the sell moved the average"
    assert abs(st["realized"] - 100 * (0.90 - 0.50)) < 1e-6


def test_an_unknown_fee_marks_the_whole_group_unknown(fresh_db):
    _seed_fills([{"action": "buy", "size": 10, "price": 50.0, "fee": 0.1}])
    with db.get_db() as conn:
        conn.execute("UPDATE terminal_fills SET fees_usd=NULL WHERE trade_id='t0'")
    st = tt._ledger_basis()[("0xdead", "yes")]
    assert st["fees_known"] is False


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_empty_watchlist_says_it_is_empty(monkeypatch):
    monkeypatch.setattr(t, "watchlist_list", lambda: [])
    r = _run(t.discover("watchlist"))
    assert "Nothing on your watchlist yet" in r["note"]


def test_unloadable_watchlist_does_not_claim_to_be_empty(monkeypatch):
    monkeypatch.setattr(
        t, "watchlist_list",
        lambda: [{"conditionId": "0xdead", "title": "x", "addedAt": "", "note": None}])

    async def _none(_ids):
        return []
    monkeypatch.setattr(t, "_summaries_for", _none)

    r = _run(t.discover("watchlist"))
    assert r["rows"] == []
    assert "Nothing on your watchlist yet" not in r["note"]
    assert "watchlist is intact" in r["note"]


def test_partially_loadable_watchlist_reports_the_shortfall(monkeypatch):
    monkeypatch.setattr(t, "watchlist_list", lambda: [
        {"conditionId": "0xa", "title": "a", "addedAt": "", "note": None},
        {"conditionId": "0xb", "title": "b", "addedAt": "", "note": None},
    ])

    async def _one(_ids):
        return [{"conditionId": "0xa", "title": "a", "category": "crypto",
                 "probability": 50.0, "sources": {}, "unknowns": [],
                 "volume24hUsd": None, "liquidityUsd": None,
                 "hoursToClose": None}]
    monkeypatch.setattr(t, "_summaries_for", _one)

    r = _run(t.discover("watchlist"))
    assert len(r["rows"]) == 1
    assert "1 of your 2 watched market(s) could not be loaded" in r["note"]


def _cursor_pages(pages):
    calls = {"n": 0}

    async def _req(method, path, *, params=None, **kw):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(pages):
            return {"data": [], "next_cursor": "LTE="}
        last = i == len(pages) - 1
        return {"data": pages[i], "next_cursor": "LTE=" if last else f"c{i + 1}"}
    return _req, calls


def test_fill_walk_follows_the_cursor_to_the_end(monkeypatch):
    pages = [[{"id": "a"}], [{"id": "b"}], [{"id": "c"}]]
    req, calls = _cursor_pages(pages)
    monkeypatch.setattr(tt.api, "_authed_request", req)
    rows, complete = _run(tt._walk_trades())
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert complete is True
    assert calls["n"] == 3


def test_fill_walk_reports_incomplete_when_it_hits_the_page_cap(monkeypatch):
    async def _endless(method, path, *, params=None, **kw):
        return {"data": [{"id": "x"}], "next_cursor": f"c{params.get('next_cursor', '0')}z"}
    monkeypatch.setattr(tt.api, "_authed_request", _endless)
    rows, complete = _run(tt._walk_trades(max_pages=4))
    assert complete is False, "a truncated walk must not report success"
    assert len(rows) == 4


def test_fill_walk_stops_on_a_repeated_cursor(monkeypatch):
    async def _stuck(method, path, *, params=None, **kw):
        return {"data": [{"id": "x"}], "next_cursor": "same"}
    monkeypatch.setattr(tt.api, "_authed_request", _stuck)
    rows, complete = _run(tt._walk_trades(max_pages=20))
    assert complete is True and len(rows) == 2


def test_fill_walk_accepts_the_legacy_list_shape(monkeypatch):
    async def _list(method, path, *, params=None, **kw):
        return [{"id": "a"}, {"id": "b"}]
    monkeypatch.setattr(tt.api, "_authed_request", _list)
    rows, complete = _run(tt._walk_trades())
    assert len(rows) == 2 and complete is True


def _trade(tid, *, ts, taker="", makers=(), price="0.60", size="10"):
    return {"id": tid, "market": "0xabc", "side": "BUY", "outcome": "Yes",
            "price": price, "size": size, "match_time": str(int(ts)),
            "taker_order_id": taker, "maker_orders": list(makers)}


def _reconcile(monkeypatch, trades):
    async def _req(method, path, *, params=None, **kw):
        return {"data": list(trades), "next_cursor": "LTE="}

    async def _summaries(_ids):
        return [{"conditionId": "0xabc", "title": "T", "category": "crypto",
                 "_raw": {"feesEnabled": False}}]

    monkeypatch.setattr(tt.api, "_authed_request", _req)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    return _run(tt.reconcile_fills())


def _fills():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM terminal_fills ORDER BY trade_id")]


def test_a_wipe_stops_history_being_re_imported_as_someone_elses(
        monkeypatch, fresh_db):
    import time as _time
    db.clear_trade_history()
    r = _reconcile(monkeypatch, [
        _trade("pre", ts=_time.time() - 86_400 * 30),
        _trade("post", ts=_time.time()),
    ])
    assert [f["trade_id"] for f in _fills()] == ["post"]
    assert r["skipped"] == 1
    assert "cleared your stats" in (r["note"] or "")


def test_with_no_wipe_the_whole_history_still_reconciles(monkeypatch, fresh_db):
    import time as _time
    r = _reconcile(monkeypatch,
                   [_trade("ancient", ts=_time.time() - 86_400 * 900)])
    assert r["skipped"] == 0
    assert [f["trade_id"] for f in _fills()] == ["ancient"]


def test_an_unattributable_fill_has_an_unknown_fee_not_a_zero_one(
        monkeypatch, fresh_db):
    import time as _time
    now = _time.time()
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO terminal_orders
                 (condition_id, title, side, action, order_type,
                  requested_size, requested_price_cents, order_id, status,
                  origin, network)
               VALUES ('0xabc','T','yes','buy','GTC',10,60,'oid_mk','placed',
                       'manual','mainnet')""")
    _reconcile(monkeypatch, [
        _trade("ext", ts=now, taker="someone_elses_order"),
        _trade("mine", ts=now, taker="their_taker", makers=["oid_mk"]),
    ])

    by_id = {f["trade_id"]: f for f in _fills()}
    assert by_id["ext"]["fees_usd"] is None, "an unknown fee was recorded as 0"
    assert by_id["ext"]["origin"] == "external"
    assert by_id["mine"]["fees_usd"] == 0.0
    assert by_id["mine"]["origin"] == "manual"


def _fake_portfolio(monkeypatch, *, resolved, side, redeemable=False, cur_price=0.97):
    async def _positions(*a, **kw):
        return [{
            "ticker": "0xabc", "title": "Did it happen?",
            "position_fp": 100.0 if side == "yes" else -100.0,
            "cur_price": cur_price, "redeemable": redeemable,
        }]

    async def _summaries(_ids):
        return [{
            "conditionId": "0xabc", "title": "Did it happen?", "slug": "s",
            "status": "settled" if resolved else "open",
            "resolvedOutcome": resolved, "closeTime": "2026-01-01T00:00:00Z",
        }]

    async def _balance():
        raise RuntimeError("no wallet in tests")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)


def test_a_won_settled_position_marks_at_exactly_100c(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved="yes", side="yes", cur_price=0.97)
    p = _run(tt.portfolio())["positions"][0]
    assert p["settled"] is True
    assert p["markCents"] == 100.0, "a decided winner is worth exactly $1, not the last mark"
    assert p["status"] == "settled"
    assert p["resolvedOutcome"] == "yes"


def test_a_lost_settled_position_marks_at_exactly_0c(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved="no", side="yes", cur_price=0.03)
    p = _run(tt.portfolio())["positions"][0]
    assert p["markCents"] == 0.0
    assert p["valueUsd"] == 0.0


def test_an_open_position_keeps_its_mark_and_is_not_called_settled(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved=None, side="yes", cur_price=0.62)
    p = _run(tt.portfolio())["positions"][0]
    assert p["settled"] is False
    assert p["markCents"] == 62.0
    assert p["status"] == "open"


def test_redeemable_positions_are_counted_and_explained(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved="yes", side="yes", redeemable=True)
    r = _run(tt.portfolio())
    assert r["redeemableCount"] == 1
    assert r["positions"][0]["redeemable"] is True
    assert "redeem" in (r["redeemableNote"] or "").lower()


def test_an_unreadable_wallet_is_flagged_not_reported_as_empty(
        monkeypatch, fresh_db):
    async def _boom(*_a, **_kw):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(tt.api, "get_positions", _boom)
    r = _run(tt.portfolio())
    assert r["positions"] == []
    assert r["walletReadable"] is False
    assert "could not read" in (r["unreconciledNote"] or "").lower()


def test_a_wallet_that_answers_is_marked_readable(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved=None, side="yes", cur_price=0.62)
    assert _run(tt.portfolio())["walletReadable"] is True


def test_a_settled_position_with_no_basis_still_reports_unknown_pnl(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved="yes", side="yes")
    p = _run(tt.portfolio())["positions"][0]
    assert p["markCents"] == 100.0
    assert p["avgCostCents"] is None
    assert p["unrealizedPnlUsd"] is None
    assert p["unreconciled"] is True


def test_offline_mode_makes_no_requests(monkeypatch):
    called = {"n": 0}

    async def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("offline mode still hit the network")
    monkeypatch.setattr(t.api, "_get_client", _boom)

    t.set_offline(True)
    try:
        r = _run(t.discover("volume"))
    finally:
        t.set_offline(False)
    assert r["rows"] == []
    assert called["n"] == 0


def test_offline_reason_beats_a_column_specific_note(monkeypatch):
    t.set_offline(True)
    try:
        for column in ("new", "volatility", "volume", "closing"):
            r = _run(t.discover(column))
            assert r["rows"] == []
            assert r["note"] == t.OFFLINE_REASON, (
                f"{column} explained itself with something other than the real "
                f"reason: {r['note']!r}")
    finally:
        t.set_offline(False)


def test_offline_mode_does_not_serve_stale_cache(monkeypatch):
    key = t._cache_key("gamma", "/markets", {"limit": 1})
    t._cache[key] = (t.time.monotonic() + 999, [{"conditionId": "0x1"}], t._now_iso())
    t.set_offline(True)
    try:
        data, _ = _run(t._fetch("gamma", "/markets", {"limit": 1}))
    finally:
        t.set_offline(False)
        t._cache.pop(key, None)
    assert data is None


def test_health_reports_the_switch_state():
    t.set_offline(True)
    try:
        assert t.health()["offline"] is True
    finally:
        t.set_offline(False)
    assert t.health()["offline"] is False


def test_a_thin_all_or_none_order_warns_rather_than_blocks(monkeypatch):
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "orderMinSize": 1,
                      "acceptingOrders": True, "feesEnabled": False}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": True, "bids": [], "note": None,
                "asks": [{"priceCents": 50.0, "size": 3, "cumulative": 3,
                          "notionalUsd": 1.5}]}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.api, "_deposit_wallet_issue", lambda: "")

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 55, "size": 10,
                          "orderType": "FOK"}))
    assert not pv["blockers"], f"a thin FOK was blocked: {pv['blockers']}"
    assert any("killed" in w for w in pv["warnings"])


def _thin_book(monkeypatch, *, asks=(), bids=()):
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "orderMinSize": 1,
                      "acceptingOrders": True, "feesEnabled": False}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": True, "note": None,
                "asks": [{"priceCents": p, "size": s} for p, s in asks],
                "bids": [{"priceCents": p, "size": s} for p, s in bids]}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.api, "_deposit_wallet_issue", lambda: "")


def test_a_fak_costs_what_it_can_actually_spend(monkeypatch):
    _thin_book(monkeypatch, asks=[(60.0, 3)])
    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 65, "size": 100,
                          "orderType": "FAK"}))
    assert pv["fillableSize"] == 3
    assert pv["costUsd"] == 1.80
    assert pv["feeUsd"] == 0.0
    assert pv["maxPayoutUsd"] == 3.0
    assert pv["maxLossUsd"] == 1.80


def test_a_gtc_costs_its_real_worst_case(monkeypatch):
    _thin_book(monkeypatch, asks=[(60.0, 3)])
    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 65, "size": 100,
                          "orderType": "GTC"}))
    assert pv["costUsd"] == 64.85
    assert pv["maxPayoutUsd"] == 100.0
    assert pv["maxLossUsd"] == 64.85


def test_an_order_that_crosses_nothing_costs_nothing_only_if_it_is_killed(monkeypatch):
    _thin_book(monkeypatch, asks=[(70.0, 500)])
    fak = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "buy", "priceCents": 65, "size": 100,
                           "orderType": "FAK"}))
    assert fak["fillableSize"] == 0.0
    assert fak["costUsd"] == 0.0 and fak["maxLossUsd"] == 0.0
    gtc = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "buy", "priceCents": 65, "size": 100,
                           "orderType": "GTC"}))
    assert gtc["costUsd"] == 65.00


def test_sell_proceeds_follow_the_same_rule(monkeypatch):
    _thin_book(monkeypatch, bids=[(60.0, 3)])
    fak = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "sell", "priceCents": 55, "size": 100,
                           "orderType": "FAK"}))
    assert fak["costUsd"] == 1.80
    assert fak["maxPayoutUsd"] == 1.80
    assert fak["maxLossUsd"] == 1.20
    gtc = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "sell", "priceCents": 55, "size": 100,
                           "orderType": "GTC"}))
    assert gtc["costUsd"] == 55.15


def test_an_unreadable_book_prices_the_whole_order_at_the_limit(monkeypatch):
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "orderMinSize": 1,
                      "acceptingOrders": True, "feesEnabled": False}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": False, "bids": [], "asks": [], "note": "no book"}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.api, "_deposit_wallet_issue", lambda: "")

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 65, "size": 100,
                          "orderType": "FAK"}))
    assert pv["fillableSize"] is None
    assert pv["costUsd"] == 65.00


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
        if positions is None:
            raise RuntimeError("data api down")
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


def test_a_stop_retires_itself_when_the_position_is_gone(monkeypatch):
    placed = []
    entries = _standing(monkeypatch, positions=[], placed=placed)
    r = _run(tt.standing_tick(allow_trading=True))
    assert placed == [], "a stop fired against a position that no longer exists"
    assert entries[0]["armed"] is False
    assert "no longer hold" in entries[0]["note"]
    assert r["alerts"] and r["alerts"][0]["blocked"] is True


def test_a_stop_holds_fire_when_positions_cannot_be_read(monkeypatch):
    placed = []
    entries = _standing(monkeypatch, positions=None, placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert placed == []
    assert entries[0]["armed"] is True, "an API blip disarmed a live stop"
    assert entries[0]["firedAt"] is None
    assert "could not be read" in entries[0]["note"]


def test_a_stop_sells_only_what_is_actually_held(monkeypatch):
    placed = []
    entries = _standing(
        monkeypatch,
        positions=[{"ticker": "0xabc", "position_fp": 30.0}],
        placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == 1
    assert placed[0]["size"] == 30.0, "sold more than the wallet holds"
    assert "only 30" in entries[0]["note"]


def test_a_stop_fires_normally_when_the_position_is_intact(monkeypatch):
    placed = []
    entries = _standing(
        monkeypatch,
        positions=[{"ticker": "0xabc", "position_fp": 100.0}],
        placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == 1 and placed[0]["size"] == 100.0
    assert entries[0]["armed"] is False and entries[0]["firedAt"]


def test_a_no_side_stop_reads_the_no_holding(monkeypatch):
    placed = []
    entries = _standing(
        monkeypatch,
        positions=[{"ticker": "0xabc", "position_fp": -100.0}],
        placed=placed)
    entries[0]["side"] = "no"
    _run(tt.standing_tick(allow_trading=True))
    assert placed == []
    entries[0]["triggerCents"] = 80.0
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == 1 and placed[0]["side"] == "no"


def test_an_alert_does_not_need_a_position(monkeypatch):
    placed = []
    entries = _standing(monkeypatch, positions=[], placed=placed)
    entries[0]["kind"] = "alertBelow"
    entries[0]["size"] = None
    r = _run(tt.standing_tick(allow_trading=True))
    assert r["fired"] == 1
    assert placed == [], "an alert placed an order"


def _standing_store(monkeypatch, entries):
    import copy
    store = {"v": copy.deepcopy(entries)}
    monkeypatch.setattr(tt, "standing_list", lambda: copy.deepcopy(store["v"]))
    monkeypatch.setattr(
        tt, "_standing_put", lambda e: store.__setitem__("v", copy.deepcopy(e)))
    return store


def _stop_entry(**over):
    e = {
        "id": "st_1", "conditionId": "0xabc", "title": "T", "side": "yes",
        "kind": "stopLoss", "triggerCents": 40.0, "size": 100,
        "createdAt": "", "armed": True, "lastCheckedAt": None,
        "firedAt": None, "note": None,
    }
    e.update(over)
    return e


def test_a_stop_cancelled_during_a_tick_is_not_resurrected(monkeypatch):
    store = _standing_store(monkeypatch, [_stop_entry()])

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": 30.0}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _preview(req):
        await asyncio.sleep(0)
        tt.standing_cancel("st_1")
        return {"costUsd": 1.0}
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place(req, **kw):
        return {"ok": True, "message": "sold"}
    monkeypatch.setattr(tt, "place", _place)

    _run(tt.standing_tick(allow_trading=True))
    assert store["v"] == [], f"a cancelled instruction came back: {store['v']}"


def test_a_stop_disarmed_during_a_tick_stays_disarmed(monkeypatch):
    store = _standing_store(monkeypatch, [_stop_entry()])

    async def _market(cid, include_raw=False):
        await asyncio.sleep(0)
        tt.standing_set_armed("st_1", False)
        return {"conditionId": cid, "title": "T", "probability": 90.0}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    _run(tt.standing_tick(allow_trading=True))
    assert store["v"][0]["armed"] is False, "a disarmed stop was re-armed"
    assert store["v"][0]["lastCheckedAt"] is not None


def test_an_instruction_added_during_a_tick_survives_it(monkeypatch):
    store = _standing_store(monkeypatch, [_stop_entry()])

    async def _market(cid, include_raw=False):
        await asyncio.sleep(0)
        if not any(e["id"] == "st_2" for e in store["v"]):
            store["v"].append(_stop_entry(id="st_2", conditionId="0xdef"))
        return {"conditionId": cid, "title": "T", "probability": 90.0}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    _run(tt.standing_tick(allow_trading=True))
    assert sorted(e["id"] for e in store["v"]) == ["st_1", "st_2"]


def test_opposite_legs_are_not_treated_as_the_same_question():
    a = "Will the Fed increase interest rates by 25 bps in September?"
    b = "Will the Fed decrease interest rates by 25 bps in September?"
    assert t._are_opposites(a, b)
    assert t._title_overlap(a, b) == 0.0


def test_rephrasings_of_the_same_question_still_match():
    a = "Will Trump win the 2028 election?"
    b = "Will Donald Trump win the 2028 election?"
    assert not t._are_opposites(a, b)
    assert t._title_overlap(a, b) > 0.7


def test_a_shared_updown_phrase_is_not_an_opposite():
    a = "Bitcoin Up or Down - 9:00PM"
    b = "Bitcoin Up or Down - 9:05PM"
    assert not t._are_opposites(a, b)


def test_a_negrisk_group_never_reports_a_contradicting_sibling(monkeypatch):
    async def _event_markets(_slug):
        return [
            {"conditionId": "0xa", "title": "Outcome A", "probability": 70.0},
            {"conditionId": "0xb", "title": "Outcome A variant", "probability": 20.0},
        ]
    monkeypatch.setattr(t, "_event_markets", _event_markets)
    c = _run(t._sibling_check({
        "conditionId": "0xme", "title": "Outcome A variant two",
        "probability": 70.0, "eventSlug": "e", "negRisk": True}))
    assert c["level"] == "ok"
    assert "supposed to disagree" in c["detail"]


def test_a_standalone_market_still_reports_a_real_contradiction(monkeypatch):
    async def _event_markets(_slug):
        return [{"conditionId": "0xb", "title": "Will Trump win the election",
                 "probability": 20.0}]
    monkeypatch.setattr(t, "_event_markets", _event_markets)
    c = _run(t._sibling_check({
        "conditionId": "0xme", "title": "Will Donald Trump win the election",
        "probability": 70.0, "eventSlug": "e", "negRisk": False}))
    assert c["level"] == "risk"
    assert "50.0¢ away" in c["detail"]


def test_negrisk_group_sum_counts_each_leg_exactly_once(monkeypatch):
    legs = [
        {"conditionId": "0xme", "title": "Increase 25", "probability": 34.5},
        {"conditionId": "0xa", "title": "Decrease 25", "probability": 1.05},
        {"conditionId": "0xb", "title": "Decrease 50", "probability": 0.25},
        {"conditionId": "0xc", "title": "No change", "probability": 64.5},
        {"conditionId": "0xd", "title": "Increase 50", "probability": 0.35},
    ]

    async def _event_markets(_slug):
        return legs

    async def _market(cid, include_raw=False):
        return {"conditionId": "0xme", "title": "Increase 25",
                "probability": 34.5, "eventSlug": "e", "negRisk": True,
                "sources": {}, "unknowns": []}

    monkeypatch.setattr(t, "_event_markets", _event_markets)
    monkeypatch.setattr(t, "market", _market)
    r = _run(t.related("0xme"))
    assert r["sumProbability"] in (100.6, 100.7), \
        f"legs summed to {r['sumProbability']}, expected ~100.65"
    assert all(x["conditionId"] != "0xme" for x in r["rows"])


def test_worthless_redeemable_positions_are_not_called_cash(monkeypatch, fresh_db):
    import terminal_trade as tt

    async def _positions(*a, **kw):
        return [{"ticker": "0xa", "title": "Expired loser",
                 "position_fp": 30.0, "cur_price": 0.0, "redeemable": True}]

    async def _summaries(_ids):
        return [{"conditionId": "0xa", "title": "Expired loser", "slug": None,
                 "status": "settled", "resolvedOutcome": "no",
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)

    r = _run(tt.portfolio())
    assert r["redeemableCount"] == 0, "there is nothing here to redeem"
    assert r["positions"][0]["redeemable"] is False
    assert r["settledWorthlessCount"] == 1
    assert r["redeemableValueUsd"] == 0.0
    assert "nothing to claim" in r["redeemableNote"]
    assert "cash" not in r["redeemableNote"].lower()


def test_valuable_redeemable_positions_report_what_they_are_worth(monkeypatch, fresh_db):
    import terminal_trade as tt

    async def _positions(*a, **kw):
        return [{"ticker": "0xa", "title": "Winner",
                 "position_fp": 30.0, "cur_price": 1.0, "redeemable": True}]

    async def _summaries(_ids):
        return [{"conditionId": "0xa", "title": "Winner", "slug": None,
                 "status": "settled", "resolvedOutcome": "yes",
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)

    r = _run(tt.portfolio())
    assert r["redeemableValueUsd"] == 30.0
    assert "30.00" in r["redeemableNote"]


def _standing_failing(monkeypatch, ok, placed):
    entries = [{
        "id": "st_1", "conditionId": "0xabc", "title": "T", "side": "yes",
        "kind": "stopLoss", "triggerCents": 40.0, "size": 100,
        "createdAt": "", "armed": True, "lastCheckedAt": None,
        "firedAt": None, "note": None,
    }]
    monkeypatch.setattr(tt, "standing_list", lambda: entries)
    monkeypatch.setattr(tt, "_standing_put", lambda e: None)

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": 30.0}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _preview(req):
        return {"costUsd": 1.0}
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place(req, **kw):
        placed.append(req)
        return {"ok": ok, "message": "size below the market minimum"}
    monkeypatch.setattr(tt, "place", _place)
    return entries


def test_a_refused_stop_stays_armed_and_retries(monkeypatch):
    placed = []
    entries = _standing_failing(monkeypatch, ok=False, placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert entries[0]["armed"] is True, "a refused stop disarmed itself"
    assert entries[0]["firedAt"] is None
    assert entries[0]["failures"] == 1
    assert "still armed" in entries[0]["note"]


def test_a_refused_stop_gives_up_after_a_bounded_number_of_tries(monkeypatch):
    placed = []
    entries = _standing_failing(monkeypatch, ok=False, placed=placed)
    for _ in range(tt.MAX_STANDING_FAILURES):
        _run(tt.standing_tick(allow_trading=True))
    assert entries[0]["armed"] is False
    assert len(placed) == tt.MAX_STANDING_FAILURES
    assert "giving up" in entries[0]["note"]
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == tt.MAX_STANDING_FAILURES


def test_a_successful_stop_retires_normally(monkeypatch):
    placed = []
    entries = _standing_failing(monkeypatch, ok=True, placed=placed)
    r = _run(tt.standing_tick(allow_trading=True))
    assert entries[0]["armed"] is False
    assert entries[0]["firedAt"]
    assert r["fired"] == 1
    assert len(placed) == 1


def test_a_refused_stop_is_reported_as_blocked(monkeypatch):
    placed = []
    _standing_failing(monkeypatch, ok=False, placed=placed)
    r = _run(tt.standing_tick(allow_trading=True))
    assert r["alerts"] and r["alerts"][0]["blocked"] is True


def test_a_sub_minimum_sell_warns_but_is_not_blocked(monkeypatch):
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "orderMinSize": 5,
                      "acceptingOrders": True, "feesEnabled": False}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": True, "asks": [], "note": None,
                "bids": [{"priceCents": 50.0, "size": 100, "cumulative": 100,
                          "notionalUsd": 50.0}]}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.api, "_deposit_wallet_issue", lambda: "")

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "sell", "priceCents": 50, "size": 3,
                          "orderType": "FAK"}))
    assert not pv["blockers"], f"a sub-minimum SELL was blocked: {pv['blockers']}"
    assert any("minimum order" in w for w in pv["warnings"])
    assert any("letting it settle" in w for w in pv["warnings"])

    pvb = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "buy", "priceCents": 50, "size": 3,
                           "orderType": "FAK"}))
    assert pvb["blockers"], "a sub-minimum BUY should still be blocked"


def test_a_sell_refreshes_the_conditional_allowance(monkeypatch):
    import polymarket_api as api
    calls = []

    async def _refresh(token_id):
        calls.append(token_id)
        return True

    async def _meta(ticker):
        return {"yes_token": "TOKYES", "no_token": "TOKNO",
                "tick_size": 0.01, "min_size": 5, "neg_risk": False}

    monkeypatch.setattr(api, "refresh_token_allowance", _refresh)
    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "_assert_maker_is_deposit_wallet", lambda: None)
    monkeypatch.setattr(api.auth, "create_signed_order",
                        lambda **kw: {"signed": True})

    async def _stop(*a, **kw):
        raise RuntimeError("stop before submit")
    monkeypatch.setattr(api, "ensure_api_creds", _stop)

    with pytest.raises(RuntimeError):
        _run(api.place_limit_order(
            ticker="0xabc", side="yes", action="sell", count=10,
            price_cents=50, order_type="FAK"))
    assert calls == ["TOKYES"], "the sell path did not refresh the token allowance"


def test_a_buy_does_not_refresh_the_conditional_allowance(monkeypatch):
    import polymarket_api as api
    calls = []

    async def _refresh(token_id):
        calls.append(token_id)
        return True

    async def _meta(ticker):
        return {"yes_token": "TOKYES", "no_token": "TOKNO",
                "tick_size": 0.01, "min_size": 5, "neg_risk": False}

    monkeypatch.setattr(api, "refresh_token_allowance", _refresh)
    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "_assert_maker_is_deposit_wallet", lambda: None)
    monkeypatch.setattr(api.auth, "create_signed_order",
                        lambda **kw: {"signed": True})

    async def _stop(*a, **kw):
        raise RuntimeError("stop before submit")
    monkeypatch.setattr(api, "ensure_api_creds", _stop)

    with pytest.raises(RuntimeError):
        _run(api.place_limit_order(
            ticker="0xabc", side="yes", action="buy", count=10,
            price_cents=50, order_type="FAK"))
    assert calls == [], "a buy refreshed the conditional allowance needlessly"


def _portfolio_with(monkeypatch, fresh_db, *, resolved, side="yes",
                    size=100.0, buy_cents=40.0):
    import terminal_trade as tt
    _seed_fills([{"action": "buy", "size": size, "price": buy_cents,
                  "side": side}])

    async def _positions(*a, **kw):
        return [{"ticker": "0xdead", "title": "T",
                 "position_fp": size if side == "yes" else -size,
                 "cur_price": 0.6, "redeemable": bool(resolved)}]

    async def _summaries(_ids):
        return [{"conditionId": "0xdead", "title": "T", "slug": None,
                 "status": "settled" if resolved else "open",
                 "resolvedOutcome": resolved,
                 "closeTime": "2026-01-01T00:00:00Z", "probability": 60.0}]

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


def test_a_settled_loser_is_realised_not_unrealised(monkeypatch, fresh_db):
    r = _portfolio_with(monkeypatch, fresh_db, resolved="no")
    p = r["positions"][0]
    assert p["settled"] is True
    assert p["unrealizedPnlUsd"] is None, "a decided position claimed unrealised P&L"
    assert abs(p["settledPnlUsd"] + 40.0) < 1e-6
    assert abs(p["realizedPnlUsd"] + 40.0) < 1e-6
    assert r["totalUnrealizedPnlUsd"] == 0.0
    assert abs(r["realizedPnlUsd"] + 40.0) < 1e-6
    assert abs(r["settledPnlUsd"] + 40.0) < 1e-6


def test_a_settled_winner_is_realised_at_full_value(monkeypatch, fresh_db):
    r = _portfolio_with(monkeypatch, fresh_db, resolved="yes")
    p = r["positions"][0]
    assert abs(p["realizedPnlUsd"] - 60.0) < 1e-6
    assert p["unrealizedPnlUsd"] is None
    assert abs(r["realizedPnlUsd"] - 60.0) < 1e-6


def test_an_open_position_is_unrealised_not_realised(monkeypatch, fresh_db):
    r = _portfolio_with(monkeypatch, fresh_db, resolved=None)
    p = r["positions"][0]
    assert p["settled"] is False
    assert p["settledPnlUsd"] is None
    assert abs(p["unrealizedPnlUsd"] - 20.0) < 1e-6
    assert abs(r["totalUnrealizedPnlUsd"] - 20.0) < 1e-6
    assert r["settledPnlUsd"] is None


def test_settlement_and_earlier_sells_are_both_counted_once(monkeypatch, fresh_db):
    import terminal_trade as tt
    _seed_fills([
        {"action": "buy", "size": 100, "price": 40.0},
        {"action": "sell", "size": 50, "price": 60.0},
    ])

    async def _positions(*a, **kw):
        return [{"ticker": "0xdead", "title": "T", "position_fp": 50.0,
                 "cur_price": 1.0, "redeemable": True}]

    async def _summaries(_ids):
        return [{"conditionId": "0xdead", "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": "yes",
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)

    r = _run(tt.portfolio())
    assert abs(r["settledPnlUsd"] - 30.0) < 1e-6
    assert abs(r["realizedPnlUsd"] - 40.0) < 1e-6, \
        f"realised was {r['realizedPnlUsd']}, expected 40 (10 sold + 30 settled)"


def test_a_settled_position_with_no_basis_reports_no_pnl(monkeypatch, fresh_db):
    import terminal_trade as tt

    async def _positions(*a, **kw):
        return [{"ticker": "0xnobasis", "title": "T", "position_fp": 50.0,
                 "cur_price": 0.0, "redeemable": True}]

    async def _summaries(_ids):
        return [{"conditionId": "0xnobasis", "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": "no",
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)

    r = _run(tt.portfolio())
    p = r["positions"][0]
    assert p["unrealizedPnlUsd"] is None and p["settledPnlUsd"] is None
    assert p["unreconciled"] is True
    assert r["settledPnlUsd"] is None


def _flat(monkeypatch, positions, *, bid=40.0, min_size=None, blockers=(),
          place_ok=True, paper=False, tracked=frozenset(), book_available=True,
          resolved=None, books=None):
    placed: list[dict] = []
    resolved = dict(resolved or {})

    async def _positions(*_a, **_kw):
        return list(positions)
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _summaries(ids):
        return [{"conditionId": c, "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": resolved[c],
                 "outcomes": ["Up", "Down"], "closeTime": None,
                 "probability": None}
                for c in ids if c in resolved]
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt, "_resolution_remember", lambda *_a, **_k: None)

    async def _book(cid, side="yes"):
        if books is not None:
            books.append(cid)
        if not book_available:
            return {"available": False, "bids": [], "asks": [], "bestBid": None,
                    "note": "the venue did not answer"}
        return {"available": True, "bestBid": bid, "bestAsk": None,
                "bids": [{"priceCents": bid, "size": 10_000}] if bid else [],
                "asks": []}
    monkeypatch.setattr(tt.term, "book", _book)

    async def _preview(req):
        return {
            "conditionId": req["conditionId"], "side": req["side"],
            "action": req["action"], "priceCents": req["priceCents"],
            "requestedPriceCents": req["priceCents"], "size": req["size"],
            "requestedSize": req["size"],
            "costUsd": round(req["size"] * req["priceCents"] / 100.0, 2),
            "fillableSize": req["size"], "estimatedAvgCents": req["priceCents"],
            "minOrderSize": min_size, "warnings": [],
            "blockers": list(blockers),
        }
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place(req, **kw):
        placed.append({**req, **kw})
        ok = place_ok(req) if callable(place_ok) else place_ok
        return {"ok": ok, "message": "sent" if ok else "the venue said no",
                "orderId": "o1" if ok else None}
    monkeypatch.setattr(tt, "place", _place)

    monkeypatch.setattr(tt, "_paper_mode_on", lambda: paper)
    monkeypatch.setattr(tt, "_engine_tracked_markets",
                        lambda: None if tracked is None else set(tracked))
    return placed


def _held(cid, size=100.0):
    return {"ticker": cid, "position_fp": size}


def test_flatten_sells_every_position_as_a_marketable_fak_at_the_bid(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa"), _held("0xb", -50.0)], bid=37.0)
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xa", "0xb"]
    assert all(p["action"] == "sell" for p in placed)
    assert all(p["orderType"] == "FAK" for p in placed)
    assert all(p["priceCents"] == 37.0 for p in placed), "not sold at the bid"
    assert [p["side"] for p in placed] == ["yes", "no"]
    assert [p["size"] for p in placed] == [100.0, 50.0]
    assert all(p["confirmUsd"] is not None for p in placed)
    assert r["ok"] is True
    assert (r["attempted"], r["sold"], r["refused"]) == (2, 2, 0)
    assert r["proceedsUsd"] == 55.50


def test_flatten_refuses_once_in_paper_mode_not_once_per_position(monkeypatch):
    placed = _flat(monkeypatch, [_held(f"0x{i}") for i in range(5)], paper=True)
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert placed == [], "a paper-mode flatten sent real orders"
    assert r["ok"] is False
    assert r["results"] == []
    assert r["message"].count("Paper mode is on") == 1
    assert (r["attempted"], r["sold"]) == (0, 0)


def test_flatten_holds_fire_when_the_wallet_cannot_be_read(monkeypatch):
    placed = _flat(monkeypatch, [])

    async def _boom(*_a, **_kw):
        raise RuntimeError("data-api is down")
    monkeypatch.setattr(tt.api, "get_positions", _boom)

    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["ok"] is False
    assert r["sold"] == 0 and r["attempted"] == 0
    assert "NOTHING was sold" in r["message"]
    assert "data-api is down" in r["message"]


def test_flatten_reads_positions_strictly(monkeypatch):
    seen = {}

    async def _positions(*a, **kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    _run(tt.flatten_wallet(allow_trading=True))
    assert seen.get("strict") is True


def test_flatten_is_held_to_the_live_trading_switch(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")])
    r = _run(tt.flatten_wallet(allow_trading=False))
    assert placed == []
    assert r["ok"] is False
    assert "Live trading is off" in r["message"]


def test_flatten_carries_on_past_one_refusal(monkeypatch):
    placed = _flat(
        monkeypatch, [_held("0xa"), _held("0xb"), _held("0xc")],
        place_ok=lambda req: req["conditionId"] != "0xb")
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xa", "0xb", "0xc"], \
        "it stopped at the first failure"
    assert (r["attempted"], r["sold"], r["refused"]) == (3, 2, 1)
    assert r["ok"] is False, "a partial flatten reported success"
    bad = [x for x in r["results"] if not x["ok"]]
    assert len(bad) == 1 and bad[0]["conditionId"] == "0xb"
    assert "the venue said no" in bad[0]["message"]


def test_flatten_refuses_a_sub_minimum_sell_instead_of_sending_it(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa", 3.0), _held("0xb", 40.0)],
                   min_size=5.0)
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xb"]
    assert (r["attempted"], r["sold"], r["refused"]) == (2, 1, 1)
    stuck = [x for x in r["results"] if x["conditionId"] == "0xa"][0]
    assert "minimum order" in stuck["message"]
    assert "settle" in stuck["message"], "it did not say what the user can do"


def test_flatten_will_not_sell_into_a_book_it_could_not_read(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")], book_available=False)
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["refused"] == 1
    assert "did not answer" in r["results"][0]["message"]


def test_flatten_will_not_sell_where_nobody_is_bidding(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")], bid=None)
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["refused"] == 1
    assert "Nobody is bidding" in r["results"][0]["message"]


def test_flatten_surfaces_a_blocker_from_the_preview(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")],
                   blockers=("This market is closed. Nothing can be traded here.",))
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["results"][0]["message"].startswith("This market is closed")


def test_flatten_says_how_many_sold_positions_an_engine_was_tracking(monkeypatch):
    _flat(monkeypatch, [_held("0xa"), _held("0xb")], tracked={"0xb"})
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["engineTrackedSold"] == 1
    assert "1 of the sold position(s) were being tracked by an engine" in r["message"]


def test_flatten_reports_an_unreadable_engine_table_as_unknown(monkeypatch):
    _flat(monkeypatch, [_held("0xa")], tracked=None)
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["engineTrackedSold"] is None
    assert "unknown" in r["message"]


def test_flatten_counts_empty_wallet_rows_as_skipped_not_sold(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa"), {"ticker": "0xz", "position_fp": 0.0}])
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert len(placed) == 1
    assert (r["attempted"], r["sold"], r["skipped"]) == (1, 1, 1)


def test_flatten_says_so_when_there_is_nothing_to_sell(monkeypatch):
    placed = _flat(monkeypatch, [])
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["ok"] is True
    assert r["proceedsUsd"] == 0.0
    assert "Nothing to sell" in r["message"]


def test_flatten_leaves_proceeds_unknown_when_a_sell_could_not_be_priced(
        monkeypatch):
    _flat(monkeypatch, [_held("0xa"), _held("0xb")])
    real_preview = tt.preview

    async def _preview(req):
        pv = await real_preview(req)
        if req["conditionId"] == "0xb":
            pv["costUsd"] = None
        return pv
    monkeypatch.setattr(tt, "preview", _preview)

    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["sold"] == 2
    assert r["proceedsUsd"] is None
    assert "unknown until the fills reconcile" in r["message"]


def test_flatten_reports_a_killed_remainder_rather_than_just_order_sent(
        monkeypatch):
    _flat(monkeypatch, [_held("0xa")])
    real_preview = tt.preview

    async def _preview(req):
        pv = await real_preview(req)
        pv["fillableSize"] = 30.0
        pv["warnings"] = ["Only 30 of 100 contracts are available at 40 cents "
                          "or better. The remainder will be killed, not rested."]
        return pv
    monkeypatch.setattr(tt, "preview", _preview)

    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["sold"] == 1
    assert "killed, not rested" in r["results"][0]["message"]


def test_flatten_preview_prices_without_sending_anything(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa"), _held("0xb", 50.0)], bid=40.0)
    r = _run(tt.flatten_preview())
    assert placed == [], "the preview sent an order"
    assert r["ok"] is True
    assert r["count"] == 2
    assert r["proceedsUsd"] == 60.0
    assert [p["bidCents"] for p in r["positions"]] == [40.0, 40.0]


def test_flatten_preview_keeps_an_unreadable_wallet_unknown(monkeypatch):
    _flat(monkeypatch, [])

    async def _boom(*_a, **_kw):
        raise RuntimeError("data-api is down")
    monkeypatch.setattr(tt.api, "get_positions", _boom)

    r = _run(tt.flatten_preview())
    assert r["ok"] is False
    assert r["count"] == 0 and r["proceedsUsd"] is None
    assert "Could not read your positions" in r["message"]


def test_flatten_preview_names_what_cannot_be_sold(monkeypatch):
    _flat(monkeypatch, [_held("0xa", 2.0), _held("0xb", 60.0)], min_size=5.0)
    r = _run(tt.flatten_preview())
    assert r["count"] == 1, "a position that cannot be sold was counted"
    blocked = [p for p in r["positions"] if p["blocked"]]
    assert len(blocked) == 1 and blocked[0]["conditionId"] == "0xa"
    assert blocked[0]["proceedsUsd"] is None
    assert r["proceedsUsd"] == 24.0


def test_flatten_preview_drops_settled_positions_before_reading_a_book(monkeypatch):
    reads: list[str] = []
    _flat(monkeypatch,
          [_held("0xs1"), _held("0xs2"), _held("0xs3"),
           _held("0xo1"), _held("0xo2", 50.0)],
          bid=40.0, books=reads,
          resolved={"0xs1": "yes", "0xs2": "no", "0xs3": "yes"})
    r = _run(tt.flatten_preview())

    assert r["ok"] is True
    assert [p["conditionId"] for p in r["positions"]] == ["0xo1", "0xo2"]
    assert r["count"] == 2, "the button would say Sell 0 positions"
    assert r["settledSkipped"] == 3
    assert reads == ["0xo1", "0xo2"], "a settled market was sent to the book"
    assert not any(p["blocked"] for p in r["positions"])
    assert "3 settled position(s) were left out" in r["message"]


def test_flatten_preview_says_nothing_to_sell_when_every_position_settled(monkeypatch):
    reads: list[str] = []
    _flat(monkeypatch, [_held("0xa"), _held("0xb")], books=reads,
          resolved={"0xa": "yes", "0xb": "no"})
    r = _run(tt.flatten_preview())

    assert r["ok"] is True
    assert r["positions"] == [] and r["count"] == 0
    assert r["settledSkipped"] == 2
    assert reads == [], "a settled wallet still walked the book"
    assert "every position in the wallet is settled" in r["message"]


def test_flatten_leaves_settled_positions_alone_and_says_so(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xs"), _held("0xo", 60.0)],
                   bid=40.0, resolved={"0xs": "no"})
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xo"]
    assert (r["attempted"], r["sold"], r["refused"]) == (1, 1, 0)
    assert r["settledSkipped"] == 1
    assert "1 settled position(s) were left alone" in r["message"]


def test_flatten_of_an_all_settled_wallet_sells_nothing_and_signs_nothing(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa"), _held("0xb")],
                   resolved={"0xa": "yes", "0xb": "yes"})
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert placed == []
    assert r["ok"] is True and r["attempted"] == 0
    assert r["settledSkipped"] == 2
    assert "every position in the wallet is settled" in r["message"]


def test_an_unreadable_resolution_leaves_the_position_open(monkeypatch):
    async def _boom(_ids):
        raise RuntimeError("gamma is down")
    reads: list[str] = []
    _flat(monkeypatch, [_held("0xa")], bid=40.0, books=reads)
    monkeypatch.setattr(tt.term, "_summaries_for", _boom)

    r = _run(tt.flatten_preview())
    assert r["settledSkipped"] == 0
    assert r["count"] == 1 and reads == ["0xa"]


def test_the_unreadable_book_refusal_has_one_full_stop(monkeypatch):
    _flat(monkeypatch, [_held("0xa")])

    async def _book(_cid, _side="yes"):
        return {"available": False, "bids": [], "asks": [], "bestBid": None,
                "note": ("The CLOB did not return a book for this token. This "
                         "is a failed read, not an empty book.")}
    monkeypatch.setattr(tt.term, "book", _book)

    r = _run(tt.flatten_preview())
    blocked = r["positions"][0]["blocked"]
    assert ".." not in blocked, blocked
    assert blocked.startswith("Could not read the order book — ")
    assert "not an empty book. Nothing was sold here" in blocked
