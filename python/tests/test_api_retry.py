"""Retry-idempotency guard for the authenticated CLOB request layer.

A POST /order is a non-idempotent mutation: if the response is lost to a timeout
or 5xx, the order may already have reached the CLOB. Blindly re-firing it would
risk a DOUBLE FILL, so place_limit_order calls `_authed_request(..., idempotent=
False)` and that path must NOT retry on an ambiguous failure. Reads stay retried.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import polymarket_api as pa


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _stub_api(monkeypatch):
    async def _anoop(*a, **k):
        return None
    monkeypatch.setattr(pa, "ensure_api_creds", _anoop)
    monkeypatch.setattr(pa, "_note_net_fail", _anoop)
    monkeypatch.setattr(pa, "_note_net_ok", lambda *a, **k: None)
    monkeypatch.setattr(pa.auth, "l2_headers", lambda *a, **k: {})
    monkeypatch.setattr(pa.asyncio, "sleep", _anoop)


class _TimeoutClient:
    def __init__(self):
        self.calls = 0

    async def request(self, *a, **k):
        self.calls += 1
        raise httpx.TimeoutException("simulated network timeout")


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self._payload = {"errorMsg": "server error"}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class _StatusClient:
    def __init__(self, status):
        self.calls = 0
        self.status = status

    async def request(self, *a, **k):
        self.calls += 1
        return _Resp(self.status)


def _use_client(monkeypatch, client):
    async def _gc():
        return client
    monkeypatch.setattr(pa, "_get_client", _gc)


def test_post_order_not_retried_on_timeout(monkeypatch):
    client = _TimeoutClient()
    _use_client(monkeypatch, client)
    with pytest.raises(httpx.TimeoutException):
        _run(pa._authed_request("POST", "/order", json_body={"x": 1}, idempotent=False))
    assert client.calls == 1


def test_post_order_not_retried_on_5xx(monkeypatch):
    client = _StatusClient(500)
    _use_client(monkeypatch, client)
    with pytest.raises(pa.PolymarketAPIError):
        _run(pa._authed_request("POST", "/order", json_body={"x": 1}, idempotent=False))
    assert client.calls == 1


def test_idempotent_get_still_retries_on_timeout(monkeypatch):
    client = _TimeoutClient()
    _use_client(monkeypatch, client)
    with pytest.raises(httpx.TimeoutException):
        _run(pa._authed_request("GET", "/markets"))
    assert client.calls == pa.MAX_RETRIES


def test_idempotent_get_still_retries_on_5xx(monkeypatch):
    client = _StatusClient(503)
    _use_client(monkeypatch, client)
    with pytest.raises(pa.PolymarketAPIError):
        _run(pa._authed_request("GET", "/markets"))
    assert client.calls == pa.MAX_RETRIES



class _RResp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


def _client_returning(*responses):
    calls = []

    class _C:
        async def request(self, *a, **k):
            calls.append(1)
            return responses[min(len(calls), len(responses)) - 1]
    return _C(), calls


def _patch_client(monkeypatch, client):
    async def _get():
        return client
    monkeypatch.setattr(pa, "_get_client", _get)
    async def _creds():
        return None
    monkeypatch.setattr(pa, "ensure_api_creds", _creds)
    monkeypatch.setattr(pa.auth, "l2_headers", lambda *a, **k: {})
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(pa.asyncio, "sleep", _no_sleep)


def test_restart_425_retries_a_read_then_succeeds(monkeypatch):
    monkeypatch.setattr(pa, "_restart_until", 0.0)
    client, calls = _client_returning(_RResp(425, {"error": "restarting"}, {"retry-after": "2"}),
                                      _RResp(200, {"ok": True}))
    _patch_client(monkeypatch, client)
    assert _run(pa._authed_request("GET", "/data/orders")) == {"ok": True}
    assert len(calls) == 2


def test_restart_425_on_an_order_raises_and_flags_the_window(monkeypatch):
    monkeypatch.setattr(pa, "_restart_until", 0.0)
    client, calls = _client_returning(_RResp(425, {"error": "restarting"}, {"retry-after": "20"}))
    _patch_client(monkeypatch, client)
    with pytest.raises(pa.EngineRestartError):
        _run(pa._authed_request("POST", "/order", json_body={"x": 1}, idempotent=False))
    assert len(calls) == 1
    r = pa.trading_restriction()
    assert r and r["kind"] == "restarting"
    monkeypatch.setattr(pa, "_restart_until", 0.0)


def test_post_only_mode_503_is_recognised(monkeypatch):
    monkeypatch.setattr(pa, "_restart_until", 0.0)
    monkeypatch.setattr(pa, "_post_only_until", 0.0)
    client, _calls = _client_returning(_RResp(503, {"code": "post_only_mode", "error": "post only"}))
    _patch_client(monkeypatch, client)
    with pytest.raises(pa.PostOnlyModeError):
        _run(pa._authed_request("POST", "/order", json_body={"x": 1}, idempotent=False))
    r = pa.trading_restriction()
    assert r and r["kind"] == "post_only"
    monkeypatch.setattr(pa, "_post_only_until", 0.0)
    assert pa.trading_restriction() is None


def test_cancel_orders_batches_and_reports_refusals(monkeypatch):
    sent = []

    async def fake(method, path, *, json_body=None, **_k):
        sent.append((method, path, list(json_body)))
        if "BAD" in json_body:
            raise pa.PolymarketAPIError(400, "bad batch")
        return {"canceled": [i for i in json_body if i != "GONE"],
                "not_canceled": {"GONE": "order not found"} if "GONE" in json_body else {}}
    monkeypatch.setattr(pa, "_authed_request", fake)
    monkeypatch.setattr(pa, "_CANCEL_BATCH", 2)
    res = _run(pa.cancel_orders(["A", "B", "GONE", "A", ""]))
    assert [p for _m, p, _b in sent] == ["/orders", "/orders"]
    assert res["canceled"] == {"A", "B"}
    assert res["not_canceled"] == {"GONE": "order not found"}
    res2 = _run(pa.cancel_orders(["BAD"]))
    assert res2["canceled"] == set() and "bad batch" in res2["not_canceled"]["BAD"]
