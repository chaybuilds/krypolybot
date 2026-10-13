"""The Track screen, pinned against the cases that were actually wrong.

Every fixture in this file is shaped from a REAL wallet read (Polymarket Data
API `positions` + CLOB `/data/trades`) taken while diagnosing five complaints
about this page: no cost basis, "it says I lost most trades", everything badged
redeemable, wrong P&L, and every row labelled "placed elsewhere". The wallet
address is not here — only the public market ids, outcome labels and the
arithmetic, which is what the bugs lived in.

The one thing every test below is really guarding: OUTCOME IDENTITY IS AN
INDEX, NOT A WORD. Polymarket's outcome 0 is spelled "Yes" on a binary market,
"Up" on a crypto one, "Over" on a total, and a team or player name on a sports
one. The old code asked ``outcome == "yes"`` and filed everything else under
'no', so 823 of this wallet's 1,502 fills sat on the opposite key from the
position they paid for — and a position whose buys and sells land on different
keys has no cost basis at all.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import terminal_trade as tt

BTC_UP = "0xf996adbcb9d5e40162d514d1616325982ae9ef5a48b2622db107018df80e8884"
DOGE_DOWN = "0x322f0d6502dc08f4f61c06d9fbba252711ac250a625330f92984ff4d0cb423e7"
GIANTS = "0x38644f38c63679e539c578c59f4a1be38eb4693d1bdaf0ead50f70b824efb6ba"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "track.db")
    db.init_db()
    return tmp_path


_UPDOWN = {
    "conditionId": BTC_UP,
    "title": "Bitcoin Up or Down - July 23, 10:30PM-10:45PM ET",
    "outcomes": ["Up", "Down"],
    "yesTokenId": "734509322047132654043984907374749575921584258190061221982202671871738768366",
    "noTokenId": "81079414737192420698352096291218615017298946394104804308893688101588173084917",
}
_SPORTS = {
    "conditionId": GIANTS,
    "title": "Atlanta Braves vs. San Francisco Giants",
    "outcomes": ["Atlanta Braves", "San Francisco Giants"],
    "yesTokenId": "111", "noTokenId": "222",
}


def test_an_up_market_fill_is_outcome_zero_even_though_it_is_not_spelled_yes():
    """The exact bug. A "Bitcoin Up or Down" buy of the UP side is outcome 0 —
    the same slot "Yes" occupies on a binary market — and the position it pays
    for is keyed 'yes' off `outcomeIndex`. Reading the word instead put it on
    'no', and the market silently lost its cost basis."""
    assert tt._side_from_token(_UPDOWN["yesTokenId"], "Up", _UPDOWN) == "yes"
    assert tt._side_from_token(_UPDOWN["noTokenId"], "Down", _UPDOWN) == "no"


def test_a_named_outcome_maps_through_the_markets_own_outcome_list():
    """A sports market's outcomes are two team names. Neither is "yes", so the
    old rule filed BOTH sides of the same market under 'no' — buys of the
    Braves and buys of the Giants netting against each other on one key."""
    assert tt._side_from_token("111", "Atlanta Braves", _SPORTS) == "yes"
    assert tt._side_from_token("222", "San Francisco Giants", _SPORTS) == "no"
    assert tt._side_from_token("", "San Francisco Giants", _SPORTS) == "no"


def test_the_token_id_beats_a_label_that_disagrees_with_it():
    """The token id IS the outcome. If the label and the token ever disagree,
    the token wins — a mislabelled row must not move money onto the wrong key."""
    assert tt._side_from_token(_UPDOWN["noTokenId"], "Up", _UPDOWN) == "no"


def test_a_fill_we_cannot_place_is_unknown_rather_than_guessed():
    """No token match, no outcome list, no known word. Filing it under a guess
    would put buys and sells on different keys and destroy a cost basis, so the
    honest answer is None — and the caller leaves it out and says so."""
    assert tt._side_from_token("", "Karolina Muchova", None) is None
    assert tt._side_from_token("999", "", {"outcomes": ["Up", "Down"]}) is None


def test_the_known_vocabulary_still_works_with_no_market_metadata():
    """A degraded fallback for when the market lookup failed, covering the
    outcome words Polymarket actually ships. Index 0 words map to 'yes'."""
    for word in ("Yes", "UP", "Over"):
        assert tt._side_from_token("", word, None) == "yes"
    for word in ("No", "down", "Under"):
        assert tt._side_from_token("", word, None) == "no"



def _reconcile(monkeypatch, trades, summaries):
    async def _req(_method, _path, *, params=None, **kw):
        return {"data": list(trades), "next_cursor": "LTE="}

    async def _summaries(_ids):
        return [dict(s, status="settled", category=None, _raw={"feesEnabled": False})
                for s in summaries]

    monkeypatch.setattr(tt.api, "_authed_request", _req)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    return _run(tt.reconcile_fills())


def _trade(tid, *, cid, asset, outcome, side="BUY", price="0.03", size="33.3333"):
    return {"id": tid, "market": cid, "asset_id": asset, "side": side,
            "outcome": outcome, "price": price, "size": size,
            "match_time": "1785718593", "taker_order_id": "", "maker_orders": []}


def _fills():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM terminal_fills ORDER BY trade_id")]


def test_an_up_down_fill_imports_onto_the_side_the_position_lives_on(
        monkeypatch, fresh_db):
    r = _reconcile(monkeypatch,
                   [_trade("t1", cid=BTC_UP, asset=_UPDOWN["yesTokenId"],
                           outcome="Up")],
                   [_UPDOWN])
    assert r["inserted"] == 1 and r["unmapped"] == 0
    assert _fills()[0]["side"] == "yes"


def test_a_mis_filed_fill_is_repaired_by_the_next_reconcile(monkeypatch, fresh_db):
    """The walk has no cursor — it re-reads the venue's WHOLE history every
    pass. That is what makes repair possible, and an INSERT OR IGNORE threw it
    away: 823 fills stayed on the wrong outcome forever because the row already
    existed. Every column of a fill is DERIVED from the venue's trade record,
    so re-deriving them is correcting our reading of Polymarket's ledger, not
    overwriting the user's data."""
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO terminal_fills
                 (trade_id, order_id, condition_id, title, side, action, size,
                  price_cents, fees_usd, cost_usd, origin, network, filled_at)
               VALUES ('t1', NULL, ?, '', 'no', 'buy', 33.3333, 3.0, NULL,
                       0.9999, 'external', 'mainnet', '2026-07-23T22:30:00Z')""",
            (BTC_UP,))

    r = _reconcile(monkeypatch,
                   [_trade("t1", cid=BTC_UP, asset=_UPDOWN["yesTokenId"],
                           outcome="Up")],
                   [_UPDOWN])
    assert r["corrected"] == 1
    assert r["inserted"] == 0, "a repair is not a new fill"
    assert "wrong outcome" in (r["note"] or "")

    f = _fills()[0]
    assert f["side"] == "yes"
    assert f["title"] == _UPDOWN["title"], "the market's name should backfill"
    assert f["size"] == pytest.approx(33.3333)
    assert f["price_cents"] == 3.0
    assert f["cost_usd"] == pytest.approx(1.0, abs=0.001)
    assert f["origin"] == "external"


OUR_ADDR = "0x1111111111111111111111111111111111111111"
SOL_UP_TOKEN = "37897892725419593180475175176045621812040618671614474599612951860070331688610"
SOL_DOWN_TOKEN = "4328432964064868468872014434002136536448862940281065916646296977873570185942"
_SOL = {
    "conditionId": "0x2f39", "title": "Solana Up or Down - June 27",
    "outcomes": ["Up", "Down"],
    "yesTokenId": SOL_UP_TOKEN, "noTokenId": SOL_DOWN_TOKEN,
}

_MAKER_TRADE = {
    "id": "e87a8c40", "market": "0x2f39", "match_time": "1782000000",
    "side": "BUY", "outcome": "Down", "asset_id": SOL_DOWN_TOKEN,
    "price": "0.7", "size": "10",
    "taker_order_id": "0xdfb2",
    "trader_side": "MAKER",
    "maker_orders": [{
        "order_id": "0xmine", "maker_address": OUR_ADDR, "outcome": "Up",
        "asset_id": SOL_UP_TOKEN, "price": "0.3", "matched_amount": "10",
        "side": "BUY",
    }],
}


def test_a_maker_fill_records_our_leg_not_the_takers(monkeypatch, fresh_db):
    monkeypatch.setattr(tt.auth, "trading_address", lambda: OUR_ADDR)
    r = _reconcile(monkeypatch, [_MAKER_TRADE], [_SOL])
    assert r["inserted"] == 1 and r["unmapped"] == 0
    f = _fills()[0]
    assert f["side"] == "yes", "we bought UP; the taker bought DOWN"
    assert f["price_cents"] == 30.0, "70c was the other side of the match"
    assert f["size"] == 10.0
    assert f["cost_usd"] == pytest.approx(3.0)
    assert f["fees_usd"] == 0.0


def test_a_taker_fill_still_reads_the_top_level(monkeypatch, fresh_db):
    monkeypatch.setattr(tt.auth, "trading_address", lambda: OUR_ADDR)
    taker = dict(_MAKER_TRADE, id="f5535708", trader_side="TAKER",
                 side="BUY", outcome="Up", asset_id=SOL_UP_TOKEN,
                 price="0.3", size="20",
                 maker_orders=[{
                     "order_id": "0xsomeone", "maker_address": "0xother",
                     "outcome": "Down", "asset_id": SOL_DOWN_TOKEN,
                     "price": "0.7", "matched_amount": "20", "side": "BUY"}])
    _reconcile(monkeypatch, [taker], [_SOL])
    f = _fills()[0]
    assert f["side"] == "yes" and f["price_cents"] == 30.0 and f["size"] == 20.0


def test_a_wrong_leg_fill_already_in_the_ledger_is_re_derived(
        monkeypatch, fresh_db):
    """The repair path for the money, not just the outcome. This is the row
    that made one real position show 20 UP + 10 DOWN against a venue that said
    30 UP at an average of 30c."""
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO terminal_fills
                 (trade_id, order_id, condition_id, title, side, action, size,
                  price_cents, fees_usd, cost_usd, origin, network, filled_at)
               VALUES ('e87a8c40', NULL, '0x2f39', '', 'no', 'buy', 10.0, 70.0,
                       NULL, 7.0, 'external', 'mainnet', '2026-06-27T04:39:48Z')""")
    monkeypatch.setattr(tt.auth, "trading_address", lambda: OUR_ADDR)
    r = _reconcile(monkeypatch, [_MAKER_TRADE], [_SOL])
    assert r["corrected"] == 1
    f = _fills()[0]
    assert (f["side"], f["price_cents"], f["cost_usd"]) == ("yes", 30.0, 3.0)


def test_an_ambiguous_maker_match_is_left_out_rather_than_picked(
        monkeypatch, fresh_db):
    """Two maker legs, neither carrying an address we recognise. Choosing one
    would be a coin flip written into the cost basis."""
    monkeypatch.setattr(tt.auth, "trading_address",
                        lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    ambiguous = dict(_MAKER_TRADE, maker_orders=[
        {"order_id": "a", "maker_address": "0xa", "outcome": "Up",
         "asset_id": SOL_UP_TOKEN, "price": "0.3", "matched_amount": "5",
         "side": "BUY"},
        {"order_id": "b", "maker_address": "0xb", "outcome": "Down",
         "asset_id": SOL_DOWN_TOKEN, "price": "0.7", "matched_amount": "5",
         "side": "BUY"},
    ])
    r = _reconcile(monkeypatch, [ambiguous], [_SOL])
    assert r["unmapped"] == 1
    assert _fills() == []


def test_an_unplaceable_fill_is_left_out_and_counted_out_loud(
        monkeypatch, fresh_db):
    r = _reconcile(monkeypatch,
                   [_trade("t1", cid=BTC_UP, asset="not-a-token",
                           outcome="Karolina Muchova")],
                   [{"conditionId": BTC_UP, "title": "T"}])
    assert r["unmapped"] == 1
    assert _fills() == []
    assert "guess" in (r["note"] or "")



def _seed(rows):
    with db.get_db() as conn:
        for i, r in enumerate(rows):
            conn.execute(
                """INSERT INTO terminal_fills
                     (trade_id, order_id, condition_id, title, side, action,
                      size, price_cents, fees_usd, cost_usd, origin, network,
                      filled_at)
                   VALUES (?,NULL,?,'',?,?,?,?,0.0,?,?,'mainnet',?)""",
                (f"f{i}", r["cid"], r["side"], r["action"], r["size"],
                 r["price"], r["size"] * r["price"] / 100.0,
                 r.get("origin", "external"),
                 f"2026-07-0{i + 1}T00:00:00Z"))


def _portfolio(monkeypatch, positions, summaries):
    async def _positions(*a, **kw):
        return positions

    async def _summaries(_ids):
        return summaries

    async def _balance():
        raise RuntimeError("no wallet in tests")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


def _pos(cid, *, idx, size, avg, current=0.0, redeemable=True, outcome="Up",
         title="M", cur_price=0.0):
    """One Data API position, in the shape `get_positions` hands us."""
    return {
        "ticker": cid, "title": title,
        "position_fp": size if idx == 0 else -size,
        "cur_price": cur_price, "redeemable": redeemable,
        "_raw": {
            "conditionId": cid, "size": size, "avgPrice": avg,
            "initialValue": round(size * avg, 4), "currentValue": current,
            "cashPnl": round(current - size * avg, 4),
            "realizedPnl": -0.0679, "curPrice": cur_price,
            "redeemable": redeemable, "outcome": outcome, "outcomeIndex": idx,
            "title": title,
        },
    }


def _sum(cid, *, outcomes, resolved, title="M"):
    return {"conditionId": cid, "title": title, "slug": "s",
            "status": "settled" if resolved else "open",
            "outcomes": list(outcomes), "resolvedOutcome": resolved,
            "closeTime": "2026-07-24T00:00:00Z"}


def test_an_engine_placed_fill_still_gives_the_position_a_cost_basis(
        monkeypatch, fresh_db):
    """origin='external' is what every fill on this wallet was stamped, because
    the engines do not write terminal_orders rows. Letting origin decide whether
    a position HAS a basis meant a wallet that had spent $634 showed a cost of
    nothing. Origin picks which track record a trade counts toward; it never
    decides whether the money was spent."""
    _seed([{"cid": BTC_UP, "side": "yes", "action": "buy", "size": 33.3333,
            "price": 3.0, "origin": "external"}])
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["avgCostCents"] == 3.0
    assert p["basisSource"] == "ledger"
    assert p["unreconciled"] is False
    assert r["totalCostUsd"] == pytest.approx(1.0, abs=0.01)


def test_a_position_our_ledger_cannot_reach_uses_the_venues_own_average(
        monkeypatch, fresh_db):
    """History older than anything we ever imported. The venue keeps its own
    average for THIS wallet, so showing "—" for a position we can see the cost
    of is a worse answer than showing theirs and saying whose it is."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["avgCostCents"] == 2.99
    assert p["basisSource"] == "data-api"
    assert p["sources"]["avgCost"]["source"] == "data-api", "provenance is mandatory"
    assert p["unreconciled"] is False, "a basis is a basis, wherever it came from"
    assert r["venueBasisCount"] == 1
    assert "Polymarket's own average" in (r["unreconciledNote"] or "")


def test_with_no_basis_anywhere_it_stays_unknown_and_out_of_the_totals(
        monkeypatch, fresh_db):
    raw_less = _pos(BTC_UP, idx=0, size=10, avg=0.5)
    raw_less.pop("_raw")
    r = _portfolio(monkeypatch, [raw_less],
                   [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["avgCostCents"] is None and p["basisSource"] is None
    assert p["unrealizedPnlUsd"] is None and p["settledPnlUsd"] is None
    assert p["unreconciled"] is True
    assert r["unreconciledCount"] == 1
    assert r["totalCostUsd"] is None



@pytest.mark.parametrize("outcomes,label", [
    (["Yes", "No"], "Yes"),
    (["Up", "Down"], "Up"),
    (["Over", "Under"], "Over"),
    (["Atlanta Braves", "San Francisco Giants"], "Atlanta Braves"),
])
def test_holding_the_winning_outcome_reads_as_a_win_in_every_vocabulary(
        monkeypatch, fresh_db, outcomes, label):
    """`resolvedOutcome` and `side` are BOTH index-derived, so this comparison
    means the same thing on a Yes/No, an Up/Down, a total and a named-team
    market. It is also the reason the settled badge can say "settled won"
    without the label text ever entering the arithmetic."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.40, current=10.0, outcome=label,
              cur_price=1.0)],
        [_sum(BTC_UP, outcomes=outcomes, resolved="yes")])
    p = r["positions"][0]
    assert p["side"] == "yes"
    assert p["sideLabel"] == label
    assert p["resolvedOutcome"] == "yes"
    assert p["resolvedOutcomeLabel"] == outcomes[0]
    assert p["markCents"] == 100.0, "a decided winner is worth exactly $1"
    assert p["settledPnlUsd"] == pytest.approx(6.0)


def test_holding_the_losing_outcome_reads_as_a_loss_not_as_missing_data(
        monkeypatch, fresh_db):
    r = _portfolio(
        monkeypatch,
        [_pos(DOGE_DOWN, idx=1, size=29.9987, avg=0.16, outcome="Down")],
        [_sum(DOGE_DOWN, outcomes=["Up", "Down"], resolved="yes")])
    p = r["positions"][0]
    assert p["side"] == "no" and p["sideLabel"] == "Down"
    assert p["resolvedOutcome"] == "yes" and p["resolvedOutcomeLabel"] == "Up"
    assert p["markCents"] == 0.0
    assert p["settledPnlUsd"] == pytest.approx(-4.80, abs=0.01)


def test_an_unread_outcome_stays_unknown_rather_than_becoming_a_loss(
        monkeypatch, fresh_db):
    """`resolvedOutcome == side` is false for BOTH a loss and an outcome we
    could not read, and only one of those is a claim we are entitled to make."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.4, current=4.0, cur_price=0.4)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved=None)])
    p = r["positions"][0]
    assert p["settled"] is False
    assert p["resolvedOutcome"] is None
    assert p["markCents"] == 40.0, "still a mark, not a settlement"



def test_a_worthless_resolved_position_is_not_advertised_as_redeemable(
        monkeypatch, fresh_db):
    """Straight from the wallet that reported this: 105 expired losers, every
    single one flagged `redeemable: true` by Polymarket with `currentValue: 0`,
    and the page told the user all 105 were "ready to redeem — claim them to
    turn them back into cash". Total claimable: $0.00."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299, current=0.0,
              redeemable=True)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    assert r["positions"][0]["redeemable"] is False
    assert r["redeemableCount"] == 0
    assert r["settledWorthlessCount"] == 1
    note = r["redeemableNote"] or ""
    assert "$0.00" in note and "nothing to claim" in note


def test_a_won_unredeemed_position_really_is_redeemable(monkeypatch, fresh_db):
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.4, current=10.0, redeemable=True,
              cur_price=1.0)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="yes")])
    assert r["positions"][0]["redeemable"] is True
    assert r["redeemableCount"] == 1
    assert r["redeemableValueUsd"] == pytest.approx(10.0)
    assert "ready to redeem" in (r["redeemableNote"] or "")


def test_a_redeemable_flag_with_no_readable_value_is_unknown_not_a_claim(
        monkeypatch, fresh_db):
    """We cannot see what it is worth, so we say neither "claim this" nor "this
    is worthless" — the badge stays off and the row carries the reason."""
    p = _pos(BTC_UP, idx=0, size=10, avg=0.4, redeemable=True)
    p["cur_price"] = None
    p["_raw"]["currentValue"] = None
    r = _portfolio(monkeypatch, [p],
                   [{"conditionId": BTC_UP, "title": "M", "slug": None,
                     "status": "closed", "resolvedOutcome": None,
                     "closeTime": None, "outcomes": ["Up", "Down"]}])
    row = r["positions"][0]
    assert row["redeemable"] is False
    assert r["redeemableCount"] == 0
    assert r["settledWorthlessCount"] == 0, "unknown is not the same as worthless"
    assert any(u["field"] == "redeemable" for u in row["unknowns"])



def test_our_settled_pnl_agrees_with_the_venues_cash_pnl(monkeypatch, fresh_db):
    """The five real positions this bug was diagnosed on, one from each outcome
    vocabulary. On the live wallet the same arithmetic put our settled P&L at
    -$633.79 against Polymarket's -$633.96 across 105 positions — the gap is
    entry fees, which their `avgPrice` excludes and our ledger price does too."""
    rows = [
        (BTC_UP, 0, 33.3333, 0.0299, "Up", ["Up", "Down"]),
        (DOGE_DOWN, 1, 29.9987, 0.16, "Down", ["Up", "Down"]),
        ("0x362a", 0, 6.0, 0.4599, "Over", ["Over", "Under"]),
        ("0x180c", 1, 7.0, 0.6099, "Naomi Osaka",
         ["Karolina Muchova", "Naomi Osaka"]),
        (GIANTS, 1, 5.0, 0.61, "San Francisco Giants",
         ["Atlanta Braves", "San Francisco Giants"]),
    ]
    positions = [_pos(cid, idx=i, size=sz, avg=avg, outcome=lab)
                 for cid, i, sz, avg, lab, _ in rows]
    summaries = [_sum(cid, outcomes=outs, resolved="no" if i == 0 else "yes")
                 for cid, i, _sz, _avg, _lab, outs in rows]
    r = _portfolio(monkeypatch, positions, summaries)

    assert all(p["markCents"] == 0.0 for p in r["positions"])
    assert r["settledPnlUsd"] == pytest.approx(r["venueCashPnlUsd"], abs=0.01)
    assert r["settledPnlUsd"] == pytest.approx(-15.88, abs=0.01)
    assert r["redeemableCount"] == 0 and r["settledWorthlessCount"] == 5


def test_every_position_carries_the_venues_own_pnl_for_comparison(
        monkeypatch, fresh_db):
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["venueCashPnlUsd"] == pytest.approx(-1.0, abs=0.01)
    assert p["settledPnlUsd"] == pytest.approx(p["venueCashPnlUsd"], abs=0.01)



def test_a_position_no_longer_claims_an_origin(monkeypatch, fresh_db):
    """Every fill on this wallet was stamped 'external' — the engines write no
    terminal_orders rows — so the Positions and Fill-ledger tables labelled all
    1,502 of the user's own trades "Placed elsewhere". The column is gone; the
    DB column stays, because who placed an order is still worth recording."""
    _seed([{"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10,
            "price": 40.0, "origin": "external"}])
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.4)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    assert "origin" not in r["positions"][0]
    assert tt.fills()["fills"][0]["origin"] == "external"


def test_the_fill_ledger_counts_every_origin_towards_the_basis(fresh_db):
    """One market, bought by the crypto engine and by hand. Average cost is
    across BOTH, because both spent the same balance."""
    _seed([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10,
         "price": 40.0, "origin": "external"},
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10,
         "price": 60.0, "origin": "manual"},
    ])
    st = tt._ledger_basis()[(BTC_UP, "yes")]
    assert st["size"] == 20
    assert st["cost"] == pytest.approx(10.0)
    assert "origin" not in st, "origin must not reach the basis calculation"
