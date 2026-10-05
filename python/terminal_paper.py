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


@contextlib.contextmanager
def using_book(book: str) -> Iterator[None]:
    if book not in BOOKS:
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
            st["cost"] += sz * px / 100.0
            cash -= sz * px / 100.0 + fee
        else:
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                st["realized"] += close * (px / 100.0 - avg) - fee
                st["cost"] -= close * avg
                st["size"] -= close
            cash += close * px / 100.0 - fee
            if str(r["order_type"]) == "SETTLE":
                st["settled"] = True
    return pos, cash, fees_known


def _held_gross(condition_id: str, side: str) -> float:
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
    return max(0.0, _held_gross(condition_id, side)
               - _reserved_by_resting_sells(condition_id, side))


def _bankroll(cfg: Optional[dict]) -> float:
    key = ("mcp_paper_bankroll_usd" if current_book() == "ai"
           else "terminal_paper_bankroll_usd")
    v = num((cfg or {}).get(key))
    return v if v and v > 0 else DEFAULT_BANKROLL_USD


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
        got, avg = tt._walk_book(levels, want, limit, buy)
        got = got or 0.0
        if got <= 0 or avg is None:
            continue

        if not buy:
            with using_book(str(r.get("book") or "manual")):
                held_now = _held_gross(cid, side)
            if held_now <= 1e-9:
                with _conn() as conn:
                    conn.execute(
                        """UPDATE terminal_paper_orders
                           SET status='canceled', resting_size=0,
                               note = TRIM(COALESCE(note,'') || ?)
                           WHERE id=?""",
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

        with _conn() as conn:
            conn.execute(
                """UPDATE terminal_paper_orders
                   SET filled_size=?, avg_fill_price_cents=?, resting_size=?,
                       cost_usd=?, status=?, filled_at=COALESCE(filled_at, ?),
                       note = TRIM(COALESCE(note,'') || ?)
                   WHERE id=?""",
                (total, round(new_avg, 3), max(0.0, remaining),
                 round(total * new_avg / 100.0, 4),
                 "filled" if remaining <= 1e-9 else "partial",
                 _now_iso(),
                 f" Market traded through {limit:g}¢, so {got:g} filled as "
                 f"maker (no fee).",
                 int(r["id"])),
            )
        n += 1
    return n


async def _settle_resolved() -> int:
    n = 0
    for book in BOOKS:
        with using_book(book):
            n += await _settle_resolved_book()
    return n


async def _settle_resolved_book() -> int:
    pos, _, _ = _book_state()
    open_keys = [(cid, side) for (cid, side), st in pos.items()
                 if st["size"] > 1e-9 and not st["settled"]]
    if not open_keys:
        return 0

    markets = {}
    try:
        for m in await term._summaries_for([cid for cid, _ in open_keys]):
            markets[m["conditionId"]] = m
    except Exception as e:
        logger.debug("paper settle lookup failed: %s", e)
        return 0

    n = 0
    for cid, side in open_keys:
        m = markets.get(cid)
        if not m or m.get("resolvedOutcome") is None:
            continue
        won = m["resolvedOutcome"] == side
        size = pos[(cid, side)]["size"]
        settle_cents = 100.0 if won else 0.0
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
                f"Market resolved {str(m['resolvedOutcome']).upper()} — this "
                f"side paid {settle_cents:.0f}¢ per contract. Exact, not a mark."),
            "filled_at": _now_iso(),
        })
        n += 1
    return n


async def portfolio(cfg: Optional[dict] = None) -> dict:
    pos, cash_delta, fees_known = _book_state()
    bankroll = _bankroll(cfg)

    open_keys = [(k, st) for k, st in pos.items() if st["size"] > 1e-9]
    marks: dict[str, dict] = {}
    if open_keys:
        try:
            for m in await term._summaries_for([cid for (cid, _), _ in open_keys]):
                marks[m["conditionId"]] = m
        except Exception as e:
            logger.debug("paper portfolio mark lookup failed: %s", e)

    positions = []
    tot_val = 0.0
    any_marked = False
    for (cid, side), st in sorted(pos.items(), key=lambda kv: -kv[1]["size"]):
        if st["size"] <= 1e-9:
            continue
        m = marks.get(cid)
        resolved = (m or {}).get("resolvedOutcome")
        if resolved is not None:
            mark = 100.0 if resolved == side else 0.0
        else:
            p = (m or {}).get("probability")
            mark = p if side == "yes" else (None if p is None else round(100.0 - p, 2))
        avg = round(st["cost"] / st["size"] * 100.0, 3) if st["size"] > 0 else None
        value = round(st["size"] * mark / 100.0, 2) if mark is not None else None
        pnl = (round(st["size"] * (mark - avg) / 100.0, 2)
               if (mark is not None and avg is not None) else None)
        if value is not None:
            tot_val += value
            any_marked = True
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
            "settled": resolved is not None,
            "redeemable": False,
            "unreconciled": False, "unreconciledSize": None,
            "origin": "ai" if current_book() == "ai" else "manual",
            "sources": ({} if mark is None else {
                "probability": {
                    "source": "gamma" if resolved is not None else "clob-ws",
                    "observedAt": _now_iso(), "ageSec": 0.0}}),
            "unknowns": ([] if mark is not None else [{
                "field": "markCents",
                "reason": (
                    "No live price for this market right now, so this paper "
                    "position cannot be valued. Unknown, not zero."),
            }]),
        })

    realized = round(sum(st["realized"] for st in pos.values()), 2)
    cash = round(bankroll + cash_delta, 2)

    note = None
    if not fees_known:
        note = (
            "At least one paper fill happened in a market whose fee rate we "
            "could not read, so the totals below EXCLUDE those fees. The live "
            "ticket warns about the same markets.")

    return {
        "positions": positions,
        "walletReadable": True,
        "totalValueUsd": round(tot_val, 2) if any_marked else None,
        "totalCostUsd": round(sum(st["cost"] for st in pos.values()), 2),
        "totalUnrealizedPnlUsd": (
            round(sum(p["unrealizedPnlUsd"] for p in positions
                      if p["unrealizedPnlUsd"] is not None), 2)
            if any(p["unrealizedPnlUsd"] is not None for p in positions) else None),
        "realizedPnlUsd": realized,
        "settledPnlUsd": None,
        "cashUsd": cash,
        "bankrollUsd": bankroll,
        "equityUsd": round(cash + tot_val, 2) if any_marked else None,
        "returnPct": (
            round((cash + tot_val - bankroll) / bankroll * 100.0, 2)
            if any_marked and bankroll > 0 else None),
        "unreconciledCount": 0,
        "unreconciledNote": note,
        "redeemableCount": 0, "redeemableValueUsd": None,
        "redeemableNote": None,
        "fetchedAt": _now_iso(),
        "costBasisConvention": "average",
    }


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
    with _conn() as conn:
        book = current_book()
        n = int(conn.execute(
            "SELECT COUNT(*) FROM terminal_paper_orders "
            "WHERE COALESCE(book,'manual')=?", (book,)).fetchone()[0])
        conn.execute("DELETE FROM terminal_paper_orders "
                     "WHERE COALESCE(book,'manual')=?", (book,))
    return {"cleared": n,
            "message": f"Paper account reset — {n} simulated order(s) cleared."}
