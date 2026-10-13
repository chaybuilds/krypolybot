"""Session-key deposit wallets must keep signing with the session envelope
across an app restart.

Regression for the 2026-10-11 audit finding. A POLY_1271 order signed by a
deposit wallet's SESSION KEY is only accepted wrapped as
abi.encode(bytes32 signer, bytes32 0, bytes sig) followed by the 0x6492… magic. The
bot learned that the key was a session signer only from the manual Test button,
and held it only in memory. Startup (`service._establish_auth`) never asked, so
after every restart each order was signed in the owner form and rejected, exits
included, until the user clicked Test again.

Every test here uses a freshly generated throwaway key and a stubbed chain: no
network, no real wallet, no order is ever sent anywhere.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from eth_abi import decode as abi_decode
from eth_account import Account

import polymarket_api as api
import polymarket_auth as auth
import service

DEP = "0xDDDD000000000000000000000000000000000009"
OTHER_DEP = "0xDDDD00000000000000000000000000000000000A"
OWNER = "0xCCCC000000000000000000000000000000000003"
MAGIC_HEX = auth.SESSION_SIGNER_MAGIC.hex()



def _import_wallet(monkeypatch, tmp_path, *, funder=DEP, sig_type=None):
    """Save a brand-new throwaway key (+ deposit wallet) into a tmp credentials
    dir, exactly as the API Keys page does. Returns its address."""
    monkeypatch.setattr(auth, "_credentials_dir", lambda: tmp_path)
    monkeypatch.setattr(auth, "_cached_account", None)
    monkeypatch.setattr(auth, "_cached_api_creds", None)
    monkeypatch.setattr(auth, "_wallet_meta_cache", {})
    acct = Account.create()
    auth.save_credentials(acct.key.hex(), funder=funder, signature_type=sig_type)
    return acct.address


def _restart(monkeypatch):
    """What a new backend process starts with: nothing cached, and the signer
    role at its import-time value. Files on disk (key, meta, sidecars) stay."""
    monkeypatch.setattr(auth, "_cached_account", None)
    monkeypatch.setattr(auth, "_cached_api_creds", None)
    monkeypatch.setattr(auth, "_wallet_meta_cache", {})
    monkeypatch.setattr(auth, "_signer_role", "OWNER")
    monkeypatch.setattr(auth, "_signer_role_for", None, raising=False)
    monkeypatch.setattr(api, "_last_signer_role_recheck", None, raising=False)
    monkeypatch.setattr(service, "_sig_type_reconciled", False)


class _Chain:
    """A stubbed Polygon: owner() and sessionSignerAuthorizedUntil(key) of the
    deposit wallet. `down=True` makes every read fail like an RPC outage."""

    def __init__(self, monkeypatch, *, owner=OWNER, session_until=None, down=False):
        self.owner = owner
        self.session_until = (int(time.time()) + 86_400
                              if session_until is None else session_until)
        self.down = down
        self.calls: list[str] = []

        async def _rpc(method, params):
            data = params[0].get("data", "") if params and isinstance(params[0], dict) else ""
            self.calls.append(data[:10])
            if self.down:
                return None
            if data == api._SEL_OWNER:
                return "0x" + self.owner[2:].lower().rjust(64, "0")
            if data.startswith(api._SEL_SESSION_SIGNER_UNTIL):
                return "0x" + hex(self.session_until)[2:].rjust(64, "0")
            return None

        monkeypatch.setattr(api, "_rpc_first", _rpc)

    @property
    def role_reads(self) -> list[str]:
        return [c for c in self.calls
                if c in (api._SEL_OWNER, api._SEL_SESSION_SIGNER_UNTIL)]


def _stub_connect(monkeypatch):
    """Stub the network legs of `_establish_auth` other than the role reads:
    L2 cred derivation, the signature-type detection (keeps the stored type)
    and the balance warm."""
    async def _creds():
        return None

    async def _detect():
        return auth.get_signature_type()

    async def _balance():
        return {"balance": 0, "portfolio_value": 0}

    monkeypatch.setattr(api, "ensure_api_creds", _creds)
    monkeypatch.setattr(api, "detect_wallet_signature_type", _detect)
    monkeypatch.setattr(api, "get_balance", _balance)
    monkeypatch.setattr(service, "_sig_type_reconciled", False)


def _connect() -> bool:
    return asyncio.run(service._establish_auth(retries=1))


def _sign() -> str:
    o = auth.create_signed_order(token_id="123456789", side="SELL", price=0.42, size=7)
    return str(o["signature"])


def _assert_session_envelope(sig: str, signer: str) -> None:
    raw = bytes.fromhex(sig[2:])
    assert sig.endswith(MAGIC_HEX), "session-key order signed WITHOUT the envelope"
    sid, zero, inner = abi_decode(["bytes32", "bytes32", "bytes"], raw[:-32])
    assert sid == bytes.fromhex(signer[2:]).rjust(32, b"\x00")
    assert zero == b"\x00" * 32
    assert len(inner) >= 65


def _assert_owner_form(sig: str) -> None:
    assert not sig.endswith(MAGIC_HEX), "owner-form order wrongly wrapped in the envelope"



def test_first_order_after_a_restart_carries_the_session_envelope(monkeypatch, tmp_path):
    signer = _import_wallet(monkeypatch, tmp_path)
    _restart(monkeypatch)
    _stub_connect(monkeypatch)
    chain = _Chain(monkeypatch)

    assert _connect() is True
    _assert_session_envelope(_sign(), signer)
    assert api._SEL_OWNER in chain.role_reads


def test_saved_role_covers_a_restart_while_the_chain_is_unreachable(monkeypatch, tmp_path):
    signer = _import_wallet(monkeypatch, tmp_path)
    _stub_connect(monkeypatch)
    _Chain(monkeypatch)
    assert _connect() is True
    _assert_session_envelope(_sign(), signer)

    _restart(monkeypatch)
    _Chain(monkeypatch, down=True)
    assert _connect() is True
    _assert_session_envelope(_sign(), signer)

    side = auth._signer_role_file(auth.NETWORK)
    assert side.parent == auth._wallet_meta_file(auth.NETWORK).parent
    saved = json.loads(side.read_text(encoding="utf-8"))
    assert saved["role"] == "SESSION_KEY"
    assert saved["signer"] == signer.lower() and saved["funder"] == DEP.lower()
    auth.clear_credentials()
    assert not side.exists()


def test_a_fresh_chain_answer_beats_a_stale_saved_role(monkeypatch, tmp_path):
    signer = _import_wallet(monkeypatch, tmp_path)
    _Chain(monkeypatch)
    asyncio.run(api.check_deposit_wallet_authorization())
    _restart(monkeypatch)
    _stub_connect(monkeypatch)
    _Chain(monkeypatch, owner=signer)

    assert _connect() is True
    _assert_owner_form(_sign())
    saved = json.loads(auth._signer_role_file(auth.NETWORK).read_text(encoding="utf-8"))
    assert saved["role"] == "OWNER"


def test_the_owner_key_is_never_wrapped(monkeypatch, tmp_path):
    signer = _import_wallet(monkeypatch, tmp_path)
    _restart(monkeypatch)
    _stub_connect(monkeypatch)
    _Chain(monkeypatch, owner=signer)

    assert _connect() is True
    _assert_owner_form(_sign())


def test_a_learned_role_never_follows_a_different_key_or_wallet(monkeypatch, tmp_path):
    _import_wallet(monkeypatch, tmp_path)
    _Chain(monkeypatch)
    asyncio.run(api.check_deposit_wallet_authorization())
    assert auth.get_signer_role() == "SESSION_KEY"

    _import_wallet(monkeypatch, tmp_path)
    _stub_connect(monkeypatch)
    _Chain(monkeypatch, down=True)
    assert _connect() is True
    _assert_owner_form(_sign())

    signer = _import_wallet(monkeypatch, tmp_path)
    _Chain(monkeypatch)
    asyncio.run(api.check_deposit_wallet_authorization())
    auth.set_wallet_meta(funder=OTHER_DEP, signature_type=auth.SIGNATURE_TYPE_POLY_1271)
    _Chain(monkeypatch, down=True)
    assert auth.signer_role_for(signer, OTHER_DEP) == ("OWNER", "default")
    _assert_owner_form(_sign())


@pytest.mark.parametrize("sig_type,funder", [
    (auth.SIGNATURE_TYPE_EOA, ""),
    (auth.SIGNATURE_TYPE_POLY_PROXY, DEP),
    (auth.SIGNATURE_TYPE_POLY_GNOSIS_SAFE, DEP),
])
def test_other_wallet_modes_skip_the_role_check_and_never_wrap(
        monkeypatch, tmp_path, sig_type, funder):
    _import_wallet(monkeypatch, tmp_path, funder=funder, sig_type=sig_type)
    chain = _Chain(monkeypatch)
    if funder:
        asyncio.run(api.check_deposit_wallet_authorization())
    _stub_connect(monkeypatch)
    chain.calls.clear()

    assert _connect() is True
    assert chain.role_reads == []
    o = auth.create_signed_order(token_id="123456789", side="BUY", price=0.5, size=10)
    assert int(o["signatureType"]) == sig_type
    _assert_owner_form(str(o["signature"]))


def test_a_session_read_blip_still_selects_the_envelope_and_is_not_a_reject(
        monkeypatch, tmp_path):
    signer = _import_wallet(monkeypatch, tmp_path)
    chain = _Chain(monkeypatch)

    async def _rpc(method, params):
        if params[0]["data"] == api._SEL_OWNER:
            return "0x" + chain.owner[2:].lower().rjust(64, "0")
        return None
    monkeypatch.setattr(api, "_rpc_first", _rpc)

    out = asyncio.run(api.check_deposit_wallet_authorization())
    assert out["ok"] is True and out["tested"] is False
    assert auth.signer_role_for(signer, DEP) == ("SESSION_KEY", "chain")
    _assert_session_envelope(_sign(), signer)



def _stub_order_post(monkeypatch, replies):
    """Stub place_limit_order's network legs. Each POST pops the next reply: an
    exception is raised, anything else returned. Captures every posted body."""
    posted: list[dict] = []

    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222",
                "tick_size": 0.01, "min_size": 5, "neg_risk": False}

    async def _ensure():
        return None

    async def _allowance(_token_id):
        return None

    async def _authed(method, path, *, json_body=None, idempotent=True, **_kw):
        if method != "POST":
            raise api.PolymarketAPIError(404, "not found")
        posted.append(json_body)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "ensure_api_creds", _ensure)
    monkeypatch.setattr(api, "refresh_token_allowance", _allowance)
    monkeypatch.setattr(api.auth, "get_api_creds", lambda: {"apiKey": "ak"})
    monkeypatch.setattr(api, "_authed_request", _authed)
    monkeypatch.setattr(api, "_AMBIGUOUS_LOOKUP_GAP_SEC", 0.0)
    return posted


def _place():
    return asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="sell", count=5, price_cents=40))


@pytest.mark.parametrize("rejection", [
    api.PolymarketAPIError(400, {"error": "invalid signature"}),
    {"success": False, "errorMsg": "invalid signature"},
])
def test_a_signature_rejection_relearns_the_role_and_resigns_once(
        monkeypatch, tmp_path, rejection):
    signer = _import_wallet(monkeypatch, tmp_path)
    _restart(monkeypatch)
    _stub_connect(monkeypatch)
    chain = _Chain(monkeypatch, down=True)
    assert _connect() is True
    chain.down = False

    posted = _stub_order_post(monkeypatch, [
        rejection, {"orderID": "oid-2", "status": "live", "success": True},
    ])
    res = _place()
    assert len(posted) == 2
    _assert_owner_form(posted[0]["order"]["signature"])
    _assert_session_envelope(posted[1]["order"]["signature"], signer)
    assert res["order"]["order_id"] == "oid-2"
    _assert_session_envelope(_sign(), signer)


def test_a_rejection_that_does_not_change_the_role_is_not_resent(monkeypatch, tmp_path):
    signer = _import_wallet(monkeypatch, tmp_path)
    _restart(monkeypatch)
    _stub_connect(monkeypatch)
    chain = _Chain(monkeypatch, owner=signer)
    assert _connect() is True

    posted = _stub_order_post(monkeypatch, [
        api.PolymarketAPIError(400, {"error": "invalid signature"}),
    ])
    with pytest.raises(api.PolymarketAPIError):
        _place()
    assert len(posted) == 1

    reads = len(chain.role_reads)
    posted = _stub_order_post(monkeypatch, [
        api.PolymarketAPIError(502, "bad gateway: signature service"),
    ])
    with pytest.raises(api.PolymarketAPIError):
        _place()
    assert len(posted) == 1
    posted = _stub_order_post(monkeypatch, [
        api.PolymarketAPIError(400, {"error": "invalid signature"}),
    ])
    with pytest.raises(api.PolymarketAPIError):
        _place()
    assert len(posted) == 1
    assert len(chain.role_reads) == reads


def test_other_wallet_modes_are_never_resent_on_a_signature_rejection(monkeypatch, tmp_path):
    _import_wallet(monkeypatch, tmp_path, sig_type=auth.SIGNATURE_TYPE_POLY_PROXY)
    chain = _Chain(monkeypatch)
    posted = _stub_order_post(monkeypatch, [
        api.PolymarketAPIError(400, {"error": "invalid signature"}),
    ])
    with pytest.raises(api.PolymarketAPIError):
        _place()
    assert len(posted) == 1 and chain.role_reads == []



_CHILD = textwrap.dedent("""
    import asyncio, json, sys, time
    import polymarket_api as api, polymarket_auth as auth
    owner = sys.argv[2]
    up = sys.argv[1] == "learn"

    async def _rpc(method, params):
        if not up:
            return None                      # the chain is unreachable
        if params[0]["data"] == api._SEL_OWNER:
            return "0x" + owner[2:].lower().rjust(64, "0")
        return "0x" + hex(int(time.time()) + 86400)[2:].rjust(64, "0")
    api._rpc_first = _rpc

    if up:
        asyncio.run(api.check_deposit_wallet_authorization())
    o = auth.create_signed_order(token_id="123456789", side="SELL", price=0.42, size=7)
    print(json.dumps({"signer": auth.get_address(), "signature": o["signature"]}))
""")


def test_the_role_survives_a_real_process_restart(tmp_path):
    env = dict(os.environ)
    env["KRYPT_POLYBOT_USERDATA"] = str(tmp_path / "userdata")
    env["KRYPT_POLYBOT_VAULT"] = str(tmp_path / "vault")
    env["PYTHONIOENCODING"] = "utf-8"
    py_dir = str(Path(api.__file__).resolve().parent)

    setup = ("import sys, polymarket_auth as auth\n"
             "auth.save_credentials(sys.argv[1], funder=sys.argv[2])\n")
    subprocess.run([sys.executable, "-c", setup, Account.create().key.hex(), DEP],
                   cwd=py_dir, env=env, check=True, timeout=120)

    def _launch(mode):
        out = subprocess.run([sys.executable, "-c", _CHILD, mode, OWNER],
                             cwd=py_dir, env=env, check=True, timeout=120,
                             capture_output=True, text=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    first = _launch("learn")
    _assert_session_envelope(first["signature"], first["signer"])
    second = _launch("offline")
    _assert_session_envelope(second["signature"], second["signer"])


_ORDER_TYPE = ("Order(uint256 salt,address maker,address signer,uint256 tokenId,"
               "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
               "uint256 timestamp,bytes32 metadata,bytes32 builder)")


def _erc7739_digest(order: dict, exchange: str) -> tuple[bytes, bytes, bytes]:
    """(digest, app domain separator, contents hash) for a POLY_1271 order."""
    from eth_abi import encode
    from eth_utils import keccak

    contents = keccak(encode(
        ["bytes32", "uint256", "address", "address", "uint256", "uint256", "uint256",
         "uint8", "uint8", "uint256", "bytes32", "bytes32"],
        [keccak(text=_ORDER_TYPE), int(order["salt"]), order["maker"], order["signer"],
         int(order["tokenId"]), int(order["makerAmount"]), int(order["takerAmount"]),
         0 if order["side"] == "BUY" else 1, int(order["signatureType"]),
         int(order["timestamp"]), bytes.fromhex(order["metadata"][2:]),
         bytes.fromhex(order["builder"][2:])]))
    app_domain = keccak(encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [keccak(text="EIP712Domain(string name,string version,uint256 chainId,"
                     "address verifyingContract)"),
         keccak(text="Polymarket CTF Exchange"), keccak(text=auth.EXCHANGE_VERSION),
         auth.CHAIN_ID, exchange]))
    sign_struct = keccak(encode(
        ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "address", "bytes32"],
        [keccak(text="TypedDataSign(Order contents,string name,string version,"
                     "uint256 chainId,address verifyingContract,bytes32 salt)" + _ORDER_TYPE),
         contents, keccak(text="DepositWallet"), keccak(text="1"),
         auth.CHAIN_ID, order["signer"], b"\x00" * 32]))
    return keccak(b"\x19\x01" + app_domain + sign_struct), app_domain, contents


@pytest.mark.parametrize("neg_risk", [False, True])
@pytest.mark.parametrize("role", ["SESSION_KEY", "OWNER"])
def test_a_deposit_wallet_order_recovers_to_the_imported_key(
        monkeypatch, tmp_path, role, neg_risk):
    signer = _import_wallet(monkeypatch, tmp_path)
    auth.set_signer_role(role, signer, DEP)
    order = auth.create_signed_order(token_id="123456789", side="BUY", price=0.42,
                                     size=7, neg_risk=neg_risk)
    assert order["signatureType"] == auth.SIGNATURE_TYPE_POLY_1271
    assert order["maker"] == order["signer"] == DEP

    sig = bytes.fromhex(order["signature"][2:])
    if role == "SESSION_KEY":
        assert sig[-32:] == auth.SESSION_SIGNER_MAGIC
        sid, zero, inner = abi_decode(["bytes32", "bytes32", "bytes"], sig[:-32])
        assert sid == bytes.fromhex(signer[2:]).rjust(32, b"\x00")
        assert zero == b"\x00" * 32
    else:
        assert sig[-32:] != auth.SESSION_SIGNER_MAGIC
        inner = sig

    exchange = auth.NEG_RISK_EXCHANGE_ADDRESS if neg_risk else auth.EXCHANGE_ADDRESS
    digest, app_domain, contents = _erc7739_digest(order, exchange)
    assert inner[65:97] == app_domain
    assert inner[97:129] == contents
    assert inner[129:-2] == _ORDER_TYPE.encode()
    assert int.from_bytes(inner[-2:], "big") == len(_ORDER_TYPE)
    assert Account._recover_hash(digest, signature=inner[:65]) == signer
