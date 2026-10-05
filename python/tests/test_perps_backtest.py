from __future__ import annotations

import asyncio
import time

import pytest

import config
import db
import perps_api as pa
import perps_backtest as bt
import perps_maker as pm
import perps_ws

from test_perps import FakeExchange, _enabled_cfg


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def tape_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "tape.db")
    db.init_db()
    pm.reset_state()
    yield tmp_path
    pm.reset_state()


def test_parse_bbo_and_trades():
    q = perps_ws.parse_bbo({"ch": "bbo::34", "ts": 5, "data": {
        "iid": 34, "bp": "3.8289", "bq": "720", "ap": "3.8317", "aq": "1043"}})
    assert q == {"iid": 34, "bid": 3.8289, "ask": 3.8317, "bq": 720.0, "aq": 1043.0, "ts": 5}
    assert perps_ws.parse_bbo({"ch": "bbo::1", "data": {"iid": 1, "bp": "2", "ap": "2"}}) is None
    assert perps_ws.parse_bbo({"ch": "bbo::1", "data": {"iid": 1, "bp": "0", "ap": "2"}}) is None
    tr = perps_ws.parse_trades({"ch": "trades::6", "data": [
        {"tid": 7, "iid": 6, "side": "long", "p": "85884", "qty": "0.013", "ts": 9, "settlement": False},
        {"tid": 8, "iid": 6, "side": "short", "p": "1", "qty": "1", "ts": 9, "settlement": True},
        {"tid": "bad"}]})
    assert tr == [{"trade_id": 7, "iid": 6, "side": "long", "price": 85884.0, "qty": 0.013, "ts": 9}]


def test_feed_wakes_on_moves_and_buffers_the_tape():
    f = perps_ws.PerpsFeed()
    f.wake = asyncio.Event()
    f.wake_bps = 1.0
    f.connected = True

    def bbo(bid, ask, ts):
        f.handle({"ch": "bbo::6", "ts": ts, "data": {"iid": 6, "bp": str(bid), "ap": str(ask)}})

    bbo(100.0, 100.02, 1)
    assert not f.wake.is_set()
    bbo(100.0, 100.021, 2)
    assert not f.wake.is_set()
    bbo(100.05, 100.07, 3)
    assert f.wake.is_set() and f.take_moved() == {6} and f.take_moved() == set()
    assert f.bbo(6)["mid"] == pytest.approx(100.06)
    f.handle([{"ch": "trades::6", "data": [{"tid": 1, "iid": 6, "side": "short", "p": "100.05",
                                             "qty": "1", "ts": 4}]}])
    trades, samples = f.drain()
    assert [t["trade_id"] for t in trades] == [1]
    assert samples and samples[0]["iid"] == 6
    f.connected = False
    assert f.bbo(6) is None


def test_fast_path_requotes_from_the_live_touch_and_tracks_acks(tape_db, monkeypatch):
    ex = FakeExchange(monkeypatch)
    feed = perps_ws.PerpsFeed()
    feed.connected = True
    monkeypatch.setattr(perps_ws, "feed", feed)
    cfg = _enabled_cfg()
    _run(pm.run_tick(cfg))
    assert len(ex.created) == 2
    assert {o["order_id"] for os_ in pm._our_orders.values() for o in os_} == {0, 1}
    feed.handle({"ch": "bbo::6", "ts": 1, "data": {"iid": 6, "bp": "86650", "ap": "86651"}})
    ex.created.clear()
    assert _run(pm.run_fast({**cfg, "perps_vol_pull_bps": 0.0}, {6})) is True
    assert sorted(ex.cancelled) == [0, 1]
    bids = [float(r[2]) for r in ex.created if r[1]]
    assert bids and bids[0] > 86600
    st = pm.status(cfg)["markets"][0]
    assert st["priceSource"] == "live"


def test_fast_path_does_nothing_when_disabled_or_unselected(tape_db, monkeypatch):
    ex = FakeExchange(monkeypatch)
    assert _run(pm.run_fast(_enabled_cfg(perpsEnabled=False), {6})) is False
    assert _run(pm.run_fast(_enabled_cfg(), {6})) is False
    assert ex.created == []


def test_realtime_off_unsubscribes(tape_db, monkeypatch):
    FakeExchange(monkeypatch)
    feed = perps_ws.PerpsFeed()
    monkeypatch.setattr(perps_ws, "feed", feed)
    _run(pm.run_tick(_enabled_cfg()))
    assert feed.instruments() == {6}
    _run(pm.run_tick(_enabled_cfg(perpsRealtime=False)))
    assert feed.instruments() == set()


def test_tick_records_the_live_tape(tape_db, monkeypatch):
    FakeExchange(monkeypatch)
    feed = perps_ws.PerpsFeed()
    monkeypatch.setattr(perps_ws, "feed", feed)
    feed.handle({"ch": "trades::6", "data": [{"tid": 11, "iid": 6, "side": "long",
                                              "p": "86391", "qty": "0.1", "ts": 1000}]})
    feed.handle({"ch": "bbo::6", "ts": 1000, "data": {"iid": 6, "bp": "86390", "ap": "86391"}})
    _run(pm.run_tick(_enabled_cfg()))
    with db.get_db() as conn:
        assert db.perps_trades_range(conn, 6, 0, 2000) == [(1000, "long", 86391.0, 0.1)]
        assert db.perps_ticks_range(conn, 6, 0, 2000) == [(1000, 86390.0, 86391.0)]


def test_missing_ranges():
    assert bt.missing_ranges([], 0, 100) == [(0, 100)]
    assert bt.missing_ranges([(0, 100)], 0, 100) == []
    assert bt.missing_ranges([(10, 20), (40, 50)], 0, 100) == [(0, 9), (21, 39), (51, 100)]
    assert bt.missing_ranges([(-5, 30)], 0, 100) == [(31, 100)]


def test_coverage_merges(tape_db):
    with db.get_db() as conn:
        db.add_perps_coverage(conn, 6, 0, 10)
        db.add_perps_coverage(conn, 6, 11, 20)
        db.add_perps_coverage(conn, 6, 50, 60)
        db.add_perps_coverage(conn, 6, 15, 55)
        db.add_perps_coverage(conn, 7, 0, 1)
        assert db.perps_coverage(conn, 6) == [(0, 60)]
        assert db.perps_coverage(conn, 7) == [(0, 1)]


def test_backfill_pages_backwards_and_never_refetches(tape_db, monkeypatch):
    tape = [{"trade_id": i, "instrument_id": 6, "side": "long" if i % 2 else "short",
             "price": "100", "quantity": "1", "settlement": False, "timestamp": 1_000_000 + i * 1000}
            for i in range(250)]
    calls = []

    async def fake_get(path, params=None, **_k):
        calls.append(dict(params))
        if path.endswith("/funding"):
            return {"data": [], "more": False}
        a, b = params["start_timestamp"], params["end_timestamp"]
        rows = sorted((t for t in tape if a <= t["timestamp"] <= b), key=lambda t: -t["timestamp"])
        return {"data": rows[:100], "more": len(rows) > 100}
    monkeypatch.setattr(pa, "_get", fake_get)
    monkeypatch.setattr(bt, "_MIN_SPACING", 0.0)
    start, end = 1_000_000, 1_000_000 + 249_000
    r = _run(bt.ensure_history(6, start, end))
    assert r["complete"] and r["fetched"] == 250 and r["coveredFrom"] == start
    n = len(calls)
    assert n == 3
    r2 = _run(bt.ensure_history(6, start, end))
    assert r2["fetched"] == 0 and len(calls) == n


def test_backfill_survives_rate_limits(tape_db, monkeypatch):
    hits = {"n": 0}

    async def flaky(path, params=None, **_k):
        hits["n"] += 1
        if hits["n"] <= 2:
            raise pa.PerpsAPIError("slow down", status=429, error="ip_rate_limited", retry_after=0.01)
        return {"data": [{"trade_id": 1, "side": "long", "price": "1", "quantity": "1",
                          "timestamp": 5}], "more": False}
    monkeypatch.setattr(pa, "_get", flaky)
    monkeypatch.setattr(bt, "_MIN_SPACING", 0.0)
    monkeypatch.setattr(bt, "_cooldown_until", 0.0)
    r = _run(bt.ensure_history(6, 0, 10))
    assert r["complete"] and r["fetched"] == 1 and hits["n"] == 3


INST = {"price_decimals": 2, "quantity_decimals": 3, "min_notional": "10"}
DAY0 = 1_790_000_000_000 - (1_790_000_000_000 % 86_400_000)


def _cfg(**kw):
    base = {"perpsQuoteBps": 1.0, "perpsSizeUsd": 100, "perpsMaxInventoryUsd": 1000,
            "perpsSkewBps": 0, "perpsVolPullBps": 0, "perpsMarkoutKillBps": 0,
            "perpsDailyLossLimit": 0, "perpsRequoteBps": 0.5}
    base.update(kw)
    return config.merge_with_defaults(base)


def _bounce(n, *, step_ms=1000, bid=99.99, ask=100.01, qty=5.0, t0=DAY0):
    out = []
    for i in range(n):
        side = "long" if i % 2 == 0 else "short"
        out.append((t0 + i * step_ms, side, ask if side == "long" else bid, qty))
    return out


def test_flat_market_round_trips_capture_spread_minus_fees():
    tape = _bounce(400)
    r = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 400_000,
                    maker_fee=0.000125, latency_ms=0)
    assert r["fills"] > 50
    assert r["realizedPnlUsd"] >= 0
    assert r["feesUsd"] == pytest.approx(r["volumeUsd"] * 0.000125, rel=1e-9)
    assert abs(r["endPositionQty"]) * 100 <= 1000 + 1e-6


def test_at_price_prints_respect_the_queue_share():
    tape = _bounce(400)
    full = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 400_000,
                       queue_share=1.0, latency_ms=0)
    none = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 400_000,
                       queue_share=0.0, latency_ms=0)
    assert none["fills"] == 0
    assert full["fills"] > 0


def test_a_print_through_our_quote_fills_us_at_our_price():
    tape = [(DAY0 + i * 1000, "long" if i % 2 == 0 else "short",
             100.10 if i % 2 == 0 else 99.90, 5.0) for i in range(40)]
    r = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 40_000,
                    queue_share=0.0, latency_ms=0)
    assert r["fills"] > 0
    assert r["realizedPnlUsd"] > 0


def test_latency_keeps_new_quotes_out_of_the_very_next_print():
    tape = _bounce(400, step_ms=100)
    slow = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 40_000,
                       queue_share=1.0, latency_ms=10_000)
    fast = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 40_000,
                       queue_share=1.0, latency_ms=0)
    assert slow["fills"] < fast["fills"]


def test_trend_loses_and_the_markout_kill_stops_it():
    tape = []
    for i in range(3000):
        mid = 100 * (1 + i * 1e-4)
        side = "short" if i % 3 else "long"
        tape.append((DAY0 + i * 1000, side, round(mid * (0.9999 if side == "short" else 1.0001), 2), 5.0))
    tape = [(t, s, p, q) for t, s, p, q in tape]
    loose = bt.simulate(tape, inst=INST, cfg=_cfg(perpsMaxInventoryUsd=100000),
                        start_ms=DAY0, end_ms=DAY0 + 3_000_000, queue_share=1.0, latency_ms=0)
    killed = bt.simulate(tape, inst=INST, cfg=_cfg(perpsMaxInventoryUsd=100000,
                                                   perpsMarkoutKillBps=2, perpsMarkoutMinFills=5),
                         start_ms=DAY0, end_ms=DAY0 + 3_000_000, queue_share=1.0, latency_ms=0)
    assert loose["avgMarkoutBps"] is not None
    assert killed["fills"] < loose["fills"]
    assert killed["uptimePct"] < loose["uptimePct"]


def test_funding_is_charged_on_the_held_position():
    tape = _bounce(20)
    t = DAY0 + 20_000
    for i in range(20):
        tape.append((t + i * 1000, "short", 99.99, 5.0))
    r = bt.simulate(tape, funding=[(t + 30_000, 0.001)], inst=INST,
                    cfg=_cfg(perpsMaxInventoryUsd=100000),
                    start_ms=DAY0, end_ms=t + 40_000, queue_share=1.0, latency_ms=0)
    assert r["endPositionQty"] > 0
    assert r["fundingUsd"] < 0
    assert r["fundingUsd"] == pytest.approx(-r["endPositionQty"] * 100.0 * 0.001, rel=0.01)


def test_daily_loss_limit_halts_quoting():
    tape = []
    for i in range(2000):
        mid = 100 * (1 - i * 2e-4)
        tape.append((DAY0 + i * 1000, "short", round(mid * 0.9999, 2), 5.0))
    halted = bt.simulate(tape, inst=INST, cfg=_cfg(perpsMaxInventoryUsd=100000, perpsDailyLossLimit=5),
                         start_ms=DAY0, end_ms=DAY0 + 2_000_000, queue_share=1.0, latency_ms=0)
    free = bt.simulate(tape, inst=INST, cfg=_cfg(perpsMaxInventoryUsd=100000),
                       start_ms=DAY0, end_ms=DAY0 + 2_000_000, queue_share=1.0, latency_ms=0)
    assert halted["fills"] < free["fills"]


def test_reward_estimate_scales_with_uptime_and_competition():
    tape = _bounce(200)
    alone = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 200_000,
                        comp_score=0.0, pool_usd=850.0, latency_ms=0)
    crowded = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 200_000,
                          comp_score=1e6, pool_usd=850.0, latency_ms=0)
    unknown = bt.simulate(tape, inst=INST, cfg=_cfg(), start_ms=DAY0, end_ms=DAY0 + 200_000,
                          comp_score=None, pool_usd=850.0, latency_ms=0)
    assert alone["estRewardPerDayUsd"] > crowded["estRewardPerDayUsd"] > 0
    assert alone["estRewardPerDayUsd"] <= 850.0 * alone["uptimePct"] / 100 + 1e-6
    assert unknown["estRewardPerDayUsd"] is None


def test_grid_variants_are_bounded_and_clean():
    v = bt.grid_variants({"perpsQuoteBps": [1, 3, "x", 3, 99], "perpsSkewBps": [2, 6],
                          "perpsEnabled": [1]})
    assert [x["config"] for x in v] == [
        {"perpsQuoteBps": 1.0, "perpsSkewBps": 2.0}, {"perpsQuoteBps": 1.0, "perpsSkewBps": 6.0},
        {"perpsQuoteBps": 3.0, "perpsSkewBps": 2.0}, {"perpsQuoteBps": 3.0, "perpsSkewBps": 6.0}]
    big = bt.grid_variants({"perpsQuoteBps": list(range(1, 9)), "perpsSizeUsd": [10, 20, 30, 40, 50, 60]})
    assert len(big) == bt.MAX_VARIANTS
    assert bt.grid_variants({}) == []


def test_run_end_to_end_on_a_fake_exchange(tape_db, monkeypatch):
    FakeExchange(monkeypatch)
    now = int(time.time() * 1000)
    tape = [{"trade_id": i, "instrument_id": 6, "side": "long" if i % 2 else "short",
             "price": "86391" if i % 2 else "86390", "quantity": "0.01", "settlement": False,
             "timestamp": now - 3_600_000 + i * 2000} for i in range(1500)]

    async def fake_get(path, params=None, **_k):
        if path.endswith("/funding"):
            return {"data": [{"funding_rate": "0.0000125", "timestamp": now - 1_800_000}], "more": False}
        a, b = params["start_timestamp"], params["end_timestamp"]
        rows = sorted((t for t in tape if a <= t["timestamp"] <= b), key=lambda t: -t["timestamp"])
        return {"data": rows[:100], "more": len(rows) > 100}
    monkeypatch.setattr(pa, "_get", fake_get)
    monkeypatch.setattr(bt, "_MIN_SPACING", 0.0)
    cfg = config.merge_with_defaults({"perpsSelectMode": "manual", "perpsMarkets": ["BTC-USD"]})
    res = _run(bt.run(cfg, days=0.25, grid={"perpsQuoteBps": [0, 3]}))
    assert res["ok"] and res["markets"] == ["BTC-USD"]
    assert [r["name"] for r in res["runs"]] == ["Current settings", "dist 0", "dist 3"]
    assert res["runs"][1]["config"] == {"perpsQuoteBps": 0.0}
    m = res["runs"][0]["markets"][0]
    assert m["trades"] == 1500 and m["dataComplete"]
    assert res["runs"][1]["totals"]["fills"] >= res["runs"][2]["totals"]["fills"]
