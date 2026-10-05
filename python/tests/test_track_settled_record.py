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
    tt._resolution_scope_check()
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
                 r.get("at", f"2026-01-01T00:00:{i:02d}Z")))


def _record(monkeypatch, *, wallet=(), markets=None, wallet_error=False,
            calls=None):
    markets = dict(markets or {})

    async def _positions(*_a, **_kw):
        if wallet_error:
            raise RuntimeError("data-api is down")
        return [
            {"ticker": w["cid"], "title": w.get("title", "T"),
             "position_fp": w["size"] if w.get("side", "yes") == "yes" else -w["size"],
             "cur_price": w.get("price", 0.0), "redeemable": False,
             "_raw": None}
            for w in wallet
        ]

    async def _summaries(ids):
        if calls is not None:
            calls.append(list(ids))
        out = []
        for cid in ids:
            m = markets.get(cid)
            if m is None:
                continue
            out.append({
                "conditionId": cid, "title": m.get("title", "T"), "slug": None,
                "status": "settled" if m.get("outcome") else "open",
                "resolvedOutcome": m.get("outcome"),
                "outcomes": m.get("labels", ["Up", "Down"]),
                "closeTime": m.get("close"), "probability": 50.0,
            })
        return out

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    return _run(tt.settled_record())


def test_a_redeemed_winner_is_in_the_record_and_not_in_the_wallet(fresh_db, monkeypatch):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0, "fee": 0.10}])
    r = _record(monkeypatch, wallet=[],
                markets={BTC_UP: {"outcome": "yes", "labels": ["Up", "Down"],
                                  "close": "2026-01-02T00:00:00Z"}})

    assert len(r["rows"]) == 1
    row = r["rows"][0]
    assert row["outcome"] == "won"
    assert row["inWallet"] is False, "a redeemed winner is gone from the wallet"
    assert row["contracts"] == 5.0
    assert row["payoutUsd"] == pytest.approx(5.0)
    assert row["costUsd"] == pytest.approx(2.0)
    assert row["pnlUsd"] == pytest.approx(2.90)
    assert row["avgCostCents"] == pytest.approx(40.0)
    assert row["sideLabel"] == "Up"
    assert row["resolvedOutcomeLabel"] == "Up"
    assert row["settledAt"] == "2026-01-02T00:00:00Z"
    assert r["totals"]["won"] == 1 and r["totals"]["lost"] == 0


def test_a_dust_loser_is_in_the_record_and_still_in_the_wallet(fresh_db, monkeypatch):
    _fills([{"cid": DOGE_DOWN, "side": "no", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _record(
        monkeypatch,
        wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0, "price": 0.0}],
        markets={DOGE_DOWN: {"outcome": "yes", "labels": ["Up", "Down"]}})

    row = r["rows"][0]
    assert row["outcome"] == "lost"
    assert row["inWallet"] is True, "a resolved loser sits in the wallet as dust"
    assert row["payoutUsd"] == pytest.approx(0.0)
    assert row["pnlUsd"] == pytest.approx(-2.0)
    assert row["sideLabel"] == "Down"
    assert row["settledAt"] is None
    assert r["totals"]["lost"] == 1 and r["totals"]["won"] == 0


def test_the_record_shows_the_wins_the_wallet_cannot(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 45.0},
        {"cid": GIANTS, "side": "no", "action": "buy", "size": 5.0, "price": 50.0},
        {"cid": DOGE_DOWN, "side": "no", "action": "buy", "size": 5.0, "price": 40.0},
    ])
    markets = {BTC_UP: {"outcome": "yes"}, ETH_UP: {"outcome": "yes"},
               GIANTS: {"outcome": "no"}, DOGE_DOWN: {"outcome": "yes"}}
    r = _record(monkeypatch,
                wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0}],
                markets=markets)

    assert r["totals"]["won"] == 3
    assert r["totals"]["lost"] == 1
    assert r["totals"]["markets"] == 4
    assert [x["outcome"] for x in r["rows"] if x["outcome"] == "won"].count("won") == 3
    assert r["totals"]["pnlUsd"] == pytest.approx(6.25)
    assert sorted(x["inWallet"] for x in r["rows"]) == [False, False, False, True]


def test_a_closed_out_market_has_no_payout_and_its_pnl_is_the_sells(fresh_db, monkeypatch):
    _fills([
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 10.0,
         "price": 50.0, "fee": 0.20},
        {"cid": ETH_UP, "side": "yes", "action": "sell", "size": 10.0,
         "price": 60.0, "fee": 0.25},
    ])
    r = _record(monkeypatch, wallet=[], markets={ETH_UP: {"outcome": "yes"}})

    row = r["rows"][0]
    assert row["outcome"] == "closedOut"
    assert row["payoutUsd"] is None, "a market we sold out of paid us nothing"
    assert row["contracts"] == 0.0
    assert row["avgCostCents"] is None, "no contracts left to have an average"
    assert row["soldPnlUsd"] == pytest.approx(1.0)
    assert row["pnlUsd"] == pytest.approx(0.55)
    assert r["totals"]["closedOut"] == 1
    assert r["totals"]["won"] == 0 and r["totals"]["lost"] == 0


def test_an_unreadable_outcome_is_unknown_never_a_loss(fresh_db, monkeypatch):
    _fills([{"cid": GIANTS, "side": "yes", "action": "buy",
             "size": 4.0, "price": 30.0}])
    r = _record(monkeypatch, wallet=[], markets={})

    row = r["rows"][0]
    assert row["outcome"] == "unknown"
    assert row["payoutUsd"] is None and row["pnlUsd"] is None
    assert row["sideLabel"] is None, "no labels means no invented outcome word"
    assert r["totals"]["unknown"] == 1
    assert r["totals"]["lost"] == 0, "an unknown outcome was counted as a loss"
    assert r["totals"]["pnlUsd"] is None
    assert r["totals"]["pnlMissingRows"] == 1
    assert "could not read" in r["note"]


def test_a_market_held_on_both_sides_is_labelled_not_double_counted(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10.0, "price": 51.0},
        {"cid": BTC_UP, "side": "no", "action": "buy", "size": 10.0, "price": 48.0},
    ])
    r = _record(monkeypatch, wallet=[],
                markets={BTC_UP: {"outcome": "yes", "labels": ["Up", "Down"]}})

    assert len(r["rows"]) == 2
    assert all(x["bothSides"] for x in r["rows"])
    assert {x["outcome"] for x in r["rows"]} == {"won", "lost"}
    t = r["totals"]
    assert (t["won"], t["lost"], t["bothSides"], t["markets"]) == (1, 1, 1, 1)
    assert t["won"] + t["lost"] != t["markets"], "the counts pretend to add up"
    assert t["pnlUsd"] == pytest.approx(0.10)


def test_a_residual_too_small_to_redeem_does_not_score(fresh_db, monkeypatch):
    _fills([
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 10.0, "price": 50.0},
        {"cid": ETH_UP, "side": "yes", "action": "sell", "size": 9.92, "price": 60.0},
    ])
    r = _record(monkeypatch, wallet=[], markets={ETH_UP: {"outcome": "yes"}})

    row = r["rows"][0]
    assert row["outcome"] == "closedOut"
    assert row["contracts"] == pytest.approx(0.08)
    assert r["totals"]["won"] == 0


def _portfolio(monkeypatch, *, wallet=(), markets=None):
    markets = dict(markets or {})

    async def _positions(*_a, **_kw):
        return [
            {"ticker": w["cid"], "title": "T",
             "position_fp": w["size"] if w.get("side", "yes") == "yes" else -w["size"],
             "cur_price": w.get("price", 0.0), "redeemable": False, "_raw": None}
            for w in wallet
        ]

    async def _summaries(ids):
        return [{"conditionId": c, "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": markets[c].get("outcome"),
                 "outcomes": markets[c].get("labels", ["Up", "Down"]),
                 "closeTime": markets[c].get("close"), "probability": 50.0}
                for c in ids if c in markets]

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


def test_the_totals_are_the_same_counts_the_header_reports(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0, "fee": 0.05},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 45.0, "fee": 0.05},
        {"cid": GIANTS, "side": "no", "action": "buy", "size": 5.0, "price": 50.0, "fee": 0.05},
        {"cid": DOGE_DOWN, "side": "no", "action": "buy", "size": 5.0, "price": 40.0, "fee": 0.05},
        {"cid": GIANTS, "side": "yes", "action": "buy", "size": 4.0, "price": 20.0, "fee": 0.02},
        {"cid": GIANTS, "side": "yes", "action": "sell", "size": 4.0, "price": 30.0, "fee": 0.02},
    ])
    markets = {BTC_UP: {"outcome": "yes"}, ETH_UP: {"outcome": "yes"},
               GIANTS: {"outcome": "no"}, DOGE_DOWN: {"outcome": "yes"}}
    wallet = [{"cid": DOGE_DOWN, "side": "no", "size": 5.0}]

    rec = _record(monkeypatch, wallet=wallet, markets=markets)
    pf = _portfolio(monkeypatch, wallet=wallet, markets=markets)

    assert rec["totals"]["won"] == pf["wonMarkets"]
    assert rec["totals"]["lost"] == pf["lostMarkets"]
    assert rec["totals"]["markets"] == pf["ledgerMarkets"]
    assert rec["totals"]["bothSides"] == pf["bothSidesMarkets"]
    assert rec["totals"]["closedOut"] == pf["closedOutMarkets"]
    assert rec["totals"]["pnlUsd"] == pytest.approx(pf["realizedPnlNetUsd"])
    assert rec["totals"]["feesUsd"] == pytest.approx(pf["realizedFeesUsd"])


def test_the_record_is_newest_first(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 1.0,
         "price": 40.0, "at": "2026-01-01T00:00:00Z"},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 1.0,
         "price": 40.0, "at": "2026-03-01T00:00:00Z"},
        {"cid": GIANTS, "side": "yes", "action": "buy", "size": 1.0,
         "price": 40.0, "at": "2026-02-01T00:00:00Z"},
    ])
    r = _record(monkeypatch, wallet=[], markets={
        BTC_UP: {"outcome": "yes", "close": "2026-01-02T00:00:00Z"},
        ETH_UP: {"outcome": "yes", "close": "2026-03-02T00:00:00Z"},
        GIANTS: {"outcome": "yes", "close": "2026-02-02T00:00:00Z"},
    })
    assert [x["conditionId"] for x in r["rows"]] == [ETH_UP, GIANTS, BTC_UP]


def test_an_unreadable_wallet_leaves_in_wallet_unknown_not_false(fresh_db, monkeypatch):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _record(monkeypatch, wallet_error=True,
                markets={BTC_UP: {"outcome": "yes"}})

    assert r["walletReadable"] is False
    assert r["rows"][0]["inWallet"] is None
    assert r["rows"][0]["outcome"] == "won"


def test_an_unknown_fee_leaves_the_pnl_unknown_not_gross(fresh_db, monkeypatch):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0,
             "price": 40.0, "fee": None}])
    r = _record(monkeypatch, wallet=[], markets={BTC_UP: {"outcome": "yes"}})

    row = r["rows"][0]
    assert row["feesKnown"] is False
    assert row["feesUsd"] is None
    assert row["pnlUsd"] is None, "a net figure was printed over unknown fees"
    assert row["payoutUsd"] == pytest.approx(5.0)
    assert r["totals"]["feesKnown"] is False
    assert r["totals"]["feesUsd"] is None


def test_an_empty_ledger_says_so_rather_than_showing_a_clean_slate(fresh_db, monkeypatch):
    _fills([])
    r = _record(monkeypatch, wallet=[], markets={})

    assert r["rows"] == []
    assert r["totals"]["markets"] == 0
    assert r["totals"]["pnlUsd"] is None, "an empty ledger reported $0.00 P&L"
    assert "Reconcile fills" in r["note"]


def test_a_resolved_market_is_asked_about_once_and_then_cached(fresh_db, monkeypatch):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    calls: list[list[str]] = []
    markets = {BTC_UP: {"outcome": "yes"}}

    first = _record(monkeypatch, wallet=[], markets=markets, calls=calls)
    assert first["rows"][0]["outcome"] == "won"
    assert calls, "the first read never asked"
    calls.clear()

    second = _record(monkeypatch, wallet=[], markets=markets, calls=calls)
    assert second["rows"][0]["outcome"] == "won"
    assert calls == [], "a resolved outcome was looked up twice"
