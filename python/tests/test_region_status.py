"""Polymarket's region check (GET polymarket.com/api/geoblock), and what the
wallet Test makes of it.

A restricted region used to look completely healthy: market data flows, the
balance reads, Test went green, and every order bounced with a 403 after it
was signed. The live probe only ran for some wallets, and its region refusal
read as "inconclusive". HTTP is stubbed throughout.
"""
from __future__ import annotations

import asyncio

import pytest

import polymarket_api as api


@pytest.fixture(autouse=True)
def _fresh_region(monkeypatch):
    """Each test starts with no cached verdict, and leaves none behind."""
    monkeypatch.setattr(api, "_region_state", {"value": None, "at": 0.0, "task": None})


def _geoblock(monkeypatch, reply):
    """Answer the geoblock URL with `reply` (an exception is raised); count calls."""
    calls: list = []

    async def _get(url, *_a, **_k):
        if url == api.GEOBLOCK_URL:
            calls.append(url)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return [{"id": "1"}]

    monkeypatch.setattr(api, "_get", _get)
    return calls


def test_an_unrestricted_place_is_allowed(monkeypatch):
    _geoblock(monkeypatch, {"blocked": False, "ip": "1.2.3.4", "country": "CA", "region": "NS"})
    r = asyncio.run(api.region_status())
    assert r["status"] == "allowed"
    assert r["country"] == "CA" and r["region"] == "CA-NS"
    assert r["checkedAt"] > 0 and r["message"]
    assert "ip" not in r


@pytest.mark.parametrize("reply", [
    {"blocked": True, "country": "IR", "region": ""},
    {"blocked": True, "country": "UA", "region": "43"},
    {"blocked": True, "country": "UA", "region": "UA-14"},
    {"blocked": True, "country": "KP", "region": ""},
    {"blocked": True, "country": "ZZ", "region": ""},
])
def test_a_blocked_place_is_blocked(monkeypatch, reply):
    _geoblock(monkeypatch, reply)
    r = asyncio.run(api.region_status())
    assert r["status"] == "blocked"
    assert r["message"] == api.REGION_BLOCKED_MSG
    assert "vpn" not in r["message"].lower()


@pytest.mark.parametrize("reply", [
    {"blocked": True, "country": "CA", "region": "QC"},
    {"blocked": True, "country": "CA", "region": "Quebec"},
    {"blocked": True, "country": "US", "region": "NY"},
    {"blocked": True, "country": "GB", "region": "ENG"},
    {"blocked": False, "country": "FR", "region": "IDF"},
])
def test_a_close_only_place_is_close_only(monkeypatch, reply):
    _geoblock(monkeypatch, reply)
    r = asyncio.run(api.region_status())
    assert r["status"] == "close_only"
    assert r["message"] == api.REGION_CLOSE_ONLY_MSG
    assert "vpn" not in r["message"].lower()


def test_a_website_only_close_only_country_can_trade_through_the_api(monkeypatch):
    _geoblock(monkeypatch, {"blocked": False, "country": "JP", "region": "13"})
    assert asyncio.run(api.region_status())["status"] == "allowed"


@pytest.mark.parametrize("reply", [None, [], "<html>", {"country": "CA"}, RuntimeError("boom")])
def test_a_failed_or_unreadable_check_is_unknown_not_a_crash(monkeypatch, reply):
    _geoblock(monkeypatch, reply)
    r = asyncio.run(api.region_status())
    assert r["status"] == "unknown"
    assert r["message"] == api.REGION_UNKNOWN_MSG


def test_a_verdict_is_reused_for_ten_minutes(monkeypatch):
    calls = _geoblock(monkeypatch, {"blocked": False, "country": "CA", "region": "NS"})
    t = [1_000_000.0]
    monkeypatch.setattr(api.time, "time", lambda: t[0])
    asyncio.run(api.region_status())
    t[0] += api.REGION_TTL_SEC - 1
    asyncio.run(api.region_status())
    assert len(calls) == 1
    t[0] += 2
    asyncio.run(api.region_status())
    assert len(calls) == 2
    asyncio.run(api.region_status(force=True))
    assert len(calls) == 3


def test_an_unknown_verdict_is_retried_sooner(monkeypatch):
    calls = _geoblock(monkeypatch, None)
    t = [1_000_000.0]
    monkeypatch.setattr(api.time, "time", lambda: t[0])
    asyncio.run(api.region_status())
    t[0] += api.REGION_UNKNOWN_TTL_SEC + 1
    asyncio.run(api.region_status())
    assert len(calls) == 2


def test_the_cached_accessors_never_touch_the_network(monkeypatch):
    calls = _geoblock(monkeypatch, {"blocked": True, "country": "US", "region": "NY"})
    assert api.region_cached() is None
    assert api.region_order_refusal("buy") is None
    asyncio.run(api.region_status())
    c = api.region_cached()
    assert c["status"] == "close_only" and c["ageSec"] >= 0
    assert api.region_order_refusal("buy") == api.REGION_CLOSE_ONLY_MSG
    assert api.region_order_refusal("BUY") == api.REGION_CLOSE_ONLY_MSG
    assert api.region_order_refusal("sell") is None
    assert len(calls) == 1


def test_a_blocked_region_refuses_sells_too(monkeypatch):
    _geoblock(monkeypatch, {"blocked": True, "country": "CU", "region": ""})
    asyncio.run(api.region_status())
    assert api.region_order_refusal("buy") == api.REGION_BLOCKED_MSG
    assert api.region_order_refusal("sell") == api.REGION_BLOCKED_MSG


def test_unknown_refuses_nothing(monkeypatch):
    _geoblock(monkeypatch, None)
    asyncio.run(api.region_status())
    assert api.region_order_refusal("buy") is None


def test_the_snapshot_schedules_one_background_recheck(monkeypatch):
    calls = _geoblock(monkeypatch, {"blocked": True, "country": "CA", "region": "QC"})

    async def go():
        assert api.region_snapshot(refresh=True) is None
        api.region_snapshot(refresh=True)
        await api._region_state["task"]
        return api.region_snapshot(refresh=True)

    snap = asyncio.run(go())
    assert snap["status"] == "close_only"
    assert len(calls) == 1


def test_the_snapshot_without_refresh_never_schedules(monkeypatch):
    calls = _geoblock(monkeypatch, {"blocked": False, "country": "CA", "region": "NS"})

    async def go():
        return api.region_snapshot(refresh=False)

    assert asyncio.run(go()) is None
    assert calls == [] and api._region_state["task"] is None


def _probe(monkeypatch, error):
    monkeypatch.setattr(api.auth, "get_signature_type", lambda: api.auth.SIGNATURE_TYPE_POLY_1271)
    monkeypatch.setattr(api.auth, "get_address", lambda: "0x" + "cd" * 20)
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0x" + "ef" * 20)
    monkeypatch.setattr(api, "_signer_check_cache", {})

    async def _markets(**_k):
        return [{"ticker": "t", "yes_token": "1", "status": "open", "yes_bid_dollars": 0.5}], None

    async def _meta(_t):
        return {"min_size": 5}

    async def _place(**_k):
        raise error

    monkeypatch.setattr(api, "fetch_markets", _markets)
    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "place_limit_order", _place)
    return asyncio.run(api.verify_order_signer_live())


@pytest.mark.parametrize("error", [
    api.PolymarketAPIError(403, {"error": "Trading restricted in your region, please refer to available regions"}),
    api.PolymarketAPIError(403, "Forbidden: geoblock"),
])
def test_a_region_refusal_is_a_hard_verdict_not_inconclusive(monkeypatch, error):
    r = _probe(monkeypatch, error)
    assert r["ok"] is False and r["tested"] is True and r["regionRefused"] is True
    assert r["reason"] == api.REGION_REFUSED_MSG
    assert api._signer_check_cache == {}


def test_another_rejection_is_still_inconclusive(monkeypatch):
    r = _probe(monkeypatch, api.PolymarketAPIError(400, {"error": "not enough balance"}))
    assert r == {"ok": True, "tested": False, "reason": "inconclusive (CLOB 400)"}


MAX = str(2 ** 256 - 1)


def _ready(monkeypatch, *, geoblock, sig_type=api.auth.SIGNATURE_TYPE_POLY_PROXY, balance=25.0,
           probe=None, allowances=None):
    calls = _geoblock(monkeypatch, geoblock)
    funder = "0x" + "ef" * 20
    probes: list = []

    async def _none(*_a, **_k):
        return None

    async def _authz():
        return {"tested": False}

    async def _bal():
        return {"balance": int(round(balance * 100))}

    async def _probe_fn(*_a, **_k):
        probes.append(1)
        return probe or {"tested": False}

    async def _authed(*_a, **_k):
        if allowances is not None:
            return {"allowances": allowances}
        return {"allowances": {api.auth.EXCHANGE_ADDRESS: MAX, api.auth.NEG_RISK_EXCHANGE_ADDRESS: MAX,
                               api.auth.NEG_RISK_ADAPTER_ADDRESS: MAX}}

    monkeypatch.setattr(api, "ensure_api_creds", _none)
    monkeypatch.setattr(api.auth, "trading_address", lambda: funder)
    monkeypatch.setattr(api.auth, "get_address", lambda: "0x" + "cd" * 20)
    monkeypatch.setattr(api.auth, "get_funder", lambda: funder)
    monkeypatch.setattr(api.auth, "get_signature_type", lambda: sig_type)
    monkeypatch.setattr(api, "detect_wallet_signature_type", _none)
    monkeypatch.setattr(api, "check_deposit_wallet_authorization", _authz)
    monkeypatch.setattr(api, "get_balance", _bal)
    monkeypatch.setattr(api, "verify_order_signer_live", _probe_fn)
    monkeypatch.setattr(api, "_authed_request", _authed)

    async def _no_usdc(*_a, **_k):
        return 0.0

    monkeypatch.setattr(api, "_erc20_balance_usd", _no_usdc)
    return asyncio.run(api.check_trading_ready()), calls, probes


ALL_TYPES = [api.auth.SIGNATURE_TYPE_POLY_PROXY, api.auth.SIGNATURE_TYPE_POLY_GNOSIS_SAFE,
             api.auth.SIGNATURE_TYPE_POLY_1271]


@pytest.mark.parametrize("sig_type", ALL_TYPES)
@pytest.mark.parametrize("balance", [25.0, 1.2])
def test_every_wallet_type_and_balance_gets_the_region_verdict(monkeypatch, sig_type, balance):
    res, calls, _ = _ready(monkeypatch, geoblock={"blocked": True, "country": "CA", "region": "QC"},
                           sig_type=sig_type, balance=balance)
    assert calls == [api.GEOBLOCK_URL]
    assert res["region"]["status"] == "close_only"
    assert res["ok"] is False
    assert api.REGION_CLOSE_ONLY_MSG in res["issues"]
    assert api.REGION_CLOSE_ONLY_MSG in res["fatalIssues"]


def test_a_blocked_region_is_a_fatal_issue(monkeypatch):
    res, _, probes = _ready(monkeypatch, geoblock={"blocked": True, "country": "SY", "region": ""},
                            sig_type=api.auth.SIGNATURE_TYPE_POLY_1271)
    assert res["ok"] is False
    assert res["fatalIssues"] == [api.REGION_BLOCKED_MSG]
    assert probes == []


def test_an_allowed_region_adds_nothing(monkeypatch):
    res, _, _ = _ready(monkeypatch, geoblock={"blocked": False, "country": "CA", "region": "NS"})
    assert res["ok"] is True, res["issues"]
    assert res["region"]["status"] == "allowed"
    assert res["fatalIssues"] == []


def test_an_unknown_region_is_reported_but_does_not_fail_the_wallet(monkeypatch):
    res, _, _ = _ready(monkeypatch, geoblock=RuntimeError("offline"))
    assert res["region"]["status"] == "unknown"
    assert res["ok"] is True, res["issues"]


def test_a_region_refused_probe_is_fatal_and_not_a_signer_verdict(monkeypatch):
    res, _, probes = _ready(
        monkeypatch, geoblock=None, sig_type=api.auth.SIGNATURE_TYPE_POLY_1271,
        probe={"ok": False, "tested": True, "regionRefused": True, "reason": api.REGION_REFUSED_MSG})
    assert probes == [1]
    assert res["ok"] is False
    assert res["signerOk"] is None
    assert api.REGION_REFUSED_MSG in res["fatalIssues"]
    assert res["region"].get("refusedOrder") is True


def test_untradeable_wallets_are_fatal_cautions_are_not(monkeypatch):
    res, _, _ = _ready(monkeypatch, geoblock={"blocked": False, "country": "CA", "region": "NS"},
                       allowances={api.auth.EXCHANGE_ADDRESS: "0", api.auth.NEG_RISK_EXCHANGE_ADDRESS: "0",
                                   api.auth.NEG_RISK_ADAPTER_ADDRESS: "0"})
    assert any("allowance is 0" in i for i in res["fatalIssues"]), res
    res, _, _ = _ready(monkeypatch, geoblock={"blocked": False, "country": "CA", "region": "NS"},
                       balance=0.0)
    assert any("holds no pUSD" in i for i in res["fatalIssues"]), res
    res, _, _ = _ready(monkeypatch, geoblock={"blocked": False, "country": "CA", "region": "NS"},
                       balance=3.0)
    assert res["issues"] and res["fatalIssues"] == []


def test_test_credentials_returns_the_region_and_fatal_issues(monkeypatch):
    import service

    verdict = {"status": "close_only", "country": "CA", "region": "CA-QC",
               "checkedAt": 1.0, "message": api.REGION_CLOSE_ONLY_MSG}

    async def _ready():
        return {"ok": False, "address": "0x" + "ef" * 20, "balanceUsd": 12.0,
                "approvalsOk": True, "signerOk": None,
                "issues": [api.REGION_CLOSE_ONLY_MSG], "fatalIssues": [api.REGION_CLOSE_ONLY_MSG],
                "region": verdict}

    async def _emit(*_a, **_k):
        return None

    monkeypatch.setattr(service.polymarket_auth, "credentials_present", lambda *_a: True)
    monkeypatch.setattr(service.polymarket_auth, "reset_credential_cache", lambda: None)
    monkeypatch.setattr(service.polymarket_auth, "prime_credentials", lambda **_k: None)
    monkeypatch.setattr(service.polymarket_api, "check_trading_ready", _ready)
    monkeypatch.setattr(service, "emit_event", _emit)
    monkeypatch.setattr(service.STATE, "auth_ok", service.STATE.auth_ok)
    out = asyncio.run(service._h_testCredentials({}))
    assert out["region"] == verdict
    assert out["fatalIssues"] == [api.REGION_CLOSE_ONLY_MSG]
    assert out["ready"] is False
