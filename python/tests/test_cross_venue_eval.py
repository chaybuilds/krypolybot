from __future__ import annotations

import asyncio
import gzip
import json
import pathlib

import pytest

import cross_venue as cv
import kalshi
import terminal as t

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

MIN_RECALL = 0.80
MIN_PRECISION = 1.00

_LISTING_DATE = "2026-05-13T21:23:15.981Z"


@pytest.fixture(scope="module")
def truth() -> dict:
    with open(FIXTURES / "cross_venue_truth.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def snapshot() -> dict:
    with gzip.open(FIXTURES / "cross_venue_kalshi_snapshot.json.gz",
                   "rt", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def frozen_venues(monkeypatch, snapshot):
    rows = [dict(r, settlementSources=[], contractUrl=None)
            for r in snapshot["series"]]

    async def series_index():
        return rows, True

    async def events_in_series(series_ticker: str, limit: int = 200):
        out = []
        for ev in snapshot["events"].get(str(series_ticker).upper(), []):
            for raw in ev.get("markets") or []:
                if raw.get("mve_collection_ticker"):
                    continue
                row = kalshi.market_row(raw, "2026-09-02T00:00:00Z", ev)
                if row:
                    row["seriesTicker"] = (row["seriesTicker"]
                                           or str(series_ticker).upper())
                    out.append(row)
        return out

    async def candles(*a, **k):
        return []

    async def price_history(*a, **k):
        return {"points": []}

    monkeypatch.setattr(kalshi, "series_index", series_index)
    monkeypatch.setattr(kalshi, "events_in_series", events_in_series)
    monkeypatch.setattr(kalshi, "candles", candles)
    monkeypatch.setattr(t, "price_history", price_history)
    monkeypatch.setattr(t, "is_offline", lambda: False)
    monkeypatch.setattr(cv, "_index", None)
    yield
    monkeypatch.setattr(cv, "_index", None)


def _pm(case: dict) -> dict:
    return {
        "conditionId": case["conditionId"],
        "title": case["title"],
        "slug": case["slug"],
        "eventSlug": case["eventSlug"],
        "closeTime": case["closeTime"],
        "category": case["category"],
        "probability": 50.0,
        "bid": 49.0, "ask": 51.0, "spread": 2.0,
        "volume24hUsd": None, "liquidityUsd": None,
        "_raw": {"startDate": _LISTING_DATE, "slug": case["slug"]},
    }


def run_compare(case: dict, monkeypatch) -> dict:
    async def market(cid, **kw):
        return _pm(case)

    monkeypatch.setattr(t, "market", market)
    return asyncio.run(cv.compare(case["conditionId"], "1d"))


def quoted_tickers(snapshot: dict) -> set[str]:
    out = set()
    for events in snapshot["events"].values():
        for ev in events:
            for raw in ev.get("markets") or []:
                row = kalshi.market_row(raw, "2026-09-02T00:00:00Z", ev)
                if row and row["probability"] is not None:
                    out.add(row["conditionId"])
    return out


@pytest.fixture(scope="function")
def measured(truth, snapshot, monkeypatch) -> dict:
    quoted = quoted_tickers(snapshot)
    tp = fp = fn = tn = 0
    detail = []
    for c in truth["cases"]:
        out = run_compare(c, monkeypatch)
        found = (out.get("kalshi") or {}).get("ticker")
        want = c["expectedKalshiTicker"]
        scorable = (want is None) or (want in quoted)
        if want and found == want:
            why = "found"
        elif want and found:
            why = "wrong-candidate-preferred"
        elif want and not found:
            why = "not-found" if scorable else "counterpart-unquoted"
        elif found:
            why = "fictional-match"
        else:
            why = "correctly-absent"
        if scorable:
            if why == "found":
                tp += 1
            elif why in ("wrong-candidate-preferred", "fictional-match"):
                fp += 1
            elif why == "not-found":
                fn += 1
            else:
                tn += 1
        elif why == "wrong-candidate-preferred":
            fp += 1
        detail.append({"case": c, "result": out, "outcome": why,
                       "found": found, "scorable": scorable})
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "detail": detail,
        "recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "precision": tp / (tp + fp) if (tp + fp) else 1.0,
    }


def test_the_fixture_is_a_real_and_balanced_sample(truth):
    cases = truth["cases"]
    assert len(cases) >= 60
    have = [c for c in cases if c["expectedKalshiTicker"]]
    assert 20 <= len(have) <= len(cases) - 20
    assert all(c["why"] for c in cases), "every label states its reasoning"


def test_recall_clears_the_bar(measured):
    assert measured["recall"] >= MIN_RECALL, (
        f"recall fell to {measured['recall']:.3f}; misses: "
        + "; ".join(d["case"]["title"][:40] for d in measured["detail"]
                    if d["outcome"] in ("not-found", "wrong-candidate-preferred")))


def test_precision_never_slips(measured):
    bad = [d for d in measured["detail"]
           if d["outcome"] in ("fictional-match", "wrong-candidate-preferred")]
    assert measured["precision"] >= MIN_PRECISION, (
        "precision dropped: "
        + "; ".join(f"{d['case']['title'][:40]} -> {d['found']}" for d in bad))


def test_nothing_is_invented_for_a_question_kalshi_does_not_list(measured):
    invented = [d for d in measured["detail"]
                if d["outcome"] == "fictional-match"]
    assert not invented, "; ".join(
        f"{d['case']['title'][:50]} -> {d['found']}" for d in invented)


def test_the_listing_date_is_never_read_as_a_settlement_date(measured):
    fed = [d for d in measured["detail"]
           if "Fed increase interest rates by 25 bps" in d["case"]["title"]]
    assert fed, "the Fed regression case is missing from the fixture"
    for d in fed:
        assert d["found"] == d["case"]["expectedKalshiTicker"], (
            f"the September 2026 Fed question found {d['found']!r} — the "
            f"listing date is being read as a settlement date again")


def test_every_reported_match_carries_its_reasoning(measured):
    for d in measured["detail"]:
        m = d["result"].get("match")
        if not m:
            continue
        assert m["for"], f"{d['case']['title']}: a match with no reasons for it"
        assert d["result"]["kalshi"]["ticker"]
        assert m["comparedSides"]["polymarket"] == "yes"
        assert m["comparedSides"]["kalshi"] in ("yes", "no")


def test_a_match_against_the_no_side_says_so(measured):
    for d in measured["detail"]:
        m = d["result"].get("match")
        if m and m["comparedSides"]["kalshi"] == "no":
            assert any("NO side" in a for a in m["against"]), (
                f"{d['case']['title']}: compared the NO side without saying so")
            assert "NO on" in (d["result"]["kalshi"]["subtitle"] or "")


def test_a_spread_only_ever_prints_under_a_strong_match(measured):
    for d in measured["detail"]:
        m = d["result"].get("match")
        if m and m["confidence"] != "strong":
            assert d["result"]["spreadCents"] is None, (
                f"{d['case']['title']}: a {m['confidence']} match printed a "
                f"spread")


def test_a_family_we_understand_gets_a_note_that_says_what_is_different(measured):
    checked = 0
    for d in measured["detail"]:
        want = d["case"].get("expectedNoteContains")
        if not want:
            continue
        checked += 1
        note = d["result"].get("note") or ""
        assert d["result"]["kalshi"] is None, (
            f"{d['case']['title']}: expected no match")
        assert want in note, f"{d['case']['title']}: note was {note!r}"
        assert "no counterpart — this is a real answer" not in note, (
            f"{d['case']['title']}: fell back to the generic note")
    assert checked >= 4, "the timed-crypto note cases are missing"
