from __future__ import annotations

import asyncio
import contextlib

import pytest

import service


class _FakeConn:

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _drive_one_scan(monkeypatch, *, source, rows):
    events: list[tuple[str, object]] = []

    async def _emit(name, data=None):
        events.append((name, data))
    monkeypatch.setattr(service, "emit_event", _emit)

    monkeypatch.setattr(service.STATE, "auth_ok", False, raising=False)
    monkeypatch.setattr(service.STATE, "paused", False, raising=False)
    monkeypatch.setattr(service.STATE, "cfg", {
        "main_record_signals": True,
        "enable_trading": False,
        "whale_scan_interval": 0,
        "momentum_scan_interval": 0,
    }, raising=False)

    async def _no_markets(*a, **k):
        return 0
    monkeypatch.setattr(service.scanner, "sync_markets", _no_markets)
    monkeypatch.setattr(service.scanner, "sync_events", _no_markets)

    async def _scan_whales(cfg):
        return (len(rows), rows) if source == "whale" else (0, [])

    async def _scan_momentum(cfg):
        return (len(rows), rows) if source == "momentum" else (0, [])
    monkeypatch.setattr(service.scanner, "scan_whales", _scan_whales)
    monkeypatch.setattr(service.scanner, "scan_momentum", _scan_momentum)

    monkeypatch.setattr(service.db, "get_db", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(service.db, "already_traded_signal_ids",
                        lambda *a, **k: set())

    async def _run():
        service._loop_stop = asyncio.Event()
        task = asyncio.create_task(service._scanner_and_trader_loop())
        await asyncio.sleep(0.3)
        service._loop_stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    asyncio.run(_run())
    return events


def _rows(n, source):
    return [
        {
            "id": i,
            "source": source,
            "ticker": f"0x{i:040x}",
            "title": f"Market {i}",
            "side": "yes",
            "confidence": 70,
            "price_cents": 55,
            "created_at": "2026-01-01T00:00:00Z",
        }
        for i in range(1, n + 1)
    ]


@pytest.mark.parametrize("source", ["whale", "momentum"])
def test_a_scan_emits_exactly_one_signal_batch(monkeypatch, source):
    rows = _rows(12, source)
    events = _drive_one_scan(monkeypatch, source=source, rows=rows)

    batches = [d for (n, d) in events if n == "signal:batch"]
    assert len(batches) == 1, f"expected 1 signal:batch, got {len(batches)}"
    assert [n for (n, _) in events].count("signal:new") == 0, \
        "per-row signal:new events are what this change removed"

    payload = batches[0]
    assert isinstance(payload, dict) and isinstance(payload["signals"], list)
    assert len(payload["signals"]) == 12
    assert [s["id"] for s in payload["signals"]] == [r["id"] for r in rows]
    assert {s["source"] for s in payload["signals"]} == {source}


def test_an_empty_scan_emits_nothing(monkeypatch):
    events = _drive_one_scan(monkeypatch, source="whale", rows=[])
    assert [n for (n, _) in events if n.startswith("signal:")] == []


class _RecordingStream:
    def __init__(self, line_buffering):
        self.line_buffering = line_buffering
        self.written: list[str] = []
        self.flushes = 0

    def write(self, s):
        self.written.append(s)
        return len(s)

    def flush(self):
        self.flushes += 1


def test_send_skips_the_redundant_flush_on_a_line_buffered_stream(monkeypatch):
    out = _RecordingStream(line_buffering=True)
    monkeypatch.setattr(service.sys, "stdout", out)
    asyncio.run(service._send({"type": "event", "name": "x"}))
    assert out.written and out.written[0].endswith("\n")
    assert out.flushes == 0


def test_send_still_flushes_a_block_buffered_stream(monkeypatch):
    out = _RecordingStream(line_buffering=False)
    monkeypatch.setattr(service.sys, "stdout", out)
    asyncio.run(service._send({"type": "event", "name": "x"}))
    assert out.flushes == 1


def test_send_flushes_a_stream_with_no_line_buffering_attribute(monkeypatch):
    class _Bare:
        def __init__(self):
            self.flushes = 0

        def write(self, s):
            return len(s)

        def flush(self):
            self.flushes += 1

    out = _Bare()
    monkeypatch.setattr(service.sys, "stdout", out)
    asyncio.run(service._send({"type": "event", "name": "x"}))
    assert out.flushes == 1
