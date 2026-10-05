from __future__ import annotations

import asyncio
import time

import pytest

import config
import db
import perps_api as pa
import perps_board as pb
import perps_maker as pm


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


SDK_VECTORS = [
    (["createOrders", [[1, True, "65000", "0.01", "ioc", False, None,
                        "7f9e4a2b6c8d0e1f1234567890abcdef", None]]],
     "0x33c8d67201940ff3138d7b500e8bd27b8dfe0fd13131546f8d76de1acdb5d46c"),
    (["cancelAll", [6]],
     "0x76743794ef312b036b7b122aa5e5b3aadff5c0010fd955651d1a213ec58c0107"),
    (["updateLeverage", [6, 5, False]],
     "0x481e33c65f5ecc1d9064a51391548ccad9e05e0f786ba52fbbfb1432ab092b41"),
]


@pytest.mark.parametrize("op,expected", SDK_VECTORS)
def test_op_hash_matches_official_sdk(op, expected):
    assert pa.hash_op(op) == expected


def test_msgpack_subset_encodings():
    assert pa._pack(None) == b"\xc0"
    assert pa._pack(True) == b"\xc3" and pa._pack(False) == b"\xc2"
    assert pa._pack(127) == b"\x7f"
    assert pa._pack(128) == b"\xcc\x80"
    assert pa._pack(65536) == b"\xce\x00\x01\x00\x00"
    assert pa._pack(5167506594748800) == b"\xcf" + (5167506594748800).to_bytes(8, "big")
    assert pa._pack(-1) == b"\xff"
    assert pa._pack(-33) == b"\xd0\xdf"
    assert pa._pack("a" * 32)[:2] == b"\xd9\x20"
    assert pa._pack([1, 2]) == b"\x92\x01\x02"
    with pytest.raises(TypeError):
        pa._pack(1.5)


def test_signature_recovers_to_the_proxy_key():
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    key = "0x" + "11" * 32
    op = ["cancelOrders", [42]]
    signed = pa.sign_op(key, op)
    typed = pa.op_typed_data(op, signed["salt"], signed["ts"])
    who = Account.recover_message(encode_typed_data(full_message=typed),
                                  signature=signed["sig"])
    assert who == Account.from_key(key).address


def test_body_op_matches_sdk_keyed_form():
    row = pa.order_row(6, True, "86354", "0.00115", coid="ab" * 16)
    assert pa.body_op(["createOrders", [row]]) == {"type": "createOrders", "args": [
        {"iid": 6, "buy": True, "po": True, "qty": "0.00115", "tif": "gtc",
         "p": "86354", "c": "ab" * 16}]}
    ro = pa.order_row(6, False, "86000", "0.001", tif="ioc", post_only=False, reduce_only=True)
    assert pa.body_op(["createOrders", [ro]])["args"][0]["ro"] is True
    assert pa.order_row(1, True, "1", "1")[6] is None
    assert pa.body_op(["cancelAll", []]) == {"type": "cancelAll", "args": {}}
    assert pa.body_op(["cancelAll", [6]]) == {"type": "cancelAll", "args": {"iid": 6}}
    assert pa.body_op(["autoCancel", [123]]) == {"type": "autoCancel", "args": {"time": 123}}
    assert pa.body_op(["updateLeverage", [6, 5, False]]) == {
        "type": "updateLeverage", "args": {"cross": False, "iid": 6, "lev": 5}}


def test_price_rounds_away_from_the_market_at_five_sig_figs():
    assert pa.fmt_price(86354.7, 1, side="buy") == "86354"
    assert pa.fmt_price(86354.2, 1, side="sell") == "86355"
    assert pa.fmt_price(7721.37, 1, side="buy") == "7721.3"
    assert pa.fmt_price(0.00645678, 7, side="buy") == "0.0064567"
    assert pa.fmt_price(0.00645671, 7, side="sell") == "0.0064568"
    assert pa.fmt_price(100.0, 2, side="sell") == "100"


def test_quantity_floors_to_decimals_and_sig_figs():
    assert pa.fmt_qty(0.00115789, 5) == "0.00115"
    assert pa.fmt_qty(15463.98, 0) == "15463"
    assert pa.fmt_qty(123456.7, 0) == "123450"
    assert pa.fmt_qty(0.0, 3) == "0"


BTC = {"price_decimals": 1, "quantity_decimals": 5, "min_notional": "10"}
P = {"quote_bps": 3.0, "levels": 1, "step_bps": 3.0, "size_usd": 100.0,
     "max_inv_usd": 300.0, "skew_bps": 4.0}


def test_plan_is_symmetric_and_passive_when_flat():
    qs, note = pm.plan_quotes(best_bid=86390, best_ask=86391, mid=86390.5,
                              position_qty=0, inst=BTC, p=P)
    assert note == ""
    bid = next(q for q in qs if q.buy)
    ask = next(q for q in qs if not q.buy)
    assert bid.px < 86390.5 < ask.px
    assert bid.px <= 86390 and ask.px >= 86391
    for q in (bid, ask):
        assert abs(q.px - 86390.5) / 86390.5 * 1e4 == pytest.approx(3.0, abs=0.2)
        assert float(q.qty) * q.px >= 10


def test_zero_distance_joins_the_touch_without_crossing():
    qs, _ = pm.plan_quotes(best_bid=86390, best_ask=86391, mid=86390.5,
                           position_qty=0, inst=BTC, p={**P, "quote_bps": 0.0})
    assert {(q.buy, q.px) for q in qs} == {(True, 86390.0), (False, 86391.0)}


def test_inventory_cap_pauses_the_adding_side_and_skews_toward_unloading():
    long_qty = 400 / 86390.5
    qs, note = pm.plan_quotes(best_bid=86390, best_ask=86391, mid=86390.5,
                              position_qty=long_qty, inst=BTC, p=P)
    assert [q.buy for q in qs] == [False]
    assert "bids paused" in note
    flat, _ = pm.plan_quotes(best_bid=86390, best_ask=86391, mid=86390.5,
                             position_qty=0, inst=BTC, p=P)
    flat_ask = next(q for q in flat if not q.buy)
    assert qs[0].px < flat_ask.px

    half_long = 150 / 86390.5
    skewed, _ = pm.plan_quotes(best_bid=86000, best_ask=86800, mid=86400,
                               position_qty=half_long, inst=BTC, p=P)
    center = (skewed[0].px + skewed[1].px) / 2
    assert center < 86400


def test_quote_below_min_notional_is_withheld():
    qs, note = pm.plan_quotes(best_bid=86390, best_ask=86391, mid=86390.5,
                              position_qty=0, inst=BTC, p={**P, "size_usd": 5.0})
    assert qs == [] and "minimum" in note


def test_no_book_no_quotes():
    assert pm.plan_quotes(best_bid=None, best_ask=1.0, mid=None, position_qty=0,
                          inst=BTC, p=P)[0] == []


def test_ladder_levels_step_outward():
    qs, _ = pm.plan_quotes(best_bid=86000, best_ask=86800, mid=86400, position_qty=0,
                           inst=BTC, p={**P, "levels": 2, "step_bps": 5.0})
    bids = sorted((q.px for q in qs if q.buy), reverse=True)
    assert len(bids) == 2 and bids[0] > bids[1]


def test_diff_keeps_close_orders_and_replaces_the_rest():
    desired = [pm.Quote(True, "100", "1", 100.0, 0), pm.Quote(False, "101", "1", 101.0, 0)]
    live = [
        {"order_id": 1, "buy": True, "price": "100.01", "resting_quantity": "1"},
        {"order_id": 2, "buy": False, "price": "101.5", "resting_quantity": "1"},
        {"order_id": 3, "buy": True, "price": "100", "resting_quantity": "0.2"},
    ]
    cancel, place = pm.diff_orders(live, desired, requote_bps=2.0)
    assert sorted(cancel) == [2, 3]
    assert [(q.buy, q.price) for q in place] == [(False, "101")]


def test_move_and_markout_signs():
    import collections
    mids = collections.deque([(0.0, 100.0), (5.0, 100.1), (9.0, 100.3)])
    assert pm.moved_bps(mids, 10.0, 10.0) == pytest.approx(30.0, rel=1e-6)
    assert pm.moved_bps(mids, 10.0, 3.0) == pytest.approx(0.0)
    assert pm.markout_bps("long", 100.0, 100.1) == pytest.approx(10.0)
    assert pm.markout_bps("short", 100.0, 100.1) == pytest.approx(-10.0)


def test_coids_are_ours_and_well_formed():
    c = pm.new_coid()
    assert len(c) == 32 and c == c.lower() and int(c, 16) >= 0
    assert pm.is_ours({"client_order_id": c})
    assert not pm.is_ours({"client_order_id": "7f9e4a2b6c8d0e1f1234567890abcdef"})
    assert not pm.is_ours({})


def test_own_score_is_capped_per_tier_but_competition_is_not():
    mid = 100.0
    lv = [(99.98, 3000.0)]
    assert pb.side_score(lv, mid) == pytest.approx(100_000.0)
    assert pb.side_score(lv, mid, cap=False) == pytest.approx(299_940.0)
    assert pb.tier_weight(4.9) == 1.0 and pb.tier_weight(9) == 0.25
    assert pb.tier_weight(19.9) == 0.1 and pb.tier_weight(21) == 0.0


def test_book_depth_excludes_our_own_orders():
    bids = [["99.99", "10"], ["99.9", "5"]]
    asks = [["100.01", "10"]]
    whole = pb.book_depth(bids, asks)
    mine = pb.book_depth(bids, asks, own=[(True, 99.99, 10.0)])
    assert mine["compBid"] < whole["compBid"]
    assert mine["bestBid"] == 99.99


def test_share_estimate():
    assert pb.est_share(0, 100) == 0
    assert pb.est_share(100, 0) == 1
    assert pb.est_share(100, 100) == pytest.approx(0.5)
    assert 0 < pb.est_share(100, 15_000) < 0.05
    assert pb.quote_score(100, 3, 1, 3) == pytest.approx(100)
    assert pb.quote_score(100, 3, 2, 5) == pytest.approx(125)


def _inst(iid, sym, cat, close_only=False, min_notional="10"):
    return {"instrument_id": iid, "symbol": sym, "category": cat, "close_only": close_only,
            "max_leverage": 20, "min_notional": min_notional, "price_decimals": 2,
            "quantity_decimals": 3}


def test_board_build_and_pick_modes(monkeypatch):
    pb.reset_cache()
    inst = [_inst(1, "AAA-USD", "crypto"), _inst(2, "BBB-USD", "crypto"),
            _inst(3, "CCC-USD", "equity"), _inst(4, "DDD-USD", "crypto", close_only=True),
            _inst(5, "EEE-USD", "crypto", min_notional="500")]
    now = time.time()
    pb._books.update({
        1: {"at": now, "bestBid": 9.99, "bestAsk": 10.01, "mid": 10.0, "compBid": 50_000,
            "compAsk": 50_000, "comp": 50_000.0},
        2: {"at": now, "bestBid": 9.99, "bestAsk": 10.01, "mid": 10.0, "compBid": 500,
            "compAsk": 500, "comp": 500.0},
        3: {"at": now, "bestBid": 9.99, "bestAsk": 10.01, "mid": 10.0, "compBid": 10,
            "compAsk": 10, "comp": 10.0},
    })
    pb._volumes.update({1: {"at": now, "vol7": 7e6}, 2: {"at": now, "vol7": 7e4},
                        3: {"at": now, "vol7": 7e5}})
    cfg = config.merge_with_defaults({})
    board = pb.build(cfg, inst, {})
    assert board["activeMarkets"] == 4
    assert board["poolPerMarketUsd"] == pytest.approx(75_000 / 4)
    assert board["makerVol7dNeededUsd"] == pytest.approx(0.01 * (7e6 + 7e4 + 7e5))
    rows = {r["symbol"]: r for r in board["markets"]}
    assert rows["DDD-USD"]["poolUsd"] == 0
    assert rows["BBB-USD"]["estRewardUsd"] > rows["AAA-USD"]["estRewardUsd"]

    rewards = pb.pick_markets(board, {**cfg, "perps_market_count": 5})
    syms = [r["symbol"] for r in rewards]
    assert syms[:2] == ["BBB-USD", "AAA-USD"]
    assert "CCC-USD" not in syms and "DDD-USD" not in syms
    assert "EEE-USD" not in syms

    vol = pb.pick_markets(board, {**cfg, "perps_select_mode": "volume",
                                  "perps_categories": ["crypto", "equity"]})
    assert [r["symbol"] for r in vol][:3] == ["AAA-USD", "CCC-USD", "BBB-USD"]

    manual = pb.pick_markets(board, {**cfg, "perps_select_mode": "manual",
                                     "perps_markets": ["CCC-USD", "AAA-USD", "ZZZ-USD"]})
    assert [r["symbol"] for r in manual] == ["CCC-USD", "AAA-USD"]
    pb.reset_cache()


def test_config_clamps_perps_knobs():
    c = config.merge_with_defaults({
        "perpsSelectMode": "yolo", "perpsSizeUsd": 1, "perpsLevels": 9,
        "perpsDailyLossLimit": 40, "perpsLifetimeLossLimitUsd": -200,
        "perpsMarkets": ["btc-usd", "BTC-USD", 7, " eth-usd "],
        "perpsCategories": ["equity", "nonsense"], "perpsPollSec": 0,
    })
    assert c["perps_select_mode"] == "rewards"
    assert c["perps_size_usd"] == 10.0
    assert c["perps_levels"] == 3
    assert c["perps_daily_loss_limit"] == -40.0
    assert c["perps_lifetime_loss_limit_usd"] == 200.0
    assert c["perps_markets"] == ["BTC-USD", "ETH-USD"]
    assert c["perps_categories"] == ["equity"]
    assert c["perps_poll_sec"] == 1
    assert c["perps_enabled"] is False


@pytest.fixture
def perps_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "perps.db")
    db.init_db()
    pm.reset_state()
    pb.reset_cache()
    yield tmp_path
    pm.reset_state()
    pb.reset_cache()


def _fill(tid, *, pnl=0.0, fee=0.01, notional=100.0, ts=None, taker=False):
    return {"trade_id": tid, "order_id": 1, "instrument_id": 6, "symbol": "BTC-USD",
            "side": "long", "price": 100.0, "quantity": notional / 100.0,
            "notional": notional, "taker": taker, "fee": fee, "pnl": pnl,
            "ts": ts if ts is not None else int(time.time() * 1000)}


def test_breakers_daily_loss_and_volume_target(perps_db):
    cfg = config.merge_with_defaults({"perpsDailyLossLimit": 20})
    with db.get_db() as conn:
        assert db.insert_perps_fill(conn, _fill(1, pnl=-5))
        assert not db.insert_perps_fill(conn, _fill(1, pnl=-5))
    halt, today = pm.breakers(cfg)
    assert halt == "" and today["fills"] == 1
    with db.get_db() as conn:
        db.insert_perps_fill(conn, _fill(2, pnl=-30))
        db.insert_perps_fill(conn, _fill(3, pnl=-500, ts=pm.utc_day_start_ms() - 1000))
    halt, _ = pm.breakers(cfg)
    assert halt.startswith("daily loss limit")

    vol_cfg = config.merge_with_defaults({"perpsDailyLossLimit": 0,
                                          "perpsDailyVolumeTargetUsd": 150})
    halt, _ = pm.breakers(vol_cfg)
    assert halt.startswith("daily volume target")

    life_cfg = config.merge_with_defaults({"perpsDailyLossLimit": 0,
                                           "perpsLifetimeLossLimitUsd": 400})
    halt, _ = pm.breakers(life_cfg)
    assert halt.startswith("lifetime loss limit")


class FakeExchange:

    def __init__(self, monkeypatch, *, open_orders=None, position=0.0):
        self.created: list[list] = []
        self.cancelled: list[int] = []
        self.autocancel: list[int] = []
        self.leverage: list[tuple] = []
        self._orders = open_orders or []
        self._position = position
        creds = {"proxy": "0x" + "22" * 20, "privateKey": "0x" + "33" * 32,
                 "secret": "s", "expiresAt": int(time.time() * 1000) + 10**9}
        inst = [{"instrument_id": 6, "symbol": "BTC-USD", "category": "crypto",
                 "close_only": False, "max_leverage": 20, "min_notional": "10",
                 "price_decimals": 1, "quantity_decimals": 5}]
        tick = [{"instrument_id": 6, "index_price": "86390", "mark_price": "86390",
                 "mid_price": "86390.5", "open_interest": "1", "funding_rate": "0.0000125"}]
        book = {"bids": [["86390", "0.5"]], "asks": [["86391", "0.5"]]}

        async def _instruments(force=False):
            return inst

        async def _tickers():
            return tick

        async def _book(iid, depth=100):
            return book

        async def _klines(*a, **k):
            return []

        async def _portfolio(c):
            pos = [{"instrument_id": 6, "size": str(self._position), "unrealized_pnl": "0"}] \
                if self._position else []
            return {"positions": pos, "margin": {"total_account_value": "1000"}}

        async def _open_orders(c, iid=None):
            return list(self._orders)

        async def _fills(c, start, end=None, max_pages=20):
            return []

        async def _empty(c):
            return {}

        async def _rewards(c):
            return []

        async def _create(c, rows):
            self.created.extend(rows)
            return [{"status": "ok", "oid": i} for i, _ in enumerate(rows)]

        async def _cancel(c, ids):
            self.cancelled.extend(ids)
            return [{"status": "ok", "oid": i} for i in ids]

        async def _auto(c, at):
            self.autocancel.append(at)

        async def _lev(c, iid, lev, cross):
            self.leverage.append((iid, lev, cross))

        for name, fn in (("instruments", _instruments), ("tickers", _tickers),
                         ("book", _book), ("klines", _klines), ("portfolio", _portfolio),
                         ("open_orders", _open_orders), ("fills", _fills), ("stats", _empty),
                         ("rewards", _rewards), ("create_orders", _create),
                         ("cancel_orders", _cancel), ("auto_cancel", _auto),
                         ("update_leverage", _lev)):
            monkeypatch.setattr(pa, name, fn)
        monkeypatch.setattr(pa, "creds", lambda: creds)
        monkeypatch.setattr(pa, "creds_status", lambda: {
            "linked": True, "expired": False, "expiresAt": creds["expiresAt"],
            "proxy": creds["proxy"], "owner": "0x" + "44" * 20, "walletPresent": True})


def _enabled_cfg(**kw):
    return config.merge_with_defaults({"perpsEnabled": True, "perpsSelectMode": "manual",
                                       "perpsMarkets": ["BTC-USD"], **kw})


def test_tick_quotes_both_sides_post_only(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch)
    assert _run(pm.run_tick(_enabled_cfg())) is True
    assert len(ex.created) == 2
    for row in ex.created:
        assert row[0] == 6 and row[4] == "gtc" and row[5] is True
        assert row[6] is None
        assert pm.is_ours({"client_order_id": row[7]})
    buys = [float(r[2]) for r in ex.created if r[1]]
    sells = [float(r[2]) for r in ex.created if not r[1]]
    assert buys[0] < 86390.5 < sells[0]
    assert ex.leverage == [(6, 3, False)]
    assert ex.autocancel and ex.autocancel[0] > time.time() * 1000
    st = pm.status(_enabled_cfg())
    assert st["trading"] is True
    assert st["markets"][0]["state"] == "quoting"


def test_tick_leaves_good_quotes_alone(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch)
    _run(pm.run_tick(_enabled_cfg()))
    resting = [{"order_id": 100 + i, "instrument_id": 6, "buy": r[1], "price": r[2],
                "resting_quantity": r[3], "client_order_id": r[7]}
               for i, r in enumerate(ex.created)]
    ex2 = FakeExchange(monkeypatch, open_orders=resting)
    _run(pm.run_tick(_enabled_cfg()))
    assert ex2.created == [] and ex2.cancelled == []


def test_disabling_cancels_only_our_orders(perps_db, monkeypatch):
    mine = {"order_id": 7, "instrument_id": 6, "buy": True, "price": "86300",
            "resting_quantity": "0.001", "client_order_id": pm.new_coid()}
    manual = {"order_id": 8, "instrument_id": 6, "buy": True, "price": "80000",
              "resting_quantity": "0.001", "client_order_id": "ff" * 16}
    ex = FakeExchange(monkeypatch, open_orders=[mine, manual])
    _run(pm.run_tick(_enabled_cfg(perpsEnabled=False)))
    assert ex.cancelled == [7]
    assert ex.created == []


def test_breaker_halt_cancels_and_stops_quoting(perps_db, monkeypatch):
    mine = {"order_id": 9, "instrument_id": 6, "buy": False, "price": "86500",
            "resting_quantity": "0.001", "client_order_id": pm.new_coid()}
    ex = FakeExchange(monkeypatch, open_orders=[mine])
    with db.get_db() as conn:
        db.insert_perps_fill(conn, _fill(50, pnl=-100))
    _run(pm.run_tick(_enabled_cfg(perpsDailyLossLimit=50)))
    assert ex.cancelled == [9] and ex.created == []
    assert pm.status(_enabled_cfg(perpsDailyLossLimit=50))["haltReason"]


def test_inventory_cap_only_quotes_the_unloading_side(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch, position=0.01)
    _run(pm.run_tick(_enabled_cfg()))
    assert [r[1] for r in ex.created] == [False]


def test_no_credentials_is_a_noop(perps_db, monkeypatch):
    monkeypatch.setattr(pa, "creds", lambda: None)
    called = []

    async def _boom(*a, **k):
        called.append(1)
        raise AssertionError("must not touch the exchange")
    monkeypatch.setattr(pa, "tickers", _boom)
    assert _run(pm.run_tick(_enabled_cfg())) is False
    assert called == []


def test_preview_without_an_account_plans_around_the_fresh_mid(perps_db, monkeypatch):
    FakeExchange(monkeypatch)
    monkeypatch.setattr(pa, "creds", lambda: None)
    cfg = config.merge_with_defaults({"perpsSelectMode": "manual", "perpsMarkets": ["BTC-USD"]})
    _run(pb.snapshot(cfg, force=True))
    pb._books[6].update(bestBid=80000.0, bestAsk=80001.0, mid=80000.5)
    _run(pm.preview(cfg))
    m = pm.status(cfg)["markets"][0]
    assert m["symbol"] == "BTC-USD" and m["state"] == "off"
    bid = next(q["price"] for q in m["desired"] if q["buy"])
    ask = next(q["price"] for q in m["desired"] if not q["buy"])
    assert bid < 86390.5 < ask
    assert abs((ask - 86390.5) - (86390.5 - bid)) <= 1.0
