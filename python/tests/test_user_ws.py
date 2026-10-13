"""CLOB user feed: only OUR orders' events wake the engine; nothing else.
The connection loop runs against an in-process fake websocket — never a real
socket — and every wait is on an event, not a short sleep."""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

import user_ws
import ws_ssl


def _feed():
    f = user_ws.UserFeed()
    f.wake = asyncio.Event()
    f._api_key = "KEY"
    return f


def test_order_events_are_tracked_and_wake():
    f = _feed()
    assert f.handle({"event_type": "order", "id": "O1", "type": "UPDATE", "status": "MATCHED",
                     "size_matched": "5", "original_size": "10", "price": "0.52"})
    assert f.wake.is_set()
    assert f.order("O1")["sizeMatched"] == 5.0 and f.order("O1")["status"] == "MATCHED"


def test_trade_events_wake_only_for_our_orders():
    f = _feed()
    theirs = {"event_type": "trade", "id": "T1", "taker_order_id": "X", "trade_owner": "OTHER",
              "maker_orders": [{"order_id": "Y", "owner": "OTHER"}]}
    assert f.handle(theirs) is False and not f.wake.is_set()
    ours_as_maker = {"event_type": "trade", "id": "T2", "taker_order_id": "X", "trade_owner": "OTHER",
                     "status": "MATCHED", "size": "3", "price": "0.4",
                     "maker_orders": [{"order_id": "M1", "owner": "KEY", "matched_amount": "3"}]}
    assert f.handle(ours_as_maker) is True and f.wake.is_set()
    assert f.trades[-1]["orders"] == ["M1"]


def test_junk_and_pongs_are_ignored():
    f = _feed()
    for junk in ("PONG", 5, None, {"event_type": "price_change"}, [{"foo": 1}]):
        assert f.handle(junk) is False
    assert f.events == 0 and not f.wake.is_set()


_ORDER = json.dumps({"event_type": "order", "id": "O1", "type": "PLACEMENT", "status": "LIVE",
                     "size_matched": "0", "original_size": "10", "price": "0.5"})


class _FakeSocket:
    """One connection. `script` is what `recv` hands out in turn — a frame,
    or an exception raised there (the connection dropping) — after which it
    waits until the loop is cancelled. An exception instead of a list fails
    the connection as it opens."""

    def __init__(self, script):
        self.script = script
        self.sent: list[str] = []

    async def __aenter__(self):
        if isinstance(self.script, BaseException):
            raise self.script
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        await asyncio.Event().wait()


class _FakeServer:
    def __init__(self, monkeypatch, *scripts, creds=None):
        self.scripts = list(scripts)
        self.socks: list[_FakeSocket] = []
        self.kwargs: list[dict] = []
        keys = list(creds or [{"apiKey": "KEY", "secret": "SEC", "passphrase": "PP"}])
        monkeypatch.setattr(user_ws, "_creds", lambda: keys.pop(0) if len(keys) > 1 else keys[0])
        monkeypatch.setattr(ws_ssl, "client_context", lambda: "CTX")
        monkeypatch.setattr(websockets, "connect", self._connect)
        monkeypatch.setattr(user_ws, "_RECONNECT_MAX", 0)

    def _connect(self, url, **kw):
        assert url == user_ws.WS_URL
        self.kwargs.append(kw)
        sock = _FakeSocket(self.scripts.pop(0))
        self.socks.append(sock)
        return sock


def _drive(feed: user_ws.UserFeed, check):
    """Start the feed, wait (up to 5s) for it to handle one of our events,
    run `check`, then stop it."""
    async def go():
        feed.wake = asyncio.Event()
        feed.start()
        try:
            await asyncio.wait_for(feed.wake.wait(), timeout=5)
            check()
        finally:
            await feed.stop()
        assert not feed.connected and not feed.is_running()
    asyncio.run(go())


def test_run_authenticates_first_then_pings_and_handles_our_events(monkeypatch):
    server = _FakeServer(monkeypatch, ["PONG", "not json", _ORDER])
    f = user_ws.UserFeed()

    def check():
        sock = server.socks[0]
        assert json.loads(sock.sent[0]) == {
            "auth": {"apiKey": "KEY", "secret": "SEC", "passphrase": "PP"}, "type": "user"}
        assert sock.sent[1:] == ["PING"]
        assert server.kwargs[0]["ssl"] == "CTX" and server.kwargs[0]["ping_interval"] is None
        assert f.connected and f.order("O1")["status"] == "LIVE"
        assert f.events == 1
    _drive(f, check)


@pytest.mark.parametrize("failure", [
    [ConnectionResetError("connection dropped")],
    OSError("connect refused"),
])
def test_run_reconnects_and_resubscribes_after_a_drop(monkeypatch, failure):
    server = _FakeServer(monkeypatch, failure, [_ORDER], creds=[
        {"apiKey": "OLD", "secret": "S1", "passphrase": "P1"},
        {"apiKey": "NEW", "secret": "S2", "passphrase": "P2"}])
    f = user_ws.UserFeed()

    def check():
        assert len(server.socks) == 2
        assert json.loads(server.socks[1].sent[0])["auth"]["apiKey"] == "NEW"
        assert f._api_key == "NEW"
        if isinstance(failure, list):
            assert json.loads(server.socks[0].sent[0])["auth"]["apiKey"] == "OLD"
        assert f.connected and f.order("O1") is not None
        assert "connect refused" in f.last_error or "dropped" in f.last_error
    _drive(f, check)
