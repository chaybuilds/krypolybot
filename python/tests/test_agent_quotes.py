"""Agent and paper marks read the live CLOB book, not Gamma.

Gamma's bid/ask/outcomePrices are an indexer's copy that sits 20-67c off the
live book near a close. The agent's forecast baseline, record_forecast's
edges, paper marks and the agents' daily loss stop all priced off it. They now
read the book (our own feed when fresh, else one batched read for every
market), and keep Gamma only as a fallback that says where it came from and
how old it is.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import db
import forecast_ledger
import mcp_rails
import terminal
import terminal_paper as tp
from test_mcp import (_BUY, _agent_buy, _call, _fake_service, _forecast, _live,
                      _mock_book, _run, _text, fresh)  # noqa: F401 — fixture

_GAMMA_AT = "2026-10-11T15:00:00Z"


def _gamma_row(cid, *, bid=60.0, ask=62.0, token=True):
    src = {"source": "gamma", "observedAt": _GAMMA_AT, "ageSec": 9.0}
    return {"conditionId": cid, "title": "T", "status": "open", "resolvedOutcome": None,
            "closeTime": "2030-01-01T00:00:00Z",
            "yesTokenId": f"tok-{cid}" if token else None,
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "spread": ask - bid,
            "probability": (bid + ask) / 2,
            "sources": {k: dict(src) for k in ("bid", "ask", "mid", "spread", "probability")},
            "unknowns": []}


def _book(bid=None, ask=None):
    bids = [] if bid is None else [{"price": f"{(bid - 3) / 100:.2f}", "size": "100"},
                                   {"price": f"{bid / 100:.2f}", "size": "50"}]
    asks = [] if ask is None else [{"price": f"{(ask + 3) / 100:.2f}", "size": "100"},
                                   {"price": f"{ask / 100:.2f}", "size": "40"}]
    return {"market": "0x", "bids": bids, "asks": asks, "tick_size": "0.01"}


def _fake_clob(monkeypatch, books, *, batch_ok=True, feed=None):
    """CLOB reads served from `books` {token: book}; returns the call log."""
    calls: list = []

    async def _fetch(host, path, params=None, *, ttl=0.0, timeout=10.0, json_body=None):
        calls.append((host, path, params, json_body))
        if host != "clob":
            return None, None
        if path == "/books" and batch_ok and isinstance(json_body, list):
            return ([dict(books[t["token_id"]], asset_id=t["token_id"])
                     for t in json_body if t["token_id"] in books], terminal._now_iso())
        if path == "/book" and params and params.get("token_id") in books:
            return dict(books[params["token_id"]], asset_id=params["token_id"]), terminal._now_iso()
        return None, None
    monkeypatch.setattr(terminal, "_fetch", _fetch)
    monkeypatch.setattr(terminal, "_live_quote", lambda tok: (feed or {}).get(tok))
    return calls



def test_marks_come_from_one_batched_book_read(monkeypatch):
    rows = [_gamma_row(c) for c in ("0xa", "0xb", "0xc")]
    calls = _fake_clob(monkeypatch, {f"tok-{c}": _book(20, 22) for c in ("0xa", "0xb", "0xc")})
    asyncio.run(terminal.clob_quotes(rows))
    assert [c[1] for c in calls] == ["/books"], "N markets cost N round trips"
    assert [t["token_id"] for t in calls[0][3]] == ["tok-0xa", "tok-0xb", "tok-0xc"]
    for r in rows:
        assert (r["bid"], r["ask"], r["mid"], r["probability"]) == (20.0, 22.0, 21.0, 21.0)
        assert r["sources"]["bid"]["source"] == "clob"
        assert r["sources"]["probability"]["source"] == "clob"


def test_a_fresh_feed_quote_costs_no_request(monkeypatch):
    rows = [_gamma_row("0xa"), _gamma_row("0xb")]
    calls = _fake_clob(monkeypatch, {"tok-0xb": _book(30, 33)},
                       feed={"tok-0xa": {"bid_cents": 40.0, "ask_cents": 41.0}})
    asyncio.run(terminal.clob_quotes(rows))
    assert [t["token_id"] for t in calls[0][3]] == ["tok-0xb"]
    assert (rows[0]["bid"], rows[0]["ask"]) == (40.0, 41.0)
    assert rows[0]["sources"]["ask"]["source"] == "clob-ws"
    assert (rows[1]["bid"], rows[1]["ask"]) == (30.0, 33.0)


def test_an_empty_side_on_the_book_is_unknown_not_gamma(monkeypatch):
    rows = [_gamma_row("0xa")]
    _fake_clob(monkeypatch, {"tok-0xa": _book(bid=20)})
    asyncio.run(terminal.clob_quotes(rows))
    r = rows[0]
    assert r["bid"] == 20.0 and r["ask"] is None and r["mid"] is None
    assert any(u["field"] == "ask" for u in r["unknowns"])
    assert r["probability"] == 61.0 and r["sources"]["probability"]["source"] == "gamma"


def test_a_failed_batch_read_falls_back_to_each_book(monkeypatch):
    rows = [_gamma_row("0xa"), _gamma_row("0xb")]
    calls = _fake_clob(monkeypatch, {"tok-0xa": _book(20, 22), "tok-0xb": _book(70, 72)},
                       batch_ok=False)
    asyncio.run(terminal.clob_quotes(rows))
    assert [c[1] for c in calls] == ["/books", "/book", "/book"]
    assert rows[0]["bid"] == 20.0 and rows[1]["ask"] == 72.0


def test_an_unreadable_market_keeps_gamma_labelled_with_its_age(monkeypatch):
    rows = [_gamma_row("0xa"), _gamma_row("0xnotok", token=False)]
    _fake_clob(monkeypatch, {}, batch_ok=False)
    asyncio.run(terminal.clob_quotes(rows))
    for r in rows:
        assert r["bid"] == 60.0
        assert r["sources"]["bid"]["source"] == "gamma"
        assert r["sources"]["bid"]["ageSec"] is not None



def _summaries_at_gamma(monkeypatch, **kw):
    async def _summaries(ids):
        return [_gamma_row(c, **kw) for c in ids]
    monkeypatch.setattr(terminal, "_summaries_for", _summaries)


def test_the_agents_live_loss_stop_marks_at_the_books_bid(fresh, monkeypatch):
    _live(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=10, filled_shares=10, cid="0xdown")
    _summaries_at_gamma(monkeypatch)
    _fake_clob(monkeypatch, {"tok-0xdown": _book(20, 22)})
    d = _run(mcp_rails.day_loss("live", {}))
    assert d["lossUsd"] == 3.0, d


def test_paper_marks_and_the_paper_loss_stop_use_the_book(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    res = _call("place_order", {**_BUY, "mode": "paper", "orderType": "FAK",
                                "forecastId": _forecast()})
    assert not res["isError"], _text(res)
    _summaries_at_gamma(monkeypatch, bid=69.0, ask=71.0)
    _fake_clob(monkeypatch, {"tok-0xabc": _book(20, 22)})
    with tp.using_book("ai"):
        pf = _run(tp.portfolio({}))
        day = _run(tp.day_pnl(datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    pos = pf["positions"][0]
    assert pos["markCents"] == 21.0
    assert pos["sources"]["probability"]["source"] == "clob"
    assert day["openLossUsd"] == 3.0


def test_a_forecast_is_benchmarked_against_the_live_book(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)

    async def _market(cid, include_raw=False):
        return _gamma_row(cid)
    monkeypatch.setattr(terminal, "market", _market)
    _fake_clob(monkeypatch, {"tok-0xabc": _book(20, 22)})
    res = _call("record_forecast", {"conditionId": "0xabc", "fairValueCents": 40,
                                    "rationale": "The rules resolve on the official tally."})
    out = json.loads(_text(res))
    assert out["market"]["mid"] == 21.0 and out["market"]["source"] == "clob"
    assert out["buyYes"]["atPriceCents"] == 22.0
    row = forecast_ledger.get(out["forecastId"])
    assert row["market_mid_cents"] == 21.0 and row["yes_ask_cents"] == 22.0


def test_a_gamma_fallback_quote_tells_the_agent_its_age(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)

    async def _market(cid, include_raw=False):
        return _gamma_row(cid)
    monkeypatch.setattr(terminal, "market", _market)
    _fake_clob(monkeypatch, {}, batch_ok=False)
    res = _call("record_forecast", {"conditionId": "0xabc", "fairValueCents": 40,
                                    "rationale": "The rules resolve on the official tally."})
    out = json.loads(_text(res))
    assert out["market"]["source"] == "gamma"
    assert out["market"]["ageSec"] is not None
    assert "Gamma" in out["market"]["note"]
