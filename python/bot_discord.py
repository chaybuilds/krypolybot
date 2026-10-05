from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import bot_secrets
import remote
import terminal

logger = logging.getLogger("krypt.remote.discord")

API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"

_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)

_ACK_GRACE = 1.5

_task: Optional[asyncio.Task] = None
_stop = False
_last_error: Optional[str] = None
_self_id: Optional[str] = None
_connected = False
_dm_channels: dict[str, str] = {}


def _task_alive() -> bool:
    return _task is not None and not _task.done()


def is_running() -> bool:
    return _task_alive() and _connected


def last_error() -> Optional[str]:
    return _last_error


def has_token() -> bool:
    return bot_secrets.has_token("discord")


async def _client():
    import polymarket_api as api
    return await api._get_client()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "KryptPolyBot (https://krypt.cc, 2.0)"}


async def verify_token(token: str) -> dict:
    t = (token or "").strip()
    if not t:
        return {"ok": False, "message": "No token given."}
    try:
        client = await _client()
        r = await client.get(f"{API}/users/@me", headers=_headers(t), timeout=15.0)
    except Exception as e:
        return {"ok": False, "message": f"Could not reach Discord: {e}"}
    if r.status_code == 401:
        return {"ok": False, "message": "Discord rejected that token."}
    if r.status_code >= 400:
        return {"ok": False, "message": f"Discord returned {r.status_code}."}
    me = r.json() if r.content else {}
    return {"ok": True, "username": me.get("username"),
            "message": (
                f"Connected as {me.get('username')}. Make sure the MESSAGE "
                f"CONTENT intent is enabled for this bot in the Discord "
                f"developer portal, or it will receive empty messages.")}


async def _dm_channel(user_id: str) -> Optional[str]:
    if user_id in _dm_channels:
        return _dm_channels[user_id]
    token = bot_secrets.load_token("discord")
    if not token:
        return None
    client = await _client()
    r = await client.post(f"{API}/users/@me/channels", headers=_headers(token),
                          content=json.dumps({"recipient_id": str(user_id)}),
                          timeout=15.0)
    if r.status_code >= 400:
        raise RuntimeError(f"could not open a DM ({r.status_code})")
    cid = str((r.json() or {}).get("id") or "")
    if cid:
        _dm_channels[user_id] = cid
    return cid or None


async def send(user_id: str, text: str) -> None:
    token = bot_secrets.load_token("discord")
    if not token:
        return
    channel = await _dm_channel(str(user_id))
    if not channel:
        return
    client = await _client()
    body = text or ""
    while body:
        chunk, body = body[:1900], body[1900:]
        started = time.monotonic()
        r = await client.post(
            f"{API}/channels/{channel}/messages", headers=_headers(token),
            content=json.dumps({"content": chunk}), timeout=15.0)
        terminal.note_external_call(
            "discord", ok=r.status_code < 400,
            ms=(time.monotonic() - started) * 1000)


async def _send_to_channel(channel_id: str, text: str) -> None:
    token = bot_secrets.load_token("discord")
    if not token:
        return
    client = await _client()
    body = text or ""
    while body:
        chunk, body = body[:1900], body[1900:]
        started = time.monotonic()
        r = await client.post(
            f"{API}/channels/{channel_id}/messages", headers=_headers(token),
            content=json.dumps({"content": chunk}), timeout=15.0)
        terminal.note_external_call(
            "discord", ok=r.status_code < 400,
            ms=(time.monotonic() - started) * 1000)


async def _heartbeat(ws, interval_ms: int, state: dict) -> None:
    interval = max(0.001, interval_ms / 1000.0)
    try:
        while True:
            await asyncio.sleep(interval)
            silent = time.monotonic() - float(state.get("ackedAt") or 0.0)
            if silent > interval * _ACK_GRACE:
                raise RuntimeError(
                    f"gateway stopped ACKing heartbeats ({silent:.0f}s silent)")
            await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": state.get("seq")}))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        state["fatal"] = str(e)
        logger.info("discord heartbeat: %s", e)
        try:
            await ws.close()
        except Exception:
            pass


def _wanted() -> bool:
    try:
        import service
        from config import merge_with_defaults
        cfg = merge_with_defaults(dict(service.STATE.cfg or {}))
        return bool(cfg.get("remote_enabled")) and bool(
            cfg.get("remote_discord_enabled"))
    except Exception:
        return True


async def _run() -> None:
    global _last_error, _self_id, _connected
    import websockets
    import ws_ssl

    backoff = 1.0
    session_id: Optional[str] = None
    resume_url: Optional[str] = None
    state: dict = {"seq": None}

    while not _stop:
        token = bot_secrets.load_token("discord")
        if not token:
            _last_error = "No bot token saved."
            return
        url = resume_url or GATEWAY
        hb: Optional[asyncio.Task] = None
        try:
            async with websockets.connect(
                url, ping_interval=None, open_timeout=20, close_timeout=5,
                max_size=2 ** 20, ssl=ws_ssl.client_context(),
            ) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if hello.get("op") != _OP_HELLO:
                    raise RuntimeError("no HELLO from the gateway")
                interval = int((hello.get("d") or {}).get("heartbeat_interval") or 41250)
                state["ackedAt"] = time.monotonic()
                state.pop("fatal", None)
                hb = asyncio.ensure_future(_heartbeat(ws, interval, state))

                if session_id and resume_url:
                    await ws.send(json.dumps({
                        "op": _OP_RESUME,
                        "d": {"token": token, "session_id": session_id,
                              "seq": state.get("seq")},
                    }))
                else:
                    await ws.send(json.dumps({
                        "op": _OP_IDENTIFY,
                        "d": {"token": token, "intents": _INTENTS,
                              "properties": {"os": "windows", "browser": "krypt",
                                             "device": "krypt"}},
                    }))

                _last_error = None
                backoff = 1.0

                while not _stop:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    op = msg.get("op")
                    if msg.get("s") is not None:
                        state["seq"] = msg["s"]

                    if op == _OP_HEARTBEAT_ACK:
                        state["ackedAt"] = time.monotonic()
                        continue
                    if op == _OP_HEARTBEAT:
                        await ws.send(json.dumps(
                            {"op": _OP_HEARTBEAT, "d": state.get("seq")}))
                        continue
                    if op == _OP_RECONNECT:
                        break
                    if op == _OP_INVALID_SESSION:
                        session_id, resume_url = None, None
                        state["seq"] = None
                        await asyncio.sleep(2.0)
                        break
                    if op != _OP_DISPATCH:
                        continue

                    ev = msg.get("t")
                    d = msg.get("d") or {}
                    if ev == "READY":
                        _self_id = str((d.get("user") or {}).get("id") or "")
                        session_id = d.get("session_id")
                        ru = d.get("resume_gateway_url")
                        resume_url = f"{ru}/?v=10&encoding=json" if ru else None
                        _connected = True
                        logger.info("discord bot ready as %s",
                                    (d.get("user") or {}).get("username"))
                        continue
                    if ev == "RESUMED":
                        _connected = True
                        continue
                    if ev != "MESSAGE_CREATE":
                        continue

                    author = d.get("author") or {}
                    uid = str(author.get("id") or "")
                    if not uid or uid == _self_id or author.get("bot"):
                        continue
                    text = d.get("content") or ""
                    if not text:
                        continue
                    channel_id = str(d.get("channel_id") or "")
                    label = author.get("username") or ""
                    try:
                        reply = await remote.handle(
                            "discord", uid, text, label=label)
                        if reply and channel_id:
                            await _send_to_channel(channel_id, reply)
                    except Exception as e:
                        logger.debug("discord handler failed: %s", e, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _last_error = str(state.get("fatal") or e)[:200]
            logger.info("discord gateway error: %s", _last_error)
        finally:
            _connected = False
            if hb:
                hb.cancel()
                try:
                    await hb
                except (asyncio.CancelledError, Exception):
                    pass
        if _stop or not _wanted():
            break
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 2)


async def start() -> bool:
    global _task, _stop, _last_error
    if _task_alive():
        return True
    if not has_token():
        _last_error = "No bot token saved."
        return False
    try:
        import websockets
    except Exception:
        _last_error = "The websockets library is missing from this build."
        return False
    _stop = False
    _last_error = None
    _task = asyncio.ensure_future(_run())
    remote.register_sender("discord", send)
    return True


async def stop() -> None:
    global _task, _stop, _connected
    _stop = True
    _connected = False
    remote.unregister_sender("discord")
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
