from __future__ import annotations

import asyncio

import pytest

import activity_ws
import db
import polymarket_api as api
import scanner
import service


CONDITION_ID = "0x" + "ab" * 32


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "whales.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _raw_trade(*, size=10_000.0, price=0.70, tx="0xdeadbeef", asset="tok-1"):
    return {
        "proxyWallet": "0x00000000000000000000000000000000000000aa",
        "side": "BUY",
        "asset": asset,
        "conditionId": CONDITION_ID,
        "size": size,
        "price": price,
        "outcome": "Yes",
        "outcomeIndex": 0,
        "slug": "will-something-happen-2026",
        "eventSlug": "something-2026",
        "title": "Will something happen?",
        "transactionHash": tx,
        "timestamp": 1_756_000_000,
    }


@pytest.fixture
def wired(monkeypatch, fresh_db):
    events: list[tuple[str, object]] = []

    async def _emit(name, data=None):
        events.append((name, data))

    monkeypatch.setattr(service, "emit_event", _emit)
    monkeypatch.setattr(service.STATE, "cfg", {
        "main_record_signals": True,
        "enable_trading": False,
        "min_whale_usd": 2500,
        "min_entry_price_frac": 0.50,
    }, raising=False)

    async def _no_markets(_tickers):
        return {}
    monkeypatch.setattr(api, "fetch_markets_map", _no_markets)

    activity_ws.set_trade_hook(service._on_activity_trade)
    service._socket_whale_inflight.clear()
    yield events
    activity_ws.set_trade_hook(None)
    service._socket_whale_inflight.clear()


async def _feed_socket(frame) -> None:
    activity_ws.handle_message({"topic": "activity", "type": "trades",
                                "payload": frame})
    for _ in range(3):
        pending = [t for t in list(service._bg_tasks) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


def _whale_rows():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM whale_trades ORDER BY id"
        ).fetchall()]


def test_socket_and_rest_derive_the_same_trade_id():
    raw = _raw_trade()
    from_socket = api.normalize_activity_trade(raw)
    from_rest = api.normalize_trade(raw)
    assert from_socket == from_rest
    assert from_socket["trade_id"] == "0xdeadbeef-tok-1-700-10000"


def test_a_frame_without_the_id_bearing_fields_is_dropped():
    assert api.normalize_activity_trade(_raw_trade(tx="")) is None
    assert api.normalize_activity_trade(_raw_trade(asset="")) is None


def test_a_socket_whale_inserts_one_row_and_emits_one_batch(wired):
    events = wired
    asyncio.run(_feed_socket(_raw_trade()))

    rows = _whale_rows()
    assert len(rows) == 1
    assert rows[0]["ticker"] == CONDITION_ID
    assert rows[0]["taker_side"] == "yes"
    assert rows[0]["dollar_value"] == pytest.approx(7000.0)

    batches = [d for (n, d) in events if n == "signal:batch"]
    assert len(batches) == 1
    signals = batches[0]["signals"]
    assert len(signals) == 1
    assert signals[0]["source"] == "whale"
    assert signals[0]["id"] == rows[0]["id"]


def test_a_sub_threshold_socket_trade_does_nothing(wired):
    events = wired
    asyncio.run(_feed_socket(_raw_trade(size=1_000.0, price=0.70)))
    assert _whale_rows() == []
    assert [n for (n, _) in events if n == "signal:batch"] == []


def test_a_cheap_entry_is_gated_the_same_as_on_the_rest_path(wired):
    events = wired
    asyncio.run(_feed_socket(_raw_trade(size=10_000.0, price=0.40)))
    assert _whale_rows() == []
    assert [n for (n, _) in events if n == "signal:batch"] == []


@pytest.mark.parametrize("frame", [
    None,
    "not-a-dict",
    {},
    {"side": "BUY"},
    {"side": "NOPE", "size": 1e6, "price": 0.9},
    {"side": "BUY", "size": "banana", "price": None,
     "conditionId": CONDITION_ID, "asset": "a", "transactionHash": "0x1"},
])
def test_a_garbled_frame_is_ignored_and_never_raises(wired, frame):
    events = wired
    asyncio.run(_feed_socket(frame))
    assert _whale_rows() == []
    assert [n for (n, _) in events if n == "signal:batch"] == []


def test_socket_then_rest_yields_exactly_one_row(wired, monkeypatch):
    events = wired
    raw = _raw_trade()

    async def _get(_url, params=None, **_kw):
        return [raw]
    monkeypatch.setattr(api, "_get", _get)

    async def _run():
        await _feed_socket(raw)
        return await scanner.scan_whales(service.STATE.cfg)

    cnt, rows = asyncio.run(_run())
    assert (cnt, rows) == (0, []), "the REST poll re-inserted a socket whale"
    assert len(_whale_rows()) == 1
    assert len([d for (n, d) in events if n == "signal:batch"]) == 1


def test_rest_then_socket_yields_exactly_one_row(wired, monkeypatch):
    events = wired
    raw = _raw_trade()

    async def _get(_url, params=None, **_kw):
        return [raw]
    monkeypatch.setattr(api, "_get", _get)

    async def _run():
        cnt, _rows = await scanner.scan_whales(service.STATE.cfg)
        assert cnt == 1
        await _feed_socket(raw)

    asyncio.run(_run())
    assert len(_whale_rows()) == 1
    assert [d for (n, d) in events if n == "signal:batch"] == []


def test_a_repeated_frame_inserts_once(wired):
    async def _run():
        raw = _raw_trade()
        await _feed_socket(raw)
        await _feed_socket(raw)

    asyncio.run(_run())
    assert len(_whale_rows()) == 1


def test_the_socket_whale_path_respects_the_collection_gate(wired):
    events = wired
    service.STATE.cfg.update({"main_record_signals": False,
                              "enable_trading": False})
    asyncio.run(_feed_socket(_raw_trade()))
    assert _whale_rows() == []
    assert [n for (n, _) in events if n == "signal:batch"] == []


def test_whale_activity_ws_off_disables_the_push_but_not_the_poll(wired):
    events = wired
    service.STATE.cfg["whale_activity_ws"] = False
    asyncio.run(_feed_socket(_raw_trade()))
    assert _whale_rows() == []
    assert [n for (n, _) in events if n == "signal:batch"] == []


class _FakeSocket:

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.running = False
        self.watch: list = []
        self.hook = None

    def is_running(self):
        return self.running

    def start(self):
        self.started += 1
        self.running = True

    async def stop(self):
        self.stopped += 1
        self.running = False

    def set_watch(self, wallets):
        self.watch = list(wallets or [])

    def set_trade_hook(self, fn):
        self.hook = fn

    def has_hits(self):
        return False

    def drain_hits(self):
        return []


def _drive_copy_block(monkeypatch, cfg, *, copy_loop=False) -> _FakeSocket:
    import contextlib

    sock = _FakeSocket()

    async def _emit(_name, _data=None):
        return None

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(service, "emit_event", _emit)
    monkeypatch.setattr(service, "activity_ws", sock)
    monkeypatch.setattr(service.scanner, "sync_markets", _noop)
    monkeypatch.setattr(service.scanner, "sync_events", _noop)

    async def _no_whales(_cfg):
        return 0, []
    monkeypatch.setattr(service.scanner, "scan_whales", _no_whales)
    monkeypatch.setattr(service.scanner, "scan_momentum", _no_whales)
    monkeypatch.setattr(service.STATE, "auth_ok", False, raising=False)
    monkeypatch.setattr(service.STATE, "paused", False, raising=False)
    monkeypatch.setattr(service.STATE, "cfg", cfg, raising=False)

    async def _run():
        service._loop_stop = asyncio.Event()
        service._copy_wake = asyncio.Event()
        tasks = [asyncio.create_task(service._scanner_and_trader_loop())]
        if copy_loop:
            tasks.append(asyncio.create_task(service._copy_loop()))
        await asyncio.sleep(0.3)
        service._loop_stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t

    asyncio.run(_run())
    return sock


def test_the_socket_runs_for_whales_with_copy_off(monkeypatch):
    sock = _drive_copy_block(monkeypatch, {
        "main_record_signals": True,
        "copy_enabled": False,
        "copy_wallets": [],
        "whale_scan_interval": 9999,
    })
    assert sock.started == 1, "whale collection alone must bring the tape up"
    assert sock.stopped == 0
    assert sock.hook is service._on_activity_trade
    assert sock.watch == [], "copy is off — nothing to watch for wakeups"


def test_the_socket_still_runs_for_copy_with_whale_collection_off(monkeypatch):
    sock = _drive_copy_block(monkeypatch, {
        "main_record_signals": False,
        "enable_trading": False,
        "copy_enabled": True,
        "copy_wallets": ["0xabc"],
    })
    assert sock.started == 1
    assert sock.watch == ["0xabc"], "copy's wakeup watch is unchanged"


def test_the_socket_stays_down_when_neither_consumer_wants_it(monkeypatch):
    sock = _drive_copy_block(monkeypatch, {
        "main_record_signals": False,
        "enable_trading": False,
        "copy_enabled": False,
        "copy_wallets": [],
    })
    assert sock.started == 0


def test_the_frame_is_pushed_to_copy_trading_raw(wired, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(service.copy_trader, "on_activity_trade", seen.append)

    raw = _raw_trade()
    asyncio.run(_feed_socket(raw))

    assert seen == [raw]


def test_a_garbled_frame_still_reaches_copy_without_raising(wired, monkeypatch):
    def _boom(_t):
        raise RuntimeError("copy hook exploded")
    monkeypatch.setattr(service.copy_trader, "on_activity_trade", _boom)

    asyncio.run(_feed_socket(_raw_trade(tx="")))
    assert _whale_rows() == []


def test_pushed_entries_are_drained_before_the_copy_tick(monkeypatch):
    order: list[str] = []

    async def _drain(_cfg, authed=True):
        order.append("drain")
        return 0

    async def _tick(_cfg, authed=False):
        order.append("run_tick")
        return []

    monkeypatch.setattr(service.copy_trader, "drain_pushed_entries", _drain)
    monkeypatch.setattr(service.copy_trader, "run_tick", _tick)

    _drive_copy_block(monkeypatch, {
        "main_record_signals": False,
        "enable_trading": False,
        "copy_enabled": True,
        "copy_wallets": ["0xabc"],
        "copy_poll_sec": 0,
    }, copy_loop=True)

    assert order[:2] == ["drain", "run_tick"], order


def test_watched_wallet_hits_still_land_for_copy(wired):
    raw = _raw_trade()
    activity_ws.set_watch([raw["proxyWallet"]])
    try:
        asyncio.run(_feed_socket(raw))
        hits = activity_ws.drain_hits()
    finally:
        activity_ws.set_watch([])
    assert len(hits) == 1
    assert hits[0]["side"] == "BUY"
    assert hits[0]["wallet"] == raw["proxyWallet"].lower()


def test_an_unwatched_wallet_is_still_not_a_copy_hit(wired):
    activity_ws.set_watch(["0x00000000000000000000000000000000000000ff"])
    try:
        asyncio.run(_feed_socket(_raw_trade()))
        hits = activity_ws.drain_hits()
    finally:
        activity_ws.set_watch([])
    assert hits == []
    assert len(_whale_rows()) == 1, "the whale hook sees the WHOLE tape"
