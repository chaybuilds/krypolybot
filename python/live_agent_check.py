"""Live check of the AI-agent money path, through the app's own backend and
the stdio relay an AI client launches. Nothing is called in-process: every
order goes MCP client -> relay -> loopback MCP server -> rails -> CLOB, and
every approval goes through the same RPC the AI Agents page calls.

Deliberately NOT part of `npm run py:test`, and never run by CI: the live run
spends real money from the owner's wallet. Run it BY HAND, from python/:

    .venv/Scripts/python.exe live_agent_check.py --dry-run   # throwaway profile, paper, no wallet
    .venv/Scripts/python.exe live_agent_check.py             # REAL profile, REAL money (capped)

Live run, in order (aborts at the first failed check, then cleans up):
  1. refuses if Krypt PolyBot is open (two backends on one DB / one port)
  2. backend on the real profile WITHOUT Electron: settings.json is never
     written. Config = your saved config + account Live, agents Live, one
     extra agent "Live test" (live), caps of --max-usd per order. Every
     engine switch is forced off.
  3. refusals that cost nothing: buy with no forecast, over the cap,
     selling what the agent does not own
  4. RESTING buy far below the bid -> queued -> approved -> rests on
     Polymarket -> cancelled by the agent.            cost: $0
  5. a queued order REJECTED -> nothing sent.        cost: $0
  6. (unless --resting-only) a marketable FAK buy of the minimum size ->
     approved -> filled -> the agent closes it.      cost: spread + fees
  7. cleanup, always: cancel the agent's resting orders, close what it still
     holds, restore your config (Paper, agents off) and delete the test
     agent's token; shut the backend down.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue

PYDIR = Path(__file__).resolve().parent
PY = PYDIR / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = PYDIR / ".venv" / "bin" / "python"
AGENT = "livetest"

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true",
                help="throwaway profile + vault, paper only, no wallet")
ap.add_argument("--max-usd", type=float, default=1.6,
                help="per-order cap for the test agent (default $1.60)")
ap.add_argument("--resting-only", action="store_true",
                help="skip the filled buy+sell; only the $0 steps")
args = ap.parse_args()
LIVE = not args.dry_run
MODE = "live" if LIVE else "paper"

env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
REAL_UD = Path(os.environ["APPDATA"]) / "Krypt PolyBot"
if LIVE:
    env["KRYPT_POLYBOT_USERDATA"] = str(REAL_UD)
    env.pop("KRYPT_POLYBOT_VAULT", None)
else:
    scratch = tempfile.mkdtemp(prefix="krypt-agenttest-")
    env["KRYPT_POLYBOT_USERDATA"] = scratch
    env["KRYPT_POLYBOT_VAULT"] = os.path.join(scratch, "vault")

results: list[tuple[bool, str]] = []
live_sent: list[str] = []


class Abort(Exception):
    pass


def check(label: str, cond, extra: str = "", fatal: bool = True) -> bool:
    ok = bool(cond)
    results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if extra else ""), flush=True)
    if not ok and fatal:
        raise Abort(label)
    return ok


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


class Backend:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [str(PY), "service.py"], cwd=str(PYDIR),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", bufsize=1)
        self.replies: "Queue[dict]" = Queue()
        self.logs: list[str] = []
        self.ready = threading.Event()
        self.auth_ok: bool | None = None
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "rpc":
                self.replies.put(msg)
            elif msg.get("type") == "log":
                self.logs.append(f"{msg.get('level')} {msg.get('msg')}")
            elif msg.get("type") == "event":
                if msg.get("name") == "backend:authChanged":
                    self.auth_ok = bool((msg.get("data") or {}).get("authOk"))
                elif msg.get("name") == "backend:ready":
                    self.ready.set()

    def _drain_err(self) -> None:
        for line in self.proc.stderr:
            self.logs.append("stderr " + line.rstrip())

    def rpc(self, method: str, params: dict | None = None, timeout: float = 60):
        rid = uuid.uuid4().hex
        self.proc.stdin.write(json.dumps(
            {"type": "rpc", "id": rid, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                msg = self.replies.get(timeout=1)
            except Empty:
                continue
            if msg.get("id") == rid:
                if not msg.get("ok"):
                    raise RuntimeError(f"{method}: {msg.get('error')}")
                return msg.get("result")
        raise TimeoutError(f"{method} timed out")

    def close(self) -> None:
        try:
            self.rpc("shutdown", timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class Relay:
    def __init__(self, agent_id: str) -> None:
        self.proc = subprocess.Popen(
            [str(PY), "service.py", "--mcp", "--agent", agent_id], cwd=str(PYDIR),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", bufsize=1)
        self.replies: "Queue[dict]" = Queue()
        self.err: list[str] = []
        self.n = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: [self.err.append(l.rstrip()) for l in self.proc.stderr],
                         daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                self.replies.put(json.loads(line))
            except ValueError:
                pass

    def send(self, method: str, params: dict | None = None, notify: bool = False,
             timeout: float = 120):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = self.replies.get(timeout=1)
            except Empty:
                continue
            if r.get("id") == msg["id"]:
                return r
        raise TimeoutError(f"MCP {method} timed out")

    def tool(self, name: str, a: dict | None = None) -> tuple[bool, object]:
        """(ok, parsed result or refusal text)."""
        r = self.send("tools/call", {"name": name, "arguments": a or {}})
        res = r.get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content") or [])
        if r.get("error"):
            return False, str(r["error"])
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return (not res.get("isError")), body

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def geoblock() -> dict:
    """Polymarket's own region check (docs: developers/CLOB/geoblock)."""
    import urllib.request
    req = urllib.request.Request("https://polymarket.com/api/geoblock",
                                 headers={"User-Agent": "KryptPolyBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        return {"error": f"region check failed: {e}"}


def app_is_open() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Krypt PolyBot.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "Krypt PolyBot.exe" in out


def main() -> int:
    if not PY.exists():
        print(f"No backend venv at {PY}. Run `npm run dev` once to create it.")
        return 2
    print(("*** LIVE: real profile, real wallet, caps $%.2f/order ***" % args.max_usd)
          if LIVE else f"dry run (paper) in {env['KRYPT_POLYBOT_USERDATA']}")
    if LIVE and app_is_open():
        print("Krypt PolyBot is open. Close it first (tray icon -> Quit): two backends "
              "would share one database and one MCP port.")
        return 2

    base: dict = {}
    if LIVE:
        base = json.loads((REAL_UD / "settings.json").read_text(encoding="utf-8-sig"))["config"]
    agents = [a for a in (base.get("mcpAgents") or [{"id": "default", "name": "Default"}])
              if a.get("id") != AGENT]
    test_cfg = {
        **base,
        "accountMode": "live" if LIVE else "paper",
        "enableTrading": False, "crypto15mEnabled": False, "copyEnabled": False,
        "perpsEnabled": False, "scriptsLiveEnabled": False, "remoteTradingEnabled": False,
        "mcpEnabled": True, "mcpHttpEnabled": False, "mcpTradeMode": MODE,
        "mcpLiveApproval": True, "mcpMaxOrderUsd": args.max_usd,
        "mcpDailySpendUsd": round(args.max_usd * 3, 2), "mcpDailyLossUsd": args.max_usd,
        "mcpMaxPositions": 3, "mcpMinEdgeCents": 3,
        "mcpAllowLiveSwitches": False, "mcpScriptsEnabled": False,
        "mcpAgents": agents + [{"id": AGENT, "name": "Live test", "enabled": True,
                                "mode": MODE, "rules": {},
                                "guide": "Test harness for the order path. Not a view on any market."}],
    }
    restore_cfg = {**base} if LIVE else {"mcpEnabled": False, "accountMode": "paper"}

    be = Backend()
    relay = None
    state: dict = {"cid": None, "side": "yes", "bought": 0.0, "cash0": None}
    try:
        step("backend")
        check("backend answers", be.rpc("ping", timeout=90))
        check("backend finished starting", be.ready.wait(120))
        if LIVE:
            check("saved wallet verified at startup", be.auth_ok,
                  "; ".join(l[:160] for l in be.logs if "auth" in l.lower())[-600:] or "no auth log lines")
        if LIVE:
            armed = [s for s in (be.rpc("terminalStanding", {"action": "list"}) or {})
                     .get("instructions") or [] if s.get("armed") and not s.get("firedAt")]
            check("no armed standing stops/take-profits of yours to wake", not armed,
                  f"{len(armed)} armed — disarm them on the Market page first" if armed else "")
        be.rpc("setConfig", {"config": test_cfg})
        st = None
        for _ in range(40):
            st = be.rpc("mcpStatus")
            if st.get("running"):
                break
            time.sleep(0.5)
        check("MCP server running", st and st.get("running"), f"port {st.get('port')}")
        row = next((a for a in st.get("agents") or [] if a.get("id") == AGENT), {})
        check("the new agent got a token without a rotate", row.get("hasToken"))
        if LIVE:
            cs = be.rpc("credentialStatus")
            check("wallet credentials present", cs.get("present", cs.get("hasCredentials", True)), json.dumps(cs)[:160])

        step("relay (what an AI client launches)")
        relay = Relay(AGENT)
        init = relay.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "live-test harness", "version": "1"}})
        relay.send("notifications/initialized", notify=True)
        check("initialize", init.get("result"), (init.get("result") or {}).get("serverInfo", {}).get("name", ""))
        tl = relay.send("tools/list")
        n_tools = len((tl.get("result") or {}).get("tools") or [])
        check("at most 40 tools listed", 0 < n_tools <= 40, f"{n_tools}")
        ok, me = relay.tool("get_my_agent")
        check("speaking as the test agent", ok and isinstance(me, dict) and me.get("id") == AGENT,
              json.dumps(me)[:200] if not ok else f"mode {me.get('mode')}")
        check(f"agent mode is {MODE}", me.get("mode") == MODE)
        ok, status = relay.tool("get_status", {"include_account": LIVE})
        check("get_status", ok)
        if LIVE:
            acct = status.get("account") or {}
            cash = num(acct.get("cashUsd", acct.get("availableUsd", acct.get("usdc"))))
            state["cash0"] = cash
            print(f"     wallet cash: {cash}  (account keys: {sorted(acct)[:12]})")
            check("wallet connected", (status.get("wallet") or {}).get("connected"))
            check("enough cash for the test", cash is not None and cash >= min(args.max_usd, 1.2),
                  f"${cash}")
            geo = geoblock()
            check("Polymarket allows trading from this connection", geo.get("blocked") is False,
                  f"{geo.get('country')}-{geo.get('region')}" if "blocked" in geo else geo.get("error", ""))

        step("pick a market")
        ok, disc = relay.tool("discover_markets", {"column": "volume", "limit": 60})
        check("discover_markets", ok)
        items = disc.get("markets") if isinstance(disc, dict) else disc
        if isinstance(disc, dict) and items is None:
            items = next((v for v in disc.values() if isinstance(v, list)), [])
        print(f"     {len(items or [])} candidates; first item keys: {sorted((items or [{}])[0])[:30]}")

        def unfit(bid, ask, mn, tick) -> str:
            if bid is None or ask is None:
                return "no bid/ask"
            if not 12 <= ask <= 30:
                return "ask outside 12-30c"
            if ask - bid > 1.0:
                return "spread over 1c"
            if (tick or 0) < 0.01:
                return "sub-cent tick"
            if (mn or 5) * math.ceil(ask) / 100 * 1.08 > args.max_usd:
                return "minimum size over the cap"
            return ""

        why: dict = {}
        short = []
        for m in items or []:
            r = unfit(num(m.get("bid")), num(m.get("ask")), num(m.get("minOrderSize")),
                      num(m.get("tickSize")))
            if m.get("conditionId") and not r:
                short.append(m)
            else:
                why[r or "no conditionId"] = why.get(r or "no conditionId", 0) + 1
        print(f"     shortlist {len(short)}; skipped {why}")
        pick = None
        for m in short[:6]:
            cid = m["conditionId"]
            ok, pv = relay.tool("preview_order", {"conditionId": cid, "side": "yes", "action": "buy",
                                                  "size": 5, "priceCents": 50, "orderType": "GTC"})
            t = (pv or {}).get("ticket") if isinstance(pv, dict) else None
            if not t:
                print(f"     preview gave no ticket: {str(pv)[:200]}")
                continue
            bid, ask, mn = num(t.get("bestBidCents")), num(t.get("bestAskCents")), num(t.get("minOrderSize")) or 5
            r = unfit(bid, ask, mn, num(t.get("tickSize")))
            if r:
                print(f"     live book moved: {r} ({bid}/{ask}) {str(m.get('title'))[:50]!r}")
                continue
            pick = (cid, m.get("title") or m.get("question"), bid, ask, mn, num(t.get("tickSize")) or 1)
            break
        check("found a tight, cheap market", pick, "" if pick else "none matched 12-30c ask, <=1c spread")
        cid, title, bid, ask, mn, tick = pick
        ask = float(math.ceil(ask))
        state["cid"] = cid
        print(f"     {title!r}  bid {bid} / ask {ask}c, min size {mn}, tick {tick}  {cid}")

        step("refusals that cost nothing")
        ok, r = relay.tool("place_order", {"conditionId": cid, "side": "yes", "action": "buy",
                                           "size": mn, "priceCents": ask, "orderType": "FAK"})
        check("a buy with no forecast is refused", not ok and "forecast" in str(r).lower(), str(r)[:140])
        fv = min(99.0, ask + 20)
        ok, f = relay.tool("record_forecast", {
            "conditionId": cid, "fairValueCents": fv, "model": "harness",
            "rationale": "Harness value to exercise the order path; not a view on this market."})
        check("record_forecast", ok and isinstance(f, dict) and f.get("forecastId"), str(f)[:160])
        fid = f["forecastId"]
        big = math.ceil((args.max_usd * 3) / (ask / 100))
        ok, r = relay.tool("place_order", {"conditionId": cid, "side": "yes", "action": "buy",
                                           "size": big, "priceCents": ask, "orderType": "FAK",
                                           "forecastId": fid})
        check("a buy over the per-order cap is refused", not ok, str(r)[:140])
        ok, r = relay.tool("close_position", {"conditionId": cid, "side": "no"})
        check("closing a position the agent never opened is refused", not ok, str(r)[:140])

        step("resting buy far below the bid -> approve -> cancel  ($0)")
        low = max(1.0, math.floor(bid) - 8)
        size = max(mn, math.ceil(1.0 / (low / 100)) if LIVE else mn)
        if size * low / 100 > args.max_usd:
            size = mn
        ok, r = relay.tool("place_order", {"conditionId": cid, "side": "yes", "action": "buy",
                                           "size": size, "priceCents": low, "orderType": "GTC",
                                           "forecastId": fid})
        check("resting buy accepted by the rails", ok, str(r)[:200])
        if LIVE:
            check("queued, nothing sent", r.get("pending") and r.get("approvalId"), str(r)[:160])
            act = be.rpc("mcpActivity", {"limit": 10})
            check("the AI Agents page lists it as pending",
                  any(p.get("id") == r["approvalId"] for p in act.get("pending") or []))
            d = be.rpc("mcpDecide", {"id": r["approvalId"], "approve": True})
            live_sent.append(f"buy {size} YES @ {low}c GTC (resting): "
                             + ("sent" if d.get("ok") else f"refused — {str(d.get('message'))[:90]}"))
            check("approved and sent", d.get("ok"), d.get("message", "")[:200])
            ok, os_ = relay.tool("get_orders", {"approvalId": r["approvalId"]})
            oid = (os_ or {}).get("orderId") if isinstance(os_, dict) else None
            check("the approval row carries the venue orderId", oid, str(os_)[:200])
            ok, mine = relay.tool("get_orders", {"mode": "live", "conditionId": cid})
            check("it rests on Polymarket as this agent's order",
                  ok and any(o.get("orderId") == oid for o in (mine or {}).get("orders") or []),
                  str(mine)[:200])
            ok, c = relay.tool("cancel_order", {"orderId": oid})
            check("the agent cancels its own order", ok and (c or {}).get("ok"), str(c)[:200])
            ok, mine = relay.tool("get_orders", {"mode": "live", "conditionId": cid})
            check("gone from the book", ok and not any(o.get("orderId") == oid
                                                       for o in (mine or {}).get("orders") or []))
        else:
            lid = (r or {}).get("localId")
            check("paper order rests", (r or {}).get("status") in ("resting", "partial"), str(r)[:160])
            ok, c = relay.tool("cancel_order", {"localId": lid})
            check("cancel paper order", ok, str(c)[:160])

        if LIVE:
            step("queued order rejected -> nothing sent  ($0)")
            ok, r = relay.tool("place_order", {"conditionId": cid, "side": "yes", "action": "buy",
                                               "size": size, "priceCents": low, "orderType": "GTC",
                                               "forecastId": fid})
            check("queued", ok and r.get("approvalId"), str(r)[:160])
            d = be.rpc("mcpDecide", {"id": r["approvalId"], "approve": False})
            ok, os_ = relay.tool("get_orders", {"approvalId": r["approvalId"]})
            check("rejected, no orderId", d.get("ok") and (os_ or {}).get("status") == "rejected"
                  and not (os_ or {}).get("orderId"), str(os_)[:160])

        if not args.resting_only:
            step("marketable buy -> approve -> fill -> agent closes it")
            fak = max(mn, math.ceil(1.0 / (ask / 100)) + 1)
            ok, pv = relay.tool("preview_order", {"conditionId": cid, "side": "yes", "action": "buy",
                                                  "size": fak, "priceCents": ask, "orderType": "FAK",
                                                  "forecastId": fid})
            check("preview: would place", ok and pv.get("wouldPlace"), str(pv)[:240])
            print(f"     cost ${pv.get('costUsd')}, edge {pv.get('edgeCentsAfterFee')}c, ticket {pv.get('ticket')}")
            ok, r = relay.tool("place_order", {"conditionId": cid, "side": "yes", "action": "buy",
                                               "size": fak, "priceCents": ask, "orderType": "FAK",
                                               "forecastId": fid})
            check("buy accepted by the rails", ok, str(r)[:200])
            if LIVE:
                state["bought"] = float(fak)
                d = be.rpc("mcpDecide", {"id": r["approvalId"], "approve": True})
                live_sent.append(f"buy {fak} YES @ {ask}c FAK: "
                                 + ("sent" if d.get("ok") else f"refused — {str(d.get('message'))[:90]}"))
                check("approved and sent", d.get("ok"), d.get("message", "")[:220])
                ok, os_ = relay.tool("get_orders", {"approvalId": r["approvalId"]})
                print(f"     approval row: {json.dumps(os_)[:260]}")
            pos, held = None, 0.0
            for _ in range(7):
                ok, pf = relay.tool("get_portfolio", {"mode": MODE, **({"reconcile": True} if LIVE else {})})
                pos = next((p for p in (pf or {}).get("positions") or []
                            if p.get("conditionId") == cid and str(p.get("side")).lower() == "yes"), None)
                held = num((pos or {}).get("sellableShares" if LIVE else "size")) or 0.0
                if held > 0 or not LIVE:
                    break
                time.sleep(5)
            state["bought"] = held if held > 0 else state["bought"]
            check("the fill shows as this agent's position", held > 0, json.dumps(pos)[:240])
            ok, cp = relay.tool("close_position", {"conditionId": cid, "side": "yes",
                                                   "maxSlippageCents": 3, "preview_only": True})
            print(f"     close preview: {json.dumps(cp)[:240]}")
            ok, cl = relay.tool("close_position", {"conditionId": cid, "side": "yes",
                                                   "maxSlippageCents": 3})
            if LIVE and ok:
                live_sent.append(f"sell YES FAK (close) -> {str((cl or {}).get('message'))[:80]}")
            check("closed by the agent, no approval needed", ok and (cl or {}).get("ok"), str(cl)[:240])
            state["bought"] = 0.0

        step("books")
        st = be.rpc("mcpStatus")
        print(f"     spent today ${st.get('spentTodayUsd')}, loss today ${st.get('lossTodayUsd')}")
        act = be.rpc("mcpActivity", {"limit": 12})
        for o in (act.get("orders") or [])[:12]:
            print(f"     #{o.get('id')} {o.get('mode')} {o.get('action')} {o.get('size')} @ "
                  f"{o.get('priceCents')} {o.get('status')} ok={o.get('ok')} {str(o.get('message'))[:70]}")
        if LIVE:
            ok, status = relay.tool("get_status", {"include_account": True})
            acct = (status or {}).get("account") or {}
            cash = num(acct.get("cashUsd", acct.get("availableUsd", acct.get("usdc"))))
            if state["cash0"] is not None and cash is not None:
                print(f"     wallet cash ${state['cash0']:.4f} -> ${cash:.4f}  (net {cash - state['cash0']:+.4f})")
        return 0
    except Abort as e:
        print(f"\nABORTED at: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {e}")
        return 1
    finally:
        step("cleanup")
        try:
            if relay is not None:
                if state["cid"] and state["bought"] > 0:
                    ok, cl = relay.tool("close_position", {"conditionId": state["cid"], "side": "yes",
                                                           "maxSlippageCents": 5})
                    print(f"     closed leftover position: {ok} {str(cl)[:160]}")
                ok, ca = relay.tool("cancel_all_orders")
                print(f"     cancel_all_orders (agent's own): {ok} {str(ca)[:160]}")
                relay.close()
        except Exception as e:  # noqa: BLE001
            print(f"     cleanup via relay failed: {e}  — check Polymarket for open orders")
        try:
            be.rpc("setConfig", {"config": restore_cfg, "pruneAgentTokens": True})
            st = be.rpc("mcpStatus")
            print(f"     config restored: account {st.get('accountMode')}, agents "
                  f"{'on' if st.get('enabled') else 'off'}, test agent token "
                  f"{'still there' if any(a.get('id') == AGENT for a in st.get('agents') or []) else 'deleted'}")
        except Exception as e:  # noqa: BLE001
            print(f"     restore failed: {e}")
        be.close()
        import re
        bad = [l for l in be.logs if l.startswith(("ERROR", "CRITICAL")) or "Traceback" in l
               or re.search(r"\b429\b", l) or "terminal: parked" in l]
        if bad:
            print("\nbackend errors / rate limits:")
            for l in bad[:12]:
                print("  ", l[:220])
        if relay is not None and relay.err:
            print("\nrelay stderr:")
            for l in relay.err[-8:]:
                print("  ", l[:220])
        print(f"\n{sum(ok for ok, _ in results)}/{len(results)} checks passed")
        if LIVE:
            print("sent live:", *(live_sent or ["nothing"]), sep="\n  - ")


if __name__ == "__main__":
    sys.exit(main())
