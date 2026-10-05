from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

_out_lock = threading.Lock()
_state_lock = threading.Lock()
_session_id: Optional[str] = None
_init_params: Optional[dict] = None
_need_init = True

NOT_RUNNING = (
    "Krypt PolyBot is not reachable. Open the app, go to AI Agents and turn "
    "on 'Allow AI agents' — then try again. (The app must stay open while an "
    "agent uses it: the wallet and the order books live there.)")


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[krypt-polybot mcp] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _send(obj: Any) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _endpoint() -> Optional[tuple[str, str]]:
    try:
        import mcp_server
        info = json.loads(mcp_server.bridge_path().read_text(encoding="utf-8"))
        port = int(info["port"])
        token = mcp_server.load_token()
    except Exception:
        return None
    if not token:
        return None
    return f"http://127.0.0.1:{port}/mcp", token


class _Unreachable(Exception):
    pass


def _post(payload: Any, *, timeout: float = 900.0) -> tuple[Optional[Any], Optional[str]]:
    ep = _endpoint()
    if ep is None:
        raise _Unreachable(NOT_RUNNING)
    url, token = ep
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Host": url.split("/")[2],
    }
    with _state_lock:
        if _session_id:
            headers["Mcp-Session-Id"] = _session_id
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            sid = r.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise _Unreachable(
                "The app refused the token. Rotate it on the AI Agents page "
                "and restart this client.")
        if e.code == 503:
            raise _Unreachable(NOT_RUNNING)
        raise _Unreachable(f"The app answered HTTP {e.code}.")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        _log(f"bridge unreachable: {e}")
        raise _Unreachable(NOT_RUNNING)
    return (json.loads(body) if body else None), sid


def _upstream_init() -> None:
    global _session_id, _need_init
    with _state_lock:
        params = _init_params
    if params is None:
        return
    resp, sid = _post({"jsonrpc": "2.0", "id": "relay-init", "method": "initialize",
                       "params": params}, timeout=15)
    with _state_lock:
        _session_id = sid
        _need_init = False
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)


def _local_initialize(msg: dict) -> dict:
    import mcp_server
    asked = str((msg.get("params") or {}).get("protocolVersion") or "")
    version = asked if asked in mcp_server.PROTOCOL_VERSIONS else mcp_server.PROTOCOL_VERSIONS[0]
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
        "protocolVersion": version,
        "capabilities": mcp_server.CAPABILITIES,
        "serverInfo": {"name": mcp_server.SERVER_NAME, "title": "Krypt PolyBot",
                       "version": mcp_server._app_version()},
        "instructions": mcp_server.INSTRUCTIONS,
    }}


def _handle(msg: Any) -> None:
    global _init_params, _need_init
    if not isinstance(msg, dict):
        _send({"jsonrpc": "2.0", "id": None,
               "error": {"code": -32600, "message": "invalid request"}})
        return
    method = msg.get("method")
    rid = msg.get("id")
    is_request = "id" in msg and isinstance(method, str)

    if method == "initialize":
        with _state_lock:
            _init_params = dict(msg.get("params") or {})
            _need_init = True
        _send(_local_initialize(msg))
        try:
            _upstream_init()
        except _Unreachable:
            pass
        return

    if method == "notifications/initialized":
        return

    if method in ("tools/list", "ping", "prompts/list", "prompts/get"):
        import mcp_server
        import mcp_tools
        try:
            if method == "tools/list":
                result: dict = {"tools": mcp_tools.list_tools()}
            elif method == "prompts/list":
                result = {"prompts": mcp_server.list_prompts()}
            elif method == "prompts/get":
                p = msg.get("params") or {}
                result = mcp_server.get_prompt(str(p.get("name") or ""),
                                               p.get("arguments") or {})
            else:
                result = {}
        except (KeyError, ValueError) as e:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32602, "message": f"bad prompt request: {e}"}})
            return
        _send({"jsonrpc": "2.0", "id": rid, "result": result})
        return

    try:
        with _state_lock:
            need = _need_init
        if need:
            _upstream_init()
        resp, _ = _post(msg)
        if resp is not None and is_request:
            _send(resp)
    except _Unreachable as e:
        if not is_request:
            return
        if method == "tools/call":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": str(e)}], "isError": True}})
        else:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32603, "message": str(e)}})
        with _state_lock:
            _need_init = True


def main() -> int:
    _log("relay started")
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, list):
            for m in msg:
                threading.Thread(target=_handle, args=(m,), daemon=True).start()
            continue
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            _handle(msg)
            continue
        threading.Thread(target=_handle, args=(msg,), daemon=True).start()
