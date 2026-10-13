"""A POST /order that fails ambiguously must not leave an order nobody tracks.

A timeout, a dropped connection or a 5xx after the body went out says
nothing about whether the CLOB took the order. It used to surface as a
plain failure: the engines wrote an error row with no order id, so a
resting order that HAD landed was never polled, never cancelled and never
reconciled. The order's id is its EIP-712 hash, known before sending, so
the app now looks the order up; when even that is inconclusive it says
"unknown" and hands the id to the caller.

Throwaway keys, stubbed transport: nothing touches the network.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from eth_abi import decode as abi_decode
from eth_account import Account
from eth_utils import keccak

import db
import polymarket_api as api
import polymarket_auth as auth
import terminal_trade as tt

KEY = "0x" + "5a" * 32
DEP = "0xDDDD000000000000000000000000000000000009"
TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _wallet(monkeypatch, tmp_path, sig_type=auth.SIGNATURE_TYPE_POLY_PROXY):
    acct = Account.from_key(KEY)
    monkeypatch.setattr(auth, "_credentials_dir", lambda: tmp_path)
    monkeypatch.setattr(auth, "_load_account", lambda: acct)
    monkeypatch.setattr(auth, "_load_private_key", lambda: KEY)
    monkeypatch.setattr(auth, "get_address", lambda: acct.address)
    monkeypatch.setattr(auth, "get_signature_type", lambda env=None: sig_type)
    monkeypatch.setattr(auth, "get_funder", lambda env=None: DEP)
    monkeypatch.setattr(auth, "_signer_role", "OWNER")
    monkeypatch.setattr(auth, "_signer_role_for", None)
    return acct.address



@pytest.mark.parametrize("neg_risk", [False, True])
def test_the_order_hash_is_the_digest_the_signature_commits_to(monkeypatch, tmp_path, neg_risk):
    signer = _wallet(monkeypatch, tmp_path)
    o = auth.create_signed_order(token_id=TOKEN, side="BUY", price=0.42, size=7,
                                 neg_risk=neg_risk)
    h = auth.order_hash(o, neg_risk=neg_risk)
    assert h.startswith("0x") and len(h) == 66
    assert Account._recover_hash(bytes.fromhex(h[2:]), signature=o["signature"]) == signer
    assert auth.order_hash(o, neg_risk=not neg_risk) != h


def test_a_deposit_wallet_order_hash_matches_its_signed_contents(monkeypatch, tmp_path):
    _wallet(monkeypatch, tmp_path, auth.SIGNATURE_TYPE_POLY_1271)
    monkeypatch.setattr(auth, "_signer_role", "SESSION_KEY")
    monkeypatch.setattr(auth, "signer_role_for", lambda s, f: ("SESSION_KEY", "chain"))
    o = auth.create_signed_order(token_id=TOKEN, side="SELL", price=0.42, size=7)
    sig = bytes.fromhex(o["signature"][2:])
    _sid, _zero, inner = abi_decode(["bytes32", "bytes32", "bytes"], sig[:-32])
    want = "0x" + keccak(b"\x19\x01" + inner[65:97] + inner[97:129]).hex()
    assert auth.order_hash(o) == want



def _post_then(monkeypatch, tmp_path, post_error, lookups):
    """Real signing; the POST raises `post_error`; each GET /data/order pops
    the next `lookups` item (an exception is raised, a callable is given the
    looked-up id). Returns the call log."""
    _wallet(monkeypatch, tmp_path)
    log: dict = {"posted": [], "looked_up": []}

    async def _meta(_t):
        return {"yes_token": TOKEN, "no_token": "2", "tick_size": 0.01,
                "min_size": 5, "neg_risk": False}

    async def _none(*_a, **_k):
        return None

    async def _authed(method, path, *, json_body=None, idempotent=True, **_kw):
        if method == "POST":
            log["posted"].append(json_body)
            raise post_error
        log["looked_up"].append(path)
        r = lookups.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(path.rsplit("/", 1)[1]) if callable(r) else r

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "ensure_api_creds", _none)
    monkeypatch.setattr(api, "refresh_token_allowance", _none)
    monkeypatch.setattr(api.auth, "get_api_creds", lambda: {"apiKey": "ak"})
    monkeypatch.setattr(api, "_authed_request", _authed)
    monkeypatch.setattr(api, "trading_restriction", lambda: None)
    monkeypatch.setattr(api.asyncio, "sleep", _none)
    return log


def _place(order_type="GTC"):
    return _run(api.place_limit_order(ticker="0xc", side="yes", action="buy",
                                      count=5, price_cents=40, order_type=order_type))


@pytest.mark.parametrize("post_error", [
    httpx.ReadTimeout("read timed out"),
    httpx.RemoteProtocolError("server disconnected"),
    api.PolymarketAPIError(502, "bad gateway"),
])
def test_an_order_the_clob_did_take_is_tracked_by_its_real_id(monkeypatch, tmp_path, post_error):
    log = _post_then(monkeypatch, tmp_path, post_error, [
        lambda oid: {"id": oid, "status": "LIVE", "size_matched": "0", "original_size": "5"},
    ])
    res = _place()
    want = auth.order_hash(log["posted"][0]["order"])
    assert log["looked_up"] == [f"/data/order/{want}"]
    assert res["order"] == {"order_id": want, "status": "live"}
    assert res["raw"]["recovered"] is True


def test_an_order_that_cannot_be_found_is_unknown_not_failed(monkeypatch, tmp_path):
    log = _post_then(monkeypatch, tmp_path, api.PolymarketAPIError(503, "upstream"), [
        api.PolymarketAPIError(404, "not found"),
        api.PolymarketAPIError(404, "not found"),
    ])
    with pytest.raises(api.OrderStatusUnknownError) as ei:
        _place()
    want = auth.order_hash(log["posted"][0]["order"])
    assert ei.value.order_id == want
    assert ei.value.response["order"] == {"order_id": want, "status": "unknown"}
    assert len(log["looked_up"]) == 2
    assert isinstance(ei.value, api.PolymarketAPIError)


def test_a_lookup_that_errors_too_is_unknown(monkeypatch, tmp_path):
    _post_then(monkeypatch, tmp_path, httpx.ReadTimeout("t"), [
        httpx.ReadTimeout("t"), httpx.ReadTimeout("t"),
    ])
    with pytest.raises(api.OrderStatusUnknownError):
        _place()


@pytest.mark.parametrize("post_error", [
    httpx.ConnectError("refused"),
    httpx.ConnectTimeout("connect timed out"),
    api.PolymarketAPIError(400, "not enough balance / allowance"),
    api.PostOnlyModeError(503, "post only"),
])
def test_a_definitive_failure_is_not_looked_up(monkeypatch, tmp_path, post_error):
    log = _post_then(monkeypatch, tmp_path, post_error, [])
    with pytest.raises(type(post_error)):
        _place()
    assert log["looked_up"] == []


def test_a_fak_found_killed_with_nothing_matched_is_a_clean_no_fill(monkeypatch, tmp_path):
    _post_then(monkeypatch, tmp_path, api.PolymarketAPIError(500, "x"), [
        {"id": "0xk", "status": "CANCELED", "size_matched": "0", "original_size": "5"},
    ])
    res = _place("FAK")
    assert res["order"] == {"order_id": "", "status": "unmatched"}



def _unknown(oid="0x" + "ab" * 32):
    return api.OrderStatusUnknownError(oid, httpx.ReadTimeout("t"))


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "t.db")
    db.init_db()
    return tmp_path


def test_the_manual_ticket_records_the_id_and_says_unknown(fresh_db, monkeypatch):
    async def _m(cid, include_raw=False):
        m = {"conditionId": cid, "title": "T", "category": "politics", "status": "open",
             "spread": 1.0, "liquidityUsd": 1e6, "hoursToClose": 50.0, "probability": 15.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "orderMinSize": 1,
                      "acceptingOrders": True, "feesEnabled": False}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _b(cid, side="yes"):
        return {"available": True, "note": None, "asks": [{"priceCents": 15, "size": 500}],
                "bids": [{"priceCents": 13, "size": 500}], "bestAsk": 15, "bestBid": 13}

    async def _avail(paper):
        return 100.0

    async def _place_unknown(**_kw):
        raise _unknown()

    monkeypatch.setattr(tt.term, "market", _m)
    monkeypatch.setattr(tt.term, "book", _b)
    monkeypatch.setattr(tt.api, "_deposit_wallet_issue", lambda: "")
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    monkeypatch.setattr(tt, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(tt, "_available_cash_usd", _avail)
    monkeypatch.setattr(tt.api, "place_limit_order", _place_unknown)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC", "confirmUsd": 0.70}))
    oid = "0x" + "ab" * 32
    assert r["ok"] is False and r["status"] == "unknown" and r["orderId"] == oid
    assert oid[:10] in r["message"] and "may" in r["message"]
    row = tt.order_log()["orders"][0]
    assert (row["orderId"], row["status"]) == (oid, "unknown")
