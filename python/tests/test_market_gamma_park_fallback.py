"""market() keeps answering from the last good summary while Gamma is parked."""
import asyncio

import terminal as term


def _summary(cid):
    return {"conditionId": cid, "title": "T", "yesTokenId": "1", "noTokenId": "2",
            "status": "open", "tickSize": 0.01, "minOrderSize": 5.0, "_raw": {}}


def test_parked_gamma_falls_back_to_last_good(monkeypatch):
    term._last_market.clear()
    calls = {"n": 0}

    async def ok(ids):
        calls["n"] += 1
        return [_summary(ids[0])]

    async def parked(ids):
        return []

    async def no_fetch(*a, **k):
        return None, None

    monkeypatch.setattr(term, "subscribe", lambda *a, **k: None)
    monkeypatch.setattr(term, "_overlay_live", lambda s: None)
    monkeypatch.setattr(term, "_fetch", no_fetch)
    monkeypatch.setattr(term, "_summaries_for", ok)
    first = asyncio.run(term.market("0xabc", include_raw=True))
    assert first and "metaStaleSec" not in first

    monkeypatch.setattr(term, "_summaries_for", parked)
    again = asyncio.run(term.market("0xabc", include_raw=True))
    assert again and again["conditionId"] == "0xabc" and again["metaStaleSec"] >= 0


def test_no_fallback_for_unknown_or_expired(monkeypatch):
    term._last_market.clear()

    async def parked(ids):
        return []

    async def no_fetch(*a, **k):
        return None, None

    monkeypatch.setattr(term, "_summaries_for", parked)
    monkeypatch.setattr(term, "_fetch", no_fetch)
    assert asyncio.run(term.market("0xnever")) is None

    term._last_market["0xold"] = (term.time.monotonic() - term._LAST_MARKET_TTL - 1, _summary("0xold"))
    assert asyncio.run(term.market("0xold")) is None
