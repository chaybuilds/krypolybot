"""Paper trading for the manual terminal.

The point of paper mode is to be WRONG IN THE SAME PLACES the real thing is.
A simulator that fills your whole order at your limit price, instantly, for
free, does not teach you to trade — it teaches you that trading is easy, and
then the first real order behaves nothing like it. So every simplification
here is deliberate, conservative, and stated to the user.

What is simulated faithfully:

* **Fills come out of the REAL book.** We walk the live depth exactly as the
  order ticket's preview does. You get what was actually resting at your price
  and no more; the rest is unfilled, not quietly granted.
* **The price you get is the WALKED average**, not your limit. Sweeping three
  levels costs what sweeping three levels costs.
* **Fees are the real fee-v2 curve**, from the market's own Gamma schedule
  where it has one. An unknown fee stays unknown rather than becoming zero.
* **Tick size and minimum order size** are the venue's, so an order that the
  real CLOB would reject is rejected here too.
* **You cannot sell what you do not hold.** Polymarket has no naked short and
  neither does this.
* **Settlement is exact.** A resolved market pays 100¢ or 0¢, because that is
  what a binary contract does — or 50¢ on BOTH sides when UMA resolves it
  50-50 (Gamma ``outcomePrices == ["0.5","0.5"]``: a cancelled game, a map
  that was never played). See ``split_resolution``.
* **Fees are part of what a position cost.** A taker buy's fee goes into the
  position's cost basis (so ``avgCostCents`` is fee-inclusive), and a sell's
  fee comes off its realised P&L. For every book, at all times:
  ``realised + unrealised == cash + position value − bankroll`` — the P&L the
  record shows is exactly the money the account made, net of every fee.

What is approximated, and why (all of this is surfaced in the UI):

* **Queue position.** We cannot know where in the queue a resting order would
  sit, so a resting order fills only when the market trades STRICTLY THROUGH
  its price — never merely to it. That is the pessimistic assumption: in real
  life you would sometimes fill at the touch. Erring the other way would
  manufacture fills that never happened, which is the exact lie this module
  exists to avoid. When it does fill, it fills at its OWN limit: a maker is
  paid its price, not the better one the book has since moved to.
* **Market impact.** Your paper order consumes real depth in the simulation
  but does not move the real book, so a large order is modelled slightly
  better than it would really be. The UI warns once an order eats a
  meaningful share of the visible depth.

Its own tables, its own totals. A paper number is never added to a real one.

Two paper ACCOUNTS share the table: ``manual`` (the ticket) and ``ai`` (an AI
agent over MCP). Every read and write is scoped to the account selected with
``using_book()``; the default is ``manual``, so every existing caller is
unchanged. An agent's paper record has to stand on its own, or nobody can tell
whether the agent is any good.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any, Iterator, Optional

import polymarket_api as api
import terminal as term
import terminal_trade as tt
from terminal import cents, num, _now_iso

logger = logging.getLogger("krypt.terminal.paper")

DEFAULT_BANKROLL_USD = 1000.0

_IMPACT_WARN_FRACTION = 0.25

BOOKS = ("manual", "ai")
_BOOK: contextvars.ContextVar[str] = contextvars.ContextVar(
    "paper_book", default="manual")


def current_book() -> str:
    return _BOOK.get()


def valid_book(book: str) -> bool:
    """`manual`, `ai` (the Default agent), or `ai:<agent id>` — each named
    agent's own account (mcp_agents.paper_book)."""
    if book in BOOKS:
        return True
    if isinstance(book, str) and book.startswith("ai:"):
        import mcp_agents
        return mcp_agents.valid_id(book[3:])
    return False


def is_agent_book(book: str) -> bool:
    return book == "ai" or (isinstance(book, str) and book.startswith("ai:"))


def books_in_use() -> list[str]:
    """Every paper account with a row, plus the two built-in ones."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT COALESCE(book,'manual') FROM terminal_paper_orders"
        ).fetchall()
    out = list(BOOKS)
    for r in rows:
        b = str(r[0] or "manual")
        if b not in out and valid_book(b):
            out.append(b)
    return out


@contextlib.contextmanager
def using_book(book: str) -> Iterator[None]:
    if not valid_book(book):
        raise ValueError(f"unknown paper book {book!r}")
    token = _BOOK.set(book)
    try:
        yield
    finally:
        _BOOK.reset(token)

_MAX_BOOKS_PER_TICK = 12


def _conn():
    import db
    return db.get_db()


def _net() -> str:
    return tt._net()



def _rows() -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM terminal_paper_orders "
            "WHERE filled_size > 0 AND COALESCE(book,'manual')=? "
            "ORDER BY COALESCE(filled_at, created_at) ASC", (current_book(),))]


def _book_state() -> tuple[dict[tuple[str, str], dict], float, bool]:
    """→ (positions by (conditionId, side), cash_delta, fees_all_known)."""
    pos: dict[tuple[str, str], dict] = {}
    cash = 0.0
    fees_known = True
    for r in _rows():
        key = (str(r["condition_id"]), str(r["side"]))
        st = pos.setdefault(key, {
            "size": 0.0, "cost": 0.0, "realized": 0.0,
            "title": r["title"] or "", "settled": False,
        })
        sz = float(r["filled_size"] or 0.0)
        px = float(r["avg_fill_price_cents"] or 0.0)
        fee = r["fees_usd"]
        if fee is None:
            fees_known = False
            fee = 0.0
        fee = float(fee)

        if str(r["action"]) == "buy":
            st["size"] += sz
            st["cost"] += sz * px / 100.0 + fee
            cash -= sz * px / 100.0 + fee
        else:
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                st["realized"] += close * (px / 100.0 - avg)
                st["cost"] -= close * avg
                st["size"] -= close
            st["realized"] -= fee
            cash += close * px / 100.0 - fee
            if str(r["order_type"]) == "SETTLE":
                st["settled"] = True
    return pos, cash, fees_known


def _held_gross(condition_id: str, side: str) -> float:
    """Contracts held right now, before any resting sell is counted."""
    pos, _, _ = _book_state()
    return float((pos.get((condition_id, side)) or {}).get("size") or 0.0)


def _reserved_by_resting_sells(condition_id: str, side: str) -> float:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(resting_size), 0) FROM terminal_paper_orders "
            "WHERE condition_id=? AND side=? AND action='sell' "
            "AND status IN ('resting','partial') AND COALESCE(book,'manual')=?",
            (condition_id, side, current_book())).fetchone()
    return float(row[0] or 0.0)


def _held(condition_id: str, side: str) -> float:
    """Contracts available to SELL: held, minus what resting sells already
    claim. A resting sell reserves no inventory at the venue either — it is
    simply rejected on fill — but here two GTC sells against one holding used
    to both pass the naked-short check and both fill."""
    return max(0.0, _held_gross(condition_id, side)
               - _reserved_by_resting_sells(condition_id, side))


def _bankroll(cfg: Optional[dict]) -> float:
    key = ("mcp_paper_bankroll_usd" if is_agent_book(current_book())
           else "terminal_paper_bankroll_usd")
    v = num((cfg or {}).get(key))
    return v if v and v > 0 else DEFAULT_BANKROLL_USD



def split_resolution(m: Optional[dict]) -> bool:
    """Did this market resolve 50-50?

    UMA can resolve a binary market as a split — a cancelled or postponed
    game, a map or game of a series that was never played — and then BOTH
    outcome tokens redeem for 50¢. Gamma shows it as ``closed`` with
    ``umaResolutionStatus == "resolved"`` and ``outcomePrices ==
    ["0.5","0.5"]`` (seen live on 2026-10-12, e.g. esports "Game 3: …"
    markets). ``terminal.summary_from_gamma`` only knows a 100/0 resolution,
    so such a market reads ``status: "settled"`` with ``resolvedOutcome:
    None`` — which used to mean "never settles": paper cash stayed locked in a
    position nobody could sell, and the record never included it.

    Read from the summary's ``_raw`` Gamma row (``term._summaries_for`` keeps
    it). All three conditions are required: a closed market that is merely
    AWAITING resolution can show any pair of mids, 50/50 included, and
    settling that at 50¢ would be an invented payout. No ``_raw`` → False.
    """
    if not isinstance(m, dict) or m.get("resolvedOutcome") is not None:
        return False
    raw = m.get("_raw")
    if not isinstance(raw, dict) or not raw.get("closed"):
        return False
    if str(raw.get("umaResolutionStatus") or "").lower() != "resolved":
        return False
    prices = term._jlist(raw.get("outcomePrices"))
    if len(prices) != 2:
        return False
    vals = [num(v) for v in prices]
    return all(v is not None and abs(v - 0.5) < 1e-6 for v in vals)


def payout_cents(m: Optional[dict], side: str) -> Optional[float]:
    """What one contract of `side` pays in this market: 100 / 0 for a
    decided market, 50 on a 50-50 split, None while it is unresolved (or the
    market could not be read). Never a guess."""
    resolved = (m or {}).get("resolvedOutcome")
    if resolved in ("yes", "no"):
        return 100.0 if resolved == side else 0.0
    if split_resolution(m):
        return 50.0
    return None


def _resolution_word(m: dict) -> str:
    r = m.get("resolvedOutcome")
    return str(r).upper() if r in ("yes", "no") else "50-50 (UMA split)"



def _record(row: dict) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO terminal_paper_orders
               (condition_id, title, side, action, order_type, requested_size,
                requested_price_cents, filled_size, avg_fill_price_cents,
                resting_size, fees_usd, cost_usd, status, note, network,
                filled_at, book)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["condition_id"], row.get("title") or "", row["side"],
             row["action"], row["order_type"], row["requested_size"],
             row["requested_price_cents"], row.get("filled_size") or 0.0,
             row.get("avg_fill_price_cents"), row.get("resting_size") or 0.0,
             row.get("fees_usd"), row.get("cost_usd"), row["status"],
             row.get("note") or "", _net(),
             row.get("filled_at"), current_book()),
        )
        return int(cur.lastrowid or 0)


async def place(req: dict, cfg: Optional[dict] = None) -> dict:
    """Simulate one hand-placed order against the live book."""
    pv = await tt.preview({**req, "paper": True})
    blockers = pv["blockers"]
    if blockers:
        return {"ok": False, "localId": None, "message": blockers[0],
                "filledSize": None, "avgPriceCents": None, "status": None}

    cid = str(req["conditionId"])
    side = pv["side"]
    action = pv["action"]
    order_type = str(req.get("orderType") or "GTC").upper()
    size = float(pv["size"] or 0.0)
    price = float(pv["priceCents"] or 0.0)

    summary = await term.market(cid, include_raw=True)
    title = (summary or {}).get("title") or ""

    if action == "sell":
        have = _held(cid, side)
        if have < size - 1e-9:
            return {
                "ok": False, "localId": None, "filledSize": None,
                "avgPriceCents": None, "status": None,
                "message": (
                    f"You hold {have:g} paper {side.upper()} contract(s), so "
                    f"you cannot sell {size:g}. Polymarket has no naked short "
                    f"and neither does paper mode."),
            }

    bk = await term.book(cid, side)
    if not bk.get("available"):
        return {
            "ok": False, "localId": None, "filledSize": None,
            "avgPriceCents": None, "status": None,
            "message": (
                "Could not read the order book, so there is nothing honest to "
                "simulate against. A paper fill invented without a book is "
                f"worse than no fill at all. ({bk.get('note') or 'no book'})"),
        }

    levels = bk["asks"] if action == "buy" else bk["bids"]
    fillable, avg = tt._walk_book(levels, size, price, action == "buy")
    fillable = fillable or 0.0
    warnings = list(pv.get("warnings") or [])

    visible = sum(float(l["size"]) for l in levels) or 0.0
    if visible and fillable / visible > _IMPACT_WARN_FRACTION:
        warnings.append(
            f"This order takes {fillable / visible * 100:.0f}% of the visible "
            f"depth. Paper mode does not move the real book, so a live order "
            f"this size would fill worse than this.")

    note_bits: list[str] = []
    resting = 0.0
    status = "unfilled"

    if order_type == "FOK" and fillable < size - 1e-9:
        fillable, avg = 0.0, None
        status = "unfilled"
        note_bits.append(
            "All-or-none order killed: the book could not fill the whole size "
            "at your price.")
    elif order_type in ("FAK", "FOK"):
        status = "filled" if fillable >= size - 1e-9 else ("partial" if fillable > 0 else "unfilled")
        if fillable < size:
            note_bits.append(
                f"{size - fillable:g} contract(s) killed — nothing was resting "
                f"at your price.")
    else:
        resting = round(size - fillable, 4)
        if fillable > 0 and resting > 0:
            status = "partial"
            note_bits.append(
                f"{fillable:g} crossed immediately; {resting:g} is resting on "
                f"the book and will fill only if the market trades through "
                f"{price:g}¢.")
        elif fillable > 0:
            status = "filled"
        else:
            status = "resting"
            note_bits.append(
                f"Nothing crossed at {price:g}¢, so the whole order is resting. "
                f"Paper mode cannot know your place in the queue, so it fills "
                f"a resting order only when the market trades STRICTLY through "
                f"your price — the pessimistic assumption.")

    fee = None
    if fillable > 0 and avg is not None:
        fee_ps = tt._fee_per_share(
            avg, (summary or {}).get("category"),
            api._fee_schedule_from_gamma((summary or {}).get("_raw") or {}))
        fee = round(fee_ps * fillable, 4) if fee_ps is not None else None
        if fee is None:
            note_bits.append(
                "This market's fee rate is unknown, so the cost below excludes "
                "fees — exactly as the live ticket would tell you.")

    cost = round(fillable * avg / 100.0, 4) if (fillable > 0 and avg is not None) else None

    local = _record({
        "condition_id": cid, "title": title, "side": side, "action": action,
        "order_type": order_type, "requested_size": size,
        "requested_price_cents": price,
        "filled_size": fillable, "avg_fill_price_cents": avg,
        "resting_size": resting, "fees_usd": fee, "cost_usd": cost,
        "status": status, "note": " ".join(note_bits),
        "filled_at": _now_iso() if fillable > 0 else None,
    })

    if fillable > 0:
        msg = (f"Paper {action}: {fillable:g} {side.upper()} at "
               f"{avg:.1f}¢ average"
               + (f" (you asked for {price:g}¢)" if abs(avg - price) > 0.05 else "")
               + f" — ${cost:,.2f}.")
    else:
        msg = f"Paper order recorded with no fill. {note_bits[0] if note_bits else ''}"

    return {
        "ok": True, "localId": local, "status": status,
        "filledSize": fillable or 0.0, "avgPriceCents": avg,
        "message": msg.strip(), "warnings": warnings,
    }


async def cancel(local_id: int) -> dict:
    with _conn() as conn:
        r = conn.execute(
            "SELECT status FROM terminal_paper_orders WHERE id=? "
            "AND COALESCE(book,'manual')=?",
            (int(local_id), current_book())).fetchone()
        if not r:
            return {"ok": False, "message": "No such paper order."}
        if str(r["status"]) not in ("resting", "partial"):
            return {"ok": False, "message": "That paper order is not resting."}
        conn.execute(
            "UPDATE terminal_paper_orders SET status='canceled', resting_size=0 "
            "WHERE id=?", (int(local_id),))
    return {"ok": True, "message": "Paper order canceled."}



async def tick() -> dict:
    """Advance the paper book. Cheap and safe to call every few seconds."""
    filled = await _fill_resting()
    settled = await _settle_resolved()
    return {"filled": filled, "settled": settled}


async def _fill_resting() -> int:
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM terminal_paper_orders "
            "WHERE status IN ('resting','partial') AND resting_size > 0")]
    if not rows:
        return 0

    books: dict[tuple[str, str], dict] = {}
    fetched = 0

    n = 0
    for r in rows:
        cid = str(r["condition_id"])
        side = str(r["side"])
        want = float(r["resting_size"] or 0.0)
        limit = float(r["requested_price_cents"] or 0.0)
        buy = str(r["action"]) == "buy"

        key = (cid, side)
        if key not in books:
            if fetched >= _MAX_BOOKS_PER_TICK:
                break
            fetched += 1
            books[key] = await term.book(cid, side)
        bk = books[key]
        if not bk.get("available"):
            continue
        touch = bk["bestAsk"] if buy else bk["bestBid"]
        if touch is None:
            continue

        crossed = (touch < limit) if buy else (touch > limit)
        if not crossed:
            continue

        levels = bk["asks"] if buy else bk["bids"]
        got, _ = tt._walk_book(levels, want, limit, buy)
        got = got or 0.0
        if got <= 0:
            continue
        avg = limit

        if not buy:
            with using_book(str(r.get("book") or "manual")):
                held_now = _held_gross(cid, side)
            if held_now <= 1e-9:
                with _conn() as conn:
                    conn.execute(
                        """UPDATE terminal_paper_orders
                           SET status='canceled', resting_size=0,
                               note = TRIM(COALESCE(note,'') || ?)
                           WHERE id=? AND status IN ('resting','partial')""",
                        (" Canceled: the position was no longer held when the "
                         "market traded through.", int(r["id"])))
                continue
            got = min(got, held_now)

        prev_size = float(r["filled_size"] or 0.0)
        prev_avg = r["avg_fill_price_cents"]
        total = prev_size + got
        new_avg = (
            (prev_size * float(prev_avg) + got * avg) / total
            if prev_avg is not None and total > 0 else avg)
        remaining = round(want - got, 4)
        fee = r.get("fees_usd")
        if fee is None and prev_size <= 0:
            fee = 0.0

        with _conn() as conn:
            cur = conn.execute(
                """UPDATE terminal_paper_orders
                   SET filled_size=?, avg_fill_price_cents=?, resting_size=?,
                       cost_usd=?, fees_usd=?, status=?,
                       filled_at=COALESCE(filled_at, ?),
                       note = TRIM(COALESCE(note,'') || ?)
                   WHERE id=? AND status IN ('resting','partial')""",
                (total, round(new_avg, 3), max(0.0, remaining),
                 round(total * new_avg / 100.0, 4), fee,
                 "filled" if remaining <= 1e-9 else "partial",
                 _now_iso(),
                 f" Market traded through {limit:g}¢, so {got:g} filled at "
                 f"{limit:g}¢ as maker (no fee).",
                 int(r["id"])),
            )
        if cur.rowcount:
            n += 1
    return n


async def _settle_resolved() -> int:
    """Pay out paper positions in markets that have actually resolved, in
    every paper account."""
    n = 0
    for book in books_in_use():
        with using_book(book):
            n += await _settle_resolved_book()
    return n


async def _settle_resolved_book() -> int:
    """Settle the CURRENT paper account.

    Written as a synthetic sell at exactly 100¢ or 0¢ — 50¢ on both sides of
    a 50-50 split (split_resolution) — with order_type='SETTLE',
    so the average-cost and cash maths stay on one path. Orders still resting
    on a resolved market are canceled: there is no book left to fill them,
    and left resting a buy held its cash and a sell kept claiming contracts
    the settlement already paid out.
    """
    pos, _, _ = _book_state()
    open_keys = [(cid, side) for (cid, side), st in pos.items()
                 if st["size"] > 1e-9 and not st["settled"]]
    resting = _resting_markets()
    if not open_keys and not resting:
        return 0

    markets = {}
    try:
        cids = {cid for cid, _ in open_keys} | resting
        for m in await term._summaries_for(sorted(cids)):
            markets[m["conditionId"]] = m
    except Exception as e:  # noqa: BLE001
        logger.debug("paper settle lookup failed: %s", e)
        return 0

    n = 0
    for cid, side in open_keys:
        m = markets.get(cid)
        settle_cents = payout_cents(m, side)
        if m is None or settle_cents is None:
            continue
        size = pos[(cid, side)]["size"]
        _record({
            "condition_id": cid, "title": m.get("title") or "", "side": side,
            "action": "sell", "order_type": "SETTLE",
            "requested_size": size, "requested_price_cents": settle_cents,
            "filled_size": size, "avg_fill_price_cents": settle_cents,
            "resting_size": 0.0,
            "fees_usd": 0.0,
            "cost_usd": round(size * settle_cents / 100.0, 4),
            "status": "settled",
            "note": (
                f"Market resolved {_resolution_word(m)} — this "
                f"side paid {settle_cents:.0f}¢ per contract. Exact, not a mark."),
            "filled_at": _now_iso(),
        })
        n += 1

    for cid in resting:
        m = markets.get(cid)
        if not m or payout_cents(m, "yes") is None:
            continue
        with _conn() as conn:
            conn.execute(
                """UPDATE terminal_paper_orders
                   SET status='canceled', resting_size=0,
                       note = TRIM(COALESCE(note,'') || ?)
                   WHERE condition_id=? AND COALESCE(book,'manual')=?
                     AND status IN ('resting','partial') AND resting_size > 0""",
                (f" Canceled: the market resolved "
                 f"{_resolution_word(m)} while this was resting.",
                 cid, current_book()))
    return n


def _resting_markets() -> set[str]:
    """conditionIds the CURRENT account has any resting order in."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT condition_id FROM terminal_paper_orders "
            "WHERE status IN ('resting','partial') AND resting_size > 0 "
            "AND COALESCE(book,'manual')=?", (current_book(),)).fetchall()
    return {str(r[0]) for r in rows}



async def portfolio(cfg: Optional[dict] = None) -> dict:
    pos, cash_delta, fees_known = _book_state()
    bankroll = _bankroll(cfg)

    open_keys = [(k, st) for k, st in pos.items() if st["size"] > 1e-9]
    marks: dict[str, dict] = {}
    if open_keys:
        try:
            for m in await term.marked_summaries([cid for (cid, _), _ in open_keys]):
                marks[m["conditionId"]] = m
        except Exception as e:  # noqa: BLE001
            logger.debug("paper portfolio mark lookup failed: %s", e)

    positions = []
    tot_val = 0.0
    tot_unreal = 0.0
    any_unmarked = False
    for (cid, side), st in sorted(pos.items(), key=lambda kv: -kv[1]["size"]):
        if st["size"] <= 1e-9:
            continue
        m = marks.get(cid)
        resolved = (m or {}).get("resolvedOutcome")
        payout = payout_cents(m, side)
        if payout is not None:
            mark = payout
            stamp = {"source": "gamma", "observedAt": _now_iso(), "ageSec": 0.0}
        else:
            p = (m or {}).get("probability")
            mark = p if side == "yes" else (None if p is None else round(100.0 - p, 2))
            stamp = ((m or {}).get("sources") or {}).get("probability")
        avg = round(st["cost"] / st["size"] * 100.0, 3) if st["size"] > 0 else None
        exact_val = st["size"] * mark / 100.0 if mark is not None else None
        value = round(exact_val, 2) if exact_val is not None else None
        pnl = round(exact_val - st["cost"], 2) if exact_val is not None else None
        if exact_val is not None:
            tot_val += exact_val
            tot_unreal += exact_val - st["cost"]
        else:
            any_unmarked = True
        positions.append({
            "conditionId": cid, "venue": "polymarket",
            "title": (m or {}).get("title") or st["title"] or None,
            "slug": (m or {}).get("slug"),
            "side": side, "size": round(st["size"], 2),
            "avgCostCents": avg,
            "costBasisUsd": round(st["cost"], 2),
            "markCents": mark, "valueUsd": value,
            "unrealizedPnlUsd": pnl,
            "realizedPnlUsd": round(st["realized"], 2),
            "settledPnlUsd": None,
            "status": (m or {}).get("status") or "unknown",
            "resolvedOutcome": resolved,
            "closeTime": (m or {}).get("closeTime"),
            "settled": payout is not None,
            "redeemable": False,
            "unreconciled": False, "unreconciledSize": None,
            "origin": "ai" if is_agent_book(current_book()) else "manual",
            "sources": ({"probability": stamp} if (mark is not None and stamp) else {}),
            "unknowns": ([] if mark is not None else [{
                "field": "markCents",
                "reason": (
                    "No live price for this market right now, so this paper "
                    "position cannot be valued. Unknown, not zero."),
            }]),
        })

    realized = round(sum(st["realized"] for st in pos.values()), 2)
    cash = round(bankroll + cash_delta, 2)
    totals_known = not any_unmarked

    note = None
    if not fees_known:
        note = (
            "At least one paper fill happened in a market whose fee rate we "
            "could not read, so the totals below EXCLUDE those fees. The live "
            "ticket warns about the same markets.")

    return {
        "positions": positions,
        "walletReadable": True,
        "totalValueUsd": round(tot_val, 2) if totals_known else None,
        "totalCostUsd": round(sum(st["cost"] for st in pos.values()), 2),
        "totalUnrealizedPnlUsd": round(tot_unreal, 2) if totals_known else None,
        "realizedPnlUsd": realized,
        "settledPnlUsd": None,
        "cashUsd": cash,
        "bankrollUsd": bankroll,
        "equityUsd": round(bankroll + cash_delta + tot_val, 2) if totals_known else None,
        "returnPct": (
            round((cash_delta + tot_val) / bankroll * 100.0, 2)
            if totals_known and bankroll > 0 else None),
        "unreconciledCount": 0,
        "unreconciledNote": note,
        "redeemableCount": 0, "redeemableValueUsd": None,
        "redeemableNote": None,
        "fetchedAt": _now_iso(),
        "costBasisConvention": "average",
    }


async def day_pnl(today: str) -> dict:
    """Today's (UTC date `today`, "YYYY-MM-DD") P&L of the CURRENT paper
    account, for the agents' daily loss stop.

    Same fee convention as `portfolio` (see _book_state): a buy's fee is in
    the position's cost, a sell's fee comes off that sell. Realised: closes
    and settlements filled today against that fee-inclusive average cost,
    less every sell fee paid today. Unrealised: open positions at the price
    they could be SOLD at now — the bid, not the mid — minus their cost, fee
    included (an entry fee is an open loss until the position earns it back);
    a position nobody bids (or whose market could not be read) at its whole
    cost, because the stop must not fail open exactly when the book goes
    quiet. For a book whose whole history is today, realised + unrealised is
    exactly the portfolio's realised + unrealised, marked at the bid."""
    pos: dict[tuple[str, str], dict] = {}
    realized = 0.0
    for r in _rows():
        key = (str(r["condition_id"]), str(r["side"]))
        st = pos.setdefault(key, {"size": 0.0, "cost": 0.0, "settled": False})
        sz = float(r["filled_size"] or 0.0)
        px = float(r["avg_fill_price_cents"] or 0.0)
        fee = float(r["fees_usd"] or 0.0)
        is_today = str(r["filled_at"] or r["created_at"] or "")[:10] == today
        if str(r["action"]) == "buy":
            st["size"] += sz
            st["cost"] += sz * px / 100.0 + fee
        else:
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                if is_today:
                    realized += close * (px / 100.0 - avg)
                st["cost"] -= close * avg
                st["size"] -= close
            if is_today:
                realized -= fee
            if str(r["order_type"]) == "SETTLE":
                st["settled"] = True

    open_keys = [(k, st) for k, st in pos.items() if st["size"] > 1e-9]
    marks: dict[str, dict] = {}
    if open_keys:
        try:
            for m in await term.marked_summaries([cid for (cid, _), _ in open_keys]):
                marks[m["conditionId"]] = m
        except Exception as e:  # noqa: BLE001 — unread marks count at full cost
            logger.debug("paper day_pnl mark lookup failed: %s", e)
    unreal = open_loss = unmarked_cost = 0.0
    unmarked: list[str] = []
    for (cid, side), st in open_keys:
        m = marks.get(cid) or {}
        payout = payout_cents(m, side)
        if payout is not None:
            bid = payout
        elif side == "yes":
            bid = num(m.get("bid"))
        else:
            ask = num(m.get("ask"))
            bid = None if ask is None else 100.0 - ask
        if bid is None:
            unmarked.append(cid)
            unmarked_cost += st["cost"]
        else:
            u = st["size"] * bid / 100.0 - st["cost"]
            unreal += u
            open_loss += max(0.0, -u)
    return {"realizedUsd": round(realized, 4), "unrealizedUsd": round(unreal, 4),
            "openLossUsd": round(open_loss, 4),
            "unmarked": unmarked, "unmarkedCostUsd": round(unmarked_cost, 4)}


def reserved_buy_usd() -> float:
    """Cash the CURRENT account's resting paper buys would still spend if they
    filled. A resting order is not a position, but it is a claim on the cash:
    without this, ten resting buys that each fit the balance could all fill."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(resting_size * requested_price_cents / 100.0), 0) "
            "FROM terminal_paper_orders WHERE action='buy' "
            "AND status IN ('resting','partial') AND COALESCE(book,'manual')=?",
            (current_book(),)).fetchone()
    return float(row[0] or 0.0)


def _resting_buys() -> dict[str, float]:
    """conditionId -> contracts the CURRENT account's resting buys could
    still add."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT condition_id, COALESCE(SUM(resting_size), 0) "
            "FROM terminal_paper_orders WHERE action='buy' "
            "AND status IN ('resting','partial') AND COALESCE(book,'manual')=? "
            "GROUP BY condition_id", (current_book(),)).fetchall()
    return {str(r[0]): float(r[1]) for r in rows if float(r[1] or 0.0) > 1e-9}


def open_markets() -> set[str]:
    """conditionIds the CURRENT paper account holds a position in or has a
    resting buy in. For the agents' position caps: a resting buy is a
    position it can still get, and the live caps count it the same way —
    otherwise any number of resting buys pass the cap and all fill later."""
    pos, _, _ = _book_state()
    return ({cid for (cid, _), st in pos.items() if st["size"] > 1e-9}
            | set(_resting_buys()))


def exposure(condition_id: str) -> float:
    """Contracts the CURRENT account has in one market, both sides, or could
    still get there: what it holds before any resting sell (shares are held
    until a sell FILLS) plus what its resting buys could add. For the agents'
    per-market cap, so it errs high like the live count (mcp_rails.live_exposure)."""
    pos, _, _ = _book_state()
    held = sum(st["size"] for (cid, _), st in pos.items() if cid == condition_id)
    return max(0.0, held) + _resting_buys().get(condition_id, 0.0)


def held(condition_id: str, side: str) -> float:
    """Contracts the CURRENT paper account may sell (net of resting sells)."""
    return _held(condition_id, side)


def orders(limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 500))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM terminal_paper_orders "
            "WHERE COALESCE(book,'manual')=? ORDER BY created_at DESC "
            "LIMIT ?", (current_book(), limit)).fetchall()
    return {
        "orders": [{
            "localId": int(r["id"]),
            "conditionId": str(r["condition_id"]),
            "title": r["title"] or None,
            "side": str(r["side"]), "action": str(r["action"]),
            "orderType": str(r["order_type"]),
            "requestedSize": float(r["requested_size"]),
            "requestedPriceCents": float(r["requested_price_cents"]),
            "filledSize": float(r["filled_size"] or 0.0),
            "avgFillPriceCents": (float(r["avg_fill_price_cents"])
                                  if r["avg_fill_price_cents"] is not None else None),
            "restingSize": float(r["resting_size"] or 0.0),
            "feesUsd": (float(r["fees_usd"]) if r["fees_usd"] is not None else None),
            "costUsd": (float(r["cost_usd"]) if r["cost_usd"] is not None else None),
            "status": str(r["status"]),
            "note": r["note"] or None,
            "createdAt": str(r["created_at"]),
            "filledAt": r["filled_at"] or None,
        } for r in rows],
        "fetchedAt": _now_iso(),
    }


def reset() -> dict:
    """Wipe the paper account back to the starting bankroll."""
    with _conn() as conn:
        book = current_book()
        n = int(conn.execute(
            "SELECT COUNT(*) FROM terminal_paper_orders "
            "WHERE COALESCE(book,'manual')=?", (book,)).fetchone()[0])
        conn.execute("DELETE FROM terminal_paper_orders "
                     "WHERE COALESCE(book,'manual')=?", (book,))
    return {"cleared": n,
            "message": f"Paper account reset — {n} simulated order(s) cleared."}
