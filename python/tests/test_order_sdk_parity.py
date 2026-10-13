"""The order the app signs and POSTs is the order Polymarket's own SDK would.

py_clob_client_v2 builds the V2 /order body with `order_to_json_v2`. The app
signs EOA / POLY_PROXY / GNOSIS_SAFE orders with its own inline EIP-712 code
and hands POLY_1271 orders to the SDK's builder, so these pin both paths to
the SDK: the same keys, the same values, the same wire bytes.

Every key here is a throwaway generated for the test; nothing is sent anywhere.
"""
from __future__ import annotations

import json

import pytest
from eth_account import Account

import polymarket_auth as auth
from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2
from py_clob_client_v2.order_utils.model.order_data_v2 import (
    OrderDataV2, SignedOrderV2, order_to_json_v2,
)
from py_clob_client_v2.order_utils.model.side import Side
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.signer import Signer

KEY = "0x" + "42" * 32
FUNDER = "0xDDDD000000000000000000000000000000000009"
TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
SALT = 123456789012
TS_MS = 1_760_000_000_000
SHARES, CASH = "7000000", "2940000"


def _wire(monkeypatch, tmp_path, sig_type: int, funder: str) -> str:
    """Sign as KEY in the given wallet mode, with no credential files read."""
    acct = Account.from_key(KEY)
    monkeypatch.setattr(auth, "_credentials_dir", lambda: tmp_path)
    monkeypatch.setattr(auth, "_load_account", lambda: acct)
    monkeypatch.setattr(auth, "_load_private_key", lambda: KEY)
    monkeypatch.setattr(auth, "get_signature_type", lambda env=None: sig_type)
    monkeypatch.setattr(auth, "get_funder", lambda env=None: funder)
    monkeypatch.setattr(auth, "_signer_role", "OWNER")
    monkeypatch.setattr(auth, "_signer_role_for", None)
    return acct.address


@pytest.mark.parametrize("neg_risk", [False, True])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("sig_type,funder", [
    (auth.SIGNATURE_TYPE_EOA, ""),
    (auth.SIGNATURE_TYPE_POLY_PROXY, FUNDER),
    (auth.SIGNATURE_TYPE_POLY_GNOSIS_SAFE, FUNDER),
])
def test_an_eoa_signed_order_body_is_the_sdks_byte_for_byte(
        monkeypatch, tmp_path, sig_type, funder, side, neg_risk):
    addr = _wire(monkeypatch, tmp_path, sig_type, funder)
    ours = auth.create_signed_order(
        token_id=TOKEN, side=side, price=0.42, size=7, neg_risk=neg_risk,
        salt=SALT, ts_ms=TS_MS)

    exchange = auth.NEG_RISK_EXCHANGE_ADDRESS if neg_risk else auth.EXCHANGE_ADDRESS
    builder = ExchangeOrderBuilderV2(exchange, auth.CHAIN_ID, Signer(KEY, auth.CHAIN_ID),
                                     generate_salt=lambda: str(SALT))
    sdk = builder.build_signed_order(OrderDataV2(
        maker=funder or addr, signer=addr, tokenId=TOKEN,
        makerAmount=CASH if side == "BUY" else SHARES,
        takerAmount=SHARES if side == "BUY" else CASH,
        side=Side.BUY if side == "BUY" else Side.SELL,
        signatureType=SignatureTypeV2(sig_type), timestamp=str(TS_MS), expiration="0",
    ))
    want = order_to_json_v2(sdk, owner="ak", order_type="GTC")["order"]

    assert set(ours) == set(want), f"keys the SDK does not send: {set(ours) - set(want)}"
    assert ours == want
    assert json.dumps(ours, sort_keys=True) == json.dumps(want, sort_keys=True)


def test_a_deposit_wallet_order_body_is_the_sdks(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, auth.SIGNATURE_TYPE_POLY_1271, FUNDER)
    ours = auth.create_signed_order(token_id=TOKEN, side="SELL", price=0.42, size=7)
    signed = SignedOrderV2(
        salt=str(ours["salt"]), maker=ours["maker"], signer=ours["signer"],
        tokenId=ours["tokenId"], makerAmount=ours["makerAmount"],
        takerAmount=ours["takerAmount"], side=Side.SELL,
        signatureType=SignatureTypeV2.POLY_1271, timestamp=ours["timestamp"],
        metadata=ours["metadata"], builder=ours["builder"],
        expiration=ours["expiration"], signature=ours["signature"],
    )
    want = order_to_json_v2(signed, owner="ak", order_type="GTC")["order"]
    assert ours == want
    assert (ours["maker"], ours["signer"]) == (FUNDER, FUNDER)
    assert (ours["makerAmount"], ours["takerAmount"]) == (SHARES, CASH)



def test_the_apps_own_signer_produces_the_selftest_vector(monkeypatch, tmp_path):
    v = auth.SIGNER_VECTOR
    acct = Account.from_key(v["key"])
    monkeypatch.setattr(auth, "_load_account", lambda: acct)
    monkeypatch.setattr(auth, "get_signature_type", lambda env=None: auth.SIGNATURE_TYPE_EOA)
    monkeypatch.setattr(auth, "get_funder", lambda env=None: "")
    o = auth.create_signed_order(token_id=v["tokenId"], side="BUY", price=0.42, size=1,
                                 salt=v["salt"], ts_ms=v["timestamp"])
    assert (o["makerAmount"], o["takerAmount"]) == (v["makerAmount"], v["takerAmount"])
    assert o["signature"] == auth._SIGNER_VECTOR_EOA_SIG


def test_verify_signer_returns_the_known_deposit_wallet_signature():
    assert auth.verify_signer() == auth._SIGNER_VECTOR_1271_SIG


def test_verify_signer_fails_a_signer_that_signs_something_else(monkeypatch):
    import py_clob_client_v2.order_utils.exchange_order_builder_v2 as eob
    monkeypatch.setattr(eob, "CTF_EXCHANGE_V2_DOMAIN_NAME", "Polymarket CTF Exchange V9")
    with pytest.raises(RuntimeError, match="drifted"):
        auth.verify_signer()
