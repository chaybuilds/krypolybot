"""Trading readiness reads the deposit wallet's real approvals.

It used to report approvals OK for any deposit wallet holding a balance,
without reading an allowance at all. The CLOB's /balance-allowance reply
carries an `allowances` map keyed by spender; what matters is the V2
exchanges the app actually signs orders for. HTTP is stubbed throughout.
"""
from __future__ import annotations

import asyncio

import pytest

import polymarket_api as api

EX = api.auth.EXCHANGE_ADDRESS
NR = api.auth.NEG_RISK_EXCHANGE_ADDRESS
ADAPTER = api.auth.NEG_RISK_ADAPTER_ADDRESS
V1_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
MAX = str(2 ** 256 - 1)


def _ready(monkeypatch, *, sig_type, allowance_reply, balance=25.0):
    """Run check_trading_ready with every leg stubbed; the CLOB answers
    /balance-allowance with `allowance_reply` (an exception is raised)."""
    seen: list = []
    funder = "" if sig_type == api.auth.SIGNATURE_TYPE_EOA else "0x" + "ef" * 20

    async def _none(*_a, **_k):
        return None

    async def _authz():
        return {"tested": False}

    async def _bal():
        return {"balance": int(round(balance * 100))}

    async def _probe(*_a, **_k):
        return {"tested": False}

    async def _get(*_a, **_k):
        return [{"id": "1"}]

    async def _authed(method, path, *, params=None, **_kw):
        seen.append((method, path, dict(params or {})))
        if isinstance(allowance_reply, Exception):
            raise allowance_reply
        return allowance_reply

    monkeypatch.setattr(api, "ensure_api_creds", _none)
    monkeypatch.setattr(api.auth, "trading_address", lambda: funder or "0x" + "ab" * 20)
    monkeypatch.setattr(api.auth, "get_address", lambda: "0x" + "cd" * 20)
    monkeypatch.setattr(api.auth, "get_funder", lambda: funder)
    monkeypatch.setattr(api.auth, "get_signature_type", lambda: sig_type)
    monkeypatch.setattr(api, "detect_wallet_signature_type", _none)
    monkeypatch.setattr(api, "check_deposit_wallet_authorization", _authz)
    monkeypatch.setattr(api, "get_balance", _bal)
    monkeypatch.setattr(api, "verify_order_signer_live", _probe)
    monkeypatch.setattr(api, "_get", _get)
    monkeypatch.setattr(api, "_authed_request", _authed)
    return asyncio.run(api.check_trading_ready()), seen


DEPOSIT_MODES = [api.auth.SIGNATURE_TYPE_POLY_PROXY, api.auth.SIGNATURE_TYPE_POLY_GNOSIS_SAFE,
                 api.auth.SIGNATURE_TYPE_POLY_1271]


@pytest.mark.parametrize("sig_type", DEPOSIT_MODES)
def test_a_deposit_wallet_reads_its_allowance_for_its_own_signature_type(monkeypatch, sig_type):
    res, seen = _ready(monkeypatch, sig_type=sig_type, allowance_reply={
        "balance": "25000000", "allowances": {EX: MAX, NR: MAX, ADAPTER: MAX}})
    assert ("GET", "/balance-allowance",
            {"asset_type": "COLLATERAL", "signature_type": str(sig_type)}) in seen
    assert res["approvalsOk"] is True
    assert not any("allowance" in i for i in res["issues"]), res["issues"]


@pytest.mark.parametrize("sig_type", DEPOSIT_MODES)
def test_a_funded_deposit_wallet_without_v2_approval_is_not_ok(monkeypatch, sig_type):
    res, _ = _ready(monkeypatch, sig_type=sig_type, allowance_reply={
        "balance": "25000000", "allowances": {EX.lower(): "0", NR: MAX}})
    assert res["approvalsOk"] is False
    assert any("allowance" in i and "exchange" in i for i in res["issues"]), res["issues"]
    assert res["ok"] is False


def test_only_a_v1_approval_is_not_a_v2_approval(monkeypatch):
    res, _ = _ready(monkeypatch, sig_type=api.auth.SIGNATURE_TYPE_POLY_1271, allowance_reply={
        "balance": "25000000", "allowances": {V1_EXCHANGE: MAX}})
    assert res["approvalsOk"] is None
    assert not any("allowance" in i for i in res["issues"])


def test_an_unreadable_allowance_is_unknown_not_ok(monkeypatch):
    res, _ = _ready(monkeypatch, sig_type=api.auth.SIGNATURE_TYPE_POLY_1271,
                    allowance_reply=api.PolymarketAPIError(500, "boom"))
    assert res["approvalsOk"] is None


def test_a_raw_wallet_reads_the_v2_exchanges_too(monkeypatch):
    res, seen = _ready(monkeypatch, sig_type=api.auth.SIGNATURE_TYPE_EOA, allowance_reply={
        "balance": "25000000", "allowances": {EX: "0", NR: "0", V1_EXCHANGE: MAX}})
    assert ("GET", "/balance-allowance",
            {"asset_type": "COLLATERAL", "signature_type": "0"}) in seen
    assert res["approvalsOk"] is False
