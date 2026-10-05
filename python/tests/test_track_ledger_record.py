from __future__ import annotations

import asyncio

import pytest

import db
import terminal_trade as tt

BTC_UP = "0xf996adbcb9d5e40162d514d1616325982ae9ef5a48b2622db107018df80e8884"
DOGE_DOWN = "0x322f0d6502dc08f4f61c06d9fbba252711ac250a625330f92984ff4d0cb423e7"
GIANTS = "0x38644f38c63679e539c578c59f4a1be38eb4693d1bdaf0ead50f70b824efb6ba"
ETH_UP = "0x7d13e4a513aeabd0f0ed3c5703b0195d5feb6e23d17ccdac86f8a1ff41e3635a"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "record.db")
    db.init_db()
    return tmp_path


def _fills(rows):
    with db.get_db() as conn:
        conn.execute("DELETE FROM terminal_fills")
        for i, r in enumerate(rows):
            conn.execute(
                """INSERT INTO terminal_fills
                   (trade_id, order_id, condition_id, title, side, action,
                    size, price_cents, fees_usd, cost_usd, origin, network,
                    filled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"t{i}", None, r["cid"], r.get("title", "T"),
                 r.get("side", "yes"), r["action"], r["size"], r["price"],
                 r.get("fee", 0.0), r["size"] * r["price"] / 100.0,
                 r.get("origin", "manual"), "mainnet",
                 f"2026-01-01T00:00:{i:02d}Z"))


def _portfolio(monkeypatch, *, wallet=(), resolved=None, calls=None):
    resolved = dict(resolved or {})

    async def _positions(*a, **kw):
        return [
            {"ticker": w["cid"], "title": w.get("title", "T"),
             "position_fp": w["size"] if w.get("side", "yes") == "yes" else -w["size"],
             "cur_price": w.get("price", 0.0),
             "redeemable": bool(w.get("redeemable")),
             "_raw": w.get("raw")}
            for w in wallet
        ]

    async def _summaries(ids):
        if calls is not None:
            calls.append(list(ids))
        out = []
        for cid in ids:
            if cid not in resolved:
                continue
            out.append({
                "conditionId": cid, "title": "T", "slug": None,
                "status": "settled" if resolved[cid] else "open",
                "resolvedOutcome": resolved[cid],
                "outcomes": ["Up", "Down"],
                "closeTime": "2026-01-01T00:00:00Z", "probability": 50.0,
            })
        return out

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


def test_a_redeemed_winner_absent_from_the_wallet_still_counts(fresh_db, monkeypatch):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["positions"] == [], "the winner really is out of the wallet"
    assert r["realizedPnlUsd"] == pytest.approx(3.0)
    assert r["settledPnlUsd"] == pytest.approx(3.0)
    assert r["wonMarkets"] == 1
    assert r["lostMarkets"] == 0
    assert r["ledgerMarkets"] == 1


def test_a_losing_dust_position_counts_as_a_loss(fresh_db, monkeypatch):
    _fills([{"cid": DOGE_DOWN, "side": "no", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _portfolio(
        monkeypatch,
        wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0, "price": 0.0,
                 "redeemable": True}],
        resolved={DOGE_DOWN: "yes"})

    assert r["realizedPnlUsd"] == pytest.approx(-2.0)
    assert r["lostMarkets"] == 1
    assert r["wonMarkets"] == 0
    assert r["settledWorthlessCount"] == 1
    assert r["redeemableCount"] == 0


def test_the_wallet_alone_reports_every_bet_as_a_loss(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 45.0},
        {"cid": GIANTS, "side": "no", "action": "buy", "size": 5.0, "price": 50.0},
        {"cid": DOGE_DOWN, "side": "no", "action": "buy", "size": 5.0, "price": 40.0},
    ])
    r = _portfolio(
        monkeypatch,
        wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0, "price": 0.0}],
        resolved={BTC_UP: "yes", ETH_UP: "yes", GIANTS: "no", DOGE_DOWN: "yes"})

    assert r["wonMarkets"] == 3
    assert r["lostMarkets"] == 1
    assert r["ledgerMarkets"] == 4
    assert r["realizedPnlUsd"] == pytest.approx(6.25)
    assert r["realizedPnlUsd"] > 0, "a profitable ledger reported as a loss"
    assert r["walletPnlUsd"] == pytest.approx(-2.0)


def test_a_sold_out_unresolved_market_realises_its_sells_only(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10.0, "price": 50.0},
        {"cid": BTC_UP, "side": "yes", "action": "sell", "size": 10.0, "price": 60.0},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: None})

    assert r["realizedPnlUsd"] == pytest.approx(1.0)
    assert r["settledPnlUsd"] is None, "an open market settled something"
    assert r["wonMarkets"] == 0
    assert r["lostMarkets"] == 0
    assert r["closedOutMarkets"] == 1
    assert r["unknownResolutionMarkets"] == 0


def test_a_sub_contract_remainder_does_not_score(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy",
         "size": 5.0794, "price": 64.64},
        {"cid": BTC_UP, "side": "yes", "action": "sell",
         "size": 5.0, "price": 60.35},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["wonMarkets"] == 0, "a 0.08-contract remainder scored as a win"
    assert r["lostMarkets"] == 0
    assert r["closedOutMarkets"] == 1
    assert r["settledPnlUsd"] == pytest.approx(0.03, abs=0.01)


def test_both_sides_held_to_settlement_land_in_both_columns(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 55.0},
        {"cid": BTC_UP, "side": "no", "action": "buy", "size": 5.0, "price": 48.0},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["wonMarkets"] == 1
    assert r["lostMarkets"] == 1
    assert r["bothSidesMarkets"] == 1
    assert r["ledgerMarkets"] == 1
    assert r["realizedPnlUsd"] == pytest.approx(-0.15)
    assert "BOTH columns" in (r["recordNote"] or "")


def test_an_unreadable_outcome_is_counted_never_assumed_lost(fresh_db, monkeypatch):
    _fills([{"cid": GIANTS, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _portfolio(monkeypatch, wallet=[], resolved={})

    assert r["unknownResolutionMarkets"] == 1
    assert r["wonMarkets"] == 0
    assert r["lostMarkets"] == 0, "an unreadable market was scored as a loss"
    assert r["settledPnlUsd"] is None
    assert r["realizedPnlUsd"] is None, "an unknowable total was reported as $0"
    assert "could not read" in (r["recordNote"] or "")


def test_one_unknown_market_does_not_void_the_rest(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
        {"cid": GIANTS, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["realizedPnlUsd"] == pytest.approx(3.0)
    assert r["wonMarkets"] == 1
    assert r["unknownResolutionMarkets"] == 1
    assert r["ledgerMarkets"] == 2


def test_fees_are_reported_and_netted_rather_than_buried(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy",
         "size": 5.0, "price": 40.0, "fee": 0.08},
        {"cid": DOGE_DOWN, "side": "yes", "action": "buy",
         "size": 5.0, "price": 40.0, "fee": 0.08},
    ])
    r = _portfolio(monkeypatch, wallet=[],
                   resolved={BTC_UP: "yes", DOGE_DOWN: "no"})

    assert r["realizedPnlUsd"] == pytest.approx(1.0)
    assert r["realizedFeesUsd"] == pytest.approx(0.16)
    assert r["realizedPnlNetUsd"] == pytest.approx(0.84)


def test_a_resolved_outcome_is_asked_for_once_and_then_remembered(fresh_db, monkeypatch):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    calls: list = []
    first = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"},
                       calls=calls)
    asked_first = sum(len(c) for c in calls)
    calls.clear()
    second = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"},
                        calls=calls)

    assert asked_first >= 1, "the first pass never looked the market up"
    assert sum(len(c) for c in calls) == 0, "a settled outcome was re-fetched"
    assert second["realizedPnlUsd"] == first["realizedPnlUsd"]
    assert second["wonMarkets"] == 1


def test_the_cache_does_not_survive_the_database_moving(fresh_db, monkeypatch,
                                                        tmp_path):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})
    assert tt._resolutions_cached().get(BTC_UP) == "yes"

    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "other.db")
    db.init_db()
    assert tt._resolutions_cached().get(BTC_UP) is None, "cache crossed databases"
