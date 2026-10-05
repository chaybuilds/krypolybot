from __future__ import annotations

import asyncio
import time

import pytest

import terminal as t


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status_code = status
        self._payload = [] if payload is None else payload

    def json(self):
        return self._payload


class _Stub:

    def __init__(self, delay: float = 0.05, status=200) -> None:
        self.delay = delay
        self._status = status
        self.starts: list[float] = []
        self.count = 0
        self.inflight = 0
        self.peak = 0

    def _status_for(self, n: int) -> int:
        return self._status(n) if callable(self._status) else self._status

    async def get(self, url, params=None, timeout=None):
        n = self.count
        self.count += 1
        self.starts.append(time.perf_counter())
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.inflight -= 1
        return _Resp(self._status_for(n))


@pytest.fixture
def stub(monkeypatch):
    def _install(**kw) -> _Stub:
        s = _Stub(**kw)

        async def _get_client():
            return s

        monkeypatch.setattr(t.api, "_get_client", _get_client)
        return s

    return _install


@pytest.fixture
def gate():
    touched: dict[str, dict] = {}

    def _get(host: str, *, min_gap=None, concurrency=None) -> t._Gate:
        g = t._gates[host]
        touched.setdefault(host, {**g.__dict__, "_lat": list(g._lat)})
        g._lock = None
        g._slots = None
        g.last = 0.0
        g.parked_until = 0.0
        g.calls = g.errors = g.cache_hits = g.cache_misses = 0
        if min_gap is not None:
            g.min_gap = min_gap
        if concurrency is not None:
            g.concurrency = concurrency
        return g

    _cache_before = dict(t._cache)
    t._cache.clear()
    t._inflight.clear()
    yield _get
    for host, snap in touched.items():
        g = t._gates[host]
        g.__dict__.update(snap)
        g._lock = None
        g._slots = None
    t._cache.clear()
    t._cache.update(_cache_before)
    t._inflight.clear()


def _params(i: int) -> dict:
    return {"limit": 1, "n": i}


def test_eight_slow_requests_overlap_instead_of_queueing(gate, stub):
    g = gate("gamma", min_gap=0.01, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        started = time.perf_counter()
        await asyncio.gather(*[
            t._fetch("gamma", "/markets", _params(i), ttl=0.0) for i in range(8)
        ])
        return time.perf_counter() - started

    elapsed = _run(scenario())
    serial = 8 * (0.05 + 0.01)

    assert s.count == 8
    assert elapsed < 0.28, (
        f"8 × 50ms requests took {elapsed * 1000:.0f}ms — the gate is still "
        f"serialising the round trip (serial would be ~{serial * 1000:.0f}ms)")
    assert elapsed < serial * 0.6
    assert s.peak > 1, "no two requests were ever in flight together"


def test_in_flight_requests_are_bounded_by_the_host_concurrency(gate, stub):
    gate("gamma", min_gap=0.0, concurrency=4)
    s = stub(delay=0.02)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gamma", "/markets", _params(i), ttl=0.0) for i in range(12)
        ])

    _run(scenario())
    assert s.count == 12
    assert s.peak == 4, f"peak concurrency was {s.peak}, expected the 4-slot bound"


def test_request_starts_are_still_spaced_by_min_gap(gate, stub):
    gap = 0.02
    gate("gamma", min_gap=gap, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gamma", "/markets", _params(i), ttl=0.0) for i in range(6)
        ])

    _run(scenario())
    assert len(s.starts) == 6
    diffs = [b - a for a, b in zip(s.starts, s.starts[1:])]
    assert all(d >= gap - 0.002 for d in diffs), (
        f"starts were {[round(d * 1000, 1) for d in diffs]}ms apart, "
        f"min_gap is {gap * 1000:.0f}ms")


def test_a_429_parks_the_host_and_the_next_request_fails_fast(gate, stub):
    g = gate("gamma", min_gap=0.01, concurrency=4)
    s = stub(delay=0.01, status=429)

    async def scenario():
        data, _ = await t._fetch("gamma", "/markets", _params(0), ttl=0.0)
        assert data is None
        assert g.parked(), "a 429 did not park the host"

        started = time.perf_counter()
        again, _ = await t._fetch("gamma", "/markets", _params(1), ttl=0.0)
        return again, time.perf_counter() - started

    again, elapsed = _run(scenario())
    assert again is None
    assert elapsed < 0.02, f"the parked call took {elapsed * 1000:.1f}ms — it queued"
    assert s.count == 1, "a parked host still made a request"
    assert g.errors == 1


def test_a_parked_host_fails_fast_even_with_every_slot_busy(gate, stub):
    g = gate("gamma", min_gap=0.0, concurrency=4)
    stub(delay=0.1)

    async def scenario():
        busy = [
            asyncio.ensure_future(t._fetch("gamma", "/markets", _params(i), ttl=0.0))
            for i in range(4)
        ]
        await asyncio.sleep(0.02)
        g.park(30.0)
        started = time.perf_counter()
        data, _ = await t._fetch("gamma", "/markets", _params(99), ttl=0.0)
        elapsed = time.perf_counter() - started
        await asyncio.gather(*busy)
        return data, elapsed

    data, elapsed = _run(scenario())
    assert data is None
    assert elapsed < 0.02, f"waited {elapsed * 1000:.1f}ms for a slot to be told no"


def test_an_in_flight_response_does_not_clear_or_shorten_the_park(gate, stub):
    g = gate("gamma", min_gap=0.0, concurrency=4)
    s = stub(delay=0.02, status=lambda n: 429 if n == 0 else 200)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gamma", "/markets", _params(i), ttl=0.0) for i in range(4)
        ])

    _run(scenario())
    assert s.count == 4
    assert g.parked(), "a successful in-flight response cleared the park"
    assert g.parked_until - time.monotonic() > t._PARK_SECS - 1.0

    far = g.parked_until
    g.park(0.5)
    assert g.parked_until == far, "a second 429 pulled the park expiry back in"


def test_the_ai_hosts_are_declared_serial():
    for host in ("anthropic", "openai"):
        assert t._HOSTS[host]["concurrency"] == 1
        assert t._gates[host].concurrency == 1


def test_an_ai_host_runs_one_call_at_a_time(gate):
    import ai_analysis as ai

    g = gate("anthropic", min_gap=0.0, concurrency=1)
    state = {"inflight": 0, "peak": 0, "done": 0}

    async def one():
        async with ai._gate_call("anthropic"):
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            await asyncio.sleep(0.02)
            state["inflight"] -= 1
            state["done"] += 1

    async def scenario():
        await asyncio.gather(*[one() for _ in range(4)])

    _run(scenario())
    assert state["done"] == 4
    assert state["peak"] == 1, "two AI provider calls were in flight at once"
    assert g.calls == 4


def test_every_request_that_starts_is_counted(gate, stub):
    g = gate("gamma", min_gap=0.0, concurrency=4)
    s = stub(delay=0.01)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gamma", "/markets", _params(i), ttl=0.0) for i in range(7)
        ])

    _run(scenario())
    assert s.count == 7
    assert g.calls == 7, f"{s.count} requests were made but {g.calls} counted"
    assert g.errors == 0
    assert g.latency_ms() is not None


def test_a_transport_failure_is_still_a_call_and_an_error(gate, monkeypatch):
    g = gate("gamma", min_gap=0.0, concurrency=4)

    class _Boom:
        async def get(self, url, params=None, timeout=None):
            raise RuntimeError("socket died")

    async def _get_client():
        return _Boom()

    monkeypatch.setattr(t.api, "_get_client", _get_client)

    async def scenario():
        return await asyncio.gather(*[
            t._fetch("gamma", "/markets", _params(i), ttl=0.0) for i in range(3)
        ])

    out = _run(scenario())
    assert out == [(None, None)] * 3
    assert g.calls == 3
    assert g.errors == 3
    assert not g.parked(), "a transport failure is not a rate limit"


def test_health_reports_the_calls_the_gate_counted(gate, stub):
    g = gate("gamma", min_gap=0.0, concurrency=4)
    stub(delay=0.0)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gamma", "/markets", _params(i), ttl=0.0) for i in range(5)
        ])

    _run(scenario())
    row = next(r for r in t.health()["providers"]
               if r["host"] == t._HOSTS["gamma"]["host"])
    assert row["calls"] == 5 == g.calls
    assert row["errors"] == 0


def test_identical_concurrent_requests_are_coalesced(gate, stub):
    g = gate("gamma", min_gap=0.0, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        return await asyncio.gather(*[
            t._fetch("gamma", "/markets", {"limit": 1}, ttl=30.0) for _ in range(3)
        ])

    out = _run(scenario())
    assert s.count == 1, f"{s.count} identical concurrent requests hit the network"
    assert all(o == out[0] for o in out), "the coalesced callers got different answers"
    assert out[0][0] == []
    assert g.calls == 1
    assert g.cache_hits == 2


def test_a_cancelled_caller_does_not_strand_the_others(gate, stub):
    gate("gamma", min_gap=0.0, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        first = asyncio.ensure_future(
            t._fetch("gamma", "/markets", {"limit": 7}, ttl=30.0))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(
            t._fetch("gamma", "/markets", {"limit": 7}, ttl=30.0))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await asyncio.wait_for(second, timeout=2.0)

    data, observed = _run(scenario())
    assert data == []
    assert observed is not None
    assert s.count == 1
    assert not t._inflight, "the in-flight registry leaked an entry"


def test_the_inflight_registry_is_empty_after_a_failure(gate, monkeypatch):
    gate("gamma", min_gap=0.0, concurrency=4)

    class _Boom:
        async def get(self, url, params=None, timeout=None):
            raise RuntimeError("socket died")

    async def _get_client():
        return _Boom()

    monkeypatch.setattr(t.api, "_get_client", _get_client)
    assert _run(t._fetch("gamma", "/markets", {"limit": 3}, ttl=30.0)) == (None, None)
    assert not t._inflight


def test_offline_still_short_circuits_before_any_slot_is_taken(gate, stub):
    g = gate("gamma", min_gap=0.0, concurrency=4)
    s = stub(delay=0.0)
    t.set_offline(True)
    try:
        assert _run(t._fetch("gamma", "/markets", _params(0), ttl=0.0)) == (None, None)
    finally:
        t.set_offline(False)
    assert s.count == 0
    assert g.calls == 0
