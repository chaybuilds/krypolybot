"""Polymarket Perps market maker: signing, formatting, planning, the board's
reward model, the breakers, and one engine tick end to end against a fake
exchange.

The signing vectors were produced by the official SDK's own path
(`polymarket-client` 0.12: real msgpack + keccak) — the app hand-rolls the
msgpack subset, so these pin it byte-for-byte. A drift here means every order
the engine sends is rejected as "invalid signature".
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import config
import db
import perps_api as pa
import perps_board as pb
import perps_maker as pm
import perps_ws


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


@pytest.mark.parametrize("sign", [1, -1])
def test_inventory_cap_counts_the_whole_ladder_and_shrinks_the_last_quote(sign):
    mid = 86390.5
    qs, _ = pm.plan_quotes(best_bid=86000, best_ask=86800, mid=mid, position_qty=sign * 250 / mid,
                           inst=BTC, p={**P, "levels": 3})
    adding = [q for q in qs if q.buy == (sign > 0)]
    assert len(adding) == 1 and adding[0].trimmed
    assert 40 < float(adding[0].qty) * mid <= 50
    unloading = [q for q in qs if q.buy != (sign > 0)]
    assert len(unloading) == 3 and not any(q.trimmed for q in unloading)
    qs, note = pm.plan_quotes(best_bid=86000, best_ask=86800, mid=mid,
                              position_qty=sign * 295 / mid, inst=BTC, p={**P, "levels": 3})
    assert [q for q in qs if q.buy == (sign > 0)] == []
    assert ("bids paused" if sign > 0 else "asks paused") in note


def test_orders_that_may_have_filled_unseen_count_against_the_cap():
    mid = 86390.5
    qs, note = pm.plan_quotes(best_bid=86000, best_ask=86800, mid=mid, position_qty=150 / mid,
                              inst=BTC, p=P, unseen_buy_qty=100 / mid)
    bid = next(q for q in qs if q.buy)
    assert bid.trimmed and float(bid.qty) * mid <= 50
    qs, note = pm.plan_quotes(best_bid=86000, best_ask=86800, mid=mid, position_qty=0,
                              inst=BTC, p=P, unseen_sell_qty=300 / mid)
    assert [q.buy for q in qs] == [True] and "asks paused" in note


def test_a_resting_order_bigger_than_a_trimmed_quote_is_replaced():
    live = [{"order_id": 1, "buy": True, "price": "100", "resting_quantity": "1.4"}]
    trimmed = pm.Quote(True, "100", "1", 100.0, 0, trimmed=True)
    assert pm.diff_orders(live, [trimmed], requote_bps=2.0) == ([1], [trimmed])
    full = pm.Quote(True, "100", "1", 100.0, 0)
    assert pm.diff_orders(live, [full], requote_bps=2.0) == ([], [])


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
    """Just enough of perps_api for the engine, stubbed at its HTTP boundary.

    It keeps the exchange's own list of resting orders, so a test can compare
    it with what the engine believes rests, and it can be told to fail: a
    whole request (`cancel_error`, `create_error`, `auto_error`,
    `read_error`), single cancels (`cancel_codes`, oid -> rejection code), or
    a create that lands but whose response is lost (`create_lands`)."""

    def __init__(self, monkeypatch, *, open_orders=None, position=0.0,
                 expires_in_ms=10**9):
        self.created: list[list] = []
        self.cancelled: list[int] = []
        self.autocancel: list[int] = []
        self.leverage: list[tuple] = []
        self.calls: list[str] = []
        self.create_proxies: list[str] = []
        self.linked: list[str] = []
        self._orders = [dict(o) for o in (open_orders or [])]
        self._position = position
        self._next_oid = 1000
        self.cancel_error = None
        self.cancel_codes: dict[int, str] = {}
        self.create_error = None
        self.create_lands = False
        self.auto_error = None
        self.read_error = None
        self.link_error = None
        self.load = 0
        self.creds = {"proxy": "0x" + "22" * 20, "privateKey": "0x" + "33" * 32,
                      "secret": "s", "expiresAt": int(time.time() * 1000) + expires_in_ms}
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
            if self.read_error:
                raise self.read_error
            return [dict(o) for o in self._orders]

        async def _fills(c, start, end=None, max_pages=20):
            return []

        async def _empty(c):
            return {}

        async def _rewards(c):
            return []

        async def _create(c, rows):
            self.calls.append("create")
            self.created.extend(rows)
            self.create_proxies.append(c["proxy"])
            if self.create_error and not self.create_lands:
                raise self.create_error
            acks = []
            for r in rows:
                oid = self._next_oid
                self._next_oid += 1
                self._orders.append({"order_id": oid, "instrument_id": r[0], "buy": r[1],
                                     "price": r[2], "resting_quantity": r[3],
                                     "quantity": r[3], "client_order_id": r[7]})
                acks.append({"status": "ok", "oid": oid})
            if self.create_error:
                raise self.create_error
            return acks

        async def _cancel(c, ids):
            self.calls.append("cancel")
            if self.cancel_error:
                raise self.cancel_error
            self.cancelled.extend(ids)
            out = []
            for i in ids:
                code = self.cancel_codes.get(i)
                if code:
                    out.append({"status": "err", "error": code, "oid": i})
                    if code in ("order_not_found", "order_already_terminal"):
                        self._orders = [o for o in self._orders if o["order_id"] != i]
                else:
                    self._orders = [o for o in self._orders if o["order_id"] != i]
                    out.append({"status": "ok", "oid": i})
            return out

        async def _auto(c, at):
            self.calls.append("auto")
            if self.auto_error and at:
                raise self.auto_error
            self.autocancel.append(at)

        async def _lev(c, iid, lev, cross):
            self.leverage.append((iid, lev, cross))

        async def _link(label="krypt-polybot"):
            self.linked.append(self.creds["proxy"])
            if self.link_error:
                raise self.link_error
            self.creds = {"proxy": "0x" + "55" * 20, "privateKey": "0x" + "66" * 32,
                          "secret": "s2", "expiresAt": int(time.time() * 1000) + pa.PROXY_TTL_MS}
            return {"linked": True}

        for name, fn in (("instruments", _instruments), ("tickers", _tickers),
                         ("book", _book), ("klines", _klines), ("portfolio", _portfolio),
                         ("open_orders", _open_orders), ("fills", _fills), ("stats", _empty),
                         ("rewards", _rewards), ("create_orders", _create),
                         ("cancel_orders", _cancel), ("auto_cancel", _auto),
                         ("update_leverage", _lev), ("link_account", _link)):
            monkeypatch.setattr(pa, name, fn)
        monkeypatch.setattr(pa, "recent_requests", lambda window=1.0: self.load, raising=False)
        monkeypatch.setattr(pa, "creds", lambda: self.creds)
        monkeypatch.setattr(pa, "creds_status", lambda: {
            "linked": True, "expired": False, "expiresAt": self.creds["expiresAt"],
            "proxy": self.creds["proxy"], "owner": "0x" + "44" * 20, "walletPresent": True})

    def resting(self) -> list[dict]:
        return list(self._orders)

    def resting_ids(self) -> list[int]:
        return sorted(int(o["order_id"]) for o in self._orders)


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


def test_a_fast_requote_counts_a_vanished_bid_as_filled(perps_db, monkeypatch):
    mid = 86390.5
    ex = FakeExchange(monkeypatch, position=150 / mid)
    feed = _live_feed(monkeypatch, mid)
    cfg = _enabled_cfg()
    _run(pm.run_tick(cfg))
    bid = next(o for o in ex.resting() if o["buy"])
    ex._orders.remove(bid)
    ex._position += float(bid["resting_quantity"])
    ex.cancel_codes = {int(bid["order_id"]): "order_already_terminal"}

    def long_usd():
        rest = sum(float(o["resting_quantity"]) for o in ex.resting() if o["buy"])
        return (ex._position + rest) * feed["mid"]
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    assert ex.calls[-1] == "create" and long_usd() <= 300
    feed["mid"] -= 26
    _run(pm.run_fast(cfg, {6}))
    assert any(o["buy"] for o in ex.resting()) and long_usd() <= 300


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
    """Unlinked, the tab still shows what would be quoted — named by symbol,
    centred on the ticker mid even when the sampled book has gone stale."""
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


_RATE_LIMITED = pa.PerpsAPIError("ip_rate_limited", status=429, error="ip_rate_limited",
                                 retry_after=1.0)
_NETWORK = pa.PerpsAPIError("Perps API unreachable: ReadTimeout", error="network")


def _live_feed(monkeypatch, mid=86390.5):
    """The live feed's touch, under the test's control (`state["mid"]`)."""
    state = {"mid": mid}

    def _bbo(iid, max_age=5.0):
        m = state["mid"]
        return {"iid": int(iid), "bid": m - 0.5, "ask": m + 0.5, "mid": m,
                "ts": 0, "at": time.monotonic()}
    monkeypatch.setattr(perps_ws.feed, "bbo", _bbo)
    return state


def _shift_clock(monkeypatch, sec):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + sec)


def _tracked() -> list[int]:
    return sorted(int(o["order_id"]) for os_ in pm._our_orders.values() for o in os_)


def _quoting(monkeypatch, **kw):
    """A fake exchange with the engine's two quotes resting after one tick."""
    ex = FakeExchange(monkeypatch)
    feed = _live_feed(monkeypatch)
    cfg = _enabled_cfg(**kw)
    assert _run(pm.run_tick(cfg)) is True
    assert len(ex.resting()) == 2 and _tracked() == ex.resting_ids()
    return ex, feed, cfg


def test_a_failed_cancel_never_stacks_replacement_quotes(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    before = ex.resting_ids()
    ex.cancel_error = _RATE_LIMITED
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    assert ex.calls[-1] == "cancel"
    assert ex.resting_ids() == before
    assert _tracked() == before
    sent = len(ex.calls)
    _run(pm.run_fast(cfg, {6}))
    assert len(ex.calls) == sent
    ex.cancel_error = None
    _shift_clock(monkeypatch, 1.5)
    _run(pm.run_fast(cfg, {6}))
    assert ex.calls[-2:] == ["cancel", "create"]
    assert len(ex.resting()) == 2 and _tracked() == ex.resting_ids()


@pytest.mark.parametrize("code,held", [
    ("order_in_flight", True),
    ("order_not_pending_engine", True),
    ("order_not_found", False),
    ("order_already_terminal", False),
])
def test_only_a_confirmed_cancel_releases_the_replacement(perps_db, monkeypatch, code, held):
    ex, feed, cfg = _quoting(monkeypatch)
    bid = next(int(o["order_id"]) for o in ex.resting() if o["buy"])
    ex.cancel_codes = {bid: code}
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    assert len([o for o in ex.resting() if o["buy"]]) <= 1
    if held:
        assert ex.calls[-1] == "cancel"
        assert _tracked() == [bid] == ex.resting_ids()
        ex.cancel_codes = {}
        _run(pm.run_fast(cfg, {6}))
        assert ex.calls[-2:] == ["cancel", "create"]
    else:
        assert [o["buy"] for o in ex.resting()] == [False]
        _run(pm.run_fast(cfg, {6}))
    assert len(ex.resting()) == 2 and _tracked() == ex.resting_ids()


def test_cancel_results_without_ids_are_matched_by_position(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    ask = next(int(o["order_id"]) for o in ex.resting() if not o["buy"])
    real = pa.cancel_orders

    async def _no_ids(c, ids):
        res = await real(c, ids)
        return [{k: v for k, v in r.items() if k != "oid"} for r in res]
    monkeypatch.setattr(pa, "cancel_orders", _no_ids)
    ex.cancel_codes = {ask: "order_in_flight"}
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    assert _tracked() == [ask] == ex.resting_ids()


def test_autocancel_is_armed_before_the_first_quote(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch)
    _run(pm.run_tick(_enabled_cfg()))
    assert "create" in ex.calls
    assert ex.calls.index("auto") < ex.calls.index("create")


def test_no_quotes_go_out_when_the_autocancel_cannot_be_armed(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch)
    ex.auto_error = pa.PerpsAPIError("auto_cancel_daily_limit_reached", status=400,
                                     error="auto_cancel_daily_limit_reached")
    _run(pm.run_tick(_enabled_cfg()))
    assert ex.created == [] and ex.resting() == []
    assert "auto-cancel" in pm.status(_enabled_cfg())["lastError"]


def test_a_fast_requote_refreshes_a_lapsing_autocancel_first(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch, perpsPollSec=30)
    _shift_clock(monkeypatch, pm._AUTOCANCEL_EVERY_SEC + 1)
    n = len(ex.calls)
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    new = ex.calls[n:]
    assert "create" in new
    assert "auto" in new and new.index("auto") < new.index("create")


def test_switching_off_keeps_the_autocancel_until_our_quotes_are_gone(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    off = _enabled_cfg(perpsEnabled=False)
    ex.cancel_error = _NETWORK
    _run(pm.run_tick(off))
    assert 0 not in ex.autocancel
    assert len(ex.resting()) == 2
    ex.cancel_error = None
    _shift_clock(monkeypatch, 5)
    _run(pm.run_tick(off))
    assert ex.resting() == [] and ex.autocancel[-1] == 0


def test_shutdown_leaves_the_autocancel_armed_if_a_cancel_failed(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    ex.cancel_error = _NETWORK
    _run(pm.shutdown())
    assert 0 not in ex.autocancel and len(ex.resting()) == 2
    ex.cancel_error = None
    _run(pm.shutdown())
    assert ex.resting() == [] and ex.autocancel[-1] == 0


def test_a_create_with_an_unknown_outcome_is_not_repeated_blind(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    ex.create_error, ex.create_lands = _NETWORK, True
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    assert len(ex.resting()) == 2
    ex.create_error = None
    monkeypatch.setattr(pm, "_backoff_until", 0.0, raising=False)
    feed["mid"] -= 26
    _run(pm.run_fast(cfg, {6}))
    assert len(ex.resting()) == 2
    _run(pm.run_tick(cfg))
    assert len(ex.resting()) == 2 and _tracked() == ex.resting_ids()


def test_a_tick_cancelled_mid_create_does_not_requote_blind(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    ex.create_error, ex.create_lands = asyncio.CancelledError(), True
    feed["mid"] += 26
    with pytest.raises(asyncio.CancelledError):
        _run(pm.run_fast(cfg, {6}))
    assert len(ex.resting()) == 2
    ex.create_error = None
    feed["mid"] -= 26
    _run(pm.run_fast(cfg, {6}))
    assert len(ex.resting()) == 2
    _run(pm.run_tick(cfg))
    assert len(ex.resting()) == 2 and _tracked() == ex.resting_ids()


def test_fast_requotes_stop_while_full_ticks_are_not_syncing(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    _shift_clock(monkeypatch, 6)
    n = len(ex.calls)
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    assert ex.calls[n:] == []


def test_fast_requotes_yield_when_the_request_budget_is_spent(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    ex.load = 7
    n = len(ex.calls)
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    assert ex.calls[n:] == []


def test_the_trading_key_is_renewed_before_it_lapses(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch, expires_in_ms=3_600_000)
    old = ex.creds["proxy"]
    _run(pm.run_tick(_enabled_cfg()))
    assert ex.linked == [old]
    assert ex.creds["expiresAt"] > time.time() * 1000 + 6 * 86_400_000
    assert ex.create_proxies == [ex.creds["proxy"]]
    _run(pm.run_tick(_enabled_cfg()))
    assert len(ex.linked) == 1


def test_the_key_is_not_renewed_while_the_engine_is_off(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch, expires_in_ms=3_600_000)
    _run(pm.run_tick(_enabled_cfg(perpsEnabled=False)))
    assert ex.linked == []


def test_an_unrenewable_key_pulls_the_quotes_before_it_expires(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    ex.creds["expiresAt"] = int(time.time() * 1000) + 5 * 60_000
    ex.link_error = pa.PerpsAPIError("Perps request refused (HTTP 403)", status=403)
    _run(pm.run_tick(cfg))
    assert len(ex.linked) == 1
    assert ex.resting() == []
    st = pm.status(cfg)
    assert "key" in st["haltReason"] and st["trading"] is False
    assert "403" in st["link"]["renewError"]
    _run(pm.run_tick(cfg))
    assert len(ex.linked) == 1



def test_a_markout_kill_cancels_the_market_and_stops_quoting(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    pm._markets[6].markouts.extend([-20.0] * 20)
    _run(pm.run_tick(cfg))
    assert ex.resting() == [] and _tracked() == []
    m = pm.status(cfg)["markets"][0]
    assert m["state"] == "killed" and "rest of the UTC day" in m["note"]
    n = len(ex.created)
    feed["mid"] += 26
    _run(pm.run_fast(cfg, {6}))
    _run(pm.run_tick(cfg))
    assert len(ex.created) == n and ex.resting() == []


def test_a_markout_kill_keeps_retrying_a_cancel_that_failed(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    pm._markets[6].markouts.extend([-20.0] * 20)
    ex.cancel_error = _NETWORK
    _run(pm.run_tick(cfg))
    assert len(ex.resting()) == 2 and ex.calls[-1] == "cancel"
    ex.cancel_error = None
    _shift_clock(monkeypatch, 5)
    _run(pm.run_tick(cfg))
    assert ex.resting() == [] and ex.calls[-1] == "cancel"


def test_flatten_cancels_our_quotes_then_closes_reduce_only(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    quotes = ex.resting_ids()
    ex._position = -0.002
    n = len(ex.calls)
    res = _run(pm.flatten_all())
    assert res["ok"] is True and ex.calls[n:] == ["cancel", "create"]
    assert not set(quotes) & set(ex.resting_ids())
    close = ex.created[-1]
    assert close[1] is True and close[4] == "ioc" and close[6] is True
    assert float(close[3]) == pytest.approx(0.002)


def test_flatten_refuses_when_the_account_cannot_be_read(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch, position=0.01)

    async def _down(c):
        raise _NETWORK
    monkeypatch.setattr(pa, "portfolio", _down)
    res = _run(pm.flatten_all())
    assert res["ok"] is False and ex.created == []


def test_an_unreadable_portfolio_is_not_a_flat_account(perps_db, monkeypatch):
    real_portfolio = pa.portfolio
    ex = FakeExchange(monkeypatch, position=0.01)
    monkeypatch.setattr(pa, "portfolio", real_portfolio)

    async def _get(path, params=None, *, creds=None):
        return None
    monkeypatch.setattr(pa, "_get", _get)
    res = _run(pm.flatten_all())
    assert res["ok"] is False and "no open positions" not in res["message"]
    _live_feed(monkeypatch)
    _run(pm.run_tick(_enabled_cfg()))
    assert not [r for r in ex.created if r[1]]


def test_an_unreadable_open_orders_list_is_an_error(monkeypatch):
    async def _get(path, params=None, *, creds=None):
        return None
    monkeypatch.setattr(pa, "_get", _get)
    with pytest.raises(pa.PerpsAPIError):
        _run(pa.open_orders({}))


def test_flatten_says_so_when_a_position_could_not_be_closed(perps_db, monkeypatch):
    ex = FakeExchange(monkeypatch, position=0.01)

    async def _no_book(iid, depth=100):
        raise _NETWORK
    monkeypatch.setattr(pa, "book", _no_book)
    res = _run(pm.flatten_all())
    assert res["ok"] is False and "no open positions" not in res["message"]
    assert "BTC-USD" in res["message"]


def test_flatten_says_so_when_our_quotes_could_not_be_cancelled(perps_db, monkeypatch):
    ex, feed, cfg = _quoting(monkeypatch)
    ex._position = 0.002
    ex.cancel_error = _NETWORK
    res = _run(pm.flatten_all())
    assert res["ok"] is False and "quote" in res["message"]
    assert [r for r in ex.created if r[6]]


def test_autocancel_rejected_in_a_200_body_raises(monkeypatch):
    async def _signed(method, path, op, creds):
        return {"status": "err", "error": "auto_cancel_daily_limit_reached"}
    monkeypatch.setattr(pa, "_signed", _signed)
    with pytest.raises(pa.PerpsAPIError) as e:
        _run(pa.auto_cancel({}, int(time.time() * 1000) + 60_000))
    assert e.value.error == "auto_cancel_daily_limit_reached"


def test_every_request_counts_toward_the_per_second_budget(monkeypatch):
    def _handler(request):
        return httpx.Response(200, json=[])
    client = httpx.AsyncClient(base_url=pa.BASE_URL, transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(pa, "_get_client", lambda: client)
    base = pa.recent_requests()
    _run(pa.tickers())
    _run(pa.tickers())
    assert pa.recent_requests() == base + 2
