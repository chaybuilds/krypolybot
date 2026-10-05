from __future__ import annotations

import asyncio

import terminal_trade as tt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_market_never_looked_up_is_looked_up_even_seconds_after_boot(monkeypatch):
    monkeypatch.setattr(tt.time, "monotonic", lambda: 12.0)
    tt._resolution_misses.clear()
    calls: list[list[str]] = []

    async def _summaries(ids):
        calls.append(list(ids))
        return [{"conditionId": c, "status": "settled", "resolvedOutcome": "yes",
                 "outcomes": ["Up", "Down"], "closeTime": "2026-01-01T00:00:00Z",
                 "title": "T", "slug": None, "probability": 50.0} for c in ids]
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt, "_resolutions_cached", lambda: {})
    monkeypatch.setattr(tt, "_resolutions_remember", lambda *a, **k: None, raising=False)

    out = _run(tt._resolutions_for(["0xfresh"]))
    assert calls == [["0xfresh"]], "a never-seen market was treated as a recent miss"
    assert out.get("0xfresh") == "yes"


def test_a_real_recent_miss_is_still_throttled(monkeypatch):
    monkeypatch.setattr(tt.time, "monotonic", lambda: 12.0)
    tt._resolution_misses.clear()
    tt._resolution_misses["0xmissed"] = 10.0
    calls: list[list[str]] = []

    async def _summaries(ids):
        calls.append(list(ids))
        return []
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt, "_resolutions_cached", lambda: {})

    _run(tt._resolutions_for(["0xmissed"]))
    assert calls == [], "a miss two seconds old was retried"


def test_miss_expiry_rule():
    assert tt._miss_expired(None, 5.0) is True
    assert tt._miss_expired(0.0, 5.0) is False
    assert tt._miss_expired(0.0, tt._RESOLUTION_MISS_TTL + 1.0) is True
