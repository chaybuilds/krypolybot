"""rtds_ws reconnect bookkeeping, driven through `_run` against a scripted
fake socket — never a real one (on Windows/3.11 a real socket in a test has
wedged asyncio.run's shutdown).

PolyBolt falls back to legacy RTDS after `_PB_FAILS_BEFORE_FALLBACK`
consecutive failures. The counter used to reset only when a session returned
cleanly, and a session ends by raising, so two drops hours apart — each after
a healthy authed stream — counted as "consecutive" and moved the feed to the
deprecated fallback. Routine server closes counted too.
"""
from __future__ import annotations

import asyncio
import json

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

import rtds_ws
import ws_ssl

_CREDS = {"apiKey": "k", "secret": "s", "passphrase": "p"}


def _closed(code: int) -> Exception:
    if code in (1000, 1001):
        return ConnectionClosedOK(Close(code, ""), Close(code, ""), True)
    return ConnectionClosedError(Close(code, ""), None)


class _FakeWS:
    """Replays `script` from recv(); each item is a message (dict → JSON) or
    an exception to raise."""

    def __init__(self, script):
        self.script = list(script)
        self.sent: list = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        await asyncio.sleep(0)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item) if isinstance(item, dict) else item

    async def close(self):
        pass


class _Connect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


def _drive(monkeypatch, client, sessions, *, creds=_CREDS):
    """Run `client._run` over the scripted sessions (one per connect) and stop
    after the last. Returns ([(url, _pb_fails) per connect], [backoff per
    sleep]) — the failure count as each attempt starts."""
    urls: list[tuple[str, int]] = []
    sleeps: list[float] = []
    queue = list(sessions)

    def _connect(url, **kw):
        urls.append((url, client._pb_fails))
        if not queue:
            client._stop = True
            raise RuntimeError("script done")
        return _Connect(_FakeWS(queue.pop(0)))

    real_sleep = asyncio.sleep

    async def _sleep(secs, *a, **k):
        if secs:
            sleeps.append(secs)
        await real_sleep(0)

    monkeypatch.setattr(rtds_ws.websockets, "connect", _connect)
    monkeypatch.setattr(rtds_ws.asyncio, "sleep", _sleep)
    monkeypatch.setattr(rtds_ws, "_creds", lambda: creds)
    monkeypatch.setattr(ws_ssl, "client_context", lambda: None)
    asyncio.run(asyncio.wait_for(client._run(), 5.0))
    return urls, sleeps


_AUTHED = {"op": "authed"}
_POINT = {"v": 1, "channel": "price.crypto.twap", "payload": {
    "symbol": "btcusd", "value": 100.0, "timestamp": 1_791_000_000_000}}


def test_a_session_that_subscribed_clears_earlier_polybolt_failures(monkeypatch):
    c = rtds_ws._Client()
    c._pb_fails = 1
    attempts, _ = _drive(monkeypatch, c, [[_AUTHED, _POINT, _closed(1006)]])
    assert attempts == [(rtds_ws._POLYBOLT_URL, 1), (rtds_ws._POLYBOLT_URL, 1)]


def test_routine_server_closes_are_not_failures(monkeypatch):
    c = rtds_ws._Client()
    c._pb_fails = 1
    attempts, sleeps = _drive(monkeypatch, c, [
        [_AUTHED, _POINT, _closed(1000)],
        [_AUTHED, _POINT, _closed(4003)],
    ])
    assert attempts == [(rtds_ws._POLYBOLT_URL, 1), (rtds_ws._POLYBOLT_URL, 0),
                        (rtds_ws._POLYBOLT_URL, 0)]
    assert sleeps[:2] == [rtds_ws._ROUTINE_RECONNECT_SEC] * 2


def test_a_close_before_the_subscription_still_counts(monkeypatch):
    c = rtds_ws._Client()
    attempts, _ = _drive(monkeypatch, c, [[_closed(4003)], [_closed(4003)]])
    assert attempts == [(rtds_ws._POLYBOLT_URL, 0), (rtds_ws._POLYBOLT_URL, 1),
                        (rtds_ws._URL, 2)]


def test_a_failing_rtds_fallback_sends_the_feed_back_to_polybolt(monkeypatch):
    c = rtds_ws._Client()
    c._pb_fails = rtds_ws._PB_FAILS_BEFORE_FALLBACK
    attempts, _ = _drive(monkeypatch, c, [[OSError("server rejected WebSocket connection")]])
    assert attempts == [(rtds_ws._URL, 2), (rtds_ws._POLYBOLT_URL, 0)]
