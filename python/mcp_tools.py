from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("krypt.mcp.tools")

_live_lock = asyncio.Lock()


class Refused(Exception):
    pass


def _cfg() -> dict:
    import service
    from config import merge_with_defaults
    return merge_with_defaults(dict(service.STATE.cfg or {}))


def _require(cfg: dict, gate: str) -> None:
    if gate == "live" and not cfg.get("mcp_live_trading_enabled"):
        raise Refused(
            "Live trading by AI agents is OFF. The user can turn it on in "
            "Krypt PolyBot → AI Agents → 'Allow live trading'. Until then, use "
            "mode='paper' — paper fills come out of the real order book.")
    if gate == "scripts" and not cfg.get("mcp_scripts_enabled"):
        raise Refused(
            "Writing and running strategy scripts is OFF for AI agents. The "
            "user can turn it on in Krypt PolyBot → AI Agents → 'Allow "
            "scripts'. Reading the script API docs and the built-in backtests "
            "work without it.")


def _require_live_order(cfg: dict) -> None:
    import service
    _require(cfg, "live")
    if cfg.get("terminal_paper_mode"):
        raise Refused(
            "The app is in PAPER MODE, so no real order can be sent from "
            "anywhere — including an agent. Use mode='paper', or ask the user "
            "to turn paper mode off in Settings.")
    if not service.STATE.auth_ok:
        raise Refused("The wallet is not connected, so no order can be signed.")


def _mode(args: dict) -> str:
    m = str(args.get("mode") or "").lower()
    if m not in ("paper", "live"):
        raise Refused("mode is required and must be 'paper' or 'live'.")
    return m


def _cid(args: dict) -> str:
    cid = str(args.get("conditionId") or "").strip()
    if not cid:
        raise Refused("conditionId is required (find one with search_markets).")
    return cid


def _strip_private(v: Any, depth: int = 0) -> Any:
    if depth > 12:
        return v
    if isinstance(v, dict):
        return {k: _strip_private(x, depth + 1) for k, x in v.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(v, list):
        return [_strip_private(x, depth + 1) for x in v]
    return v


def spent_today_usd() -> float:
    try:
        import db
        import terminal_trade
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(sent_size, requested_size) * "
                "COALESCE(sent_price_cents, requested_price_cents) / 100.0), 0) "
                "FROM terminal_orders WHERE origin='ai' AND action='buy' "
                "AND status != 'unmatched' AND created_at >= date('now') "
                "AND network=?", (terminal_trade._net(),)).fetchone()
        return round(float(row[0] or 0.0), 2)
    except Exception as e:
        logger.warning("mcp: could not read today's AI spend: %s", e)
        return float("inf")


def _order_from(args: dict, mode: str) -> dict:
    order = {
        "conditionId": _cid(args),
        "side": "no" if str(args.get("side") or "").lower() == "no" else "yes",
        "action": "sell" if str(args.get("action") or "").lower() == "sell" else "buy",
        "priceCents": args.get("priceCents"),
        "size": args.get("size"),
        "orderType": str(args.get("orderType") or "GTC").upper(),
        "paper": mode == "paper",
    }
    if str(args.get("side") or "").lower() not in ("yes", "no"):
        raise Refused("side must be 'yes' or 'no'.")
    if str(args.get("action") or "").lower() not in ("buy", "sell"):
        raise Refused("action must be 'buy' or 'sell'.")
    if order["orderType"] not in ("GTC", "FAK", "FOK"):
        raise Refused("orderType must be GTC, FAK or FOK.")
    return order


async def _set_cfg(patch: dict) -> None:
    import service
    from config import merge_with_defaults
    await service.emit_event("mcp:configPatch", {"patch": patch})
    service.STATE.cfg = merge_with_defaults({**(service.STATE.cfg or {}), **patch})


def _push(text: str) -> None:
    try:
        import remote
        t = asyncio.get_running_loop().create_task(remote.push(text))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    except Exception:
        pass


_bg: set = set()


@dataclass
class Tool:
    name: str
    title: str
    description: str
    props: dict
    required: tuple
    fn: Callable[[dict], Awaitable[Any]]
    kind: str
    read_only: bool = False
    destructive: bool = False


_TOOLS: dict[str, Tool] = {}


def tool(name: str, title: str, description: str, *, props: Optional[dict] = None,
         required: tuple = (), kind: str = "read", read_only: bool = False,
         destructive: bool = False):
    def deco(fn):
        _TOOLS[name] = Tool(name, title, description, props or {}, required, fn,
                            kind, read_only, destructive)
        return fn
    return deco


def has_tool(name: str) -> bool:
    return name in _TOOLS


def list_tools() -> list[dict]:
    out = []
    for t in _TOOLS.values():
        out.append({
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "inputSchema": {"type": "object", "properties": t.props,
                            "required": list(t.required),
                            "additionalProperties": False},
            "annotations": {
                "title": t.title,
                "readOnlyHint": t.read_only,
                "destructiveHint": t.destructive,
                "idempotentHint": t.read_only,
                "openWorldHint": True,
            },
        })
    return out


def _summ(args: dict) -> str:
    keep = ("mode", "conditionId", "side", "action", "size", "priceCents",
            "orderType", "id", "enabled", "live", "engines", "kind", "confirm",
            "markets", "days")
    bits = []
    for k in keep:
        if k in args:
            v = args[k]
            if k == "conditionId":
                v = str(v)[:12] + "…"
            bits.append(f"{k}={json.dumps(v, default=str)[:60]}")
    return " ".join(bits)


async def call(name: str, args: dict, session: dict) -> dict:
    import mcp_server
    t = _TOOLS[name]
    client = session.get("client") or "unknown client"
    unknown = set(args) - set(t.props)
    try:
        if unknown:
            raise Refused(f"unknown argument(s): {', '.join(sorted(unknown))}")
        missing = [r for r in t.required if args.get(r) in (None, "")]
        if missing:
            raise Refused(f"missing required argument(s): {', '.join(missing)}")
        result = await t.fn(args)
    except Refused as e:
        session["refused"] = session.get("refused", 0) + 1
        mcp_server.audit(client, name, "refused", f"{e} [{_summ(args)}]")
        return {"content": [{"type": "text", "text": f"Refused: {e}"}], "isError": True}
    except Exception as e:
        logger.debug("mcp tool %s failed", name, exc_info=True)
        if not t.read_only:
            mcp_server.audit(client, name, "error", f"{type(e).__name__}: {e} [{_summ(args)}]")
        return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True}

    failed = isinstance(result, dict) and result.get("ok") is False
    if t.read_only:
        session["reads"] = session.get("reads", 0) + 1
    else:
        session["writes"] = session.get("writes", 0) + 1
        msg = result.get("message") if isinstance(result, dict) else None
        mcp_server.audit(client, name, "failed" if failed else "accepted",
                         f"{_summ(args)}{(' — ' + str(msg)) if msg else ''}")
    text = json.dumps(_strip_private(result), default=str, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": bool(failed)}


_CID = {"type": "string", "description": "The market's conditionId (0x…)."}
_SIDE = {"type": "string", "enum": ["yes", "no"],
         "description": "yes = first outcome (Up for Up/Down markets), no = second."}
_MODE = {"type": "string", "enum": ["paper", "live"],
         "description": "paper = your own simulated account on the real book; "
                        "live = real money (needs the user's permission)."}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 500}
_CONFIG = {"type": "object", "description":
           "Config keys to override for this run only (snake_case, as "
           "returned by get_config). Never saved."}
_SINCE = {"type": "integer", "minimum": 1, "maximum": 365,
          "description": "Days of recorded data to replay (default 60)."}


@tool("get_status", "Status and permissions",
      "What you may do right now (paper only, or live and with what caps), "
      "today's remaining live budget, wallet connection, which engines are "
      "running, and global paper/offline mode. Call this first. "
      "include_config=true adds the full engine configuration (snake_case "
      "keys) — what update_config changes and backtests override.",
      props={"include_config": {"type": "boolean"}}, read_only=True)
async def _get_status(a: dict) -> dict:
    import service
    cfg = _cfg()
    spent = spent_today_usd()
    daily = float(cfg.get("mcp_daily_limit_usd") or 0)
    live = bool(cfg.get("mcp_live_trading_enabled"))
    return {
        "app": {"version": __import__("mcp_server")._app_version()},
        "now": __import__("mcp_server")._iso(),
        "wallet": {"connected": bool(service.STATE.auth_ok)},
        "permissions": {
            "read": True, "paperTrading": True, "backtests": True,
            "liveTrading": live and not cfg.get("terminal_paper_mode"),
            "liveTradingSwitch": live,
            "scripts": bool(cfg.get("mcp_scripts_enabled")),
            "pauseEngines": True,
        },
        "liveLimits": {
            "maxOrderUsd": cfg.get("mcp_max_order_usd") or None,
            "dailyLimitUsd": daily or None,
            "spentTodayUsd": None if spent == float("inf") else spent,
            "remainingTodayUsd": (None if not daily else
                                  max(0.0, round(daily - spent, 2))
                                  if spent != float("inf") else 0.0),
            "note": ("Caps apply to BUYS only and are counted at the size and "
                     "price sent, per UTC day. Exits are never capped. "
                     "null = no cap."),
        },
        "paperAccount": {"bankrollUsd": cfg.get("mcp_paper_bankroll_usd"),
                         "note": "Your own paper account, separate from the user's."},
        "globalPaperMode": bool(cfg.get("terminal_paper_mode")),
        "offlineMode": bool(cfg.get("terminal_offline_mode")),
        "engines": {
            "main_engine": bool(cfg.get("enable_trading")),
            "crypto15m": bool(cfg.get("crypto15m_enabled")),
            "copy_trading": bool(cfg.get("copy_enabled")),
            "scripts_live": bool(cfg.get("scripts_live_enabled")),
            "perps": bool(cfg.get("perps_enabled")),
            "paused": bool(service.STATE.paused),
        },
        **({"config": cfg} if a.get("include_config") else {}),
    }


@tool("get_account", "Account balance",
      "Wallet cash, portfolio value, session and lifetime P&L for the live "
      "account.", read_only=True)
async def _get_account(_a: dict) -> dict:
    import service
    return await service._h_account({})


@tool("get_portfolio", "Open positions",
      "Open positions with cost basis, mark and unrealised P&L. mode=live is "
      "the real wallet (everything in it, whoever placed it); mode=paper is "
      "YOUR paper account. reconcile=true (live) first pulls fresh fills "
      "from Polymarket's own ledger, so just-filled orders show up.",
      props={"mode": _MODE, "reconcile": {"type": "boolean"}},
      required=("mode",), read_only=True)
async def _get_portfolio(a: dict) -> dict:
    if _mode(a) == "paper":
        import terminal_paper
        with terminal_paper.using_book("ai"):
            return await terminal_paper.portfolio(_cfg())
    import terminal_trade
    if a.get("reconcile"):
        await terminal_trade.reconcile_fills()
    return await terminal_trade.portfolio()


@tool("get_orders", "Resting / recent orders",
      "mode=live: orders resting on Polymarket right now (any origin). "
      "mode=paper: your recent paper orders, including fills and notes.",
      props={"mode": _MODE, "conditionId": _CID, "limit": _LIMIT},
      required=("mode",), read_only=True)
async def _get_orders(a: dict) -> dict:
    if _mode(a) == "paper":
        import terminal_paper
        with terminal_paper.using_book("ai"):
            return terminal_paper.orders(int(a.get("limit") or 100))
    import terminal_trade
    return await terminal_trade.resting_orders(a.get("conditionId") or None)


@tool("get_trade_history", "Trade history",
      "Live-account history. kind=fills: the venue's own fill ledger. "
      "kind=order_log: every order sent from this app including failures, "
      "with origin (ai = you). kind=settled: resolved positions and the real "
      "win/loss record.",
      props={"kind": {"type": "string", "enum": ["fills", "order_log", "settled"]},
             "limit": _LIMIT},
      required=("kind",), read_only=True)
async def _get_trade_history(a: dict) -> dict:
    import terminal_trade
    kind = a["kind"]
    limit = int(a.get("limit") or 100)
    if kind == "fills":
        return terminal_trade.fills(limit)
    if kind == "order_log":
        return terminal_trade.order_log(limit)
    if kind == "settled":
        return await terminal_trade.settled_record()
    raise Refused("kind must be fills, order_log or settled.")


@tool("get_engine_activity", "Engine activity",
      "What the automated engines are doing. kind=positions: main-engine "
      "positions. kind=signals: recent whale/momentum signals. kind=pnl: P&L "
      "series. kind=scanner: scanner stats. kind=crypto15m: the short-term "
      "crypto engine's status. kind=copy: copy-trading status.",
      props={"kind": {"type": "string", "enum": [
          "positions", "signals", "pnl", "scanner", "crypto15m", "copy"]},
             "limit": _LIMIT},
      required=("kind",), read_only=True)
async def _get_engine_activity(a: dict) -> Any:
    import service
    kind = a["kind"]
    limit = int(a.get("limit") or 100)
    if kind == "positions":
        return {"positions": await service._h_positions({"limit": limit})}
    if kind == "signals":
        return {"signals": await service._h_signals({"limit": limit})}
    if kind == "pnl":
        return {"series": await service._h_pnlSeries({"sinceHours": 168})}
    if kind == "scanner":
        return await service._h_scannerStats({})
    if kind == "crypto15m":
        return await service._h_crypto15mStatus({})
    if kind == "copy":
        return await service._h_copyStatus({})
    raise Refused("unknown kind")


@tool("discover_markets", "Discover markets",
      "A live list of markets. column: new, closing (closing soon), volume "
      "(high 24h volume), volatility, or watchlist (the user's starred "
      "markets). filters is optional, e.g. {\"minVolume24hUsd\": 10000, "
      "\"categories\": [\"politics\"]}.",
      props={"column": {"type": "string", "enum": [
          "new", "closing", "volume", "volatility", "watchlist"]},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100},
             "filters": {"type": "object"}},
      required=("column",), read_only=True)
async def _discover(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_discover(
        {"column": a["column"], "limit": a.get("limit") or 25,
         "filters": a.get("filters")})


@tool("search_markets", "Search markets",
      "Full-text search across open Polymarket markets. Returns conditionIds.",
      props={"query": {"type": "string"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
      required=("query",), read_only=True)
async def _search(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_search(
        {"query": a["query"], "limit": a.get("limit") or 20})


@tool("get_market", "Market details",
      "One market: title, rules/description, probability, bid/ask, spread, "
      "volume, liquidity, close time, status and resolution.",
      props={"conditionId": _CID}, required=("conditionId",), read_only=True)
async def _get_market(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_market({"conditionId": _cid(a)})


@tool("get_order_book", "Order book",
      "The live order book for one side, in cents and contracts, plus "
      "microstructure (imbalance, depth) when include_micro is true.",
      props={"conditionId": _CID, "side": _SIDE,
             "include_micro": {"type": "boolean"}},
      required=("conditionId",), read_only=True)
async def _get_book(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    p = {"conditionId": _cid(a), "side": a.get("side") or "yes"}
    out = await service._h_terminal_book(p)
    if a.get("include_micro"):
        out = {**out, "micro": await service._h_terminal_micro(p)}
    return out


@tool("get_price_history", "Price history",
      "Probability history for one side. interval: 1h, 6h, 1d, 1w or max.",
      props={"conditionId": _CID, "side": _SIDE,
             "interval": {"type": "string", "enum": ["1h", "6h", "1d", "1w", "max"]}},
      required=("conditionId",), read_only=True)
async def _get_history(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_history(
        {"conditionId": _cid(a), "side": a.get("side") or "yes",
         "interval": a.get("interval") or "1d"})


@tool("get_trade_tape", "Trade tape",
      "Recent public trades in a market (who bought what, at what price).",
      props={"conditionId": _CID, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
      required=("conditionId",), read_only=True)
async def _get_tape(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_tape(
        {"conditionId": _cid(a), "limit": a.get("limit") or 40})


@tool("get_resolution_risk", "Resolution risk",
      "The app's resolution-risk report: ambiguous wording, oracle and "
      "deadline traps, each check with its evidence. Scored only over checks "
      "that actually resolved.",
      props={"conditionId": _CID}, required=("conditionId",), read_only=True)
async def _get_resolution(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_resolution({"conditionId": _cid(a)})


@tool("get_related_markets", "Related markets",
      "Other markets in the same event or on the same topic.",
      props={"conditionId": _CID, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
      required=("conditionId",), read_only=True)
async def _get_related(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_related(
        {"conditionId": _cid(a), "limit": a.get("limit") or 15})


@tool("get_cross_venue", "Kalshi comparison",
      "The matching Kalshi market, if one is found, with the reasons for and "
      "against the match. The spread is withheld below the confidence floor.",
      props={"conditionId": _CID}, required=("conditionId",), read_only=True)
async def _get_cross(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_cross_venue({"conditionId": _cid(a)})


@tool("get_crypto_updown_markets", "Crypto Up/Down markets",
      "Live snapshot of Polymarket's recurring crypto Up-or-Down markets "
      "(5m / 15m / hourly / 4h; BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC): window "
      "timing, spot, prices and indicators.", read_only=True)
async def _get_crypto(_a: dict) -> dict:
    import service
    return await service._h_crypto15m({})


@tool("manage_watchlist", "Watchlist",
      "The user's starred markets (shared with the app's Discover screen). "
      "action=list, add (conditionId, optional note) or remove (conditionId).",
      props={"action": {"type": "string", "enum": ["list", "add", "remove"]},
             "conditionId": _CID, "note": {"type": "string"}},
      required=("action",))
async def _watchlist(a: dict) -> dict:
    import service
    act = a["action"]
    p: dict = {"action": act}
    if act in ("add", "remove"):
        p["conditionId"] = _cid(a)
        if act == "add":
            m = (await service._h_terminal_market(
                {"conditionId": p["conditionId"]})).get("market") or {}
            p["title"] = m.get("title")
            p["note"] = a.get("note")
    return await service._h_terminal_watchlist(p)


_ADDR = {"type": "string", "pattern": "^0x[0-9a-fA-F]{40}$",
         "description": "A Polymarket wallet address (0x…, 40 hex)."}


@tool("get_trader", "Look up a trader",
      "Any Polymarket wallet's open positions and recent activity (trades, "
      "redeems) from the public Data API — to research a trader before "
      "following them with copy trading (copy_wallets in update_config).",
      props={"address": _ADDR,
             "include": {"type": "array", "items": {
                 "type": "string", "enum": ["positions", "activity"]}},
             "limit": {"type": "integer", "minimum": 1, "maximum": 500}},
      required=("address",), read_only=True)
async def _get_trader(a: dict) -> dict:
    import re
    import polymarket_api
    addr = str(a["address"]).strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        raise Refused("address must be 0x followed by 40 hex characters.")
    inc = a.get("include") or ["positions", "activity"]
    limit = int(a.get("limit") or 100)
    out: dict = {"address": addr}
    if "positions" in inc:
        out["positions"] = (await polymarket_api.get_positions(
            limit=limit, user=addr))[:limit]
    if "activity" in inc:
        out["activity"] = (await polymarket_api.get_activity(
            limit=limit, user=addr))[:limit]
    return out


_ORDER_PROPS = {
    "mode": _MODE, "conditionId": _CID, "side": _SIDE,
    "action": {"type": "string", "enum": ["buy", "sell"]},
    "size": {"type": "number", "exclusiveMinimum": 0,
             "description": "Contracts (shares)."},
    "priceCents": {"type": "number", "minimum": 1, "maximum": 99,
                   "description": "Limit price in cents."},
    "orderType": {"type": "string", "enum": ["GTC", "FAK", "FOK"],
                  "description": "GTC rests on the book (default); FAK fills "
                                 "what it can and kills the rest; FOK all or "
                                 "nothing."},
}
_ORDER_REQ = ("mode", "conditionId", "side", "action", "size", "priceCents")


@tool("preview_order", "Preview an order",
      "Price an order against the live book WITHOUT sending it: cost, fee, "
      "what is fillable now and at what average, slippage, max payout/loss, "
      "tick and minimum size, warnings and blockers.",
      props=_ORDER_PROPS, required=_ORDER_REQ, read_only=True)
async def _preview(a: dict) -> dict:
    import terminal_trade
    mode = _mode(a)
    pv = await terminal_trade.preview(_order_from(a, mode))
    if mode == "live" and pv.get("action") == "buy":
        cfg = _cfg()
        cap = float(cfg.get("mcp_max_order_usd") or 0)
        cost = pv.get("costUsd")
        if cap and cost is not None and cost > cap:
            pv.setdefault("blockers", []).append(
                f"${cost:,.2f} is over the user's AI per-order cap of ${cap:,.2f}.")
    return pv


@tool("place_order", "Place an order",
      "Send an order. mode=paper fills against the real book into YOUR paper "
      "account. mode=live sends a real, signed order from the user's wallet "
      "and needs the user's live permission; live buys must fit the per-order "
      "and daily caps (see get_status). Preview first.",
      props=_ORDER_PROPS, required=_ORDER_REQ, kind="mixed", destructive=True)
async def _place(a: dict) -> dict:
    import terminal_trade
    mode = _mode(a)
    order = _order_from(a, mode)
    cfg = _cfg()
    if mode == "paper":
        import terminal_paper
        with terminal_paper.using_book("ai"):
            return await terminal_paper.place(order, cfg)

    _require_live_order(cfg)
    async with _live_lock:
        pv = await terminal_trade.preview(order)
        if pv["blockers"]:
            raise Refused(pv["blockers"][0])
        cost = pv.get("costUsd")
        if pv["action"] == "buy":
            if cost is None:
                raise Refused(
                    "The cost of this order could not be priced, so the "
                    "user's caps cannot be enforced. Not sent.")
            cap = float(cfg.get("mcp_max_order_usd") or 0)
            if cap and cost > cap:
                raise Refused(
                    f"${cost:,.2f} is over the user's AI per-order cap of "
                    f"${cap:,.2f}. Do not split the order to get under it; "
                    f"ask the user if they want the cap raised.")
            daily = float(cfg.get("mcp_daily_limit_usd") or 0)
            if daily:
                spent = spent_today_usd()
                if spent == float("inf"):
                    raise Refused("Today's AI spend could not be read, so the "
                                  "daily cap cannot be enforced. Not sent.")
                if spent + cost > daily + 1e-9:
                    raise Refused(
                        f"This ${cost:,.2f} buy would take today's AI spend to "
                        f"${spent + cost:,.2f}, over the user's daily cap of "
                        f"${daily:,.2f} (${max(0.0, daily - spent):,.2f} left). "
                        f"It resets at 00:00 UTC.")
        order["confirmUsd"] = cost if cost is not None else 0.0
        res = await terminal_trade.place(order, origin="ai")
    if res.get("ok"):
        logger.info("mcp: live %s %s %s @ %s — %s", order["action"], order["size"],
                    order["side"], order["priceCents"], res.get("message"))
        _push(f"AI agent: {order['action'].upper()} {order['size']} "
              f"{order['side'].upper()} @ {order['priceCents']}¢ — "
              f"{res.get('message')}")
    return res


@tool("cancel_order", "Cancel an order",
      "Cancel one resting order. mode=live takes the venue orderId; "
      "mode=paper takes the paper localId.",
      props={"mode": _MODE, "orderId": {"type": "string"},
             "localId": {"type": "integer"}},
      required=("mode",), kind="mixed")
async def _cancel(a: dict) -> dict:
    if _mode(a) == "paper":
        import terminal_paper
        if not a.get("localId"):
            raise Refused("localId is required for a paper cancel.")
        with terminal_paper.using_book("ai"):
            return await terminal_paper.cancel(int(a["localId"]))
    import terminal_trade
    cfg = _cfg()
    _require(cfg, "live")
    if not a.get("orderId"):
        raise Refused("orderId is required for a live cancel.")
    return await terminal_trade.cancel(str(a["orderId"]))


@tool("close_position", "Close a position",
      "Exit (part of) a position with one marketable fill-and-kill at the "
      "best bid, within maxSlippageCents (default 5). size defaults to the "
      "whole position. preview_only=true prices it without sending. Exits "
      "are never capped.",
      props={"mode": _MODE, "conditionId": _CID, "side": _SIDE,
             "size": {"type": "number", "exclusiveMinimum": 0},
             "maxSlippageCents": {"type": "number", "minimum": 0, "maximum": 20},
             "preview_only": {"type": "boolean"}},
      required=("mode", "conditionId", "side"), kind="mixed", destructive=True)
async def _close(a: dict) -> dict:
    import terminal_paper
    import terminal_trade
    mode = _mode(a)
    req = {"conditionId": _cid(a), "side": a["side"], "paper": mode == "paper"}
    if a.get("size") is not None:
        req["size"] = a["size"]
    if a.get("maxSlippageCents") is not None:
        req["maxSlippageCents"] = a["maxSlippageCents"]
    book = "ai" if mode == "paper" else "manual"
    with terminal_paper.using_book(book):
        pv = await terminal_trade.sell_position_preview(req)
        if a.get("preview_only"):
            return pv
        if mode == "live":
            _require_live_order(_cfg())
        if pv["blockers"]:
            raise Refused(pv["blockers"][0])
        req["confirmUsd"] = pv.get("proceedsUsd") or 0.0
        res = await terminal_trade.sell_position(req, origin="ai")
    if mode == "live" and res.get("ok"):
        _push(f"AI agent closed {a['side'].upper()} in {req['conditionId'][:12]}… — "
              f"{res.get('message')}")
    return res


@tool("cancel_all_orders", "Cancel all live orders",
      "Cancel every resting live order: the main engine's and the terminal's "
      "(manual, standing, crypto, copy, script, AI).",
      kind="live", destructive=True)
async def _cancel_all(_a: dict) -> dict:
    import remote
    _require(_cfg(), "live")
    return {"ok": True, "message": await remote._cancel_all()}


@tool("flatten_wallet", "Sell everything",
      "Close the main engine's positions AND sell every position in the live "
      "wallet at market. confirm=false (default) only prices it; confirm=true "
      "sends. Not capped: it is a way out.",
      props={"confirm": {"type": "boolean"}}, kind="live", destructive=True)
async def _flatten(a: dict) -> dict:
    import service
    import terminal_trade
    if not a.get("confirm"):
        return await terminal_trade.flatten_preview()
    _require_live_order(_cfg())
    r = await service._h_flatten({})
    fw = await terminal_trade.flatten_wallet(
        allow_trading=True, origin="ai", reason="AI agent flatten")
    _push(f"AI agent flattened the wallet — {fw.get('message')}")
    return {**fw, "mainEngineClosed": int(r.get("closed") or 0)}


@tool("manage_standing_instructions", "Stops, take-profits, alerts",
      "Standing instructions, checked every few seconds while the app runs. "
      "mode=live: on the real wallet (stops sell with a marketable order). "
      "mode=paper: on YOUR paper account. action=list shows them. action=add "
      "needs kind (stopLoss | takeProfit | alertAbove | alertBelow), "
      "conditionId, side, triggerCents (a probability point 1–99, not a "
      "percent move) and size for stops/take-profits. action=cancel needs id.",
      props={"mode": _MODE,
             "action": {"type": "string", "enum": ["list", "add", "cancel"]},
             "kind": {"type": "string", "enum": [
                 "stopLoss", "takeProfit", "alertAbove", "alertBelow"]},
             "conditionId": _CID, "side": _SIDE,
             "triggerCents": {"type": "number", "minimum": 1, "maximum": 99},
             "size": {"type": "number", "exclusiveMinimum": 0},
             "note": {"type": "string"}, "id": {"type": "string"}},
      required=("mode", "action"), kind="mixed")
async def _standing(a: dict) -> dict:
    import terminal_trade
    act = a["action"]
    if _mode(a) == "paper":
        return _paper_standing(a)
    if act == "list":
        return {"instructions": terminal_trade.standing_list()}
    _require(_cfg(), "live")
    if act == "add":
        item = {k: a.get(k) for k in ("kind", "conditionId", "side",
                                      "triggerCents", "size")}
        item["note"] = ("[AI] " + str(a.get("note") or "")).strip()
        item["origin"] = "ai"
        try:
            entry = terminal_trade.standing_add(item)
        except terminal_trade.TerminalTradeError as e:
            raise Refused(str(e))
        return {"ok": True, "instruction": entry}
    if act == "cancel":
        if not a.get("id"):
            raise Refused("id is required.")
        return {"ok": True, "instructions": terminal_trade.standing_cancel(str(a["id"]))}
    raise Refused("action must be list, add or cancel.")


@tool("reset_paper_account", "Reset your paper account",
      "Wipe YOUR paper account back to its starting bankroll. The user's "
      "paper account is not touched.", kind="paper", destructive=True)
async def _reset_paper(_a: dict) -> dict:
    import terminal_paper
    with terminal_paper.using_book("ai"):
        return {"ok": True, **terminal_paper.reset()}


_ENGINE_KEYS = {
    "main_engine": "enableTrading",
    "crypto15m": "crypto15mEnabled",
    "copy_trading": "copyEnabled",
    "scripts_live": "scriptsLiveEnabled",
    "perps": "perpsEnabled",
}


def _refuse_perps_on_in_paper(cfg: dict) -> None:
    if cfg.get("terminal_paper_mode"):
        raise Refused(
            "The app is in PAPER MODE, and the Perps market maker has no paper "
            "mode: it would quote with real money. Backtest it with perps "
            "action='backtest' instead, or ask the user to turn paper mode off.")


@tool("pause_all_engines", "Pause every engine",
      "Turn off every automated engine (main, crypto, copy, live scripts, "
      "the Perps market maker, which then pulls its quotes). "
      "Always allowed. Does not cancel resting orders or sell anything.",
      kind="derisk")
async def _pause_all(_a: dict) -> dict:
    await _set_cfg({v: False for v in _ENGINE_KEYS.values()})
    _push("AI agent paused every engine.")
    return {"ok": True, "message": "All engines are off."}


@tool("set_engines", "Turn engines on or off",
      "Switch automated engines. engines maps main_engine / crypto15m / "
      "copy_trading / scripts_live / perps to true or false. perps is the "
      "Polymarket Perps market maker (see the perps tool); it needs a linked "
      "Perps account and cannot be switched on in paper mode. Turning one OFF is always "
      "allowed; turning one ON needs the user's live permission, and the "
      "engine then trades with the user's own risk settings, not your caps.",
      props={"engines": {"type": "object", "properties": {
          k: {"type": "boolean"} for k in _ENGINE_KEYS},
          "additionalProperties": False}},
      required=("engines",), kind="mixed", destructive=True)
async def _set_engines(a: dict) -> dict:
    want = a.get("engines") or {}
    bad = [k for k in want if k not in _ENGINE_KEYS]
    if bad:
        raise Refused(f"unknown engine(s): {', '.join(bad)}")
    if any(bool(v) for v in want.values()):
        _require(_cfg(), "live")
    if want.get("perps"):
        _refuse_perps_on_in_paper(_cfg())
    await _set_cfg({_ENGINE_KEYS[k]: bool(v) for k, v in want.items()})
    if any(bool(v) for v in want.values()):
        _push("AI agent changed engines: " + ", ".join(
            f"{k} {'ON' if v else 'off'}" for k, v in want.items()))
    return {"ok": True, "engines": (await _get_status({}))["engines"]}


_CONFIG_BLOCKED_PREFIXES = ("mcp_", "remote_", "ai_")
_CONFIG_BLOCKED = {"network"}


def _snake_to_camel(k: str) -> str:
    head, *rest = k.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _same_type(default: Any, value: Any) -> bool:
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return False


@tool("update_config", "Change engine settings",
      "Change any engine setting: strategy thresholds, sizing, risk limits, "
      "crypto assets and posture, copy-trading wallets and sizing, script "
      "rails, the Perps market maker (perps_* keys: quote distance, size, "
      "levels, inventory cap, skew, loss limits, market selection), and so on. changes maps snake_case keys (see get_status "
      "include_config) to new values of the same type. Values are clamped to "
      "the app's safe ranges and the effective values come back. Turning "
      "something OFF (false) or turning paper mode ON is always allowed; "
      "anything else needs the user's live permission. Your own permissions "
      "and caps (mcp_*), remote-control and AI-provider settings cannot be "
      "changed. Backtest a change before making it.",
      props={"changes": {"type": "object"}}, required=("changes",),
      kind="mixed", destructive=True)
async def _update_config(a: dict) -> dict:
    from config import DEFAULT_CONFIG, _camel_to_snake
    changes = a.get("changes") or {}
    if not isinstance(changes, dict) or not changes:
        raise Refused("changes must be a non-empty object.")
    patch: dict = {}
    asked: dict = {}
    safe_only = True
    for k, v in changes.items():
        sk = _camel_to_snake(k) if any(c.isupper() for c in k) else k
        if sk.startswith(_CONFIG_BLOCKED_PREFIXES) or sk in _CONFIG_BLOCKED:
            raise Refused(f"'{sk}' cannot be changed by an agent.")
        if sk not in DEFAULT_CONFIG:
            raise Refused(f"unknown setting '{sk}' (see get_status include_config).")
        if not _same_type(DEFAULT_CONFIG[sk], v):
            raise Refused(f"'{sk}' takes a {type(DEFAULT_CONFIG[sk]).__name__}, "
                          f"not {type(v).__name__}.")
        ck = _snake_to_camel(sk)
        if _camel_to_snake(ck) != sk:
            raise Refused(f"'{sk}' cannot be patched from here; ask the user.")
        if sk == "terminal_paper_mode" and v is False:
            raise Refused("Only the user can turn paper mode OFF.")
        if sk == "perps_enabled" and v is True:
            _refuse_perps_on_in_paper(_cfg())
        if not (v is False or (sk == "terminal_paper_mode" and v is True)):
            safe_only = False
        patch[ck] = v
        asked[sk] = v
    if not safe_only:
        _require(_cfg(), "live")
    await _set_cfg(patch)
    after = _cfg()
    eff = {sk: after.get(sk) for sk in asked}
    clamped = {sk: {"asked": asked[sk], "effective": eff[sk]}
               for sk in asked if asked[sk] != eff[sk]}
    if not safe_only:
        _push("AI agent changed settings: " + ", ".join(
            f"{k}={json.dumps(v, default=str)[:40]}" for k, v in eff.items()))
    return {"ok": True, "effective": eff, **({"clamped": clamped} if clamped else {})}


_PERPS_READ = ("board", "status", "backtest")
_PERPS_ACTIONS = _PERPS_READ + ("cancel_quotes", "flatten", "link", "unlink")
_PERPS_BOARD_SORT = {
    "reward": lambda r: (r.get("estRewardUsd") is None, -(r.get("estRewardUsd") or 0.0)),
    "volume": lambda r: -(r.get("avgDailyVolUsd") or 0.0),
    "funding": lambda r: -abs(r.get("fundingAprPct") or 0.0),
}


def _require_live_perps(cfg: dict) -> None:
    _require(cfg, "live")
    if cfg.get("terminal_paper_mode"):
        raise Refused(
            "The app is in PAPER MODE, so no real order can be sent from "
            "anywhere — including an agent. Ask the user to turn paper mode "
            "off in Settings.")


async def _perps_flatten_preview() -> dict:
    import perps_api
    c = perps_api.creds()
    if not c:
        return {"ok": False, "message": "Perps account not linked."}
    pf = await perps_api.portfolio(c)
    inst = {int(i["instrument_id"]): i for i in await perps_api.instruments()}
    rows = []
    for pos in pf.get("positions") or []:
        try:
            iid, size = int(pos["instrument_id"]), float(pos.get("size") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if size:
            rows.append({"symbol": (inst.get(iid) or {}).get("symbol") or iid,
                         "side": "long" if size > 0 else "short", "qty": abs(size),
                         "valueUsd": pos.get("position_value"),
                         "unrealizedPnlUsd": pos.get("unrealized_pnl")})
    return {"ok": True, "preview": True, "positions": rows,
            "message": (f"confirm=true cancels our quotes and closes {len(rows)} "
                        "position(s) with reduce-only IOC orders up to 0.5% "
                        "through the touch." if rows else
                        "No open Perps positions; confirm=true would only cancel our quotes.")}


@tool("perps", "Polymarket Perps",
      "The Polymarket Perps market maker: it quotes post-only bids and asks "
      "around mid to earn spread and the exchange's liquidity rewards "
      "($75k/day across ~90 markets; an account earns nothing until its 7-day "
      "maker share is 1% of exchange maker volume). Actions: "
      "board = every market with mid, spread, funding, 7-day volume, "
      "competing depth and the estimated reward for the configured quote "
      "(category, sort, limit); "
      "status = the engine: link, account equity and margin, 7-day maker "
      "share, rewards, per-market quotes, positions, markouts and today's P&L, "
      "halt reason, recent fills; "
      "backtest = replay the maker over the exchange's trade history (days "
      "up to 30, markets as symbols, variants [{name, config}] and/or grid "
      "{perpsQuoteBps: [..], ...} of camelCase perps keys, feeTier 0-6, "
      "queueShare, latencyMs) — never places orders, one at a time; "
      "cancel_quotes = pull every quote the maker placed (it requotes on the "
      "next tick while switched on: use set_engines perps=false to stop it); "
      "flatten = cancel quotes and close every Perps position at market "
      "(confirm=false only lists them); link = authorise a Perps signer with "
      "the wallet key (one signature) so the maker can trade; unlink = cancel "
      "quotes and forget it. board, status and backtest need no permission; "
      "the rest need the user's live permission and paper mode off. Settings "
      "are perps_* keys in update_config; on/off is set_engines perps.",
      props={"action": {"type": "string", "enum": list(_PERPS_ACTIONS)},
             "category": {"type": "string",
                          "enum": ["crypto", "equity", "index", "commodity"]},
             "sort": {"type": "string", "enum": list(_PERPS_BOARD_SORT)},
             "limit": {"type": "integer", "minimum": 1, "maximum": 120},
             "days": {"type": "number", "minimum": 0.25, "maximum": 30},
             "markets": {"type": "array", "items": {"type": "string"},
                         "description": "Symbols, e.g. [\"BTC-USD\"]. Default: "
                                        "what the engine would pick."},
             "variants": {"type": "array", "items": {"type": "object"}},
             "grid": {"type": "object"},
             "feeTier": {"type": "integer", "minimum": 0, "maximum": 6},
             "queueShare": {"type": "number", "minimum": 0, "maximum": 1},
             "latencyMs": {"type": "integer", "minimum": 0, "maximum": 10000},
             "confirm": {"type": "boolean"}},
      required=("action",), kind="mixed", destructive=True)
async def _perps(a: dict) -> dict:
    import service
    act = str(a.get("action") or "")
    if act not in _PERPS_ACTIONS:
        raise Refused(f"action must be one of: {', '.join(_PERPS_ACTIONS)}.")
    if act == "board":
        b = dict(await service._h_perps_board({}))
        rows = list(b.get("markets") or [])
        if a.get("category"):
            rows = [r for r in rows if r.get("category") == a["category"]]
        rows.sort(key=_PERPS_BOARD_SORT[a.get("sort") or "reward"])
        n = int(a.get("limit") or 30)
        b["marketCount"] = len(rows)
        b["markets"] = rows[:n]
        return b
    if act == "status":
        return await service._h_perps_status({})
    if act == "backtest":
        p = {k: a[k] for k in ("days", "markets", "variants", "grid", "feeTier",
                               "queueShare", "latencyMs") if a.get(k) is not None}
        return await service._h_perps_backtest(p)
    if act == "flatten" and not a.get("confirm"):
        return await _perps_flatten_preview()
    _require_live_perps(_cfg())
    if act == "cancel_quotes":
        res = await service._h_perps_cancel_all({})
    elif act == "flatten":
        res = await service._h_perps_flatten({})
    elif act == "link":
        res = await service._h_perps_link({})
    else:
        res = await service._h_perps_unlink({})
    if res.get("ok"):
        _push(f"AI agent ran Perps {act} — {res.get('message')}")
    return res


@tool("backtest_crypto15m", "Backtest the crypto engine",
      "Replay the short-term crypto engine over recorded ticks through its "
      "live entry gates, fee-aware. config overrides keys for this run only.",
      props={"config": _CONFIG, "sinceDays": _SINCE}, read_only=True)
async def _bt_c15(a: dict) -> dict:
    import service
    return await service._h_c15_backtest(
        {"config": a.get("config") or {}, "sinceDays": a.get("sinceDays") or 60})


@tool("backtest_main_engine", "Backtest the main engine",
      "Replay recorded whale/momentum signals through the main engine's live "
      "gates with follower economics. config overrides keys for this run only.",
      props={"config": _CONFIG, "sinceDays": _SINCE}, read_only=True)
async def _bt_main(a: dict) -> dict:
    import service
    return await service._h_main_backtest(
        {"config": a.get("config") or {}, "sinceDays": a.get("sinceDays") or 60})


@tool("backtest_script", "Backtest a strategy script",
      "Backtest a strategy script over the recorded tick corpus — pass code "
      "(see get_script_docs) or the id of a saved script. Never places "
      "orders. Needs the user's scripts permission, because the code runs.",
      props={"code": {"type": "string"}, "id": {"type": "string"},
             "config": _CONFIG, "sinceDays": _SINCE,
             "assets": {"type": "array", "items": {"type": "string"}}},
      kind="scripts", read_only=True)
async def _bt_script(a: dict) -> dict:
    import service
    _require(_cfg(), "scripts")
    if not a.get("code") and not a.get("id"):
        raise Refused("pass code or id.")
    code = a.get("code")
    if code:
        _refuse_critical(code)
    p = {"code": code, "id": a.get("id"), "config": a.get("config") or {},
         "sinceDays": a.get("sinceDays") or 60}
    if a.get("assets") is not None:
        p["assets"] = a["assets"]
    return await service._h_script_backtest(p)


@tool("parlay_schedule", "Per-hour crypto schedule",
      "The crypto engine's per-hour schedule, mined from the recorded corpus "
      "with a holdout. action=generate mines one (save=true stores it "
      "UNARMED); status shows the stored one; arm makes it OWN the crypto "
      "engine's entries (seated hours trade their generated settings, "
      "unseated hours do not trade); disarm hands entries back. save and arm "
      "need the user's live permission.",
      props={"action": {"type": "string", "enum": ["generate", "status", "arm", "disarm"]},
             "interval": {"type": "string", "enum": ["5m", "15m", "1h"]},
             "sinceDays": _SINCE,
             "holdoutDays": {"type": "integer", "minimum": 1, "maximum": 90},
             "minWinRate": {"type": "number", "minimum": 0, "maximum": 1},
             "minTrades": {"type": "integer", "minimum": 1},
             "save": {"type": "boolean"}},
      required=("action",), kind="mixed")
async def _parlay(a: dict) -> dict:
    import service
    act = a["action"]
    if act == "status":
        return await service._h_c15_parlay_status({})
    if act in ("arm", "disarm"):
        if act == "arm":
            _require(_cfg(), "live")
        res = await service._h_c15_parlay_arm({"armed": act == "arm"})
        if res.get("error"):
            raise Refused(res["error"])
        if act == "arm":
            _push("AI agent ARMED the crypto parlay schedule.")
        return {"ok": True, **res}
    save = bool(a.get("save"))
    if save:
        _require(_cfg(), "live")
    p = {k: a[k] for k in ("interval", "sinceDays", "holdoutDays",
                           "minWinRate", "minTrades") if k in a}
    p["save"] = save
    return await service._h_c15_parlay_generate(p)


_SQL_MAX_ROWS = 2000
_SQL_TIME_BUDGET_SEC = 20.0


def _query_ro(sql: str, limit: int) -> dict:
    import sqlite3
    import time as _t
    import db
    path = db.db_path()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only = ON")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                   sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}

        def _auth(action, *_args):
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
        conn.set_authorizer(_auth)
        deadline = _t.monotonic() + _SQL_TIME_BUDGET_SEC
        conn.set_progress_handler(lambda: 1 if _t.monotonic() > deadline else 0, 10_000)
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e).lower():
                raise Refused(f"query stopped at the {_SQL_TIME_BUDGET_SEC:.0f}s "
                              f"budget — narrow it with WHERE / LIMIT.")
            raise Refused(f"SQL error: {e}. Only one read-only SELECT is allowed.")
        except (sqlite3.DatabaseError, sqlite3.Warning) as e:
            raise Refused(f"SQL error: {e}. Only one read-only SELECT is allowed.")
        return {"columns": cols, "rows": [list(r) for r in rows[:limit]],
                "rowCount": min(len(rows), limit), "truncated": len(rows) > limit}
    finally:
        conn.close()


def _schema() -> list[dict]:
    import db
    with db.get_db() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return [{"table": t, "columns": [
            f"{c[1]} {c[2]}".strip() for c in conn.execute(f'PRAGMA table_info("{t}")')]}
            for t in tables]


@tool("research_data", "Query the recorded data",
      "The app records market data for research: crypto Up/Down ticks with "
      "spot, book and indicators; whale and momentum signals; trades; fills; "
      "resolutions. With no sql: an inventory of what is recorded plus the "
      "full schema. With sql: run ONE read-only SQLite SELECT and get up to "
      "`limit` rows (default 200, max 2000; 20s budget) — test your own "
      "hypotheses here before backtesting or trading them. "
      "include_crypto_history adds the crypto engine's resolved trades.",
      props={"sql": {"type": "string"},
             "limit": {"type": "integer", "minimum": 1, "maximum": _SQL_MAX_ROWS},
             "include_crypto_history": {"type": "boolean"}}, read_only=True)
async def _research(a: dict) -> dict:
    import service
    if a.get("sql"):
        limit = max(1, min(_SQL_MAX_ROWS, int(a.get("limit") or 200)))
        return await asyncio.to_thread(_query_ro, str(a["sql"]), limit)
    out = {"collection": await service._h_collection_stats({}),
           "schema": await asyncio.to_thread(_schema)}
    if a.get("include_crypto_history"):
        out["cryptoHistory"] = await service._h_c15_history({"limit": 200})
    return out


def _refuse_critical(code: str) -> None:
    import script_audit
    import service
    rep = script_audit.audit(service._sanitize_script_code(code))
    if not rep.get("ok"):
        crit = [f for f in rep.get("findings") or [] if f.get("severity") == "critical"]
        lines = "; ".join(f"line {f.get('line')}: {f.get('message')}" for f in crit[:5])
        raise Refused(
            "The script audit found critical issues, and scripts written by "
            f"an agent with critical findings are refused: {lines or rep.get('summary')}")


@tool("get_script_docs", "Script API docs",
      "The strategy-script API (hooks, ctx fields, order helpers, rails) "
      "rendered from the live modules, plus a context pack with examples.",
      read_only=True)
async def _script_docs(_a: dict) -> dict:
    import service
    return {"docs": await service._h_script_api_docs({}),
            "contextPack": await service._h_script_context_pack({})}


@tool("list_scripts", "List scripts",
      "Saved strategy scripts with their state (enabled, shadow or live), "
      "stats and last error. With id + include_shadow, also that script's "
      "shadow (would-have-traded) orders.",
      props={"id": {"type": "string"}, "include_shadow": {"type": "boolean"},
             "limit": _LIMIT}, read_only=True)
async def _list_scripts(a: dict) -> dict:
    import service
    out = {"scripts": await service._h_scripts_list({})}
    if a.get("id") and a.get("include_shadow"):
        out["shadowOrders"] = await service._h_script_shadow_orders(
            {"id": a["id"], "limit": a.get("limit") or 100})
    return out


@tool("validate_script", "Validate a script",
      "Check script code without saving or running it: errors, warnings and "
      "the security audit.",
      props={"code": {"type": "string"}}, required=("code",), read_only=True)
async def _validate(a: dict) -> dict:
    import service
    return await service._h_script_validate({"code": a["code"]})


@tool("save_script", "Save a script",
      "Create or update a strategy script (pass id to update). Saved scripts "
      "start DISABLED and in shadow mode. Needs the scripts permission; code "
      "with a critical audit finding is refused.",
      props={"code": {"type": "string"}, "id": {"type": "string"},
             "name": {"type": "string"}, "description": {"type": "string"}},
      required=("code",), kind="scripts")
async def _save_script(a: dict) -> dict:
    import service
    _require(_cfg(), "scripts")
    _refuse_critical(a["code"])
    p = {"code": a["code"], "id": a.get("id") or "",
         "name": a.get("name") or "", "description": a.get("description") or "",
         "notes": "Written by an AI agent over MCP."}
    return {"ok": True, **(await service._h_script_save(p))}


@tool("set_script_state", "Enable / arm a script",
      "enabled=true runs a saved script in SHADOW mode (it logs what it would "
      "trade). live=true arms it for real orders and needs the user's live "
      "permission too; live=false returns it to shadow. Disabling is always "
      "allowed.",
      props={"id": {"type": "string"}, "enabled": {"type": "boolean"},
             "live": {"type": "boolean"}},
      required=("id",), kind="mixed", destructive=True)
async def _script_state(a: dict) -> dict:
    import db
    import service
    sid = str(a["id"])
    cfg = _cfg()
    turning_on = bool(a.get("enabled")) or bool(a.get("live"))
    if turning_on:
        _require(cfg, "scripts")
        with db.get_db() as conn:
            row = db.get_user_script(conn, sid)
        if not row:
            raise Refused("script not found")
        _refuse_critical(str(row.get("code") or ""))
    if a.get("live"):
        _require(cfg, "live")
    out: dict = {"ok": True}
    if "enabled" in a:
        out.update(await service._h_script_set_enabled({"id": sid, "enabled": bool(a["enabled"])}))
    if "live" in a:
        out.update(await service._h_script_set_dry_run({"id": sid, "dryRun": not a["live"]}))
        if a["live"]:
            _push(f"AI agent armed script {sid} for LIVE orders.")
    return out


_PAPER_STANDING_KV = "mcp:paper_standing"
_PAPER_STANDING_MAX = 200


def _ps_load() -> list[dict]:
    import db
    with db.get_db() as conn:
        raw = db.kv_get(conn, _PAPER_STANDING_KV)
    try:
        rows = json.loads(raw) if raw else []
    except ValueError:
        rows = []
    return rows if isinstance(rows, list) else []


def _ps_save(rows: list[dict]) -> None:
    import db
    with db.get_db() as conn:
        db.kv_set(conn, _PAPER_STANDING_KV, json.dumps(rows[:_PAPER_STANDING_MAX]))


def _paper_standing(a: dict) -> dict:
    import uuid
    import terminal_trade
    act = a["action"]
    rows = _ps_load()
    if act == "list":
        return {"instructions": rows}
    if act == "cancel":
        if not a.get("id"):
            raise Refused("id is required.")
        rows = [r for r in rows if r.get("id") != a["id"]]
        _ps_save(rows)
        return {"ok": True, "instructions": rows}
    if act != "add":
        raise Refused("action must be list, add or cancel.")
    kind = str(a.get("kind") or "")
    if kind not in terminal_trade._VALID_KINDS:
        raise Refused("kind must be stopLoss, takeProfit, alertAbove or alertBelow.")
    trig = a.get("triggerCents")
    if not isinstance(trig, (int, float)) or not (0 < trig < 100):
        raise Refused("triggerCents is a probability point between 0 and 100.")
    size = a.get("size")
    if kind in ("stopLoss", "takeProfit") and (not isinstance(size, (int, float)) or size <= 0):
        raise Refused("a stop or take-profit needs the number of contracts to close.")
    entry = {
        "id": f"ps_{uuid.uuid4().hex[:10]}", "conditionId": _cid(a),
        "side": "no" if str(a.get("side") or "yes").lower() == "no" else "yes",
        "kind": kind, "triggerCents": round(float(trig), 2),
        "size": float(size) if kind in ("stopLoss", "takeProfit") else None,
        "note": a.get("note") or None, "armed": True,
        "createdAt": __import__("mcp_server")._iso(),
        "firedAt": None, "result": None,
    }
    rows.insert(0, entry)
    _ps_save(rows)
    return {"ok": True, "instruction": entry}


async def paper_standing_tick() -> int:
    import terminal as term
    import terminal_paper
    import terminal_trade
    rows = _ps_load()
    live = [r for r in rows if r.get("armed") and not r.get("firedAt")]
    if not live:
        return 0
    fired = 0
    marks: dict[str, Any] = {}
    for e in live:
        cid = e["conditionId"]
        if cid not in marks:
            try:
                marks[cid] = await term.market(cid)
            except Exception:
                marks[cid] = None
        m = marks[cid]
        if not m or m.get("probability") is None:
            continue
        p = float(m["probability"])
        price = p if e["side"] == "yes" else round(100.0 - p, 2)
        trig = float(e["triggerCents"])
        kind = e["kind"]
        hit = ((kind in ("stopLoss", "alertBelow") and price <= trig)
               or (kind in ("takeProfit", "alertAbove") and price >= trig))
        if not hit:
            continue
        e["firedAt"] = __import__("mcp_server")._iso()
        e["armed"] = False
        fired += 1
        if kind.startswith("alert"):
            e["result"] = f"Price reached {price:g}¢ (trigger {trig:g}¢)."
            continue
        req = {"conditionId": cid, "side": e["side"], "size": e["size"], "paper": True}
        with terminal_paper.using_book("ai"):
            pv = await terminal_trade.sell_position_preview(req)
            if pv["blockers"]:
                e["result"] = f"Trigger hit at {price:g}¢; not sold: {pv['blockers'][0]}"
                continue
            req["confirmUsd"] = pv.get("proceedsUsd") or 0.0
            res = await terminal_trade.sell_position(req, origin="ai")
        e["result"] = f"Trigger hit at {price:g}¢ — {res.get('message')}"
    _ps_save(rows)
    return fired
