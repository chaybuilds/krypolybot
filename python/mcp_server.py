from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("krypt.mcp")

SERVER_NAME = "krypt-polybot"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_HEADER_LINES = 100
IDLE_TIMEOUT_SEC = 120.0
AUDIT_MAX = 500
_KV_PREFIX = "mcp:"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CAPABILITIES = {"tools": {"listChanged": False}, "prompts": {"listChanged": False}}

INSTRUCTIONS = """\
You are connected to Krypt PolyBot, a local Polymarket trading app, running
on the user's own machine with the user's own wallet.

Start with get_status: it says what you are allowed to do right now (paper
only, or live with caps), what is left of today's live budget, and whether the
wallet is connected.

Conventions:
- A market is identified by its conditionId (0x…). Find one with
  search_markets or discover_markets.
- Prices are CENTS from 1 to 99 (a 0.42 probability is 42). Sizes are
  contracts (shares); each pays 100¢ if its side wins and 0¢ otherwise.
- side is "yes" or "no" (the first or second outcome; for Up/Down markets,
  yes = Up). action is "buy" or "sell".
- mode is "paper" or "live" on every trading tool and is never defaulted.
  Paper fills are walked out of the REAL order book, with real fees, into
  your own paper account (separate from the user's).
- A null value means nobody could produce it. It is not zero, so do not
  treat it as zero.
- Always preview_order before place_order. Fees, slippage and minimum size are
  real, and preview tells you what the book can actually fill.

Live buys are capped per order and per day by the user. A refusal explains
itself; do not try to split an order to get under a cap. Paper trade a
strategy and backtest it before asking the user to let you trade it live.
"""


PROMPTS: dict[str, dict] = {
    "analyze_market": {
        "title": "Analyze a market",
        "description": "Research one market end to end and say whether there is a trade.",
        "arguments": [{"name": "market", "description": "A conditionId, or words to search for.",
                       "required": True}],
        "text": (
            "Analyze this Polymarket market: {market}\n\n"
            "1. If it is not a conditionId, find it with search_markets.\n"
            "2. Read get_market (the rules text matters most), get_resolution_risk, "
            "get_order_book with include_micro, get_price_history (1w and 1d), "
            "get_trade_tape, get_related_markets and get_cross_venue.\n"
            "3. Give your own probability for each side with the reasoning, the "
            "evidence that would change it, and how confident you are.\n"
            "4. Compare it with the price you could actually TRADE at (the ask to "
            "buy, the bid to sell, not the mid), after fees from preview_order. "
            "Only call it a trade if the edge survives fees and spread.\n"
            "5. If it is a trade, preview_order in paper mode and show the ticket. "
            "Do not place anything unless I say so."),
    },
    "paper_trading_session": {
        "title": "Paper trading session",
        "description": "Scan markets, place paper trades with stops, and report.",
        "arguments": [
            {"name": "focus", "description": "Category or theme, e.g. politics, crypto, sports.",
             "required": False},
            {"name": "budget_usd", "description": "Paper dollars to deploy (default 100).",
             "required": False}],
        "text": (
            "Run a paper trading session{focus_clause}. Budget: {budget_usd} USD of "
            "your paper account.\n\n"
            "- get_status first; work only in mode=paper.\n"
            "- Use discover_markets (volume, closing, new, volatility) and "
            "search_markets to shortlist 5-10 candidates; research each the way "
            "analyze_market would.\n"
            "- Take only trades where your edge survives the spread and fees. Size "
            "each so no single market is more than a quarter of the budget.\n"
            "- For every position, add a paper stop-loss with "
            "manage_standing_instructions (mode=paper).\n"
            "- Finish with get_portfolio (mode=paper): a table of what you hold, "
            "why, your probability vs the price paid, and what would make you exit."),
    },
    "review_paper_performance": {
        "title": "Review paper performance",
        "description": "Grade the agent's paper record honestly.",
        "arguments": [],
        "text": (
            "Review your paper trading record. Use get_portfolio and get_orders "
            "(mode=paper). Report realised and unrealised P&L, return on bankroll, "
            "win rate on settled positions, average entry vs settlement, and fees "
            "paid. Say plainly whether there is evidence of an edge yet or whether "
            "the sample is too small to tell, and what you would change."),
    },
    "build_crypto_strategy": {
        "title": "Build a crypto strategy",
        "description": "Research the recorded corpus, write a script, backtest it.",
        "arguments": [{"name": "idea", "description": "A hypothesis to test (optional).",
                       "required": False}],
        "text": (
            "Build and test a strategy for the crypto Up/Down markets{idea_clause}.\n\n"
            "1. research_data with no sql for the inventory and schema, then SQL "
            "queries to test the hypothesis on the recorded ticks.\n"
            "2. get_script_docs, then write a script; validate_script until clean.\n"
            "3. backtest_script over the full corpus AND a recent slice. Report n, "
            "win rate, net EV per contract after fees and the t-stat; distrust "
            "anything with a small n or a result that only holds in one slice.\n"
            "4. Only if it holds up: save_script and enable it in SHADOW mode. Do "
            "not arm it live; that is my call."),
    },
}


def list_prompts() -> list[dict]:
    return [{"name": k, "title": v["title"], "description": v["description"],
             "arguments": v["arguments"]} for k, v in PROMPTS.items()]


def get_prompt(name: str, args: dict) -> dict:
    p = PROMPTS.get(name)
    if p is None:
        raise KeyError(name)
    for a in p["arguments"]:
        if a.get("required") and not str(args.get(a["name"]) or "").strip():
            raise ValueError(f"missing argument: {a['name']}")
    focus = str(args.get("focus") or "").strip()
    idea = str(args.get("idea") or "").strip()
    text = p["text"].format(
        market=str(args.get("market") or "").strip(),
        budget_usd=str(args.get("budget_usd") or "100").strip(),
        focus_clause=f" focused on {focus}" if focus else "",
        idea_clause=f" around this idea: {idea}" if idea else "",
    )
    return {"description": p["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def _cred_dir() -> Path:
    import polymarket_auth as auth
    return auth._credentials_dir()


def _token_path() -> Path:
    return _cred_dir() / "mcp.token"


def bridge_path() -> Path:
    return _cred_dir().parent / "mcp" / "bridge.json"


def load_token() -> Optional[str]:
    import polymarket_auth as auth
    p = _token_path()
    if not p.exists():
        return None
    try:
        return auth._read_secret_bytes(p).decode("utf-8").strip() or None
    except Exception as e:
        logger.warning("mcp: could not read token: %s", e)
        return None


def _new_token() -> str:
    return "kp_" + secrets.token_urlsafe(32)


def ensure_token() -> str:
    tok = load_token()
    if tok:
        return tok
    import polymarket_auth as auth
    tok = _new_token()
    auth._write_secret_bytes(_token_path(), tok.encode("utf-8"))
    return tok


def rotate_token() -> str:
    import polymarket_auth as auth
    tok = _new_token()
    auth._write_secret_bytes(_token_path(), tok.encode("utf-8"))
    _sessions.clear()
    audit("app", "rotate_token", "accepted", "token rotated; old clients disconnected")
    return tok


def masked_token() -> Optional[str]:
    tok = load_token()
    if not tok:
        return None
    return tok[:6] + "…" + tok[-4:]


def _iso(ts: Optional[float] = None) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def audit(client: str, tool: str, outcome: str, detail: str = "") -> None:
    try:
        import db
        with db.get_db() as conn:
            raw = db.kv_get(conn, _KV_PREFIX + "audit")
            rows = json.loads(raw) if raw else []
            if not isinstance(rows, list):
                rows = []
            rows.insert(0, {"at": _iso(), "client": client, "tool": tool,
                            "outcome": outcome, "detail": detail[:300]})
            db.kv_set(conn, _KV_PREFIX + "audit", json.dumps(rows[:AUDIT_MAX]))
    except Exception as e:
        logger.debug("mcp audit write failed: %s", e)


def audit_log(limit: int = 100) -> list[dict]:
    try:
        import db
        with db.get_db() as conn:
            raw = db.kv_get(conn, _KV_PREFIX + "audit")
        rows = json.loads(raw) if raw else []
        return rows[:max(1, int(limit))] if isinstance(rows, list) else []
    except Exception:
        return []


_sessions: dict[str, dict] = {}
_SESSION_MAX = 50


def _session_for(sid: Optional[str]) -> dict:
    s = _sessions.get(sid or "")
    if s is None:
        s = {"id": sid or "", "client": "unknown client", "version": None,
             "protocol": None, "connectedAt": _iso(), "lastSeen": _iso(),
             "reads": 0, "writes": 0, "refused": 0}
        if sid:
            _sessions[sid] = s
    s["lastSeen"] = _iso()
    return s


def _new_session(client_info: dict, protocol: str) -> str:
    sid = secrets.token_hex(16)
    name = str((client_info or {}).get("title") or (client_info or {}).get("name")
               or "unknown client")[:60]
    _sessions[sid] = {
        "id": sid, "client": name,
        "version": str((client_info or {}).get("version") or "")[:30] or None,
        "protocol": protocol, "connectedAt": _iso(), "lastSeen": _iso(),
        "reads": 0, "writes": 0, "refused": 0,
    }
    if len(_sessions) > _SESSION_MAX:
        oldest = sorted(_sessions.values(), key=lambda s: s["lastSeen"])
        for s in oldest[: len(_sessions) - _SESSION_MAX]:
            _sessions.pop(s["id"], None)
    return sid


def _app_version() -> str:
    v = os.environ.get("KRYPT_APP_VERSION")
    if v:
        return v
    try:
        return str(json.loads(bridge_path().read_text(encoding="utf-8")).get("version") or "dev")
    except Exception:
        return "dev"


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _ok(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


async def handle_message(msg: Any, session_id: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid request"), None
    method = msg.get("method")
    rid = msg.get("id")
    is_notification = "id" not in msg
    if not isinstance(method, str):
        return None, None
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return (None if is_notification else _err(rid, -32602, "params must be an object")), None

    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        sid = _new_session(params.get("clientInfo") or {}, version)
        logger.info("mcp: %s connected (protocol %s)",
                    _sessions[sid]["client"], version)
        return _ok(rid, {
            "protocolVersion": version,
            "capabilities": CAPABILITIES,
            "serverInfo": {"name": SERVER_NAME, "title": "Krypt PolyBot",
                           "version": _app_version()},
            "instructions": INSTRUCTIONS,
        }), sid

    if is_notification:
        return None, None

    if method == "ping":
        return _ok(rid, {}), None
    if method == "tools/list":
        import mcp_tools
        return _ok(rid, {"tools": mcp_tools.list_tools()}), None
    if method == "tools/call":
        import mcp_tools
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _err(rid, -32602, "tools/call needs a name and an arguments object"), None
        if not mcp_tools.has_tool(name):
            return _err(rid, -32602, f"unknown tool: {name}"), None
        session = _session_for(session_id)
        return _ok(rid, await mcp_tools.call(name, args, session)), None
    if method == "resources/list":
        return _ok(rid, {"resources": []}), None
    if method == "resources/templates/list":
        return _ok(rid, {"resourceTemplates": []}), None
    if method == "prompts/list":
        return _ok(rid, {"prompts": list_prompts()}), None
    if method == "prompts/get":
        try:
            return _ok(rid, get_prompt(str(params.get("name") or ""),
                                       params.get("arguments") or {})), None
        except KeyError:
            return _err(rid, -32602, f"unknown prompt: {params.get('name')}"), None
        except ValueError as e:
            return _err(rid, -32602, str(e)), None
    return _err(rid, -32601, f"method not found: {method}"), None


async def handle_body(body: Any, session_id: Optional[str]) -> tuple[Optional[Any], Optional[str]]:
    if isinstance(body, list):
        if not body:
            return _err(None, -32600, "empty batch"), None
        out, new_sid = [], None
        for m in body:
            r, sid = await handle_message(m, session_id)
            new_sid = new_sid or sid
            if r is not None:
                out.append(r)
        return (out or None), new_sid
    return await handle_message(body, session_id)


_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            413: "Payload Too Large", 503: "Service Unavailable"}


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def _read_request(reader: asyncio.StreamReader) -> Optional[dict]:
    try:
        line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_SEC)
    except (asyncio.TimeoutError, ConnectionError):
        return None
    if not line:
        return None
    try:
        method, target, _ver = line.decode("latin-1").strip().split(" ", 2)
    except ValueError:
        raise _HttpError(400, "bad request line")
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADER_LINES + 1):
        h = await asyncio.wait_for(reader.readline(), 30)
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    else:
        raise _HttpError(400, "too many headers")

    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size_line = await asyncio.wait_for(reader.readline(), 30)
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                break
            if len(body) + size > MAX_BODY_BYTES:
                raise _HttpError(413, "body too large")
            body += await asyncio.wait_for(reader.readexactly(size), 30)
            await reader.readline()
    else:
        n = int(headers.get("content-length") or 0)
        if n > MAX_BODY_BYTES:
            raise _HttpError(413, "body too large")
        if n:
            body = await asyncio.wait_for(reader.readexactly(n), 30)
    return {"method": method.upper(), "path": target.split("?", 1)[0],
            "headers": headers, "body": body}


def _host_ok(host: str) -> bool:
    h = host.strip().lower()
    if h.startswith("["):
        name = h[: h.find("]") + 1]
    else:
        name = h.split(":", 1)[0]
    return name in _LOOPBACK_HOSTS


def _authorized(headers: dict) -> bool:
    tok = load_token()
    got = headers.get("authorization", "")
    if not tok or not got.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(got[7:].strip().encode(), tok.encode())


async def _route(req: dict) -> tuple[int, dict, bytes]:
    h = req["headers"]
    if not _host_ok(h.get("host", "")):
        return 403, {}, b"forbidden host"
    if h.get("origin"):
        return 403, {}, b"browser requests are not accepted"
    if req["path"].rstrip("/") != "/mcp":
        return 404, {}, b"not found"
    if not _authorized(h):
        return 401, {"WWW-Authenticate": 'Bearer realm="krypt-polybot"'}, b"unauthorized"
    if not _running_wanted():
        return 503, {}, b"AI agent access is turned off in Krypt PolyBot"

    if req["method"] == "DELETE":
        _sessions.pop(h.get("mcp-session-id", ""), None)
        return 200, {}, b""
    if req["method"] != "POST":
        return 405, {"Allow": "POST, DELETE"}, b"method not allowed"

    try:
        body = json.loads(req["body"].decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        out = _err(None, -32700, "parse error")
        return 200, {"Content-Type": "application/json"}, json.dumps(out).encode()

    resp, new_sid = await handle_body(body, h.get("mcp-session-id"))
    extra = {"Mcp-Session-Id": new_sid} if new_sid else {}
    if resp is None:
        return 202, extra, b""
    data = json.dumps(resp, default=str, ensure_ascii=False).encode("utf-8")
    return 200, {"Content-Type": "application/json", **extra}, data


async def _serve_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            try:
                req = await _read_request(reader)
            except _HttpError as e:
                await _write(writer, e.status, {}, str(e).encode(), close=True)
                return
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
                return
            if req is None:
                return
            try:
                status, headers, body = await _route(req)
            except Exception as e:
                logger.warning("mcp: request failed: %s", e, exc_info=True)
                status, headers, body = 200, {"Content-Type": "application/json"}, json.dumps(
                    _err(None, -32603, f"internal error: {type(e).__name__}")).encode()
            close = req["headers"].get("connection", "").lower() == "close"
            await _write(writer, status, headers, body, close=close)
            if close:
                return
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _write(writer: asyncio.StreamWriter, status: int, headers: dict,
                 body: bytes, *, close: bool) -> None:
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}"]
    hdrs = {"Content-Length": str(len(body)), "Cache-Control": "no-store",
            "Connection": "close" if close else "keep-alive", **headers}
    hdrs.setdefault("Content-Type", "text/plain; charset=utf-8")
    lines += [f"{k}: {v}" for k, v in hdrs.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()


_server: Optional[asyncio.base_events.Server] = None
_port: Optional[int] = None
_last_error: Optional[str] = None
_want = False


def _running_wanted() -> bool:
    return _want


def is_running() -> bool:
    return _server is not None


def _write_bridge(port: int) -> None:
    p = bridge_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "pid": os.getpid(),
                               "version": os.environ.get("KRYPT_APP_VERSION") or "dev",
                               "startedAt": _iso()}), encoding="utf-8")
    os.replace(tmp, p)


def _clear_bridge() -> None:
    try:
        p = bridge_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            if int(data.get("pid") or 0) == os.getpid():
                p.unlink()
    except Exception:
        pass


async def start(port: int) -> None:
    global _server, _port, _last_error
    if _server is not None and _port == port:
        return
    await stop()
    ensure_token()
    try:
        _server = await asyncio.start_server(_serve_conn, host="127.0.0.1", port=port)
    except OSError as e:
        _server = None
        _last_error = (f"Could not listen on 127.0.0.1:{port} — {e.strerror or e}. "
                       f"Another program is using that port; pick a different "
                       f"one on the AI Agents page.")
        logger.warning("mcp: %s", _last_error)
        return
    _port = port
    _last_error = None
    _write_bridge(port)
    logger.info("mcp: listening on http://127.0.0.1:%d/mcp", port)


async def stop() -> None:
    global _server, _port
    srv, _server = _server, None
    if srv is not None:
        srv.close()
        try:
            await asyncio.wait_for(srv.wait_closed(), 2.0)
        except Exception:
            pass
        logger.info("mcp: stopped")
    _port = None
    _sessions.clear()
    _clear_bridge()


async def sync(cfg: dict) -> dict:
    global _want
    _want = bool(cfg.get("mcp_enabled"))
    if _want:
        await start(int(cfg.get("mcp_port") or 47615))
    else:
        await stop()
    return status(cfg)


def status(cfg: Optional[dict] = None) -> dict:
    import mcp_tools
    cfg = cfg or {}
    return {
        "enabled": bool(cfg.get("mcp_enabled")),
        "running": is_running(),
        "port": _port or int(cfg.get("mcp_port") or 47615),
        "url": f"http://127.0.0.1:{_port or int(cfg.get('mcp_port') or 47615)}/mcp",
        "lastError": _last_error,
        "hasToken": load_token() is not None,
        "tokenMasked": masked_token(),
        "sessions": sorted(_sessions.values(), key=lambda s: s["lastSeen"], reverse=True),
        "toolCount": len(mcp_tools.list_tools()),
        "spentTodayUsd": mcp_tools.spent_today_usd(),
        "audit": audit_log(50),
        "fetchedAt": _iso(),
    }
