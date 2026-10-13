"""Data API v2 (v1 is retired 2026-10-24): the app reads every Data API
route through v2 and translates each row back into the v1 camelCase shape
the rest of the app stores and reads (polymarket_api.v2_row_to_v1).

The fixture is live data recorded 2026-10-11: the same positions and
activity read through v1 and through v2 (wallet and display names
anonymised). Translated v2 must carry every v1 field with the v1 value."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import polymarket_api as api
import terminal

PAIRS = json.loads((Path(__file__).parent / "fixtures" / "data_api_v2_pairs.json").read_text())


def _v2(rows, cursor=None):
    return {"data": rows, "pagination": {"has_more": cursor is not None, "next_cursor": cursor}}


@pytest.mark.parametrize("kind", ["positions", "activity"])
def test_translated_v2_rows_are_the_v1_rows(kind):
    assert PAIRS[kind], "fixture is empty"
    for pair in PAIRS[kind]:
        got = api.v2_row_to_v1(pair["v2"])
        for field, want in pair["v1"].items():
            assert field in got, f"{kind}: v1 field {field!r} missing after translation"
            if isinstance(want, float) or isinstance(got[field], float):
                assert got[field] == pytest.approx(want, abs=1e-9), field
            else:
                assert got[field] == want, field


def test_the_fixture_covers_what_the_app_reads():
    statuses = {p["v2"]["status"] for p in PAIRS["positions"]}
    assert {"OPEN", "REDEEMABLE"} <= statuses
    assert {"TRADE", "REDEEM"} <= {p["v2"]["type"] for p in PAIRS["activity"]}


def test_positions_walk_the_cursor_to_the_end(monkeypatch):
    rows = [p["v2"] for p in PAIRS["positions"]]
    calls = []

    async def _get(url, params=None, **_kw):
        calls.append((url, dict(params)))
        if "cursor" not in params:
            return _v2(rows[:1], cursor="c1")
        return _v2(rows[1:], cursor=None)
    monkeypatch.setattr(api, "_get", _get)

    out = asyncio.run(api.get_positions(user="0xabc"))
    assert [c[0].endswith("/v2/positions") for c in calls] == [True, True]
    assert calls[1][1] == {"user": "0xabc", "cursor": "c1"}
    assert len(out) == len(rows)
    by_ticker = {r["ticker"]: r for r in out}
    for p in PAIRS["positions"]:
        r = by_ticker[p["v1"]["conditionId"]]
        assert r["redeemable"] == p["v1"]["redeemable"]
        assert r["cur_price"] == pytest.approx(p["v1"]["curPrice"])
        sign = 1 if p["v1"]["outcomeIndex"] == 0 else -1
        assert r["position_fp"] == pytest.approx(sign * p["v1"]["size"])
        assert r["_raw"]["avgPrice"] == p["v1"]["avgPrice"]


def test_a_failed_page_raises_not_a_short_book(monkeypatch):
    async def _get(url, params=None, **_kw):
        return _v2([PAIRS["positions"][0]["v2"]], cursor="c1") if "cursor" not in params else None
    monkeypatch.setattr(api, "_get", _get)
    with pytest.raises(RuntimeError):
        asyncio.run(api.get_positions(user="0xabc"))


def test_an_endless_cursor_raises(monkeypatch):
    async def _get(url, params=None, **_kw):
        return _v2([PAIRS["positions"][0]["v2"]], cursor="again")
    monkeypatch.setattr(api, "_get", _get)
    with pytest.raises(RuntimeError):
        asyncio.run(api.get_positions(user="0xabc"))


def test_activity_comes_back_in_the_v1_shape(monkeypatch):
    rows = [p["v2"] for p in PAIRS["activity"]]

    async def _get(url, params=None, **_kw):
        assert url.endswith("/v2/activity")
        return _v2(rows)
    monkeypatch.setattr(api, "_get", _get)
    out = asyncio.run(api.get_activity(limit=50, user="0xabc"))
    assert [r["type"] for r in out] == [r["type"] for r in rows]
    redeem = next(r for r in out if r["type"] == "REDEEM")
    want = next(p["v1"] for p in PAIRS["activity"] if p["v1"]["type"] == "REDEEM")
    assert redeem["usdcSize"] == want["usdcSize"] and redeem["conditionId"] == want["conditionId"]


def test_activity_failure_is_empty(monkeypatch):
    async def _get(url, params=None, **_kw):
        return None
    monkeypatch.setattr(api, "_get", _get)
    assert asyncio.run(api.get_activity(limit=50, user="0xabc")) == []


def _trade(idx, asset, side="BUY", price=0.35):
    return {"price": price, "size": 10, "outcomeIndex": idx, "side": side, "conditionId": "0xc",
            "transactionHash": "0xtx", "asset": asset, "slug": "s", "title": "t", "outcome": "Up"}


def test_999_resolves_from_the_token_id(monkeypatch):
    monkeypatch.setitem(api._meta, "0xc", {"yes_token": "Y", "no_token": "N"})
    up = api.normalize_trade(_trade(999, "Y"))
    assert up["taker_side"] == "yes" and up["yes_price_dollars"] == pytest.approx(0.35)
    down = api.normalize_trade(_trade(999, "N"))
    assert down["taker_side"] == "no"


def test_999_that_cannot_be_resolved_is_dropped(monkeypatch):
    monkeypatch.delitem(api._meta, "0xc", raising=False)
    assert api.normalize_trade(_trade(999, "Y")) is None
    assert api.normalize_trade(_trade(0, "Y"))["taker_side"] == "yes"
    assert api.normalize_trade(_trade(1, "N"))["taker_side"] == "no"


def test_the_terminal_tape_reads_v2_and_skips_unlabeled(monkeypatch):
    monkeypatch.delitem(api._meta, "0xc", raising=False)
    seen = {}

    async def _fetch(host, path, params, ttl):
        seen["path"], seen["params"] = path, params
        return _v2([
            {"price": 0.4, "size": 5, "outcome_index": 0, "side": "BUY", "condition_id": "0xc",
             "token_id": "Y", "timestamp": 1791718266, "proxy_wallet": "0x" + "1" * 40},
            {"price": 0.4, "size": 5, "outcome_index": 999, "side": "BUY", "condition_id": "0xc",
             "token_id": "Z", "timestamp": 1791718266, "proxy_wallet": "0x" + "1" * 40},
        ]), 0
    monkeypatch.setattr(terminal, "_fetch", _fetch)
    out = asyncio.run(terminal.tape("0xc"))
    assert seen["path"] == "/v2/trades" and seen["params"]["condition"] == "0xc"
    assert [t["side"] for t in out["trades"]] == ["yes"]


_PNL_FIRST = {"timestamp": 1745539200, "economic_pnl": 11.309786, "realized_pnl": 2.709886,
              "unrealized_pnl": 8.5999, "fees_paid": 0.0, "maker_rebate": 0.0,
              "volume_usdc": 1159.414725, "trade_count": 8}
_PNL_LAST = {"timestamp": 1791729961, "economic_pnl": 387491.669652, "realized_pnl": 362094.730552,
             "unrealized_pnl": -26012.931, "fees_paid": -130264.630101, "maker_rebate": 25415.7489,
             "volume_usdc": 20624535.439651, "trade_count": 67009}


def test_all_time_pnl_is_the_last_cumulative_value(monkeypatch):
    """'All' used last minus first like every window, which dropped the
    wallet's whole first bucket: a day for an old wallet, most of the history
    for a young one."""
    import data_v2

    async def _get(path, params, *, ttl):
        assert path == "/user-pnl"
        return {"points": [_PNL_LAST, _PNL_FIRST]}
    monkeypatch.setattr(data_v2, "_get", _get)
    out = asyncio.run(data_v2.user_pnl("0xabc", "max"))
    assert out["economic_pnl"] == pytest.approx(387491.669652)
    assert out["trade_count"] == 67009 and out["volume_usdc"] == pytest.approx(20624535.439651)
    assert out["unrealized_pnl"] == pytest.approx(-26012.931)
    assert out["series"][0][1] == pytest.approx(11.309786, abs=1e-3)
    assert out["series"][-1][1] == pytest.approx(387491.669652, abs=1e-3)
    week = asyncio.run(data_v2.user_pnl("0xabc", "1w"))
    assert week["economic_pnl"] == pytest.approx(387491.669652 - 11.309786)
    assert week["trade_count"] == 67009 - 8 and week["series"][0][1] == 0.0
