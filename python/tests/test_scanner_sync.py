from __future__ import annotations

import asyncio
import time

import pytest

import db
import polymarket_api as api
import scanner
import service


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "sync.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _slow_gamma(pages: int, per_page_sec: float, page_cap: int = 100):
    total = pages * page_cap
    calls: list[int] = []

    async def _get(_url, params=None, **_kw):
        params = params or {}
        offset = int(params.get("offset") or 0)
        calls.append(offset)
        await asyncio.sleep(per_page_sec)
        n = max(0, min(page_cap, total - offset))
        return [{
            "conditionId": f"0x{offset + i:064x}",
            "question": f"market {offset + i}",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.5","0.5"]',
            "clobTokenIds": f'["{offset + i}a","{offset + i}b"]',
            "slug": f"market-{offset + i}",
            "volume": "1000",
            "volume24hr": "500",
        } for i in range(n)]

    return _get, calls


def test_five_pages_of_20ms_are_pipelined_not_walked(monkeypatch):
    get, calls = _slow_gamma(pages=5, per_page_sec=0.020)
    monkeypatch.setattr(api, "_get", get)

    t0 = time.perf_counter()
    rows = asyncio.run(api.fetch_all_open_markets(max_pages=10))
    elapsed = time.perf_counter() - t0

    assert len(rows) == 500
    assert elapsed < 0.150, f"pages are not pipelined: {elapsed * 1000:.0f}ms"
    assert len(calls) >= 5


def test_sync_markets_writes_every_page(monkeypatch, fresh_db):
    get, _calls = _slow_gamma(pages=5, per_page_sec=0.0)
    monkeypatch.setattr(api, "_get", get)

    assert asyncio.run(scanner.sync_markets(max_pages=10)) == 500
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 500


def test_upserts_run_off_the_event_loop(monkeypatch, fresh_db):
    import threading

    get, _calls = _slow_gamma(pages=2, per_page_sec=0.0)
    monkeypatch.setattr(api, "_get", get)

    loop_thread: dict = {}

    real_write = scanner._write_markets_blocking

    def _spy(raw, now_iso):
        loop_thread["writer"] = threading.current_thread().ident
        return real_write(raw, now_iso)

    monkeypatch.setattr(scanner, "_write_markets_blocking", _spy)

    async def _run():
        loop_thread["loop"] = threading.current_thread().ident
        return await scanner.sync_markets(max_pages=5)

    assert asyncio.run(_run()) == 200
    assert loop_thread["writer"] != loop_thread["loop"], (
        "market upserts still ran on the event loop thread"
    )


def test_overlapping_sync_requests_run_one_sync(monkeypatch):
    started = {"n": 0}
    release = None

    async def _slow_sync(*, max_pages: int = 10) -> int:
        started["n"] += 1
        await release.wait()
        return 7

    monkeypatch.setattr(service.scanner, "sync_markets", _slow_sync)

    async def _run():
        nonlocal release
        release = asyncio.Event()
        service._market_sync_task = None

        assert service._start_market_sync() is True
        await asyncio.sleep(0)
        assert service._start_market_sync() is False
        assert service._start_market_sync() is False

        release.set()
        await service._market_sync_task
        release = asyncio.Event()
        release.set()
        assert service._start_market_sync() is True
        await service._market_sync_task

    asyncio.run(_run())
    assert started["n"] == 2, f"expected 2 syncs (not 4), got {started['n']}"


def test_background_sync_failure_never_reaches_the_loop(monkeypatch):
    async def _boom(*, max_pages: int = 10) -> int:
        raise RuntimeError("gamma is down")

    monkeypatch.setattr(service.scanner, "sync_markets", _boom)

    async def _run():
        service._market_sync_task = None
        assert service._start_market_sync() is True
        await service._market_sync_task
        assert service._market_sync_task.exception() is None

    asyncio.run(_run())


def test_a_slow_market_sync_does_not_hold_the_master_loop(monkeypatch):
    import contextlib

    sync_started = asyncio.Event()
    reached_later_block = {"n": 0}

    calls = {"n": 0}

    async def _glacial_sync(*, max_pages: int = 10) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 0
        sync_started.set()
        await asyncio.sleep(30)
        return 0

    async def _emit(_name, _data=None):
        return None

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(service, "emit_event", _emit)
    monkeypatch.setattr(service.scanner, "sync_markets", _glacial_sync)
    monkeypatch.setattr(service.scanner, "sync_events", _noop)
    monkeypatch.setattr(service.STATE, "auth_ok", False, raising=False)
    monkeypatch.setattr(service.STATE, "paused", False, raising=False)
    monkeypatch.setattr(service.STATE, "cfg", {
        "main_record_signals": False,
        "enable_trading": False,
        "market_refresh_interval": 0,
    }, raising=False)

    real_block = service._block

    @contextlib.asynccontextmanager
    async def _counting_block(name, ceiling_sec=service._BLOCK_CEILING_SEC):
        async with real_block(name, ceiling_sec):
            yield
        if name == "whaleScan":
            reached_later_block["n"] += 1

    monkeypatch.setattr(service, "_block", _counting_block)

    async def _run():
        service._market_sync_task = None
        service._loop_stop = asyncio.Event()
        task = asyncio.create_task(service._scanner_and_trader_loop())
        await asyncio.sleep(0.3)
        still_pending = bool(
            service._market_sync_task and not service._market_sync_task.done()
        )
        service._loop_stop.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if service._market_sync_task:
            service._market_sync_task.cancel()
            with contextlib.suppress(BaseException):
                await service._market_sync_task
        service._market_sync_task = None
        return still_pending

    still_pending = asyncio.run(_run())

    assert sync_started.is_set(), "the recurring sync never started"
    assert still_pending, "the sync finished; the test's stub was not slow"
    assert reached_later_block["n"] >= 1, (
        "the loop never got past the market sync — it is still awaited inline"
    )
