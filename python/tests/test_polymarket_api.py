"""Tests for polymarket_api: Gamma market normalisation, the public trades
feed direction logic, and CLOB quote reads.

These pin the parsing that the scanner/trader depend on — Gamma ships
`outcomes`/`outcomePrices`/`clobTokenIds` as JSON-encoded strings, and the
trades feed must be folded into the engine's internal yes/no model.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import polymarket_api as api


def test_normalize_market_parses_gamma_json_strings():
    raw = {
        "conditionId": "0xabc",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.62", "0.38"]),
        "clobTokenIds": json.dumps(["111", "222"]),
        "volumeNum": 1234.5,
        "volume24hr": 50.0,
        "liquidityNum": 999.0,
        "negRisk": False,
        "orderPriceMinTickSize": 0.01,
        "active": True,
        "closed": False,
        "endDate": "2026-07-01T00:00:00Z",
        "events": [{"slug": "evt-x", "tags": [{"slug": "crypto"}]}],
    }
    m = api._normalize_market(raw)
    assert m["ticker"] == "0xabc"
    assert m["yes_token"] == "111" and m["no_token"] == "222"
    assert m["yes_bid_dollars"] == 0.62 and m["no_price_dollars"] == 0.38
    assert m["category"] == "crypto"
    assert m["event_ticker"] == "evt-x"
    assert m["volume_fp"] == 1234.5
    assert m["status"] == "open"
    meta = api._meta["0xabc"]
    assert meta["yes_token"] == "111" and meta["neg_risk"] is False


def _gamma_raw(**over) -> dict:
    raw = {
        "conditionId": "0xbook", "question": "q", "slug": "s",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.41", "0.59"]),
        "clobTokenIds": json.dumps(["1", "2"]),
    }
    raw.update(over)
    return raw


def test_normalize_market_keeps_gamma_top_of_book():
    """Gamma ships its own bestBid/bestAsk; the mid used to be stored as both,
    which threw the spread away for every consumer of the market table."""
    m = api._normalize_market(_gamma_raw(bestBid=0.40, bestAsk=0.42))
    assert m["yes_bid_dollars"] == 0.40
    assert m["yes_ask_dollars"] == 0.42
    assert m["last_price_dollars"] == 0.41


@pytest.mark.parametrize("bid,ask", [
    (0, 1),
    (None, 0.064),
    (0.999, 1),
    (0.55, 0.50),
])
def test_normalize_market_falls_back_to_mid_without_a_two_sided_book(bid, ask):
    """Anything short of a real two-sided book keeps the old reading, mid on
    both sides, so no consumer sees a spread that only half exists."""
    over = {"bestAsk": ask}
    if bid is not None:
        over["bestBid"] = bid
    m = api._normalize_market(_gamma_raw(**over))
    assert m["yes_bid_dollars"] == 0.41 and m["yes_ask_dollars"] == 0.41


def test_normalize_market_resolution_yes_wins():
    raw = {
        "conditionId": "0xdef",
        "question": "q", "slug": "s",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["1", "0"]),
        "clobTokenIds": json.dumps(["1", "2"]),
        "closed": True,
        "umaResolutionStatus": "resolved",
    }
    m = api._normalize_market(raw)
    assert m["result"] == "yes"
    assert m["settlement_value_dollars"] == 1.0
    assert m["status"] == "settled"


def test_normalize_market_missing_condition_id_is_none():
    assert api._normalize_market({"question": "no id"}) is None
    assert api._normalize_market(None) is None


def test_fee_schedule_passthrough_from_gamma():
    base = {
        "conditionId": "0xfee", "question": "q", "slug": "s",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps(["1", "2"]),
    }
    m = api._normalize_market({
        **base,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True},
    })
    assert m["fee_schedule"] == {"enabled": True, "rate": 0.07, "exponent": 1.0,
                                 "rebateRate": 0.0}
    mr = api._normalize_market({
        **base, "feesEnabled": True,
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True, "rebateRate": 0.2},
    })
    assert mr["fee_schedule"]["rebateRate"] == 0.2
    m2 = api._normalize_market({**base, "feesEnabled": False})
    assert m2["fee_schedule"] == {"enabled": False}
    m3 = api._normalize_market(base)
    assert m3["fee_schedule"] is None


def test_refresh_balance_allowance_once_per_session(monkeypatch):
    calls = []

    async def _authed(method, path, **kw):
        calls.append((method, path, kw.get("params")))
        return {}

    monkeypatch.setattr(api, "_authed_request", _authed)
    monkeypatch.setattr(api.auth, "get_signature_type", lambda env=None: 0)
    monkeypatch.setattr(api, "_allowance_synced", False)

    assert asyncio.run(api.refresh_balance_allowance()) is True
    assert calls == [("GET", "/balance-allowance/update",
                      {"asset_type": "COLLATERAL", "signature_type": "0"})]
    assert asyncio.run(api.refresh_balance_allowance()) is True
    assert len(calls) == 1
    assert asyncio.run(api.refresh_balance_allowance(force=True)) is True
    assert len(calls) == 2


def test_refresh_balance_allowance_failure_is_nonfatal(monkeypatch):
    async def _authed(method, path, **kw):
        raise api.PolymarketAPIError(500, "boom")

    monkeypatch.setattr(api, "_authed_request", _authed)
    monkeypatch.setattr(api.auth, "get_signature_type", lambda env=None: 0)
    monkeypatch.setattr(api, "_allowance_synced", False)
    assert asyncio.run(api.refresh_balance_allowance()) is False
    assert api._allowance_synced is False


def _v2(rows, cursor=None):
    """A Data API v2 body: the `{data, pagination}` envelope."""
    return {"data": rows, "pagination": {"has_more": cursor is not None, "next_cursor": cursor}}


def test_fetch_recent_trades_buy_yes(monkeypatch):
    seen = {}

    async def _get(url, params=None):
        seen["url"], seen["params"] = url, params
        return _v2([{
            "price": 0.30, "size": 100, "outcome_index": 0, "side": "BUY",
            "condition_id": "0xc", "transaction_hash": "0xtx", "token_id": "tok",
            "slug": "m", "title": "t", "outcome": "Yes", "event_slug": "e",
            "timestamp": 1782000000,
        }])
    monkeypatch.setattr(api, "_get", _get)
    rows = asyncio.run(api.fetch_recent_trades(limit=10))
    r = rows[0]
    assert seen["url"].endswith("/v2/trades")
    assert seen["params"]["taker_only"] == "true"
    assert r["taker_side"] == "yes"
    assert r["yes_price_dollars"] == 0.30
    assert r["count_fp"] == 100
    assert r["ticker"] == "0xc"
    assert r["trade_id"] == "0xtx-tok-300-100"


def test_fetch_recent_trades_sell_is_long_opposite(monkeypatch):
    async def _get(url, params=None):
        return _v2([{
            "price": 0.30, "size": 50, "outcome_index": 0, "side": "SELL",
            "condition_id": "0xc", "transaction_hash": "0xtx2", "token_id": "tok",
            "slug": "m", "title": "t", "outcome": "Yes",
        }])
    monkeypatch.setattr(api, "_get", _get)
    r = asyncio.run(api.fetch_recent_trades(limit=10))[0]
    assert r["taker_side"] == "no"
    assert r["no_price_dollars"] == 0.70


def test_snap_price_to_tick():
    assert api._snap_price_to_tick(0.376, 0.01) == 0.38
    assert api._snap_price_to_tick(0.37, 0.1) == 0.4
    assert api._snap_price_to_tick(0.6234, 0.001) == 0.623
    assert api._snap_price_to_tick(0.97, 0.1) == 0.9
    assert api._snap_price_to_tick(0.02, 0.1) == 0.1


def test_get_quote_reads_clob_prices(monkeypatch):
    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222"}

    async def _price(_token, side):
        return 0.58 if side == "BUY" else 0.62

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "_clob_price", _price)
    q = asyncio.run(api.get_quote("0xc", "yes"))
    assert q == {"bid_cents": 58, "ask_cents": 62}



_TOK = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_ADDR = "0x7a4043DE8fa7C458eD701Ebe487E7203eeF72197"


def test_balance_read_returns_first_valid_despite_dead_rpcs(monkeypatch):
    good = api.POLYGON_RPCS[3]

    class _R:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    class _Client:
        async def post(self, url, json=None, timeout=None):
            if url == good:
                return _R({"result": hex(1_000_000)})
            raise RuntimeError("rpc down")

    async def _fake_client():
        return _Client()
    monkeypatch.setattr(api, "_get_client", _fake_client)
    assert asyncio.run(api._erc20_balance_usd(_TOK, _ADDR)) == 1.0


def test_balance_read_all_fail_returns_none(monkeypatch):
    class _Client:
        async def post(self, url, json=None, timeout=None):
            raise RuntimeError("rpc down")

    async def _fake_client():
        return _Client()
    monkeypatch.setattr(api, "_get_client", _fake_client)
    assert asyncio.run(api._erc20_balance_usd(_TOK, _ADDR)) is None


def test_balance_read_rejects_bad_address():
    assert asyncio.run(api._erc20_balance_usd(_TOK, "not-an-address")) is None



def test_net_fail_streak_recycles_client(monkeypatch):
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api, "_last_net_ok", 0.0)
    monkeypatch.setattr(api.time, "monotonic", lambda: 10_000.0)

    async def _run():
        for _ in range(api._NET_FAIL_RECYCLE - 1):
            await api._note_net_fail()
        assert recycled["n"] == 0
        await api._note_net_fail()
        assert recycled["n"] == 1

    asyncio.run(_run())


def test_net_ok_resets_streak_so_one_bad_endpoint_never_recycles(monkeypatch):
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api, "_last_net_ok", 0.0)
    monkeypatch.setattr(api.time, "monotonic", lambda: 10_000.0)

    async def _run():
        for _ in range(api._NET_FAIL_RECYCLE * 3):
            await api._note_net_fail()
            api._note_net_ok()
        assert recycled["n"] == 0

    asyncio.run(_run())


def test_recycle_throttled_within_gap(monkeypatch):
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    clock = {"t": 10_000.0}
    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api, "_last_net_ok", 0.0)
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["t"])

    async def _run():
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 1
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 1
        clock["t"] += api._CLIENT_RECYCLE_MIN_GAP + 1
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 2

    asyncio.run(_run())


def test_recent_success_blocks_recycle(monkeypatch):
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    clock = {"t": 10_000.0}
    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["t"])

    async def _run():
        api._note_net_ok()
        for _ in range(api._NET_FAIL_RECYCLE * 2):
            await api._note_net_fail()
        assert recycled["n"] == 0
        clock["t"] += api._NET_STALL_SECS + 1
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 1

    asyncio.run(_run())



class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


def _wire_authed(monkeypatch, responses, state):
    class _Client:
        async def request(self, *a, **k):
            r = responses[state["i"]]
            state["i"] += 1
            return r

    async def _noop_ensure():
        return None

    async def _fake_client():
        return _Client()

    monkeypatch.setattr(api, "ensure_api_creds", _noop_ensure)
    monkeypatch.setattr(api, "_get_client", _fake_client)
    monkeypatch.setattr(api.auth, "l2_headers", lambda *a, **k: {})
    monkeypatch.setattr(api.auth, "clear_api_creds",
                        lambda env=None: state.__setitem__("cleared", state["cleared"] + 1))


def test_is_stale_creds_error():
    assert api._is_stale_creds_error(
        {"error": "the order signer address has to be the address of the api key"})
    assert api._is_stale_creds_error("Unauthorized")
    assert not api._is_stale_creds_error({"error": "min tick size violation"})


def test_authed_request_self_heals_stale_creds(monkeypatch):
    state = {"i": 0, "cleared": 0}
    _wire_authed(monkeypatch, [
        _Resp(400, {"error": "the order signer address has to be the address of the api key"}),
        _Resp(200, {"ok": True}),
    ], state)
    out = asyncio.run(api._authed_request("POST", "/order", json_body={"x": 1}))
    assert out == {"ok": True}
    assert state["i"] == 2 and state["cleared"] == 1


def test_authed_request_non_auth_400_does_not_rederive(monkeypatch):
    state = {"i": 0, "cleared": 0}
    _wire_authed(monkeypatch, [_Resp(400, {"error": "min tick size violation"})], state)
    with pytest.raises(api.PolymarketAPIError):
        asyncio.run(api._authed_request("POST", "/order", json_body={"x": 1}))
    assert state["cleared"] == 0



def _stub_order_path(monkeypatch, captured):
    """Stub everything place_limit_order touches except the body assembly, and
    capture the POST body so we can assert the V2 order-type fields."""
    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222",
                "tick_size": 0.01, "min_size": 5, "neg_risk": False}

    async def _ensure():
        return None

    async def _authed(method, path, *, json_body=None, idempotent=True, **_kw):
        captured["body"] = json_body
        return {"orderID": "oid-1", "status": "matched", "success": True}

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "ensure_api_creds", _ensure)
    monkeypatch.setattr(api.auth, "get_api_creds", lambda: {"apiKey": "ak"})
    monkeypatch.setattr(api.auth, "create_signed_order",
                        lambda **kw: {"salt": "1", "maker": "0x", "signature": "0x"})
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0xDeposit")
    monkeypatch.setattr(api.auth, "get_address", lambda: "0xEOAsigner")
    monkeypatch.setattr(api, "_authed_request", _authed)


def test_place_limit_order_defaults_to_gtc(monkeypatch):
    cap = {}
    _stub_order_path(monkeypatch, cap)
    asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="buy", count=5, price_cents=90))
    assert cap["body"]["orderType"] == "GTC"
    assert cap["body"]["postOnly"] is False
    assert cap["body"]["deferExec"] is False


def test_place_limit_order_marketable_fak(monkeypatch):
    cap = {}
    _stub_order_path(monkeypatch, cap)
    asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="buy", count=5, price_cents=90, order_type="fak"))
    assert cap["body"]["orderType"] == "FAK"
    assert cap["body"]["postOnly"] is False


def test_place_limit_order_rejects_unknown_order_type(monkeypatch):
    _stub_order_path(monkeypatch, {})
    with pytest.raises(ValueError):
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=5, price_cents=90,
            order_type="BOGUS"))


def test_normalize_market_keeps_protocol_version():
    m = api._normalize_market(_gamma_raw(conditionId="0xver", version="v2"))
    assert m["version"] == "v2"
    assert api._meta["0xver"]["version"] == "v2"


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_place_limit_order_refuses_a_protocol_v2_market(monkeypatch, action):
    """A Protocol V2 market settles through ExchangeV3: another signing domain,
    other token ids, other approvals. An order signed the V1 way is wrong for
    it, so nothing is signed and nothing is sent."""
    cap = {}
    _stub_order_path(monkeypatch, cap)

    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222", "tick_size": 0.01,
                "min_size": 5, "neg_risk": False, "version": "v2"}

    def _no_sign(**_kw):
        raise AssertionError("signed an order for a Protocol V2 market")

    async def _no_allowance(_token):
        return True

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api.auth, "create_signed_order", _no_sign)
    monkeypatch.setattr(api, "refresh_token_allowance", _no_allowance)
    with pytest.raises(api.UnsupportedMarketVersionError) as ei:
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action=action, count=5, price_cents=50))
    assert "Protocol V2" in str(ei.value) and "No order was sent" in str(ei.value)
    assert not str(ei.value).startswith("HTTP")
    assert ei.value.status == 400
    assert "body" not in cap


@pytest.mark.parametrize("version", ["v1", None])
def test_place_limit_order_trades_v1_and_unversioned_markets(monkeypatch, version):
    cap = {}
    _stub_order_path(monkeypatch, cap)

    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222", "tick_size": 0.01,
                "min_size": 5, "neg_risk": False, "version": version}

    monkeypatch.setattr(api, "get_market_meta", _meta)
    asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="buy", count=5, price_cents=50))
    assert cap["body"]["orderType"] == "GTC"



def test_a_200_reply_with_success_false_is_a_failure_not_a_placement(monkeypatch):
    _stub_order_path(monkeypatch, {})

    async def _rejected(method, path, *, json_body=None, idempotent=True, **_kw):
        return {"success": False, "errorMsg": "not enough balance / allowance",
                "orderID": "", "status": ""}
    monkeypatch.setattr(api, "_authed_request", _rejected)
    with pytest.raises(api.PolymarketAPIError) as ei:
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=5, price_cents=40))
    assert "not enough balance" in str(ei.value)


def _window(monkeypatch, *, restart=0.0, post_only=0.0):
    now = time.time()
    monkeypatch.setattr(api, "_restart_until", now + restart if restart else 0.0)
    monkeypatch.setattr(api, "_post_only_until", now + post_only if post_only else 0.0)


def test_post_only_window_sends_a_resting_gtc_post_only(monkeypatch):
    cap = {}
    _stub_order_path(monkeypatch, cap)
    _window(monkeypatch, post_only=60)
    asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="buy", count=5, price_cents=40))
    assert cap["body"]["orderType"] == "GTC"
    assert cap["body"]["postOnly"] is True


@pytest.mark.parametrize("order_type", ["FAK", "FOK"])
def test_post_only_window_refuses_a_marketable_order_locally(monkeypatch, order_type):
    cap = {}
    _stub_order_path(monkeypatch, cap)
    _window(monkeypatch, post_only=60)
    with pytest.raises(api.PostOnlyModeError):
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=5, price_cents=40,
            order_type=order_type))
    assert "body" not in cap


@pytest.mark.parametrize("order_type", ["GTC", "FAK"])
def test_engine_restart_window_refuses_every_order_locally(monkeypatch, order_type):
    cap = {}
    _stub_order_path(monkeypatch, cap)
    _window(monkeypatch, restart=30, post_only=60)
    with pytest.raises(api.EngineRestartError):
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=5, price_cents=40,
            order_type=order_type))
    assert "body" not in cap


def test_a_buy_below_the_market_minimum_is_refused_not_inflated(monkeypatch):
    cap, signed = {}, []
    _stub_order_path(monkeypatch, cap)
    monkeypatch.setattr(api.auth, "create_signed_order",
                        lambda **kw: signed.append(kw) or {"salt": "1"})
    with pytest.raises(api.PolymarketAPIError) as ei:
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=4, price_cents=40))
    assert ei.value.status == 400 and "minimum of 5" in str(ei.value)
    assert signed == [] and "body" not in cap


def test_is_no_match_error():
    assert api._is_no_match_error("no orders found to match with FAK order")
    assert not api._is_no_match_error("not enough balance / allowance")
    assert not api._is_no_match_error("")


def test_is_maker_not_allowed_error():
    assert api._is_maker_not_allowed_error(
        {"error": "maker address not allowed, please use the deposit wallet flow"})
    assert not api._is_maker_not_allowed_error("Invalid Order payload")
    assert not api._is_maker_not_allowed_error("")


def test_place_limit_order_refuses_eoa_maker(monkeypatch):
    cap = {}
    _stub_order_path(monkeypatch, cap)
    monkeypatch.setattr(api.auth, "get_funder", lambda: "")

    with pytest.raises(api.PolymarketAPIError) as ei:
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=5, price_cents=90))
    assert ei.value.body == api._NO_DEPOSIT_WALLET_HELP
    assert "body" not in cap


def test_place_limit_order_refuses_funder_equal_to_signer(monkeypatch):
    cap = {}
    _stub_order_path(monkeypatch, cap)
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0xeoasigner")

    with pytest.raises(api.PolymarketAPIError):
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=5, price_cents=90))
    assert "body" not in cap


def test_fak_no_liquidity_is_clean_unmatched_not_error(monkeypatch):
    _stub_order_path(monkeypatch, {})

    async def _authed_400(method, path, *, json_body=None, idempotent=True, **_kw):
        raise api.PolymarketAPIError(400, "no orders found to match with FAK order")
    monkeypatch.setattr(api, "_authed_request", _authed_400)

    res = asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="buy", count=5, price_cents=90, order_type="FAK"))
    assert res["order"]["order_id"] == "" and res["order"]["status"] == "unmatched"

    with pytest.raises(api.PolymarketAPIError):
        asyncio.run(api.place_limit_order(
            ticker="T", side="yes", action="buy", count=5, price_cents=90, order_type="GTC"))



def _wire_signer_probe(monkeypatch, *, sig_type=3, place=None):
    """Stub the deps of verify_order_signer_live and start from an empty cache.
    `place` is the fake place_limit_order (callable or raises)."""
    monkeypatch.setattr(api, "_signer_check_cache", {})
    monkeypatch.setattr(api.auth, "get_signature_type", lambda: sig_type)
    monkeypatch.setattr(api.auth, "get_address", lambda: "0xEOAsigner")
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0xDepositWallet")

    async def _markets(limit=50):
        return [{"ticker": "0xc", "yes_token": "111", "status": "open",
                 "yes_bid_dollars": 0.55}], ""

    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222", "tick_size": 0.01, "min_size": 5}

    monkeypatch.setattr(api, "fetch_markets", _markets)
    monkeypatch.setattr(api, "get_market_meta", _meta)
    if place is not None:
        monkeypatch.setattr(api, "place_limit_order", place)


def test_signer_probe_raw_eoa_fails_offline(monkeypatch):
    calls = {"n": 0}

    async def _place(**k):
        calls["n"] += 1
        return {"order": {"order_id": "", "status": "unmatched"}}
    _wire_signer_probe(monkeypatch, sig_type=0, place=_place)
    out = asyncio.run(api.verify_order_signer_live())
    assert out["ok"] is False and out["tested"] is True
    assert out["reason"] == api._NO_DEPOSIT_WALLET_HELP
    assert calls["n"] == 0


def test_signer_probe_detects_mismatch_and_caches(monkeypatch):
    calls = {"n": 0}

    async def _place(**k):
        calls["n"] += 1
        raise api.PolymarketAPIError(400, api._SIGNER_MISMATCH_HELP)
    _wire_signer_probe(monkeypatch, sig_type=3, place=_place)
    out = asyncio.run(api.verify_order_signer_live())
    assert out["ok"] is False and out["tested"] is True
    assert out["reason"] == api._SIGNER_MISMATCH_HELP
    out2 = asyncio.run(api.verify_order_signer_live())
    assert out2["ok"] is False
    assert calls["n"] == 1


def test_signer_probe_passes_when_order_accepted(monkeypatch):
    captured = {}

    async def _place(**k):
        captured.update(k)
        return {"order": {"order_id": "", "status": "unmatched"}}
    _wire_signer_probe(monkeypatch, sig_type=3, place=_place)
    out = asyncio.run(api.verify_order_signer_live())
    assert out["ok"] is True and out["tested"] is True and out["reason"] == "signer verified"
    assert captured["price_cents"] == 1 and captured["order_type"] == "FAK"
    assert captured["action"] == "buy" and captured["count"] >= 110


def test_signer_probe_inconclusive_not_cached(monkeypatch):
    calls = {"n": 0}

    async def _place(**k):
        calls["n"] += 1
        raise api.PolymarketAPIError(400, "not enough balance / allowance")
    _wire_signer_probe(monkeypatch, sig_type=3, place=_place)
    out = asyncio.run(api.verify_order_signer_live())
    assert out["ok"] is True and out["tested"] is False
    asyncio.run(api.verify_order_signer_live())
    assert calls["n"] == 2


def test_unmatched_fak_reports_no_order_id(monkeypatch):
    cap = {}
    _stub_order_path(monkeypatch, cap)

    async def _authed_unmatched(method, path, *, json_body=None, idempotent=True, **_kw):
        return {"orderID": "oid-killed", "status": "unmatched", "success": True}
    monkeypatch.setattr(api, "_authed_request", _authed_unmatched)

    res = asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="buy", count=5, price_cents=90, order_type="FAK"))
    assert res["order"]["order_id"] == ""
    res2 = asyncio.run(api.place_limit_order(
        ticker="T", side="yes", action="buy", count=5, price_cents=90, order_type="GTC"))
    assert res2["order"]["order_id"] == "oid-killed"


def _wire_balance(monkeypatch, *, cash, value, delay=0.0):
    """Stub both legs of get_balance in deposit-wallet mode.

    `cash` is dollars (or an Exception to raise / None for "all RPCs dead");
    `value` is the raw /value payload (or an Exception to raise).
    """
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0x" + "1" * 40)

    async def _pusd(_addr):
        if delay:
            await asyncio.sleep(delay)
        if isinstance(cash, Exception):
            raise cash
        return cash
    monkeypatch.setattr(api, "_pusd_balance_usd", _pusd)

    async def _get(url, params=None, **_kw):
        if delay:
            await asyncio.sleep(delay)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(api, "_get", _get)


def test_get_balance_gathers_both_legs(monkeypatch):
    _wire_balance(monkeypatch, cash=12.34, value={"data": {"proxy_wallet": "0x1", "value": 7.5}}, delay=0.05)
    t0 = time.perf_counter()
    out = asyncio.run(api.get_balance())
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.09, f"legs ran sequentially ({elapsed*1000:.0f} ms)"
    assert out == {"balance": 1234, "portfolio_value": 750}


def test_get_balance_value_leg_failure_still_returns_cash(monkeypatch):
    _wire_balance(monkeypatch, cash=12.34, value=RuntimeError("data api down"))
    out = asyncio.run(api.get_balance())
    assert out == {"balance": 1234, "portfolio_value": None}


def test_get_balance_value_leg_bad_shape_is_unknown_not_zero(monkeypatch):
    _wire_balance(monkeypatch, cash=5.0, value={"unexpected": "dict"})
    assert asyncio.run(api.get_balance()) == {"balance": 500, "portfolio_value": None}


def test_get_balance_null_value_is_a_real_zero(monkeypatch):
    _wire_balance(monkeypatch, cash=5.0, value={"data": None})
    assert asyncio.run(api.get_balance()) == {"balance": 500, "portfolio_value": 0}


def test_get_balance_cash_leg_failure_raises(monkeypatch):
    _wire_balance(monkeypatch, cash=None, value={"data": {"value": 7.5}})
    with pytest.raises(RuntimeError, match="deposit-wallet balance read failed"):
        asyncio.run(api.get_balance())


def test_get_balance_raw_eoa_missing_balance_field_raises(monkeypatch):
    monkeypatch.setattr(api.auth, "get_funder", lambda: None)
    monkeypatch.setattr(api.auth, "get_address", lambda: "0x" + "2" * 40)

    async def _authed(method, path, **_kw):
        return {"allowance": "1"}
    monkeypatch.setattr(api, "_authed_request", _authed)

    async def _get(url, params=None, **_kw):
        return [{"value": 1.0}]
    monkeypatch.setattr(api, "_get", _get)

    with pytest.raises(RuntimeError, match="missing 'balance'"):
        asyncio.run(api.get_balance())


def test_get_balance_raw_eoa_scales_1e6_units_to_cents(monkeypatch):
    monkeypatch.setattr(api.auth, "get_funder", lambda: None)
    monkeypatch.setattr(api.auth, "get_address", lambda: "0x" + "2" * 40)

    async def _authed(method, path, **_kw):
        return {"balance": "25500000"}
    monkeypatch.setattr(api, "_authed_request", _authed)

    async def _get(url, params=None, **_kw):
        return {"data": None}
    monkeypatch.setattr(api, "_get", _get)

    assert asyncio.run(api.get_balance()) == {"balance": 2550, "portfolio_value": 0}
