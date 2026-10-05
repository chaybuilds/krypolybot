from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("krypt.remote")

RATE_LIMIT_PER_MIN = 20
CONFIRM_TTL_SEC = 90.0
PAIR_CODE_TTL_SEC = 300.0
AUDIT_MAX = 500

_KV_PREFIX = "remote:"


def _kv_get(key: str, default: Any) -> Any:
    try:
        import db
        with db.get_db() as conn:
            v = db.kv_get(conn, _KV_PREFIX + key)
        return json.loads(v) if v else default
    except Exception:
        return default


def _kv_put(key: str, value: Any) -> None:
    import db
    with db.get_db() as conn:
        db.kv_set(conn, _KV_PREFIX + key, json.dumps(value))


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts or _now(), timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def paired(platform: str) -> list[dict]:
    rows = _kv_get(f"paired:{platform}", [])
    return rows if isinstance(rows, list) else []


def is_allowed(platform: str, account_id: str) -> bool:
    aid = str(account_id or "").strip()
    if not aid:
        return False
    return any(str(p.get("id")) == aid for p in paired(platform))


def add_paired(platform: str, account_id: str, label: str = "") -> list[dict]:
    aid = str(account_id or "").strip()
    if not aid:
        raise ValueError("no account id")
    rows = [p for p in paired(platform) if str(p.get("id")) != aid]
    rows.append({"id": aid, "label": label or None, "pairedAt": _iso()})
    _kv_put(f"paired:{platform}", rows)
    audit(platform, aid, "pair", "accepted", f"paired as {label or aid}")
    return rows


def remove_paired(platform: str, account_id: str) -> list[dict]:
    aid = str(account_id or "").strip()
    rows = [p for p in paired(platform) if str(p.get("id")) != aid]
    _kv_put(f"paired:{platform}", rows)
    audit(platform, aid, "unpair", "accepted", "removed from the allowlist")
    return rows


def new_pair_code(platform: str) -> dict:
    import secrets
    code = f"{secrets.randbelow(1_000_000):06d}"
    _kv_put(f"paircode:{platform}", {"code": code, "expires": _now() + PAIR_CODE_TTL_SEC})
    return {"code": code, "expiresAt": _iso(_now() + PAIR_CODE_TTL_SEC),
            "ttlSec": int(PAIR_CODE_TTL_SEC)}


def consume_pair_code(platform: str, code: str, account_id: str,
                      label: str = "") -> tuple[bool, str]:
    rec = _kv_get(f"paircode:{platform}", None)
    given = str(code or "").strip()
    if not isinstance(rec, dict) or not rec.get("code"):
        return False, "No pairing code is active. Generate one in the app first."
    if _now() > float(rec.get("expires") or 0):
        _kv_put(f"paircode:{platform}", None)
        return False, "That pairing code has expired. Generate a new one in the app."
    import hmac
    if not hmac.compare_digest(str(rec["code"]), given):
        return False, "That pairing code is not right."
    _kv_put(f"paircode:{platform}", None)
    add_paired(platform, account_id, label)
    return True, (
        "Paired. This account can now control Krypt PolyBot. "
        "Send /help to see what it can do.")


_hits: dict[str, list[float]] = {}


def rate_ok(sender: str) -> bool:
    now = _now()
    q = [t for t in _hits.get(sender, []) if now - t < 60.0]
    if len(q) >= RATE_LIMIT_PER_MIN:
        _hits[sender] = q
        return False
    q.append(now)
    _hits[sender] = q
    return True


def audit(platform: str, sender: str, command: str, outcome: str,
          detail: str = "") -> None:
    rows = _kv_get("audit", [])
    if not isinstance(rows, list):
        rows = []
    rows.insert(0, {
        "at": _iso(), "platform": platform, "sender": str(sender)[:64],
        "command": str(command)[:120], "outcome": outcome,
        "detail": str(detail)[:300],
    })
    del rows[AUDIT_MAX:]
    try:
        _kv_put("audit", rows)
    except Exception:
        pass
    if outcome != "accepted":
        logger.info("remote %s/%s %s -> %s (%s)",
                    platform, str(sender)[:24], command, outcome, detail[:120])


def audit_log(limit: int = 100) -> list[dict]:
    rows = _kv_get("audit", [])
    return rows[: max(1, min(int(limit or 100), AUDIT_MAX))] if isinstance(rows, list) else []


_pending: dict[str, dict] = {}


def _pending_for(sender: str) -> Optional[dict]:
    p = _pending.get(sender)
    if not p:
        return None
    if _now() > p["expires"]:
        _pending.pop(sender, None)
        return None
    return p


def _c(v: Any, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}{suffix}"
    return f"{v}{suffix}"


def _money(v: Any) -> str:
    return "—" if v is None else f"${float(v):,.2f}"


def _signed(v: Any) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f"{'-' if f < 0 else '+'}${abs(f):,.2f}"


HELP_READ = """Krypt PolyBot — commands

/status      engines, wallet, connection
/positions   open positions and P&L
/pnl         today and all-time
/balance     cash and portfolio value
/orders      resting orders
/price <id>  price a market by condition id
/log         recent remote commands

Safety:
/pause       stop every engine opening anything new
/resume      re-enable the engines you paused
/flatten     close EVERY open position (previews, then /confirm)
/cancelall   cancel every resting order

/whoami      show your id and what you are allowed to do
/help        this message"""

HELP_TRADE = """
Trading (only because you enabled it in the app):
/buy <id> <yes|no> <size> <price¢>
/sell <id> <yes|no> <size> <price¢>
/confirm     send the pending order
/cancel      discard the pending order

Every order is previewed first and needs /confirm within 90s."""


async def handle(platform: str, sender_id: str, text: str, *,
                 label: str = "") -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    parts = raw.split()
    cmd = parts[0].lower().lstrip("/")
    cmd = cmd.split("@")[0]
    args = parts[1:]
    sender = f"{platform}:{sender_id}"

    if not is_allowed(platform, sender_id):
        if cmd in ("pair", "start") and args:
            if not rate_ok(sender):
                audit(platform, sender_id, cmd, "rate-limited")
                return "Too many attempts. Wait a minute."
            ok, msg = consume_pair_code(platform, args[0], str(sender_id), label)
            audit(platform, sender_id, "pair",
                  "accepted" if ok else "refused", msg)
            return msg
        audit(platform, sender_id, raw[:60], "unauthorised",
              "sender is not on the allowlist")
        return (
            "This bot is not paired with you. Open Krypt PolyBot on your PC, "
            "go to Settings → Remote control, generate a pairing code, then "
            "send:  /pair <code>")

    if not rate_ok(sender):
        audit(platform, sender_id, cmd, "rate-limited")
        return "Slow down — too many commands in the last minute."

    try:
        reply = await _dispatch(platform, sender_id, cmd, args, raw)
        audit(platform, sender_id, raw[:60], "accepted")
        return reply
    except Exception as e:
        audit(platform, sender_id, raw[:60], "error", f"{type(e).__name__}: {e}")
        logger.debug("remote command failed: %s", e, exc_info=True)
        return f"That failed: {type(e).__name__}: {e}"


async def _cfg() -> dict:
    import service
    from config import merge_with_defaults
    return merge_with_defaults(dict(service.STATE.cfg or {}))


def trading_allowed(cfg: dict) -> bool:
    return bool(cfg.get("remote_enabled")) and bool(cfg.get("remote_trading_enabled"))


async def _dispatch(platform: str, sender_id: str, cmd: str,
                    args: list[str], raw: str) -> str:
    cfg = await _cfg()

    if cmd in ("help", "start", "commands"):
        return HELP_READ + (HELP_TRADE if trading_allowed(cfg) else "")

    if cmd == "whoami":
        return (
            f"Platform: {platform}\n"
            f"Your id: {sender_id}\n"
            f"Paired: yes\n"
            f"Can place orders: "
            f"{'yes' if trading_allowed(cfg) else 'no (enable it in the app)'}\n"
            f"Live trading: {'on' if cfg.get('enable_trading') else 'off'}\n"
            f"Paper mode: {'on' if cfg.get('terminal_paper_mode') else 'off'}")

    if cmd == "log":
        rows = audit_log(12)
        if not rows:
            return "No remote commands recorded yet."
        return "Recent remote commands:\n" + "\n".join(
            f"{r['at'][11:19]} {r['platform']}/{str(r['sender'])[:10]} "
            f"{r['command']} → {r['outcome']}" for r in rows)

    if cmd == "status":
        return await _status(cfg)
    if cmd in ("positions", "pos"):
        return await _positions()
    if cmd == "pnl":
        return await _pnl()
    if cmd in ("balance", "bal"):
        return await _balance()
    if cmd == "orders":
        return await _orders()
    if cmd == "price":
        if not args:
            return "Usage: /price <condition id>"
        return await _price(args[0])

    if cmd == "pause":
        return await _pause()
    if cmd == "resume":
        return await _resume()
    if cmd == "flatten":
        return await _flatten(platform, sender_id)
    if cmd in ("cancelall", "cancel_all"):
        return await _cancel_all()

    if cmd in ("buy", "sell"):
        if not trading_allowed(cfg):
            return (
                "Placing orders from chat is turned off. Enable it in the app "
                "under Settings → Remote control if you want it — it is off by "
                "default on purpose.")
        return await _stage_order(cfg, platform, sender_id, cmd, args)
    if cmd == "confirm":
        pend = _pending_for(f"{platform}:{sender_id}")
        if pend and pend.get("kind") == "flatten":
            return await _confirm_flatten(cfg, platform, sender_id)
        if not pend:
            return "Nothing pending (or it expired). Send the command again."
        if not trading_allowed(cfg):
            return "Remote trading is off."
        return await _confirm_order(cfg, platform, sender_id)
    if cmd == "cancel":
        gone = _pending.pop(f"{platform}:{sender_id}", None)
        if gone:
            return ("Pending flatten discarded — nothing was sold."
                    if gone.get("kind") == "flatten"
                    else "Pending order discarded.")
        return "Nothing pending."

    return f"Unknown command '{cmd}'. Send /help."


async def _status(cfg: dict) -> str:
    import service
    st = service.STATE
    engines = []
    for label, key in (("Main", "enable_trading"), ("Crypto", "crypto15m_enabled"),
                       ("Copy", "copy_enabled"), ("Scripts", "scripts_live_enabled")):
        engines.append(f"{label} {'on' if cfg.get(key) else 'off'}")
    bal = await _balance()
    return (
        f"Krypt PolyBot\n"
        f"Wallet: {'connected' if st.auth_ok else 'NOT connected'}\n"
        f"Engines: {' · '.join(engines)}\n"
        f"Paper mode: {'on' if cfg.get('terminal_paper_mode') else 'off'}\n"
        f"{bal}")


async def _balance() -> str:
    import polymarket_api as api
    try:
        b = await api.get_balance()
    except Exception as e:
        return f"Balance: unavailable ({type(e).__name__})"
    cash = b.get("balance")
    port = b.get("portfolio_value")
    return (f"Cash: {_money(cash / 100.0) if cash is not None else '—'} · "
            f"Portfolio: {_money(port / 100.0) if port is not None else '—'}")


async def _positions() -> str:
    import terminal_trade
    pf = await terminal_trade.portfolio()
    rows = pf.get("positions") or []
    if not rows:
        return "No open positions."
    out = [f"{len(rows)} position(s):"]
    for p in rows[:12]:
        title = (p.get("title") or p["conditionId"][:16])[:44]
        line = (f"{p['side'].upper()} {p['size']:g} · {title}\n"
                f"   mark {_c(p.get('markCents'))}¢ · "
                f"val {_money(p.get('valueUsd'))} · "
                f"P&L {_signed(p.get('unrealizedPnlUsd'))}")
        if p.get("unreconciled"):
            line += "  (no cost basis — excluded from totals)"
        out.append(line)
    if len(rows) > 12:
        out.append(f"…and {len(rows) - 12} more.")
    out.append(f"\nTotal value {_money(pf.get('totalValueUsd'))} · "
               f"unrealised {_signed(pf.get('totalUnrealizedPnlUsd'))}")
    if pf.get("unreconciledNote"):
        out.append(pf["unreconciledNote"])
    return "\n".join(out)


async def _pnl() -> str:
    import service
    snap = await service._build_account_snapshot()
    return (f"Session {_signed(snap.get('sessionPnlUsd'))}\n"
            f"Today   {_signed(snap.get('todayPnlUsd'))}\n"
            f"All time {_signed(snap.get('alltimePnlUsd'))}\n"
            f"Win rate {_c(snap.get('winRate'))}")


async def _orders() -> str:
    import terminal_trade
    r = await terminal_trade.resting_orders()
    rows = r.get("orders") or []
    if not rows:
        return r.get("note") or "No resting orders."
    out = [f"{len(rows)} resting order(s):"]
    for o in rows[:12]:
        out.append(
            f"{o['action']} {o['side'].upper()} {_c(o.get('size'))} @ "
            f"{_c(o.get('priceCents'))}¢ · {(o.get('title') or o['conditionId'][:16])[:38]}")
    return "\n".join(out)


async def _price(condition_id: str) -> str:
    import terminal
    m = await terminal.market(condition_id)
    if not m:
        return "No market with that condition id."
    return (f"{m['title'][:70]}\n"
            f"{_c(m.get('probability'))}¢  (bid {_c(m.get('bid'))} / "
            f"ask {_c(m.get('ask'))})\n"
            f"24h {_money(m.get('volume24hUsd'))} · closes {m.get('closeTime') or '—'}")


async def _set_cfg(patch: dict) -> None:
    import service
    from config import merge_with_defaults
    await service.emit_event("remote:configPatch", {"patch": patch})
    service.STATE.cfg = merge_with_defaults(
        {**(service.STATE.cfg or {}), **patch})


async def _pause() -> str:
    cfg = await _cfg()
    was = {k: bool(cfg.get(k)) for k in
           ("enable_trading", "crypto15m_enabled", "copy_enabled",
            "scripts_live_enabled", "perps_enabled")}
    _kv_put("paused_snapshot", was)
    await _set_cfg({
        "enableTrading": False, "crypto15mEnabled": False,
        "copyEnabled": False, "scriptsLiveEnabled": False,
        "perpsEnabled": False,
    })
    on = [k for k, v in was.items() if v]
    return ("Every engine paused. "
            + (f"Was on: {', '.join(on)}. /resume puts those back."
               if on else "Nothing was running."))


async def _resume() -> str:
    was = _kv_get("paused_snapshot", None)
    if not isinstance(was, dict) or not any(was.values()):
        return ("Nothing to resume — no engines were on when you paused. "
                "Turn them on in the app.")
    patch = {
        "enableTrading": bool(was.get("enable_trading")),
        "crypto15mEnabled": bool(was.get("crypto15m_enabled")),
        "copyEnabled": bool(was.get("copy_enabled")),
        "scriptsLiveEnabled": bool(was.get("scripts_live_enabled")),
    }
    if "perps_enabled" in was:
        patch["perpsEnabled"] = bool(was["perps_enabled"])
    await _set_cfg(patch)
    _kv_put("paused_snapshot", None)
    on = [k for k, v in was.items() if v]
    return f"Resumed: {', '.join(on)}."


async def _wallet_positions_left() -> Optional[int]:
    try:
        import terminal_trade
        pf = await terminal_trade.portfolio()
        return len(pf.get("positions") or [])
    except Exception:
        return None


async def _wallet_orders_left() -> Optional[int]:
    try:
        import terminal_trade
        r = await terminal_trade.resting_orders()
        return len(r.get("orders") or [])
    except Exception:
        return None


def _main_engine_open() -> Optional[int]:
    try:
        import db
        with db.get_db() as conn:
            return int(db.count_open_bot_positions(conn))
    except Exception:
        return None


async def _flatten(platform: str, sender_id: str) -> str:
    import terminal_trade
    main_open = _main_engine_open()
    pv = await terminal_trade.flatten_preview()

    if not pv.get("ok"):
        return (f"{pv.get('message') or 'Could not read your wallet.'} "
                f"Nothing was staged — try /flatten again in a moment.")

    sellable = int(pv.get("count") or 0)
    blocked = [p for p in (pv.get("positions") or []) if p.get("blocked")]
    if not sellable and not (main_open or 0):
        tail = ""
        if blocked:
            tail = (f" {len(blocked)} position(s) are held but cannot be sold "
                    f"right now — /positions shows them.")
        if main_open is None:
            tail += (" The main engine's own positions could not be read, so "
                     "check /status.")
        return "Nothing open to close." + tail

    lines = ["Flatten — this closes EVERYTHING, not just one engine:"]
    if pv.get("paper"):
        lines.append("PAPER MODE IS ON — no wallet order would leave this "
                     "machine. Turn it off in the app first.")
    lines.append(
        f"Main engine: {main_open} open position(s) will be closed."
        if main_open is not None else
        "Main engine: its open positions will be closed (count unreadable).")
    lines.append(
        f"Wallet: {sellable} position(s) sold at the best bid, "
        f"about {_money(pv.get('proceedsUsd'))} back.")
    if pv.get("engineTracked"):
        lines.append(f"({pv['engineTracked']} of those are engine-tracked; "
                     f"their reconcile books them as sold.)")
    for p in blocked[:5]:
        lines.append(f"! {str(p['conditionId'])[:12]}… {p['side'].upper()}: "
                     f"{p['blocked']}")
    if len(blocked) > 5:
        lines.append(f"! …and {len(blocked) - 5} more that cannot be sold.")
    lines.append(
        "Sold at MARKET, not at a limit — your remote order cap is an entry "
        "guard and does not apply to getting out. The proceeds figure is an "
        "estimate off the book as it is now.")
    lines.append(f"\nSend /confirm within {int(CONFIRM_TTL_SEC)}s, or /cancel.")

    _pending[f"{platform}:{sender_id}"] = {
        "kind": "flatten", "expires": _now() + CONFIRM_TTL_SEC,
    }
    return "\n".join(lines)


async def _confirm_flatten(cfg: dict, platform: str, sender_id: str) -> str:
    key = f"{platform}:{sender_id}"
    if not _pending_for(key):
        return "Nothing pending (or it expired). Send /flatten again."
    _pending.pop(key, None)

    import service
    import terminal_trade
    r = await service._h_flatten({})
    n = int(r.get("closed") or 0)
    fw = await terminal_trade.flatten_wallet(
        allow_trading=True,
        origin="manual", reason=f"remote /flatten ({platform})")

    lines = [f"Closed {n} main-engine position(s).",
             fw.get("message") or "The wallet flatten reported nothing."]
    for row in (fw.get("results") or []):
        if not row.get("ok"):
            lines.append(f"! {str(row['conditionId'])[:12]}… "
                         f"{str(row['side']).upper()}: {row.get('message')}")
    if len(lines) > 8:
        del lines[8:]
        lines.append("…see /positions for the rest.")

    left = await _wallet_positions_left()
    if left is None:
        lines.append("Could not re-read the wallet — check /positions.")
    elif left > 0:
        lines.append(f"{left} position(s) are still open — /positions says "
                     f"which, and why they could not be sold.")
    else:
        lines.append("Nothing else open in the wallet.")
    return "\n".join(lines)


async def _cancel_all() -> str:
    import service
    import terminal_trade
    r = await service._h_cancelAllOpen({})
    n = int(r.get("canceled") or 0)

    rest = await terminal_trade.resting_orders()
    rows = [o for o in (rest.get("orders") or []) if o.get("orderId")]
    term_ok = term_fail = 0
    for o in rows:
        res = await terminal_trade.cancel(str(o["orderId"]))
        if res.get("ok"):
            term_ok += 1
        else:
            term_fail += 1

    lines = [f"Canceled {n} main-engine resting order(s)."]
    if rows:
        lines.append(f"Canceled {term_ok} of {len(rows)} remaining resting "
                     f"order(s) (manual, standing, crypto, copy or script)."
                     + (f" {term_fail} refused." if term_fail else ""))
    elif rest.get("note") and "No resting orders" not in str(rest.get("note")):
        lines.append(f"The rest of your resting orders could not be read: "
                     f"{rest['note']}")

    left = await _wallet_orders_left()
    if left is None:
        lines.append("Could not re-read the wallet — check /orders.")
    elif left > 0:
        lines.append(f"{left} resting order(s) are still on the book — "
                     f"/orders says which.")
    else:
        lines.append("No resting orders left.")
    return "\n".join(lines)


async def _stage_order(cfg: dict, platform: str, sender_id: str,
                       action: str, args: list[str]) -> str:
    if len(args) < 4:
        return (f"Usage: /{action} <condition id> <yes|no> <size> <price¢>\n"
                f"e.g. /{action} 0xabc… yes 10 55")
    cid, side, size_s, price_s = args[0], args[1].lower(), args[2], args[3]
    if side not in ("yes", "no"):
        return "Side must be yes or no."
    try:
        size = float(size_s)
        price = float(price_s)
    except ValueError:
        return "Size and price must be numbers."

    order = {
        "conditionId": cid, "side": side, "action": action,
        "priceCents": price, "size": size, "orderType": "FAK",
        "paper": bool(cfg.get("terminal_paper_mode")),
    }
    import terminal_trade
    pv = await terminal_trade.preview(order)
    if pv["blockers"]:
        return "Refused: " + pv["blockers"][0]

    cost = pv.get("costUsd")
    cap = float(cfg.get("remote_max_order_usd") or 0.0)
    if cost is not None and cap > 0 and cost > cap:
        return (f"Refused: ${cost:,.2f} is over your remote order cap of "
                f"${cap:,.2f}. Raise it in the app if you meant it.")

    _pending[f"{platform}:{sender_id}"] = {
        "order": order, "costUsd": cost, "expires": _now() + CONFIRM_TTL_SEC,
    }
    lines = [
        f"{'PAPER ' if order['paper'] else ''}{action.upper()} "
        f"{pv.get('size') or size:g} {side.upper()} @ {pv.get('priceCents') or price:g}¢",
        f"Cost {_money(cost)} · fee {_money(pv.get('feeUsd'))}",
        f"Fillable now: {_c(pv.get('fillableSize'))} · "
        f"est. avg {_c(pv.get('estimatedAvgCents'))}¢",
    ]
    for w in (pv.get("warnings") or [])[:3]:
        lines.append(f"! {w}")
    lines.append(f"\nSend /confirm within {int(CONFIRM_TTL_SEC)}s, or /cancel.")
    return "\n".join(lines)


async def _confirm_order(cfg: dict, platform: str, sender_id: str) -> str:
    key = f"{platform}:{sender_id}"
    p = _pending_for(key)
    if not p:
        return "Nothing pending (or it expired). Send the order again."
    _pending.pop(key, None)

    import terminal_paper
    import terminal_trade
    order = p["order"]
    order["confirmUsd"] = p["costUsd"] or 0.0

    if order.get("paper"):
        res = await terminal_paper.place(order, cfg)
        return ("PAPER: " + res.get("message", "done")) if res.get("ok") \
            else ("Refused: " + res.get("message", "?"))

    res = await terminal_trade.place(order, origin="manual")
    return res.get("message", "done") if res.get("ok") \
        else ("Refused: " + res.get("message", "?"))


_senders: dict[str, Callable] = {}


def register_sender(platform: str, fn: Callable) -> None:
    _senders[platform] = fn


def unregister_sender(platform: str) -> None:
    _senders.pop(platform, None)


async def push(text: str) -> int:
    sent = 0
    for platform, fn in list(_senders.items()):
        for p in paired(platform):
            try:
                await fn(str(p["id"]), text)
                sent += 1
            except Exception as e:
                logger.debug("remote push to %s failed: %s", platform, e)
    return sent


def status() -> dict:
    import bot_discord
    import bot_telegram
    return {
        "discord": {
            "connected": bot_discord.is_running(),
            "lastError": bot_discord.last_error(),
            "paired": paired("discord"),
            "hasToken": bot_discord.has_token(),
        },
        "telegram": {
            "connected": bot_telegram.is_running(),
            "lastError": bot_telegram.last_error(),
            "paired": paired("telegram"),
            "hasToken": bot_telegram.has_token(),
            "botUsername": bot_telegram.bot_username(),
        },
        "audit": audit_log(25),
        "fetchedAt": _iso(),
    }
