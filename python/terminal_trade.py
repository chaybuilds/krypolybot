"""Terminal — Act and Track.

This is the half that spends money, so it is written to a different standard
than the read side.

WHO DECIDES. The app never decides to act. Every order that leaves this module
traces to something the user did: a click, or an instruction they WROTE DOWN
(a stop loss, a take profit) that fires later. A standing instruction is gated
identically to a manual click, and every pending one is listed on one page and
cancellable there. No score, signal or strategy in this codebase can reach
``place()``.

COST BASIS. What a fill actually cost is NOT what we asked for. The requested
price ignores tick-snapping, partial fills and taker fees — every one of which
makes the naive number wrong IN THE USER'S FAVOUR, on every single row. So a
fill is reconciled against the venue's own trades ledger, and a fill that
cannot be reconciled is stored ``unreconciled``, EXCLUDED from every aggregate
figure, and counted in a warning strip. Never guessed at.

Convention: AVERAGE COST, not FIFO — what a trader means by "I am up 2x on
this". The UI says so, so nobody has to guess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import polymarket_api as api
import polymarket_auth as auth
import terminal as term
from terminal import cents, num, _now_iso

logger = logging.getLogger("krypt.terminal.trade")

MAX_MANUAL_CONTRACTS = 5000
MAX_MANUAL_USD = 5000.0
MIN_MARKETABLE_BUY_USD = 1.0
CONFIRM_TOLERANCE_USD = 0.51
MAX_STANDING_FAILURES = 3


class TerminalTradeError(Exception):
    """A refusal the user should read verbatim."""


_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> Optional[asyncio.Task]:
    """Fire-and-forget, but keep the reference until it finishes."""
    try:
        t = asyncio.ensure_future(coro)
    except RuntimeError:
        coro.close()
        return None
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


def _net() -> str:
    try:
        return auth.get_env() or "mainnet"
    except Exception:
        return "mainnet"


def _conn():
    import db
    return db.get_db()



def _fee_per_share(price_cents: float, category: Optional[str],
                   schedule: Optional[dict]) -> Optional[float]:
    """Taker fee per share, in dollars, on the fee-v2 curve.

    A market's own Gamma ``feeSchedule`` beats the category table. Returns
    None when we have neither — an unknown fee is NOT a zero fee, and quoting
    a cost that silently omits it is exactly the "wrong in the user's favour"
    failure this module exists to prevent.
    """
    p = max(0.0, min(1.0, float(price_cents) / 100.0))
    if isinstance(schedule, dict):
        if schedule.get("enabled") is False:
            return 0.0
        if schedule.get("enabled") and schedule.get("rate") is not None:
            try:
                rate = float(schedule["rate"])
                exp = float(schedule.get("exponent") or 1.0)
                return rate * ((p * (1.0 - p)) ** exp)
            except (TypeError, ValueError):
                return None
    if not category:
        return None
    try:
        import backtest
        cat = backtest.fee_category(category)
        rate = backtest.CATEGORY_TAKER_FEE[cat] if cat else None
    except Exception:
        return None
    if rate is None:
        return None
    return rate * p * (1.0 - p)



def _walk_book(levels: list[dict], want: float,
               limit_cents: float, is_buy: bool) -> tuple[Optional[float], Optional[float]]:
    """Walk resting levels up to `limit_cents` → (fillable_size, avg_cents).

    Returns (None, None) when there is no book to walk — an unknown fill is
    not a zero fill, and a ticket that shows "0 available" on a book it could
    not read is telling the user something false about the market.
    """
    if not levels:
        return None, None
    filled = 0.0
    cost = 0.0
    for lvl in levels:
        p = lvl.get("priceCents")
        sz = lvl.get("size")
        if p is None or sz is None:
            continue
        if is_buy and p > limit_cents:
            break
        if not is_buy and p < limit_cents:
            break
        take = min(float(sz), want - filled)
        if take <= 0:
            break
        filled += take
        cost += take * float(p)
        if filled >= want:
            break
    if filled <= 0:
        return 0.0, None
    return round(filled, 2), round(cost / filled, 3)


async def _available_cash_usd(paper: bool) -> Optional[float]:
    """What this account can actually spend, or None when we do not know.

    None is not zero and must never be rendered as a balance. The live figure
    is the SAME cached wallet read the header shows (`trader.refresh_balance`
    fills it on the status poll), read out of the cache rather than forced, so
    a ticket keystroke never triggers a chain of RPC calls. A cache entry
    whose last read FAILED carries a stale number under `ok: False`; that is
    reported as unknown, because a blocker built on a stale balance would
    refuse an order the wallet can afford.

    Paper cash is net of what the account's resting buys would still spend.
    A resting order is not a position, but it is a claim on the cash: without
    this, three resting buys that each fit the balance were all accepted, all
    filled, and left the account at minus a thousand dollars.
    """
    if paper:
        try:
            import terminal_paper
            import service
            pf = await terminal_paper.portfolio(dict(service.STATE.cfg or {}))
            reserved = terminal_paper.reserved_buy_usd()
        except Exception:  # noqa: BLE001 — no service (tests, CLI)
            return None
        cash = num((pf or {}).get("cashUsd"))
        return None if cash is None else round(cash - reserved, 2)
    try:
        import trader
        entry = trader._balance_cache.get(auth.get_env())
    except Exception:  # noqa: BLE001 — no trader module (tests, CLI)
        return None
    if not entry or not entry.get("ok"):
        return None
    c = num(entry.get("cents"))
    return None if c is None else round(c / 100.0, 2)



def _usd(v: Optional[float]) -> Optional[str]:
    return None if v is None else f"${v:,.2f}"


_DOLLARS_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")


def _amount_in(raw: str) -> Optional[float]:
    """The dollar figure the venue quoted back, when it quoted one."""
    m = _DOLLARS_RE.search(raw or "")
    return float(m.group(1)) if m else None


def translate_venue_error(raw: str, *, action: str,
                          cost_usd: Optional[float] = None,
                          balance_usd: Optional[float] = None) -> Optional[str]:
    """One CLOB rejection → one plain sentence, or None to keep the raw text.

    Never invents a number: every figure below is one we independently know
    (the preview's cost, the cached balance) or one the venue itself quoted.
    """
    low = (raw or "").lower()

    if "restricted in your region" in low or "geoblock" in low:
        return (
            "Polymarket refused this order: trading is restricted from your "
            "current region or connection. Nothing was placed. Orders will keep "
            "being refused until you trade from a permitted location.")

    if "balance" in low and ("not enough" in low or "allowance" in low):
        if action == "sell":
            return (
                "The exchange says this wallet holds 0 of that token. The buy "
                "is still resting (see Working below), or its fill has not "
                "settled on-chain yet — give it a moment and check Track.")
        cost = _usd(cost_usd)
        bal = _usd(balance_usd)
        if bal and cost:
            return (f"Your wallet's USDC ({bal}) is below this order's cost "
                    f"({cost}).")
        if cost:
            return (f"Your wallet's USDC balance is below this order's cost "
                    f"({cost}).")
        return "Your wallet's USDC balance is below this order's cost."

    if "invalid amount" in low:
        amt = _amount_in(raw)
        if amt is None:
            amt = cost_usd
        if amt is not None:
            return (f"Polymarket's minimum for a marketable buy is $1.00; this "
                    f"order is {_usd(amt)}.")
        return "Polymarket's minimum for a marketable buy is $1.00."

    return None


async def preview(req: dict) -> dict:
    """Everything the user should know BEFORE the money leaves.

    Deliberately verbose. A terminal that shows a price without saying whether
    it came from the live book or a cached index is asking for trust, and
    trust is exactly what we are trying not to require.
    """
    cid = str(req.get("conditionId") or "").strip()
    side = "no" if str(req.get("side") or "yes").lower() == "no" else "yes"
    action = "sell" if str(req.get("action") or "buy").lower() == "sell" else "buy"
    req_price = num(req.get("priceCents"))
    req_size = num(req.get("size"))
    order_type = str(req.get("orderType") or "GTC").upper()
    paper = bool(req.get("paper"))

    out: dict = {
        "conditionId": cid, "side": side, "action": action,
        "priceCents": None, "requestedPriceCents": req_price or 0.0,
        "size": None, "requestedSize": req_size or 0.0,
        "costUsd": None, "fillableSize": None, "estimatedAvgCents": None,
        "slippageCents": None, "feeUsd": None,
        "maxPayoutUsd": None, "maxLossUsd": None,
        "tickSize": None, "minOrderSize": None,
        "bestBidCents": None, "bestAskCents": None,
        "crossesNow": None,
        "availableUsd": None,
        "warnings": [], "blockers": [],
    }

    if not cid:
        out["blockers"].append("No market selected.")
        return out
    if req_price is None or not (1 <= req_price <= 99):
        out["blockers"].append(
            "Price must be between 1¢ and 99¢. Polymarket does not accept an "
            "order at 0 or 100.")
        return out
    if req_size is None or req_size <= 0:
        out["blockers"].append("Size must be a positive number of contracts.")
        return out
    if order_type not in ("GTC", "FAK", "FOK"):
        out["blockers"].append(f"Unsupported order type {order_type}.")
        return out
    if req_size > MAX_MANUAL_CONTRACTS:
        out["blockers"].append(
            f"{req_size:,.0f} contracts is over the manual ceiling of "
            f"{MAX_MANUAL_CONTRACTS:,}. Split it, or use a script.")
        return out

    summary = await term.market(cid, include_raw=True)
    if not summary:
        out["blockers"].append(
            "Could not read this market. Refusing to send an order into a "
            "market we cannot price.")
        return out

    raw = summary.get("_raw") or {}
    tick = num(raw.get("orderPriceMinTickSize")) or 0.01
    min_size = num(raw.get("orderMinSize"))
    out["tickSize"] = tick
    out["minOrderSize"] = min_size

    snapped = round(api._snap_price_to_tick(req_price / 100.0, tick) * 100.0, 4)
    out["priceCents"] = snapped
    out["size"] = req_size
    if abs(snapped - req_price) > 1e-6:
        out["warnings"].append(
            f"Price snapped {req_price:g}¢ → {snapped:g}¢ to match this "
            f"market's {tick:g} tick. The venue rejects anything else.")

    if summary.get("status") in ("settled", "closed"):
        out["blockers"].append(
            "This market is closed. Nothing can be traded here.")
    if raw.get("acceptingOrders") is False:
        out["blockers"].append(
            "This market is not accepting orders right now.")
    if action == "buy" and min_size is not None and req_size < min_size:
        out["blockers"].append(
            f"This market's minimum order is {math.ceil(min_size):g} contracts. "
            f"Raise your size — we will not silently buy more than you asked "
            f"for.")
    elif action == "sell" and min_size is not None and req_size < min_size:
        out["warnings"].append(
            f"You are selling {req_size:g} contracts but this market's minimum "
            f"order is {math.ceil(min_size):g}. Polymarket may refuse it — if "
            f"it does, this position can only be closed by letting it settle.")

    bk = await term.book(cid, side)
    if bk.get("available"):
        out["bestBidCents"] = num(bk.get("bestBid"))
        out["bestAskCents"] = num(bk.get("bestAsk"))
        touch = out["bestAskCents"] if action == "buy" else out["bestBidCents"]
        if touch is not None:
            out["crossesNow"] = (snapped >= touch if action == "buy"
                                 else snapped <= touch)
        levels = bk["asks"] if action == "buy" else bk["bids"]
        fillable, avg = _walk_book(levels, req_size, snapped, action == "buy")
        out["fillableSize"] = fillable
        out["estimatedAvgCents"] = avg
        if avg is not None:
            out["slippageCents"] = round(avg - snapped, 3)
        if fillable is not None and fillable < req_size:
            msg = (
                f"Only {fillable:g} of {req_size:g} contracts are available at "
                f"{snapped:g}¢ or better.")
            if order_type == "FOK":
                out["warnings"].append(
                    msg + " An all-or-none order needs the whole size, so this "
                    "will most likely be killed with no fill — which costs you "
                    "nothing.")
            elif order_type == "FAK":
                out["warnings"].append(
                    msg + " The remainder will be killed, not rested.")
            else:
                out["warnings"].append(
                    msg + " The remainder will rest on the book.")
    else:
        out["warnings"].append(
            "Could not read the order book, so there is no fill estimate — "
            f"{bk.get('note') or 'the venue did not answer'}. The order is "
            "still valid; you are just sending it blind.")

    marketable = order_type in ("FAK", "FOK") or out.get("crossesNow") is True
    if action == "buy" and marketable and snapped > 0:
        notional = req_size * snapped / 100.0
        if notional < MIN_MARKETABLE_BUY_USD - 1e-9:
            need = math.ceil(MIN_MARKETABLE_BUY_USD / (snapped / 100.0) - 1e-9)
            out["blockers"].append(
                f"Polymarket's minimum for a marketable buy is $1.00; this order is "
                f"{_usd(notional)}. Buy at least {need:g} contracts at {snapped:g}¢.")

    fillable = out["fillableSize"]
    avg = out["estimatedAvgCents"]
    if fillable is None:
        fill_size = req_size
        cost = round(req_size * snapped / 100.0, 2)
    else:
        crossed = min(float(fillable), req_size)
        rest = max(0.0, req_size - crossed)
        crossed_usd = crossed * (avg if avg is not None else snapped) / 100.0
        if order_type == "GTC":
            fill_size = req_size
            cost = round(crossed_usd + rest * snapped / 100.0, 2)
        else:
            fill_size = crossed
            cost = round(crossed_usd, 2)
    out["costUsd"] = cost

    fill_price = (round(cost * 100.0 / fill_size, 4) if fill_size > 0
                  else (avg if avg is not None else snapped))
    fee_ps = _fee_per_share(
        fill_price, summary.get("category"), api._fee_schedule_from_gamma(raw))
    if fee_ps is None:
        out["warnings"].append(
            "We could not determine this market's fee rate, so the cost above "
            "EXCLUDES fees. Taker fees on Polymarket run up to ~1.75¢ per "
            "share at even odds.")
    else:
        out["feeUsd"] = round(fee_ps * fill_size, 4)
        if order_type == "GTC":
            out["warnings"].append(
                "Resting (GTC) orders that fill as MAKER pay no fee. The fee "
                "shown is the taker case — the worst case for you.")

    if action == "buy":
        out["maxPayoutUsd"] = round(fill_size * 1.0, 2)
        out["maxLossUsd"] = round(cost + (out["feeUsd"] or 0.0), 2)
    else:
        out["maxPayoutUsd"] = cost
        out["maxLossUsd"] = round(fill_size * 1.0 - cost, 2)

    if cost > MAX_MANUAL_USD:
        out["blockers"].append(
            f"${cost:,.2f} is over the ${MAX_MANUAL_USD:,.0f} per-order manual "
            f"ceiling.")

    spread = summary.get("spread")
    if spread is not None and spread > 10:
        out["warnings"].append(
            f"The spread is {spread:.1f}¢ wide. Getting out of this position "
            f"will cost meaningfully more than getting in.")
    if summary.get("liquidityUsd") is not None and summary["liquidityUsd"] < 2000:
        out["warnings"].append(
            f"Only ${summary['liquidityUsd']:,.0f} of resting liquidity in "
            f"this market.")
    hrs = summary.get("hoursToClose")
    if hrs is not None and 0 < hrs < 1:
        out["warnings"].append(
            f"This market closes in {hrs * 60:.0f} minutes.")

    if paper:
        out["warnings"].append(
            "Paper mode: no wallet is needed and nothing reaches the exchange.")
    else:
        issue = api._deposit_wallet_issue()
        if issue:
            out["blockers"].append(issue)

    avail = await _available_cash_usd(paper)
    out["availableUsd"] = avail
    if action == "buy":
        if avail is None:
            out["warnings"].append(
                "We could not read this account's balance, so nothing here "
                "checks whether you can afford this order. The exchange will.")
        elif cost is not None and cost > avail + 1e-9:
            out["blockers"].append(
                (f"Your paper account has ${avail:,.2f} not already held by "
                 f"resting buys; this order costs ${cost:,.2f}.") if paper else
                (f"Your wallet holds ${avail:,.2f} of USDC; this order costs "
                 f"${cost:,.2f}."))

    return out



def _record_order(row: dict) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO terminal_orders
               (condition_id, title, side, action, order_type,
                requested_size, requested_price_cents, sent_size,
                sent_price_cents, order_id, status, error, origin,
                standing_id, network)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["condition_id"], row.get("title") or "", row["side"],
             row["action"], row["order_type"], row["requested_size"],
             row["requested_price_cents"], row.get("sent_size"),
             row.get("sent_price_cents"), row.get("order_id"), row["status"],
             row.get("error"), row.get("origin") or "manual",
             row.get("standing_id"), _net()),
        )
        return int(cur.lastrowid or 0)


def _paper_mode_on() -> bool:
    """Is the terminal's PAPER switch on right now?

    Read from the live config rather than passed in, so every caller of
    `place()` inherits the check — the standing-instruction tick and the chat
    bots call it directly and used to bypass the RPC handler's paper gate.
    Asked through account_live, which also reads account_mode: before the
    first setConfig the config is the raw default, where only account_mode
    says Paper. Lazy import: service imports this module lazily too."""
    try:
        import service
        cfg = service.STATE.cfg or {}
    except Exception:  # noqa: BLE001 — no service (tests, CLI) means no paper
        return False
    from config import account_live
    return not account_live(cfg) or bool(cfg.get("terminalPaperMode"))


def _definite_refusal(e: BaseException) -> bool:
    """True when a failed send PROVABLY placed nothing.

    `place()` used to file every non-ambiguous failure under 'error', which
    the agent rails read as "may have reached the venue" — so a geoblocked
    user's refused buys held their spend cap and position slots forever. These
    cases cannot have placed an order:

      * a `ValueError` from the order builder — raised before anything is
        signed or sent (bad side/price/size/type);
      * a 4xx `PolymarketAPIError` — the CLOB's own definitive "no": region
        403, insufficient balance/allowance, minimum size, invalid tick, the
        engine-restart 425, plus the local pre-send refusals the API module
        raises with a 4xx status (no market meta, EOA maker, unsupported
        protocol version);
      * the post-restart post-only refusal;
      * a connection that was never established (refused/connect-timeout/
        pool-timeout), which `polymarket_api._post_outcome_ambiguous` also
        classes as "never sent".

    Everything else (a read timeout, a dropped reply, a 5xx, anything
    unexpected) stays ambiguous: the order may be on the book.
    """
    if isinstance(e, api.OrderStatusUnknownError):
        return False
    if isinstance(e, ValueError):
        return not isinstance(e, (json.JSONDecodeError, UnicodeError))
    if isinstance(e, api.PostOnlyModeError):
        return True
    if isinstance(e, api.PolymarketAPIError):
        st = int(getattr(e, "status", 0) or 0)
        return 400 <= st < 500 and st != 408
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return False
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout,
                          httpx.PoolTimeout))


def _refused(message: str) -> dict:
    return {
        "ok": False, "orderId": None, "status": None, "filledSize": None,
        "avgPriceCents": None, "localId": None, "message": message,
    }


_MATCHED_KEYS = ("size_matched", "sizeMatched", "matched_size", "filled_size")


def _matched_size(resp: Optional[dict], action: Optional[str] = None,
                  size: Optional[float] = None,
                  limit_cents: Optional[float] = None) -> Optional[float]:
    """How many contracts the venue says crossed, or None when it did not say.

    Only SHARE counts are trusted. `makingAmount` on a buy is collateral, not
    contracts, and reading it as a size would report a fill that never
    happened — the exact class of mistake this module exists to avoid. The
    placement reply's amounts are read only by direction, given the order's
    `action` and `size`: a buy TAKES shares (`takingAmount`), a sell MAKES
    (gives) them (`makingAmount`). No field means unknown, and unknown is
    not zero.
    """
    raw = (resp or {}).get("raw")
    for src in (raw, resp):
        if not isinstance(src, dict):
            continue
        for k in _MATCHED_KEYS:
            v = num(src.get(k))
            if v is not None:
                return v
    act = str(action or "").lower()
    if act not in ("buy", "sell") or not size or not isinstance(raw, dict):
        return None
    v = num(raw.get("takingAmount" if act == "buy" else "makingAmount"))
    if v is None or v <= 0:
        return None
    cap = float(size)
    if act == "buy" and limit_cents and limit_cents > 0:
        cap = float(size) * float(limit_cents) / 0.1
    for shares in (v, v / 1e6):
        if shares <= cap * (1 + 1e-9):
            return shares
    return None


async def place(req: dict, *, origin: str = "manual",
                standing_id: Optional[str] = None) -> dict:
    """Send one hand-placed order.

    Every gate here runs in the MAIN backend process, never only in the UI.
    A renderer that lies about `confirmUsd` gets refused, because the number
    is recomputed from the same book the preview used.
    """
    if _paper_mode_on():
        return _refused(
            "Paper mode is on, so no real order was sent. Turn it off in "
            "Settings if you meant to trade live.")
    pv = await preview(req)
    if pv["blockers"]:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": pv["blockers"][0],
        }

    confirm = num(req.get("confirmUsd"))
    if confirm is None:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": "Order refused: no confirmed cost was supplied.",
        }
    actual = pv["costUsd"]
    if actual is not None and abs(actual - confirm) > CONFIRM_TOLERANCE_USD:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": (
                f"Order refused: the book moved. You confirmed ${confirm:,.2f} "
                f"but this order would now cost ${actual:,.2f}. Re-check the "
                f"ticket and confirm again."),
        }

    cid = str(req["conditionId"])
    side = pv["side"]
    action = pv["action"]
    order_type = str(req.get("orderType") or "GTC").upper()
    size = int(round(pv["size"]))
    price_cents = round(float(pv["priceCents"]), 4)

    summary = await term.market(cid)
    title = (summary or {}).get("title") or ""

    base = {
        "condition_id": cid, "title": title, "side": side, "action": action,
        "order_type": order_type,
        "requested_size": pv["requestedSize"],
        "requested_price_cents": pv["requestedPriceCents"],
        "sent_size": size, "sent_price_cents": price_cents,
        "origin": origin, "standing_id": standing_id,
    }

    try:
        resp = await api.place_limit_order(
            ticker=cid, side=side, action=action, count=size,
            price_cents=price_cents, order_type=order_type,
            client_order_id=f"term_{uuid.uuid4().hex[:12]}",
        )
    except api.OrderStatusUnknownError as e:
        local = _record_order({**base, "order_id": e.order_id, "status": "unknown",
                               "error": str(e)})
        _spawn(_reconcile_soon(e.order_id))
        return {
            "ok": False, "orderId": e.order_id, "status": "unknown",
            "filledSize": None, "avgPriceCents": None, "localId": local,
            "message": (
                f"Polymarket did not confirm this order and it could not be found "
                f"yet (order {e.order_id[:10]}…). It may be resting, filled, or "
                f"never have arrived. Check your resting orders and Track before "
                f"sending it again."),
        }
    except Exception as e:  # noqa: BLE001 — every failure must leave a row
        rejected = _definite_refusal(e)
        st = "rejected" if rejected else "error"
        local = _record_order({**base, "order_id": None, "status": st,
                               "error": f"{type(e).__name__}: {e}"})
        plain = translate_venue_error(
            str(e), action=action, cost_usd=pv.get("costUsd"),
            balance_usd=pv.get("availableUsd"))
        if not rejected:
            return {
                "ok": False, "orderId": None, "status": "error", "filledSize": None,
                "avgPriceCents": None, "localId": local,
                "message": plain or str(e),
            }
        http = int(getattr(e, "status", 0) or 0) if isinstance(
            e, api.PolymarketAPIError) else None
        return {
            "ok": False, "orderId": None, "status": "rejected",
            "filledSize": 0.0,
            "avgPriceCents": None, "localId": local,
            "message": plain or str(e),
            "httpStatus": http or None,
            "geoblocked": "restricted in your region" in str(e).lower(),
        }

    order = (resp or {}).get("order") or {}
    oid = str(order.get("order_id") or "") or None
    status = str(order.get("status") or "") or "placed"

    if not oid and status.lower() in ("unmatched", "killed", "canceled", "cancelled"):
        local = _record_order({**base, "order_id": None, "status": "unmatched",
                               "error": None})
        return {
            "ok": True, "orderId": None, "status": "unmatched",
            "filledSize": 0.0, "avgPriceCents": None, "localId": local,
            "message": (
                "Nothing crossed — the order was killed with a zero fill. "
                "That is the fill-and-kill contract working, not a failure."),
        }

    local = _record_order({**base, "order_id": oid, "status": "placed",
                           "error": None})

    _spawn(_reconcile_soon(oid))

    matched = _matched_size(resp, action, size, price_cents)
    status_l = status.lower()
    ref = pv.get("bestAskCents") if action == "buy" else pv.get("bestBidCents")
    ref_label = "ask" if action == "buy" else "bid"

    if status_l == "delayed" and not matched:
        msg = (
            f"Crossing at {price_cents:g}¢ — this market has a short matching "
            f"delay, so the fill is not confirmed yet. It is not a position "
            f"until it fills; fill details appear once it reconciles against "
            f"Polymarket's own ledger.")
    elif order_type == "GTC" and status_l not in ("matched",) and not matched:
        if ref is not None:
            msg = (
                f"Resting at {price_cents:g}¢ — the best {ref_label} is {ref:g}¢, "
                f"so nothing has filled. It is not a position until it fills; "
                f"cancel or reprice from the panel below.")
        else:
            msg = (
                f"Resting at {price_cents:g}¢ — we could not read the touch, so "
                f"whether anything crossed is unknown. It is not a position "
                f"until it fills; cancel or reprice from the panel below.")
    elif matched is not None and 0 < matched < size:
        rest = round(size - matched, 4)
        if order_type == "GTC":
            msg = (
                f"Filled {matched:g} of {size} at {price_cents:g}¢; the "
                f"remaining {rest:g} is RESTING at {price_cents:g}¢ and is not a "
                f"position until it fills — cancel or reprice it from the "
                f"panel below.")
        else:
            msg = (
                f"Filled {matched:g} of {size} at {price_cents:g}¢ or better; the "
                f"remaining {rest:g} was killed, not rested — that is what "
                f"fill-and-kill means. Fill details appear once it reconciles "
                f"against Polymarket's own ledger.")
    else:
        msg = (
            f"Order sent: {action} {size} {side.upper()} at {price_cents:g}¢ "
            f"({order_type}). Fill details appear once it reconciles against "
            f"Polymarket's own ledger.")

    return {
        "ok": True, "orderId": oid, "status": status,
        "filledSize": matched, "avgPriceCents": None, "localId": local,
        "message": msg,
    }


async def _reconcile_soon(order_id: Optional[str]) -> None:
    """Give the venue a moment to book the trade, then pull the real numbers."""
    await asyncio.sleep(3.0)
    try:
        await reconcile_fills()
    except Exception as e:  # noqa: BLE001
        logger.debug("terminal reconcile after %s failed: %s", order_id, e)


async def cancel(order_id: str) -> dict:
    oid = str(order_id or "").strip()
    if not oid:
        return {"ok": False, "message": "No order id."}
    try:
        resp = await api.cancel_order(oid)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Cancel failed: {e}"}
    resp = resp if isinstance(resp, dict) else {}
    done = {str(x).lower() for x in (resp.get("canceled") or [])}
    if oid.lower() not in done:
        nc = resp.get("not_canceled")
        why = next((str(v) for k, v in (nc.items() if isinstance(nc, dict) else ())
                    if str(k).lower() == oid.lower()), "") or "not acknowledged"
        return {"ok": False, "notCanceled": why,
                "message": (f"Polymarket did not cancel order {oid[:10]}… "
                            f"({why}). If it filled, the fill shows on Track.")}
    with _conn() as conn:
        conn.execute(
            "UPDATE terminal_orders SET status='canceled' "
            "WHERE order_id=? AND COALESCE(status,'') != 'filled'", (oid,))
    try:
        import mcp_rails
        await mcp_rails.note_cancelled(oid)
    except Exception as e:  # noqa: BLE001 — the row keeps counting in full
        logger.debug("terminal cancel: agent bookkeeping for %s failed: %s", oid, e)
    return {"ok": True, "message": f"Canceled order {oid[:10]}…"}


async def resting_orders(condition_id: Optional[str] = None) -> dict:
    """Live resting orders, from the venue — tagged with WHO placed each one.

    A terminal must never blur what you did with what an engine did, so an
    order id we have no local row for is reported as 'external', not silently
    absorbed into your manual trades.

    `condition_id` narrows the list to one market AND turns on the touch read:
    each row gains `bestTouchCents`, the price this order would have to reach
    to fill (a buy needs the best ask, a sell the best bid). That is what
    turns "resting at 14¢" into "resting at 14¢, the ask is 15¢" — the
    sentence that tells the user why nothing has happened. It costs one book
    read per outcome side, which is why it is only done for the one market the
    user is looking at and never on the Track-wide list.
    """
    try:
        data = await api._authed_request("GET", "/data/orders")
    except Exception as e:  # noqa: BLE001
        return {"orders": [], "fetchedAt": _now_iso(),
                "note": f"Could not read your open orders: {e}"}

    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {"orders": [], "fetchedAt": _now_iso(),
                "note": "The CLOB returned no order list."}

    mine: dict[str, dict] = {}
    with _conn() as conn:
        for r in conn.execute(
            "SELECT order_id, condition_id, side, title, origin, standing_id "
            "FROM terminal_orders WHERE order_id IS NOT NULL"
        ):
            mine[str(r["order_id"])] = dict(r)

    want = str(condition_id or "").strip() or None
    out = []
    for o in rows:
        if not isinstance(o, dict):
            continue
        oid = str(o.get("id") or o.get("orderID") or "")
        local = mine.get(oid)
        cid = str(o.get("market") or (local or {}).get("condition_id") or "")
        if want and cid != want:
            continue
        price = cents(o.get("price"))
        raw_side = str(o.get("side") or "").upper()
        size = num(o.get("original_size"))
        filled = num(o.get("size_matched"))
        out.append({
            "orderId": oid,
            "conditionId": cid,
            "title": (local or {}).get("title") or None,
            "side": ((local or {}).get("side")
                     or ("yes" if str(o.get("outcome") or "").lower() == "yes" else "no")),
            "action": "buy" if raw_side == "BUY" else "sell",
            "priceCents": price,
            "size": size,
            "filledSize": filled,
            "remainingSize": (None if size is None or filled is None
                              else round(size - filled, 4)),
            "bestTouchCents": None,
            "createdAt": (
                datetime.fromtimestamp(int(o["created_at"]), timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z")
                if str(o.get("created_at") or "").isdigit() else None),
            "origin": (local or {}).get("origin") or "external",
            "source": "clob",
        })

    if want and out:
        books: dict[str, dict] = {}
        for s in {r["side"] for r in out}:
            try:
                books[s] = await term.book(want, s)
            except Exception as e:  # noqa: BLE001 — a missing touch is None
                logger.debug("resting-order touch read failed (%s): %s", s, e)
        for r in out:
            bk = books.get(r["side"]) or {}
            if not bk.get("available"):
                continue
            r["bestTouchCents"] = num(
                bk.get("bestAsk") if r["action"] == "buy" else bk.get("bestBid"))

    return {"orders": out, "fetchedAt": _now_iso(),
            "note": None if out else (
                "No resting orders in this market." if want
                else "No resting orders.")}



async def _flatten_positions() -> tuple[list[tuple[str, str, float]], int, int]:
    """(OPEN sellable positions, how many held nothing, how many had settled).

    Read the way `portfolio()` reads — `strict=True`, so a failed wallet read
    RAISES rather than coming back as an empty wallet. "You hold nothing" and
    "we could not tell" must never be the same answer to a function whose next
    step is selling.

    SETTLED POSITIONS ARE NOT SELLABLE, and this is where that has to be
    decided — before any book read, not after one fails. A resolved market is
    closed on the CLOB: there is no book, so every one of them came back
    "Could not read the order book", which is a true sentence about the wrong
    question. A resolved loser is worth exactly $0 and a resolved winner is
    REDEEMED on chain, not sold. So they are dropped here and reported as one
    count with one sentence, rather than as 105 identical refusals.

    Settlement is read exactly the way `portfolio()` reads it — the market's
    `resolvedOutcome` from the same Gamma summary — so the two screens can
    never disagree about which positions are still live. A lookup that fails
    leaves the position OPEN (unknown is not settled), and the book read then
    refuses it honestly, one row at a time.
    """
    rows = await api.get_positions(strict=True)
    held: list[tuple[str, str, float]] = []
    empty = 0
    for p in rows or []:
        cid = str(p.get("ticker") or "")
        signed = float(p.get("position_fp") or 0.0)
        if not cid:
            continue
        if signed == 0:
            empty += 1
            continue
        held.append((cid, "yes" if signed > 0 else "no", abs(signed)))

    if not held:
        return [], empty, 0

    resolved: dict[str, Optional[str]] = {}
    try:
        for m in await term._summaries_for([cid for cid, _s, _z in held]):
            r = m.get("resolvedOutcome")
            resolved[str(m.get("conditionId") or "")] = r
            if r in ("yes", "no"):
                _resolution_remember(str(m.get("conditionId") or ""), str(r))
    except Exception as e:  # noqa: BLE001 — unknown, which means "still open"
        logger.debug("flatten: resolution lookup failed: %s", e)

    out = [t for t in held if resolved.get(t[0]) is None]
    return out, empty, len(held) - len(out)


def _engine_tracked_markets() -> Optional[set[str]]:
    """Condition ids some ENGINE still has an open row for.

    Read-only, and best-effort: `None` means we could not tell, which is
    reported as unknown rather than as zero. Selling one of these from here is
    fine — each engine's wallet reconcile books an externally-closed position
    as `sold` — but the user should be told it happened, because it is the one
    case where this button reaches past the terminal's own trades.
    """
    try:
        import db
        keys: set[str] = set()
        with db.get_db() as conn:
            for r in db.get_open_bot_positions(conn):
                keys.add(str(r.get("ticker") or ""))
            for r in db.get_open_crypto15m(conn, _net()):
                keys.add(str(r.get("ticker") or ""))
        keys.discard("")
        return keys
    except Exception as e:  # noqa: BLE001
        logger.debug("flatten: engine position read failed: %s", e)
        return None


async def _flatten_quote(cid: str, side: str,
                         size: float) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    """Price one exit → (request, preview, refusal).

    A refusal is a REASON, and the caller surfaces it verbatim. The order is a
    marketable FAK at the current best bid: a limit above the bid rests (and a
    resting exit is not an exit), and the walked average from the preview is
    what the confirmation gate in `place()` then re-checks.
    """
    bk = await term.book(cid, side)
    if not bk.get("available"):
        why = str(bk.get("note") or "the venue did not answer").strip().rstrip(".")
        return None, None, (
            f"Could not read the order book — {why}. "
            f"Nothing was sold here rather than selling blind.")
    bid = num(bk.get("bestBid"))
    if bid is None:
        return None, None, (
            "Nobody is bidding on this side right now, so there is nothing to "
            "sell into. Left open.")

    req: dict = {
        "conditionId": cid, "side": side, "action": "sell",
        "priceCents": max(1.0, min(99.0, bid)), "size": size,
        "orderType": "FAK",
    }
    pv = await preview(req)
    if pv["blockers"]:
        return req, pv, pv["blockers"][0]
    min_size = pv.get("minOrderSize")
    if min_size is not None and size < min_size:
        return req, pv, (
            f"{size:g} contract(s) is below this market's minimum order of "
            f"{math.ceil(min_size):g}, so Polymarket would reject the sell. "
            f"This position can only be closed by letting it settle.")
    req["confirmUsd"] = pv.get("costUsd")
    return req, pv, None


def _flatten_partial_note(pv: Optional[dict]) -> Optional[str]:
    """The one preview warning that changes what the user still holds."""
    for w in ((pv or {}).get("warnings") or []):
        if "are available at" in w:
            return w
    return None


async def flatten_preview() -> dict:
    """What `flatten_wallet` would sell, and roughly for how much.

    Read-only — it prices, it does not send. Every figure is an ESTIMATE off a
    book that is already a moment old, and it says so; an unknown one is null
    so the caller prints an em dash rather than a confident number.
    """
    out: dict = {
        "ok": False, "positions": [], "count": 0, "skipped": 0,
        "settledSkipped": 0,
        "proceedsUsd": None, "engineTracked": None,
        "paper": _paper_mode_on(), "message": "",
    }
    try:
        targets, empty, settled = await _flatten_positions()
    except Exception as e:  # noqa: BLE001
        out["message"] = (
            f"Could not read your positions ({type(e).__name__}: {e}), so "
            f"there is nothing to show. Nothing has been sold.")
        return out

    out["skipped"] = empty
    out["settledSkipped"] = settled
    tracked = _engine_tracked_markets()
    proceeds = 0.0
    proceeds_known = True
    sellable = 0
    tracked_n = 0

    for cid, side, size in targets:
        try:
            _req, pv, refusal = await _flatten_quote(cid, side, size)
        except Exception as e:  # noqa: BLE001
            pv, refusal = None, (
                f"Could not price this market ({type(e).__name__}: {e}).")
        row = {
            "conditionId": cid, "side": side, "size": round(size, 2),
            "bidCents": (pv or {}).get("priceCents"),
            "fillableSize": (pv or {}).get("fillableSize"),
            "proceedsUsd": None if refusal else (pv or {}).get("costUsd"),
            "blocked": refusal,
            "partial": None if refusal else _flatten_partial_note(pv),
            "engineTracked": None if tracked is None else (cid in tracked),
        }
        out["positions"].append(row)
        if refusal:
            continue
        sellable += 1
        if tracked is not None and cid in tracked:
            tracked_n += 1
        if row["proceedsUsd"] is None:
            proceeds_known = False
        else:
            proceeds += float(row["proceedsUsd"])

    out["count"] = sellable
    out["engineTracked"] = None if tracked is None else tracked_n
    out["proceedsUsd"] = round(proceeds, 2) if proceeds_known else None
    out["ok"] = True
    settled_note = (
        f" {settled} settled position(s) were left out: a resolved market is "
        f"closed on the exchange, so there is no book to sell into — a loser "
        f"is worth $0 and a winner is redeemed rather than sold. They are in "
        f"the settled record, not in this list."
        if settled else "")
    if not targets and settled:
        out["message"] = (
            f"Nothing to sell — every position in the wallet is settled. All "
            f"{settled} of them: a resolved market is closed on the exchange, "
            f"so there is no book to sell into — a loser is worth $0 and a "
            f"winner is redeemed rather than sold. They are in the settled "
            f"record, not in this list.")
    elif not targets:
        out["message"] = "Nothing to sell — the wallet holds no open positions."
    elif not sellable:
        out["message"] = (
            f"{len(targets)} open position(s) held, none of them sellable "
            f"right now — see the reason on each." + settled_note)
    else:
        out["message"] = (
            f"{sellable} position(s) would be sold at the best bid. The "
            f"proceeds figure is an estimate off the book as it is now, not a "
            f"quote." + settled_note)
    return out


async def flatten_wallet(*, allow_trading: bool = True, origin: str = "manual",
                         reason: str = "flatten") -> dict:
    """Sell every open position in the wallet, one at a time.

    Counts: ``attempted`` is how many exits were sent or refused by us,
    ``sold`` and ``refused`` split those two ways (a venue rejection, and a
    fill-and-kill that crossed nothing, count as refused — the position is
    still open either way, which is the only thing
    the user cares about), and ``skipped`` is rows that held nothing to begin
    with. ``proceedsUsd`` is the ESTIMATE from the previews, null when any
    accepted sell could not be priced — the real number arrives when the fills
    reconcile against Polymarket's own ledger.

    Serialised deliberately. There is no burst to be gained: the CLOB rate
    limits us anyway, and firing twenty signed sells at once turns one bad
    read into twenty.
    """
    base: dict = {
        "ok": False, "attempted": 0, "sold": 0, "refused": 0, "skipped": 0,
        "settledSkipped": 0,
        "proceedsUsd": None, "engineTrackedSold": None, "reason": reason,
        "results": [], "message": "",
    }

    if _paper_mode_on():
        return {**base, "message": (
            "Paper mode is on, so nothing was sold. Not one order left this "
            "machine. Turn paper mode off in Settings if you meant to flatten "
            "for real.")}
    if not allow_trading:
        return {**base, "message": (
            "Live trading is off, so nothing was sold. Flatten is held to the "
            "same switch as every other order.")}

    try:
        targets, empty, settled = await _flatten_positions()
    except Exception as e:  # noqa: BLE001
        return {**base, "message": (
            f"Could not read your positions ({type(e).__name__}: {e}), so "
            f"NOTHING was sold. Selling against a wallet we cannot read is "
            f"worse than waiting — try again in a moment.")}

    if not targets:
        return {**base, "ok": True, "skipped": empty, "settledSkipped": settled,
                "proceedsUsd": 0.0, "engineTrackedSold": 0,
                "message": (
                    f"Nothing to sell — every position in the wallet is "
                    f"settled. All {settled} of them are resolved markets, "
                    f"which are closed on the exchange: a loser is worth $0 "
                    f"and a winner is redeemed rather than sold."
                    if settled else
                    "Nothing to sell — the wallet holds no open positions.")}

    tracked = _engine_tracked_markets()
    results: list[dict] = []
    sold = refused = tracked_sold = 0
    proceeds = 0.0
    proceeds_known = True

    for cid, side, size in targets:
        try:
            req, pv, refusal = await _flatten_quote(cid, side, size)
        except Exception as e:  # noqa: BLE001
            req, pv, refusal = None, None, (
                f"Could not price this market ({type(e).__name__}: {e}), so it "
                f"was left alone.")
        if refusal or req is None:
            refused += 1
            results.append({"conditionId": cid, "side": side,
                            "size": round(size, 2), "ok": False,
                            "message": refusal or "Refused."})
            continue

        res = await place(req, origin=origin)
        ok = bool(res.get("ok"))
        msg = str(res.get("message") or "")
        if ok and (str(res.get("status") or "").lower() == "unmatched"
                   or num(res.get("filledSize")) == 0):
            ok = False
            msg = (f"{msg} Nothing sold — this position is still open.").strip()
        if ok:
            sold += 1
            if tracked is not None and cid in tracked:
                tracked_sold += 1
            est = pv.get("costUsd") if pv else None
            if est is None:
                proceeds_known = False
            else:
                proceeds += float(est)
            partial = _flatten_partial_note(pv)
            if partial:
                msg = f"{msg} {partial}".strip()
        else:
            refused += 1
        results.append({"conditionId": cid, "side": side,
                        "size": round(size, 2), "ok": ok, "message": msg})

    attempted = sold + refused
    proceeds_usd = round(proceeds, 2) if proceeds_known else None

    parts = [f"Sold {sold} of {attempted} open position(s) at the best bid"]
    parts.append(
        f" for about {proceeds_usd:,.2f} USDC (estimated from the book — the "
        f"exact figure lands when the fills reconcile)."
        if proceeds_usd is not None else
        " — the proceeds could not be estimated, so the total is unknown "
        "until the fills reconcile.")
    if refused:
        parts.append(
            f" {refused} could not be sold; each one says why, and those "
            f"positions are still open.")
    if empty:
        parts.append(f" {empty} wallet row(s) held nothing and were skipped.")
    if settled:
        parts.append(
            f" {settled} settled position(s) were left alone: a resolved "
            f"market is closed on the exchange, so a loser is worth $0 and a "
            f"winner is redeemed rather than sold.")
    if tracked is None:
        parts.append(
            " We could not read the engines' own position tables, so how many "
            "of these an engine was tracking is unknown — each engine's wallet "
            "reconcile will book its position as sold on its next pass.")
    elif tracked_sold:
        parts.append(
            f" {tracked_sold} of the sold position(s) were being tracked by an "
            f"engine (main, copy, script or crypto); their wallet reconcile "
            f"books an externally-closed position as sold, so nothing is lost "
            f"from their record.")

    logger.info("terminal: flatten (%s) sold %d/%d, %d refused",
                reason, sold, attempted, refused)

    return {
        "ok": refused == 0, "attempted": attempted, "sold": sold,
        "refused": refused, "skipped": empty, "settledSkipped": settled,
        "proceedsUsd": proceeds_usd,
        "engineTrackedSold": None if tracked is None else tracked_sold,
        "reason": reason, "results": results, "message": "".join(parts),
    }


DEFAULT_SELL_SLIPPAGE_CENTS = 5
MAX_SELL_SLIPPAGE_CENTS = 50
_DEPTH_WARN_FRACTION = 0.25

_SELL_ESTIMATE_NOTE = (
    "Every figure here is an estimate off the book as it is now, not a quote. "
    "The fill you actually get depends on what is still resting when the order "
    "lands.")


def _walk_deepest(levels: list[dict], want: float,
                  floor_cents: float) -> Optional[float]:
    """The deepest (lowest) bid level a sell walk of `want` actually touches.

    Deliberately the same traversal as `_walk_book(..., is_buy=False)` — it
    answers the one question that walk does not: how far down did we have to
    go? That price, not the best bid, is what the limit has to be, because a
    limit at the touch only crosses the touch.
    """
    filled = 0.0
    deepest: Optional[float] = None
    for lvl in levels:
        p = lvl.get("priceCents")
        sz = lvl.get("size")
        if p is None or sz is None:
            continue
        if p < floor_cents:
            break
        take = min(float(sz), want - filled)
        if take <= 0:
            break
        filled += take
        deepest = float(p)
        if filled >= want:
            break
    return deepest


def _sell_blank(cid: str, side: str, paper: bool) -> dict:
    return {
        "conditionId": cid, "side": side, "sideLabel": None, "title": None,
        "paper": paper,
        "positionSize": None, "requestedSize": None,
        "size": None, "fillableSize": None,
        "bestBidCents": None, "limitCents": None,
        "estimatedAvgCents": None, "worstFillCents": None,
        "maxSlippageCents": DEFAULT_SELL_SLIPPAGE_CENTS,
        "proceedsUsd": None, "feeUsd": None, "netProceedsUsd": None,
        "avgCostCents": None, "basisSource": None, "costBasisUsd": None,
        "estimatedPnlUsd": None,
        "markCents": None, "settled": False, "resolvedOutcome": None,
        "minOrderSize": None, "tickSize": None,
        "warnings": [], "blockers": [], "note": _SELL_ESTIMATE_NOTE,
    }


async def _position_for(cid: str, side: str, paper: bool) -> Optional[dict]:
    """The one position row for this market + side, from the right book.

    Paper reads the PAPER ledger and live reads the wallet. They are never
    mixed: a paper number added to a real one is the single worst thing a
    simulator can do.
    """
    if paper:
        import terminal_paper
        cfg = None
        try:
            import service
            cfg = service.STATE.cfg
        except Exception:  # noqa: BLE001 — no service (tests, CLI)
            cfg = None
        pf = await terminal_paper.portfolio(cfg)
    else:
        pf = await portfolio()
    if pf.get("walletReadable") is False:
        raise TerminalTradeError(
            str(pf.get("unreconciledNote") or "the wallet did not answer"))
    for p in (pf.get("positions") or []):
        if (str(p.get("conditionId") or "") == cid
                and str(p.get("side") or "") == side):
            return p
    return None


async def sell_position_preview(req: dict) -> dict:
    """Price a marketable exit out of one position. Read-only.

    ``req``: ``conditionId``, ``side``, optional ``size`` (default: the whole
    position), optional ``maxSlippageCents`` (default 5), ``paper``.
    """
    cid = str(req.get("conditionId") or "").strip()
    side = "no" if str(req.get("side") or "yes").lower() == "no" else "yes"
    paper = bool(req.get("paper"))
    out = _sell_blank(cid, side, paper)

    slip = num(req.get("maxSlippageCents"))
    slip = (DEFAULT_SELL_SLIPPAGE_CENTS if slip is None
            else max(0.0, min(float(MAX_SELL_SLIPPAGE_CENTS), float(slip))))
    out["maxSlippageCents"] = slip

    if not cid:
        out["blockers"].append("No market selected.")
        return out

    try:
        pos = await _position_for(cid, side, paper)
    except TerminalTradeError as e:
        out["blockers"].append(
            f"Could not read your {'paper ' if paper else ''}positions — {e} "
            f"So what you hold here is unknown, not nothing. Nothing was "
            f"priced.")
        return out
    except Exception as e:  # noqa: BLE001 — unreadable is not empty
        out["blockers"].append(
            f"Could not read your {'paper ' if paper else ''}positions "
            f"({type(e).__name__}: {e}), so what you hold here is unknown, not "
            f"nothing. Nothing was priced.")
        return out

    held = float((pos or {}).get("size") or 0.0)
    if not pos or held <= 0:
        out["blockers"].append(
            f"You hold no {'paper ' if paper else ''}contracts on this side of "
            f"this market, so there is nothing to sell.")
        return out

    out["positionSize"] = round(held, 2)
    out["sideLabel"] = pos.get("sideLabel")
    out["title"] = pos.get("title")
    out["avgCostCents"] = pos.get("avgCostCents")
    out["basisSource"] = (
        "paper" if paper else pos.get("basisSource"))
    out["costBasisUsd"] = pos.get("costBasisUsd")
    out["markCents"] = pos.get("markCents")

    resolved = pos.get("resolvedOutcome")
    out["resolvedOutcome"] = resolved
    out["settled"] = bool(pos.get("settled")) or resolved is not None
    if out["settled"]:
        won = resolved == side
        out["blockers"].append(
            "This market has resolved — a won position is redeemed, not sold. "
            "There is no book to sell into on a closed market."
            if won else
            "This market has resolved and this side lost, so there is nothing "
            "to sell — these contracts are worth $0.00.")
        return out

    want_raw = num(req.get("size"))
    if want_raw is not None and want_raw <= 0:
        out["blockers"].append("Size must be a positive number of contracts.")
        return out
    requested = held if want_raw is None else float(want_raw)
    out["requestedSize"] = round(requested, 2)
    if requested > held + 1e-9:
        out["warnings"].append(
            f"You asked to sell {requested:g} contracts but hold {held:g}, so "
            f"this is sized to {held:g}. Polymarket has no naked short.")
        requested = held
    want = math.floor(requested + 1e-9)
    if want <= 0:
        out["blockers"].append(
            f"This position is {held:g} contract(s) — less than one whole "
            f"contract, which is the smallest size an order can carry. It can "
            f"only be closed by letting the market settle.")
        return out
    if want < requested - 1e-9:
        out["warnings"].append(
            f"Orders carry whole contracts, so {requested - want:g} will be "
            f"left behind.")
    out["size"] = float(want)

    summary = await term.market(cid, include_raw=True)
    if not summary:
        out["blockers"].append(
            "Could not read this market. Refusing to send an order into a "
            "market we cannot price.")
        return out
    raw = summary.get("_raw") or {}
    tick = num(raw.get("orderPriceMinTickSize")) or 0.01
    min_size = num(raw.get("orderMinSize"))
    out["tickSize"] = tick
    out["minOrderSize"] = min_size
    if summary.get("status") in ("settled", "closed"):
        out["blockers"].append(
            "This market is closed. Nothing can be traded here.")
        return out
    if raw.get("acceptingOrders") is False:
        out["blockers"].append(
            "This market is not accepting orders right now.")
        return out

    bk = await term.book(cid, side)
    if not bk.get("available"):
        why = str(bk.get("note") or "the venue did not answer").strip().rstrip(".")
        out["blockers"].append(
            f"Could not read the order book — {why}. Nothing was priced here "
            f"rather than selling blind.")
        return out
    bids = bk.get("bids") or []
    best_bid = num(bk.get("bestBid"))
    if best_bid is None or not bids:
        out["blockers"].append(
            "Nobody is bidding on this side right now, so there is nothing to "
            "sell into.")
        return out
    out["bestBidCents"] = best_bid

    floor_cents = max(1.0, float(math.ceil(best_bid - slip)))
    fillable, avg = _walk_book(bids, float(want), floor_cents, False)
    if fillable is None:
        out["blockers"].append(
            "There is no book to walk on this side, so the fill cannot be "
            "estimated. Nothing was priced.")
        return out

    sell_size = math.floor(float(fillable) + 1e-9)
    if sell_size <= 0:
        out["blockers"].append(
            f"Nothing is bidding within {slip:g}¢ of the best bid "
            f"({best_bid:g}¢), so no whole contract can be sold at that "
            f"tolerance. Widen the slippage, or wait for a bid.")
        return out
    if sell_size < float(fillable) - 1e-9:
        fillable, avg = _walk_book(bids, float(sell_size), floor_cents, False)
    deepest = _walk_deepest(bids, float(sell_size), floor_cents)
    if avg is None or deepest is None:
        out["blockers"].append(
            "The book could not be walked for this size, so there is no fill "
            "estimate. Nothing was priced.")
        return out

    out["fillableSize"] = float(sell_size)
    out["estimatedAvgCents"] = avg
    limit_cents = float(max(1.0, min(99.0, math.floor(deepest))))
    if limit_cents < floor_cents:
        limit_cents = floor_cents
    out["limitCents"] = limit_cents
    out["worstFillCents"] = limit_cents

    if sell_size < want:
        out["warnings"].append(
            f"Only {sell_size:g} of {want:g} contracts fit within {slip:g}¢ of "
            f"the {best_bid:g}¢ bid, so the order is sized to {sell_size:g} — "
            f"nothing is left resting. The other "
            f"{want - sell_size:g} stay in your position.")

    visible = sum(float(l.get("size") or 0.0) for l in bids)
    if visible > 0 and sell_size / visible > _DEPTH_WARN_FRACTION:
        out["warnings"].append(
            f"This sell eats {sell_size / visible * 100:.0f}% of the visible "
            f"bid depth. An order that size moves the book, so the real fill "
            f"is likely worse than this estimate.")

    if min_size is not None and sell_size < min_size:
        out["blockers"].append(
            f"{sell_size:g} contract(s) is below this market's minimum order "
            f"of {math.ceil(min_size):g}, so Polymarket would reject the sell. "
            f"This position can only be closed by letting it settle.")
        return out

    proceeds = round(sell_size * avg / 100.0, 2)
    out["proceedsUsd"] = proceeds
    if proceeds > MAX_MANUAL_USD:
        out["blockers"].append(
            f"${proceeds:,.2f} is over the ${MAX_MANUAL_USD:,.0f} per-order "
            f"manual ceiling. Sell it in pieces.")
        return out
    if sell_size > MAX_MANUAL_CONTRACTS:
        out["blockers"].append(
            f"{sell_size:,.0f} contracts is over the manual ceiling of "
            f"{MAX_MANUAL_CONTRACTS:,}. Sell it in pieces.")
        return out

    fee_ps = _fee_per_share(
        avg, summary.get("category"), api._fee_schedule_from_gamma(raw))
    if fee_ps is None:
        out["warnings"].append(
            "We could not determine this market's fee rate, so the proceeds "
            "above EXCLUDE fees and the P&L below is gross of them. Taker fees "
            "on Polymarket run up to ~1.75¢ per share at even odds.")
    else:
        out["feeUsd"] = round(fee_ps * sell_size, 4)
        out["netProceedsUsd"] = round(proceeds - out["feeUsd"], 2)

    avg_cost = num(out["avgCostCents"])
    if avg_cost is None:
        out["warnings"].append(
            "There is no cost basis for these contracts, so the profit or loss "
            "on this sell is unknown — not zero.")
    else:
        net = out["netProceedsUsd"]
        out["estimatedPnlUsd"] = round(
            (net if net is not None else proceeds)
            - sell_size * avg_cost / 100.0, 2)

    hrs = summary.get("hoursToClose")
    if hrs is not None and 0 < hrs < 1:
        out["warnings"].append(
            f"This market closes in {hrs * 60:.0f} minutes.")

    return out


def _sell_refused(message: str, pv: Optional[dict] = None) -> dict:
    return {
        "ok": False, "orderId": None, "status": None, "localId": None,
        "sentSize": None, "limitCents": None,
        "filledSize": None, "avgPriceCents": None, "proceedsUsd": None,
        "killedSize": None, "estimatedProceedsUsd": None,
        "message": message, "preview": pv,
    }


async def sell_position(req: dict, *, origin: str = "manual") -> dict:
    """Send ONE marketable fill-and-kill that closes (part of) a position.

    Same request as the preview, plus ``confirmUsd`` — the proceeds figure the
    user actually read. It is re-derived here and the order is refused if the
    book moved, exactly as `place()` does for the ticket: the confirmation is a
    contract about a number, not a checkbox.
    """
    paper = bool(req.get("paper"))
    pv = await sell_position_preview(req)
    if pv["blockers"]:
        return _sell_refused(pv["blockers"][0], pv)

    confirm = num(req.get("confirmUsd"))
    if confirm is None:
        return _sell_refused(
            "Sell refused: no confirmed proceeds figure was supplied.", pv)
    proceeds = pv["proceedsUsd"]
    if proceeds is not None and abs(proceeds - confirm) > CONFIRM_TOLERANCE_USD:
        return _sell_refused(
            f"Sell refused: the book moved. You confirmed ${confirm:,.2f} but "
            f"this sell would now fetch ${proceeds:,.2f}. Re-check the panel "
            f"and confirm again.", pv)

    size = float(pv["fillableSize"] or 0.0)
    order = {
        "conditionId": pv["conditionId"], "side": pv["side"], "action": "sell",
        "priceCents": pv["limitCents"], "size": size, "orderType": "FAK",
        "confirmUsd": proceeds, "paper": paper,
    }

    if paper:
        import terminal_paper
        cfg = None
        try:
            import service
            cfg = service.STATE.cfg
        except Exception:  # noqa: BLE001
            cfg = None
        res = await terminal_paper.place(order, cfg)
    else:
        res = await place(order, origin=origin)

    filled = num(res.get("filledSize"))
    fill_avg = num(res.get("avgPriceCents"))
    got = (round(filled * fill_avg / 100.0, 2)
           if (filled is not None and fill_avg is not None) else None)
    killed = (round(size - filled, 2)
              if filled is not None else None)

    return {
        "ok": bool(res.get("ok")),
        "orderId": res.get("orderId"),
        "status": res.get("status"),
        "localId": res.get("localId"),
        "sentSize": size,
        "limitCents": pv["limitCents"],
        "filledSize": filled,
        "avgPriceCents": fill_avg,
        "proceedsUsd": got,
        "killedSize": killed,
        "estimatedProceedsUsd": proceeds,
        "message": str(res.get("message") or ""),
        "preview": pv,
    }



_FILL_MAX_PAGES = 25
_FILL_PAGE = 100


async def _walk_trades(max_pages: int = _FILL_MAX_PAGES) -> tuple[list, bool]:
    """Every authed fill we can reach, oldest page last.

    Returns ``(rows, complete)``. The CLOB paginates ``/data/trades`` with an
    opaque ``next_cursor`` and signals the end with ``LTE=``. A single
    un-paginated call only ever saw the most recent page, so any position
    opened before that window could never be reconciled — it stayed
    ``unreconciled`` forever, excluded from every total, with no way to
    backfill it.
    """
    rows, complete, _ = await _walk_pages("", max_pages)
    return rows, complete


async def _walk_pages(cursor: str, max_pages: int) -> tuple[list, bool, str]:
    """`_walk_trades` from `cursor` → ``(rows, complete, next_cursor)``.
    ``next_cursor`` is where a walk that hit the page cap would go on."""
    rows: list = []
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        params: dict = {"limit": _FILL_PAGE}
        if cursor:
            params["next_cursor"] = cursor
        data = await api._authed_request("GET", "/data/trades", params=params)
        if isinstance(data, dict):
            page = data.get("data") or []
            nxt = str(data.get("next_cursor") or "")
        elif isinstance(data, list):
            return rows + data, True, ""
        else:
            return rows, False, ""
        rows.extend(page)
        if not page or not nxt or nxt == "LTE=" or nxt in seen_cursors:
            return rows, True, ""
        seen_cursors.add(nxt)
        cursor = nxt
    return rows, False, cursor


_FILL_RESUME_KV = "terminal:fills:resume:"


def _fill_resume_key() -> str:
    """Per network and wallet: a cursor into one account's history means
    nothing in another's."""
    try:
        addr = str(auth.trading_address() or "").lower()
    except Exception:  # noqa: BLE001
        addr = ""
    return f"{_FILL_RESUME_KV}{_net()}:{addr}"


async def _walk_history() -> tuple[list, bool]:
    """The newest fills, plus the next stretch of the older history.

    A walk always starts at the newest fill, so on a wallet past the page cap
    every pass re-read the same newest pages: anything older could never be
    imported, while the note told the user to run it again to continue. Where
    the walk stopped is now remembered, and each following pass also walks on
    from there until it reaches the oldest fill; then the cycle starts over,
    so old fills are still re-derived in turn. ``complete`` means the whole
    history has been read, across passes, at least once.
    """
    import db
    key = _fill_resume_key()
    end_key = key + ":end"
    rows, complete, nxt = await _walk_pages("", _FILL_MAX_PAGES)
    with _conn() as conn:
        resume = db.kv_get(conn, key)
        reached_end = bool(db.kv_get(conn, end_key))
        if complete or not resume:
            if complete:
                db.kv_delete(conn, key)
            elif nxt:
                db.kv_set(conn, key, nxt)
            return rows, complete or reached_end
    try:
        older, done, nxt = await _walk_pages(resume, _FILL_MAX_PAGES)
    except Exception as e:  # noqa: BLE001
        logger.debug("fill backfill from a saved cursor failed: %s", e)
        older, done, nxt = [], False, ""
    with _conn() as conn:
        if done or not nxt:
            db.kv_delete(conn, key)
        else:
            db.kv_set(conn, key, nxt)
        if done:
            db.kv_set(conn, end_key, "1")
    seen: set[str] = set()
    out: list = []
    for t in rows + older:
        tid = str(t.get("id") or t.get("trade_id") or "") if isinstance(t, dict) else ""
        if tid and tid in seen:
            continue
        seen.add(tid)
        out.append(t)
    return out, done or reached_end


_YES_WORDS = {"yes", "up", "over", "above", "long", "win", "true"}
_NO_WORDS = {"no", "down", "under", "below", "short", "lose", "false"}


def _side_from_token(asset_id: str, outcome_label: str,
                     summary: Optional[dict]) -> Optional[str]:
    """Which of a market's two outcomes a fill was for → 'yes' | 'no' | None.

    Mapped through the OUTCOME INDEX, never through the label text. This was
    the single biggest wrong number on the Track screen: the old rule was
    literally ``"yes" if outcome == "yes" else "no"``, so every market whose
    first outcome is not spelled "Yes" — every "Up or Down" crypto market,
    every over/under, every named-team sports market — had its buys filed
    under 'no' while the position itself (derived from ``outcomeIndex`` via
    ``position_fp``) sat under 'yes'. Buys and sells landed on different keys,
    so the position had no cost basis at all and was dropped from every total.

    Order of authority: the CLOB token id (exact), then the market's own
    ``outcomes`` list (exact), then a known vocabulary, then unknown. Unknown
    is a real answer here — a fill we cannot attribute to an outcome is left
    out rather than filed under a guess.
    """
    tid = str(asset_id or "").strip()
    if summary and tid:
        if tid == str(summary.get("yesTokenId") or ""):
            return "yes"
        if tid == str(summary.get("noTokenId") or ""):
            return "no"
    lab = str(outcome_label or "").strip().lower()
    outs = (summary or {}).get("outcomes")
    if lab and isinstance(outs, (list, tuple)) and len(outs) >= 2:
        if lab == str(outs[0]).strip().lower():
            return "yes"
        if lab == str(outs[1]).strip().lower():
            return "no"
    if lab in _YES_WORDS:
        return "yes"
    if lab in _NO_WORDS:
        return "no"
    return None


def _our_leg(t: dict, addr: str) -> Optional[dict]:
    """Which side of a match WE were on → asset, outcome, price, size, role.

    ``/data/trades`` reports every match FROM THE TAKER'S POINT OF VIEW. The
    top-level ``asset_id``, ``outcome``, ``price``, ``size`` and ``side`` are
    the taker's order; when we were the MAKER, our own leg is one of the
    entries in ``maker_orders`` — on a different token, at a different price,
    and possibly in a different direction.

    Reading the top level unconditionally recorded, for one real trade in this
    wallet, "bought 10 DOWN at 70¢" for what was actually "bought 10 UP at
    30¢": wrong outcome, wrong price, and therefore a cost of $7.00 booked
    against $3.00 actually spent. On a wallet that rests a lot of orders that
    is a systematically wrong basis, not a rounding error.

    ``role`` is the venue's own ``trader_side`` where it gives one, because it
    also settles the fee question outright: a maker pays nothing, a taker pays
    the curve, and neither has to be inferred from whether an order id happened
    to match a local row. Returns None when we cannot tell which leg was ours —
    unknown, not guessed.
    """
    role = str(t.get("trader_side") or "").upper()
    makers = [m for m in (t.get("maker_orders") or []) if isinstance(m, dict)]
    ours = [m for m in makers
            if addr and str(m.get("maker_address") or "").lower() == addr]

    if role == "MAKER" or (role != "TAKER" and ours):
        if not ours:
            if len(makers) != 1:
                return None
            ours = makers
        assets = {str(m.get("asset_id") or "") for m in ours}
        sides = {str(m.get("side") or "").upper() for m in ours}
        if len(assets) != 1 or len(sides) != 1:
            return None
        size = 0.0
        notional = 0.0
        for m in ours:
            sz = num(m.get("matched_amount"))
            px = cents(m.get("price"))
            if sz is None or px is None:
                return None
            size += sz
            notional += sz * px
        if size <= 0:
            return None
        return {
            "asset_id": assets.pop(),
            "outcome": str(ours[0].get("outcome") or ""),
            "price": round(notional / size, 4),
            "size": size,
            "action": sides.pop(),
            "role": "maker",
        }

    return {
        "asset_id": str(t.get("asset_id") or ""),
        "outcome": str(t.get("outcome") or ""),
        "price": cents(t.get("price")),
        "size": num(t.get("size")),
        "action": str(t.get("side") or "BUY").upper(),
        "role": "taker" if role == "TAKER" else "unknown",
    }


async def reconcile_fills(limit: int = 0) -> dict:
    """Pull our authed trade history and write it into terminal_fills.

    Idempotent: keyed on the venue's own trade id. This is the only writer of
    cost basis in the terminal — nothing here trusts what we asked for.
    """
    try:
        rows, complete = await _walk_history()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "inserted": 0, "matched": 0,
                "note": f"Could not read your fills: {e}"}

    if not isinstance(rows, list):
        return {"ok": False, "inserted": 0, "matched": 0,
                "note": "The CLOB returned no trade list."}
    if limit:
        rows = rows[:int(limit)]

    local_by_oid: dict[str, dict] = {}
    with _conn() as conn:
        for r in conn.execute(
            "SELECT order_id, condition_id, title, side, origin "
            "FROM terminal_orders WHERE order_id IS NOT NULL"
        ):
            local_by_oid[str(r["order_id"])] = dict(r)
        import db as _db
        floor = _db.terminal_reconcile_floor(conn)

    cids = {str(t.get("market") or "") for t in rows[:limit or len(rows)]
            if isinstance(t, dict) and t.get("market")}
    cids |= {str(v.get("condition_id") or "") for v in local_by_oid.values()}
    market_by_id: dict[str, dict] = {}
    try:
        for m in await term._summaries_for([c for c in cids if c]):
            market_by_id[m["conditionId"]] = m
    except Exception as e:  # noqa: BLE001 — a miss just means an unknown fee
        logger.debug("reconcile: market lookup failed: %s", e)

    inserted = 0
    skipped = 0
    unmapped = 0
    corrected = 0
    failed_trades = 0
    unwound = 0
    net = _net()
    try:
        our_addr = str(auth.trading_address() or "").lower()
    except Exception:  # noqa: BLE001
        our_addr = ""
    with _conn() as conn:
        prior = {str(r["trade_id"]): (str(r["side"]), str(r["action"]),
                                      float(r["size"] or 0.0),
                                      float(r["price_cents"] or 0.0),
                                      r["fees_usd"])
                 for r in conn.execute(
                     "SELECT trade_id, side, action, size, price_cents, fees_usd "
                     "FROM terminal_fills")}
        for t in rows:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or t.get("trade_id") or "")
            if not tid:
                continue
            status = str(t.get("status") or "").upper().replace("TRADE_STATUS_", "")
            if status == "FAILED":
                failed_trades += 1
                if tid in prior:
                    gone = conn.execute(
                        "SELECT order_id FROM terminal_fills WHERE trade_id=?",
                        (tid,)).fetchone()
                    conn.execute("DELETE FROM terminal_fills WHERE trade_id=?", (tid,))
                    if gone and gone["order_id"]:
                        conn.execute(
                            """UPDATE terminal_orders
                               SET reconciled=0, filled_size=NULL,
                                   avg_fill_price_cents=NULL, fees_usd=NULL,
                                   cost_usd=NULL, reconciled_at=NULL,
                                   status=CASE WHEN status='filled'
                                               THEN 'placed' ELSE status END
                               WHERE order_id=?""", (gone["order_id"],))
                    unwound += 1
                continue
            leg = _our_leg(t, our_addr)
            if leg is None:
                unmapped += 1
                continue
            price = leg["price"]
            size = leg["size"]
            if price is None or size is None or size <= 0:
                continue
            taker_oid = str(t.get("taker_order_id") or "")
            maker_oids = []
            for x in (t.get("maker_orders") or []):
                oid = str(x.get("order_id") or "") if isinstance(x, dict) else str(x)
                if oid:
                    maker_oids.append(oid)
            local = None
            matched_oid = None
            for cand in [taker_oid] + maker_oids:
                if cand and cand in local_by_oid:
                    local = local_by_oid[cand]
                    matched_oid = cand
                    break

            cid = str(t.get("market") or (local or {}).get("condition_id") or "")
            if not cid:
                continue
            action = "buy" if str(leg["action"] or "").upper() == "BUY" else "sell"
            side = _side_from_token(
                leg["asset_id"], leg["outcome"], market_by_id.get(cid))
            if side is None:
                side = (local or {}).get("side")
            if side is None:
                unmapped += 1
                continue
            ts = t.get("match_time") or t.get("timestamp")
            try:
                filled_at = datetime.fromtimestamp(
                    int(ts), timezone.utc).isoformat(
                        timespec="seconds").replace("+00:00", "Z")
            except (TypeError, ValueError, OSError):
                filled_at = _now_iso()

            if floor and filled_at < floor:
                skipped += 1
                continue

            is_taker = (leg["role"] == "taker"
                        or (leg["role"] == "unknown"
                            and bool(taker_oid and matched_oid == taker_oid)))
            is_maker = (leg["role"] == "maker"
                        or (leg["role"] == "unknown"
                            and bool(matched_oid and matched_oid != taker_oid)))
            fee = None
            if is_taker:
                mkt = market_by_id.get(cid)
                if mkt:
                    fee_ps = _fee_per_share(
                        price, mkt.get("category"),
                        api._fee_schedule_from_gamma(mkt.get("_raw") or {}))
                    fee = round(fee_ps * size, 4) if fee_ps is not None else None
            elif is_maker:
                fee = 0.0

            cost = round(size * price / 100.0, 4)
            title = ((local or {}).get("title")
                     or (market_by_id.get(cid) or {}).get("title") or "")
            was = prior.get(tid)
            same = (was is not None and was[0] == side and was[1] == action
                    and abs(was[2] - size) <= 1e-6 and abs(was[3] - price) <= 1e-6)
            if fee is None and same:
                fee = was[4]
            cur = conn.execute(
                """INSERT INTO terminal_fills
                   (trade_id, order_id, condition_id, title, side, action,
                    size, price_cents, fees_usd, cost_usd, origin, network,
                    filled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(trade_id) DO UPDATE SET
                     side = excluded.side,
                     action = excluded.action,
                     size = excluded.size,
                     price_cents = excluded.price_cents,
                     fees_usd = excluded.fees_usd,
                     cost_usd = excluded.cost_usd,
                     order_id = COALESCE(excluded.order_id, terminal_fills.order_id),
                     title = CASE WHEN COALESCE(terminal_fills.title, '') = ''
                                  THEN excluded.title ELSE terminal_fills.title END""",
                (tid, matched_oid, cid, title,
                 side, action, size, price, fee, cost,
                 (local or {}).get("origin") or "external", net, filled_at),
            )
            if was is None:
                inserted += cur.rowcount or 0
            elif not same:
                corrected += 1

        matched = 0
        for o in conn.execute(
            "SELECT id, order_id FROM terminal_orders "
            "WHERE order_id IS NOT NULL AND reconciled=0"
        ).fetchall():
            agg = conn.execute(
                "SELECT SUM(size) s, SUM(size*price_cents) sp, "
                "SUM(COALESCE(fees_usd,0)) f, SUM(cost_usd) c, "
                "COUNT(*) n, SUM(CASE WHEN fees_usd IS NULL THEN 1 ELSE 0 END) nf "
                "FROM terminal_fills WHERE order_id=?",
                (o["order_id"],)).fetchone()
            if not agg or not agg["n"]:
                continue
            total = float(agg["s"] or 0.0)
            if total <= 0:
                continue
            conn.execute(
                """UPDATE terminal_orders
                   SET filled_size=?, avg_fill_price_cents=?, fees_usd=?,
                       cost_usd=?, reconciled=1, status='filled',
                       reconciled_at=?
                   WHERE id=?""",
                (total, round(float(agg["sp"]) / total, 3),
                 (None if agg["nf"] else round(float(agg["f"]), 4)),
                 round(float(agg["c"] or 0.0), 4), _now_iso(), o["id"]),
            )
            matched += 1

    try:
        import mcp_rails
        mcp_rails.promote_from_ledger()
    except Exception as e:  # noqa: BLE001
        logger.debug("reconcile: agent unknown-order promotion failed: %s", e)

    notes = []
    if not complete:
        notes.append(
            f"Walked {len(rows)} fills and there is more history beyond that. "
            f"The next reconcile continues from where this one stopped — older "
            f"positions stay marked unreconciled until it reaches them."
        )
    if skipped:
        notes.append(
            f"{skipped} fill(s) from before you cleared your stats were left "
            f"out — their order rows were deleted, so who placed them can no "
            f"longer be established."
        )
    if corrected:
        notes.append(
            f"{corrected} fill(s) had been recorded against the wrong outcome "
            f"or the wrong side of the match, and have been re-derived from "
            f"Polymarket's own trade record. Their markets now have a correct "
            f"cost basis."
        )
    if unmapped:
        notes.append(
            f"{unmapped} fill(s) could not be tied to one of their market's "
            f"two outcomes, so they were left out rather than filed under a "
            f"guess."
        )
    if unwound:
        notes.append(
            f"{unwound} fill(s) came from trades that Polymarket has since "
            f"marked FAILED: they never settled, so nothing changed hands. "
            f"They have been taken out of the ledger."
        )
    return {"inserted": inserted, "matched": matched, "skipped": skipped,
            "corrected": corrected, "unmapped": unmapped,
            "failed": failed_trades, "unwound": unwound,
            "scanned": len(rows), "complete": complete,
            "note": " ".join(notes) or None}



def _ledger_basis() -> dict[tuple[str, str], dict]:
    """Average cost per (conditionId, side) from RECONCILED fills only.

    Average cost, not FIFO. A sell reduces size and leaves the average alone —
    which is the convention every trader means when they say "my basis".

    EVERY fill counts, whatever its `origin`. The user's wallet is the user's
    wallet: a position the crypto engine opened cost real money out of the
    same balance a hand-placed one did, and filing it as "placed elsewhere"
    used to be a reason it had no basis at all. Origin decides which TRACK
    RECORD a trade belongs to; it never decides whether a position has a cost.
    """
    out: dict[tuple[str, str], dict] = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT condition_id, side, action, size, price_cents, "
            "COALESCE(fees_usd,0) fee, fees_usd raw_fee "
            "FROM terminal_fills ORDER BY filled_at ASC"
        ).fetchall()
    for r in rows:
        key = (str(r["condition_id"]), str(r["side"]))
        st = out.setdefault(key, {
            "size": 0.0, "cost": 0.0, "fees": 0.0, "fees_known": True,
            "realized": 0.0, "bought": 0.0, "closed": 0.0,
        })
        sz = float(r["size"] or 0.0)
        px = float(r["price_cents"] or 0.0)
        if r["raw_fee"] is None:
            st["fees_known"] = False
        else:
            st["fees"] += float(r["fee"])
        if str(r["action"]) == "buy":
            st["size"] += sz
            st["bought"] += sz
            st["cost"] += sz * px / 100.0
        else:
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                st["closed"] += close
                st["realized"] += close * (px / 100.0 - avg)
                st["cost"] -= close * avg
                st["size"] -= close
    return out


_RESOLVED_KV_PREFIX = "terminal:resolved:"
_RESOLUTION_MISS_TTL = 300.0


def _miss_expired(missed_at, now: float) -> bool:
    """May this market be looked up (again)? A market never looked up has no
    miss on record — and that is NOT a miss at monotonic zero. time.monotonic()
    counts from boot, so `.get(c, 0.0)` made every market look like a
    five-minute-old miss on any machine up for less than the TTL: every CI
    runner, and every user's PC for five minutes after boot, resolved nothing."""
    return missed_at is None or (now - float(missed_at)) > _RESOLUTION_MISS_TTL
_resolution_misses: dict[str, float] = {}
_resolution_mem: dict[str, str] = {}
_MARKET_KV_PREFIX = "terminal:market:"
_market_meta_mem: dict[str, dict] = {}
_meta_misses: dict[str, float] = {}
_resolution_scope: Optional[str] = None


def _resolution_scope_check() -> None:
    global _resolution_scope
    try:
        import db
        scope = str(db.db_path())
    except Exception:  # noqa: BLE001
        scope = ""
    if scope != _resolution_scope:
        _resolution_scope = scope
        _resolution_mem.clear()
        _resolution_misses.clear()
        _market_meta_mem.clear()
        _meta_misses.clear()


def _resolutions_cached() -> dict[str, str]:
    """Every outcome we have already learned, from memory then app_kv.

    One query, not one per market: the whole point of the cache is that a
    Track refresh over a 580-market ledger costs a single SELECT.
    """
    _resolution_scope_check()
    if _resolution_mem:
        return dict(_resolution_mem)
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT k, v FROM app_kv WHERE k LIKE ?",
                (_RESOLVED_KV_PREFIX + "%",),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — a cache miss is not an error
        logger.debug("resolution cache read failed: %s", e)
        return {}
    for r in rows:
        v = str(r[1] or "")
        if v in ("yes", "no"):
            _resolution_mem[str(r[0])[len(_RESOLVED_KV_PREFIX):]] = v
    return dict(_resolution_mem)


def _resolution_remember(cid: str, outcome: str) -> None:
    """Persist one settled outcome. Written once; never revisited."""
    if outcome not in ("yes", "no") or not cid:
        return
    _resolution_scope_check()
    if _resolution_mem.get(cid) == outcome:
        return
    _resolution_mem[cid] = outcome
    _resolution_misses.pop(cid, None)
    try:
        import db
        with _conn() as conn:
            db.kv_set(conn, _RESOLVED_KV_PREFIX + cid, outcome)
    except Exception as e:  # noqa: BLE001
        logger.debug("resolution cache write failed for %s: %s", cid, e)


async def _resolutions_for(condition_ids) -> dict[str, Optional[str]]:
    """conditionId → ``'yes'`` | ``'no'`` | ``None``.

    ``None`` means WE DO NOT KNOW — the market is still open, or Gamma did not
    answer. It never means "lost". Everything that reads this has to carry the
    unknown through rather than settle it at zero.
    """
    ids = [str(c) for c in condition_ids if c]
    if not ids:
        return {}
    known = _resolutions_cached()
    out: dict[str, Optional[str]] = {c: known.get(c) for c in ids}
    now = time.monotonic()
    missing = [c for c in dict.fromkeys(ids)
               if out.get(c) is None
               and _miss_expired(_resolution_misses.get(c), now)]
    for i in range(0, len(missing), 20):
        chunk = missing[i:i + 20]
        try:
            summaries = await term._summaries_for(chunk)
        except Exception as e:  # noqa: BLE001 — a miss degrades to unknown
            logger.debug("resolution lookup failed: %s", e)
            summaries = []
        seen: set[str] = set()
        for m in summaries:
            cid = str(m.get("conditionId") or "")
            seen.add(cid)
            r = m.get("resolvedOutcome")
            if r in ("yes", "no"):
                _resolution_remember(cid, r)
                if cid in out:
                    out[cid] = r
            else:
                _resolution_misses[cid] = now
        for cid in chunk:
            if cid not in seen:
                _resolution_misses[cid] = now
    return out



def _market_meta_cached() -> dict[str, dict]:
    """Every resolved market we have already described, in one SELECT."""
    _resolution_scope_check()
    if _market_meta_mem:
        return dict(_market_meta_mem)
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT k, v FROM app_kv WHERE k LIKE ?",
                (_MARKET_KV_PREFIX + "%",),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — a cache miss is not an error
        logger.debug("market meta cache read failed: %s", e)
        return {}
    for r in rows:
        try:
            v = json.loads(str(r[1] or ""))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, dict) and v.get("outcome") in ("yes", "no"):
            _market_meta_mem[str(r[0])[len(_MARKET_KV_PREFIX):]] = v
    return dict(_market_meta_mem)


def _market_meta_remember(cid: str, entry: dict) -> None:
    """Persist one resolved market's outcome, labels and close time."""
    if not cid or entry.get("outcome") not in ("yes", "no"):
        return
    _resolution_scope_check()
    if _market_meta_mem.get(cid) == entry:
        return
    _market_meta_mem[cid] = entry
    _meta_misses.pop(cid, None)
    try:
        import db
        with _conn() as conn:
            db.kv_set(conn, _MARKET_KV_PREFIX + cid, json.dumps(entry))
    except Exception as e:  # noqa: BLE001
        logger.debug("market meta cache write failed for %s: %s", cid, e)


async def _market_meta_for(condition_ids) -> dict[str, dict]:
    """conditionId → ``{outcome, labels, closeTime, title}`` for the record.

    Everything already cached costs one SELECT. Only markets we have never
    seen RESOLVED are asked about, and an unresolved one is re-asked at most
    every few minutes — so the first time the settled table is opened on a
    580-market ledger it pays about thirty batched Gamma calls, and every
    time after that it pays none.

    A market we cannot describe is simply absent. Never guessed at, and never
    defaulted to "lost".
    """
    ids = list(dict.fromkeys(str(c) for c in condition_ids if c))
    if not ids:
        return {}
    want = set(ids)
    out: dict[str, dict] = {c: dict(v) for c, v in _market_meta_cached().items()
                            if c in want}
    now = time.monotonic()
    missing = [c for c in ids
               if c not in out
               and _miss_expired(_meta_misses.get(c), now)]
    for i in range(0, len(missing), 20):
        chunk = missing[i:i + 20]
        try:
            summaries = await term._summaries_for(chunk)
        except Exception as e:  # noqa: BLE001 — a miss degrades to unknown
            logger.debug("market meta lookup failed: %s", e)
            summaries = []
        seen: set[str] = set()
        for m in summaries:
            cid = str(m.get("conditionId") or "")
            seen.add(cid)
            labels = m.get("outcomes")
            if not (isinstance(labels, (list, tuple)) and len(labels) >= 2):
                labels = None
            entry = {
                "outcome": m.get("resolvedOutcome"),
                "labels": [str(labels[0]), str(labels[1])] if labels else None,
                "closeTime": m.get("closeTime") or None,
                "title": m.get("title") or None,
            }
            if entry["outcome"] in ("yes", "no"):
                _market_meta_remember(cid, entry)
                _resolution_remember(cid, str(entry["outcome"]))
                out[cid] = entry
            else:
                _meta_misses[cid] = now
                out[cid] = entry
        for cid in chunk:
            if cid not in seen:
                _meta_misses[cid] = now
    return out


_RECORD_MIN_CONTRACTS = 0.5


async def _ledger_record(basis: dict[tuple[str, str], dict]) -> dict:
    """Settle the WHOLE fill ledger, not just what is left in the wallet.

    THE BUG THIS EXISTS TO FIX. Realised P&L used to be summed over the
    positions the wallet still holds. But a resolved WINNER leaves the wallet
    the moment it is redeemed, while a resolved LOSER stays behind forever as
    $0 dust — and a redemption is an on-chain claim, not a CLOB trade, so it
    leaves no "sell" row either. Summing the wallet therefore added up the
    losers and silently dropped every win: a real wallet with 453 redeemed
    winners reported "105 positions, 105 losses, realised −$681.13".

    So the ledger is the unit of account. For every (market, side) we ever
    bought, the contracts still outstanding settle at exactly $1 if that side
    won and exactly $0 if it lost — whether or not the tokens are still in
    the wallet, because a redeemed winner has already been PAID.

    An unresolved market settles nothing; it realises from its sells only. A
    market whose outcome we cannot read is counted as UNKNOWN and left out of
    the total — never folded in as a loss.
    """
    cids = sorted({cid for cid, _ in basis})
    res = await _resolutions_for(cids)

    settled_pnl = 0.0
    any_settled = False
    fees = 0.0
    fees_known = True
    won: set[str] = set()
    lost: set[str] = set()
    unknown: set[str] = set()

    for (cid, side), v in basis.items():
        fees += float(v.get("fees") or 0.0)
        if not v.get("fees_known", True):
            fees_known = False
        size = float(v.get("size") or 0.0)
        if size <= 0.01:
            continue
        outcome = res.get(cid)
        if outcome is None:
            unknown.add(cid)
            continue
        payout = size if outcome == side else 0.0
        settled_pnl += payout - float(v.get("cost") or 0.0)
        any_settled = True
        if size >= _RECORD_MIN_CONTRACTS:
            (won if outcome == side else lost).add(cid)

    closed_out = set(cids) - won - lost - unknown
    return {
        "settledPnlUsd": round(settled_pnl, 2) if any_settled else None,
        "anySettled": any_settled,
        "feesUsd": round(fees, 2) if fees_known else None,
        "wonMarkets": len(won),
        "lostMarkets": len(lost),
        "unknownMarkets": len(unknown),
        "closedOutMarkets": len(closed_out),
        "ledgerMarkets": len(cids),
        "bothSidesMarkets": len(won & lost),
    }


def _record_note(record: dict) -> Optional[str]:
    """One sentence saying what the win/loss counts are and are not."""
    settled = record["wonMarkets"] + record["lostMarkets"]
    if not settled and not record["unknownMarkets"]:
        return None
    parts = []
    if settled:
        parts.append(
            f"{record['wonMarkets']} won and {record['lostMarkets']} lost across "
            f"{record['ledgerMarkets']} market(s) in your fill ledger — counted "
            f"from the fills, so a winner you already redeemed still counts.")
    if record.get("bothSidesMarkets"):
        parts.append(
            f"{record['bothSidesMarkets']} of those are in BOTH columns because "
            f"both sides were held to settlement, so the two do not add up to "
            f"the market count.")
    if record["closedOutMarkets"]:
        parts.append(
            f"{record['closedOutMarkets']} more were closed by selling before "
            f"they resolved, so no outcome decided them.")
    if record["unknownMarkets"]:
        parts.append(
            f"{record['unknownMarkets']} market(s) have contracts outstanding "
            f"whose outcome we could not read — left OUT of both the counts and "
            f"the realised figure rather than assumed lost.")
    return " ".join(parts) or None


def _ledger_meta() -> dict[tuple[str, str], dict]:
    """Per (market, side): the title we saw and when we last traded it.

    The title is the one on the most recent fill — bare columns beside a
    `MAX()` are documented in SQLite to come from the row that produced the
    maximum. `last_at` is what the record sorts by when the venue never told
    us a close time, so a row still lands in the right place rather than at
    the bottom under a fabricated date.
    """
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT condition_id, side, MAX(filled_at) AS last_at, title "
                "FROM terminal_fills GROUP BY condition_id, side"
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("ledger meta read failed: %s", e)
        return {}
    return {(str(r["condition_id"]), str(r["side"])):
            {"lastFillAt": r["last_at"], "title": r["title"]}
            for r in rows}


async def _wallet_sides() -> Optional[set[tuple[str, str]]]:
    """(market, side) the wallet still holds, or None if we could not read it.

    None is not "holds nothing". A record that quietly claimed every winner
    had left the wallet because the positions call timed out would be exactly
    the class of lie this module exists to prevent, so the rows say unknown.
    """
    try:
        rows = await api.get_positions(strict=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("settled record: wallet read failed: %s", e)
        return None
    out: set[tuple[str, str]] = set()
    for p in rows or []:
        cid = str(p.get("ticker") or "")
        signed = float(p.get("position_fp") or 0.0)
        if cid and abs(signed) > 0.01:
            out.add((cid, "yes" if signed >= 0 else "no"))
    return out


async def settled_record() -> dict:
    """The record: every market the FILL LEDGER has ever settled.

    THE SCREEN THIS EXISTS FOR. "Show settled" used to list the wallet's
    settled positions, and the wallet is a survivorship filter with the sign
    flipped: a resolved WINNER leaves it the moment it is redeemed, a resolved
    LOSER stays behind forever as $0 dust. So a real account with 453 wins and
    117 losses showed "105 losses" and not one win — every row true, the whole
    table a lie by omission.

    This reads the ledger instead. For every (market, side) we ever bought,
    the contracts still outstanding settle at exactly $1 on the winning
    outcome and exactly $0 on the loser, whether or not the tokens are still
    here — a redeemed winner has already been PAID.

    Four outcomes, and no fifth:

      ``won``       held the winning side into settlement
      ``lost``      held the losing side into settlement
      ``closedOut`` sold back before it resolved (or left a residual too small
                    to have been worth claiming) — no outcome decided it
      ``unknown``   contracts outstanding, and we could not read the outcome.
                    Never folded in as a loss.

    A market held on BOTH sides into settlement is won on one row and lost on
    the other; both carry ``bothSides`` so the screen can say so instead of
    quietly counting it twice. The counts here are the same sets
    `portfolio()` reports as `wonMarkets` / `lostMarkets`, by construction.
    """
    basis = _ledger_basis()
    meta = _ledger_meta()
    cids = sorted({cid for cid, _ in basis})
    if not cids:
        return {
            "rows": [], "totals": {
                "won": 0, "lost": 0, "bothSides": 0, "closedOut": 0,
                "unknown": 0, "markets": 0, "settledMarkets": 0, "rows": 0,
                "pnlUsd": None, "pnlMissingRows": 0,
                "feesUsd": None, "feesKnown": True, "unknownRows": 0,
            },
            "walletReadable": True, "fetchedAt": _now_iso(),
            "note": ("Your fill ledger is empty, so there is no record to "
                     "show. Press Reconcile fills to import your trade "
                     "history from Polymarket."),
        }

    market = await _market_meta_for(cids)
    wallet = await _wallet_sides()

    rows: list[dict] = []
    won: set[str] = set()
    lost: set[str] = set()
    unknown: set[str] = set()
    fees_total = 0.0
    fees_all_known = True
    pnl_total = 0.0
    pnl_rows = 0
    unknown_rows = 0

    for (cid, side), v in basis.items():
        m = meta.get((cid, side)) or {}
        mk = market.get(cid) or {}
        labels = mk.get("labels")
        res = mk.get("outcome") if mk.get("outcome") in ("yes", "no") else None

        size = float(v.get("size") or 0.0)
        cost = float(v.get("cost") or 0.0)
        realized = float(v.get("realized") or 0.0)
        fees = float(v.get("fees") or 0.0)
        fees_known = bool(v.get("fees_known", True))
        fees_total += fees
        if not fees_known:
            fees_all_known = False

        flat = size <= 0.01
        payout: Optional[float]
        gross: Optional[float]
        if flat:
            outcome, payout = "closedOut", None
            gross = realized
        elif res is None:
            outcome, payout, gross = "unknown", None, None
            unknown.add(cid)
            unknown_rows += 1
        else:
            payout = round(size if res == side else 0.0, 2)
            gross = realized + payout - cost
            if size >= _RECORD_MIN_CONTRACTS:
                outcome = "won" if res == side else "lost"
                (won if res == side else lost).add(cid)
            else:
                outcome = "closedOut"

        pnl = (None if (gross is None or not fees_known)
               else round(gross - fees, 2))
        if pnl is not None:
            pnl_total += pnl
            pnl_rows += 1

        rows.append({
            "conditionId": cid,
            "title": m.get("title") or mk.get("title") or None,
            "side": side,
            "sideLabel": (str(labels[0 if side == "yes" else 1])
                          if labels else None),
            "contracts": round(size, 2),
            "avgCostCents": (round(cost / size * 100.0, 3)
                             if size > 0.01 else None),
            "costUsd": round(cost, 2),
            "payoutUsd": payout,
            "pnlUsd": pnl,
            "feesUsd": round(fees, 2) if fees_known else None,
            "feesKnown": fees_known,
            "soldPnlUsd": round(realized, 2),
            "outcome": outcome,
            "resolvedOutcome": res,
            "resolvedOutcomeLabel": (str(labels[0 if res == "yes" else 1])
                                     if (labels and res) else None),
            "settledAt": mk.get("closeTime") or None,
            "inWallet": (None if wallet is None else (cid, side) in wallet),
            "bothSides": False,
            "_sort": str(mk.get("closeTime") or m.get("lastFillAt") or ""),
        })

    both = won & lost
    for r in rows:
        r["bothSides"] = r["conditionId"] in both
    closed_out = set(cids) - won - lost - unknown

    rows.sort(key=lambda r: (r["_sort"], r["conditionId"]), reverse=True)
    for r in rows:
        r.pop("_sort", None)

    parts: list[str] = [
        f"{len(won)} won and {len(lost)} lost across {len(cids)} market(s) in "
        f"your fill ledger. This is the RECORD, not the wallet: a winner you "
        f"already redeemed still counts here, and it left the Positions tab "
        f"the moment you claimed it."]
    if both:
        parts.append(
            f"{len(both)} of those are in BOTH columns because both sides were "
            f"held to settlement, so the two do not add up to the market count "
            f"— each is shown once per side and labelled.")
    if closed_out:
        parts.append(
            f"{len(closed_out)} more were closed by selling before they "
            f"resolved, so no outcome decided them.")
    if unknown:
        parts.append(
            f"{len(unknown)} market(s) have contracts outstanding whose "
            f"outcome we could not read — left OUT of the counts and the "
            f"total rather than assumed lost.")

    return {
        "rows": rows,
        "totals": {
            "won": len(won), "lost": len(lost), "bothSides": len(both),
            "closedOut": len(closed_out), "unknown": len(unknown),
            "markets": len(cids),
            "settledMarkets": len(won | lost | closed_out),
            "rows": len(rows),
            "pnlUsd": round(pnl_total, 2) if pnl_rows else None,
            "pnlMissingRows": len(rows) - pnl_rows,
            "feesUsd": round(fees_total, 2) if fees_all_known else None,
            "feesKnown": fees_all_known,
            "unknownRows": unknown_rows,
        },
        "walletReadable": wallet is not None,
        "fetchedAt": _now_iso(),
        "note": " ".join(parts),
    }


async def portfolio() -> dict:
    """Positions with a cost basis, and a stamp saying where each one came from.

    TWO sources of basis, in this order:

      1. OUR FILL LEDGER — every reconciled fill for this wallet, whatever
         placed it. Average cost, fee-exclusive, computed here.
      2. THE VENUE'S OWN AVERAGE — ``avgPrice`` / ``initialValue`` from the
         Data API positions read, used when our ledger cannot cover the size
         (history older than we ever imported, or a wiped ledger). Stamped
         ``data-api`` so the screen can say so.

    Only when NEITHER can answer is the basis unknown — and then it stays
    unknown: never zero, never break-even, and the row is left out of the
    totals and counted in a warning strip.

    Every outcome question — which side you hold, which side won — is decided
    by the OUTCOME INDEX (0 = the market's first outcome), never by the label
    text, so an "Up or Down" market and a named-team market are read exactly
    as reliably as a Yes/No one.
    """
    try:
        raw_positions = await api.get_positions()
    except Exception as e:  # noqa: BLE001
        return {
            "positions": [], "totalValueUsd": None, "totalCostUsd": None,
            "totalUnrealizedPnlUsd": None, "realizedPnlUsd": None,
            "realizedFeesUsd": None, "realizedPnlNetUsd": None,
            "settledPnlUsd": None, "venueCashPnlUsd": None,
            "walletPnlUsd": None,
            "unreconciledCount": 0,
            "walletReadable": False,
            "unreconciledNote": f"Could not read your positions: {e}",
            "redeemableCount": 0, "redeemableValueUsd": None,
            "redeemableNote": None, "settledWorthlessCount": 0,
            "venueBasisCount": 0,
            "wonMarkets": None, "lostMarkets": None,
            "unknownResolutionMarkets": None, "closedOutMarkets": None,
            "ledgerMarkets": None, "recordNote": None,
            "cashUsd": None, "fetchedAt": _now_iso(),
            "costBasisConvention": "average",
        }

    basis = _ledger_basis()

    market_by_id: dict[str, dict] = {}
    try:
        for m in await term._summaries_for(
                [str(p.get("ticker") or "") for p in raw_positions]):
            market_by_id[m["conditionId"]] = m
            if m.get("resolvedOutcome") in ("yes", "no"):
                _resolution_remember(str(m["conditionId"]), str(m["resolvedOutcome"]))
    except Exception as e:  # noqa: BLE001 — a miss degrades to "unknown"
        logger.debug("terminal portfolio market lookup failed: %s", e)

    positions: list[dict] = []
    unreconciled = 0
    venue_basis_rows = 0
    redeemable_count = 0
    redeemable_value = 0.0
    worthless_settled = 0
    wallet_settled_total = 0.0
    any_wallet_settled = False
    venue_extra_settled = 0.0
    any_venue_extra = False
    venue_pnl_total = 0.0
    any_venue_pnl = False
    tot_val = tot_cost = tot_pnl = 0.0
    any_reconciled = False

    for p in raw_positions:
        cid = str(p.get("ticker") or "")
        if not cid:
            continue
        signed = float(p.get("position_fp") or 0.0)
        side = "yes" if signed >= 0 else "no"
        size = abs(signed)
        mark = cents(p.get("cur_price"))
        b = basis.get((cid, side))

        vraw = p.get("_raw") if isinstance(p.get("_raw"), dict) else {}
        v_avg = num(vraw.get("avgPrice"))
        v_initial = num(vraw.get("initialValue"))
        v_current = num(vraw.get("currentValue"))
        v_cash_pnl = num(vraw.get("cashPnl"))
        v_realized = num(vraw.get("realizedPnl"))
        side_label = str(vraw.get("outcome") or "").strip() or None

        m = market_by_id.get(cid)
        status = (m or {}).get("status") or "unknown"
        resolved = (m or {}).get("resolvedOutcome")
        close_time = (m or {}).get("closeTime")
        settled = resolved is not None

        outcome_labels = (m or {}).get("outcomes")
        if not (isinstance(outcome_labels, (list, tuple))
                and len(outcome_labels) >= 2):
            outcome_labels = None
        if side_label is None and outcome_labels:
            side_label = str(outcome_labels[0 if side == "yes" else 1])
        resolved_label = None
        if resolved is not None and outcome_labels:
            resolved_label = str(outcome_labels[0 if resolved == "yes" else 1])

        if settled:
            mark = 100.0 if resolved == side else 0.0
            status = "settled"

        unknowns: list[dict] = []
        sources: dict[str, dict] = {
            "probability": {"source": "data-api", "observedAt": _now_iso(), "ageSec": 0.0},
        }

        avg_cents = None
        cost_usd = None
        basis_source = None
        pnl = None
        settle_pnl = None
        ledger_covered = round(min(size, b["size"]), 2) if b else 0.0
        unrec_size = None

        if b and b["size"] > 0.01 and b["size"] >= size - 0.01:
            avg_cents = round(b["cost"] / b["size"] * 100.0, 3)
            cost_usd = round(size * avg_cents / 100.0, 2)
            basis_source = "ledger"
            sources["avgCost"] = {"source": "ledger", "observedAt": _now_iso(),
                                  "ageSec": 0.0}
        elif v_avg is not None:
            avg_cents = round(v_avg * 100.0, 3)
            cost_usd = (round(v_initial, 2) if v_initial is not None
                        else round(size * avg_cents / 100.0, 2))
            basis_source = "data-api"
            venue_basis_rows += 1
            sources["avgCost"] = {"source": "data-api", "observedAt": _now_iso(),
                                  "ageSec": 0.0}
            if b and b["size"] > 0.01:
                unrec_size = round(size - ledger_covered, 2)
                unknowns.append({
                    "field": "avgCostCents",
                    "reason": (
                        f"Your fill ledger only covers {ledger_covered:g} of "
                        f"{size:g} contracts here, so the average shown is "
                        f"Polymarket's own for this wallet, not ours."),
                })
        else:
            unrec_size = size
            unknowns.append({
                "field": "avgCostCents",
                "reason": (
                    "Neither your fill ledger nor Polymarket's own position "
                    "record has an average price for these contracts, so there "
                    "is nothing to measure the P&L against. Not zero: unknown."),
            })

        if avg_cents is not None and mark is not None:
            delta = round(size * (mark - avg_cents) / 100.0, 2)
            if settled:
                settle_pnl = delta
            else:
                pnl = delta

        if mark is None:
            unknowns.append({
                "field": "markCents",
                "reason": (
                    "No current price for this position, and no posted outcome "
                    "to settle it against."
                    if status in ("closed", "unknown") else
                    "The Data API returned no current price for this position."),
            })
        elif settled:
            sources["mid"] = {"source": "gamma", "observedAt": _now_iso(), "ageSec": 0.0}

        value = round(size * mark / 100.0, 2) if mark is not None else None
        if value is None:
            unknowns.append({
                "field": "valueUsd",
                "reason": "Value needs a current price, and there is none.",
            })

        flagged = bool(p.get("redeemable"))
        worth = v_current if v_current is not None else value
        claimable = False
        if flagged:
            if worth is None:
                claimable = False
                unknowns.append({
                    "field": "redeemable",
                    "reason": (
                        "Polymarket flags this position redeemable, but we "
                        "could not read what it is worth — so whether there is "
                        "anything to claim is unknown, not yes and not no."),
                })
            elif worth > 0.005:
                claimable = True
            else:
                worthless_settled += 1
        if claimable:
            redeemable_count += 1
            redeemable_value += worth

        row_unreconciled = basis_source is None
        if not row_unreconciled:
            any_reconciled = True
            if value is not None:
                tot_val += value
            if cost_usd is not None:
                tot_cost += cost_usd
            if pnl is not None:
                tot_pnl += pnl
            if settle_pnl is not None:
                wallet_settled_total += settle_pnl
                any_wallet_settled = True
                uncovered = size - ledger_covered
                if uncovered > 0.01 and avg_cents is not None and mark is not None:
                    venue_extra_settled += (
                        uncovered * (mark - avg_cents) / 100.0)
                    any_venue_extra = True
        else:
            unreconciled += 1

        if v_cash_pnl is not None:
            venue_pnl_total += v_cash_pnl
            any_venue_pnl = True

        positions.append({
            "conditionId": cid, "venue": "polymarket",
            "title": p.get("title") or (m or {}).get("title") or None,
            "slug": (m or {}).get("slug"),
            "side": side, "size": round(size, 2),
            "sideLabel": side_label,
            "avgCostCents": avg_cents, "costBasisUsd": cost_usd,
            "basisSource": basis_source,
            "markCents": mark, "valueUsd": value,
            "unrealizedPnlUsd": pnl,
            "realizedPnlUsd": (
                None if (b is None and settle_pnl is None)
                else round((b["realized"] if b else 0.0) + (settle_pnl or 0.0), 2)),
            "settledPnlUsd": settle_pnl,
            "venueCashPnlUsd": v_cash_pnl,
            "venueRealizedPnlUsd": v_realized,
            "status": status,
            "resolvedOutcome": resolved,
            "resolvedOutcomeLabel": resolved_label,
            "outcomeLabels": ([str(outcome_labels[0]), str(outcome_labels[1])]
                              if outcome_labels else None),
            "closeTime": close_time,
            "redeemable": claimable,
            "settled": settled,
            "unreconciled": row_unreconciled,
            "unreconciledSize": unrec_size,
            "sources": sources,
            "unknowns": unknowns,
        })

    positions.sort(key=lambda r: -(r.get("valueUsd") or 0.0))

    record = await _ledger_record(basis)
    ledger_realized = sum(v["realized"] for v in basis.values()) if basis else None
    settled_total = record["settledPnlUsd"]
    if any_venue_extra:
        settled_total = round((settled_total or 0.0) + venue_extra_settled, 2)
    any_settled_pnl = record["anySettled"] or any_venue_extra
    any_sells = any(v.get("closed", 0.0) > 0 for v in basis.values())
    if ledger_realized is None:
        realized = None
    elif record["unknownMarkets"] and not (any_sells or any_settled_pnl):
        realized = None
    else:
        realized = round((ledger_realized or 0.0) + (settled_total or 0.0), 2)

    fees_total = record["feesUsd"]
    realized_net = (None if (realized is None or fees_total is None)
                    else round(realized - fees_total, 2))

    cash = None
    try:
        bal = await api.get_balance()
        cash_cents = num((bal or {}).get("balance"))
        cash = round(cash_cents / 100.0, 2) if cash_cents is not None else None
    except Exception:
        cash = None

    parts: list[str] = []
    if unreconciled:
        parts.append(
            f"{unreconciled} position(s) have no cost basis at all — neither "
            f"your fill ledger nor Polymarket's own position record has an "
            f"average price for them. They are shown, but deliberately left "
            f"OUT of the totals rather than counted at a guessed price.")
    if venue_basis_rows:
        parts.append(
            f"{venue_basis_rows} position(s) are priced off Polymarket's own "
            f"average for this wallet rather than your fill ledger — their "
            f"fills are older than anything we imported. Those figures are the "
            f"venue's, and are marked as such. Press Reconcile fills to walk "
            f"further back and replace them with ours.")
    note = " ".join(parts) or None

    redeem_note = None
    if redeemable_count:
        redeem_note = (
            f"{redeemable_count} settled position(s) are ready to redeem, "
            f"worth {round(redeemable_value, 2):,.2f} USDC. The outcome is "
            f"decided, so that figure is exact rather than a mark — claim "
            f"them on Polymarket to turn them back into cash.")
        if worthless_settled:
            redeem_note += (
                f" A further {worthless_settled} resolved position(s) are worth "
                f"nothing and are not counted here.")
    elif worthless_settled:
        redeem_note = (
            f"{worthless_settled} resolved position(s) are still sitting in "
            f"your wallet and every one is worth $0.00 — they lost, so there "
            f"is nothing to claim and no reason to spend gas. They stay listed "
            f"until Polymarket clears them.")

    return {
        "positions": positions,
        "walletReadable": True,
        "totalValueUsd": round(tot_val, 2) if any_reconciled else None,
        "totalCostUsd": round(tot_cost, 2) if any_reconciled else None,
        "totalUnrealizedPnlUsd": round(tot_pnl, 2) if any_reconciled else None,
        "realizedPnlUsd": realized,
        "realizedFeesUsd": fees_total,
        "realizedPnlNetUsd": realized_net,
        "settledPnlUsd": settled_total if any_settled_pnl else None,
        "venueCashPnlUsd": round(venue_pnl_total, 2) if any_venue_pnl else None,
        "walletPnlUsd": (round(wallet_settled_total + tot_pnl, 2)
                         if (any_wallet_settled or any_reconciled) else None),
        "wonMarkets": record["wonMarkets"],
        "lostMarkets": record["lostMarkets"],
        "unknownResolutionMarkets": record["unknownMarkets"],
        "bothSidesMarkets": record["bothSidesMarkets"],
        "closedOutMarkets": record["closedOutMarkets"],
        "ledgerMarkets": record["ledgerMarkets"],
        "recordNote": _record_note(record),
        "unreconciledCount": unreconciled,
        "unreconciledNote": note,
        "venueBasisCount": venue_basis_rows,
        "settledWorthlessCount": worthless_settled,
        "redeemableCount": redeemable_count,
        "redeemableValueUsd": round(redeemable_value, 2),
        "redeemableNote": redeem_note,
        "cashUsd": cash,
        "fetchedAt": _now_iso(),
        "costBasisConvention": "average",
    }


def fills(limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 500))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM terminal_fills ORDER BY filled_at DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": str(r["trade_id"]),
            "conditionId": str(r["condition_id"]),
            "title": r["title"] or None,
            "side": str(r["side"]), "action": str(r["action"]),
            "size": float(r["size"]),
            "priceCents": float(r["price_cents"]),
            "feeUsd": (float(r["fees_usd"]) if r["fees_usd"] is not None else None),
            "costUsd": (float(r["cost_usd"]) if r["cost_usd"] is not None else None),
            "ts": str(r["filled_at"]),
            "reconciled": True,
            "origin": str(r["origin"] or "unknown"),
            "source": "ledger",
        })
    return {"fills": out, "fetchedAt": _now_iso(),
            "note": None if out else (
                "No fills recorded yet. This ledger is written from "
                "Polymarket's own trade history, not from what we requested.")}


def order_log(limit: int = 100) -> dict:
    """Every manual order attempt, including the ones that failed.

    Failures are kept on purpose: an order that errored after the venue may
    have accepted it is exactly the row you need when a position appears that
    you do not remember opening.
    """
    limit = max(1, min(int(limit or 100), 500))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM terminal_orders ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return {
        "orders": [{
            "localId": int(r["id"]),
            "conditionId": str(r["condition_id"]),
            "title": r["title"] or None,
            "side": str(r["side"]), "action": str(r["action"]),
            "orderType": str(r["order_type"]),
            "requestedSize": float(r["requested_size"]),
            "requestedPriceCents": float(r["requested_price_cents"]),
            "sentSize": (float(r["sent_size"]) if r["sent_size"] is not None else None),
            "sentPriceCents": (float(r["sent_price_cents"])
                               if r["sent_price_cents"] is not None else None),
            "orderId": r["order_id"] or None,
            "status": str(r["status"]),
            "error": r["error"] or None,
            "filledSize": (float(r["filled_size"]) if r["filled_size"] is not None else None),
            "avgFillPriceCents": (float(r["avg_fill_price_cents"])
                                  if r["avg_fill_price_cents"] is not None else None),
            "feesUsd": (float(r["fees_usd"]) if r["fees_usd"] is not None else None),
            "reconciled": bool(r["reconciled"]),
            "origin": str(r["origin"]),
            "standingId": r["standing_id"] or None,
            "createdAt": str(r["created_at"]),
        } for r in rows],
        "fetchedAt": _now_iso(),
    }


_STANDING_KEY = "terminal:standing"
_STANDING_MAX = 100
_VALID_KINDS = ("stopLoss", "takeProfit", "alertAbove", "alertBelow")
_STOP_KINDS = ("stopLoss", "takeProfit")

_STANDING_SOLD_KEY = "terminal:standing:sold"


def standing_list() -> list[dict]:
    entries = term._kv_json(_STANDING_KEY, [])
    return entries if isinstance(entries, list) else []


def _standing_retired(e: dict) -> bool:
    """Done for good: fired, failed out, or found nothing to sell. A stop the
    user (or a blocked trigger) merely disarmed is NOT retired — it can be
    re-armed, so it is never pruned."""
    return bool(isinstance(e, dict) and e.get("firedAt"))


def _standing_active_count(entries: list[dict]) -> int:
    return sum(1 for e in entries if isinstance(e, dict) and not _standing_retired(e))


def standing_remaining(e: dict) -> float:
    """Contracts a stop/take-profit still has to sell: its size less what
    earlier partial fills already sold."""
    size = float(num(e.get("size")) or 0.0)
    sold = float(num(e.get("soldSize")) or 0.0)
    return max(0.0, round(size - sold, 6))


def standing_sold_records() -> dict[str, dict]:
    recs = term._kv_json(_STANDING_SOLD_KEY, {})
    return recs if isinstance(recs, dict) else {}


def _legacy_sold(instruction_id: str) -> float:
    """What a stop that fired BEFORE sold sizes were recorded sold, by the rule
    the agent rails used for it: every order it sent that was not a known
    zero-fill. Errs high, which is the safe direction for a sell allowance."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(sent_size, requested_size)), 0) "
                "FROM terminal_orders WHERE standing_id=? "
                "AND status NOT IN ('unmatched', 'rejected')",
                (str(instruction_id),)).fetchone()
        return float(row[0] or 0.0)
    except Exception as ex:  # noqa: BLE001
        logger.debug("standing: legacy sold read for %s failed: %s", instruction_id, ex)
        return 0.0


def _record_stop_sale(e: dict, sold: float, order_id: Optional[str],
                      confirmed: bool, *, final: bool) -> None:
    """Add `sold` contracts to the durable record of instruction `e`."""
    if sold <= 0:
        return
    recs = standing_sold_records()
    iid = str(e.get("id") or "")
    r = recs.get(iid) or {
        "id": iid, "conditionId": e.get("conditionId"), "side": e.get("side"),
        "kind": e.get("kind"), "origin": e.get("origin") or "manual",
        "agentId": e.get("agentId"), "soldSize": 0.0, "orderIds": [],
        "confirmed": True, "firedAt": None,
    }
    r["soldSize"] = round(float(r.get("soldSize") or 0.0) + float(sold), 6)
    if order_id and order_id not in r["orderIds"]:
        r["orderIds"].append(order_id)
    r["confirmed"] = bool(r.get("confirmed", True)) and bool(confirmed)
    if e.get("agentId") and not r.get("agentId"):
        r["agentId"] = e.get("agentId")
    r["updatedAt"] = _now_iso()
    if final:
        r["firedAt"] = r["updatedAt"]
    recs[iid] = r
    term._kv_put(_STANDING_SOLD_KEY, recs)


def _archive_before_drop(e: dict) -> None:
    """A fired stop is leaving the list (pruned or deleted). Make sure what it
    sold survives it. Entries that fired before sold sizes were recorded have
    no record yet, so one is written from the order log."""
    if not isinstance(e, dict) or e.get("kind") not in _STOP_KINDS:
        return
    if not e.get("firedAt") and not float(num(e.get("soldSize")) or 0.0):
        return
    iid = str(e.get("id") or "")
    if not iid or iid in standing_sold_records():
        return
    if e.get("retiredReason") is None and e.get("soldSize") is None:
        sold, confirmed = _legacy_sold(iid), False
    else:
        sold, confirmed = float(num(e.get("soldSize")) or 0.0), True
    _record_stop_sale(e, sold, None, confirmed, final=bool(e.get("firedAt")))


def standing_sold_shares(cid: str, side: str, *, agent_id: Optional[str] = None,
                         default_agent_id: Optional[str] = None,
                         origin: Optional[str] = "ai") -> float:
    """Contracts of (cid, side) that standing stops/take-profits have SOLD.

    Durable: reads the sold record, which outlives the bounded instruction
    list and a user deleting a fired entry. A stop that fired before sold
    sizes were recorded and is still listed falls back to its order log.
    `agent_id` filters by owner; a record with no agentId belongs to
    `default_agent_id`. `origin=None` counts every stop."""
    cid, side = str(cid), str(side)

    def _match(x: dict) -> bool:
        if str(x.get("conditionId") or "") != cid or str(x.get("side") or "") != side:
            return False
        if origin is not None and (x.get("origin") or "manual") != origin:
            return False
        if agent_id is not None and (x.get("agentId") or default_agent_id) != agent_id:
            return False
        return True

    total = 0.0
    recs = standing_sold_records()
    for r in recs.values():
        if isinstance(r, dict) and _match(r):
            total += float(r.get("soldSize") or 0.0)
    for e in standing_list():
        if (isinstance(e, dict) and e.get("kind") in _STOP_KINDS
                and e.get("firedAt") and str(e.get("id")) not in recs
                and e.get("retiredReason") is None and e.get("soldSize") is None
                and _match(e)):
            total += _legacy_sold(str(e.get("id")))
    return round(total, 6)


def _standing_put(entries: list[dict]) -> None:
    """Write the list, pruning only RETIRED entries, oldest first, when it is
    over the cap. An armed or merely disarmed instruction is never evicted —
    the cap is enforced where instructions are added, by refusing the add.
    (It used to write `entries[:100]`, which silently dropped the OLDEST entry
    whatever it was, armed stop on an open position included.)"""
    entries = [e for e in entries if isinstance(e, dict)]
    over = len(entries) - _STANDING_MAX
    if over > 0:
        retired = sorted(
            (e for e in entries if _standing_retired(e)),
            key=lambda e: str(e.get("firedAt") or e.get("createdAt") or ""))
        drop = {id(e) for e in retired[:over]}
        for e in retired[:over]:
            _archive_before_drop(e)
        entries = [e for e in entries if id(e) not in drop]
    term._kv_put(_STANDING_KEY, entries)


_TICK_FIELDS = ("lastCheckedAt", "firedAt", "note", "failures", "soldSize",
                "retiredReason")


def _standing_merge_put(worked: list[dict]) -> list[dict]:
    """Write a tick's results back WITHOUT clobbering what the user did meanwhile.

    The tick reads the whole instruction list, then awaits the book, the
    wallet and possibly an order — seconds of network — and the RPC handlers
    (`standing_add` / `standing_cancel` / `standing_set_armed`) run as
    concurrent tasks the whole time. A blind write-back of the list read at the
    top therefore RESURRECTED an instruction cancelled mid-tick — armed, with
    a live stop attached to it — and re-armed one the user had just disarmed.

    So re-read, and merge per instruction:
      * an instruction that no longer exists is NOT re-added;
      * one added meanwhile is left exactly as the user wrote it;
      * the tick's own fields are applied to the rows that survived;
      * `armed` only ever moves True → False. The tick never arms anything, so
        a user disarm during the tick stays a disarm.
    """
    by_id = {str(e.get("id")): e for e in worked if isinstance(e, dict)}
    merged: list[dict] = []
    for cur in standing_list():
        if not isinstance(cur, dict):
            continue
        e = by_id.get(str(cur.get("id")))
        if e is None:
            merged.append(cur)
            continue
        out = dict(cur)
        for k in _TICK_FIELDS:
            if k in e:
                out[k] = e[k]
        if not e.get("armed"):
            out["armed"] = False
        merged.append(out)
    _standing_put(merged)
    return merged


def standing_add(item: dict) -> dict:
    kind = str(item.get("kind") or "")
    if kind not in _VALID_KINDS:
        raise TerminalTradeError(f"Unknown instruction kind '{kind}'.")
    cid = str(item.get("conditionId") or "").strip()
    if not cid:
        raise TerminalTradeError("No market selected.")
    trigger = num(item.get("triggerCents"))
    if trigger is None or not (0 < trigger < 100):
        raise TerminalTradeError(
            "The trigger is a PROBABILITY POINT between 0 and 100 — not a "
            "percentage move.")
    size = num(item.get("size"))
    slip = None
    if kind in _STOP_KINDS:
        if size is None or size <= 0:
            raise TerminalTradeError(
                "A stop or take-profit has to say how many contracts to close.")
        if size > MAX_MANUAL_CONTRACTS:
            raise TerminalTradeError(
                f"Size is over the {MAX_MANUAL_CONTRACTS:,} manual ceiling.")
        raw_slip = num(item.get("maxSlippageCents"))
        slip = (DEFAULT_SELL_SLIPPAGE_CENTS if raw_slip is None
                else max(0.0, min(float(MAX_SELL_SLIPPAGE_CENTS), float(raw_slip))))
    else:
        size = None

    entries = standing_list()
    if _standing_active_count(entries) >= _STANDING_MAX:
        raise TerminalTradeError(
            f"You already have {_STANDING_MAX} active standing instructions, "
            f"which is the most this app keeps. Cancel ones you no longer need "
            f"and add this one again — nothing was added.")

    entry = {
        "id": f"st_{uuid.uuid4().hex[:10]}",
        "conditionId": cid,
        "title": item.get("title") or None,
        "side": "no" if str(item.get("side") or "yes").lower() == "no" else "yes",
        "kind": kind,
        "triggerCents": round(trigger, 2),
        "size": size,
        "maxSlippageCents": slip,
        "createdAt": _now_iso(),
        "armed": True,
        "lastCheckedAt": None,
        "firedAt": None,
        "note": item.get("note") or None,
        "origin": "ai" if item.get("origin") == "ai" else "manual",
    }
    entries.insert(0, entry)
    _standing_put(entries)
    return entry


def standing_cancel(instruction_id: str) -> list[dict]:
    iid = str(instruction_id or "")
    keep: list[dict] = []
    for e in standing_list():
        if e.get("id") == iid:
            _archive_before_drop(e)
            continue
        keep.append(e)
    _standing_put(keep)
    return keep


def standing_set_armed(instruction_id: str, armed: bool) -> list[dict]:
    entries = standing_list()
    for e in entries:
        if e.get("id") == str(instruction_id):
            e["armed"] = bool(armed)
            if armed and e.get("retiredReason") == "failed":
                e["firedAt"] = None
                e["failures"] = 0
                e["retiredReason"] = None
    _standing_put(entries)
    return entries


def _stop_exit_limit(ref_bid_cents: float, slip_cents: float,
                     tick: Optional[float]) -> float:
    """The limit for a stop's marketable fill-and-kill: the reference bid less
    the slippage budget, on the market's tick grid.

    Rounded UP onto the grid, so the order never reaches further below the bid
    than the budget allows; never above the bid itself; and never below one
    tick — nor below 1¢, the least `preview()` and the order builder accept. A
    FAK sells to every resting bid at or above this price, each at ITS price,
    so the limit is the worst case, not the expected fill.
    """
    tc = float(tick or 0.01) * 100.0
    if tc <= 0:
        tc = 1.0
    lo = max(1.0, tc)
    hi = min(99.0, 100.0 - tc)
    bid = float(ref_bid_cents)
    floor = bid - max(0.0, float(slip_cents))
    limit = math.ceil(floor / tc - 1e-9) * tc
    top = math.floor(bid / tc + 1e-9) * tc
    limit = min(limit, top)
    return round(max(lo, min(hi, limit)), 4)


def _stop_fill(res: dict, sent: float) -> tuple[float, bool]:
    """How many contracts a stop's sell actually sold → (sold, confirmed).

    A refusal, a kill with no fill, or a reported fill of zero sold nothing.
    An accepted order whose fill size the venue did not report is assumed to
    have sold the size sent — re-sending on that guess would risk selling the
    same contracts twice — and is marked unconfirmed until it reconciles.
    """
    if not res.get("ok"):
        return 0.0, True
    filled = num(res.get("filledSize"))
    status = str(res.get("status") or "").lower()
    if status in ("unmatched", "killed", "canceled", "cancelled", "rejected"):
        return max(0.0, float(filled or 0.0)), True
    if filled is None:
        return float(sent), False
    return max(0.0, min(float(filled), float(sent))), True


async def standing_tick(*, allow_trading: bool = True, paper: bool = False) -> dict:
    """Evaluate every armed instruction once.

    A stop loss is an instruction the user wrote down, so it executes under
    exactly the rules a manual click passes: paper mode blocks it, the wallet
    must be readable, and every order goes through `place()`. It is NOT held
    to the automation master switch — the terminal is manual, the engines are
    not. `allow_trading=False` remains available to a caller that wants to
    hold fire explicitly.
    """
    paper = bool(paper) or _paper_mode_on()
    entries = standing_list()
    if not entries:
        return {"checked": 0, "fired": 0, "alerts": []}

    checked = 0
    fired = 0
    alerts: list[dict] = []
    by_market: dict[str, Optional[dict]] = {}

    holdings: Optional[dict[tuple[str, str], float]] = None
    if any(e.get("armed") and not e.get("firedAt")
           and e.get("kind") in _STOP_KINDS for e in entries):
        try:
            holdings = {}
            for pos in await api.get_positions(strict=True):
                signed = float(pos.get("position_fp") or 0.0)
                if signed == 0:
                    continue
                key = (str(pos.get("ticker") or ""),
                       "yes" if signed > 0 else "no")
                holdings[key] = holdings.get(key, 0.0) + abs(signed)
        except Exception as e:  # noqa: BLE001
            logger.debug("standing tick: position read failed: %s", e)
            holdings = None

    for e in entries:
        if not e.get("armed") or e.get("firedAt"):
            continue
        cid = str(e.get("conditionId") or "")
        if cid not in by_market:
            try:
                m = await term.market(cid)
                if m:
                    await term.clob_quotes([m])
                by_market[cid] = m
            except Exception:
                by_market[cid] = None
        m = by_market.get(cid)
        e["lastCheckedAt"] = _now_iso()
        checked += 1
        if not m:
            continue

        side = e.get("side") or "yes"
        p = m.get("probability")
        if p is None:
            continue
        price = p if side == "yes" else round(100.0 - p, 2)
        trig = float(e["triggerCents"])
        kind = e["kind"]

        hit = (
            (kind == "stopLoss" and price <= trig)
            or (kind == "takeProfit" and price >= trig)
            or (kind == "alertAbove" and price >= trig)
            or (kind == "alertBelow" and price <= trig)
        )
        if not hit:
            continue

        if kind in ("alertAbove", "alertBelow"):
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "alert"
            alerts.append({
                "id": e["id"], "conditionId": cid,
                "title": m.get("title"), "kind": kind,
                "priceCents": price, "triggerCents": trig,
            })
            fired += 1
            continue

        if paper:
            e["note"] = (
                f"Trigger hit at {price:g}¢ but paper mode is on, so nothing "
                f"was sold. Turn paper mode off and re-arm.")
            e["armed"] = False
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True, "result": e["note"],
            })
            continue

        if not allow_trading:
            e["note"] = (
                f"Trigger hit at {price:g}¢ but live trading is off, so "
                f"nothing was sold. Re-arm after enabling trading.")
            e["armed"] = False
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True,
            })
            continue

        want = standing_remaining(e)
        prior_sold = float(num(e.get("soldSize")) or 0.0)
        if holdings is None:
            e["note"] = (
                f"Trigger hit at {price:g}¢ but your positions could not be "
                f"read just now, so nothing was sold. Still armed — this will "
                f"retry.")
            continue
        have = holdings.get((cid, side), 0.0)
        if have <= 0:
            e["armed"] = False
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "fired" if prior_sold > 0 else "noPosition"
            e["note"] = (
                f"Trigger hit at {price:g}¢, but you no longer hold this "
                f"{side.upper()} position — nothing to sell, so the "
                f"instruction has been retired.")
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True, "result": e["note"],
            })
            continue
        short_note = None
        if have < want:
            short_note = (
                f"Instruction was for {want:g} contract(s) but only {have:g} "
                f"are held — sold {have:g}.")
            want = have
        want = float(math.floor(want + 1e-9))
        if want <= 0:
            e["armed"] = False
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "fired" if prior_sold > 0 else "noPosition"
            e["note"] = (
                f"Trigger hit at {price:g}¢, but less than one whole contract "
                f"is left to sell — the smallest size an order can carry — so "
                f"the instruction has been retired. The rest can only be "
                f"closed by letting the market settle.")
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True, "result": e["note"],
            })
            continue

        slip = num(e.get("maxSlippageCents"))
        slip = (DEFAULT_SELL_SLIPPAGE_CENTS if slip is None
                else max(0.0, min(float(MAX_SELL_SLIPPAGE_CENTS), float(slip))))
        yes_bid, yes_ask = num(m.get("bid")), num(m.get("ask"))
        quote_bid = (yes_bid if side == "yes"
                     else (round(100.0 - yes_ask, 4) if yes_ask is not None else None))
        ref_bid = quote_bid if quote_bid is not None else float(price)
        limit = _stop_exit_limit(ref_bid, slip, None)
        req = {
            "conditionId": cid, "side": side, "action": "sell",
            "priceCents": limit, "size": want, "orderType": "FAK",
        }
        pv = await preview(req)
        book_bid = num((pv or {}).get("bestBidCents"))
        tick = num((pv or {}).get("tickSize"))
        if book_bid is not None or tick is not None:
            better = _stop_exit_limit(
                book_bid if book_bid is not None else ref_bid, slip, tick)
            if abs(better - limit) > 1e-9:
                req["priceCents"] = limit = better
                pv = await preview(req)
        req["confirmUsd"] = (pv or {}).get("costUsd")
        res = await place(req, origin=e.get("origin") or "manual",
                          standing_id=e["id"])
        sold, confirmed = _stop_fill(res, want)
        venue_msg = str(res.get("message") or "")

        if sold > 0:
            total_sold = round(prior_sold + sold, 6)
            e["soldSize"] = total_sold
            e["failures"] = 0
            remaining = round(want - sold, 6)
            final = remaining <= 1e-9
            try:
                _record_stop_sale(e, sold, res.get("orderId"), confirmed,
                                  final=final)
            except Exception as ex:  # noqa: BLE001
                logger.warning("standing: could not record stop sale %s: %s",
                               e.get("id"), ex)
            unconf = ("" if confirmed else
                      " The venue did not report the fill size, so this assumes "
                      "the whole order filled — Track shows the real fill once "
                      "it reconciles.")
            if final:
                e["firedAt"] = _now_iso()
                e["armed"] = False
                e["retiredReason"] = "fired"
                e["note"] = " ".join(x for x in (
                    short_note,
                    f"Sold {sold:g} contract(s) at {limit:g}¢ or better "
                    f"(trigger {trig:g}¢).{unconf}", venue_msg) if x)
                fired += 1
            else:
                e["note"] = " ".join(x for x in (
                    short_note,
                    f"Partly fired: sold {sold:g} of {want:g} at {limit:g}¢ or "
                    f"better; the other {remaining:g} found no bid within "
                    f"{slip:g}¢ and is STILL ARMED — it will retry.{unconf}",
                    venue_msg) if x)
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "result": e["note"], "ok": True, "blocked": False,
                "partial": not final, "soldSize": sold,
                "remainingSize": max(0.0, remaining),
                "limitCents": limit, "status": res.get("status"),
            })
            continue

        if res.get("ok"):
            why = (f"the sell at {limit:g}¢ (bid less {slip:g}¢ slippage) "
                   f"matched nothing")
        elif res.get("status") == "unknown":
            why = "Polymarket did not confirm the sell"
        else:
            why = "the sell was refused"
        fails = int(e.get("failures") or 0) + 1
        e["failures"] = fails
        tail = " ".join(x for x in (short_note, venue_msg) if x)
        if fails >= MAX_STANDING_FAILURES:
            e["armed"] = False
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "fired" if prior_sold > 0 else "failed"
            e["note"] = (
                f"Did NOT fire: {why}. Tried {fails} times and nothing sold — "
                f"giving up so it stops re-sending. Your position is still "
                f"open; sell it from the Market page, or re-arm this. "
                f"Last result: {tail}").strip()
        else:
            e["note"] = (
                f"Trigger hit at {price:g}¢ but the stop did NOT fire: {why}, "
                f"so nothing was sold (attempt {fails} of "
                f"{MAX_STANDING_FAILURES}) — still armed, will retry. "
                f"{tail}").strip()
        alerts.append({
            "id": e["id"], "conditionId": cid, "title": m.get("title"),
            "kind": kind, "priceCents": price, "triggerCents": trig,
            "result": e["note"], "ok": False, "blocked": True,
            "soldSize": 0.0, "limitCents": limit, "status": res.get("status"),
        })

    _standing_merge_put(entries)
    return {"checked": checked, "fired": fired, "alerts": alerts}
