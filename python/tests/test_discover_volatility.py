from __future__ import annotations

import asyncio

import pytest

import terminal as t


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _gamma(**over):
    row = {
        "conditionId": over.pop("cid", "0x01"),
        "question": over.pop("q", "Will something happen?"),
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.40", "0.60"]',
        "clobTokenIds": '["1", "2"]',
        "active": True, "closed": False,
    }
    row.update(over)
    return row


def test_price_change_is_converted_to_cents():
    s = t.summary_from_gamma(_gamma(oneDayPriceChange=0.5645, oneHourPriceChange=-0.25))
    assert s["priceChange24h"] == pytest.approx(56.45)
    assert s["priceChange1h"] == pytest.approx(-25.0)


def test_price_change_keeps_its_sign():
    s = t.summary_from_gamma(_gamma(oneDayPriceChange=-0.63))
    assert s["priceChange24h"] == pytest.approx(-63.0)


def test_missing_change_is_none_not_zero():
    s = t.summary_from_gamma(_gamma())
    assert s["priceChange24h"] is None
    assert s["priceChange1h"] is None


def test_missing_change_says_why():
    s = t.summary_from_gamma(_gamma())
    reasons = {u["field"]: u["reason"] for u in s["unknowns"]}
    assert "priceChange24h" in reasons
    assert "not flat" in reasons["priceChange24h"].lower()


def test_change_carries_provenance():
    s = t.summary_from_gamma(_gamma(oneDayPriceChange=0.1))
    assert s["sources"]["priceChange24h"]["source"] == "gamma"


def test_zero_change_is_a_real_measurement():
    s = t.summary_from_gamma(_gamma(oneDayPriceChange=0.0))
    assert s["priceChange24h"] == 0.0
    assert not any(u["field"] == "priceChange24h" for u in s["unknowns"])


def _discover_volatility(monkeypatch, rows):
    async def fake(_params):
        return [t.summary_from_gamma(r) for r in rows], "2026-08-30T00:00:00Z"
    monkeypatch.setattr(t, "_gamma_markets", fake)
    monkeypatch.setattr(t, "_overlay_live", lambda _r: None)
    return _run(t.discover("volatility", limit=50))


def test_ranked_by_absolute_move(monkeypatch):
    out = _discover_volatility(monkeypatch, [
        _gamma(cid="0xa", q="small riser", oneDayPriceChange=0.05),
        _gamma(cid="0xb", q="big faller", oneDayPriceChange=-0.40),
        _gamma(cid="0xc", q="big riser", oneDayPriceChange=0.42),
    ])
    assert [r["title"] for r in out["rows"]] == ["big riser", "big faller", "small riser"]


def test_unknown_move_sorts_last_but_is_kept(monkeypatch):
    out = _discover_volatility(monkeypatch, [
        _gamma(cid="0xa", q="unknown mover"),
        _gamma(cid="0xb", q="calm", oneDayPriceChange=0.01),
    ])
    titles = [r["title"] for r in out["rows"]]
    assert titles == ["calm", "unknown mover"], "an unknown move was ranked as calm"
    assert len(out["rows"]) == 2, "an unrankable row was dropped instead of listed"


def test_note_counts_the_unrankable(monkeypatch):
    out = _discover_volatility(monkeypatch, [
        _gamma(cid="0xa", oneDayPriceChange=0.3),
        _gamma(cid="0xb"),
        _gamma(cid="0xc"),
    ])
    assert "2 market(s) here report no 24h change" in out["note"]


def test_no_movers_at_all_blames_the_data_not_the_board(monkeypatch):
    out = _discover_volatility(monkeypatch, [_gamma(cid="0xa"), _gamma(cid="0xb")])
    assert "gap in Gamma's data" in out["note"]
    assert "not a calm board" in out["note"]


def test_zero_movers_rank_below_real_movers(monkeypatch):
    out = _discover_volatility(monkeypatch, [
        _gamma(cid="0xa", q="unknown"),
        _gamma(cid="0xb", q="flat", oneDayPriceChange=0.0),
        _gamma(cid="0xc", q="mover", oneDayPriceChange=0.2),
    ])
    assert [r["title"] for r in out["rows"]] == ["mover", "flat", "unknown"]


def test_resolving_column_is_gone():
    out = _run(t.discover("resolving", limit=5))
    assert out["rows"] == []
    assert "Unknown column" in out["note"]


def test_volatility_is_a_known_column(monkeypatch):
    out = _discover_volatility(monkeypatch, [_gamma(oneDayPriceChange=0.1)])
    assert out["column"] == "volatility"
    assert "Unknown column" not in (out["note"] or "")
