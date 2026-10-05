from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "wipe.db")
    db.init_db()
    return tmp_path


ENV = "mainnet"


def _shadow(conn, *, sid="s1", ticker="0xa", close_time="", resolved=0):
    return db.insert_script_shadow(conn, {
        "script_id": sid, "source": "signal", "signal_source": "market",
        "signal_id": None, "asset": "sports", "ticker": ticker, "side": "yes",
        "contracts": 10, "entry_cents": 60, "order_type": "GTC",
        "reason": "t", "close_time": close_time, "network": ENV,
    }) if not resolved else _shadow_resolved(conn, sid, ticker)


def _shadow_resolved(conn, sid, ticker):
    rid = db.insert_script_shadow(conn, {
        "script_id": sid, "source": "signal", "signal_source": "market",
        "signal_id": None, "asset": "sports", "ticker": ticker, "side": "yes",
        "contracts": 10, "entry_cents": 60, "order_type": "GTC",
        "reason": "t", "close_time": "", "network": ENV,
    })
    conn.execute("UPDATE script_shadow_orders SET resolved=1 WHERE id=?", (rid,))
    return rid


def _count_shadow() -> int:
    with db.get_db() as c:
        return c.execute("SELECT COUNT(*) FROM script_shadow_orders").fetchone()[0]


def test_clear_trade_history_wipes_the_shadow_ledger(fresh_db):
    with db.get_db() as c:
        _shadow(c, ticker="0xa")
    assert _count_shadow() == 1
    db.clear_trade_history()
    assert _count_shadow() == 0


def test_factory_reset_wipes_the_shadow_ledger(fresh_db):
    with db.get_db() as c:
        _shadow(c, ticker="0xa")
    db.factory_reset()
    assert _count_shadow() == 0


def test_a_wipe_stamps_a_reconcile_floor(fresh_db):
    with db.get_db() as c:
        assert db.terminal_reconcile_floor(c) is None
    db.clear_trade_history()
    with db.get_db() as c:
        floor = db.terminal_reconcile_floor(c)
    assert floor and floor.endswith("Z")


def test_factory_reset_stamps_the_floor_after_wiping_app_kv(fresh_db):
    db.factory_reset()
    with db.get_db() as c:
        assert db.terminal_reconcile_floor(c) is not None


def test_cleanup_prunes_settled_shadow_orders_and_keeps_open_ones(fresh_db):
    old = (datetime.now(timezone.utc) - timedelta(days=90)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db.get_db() as c:
        old_done = _shadow_resolved(c, "s1", "0xold")
        old_open = _shadow(c, ticker="0xopen")
        _shadow_resolved(c, "s1", "0xrecent")
        c.execute("UPDATE script_shadow_orders SET created_at=? WHERE id IN (?,?)",
                  (old, old_done, old_open))

    db.cleanup_old_data()

    with db.get_db() as c:
        left = {r["ticker"] for r in c.execute(
            "SELECT ticker FROM script_shadow_orders")}
    assert left == {"0xopen", "0xrecent"}


def test_an_old_row_that_can_still_settle_keeps_its_cap_slot(fresh_db):
    future = (datetime.now(timezone.utc) + timedelta(days=21)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc)
           - timedelta(days=db.SHADOW_STALE_DAYS + 1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db.get_db() as c:
        rid = _shadow(c, ticker="0xlong", close_time=future)
        c.execute("UPDATE script_shadow_orders SET created_at=? WHERE id=?",
                  (old, rid))
        assert db.count_open_script_shadow(c, "s1", ENV) == 1
        assert db.resolve_script_shadow(c, lambda *_: 0.0, market_results={}) == 0
        assert db.count_open_script_shadow(c, "s1", ENV) == 1


def test_a_row_the_settler_can_never_reach_still_ages_out(fresh_db):
    old = (datetime.now(timezone.utc)
           - timedelta(days=db.SHADOW_STALE_DAYS + 1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db.get_db() as c:
        rid = _shadow(c, ticker="0xnoclose", close_time="")
        assert db.count_open_script_shadow(c, "s1", ENV) == 1
        c.execute("UPDATE script_shadow_orders SET created_at=? WHERE id=?",
                  (old, rid))
        assert db.count_open_script_shadow(c, "s1", ENV) == 0


def _c15(conn, *, dry_run: int, pnl: float, ticker: str):
    conn.execute(
        """INSERT INTO crypto15m_positions
             (asset, series, ticker, side, direction, target_contracts,
              filled_contracts, entry_limit_cents, client_order_id, status,
              network, dry_run, resolved, outcome_correct, pnl_usd,
              resolved_at)
           VALUES ('BTC','S',?,'up','yes',10,10,60,?,'settled',?,?,1,?,?,
                   datetime('now'))""",
        (ticker, f"co_{ticker}_{dry_run}", ENV, dry_run,
         1 if pnl > 0 else 0, pnl),
    )


def test_crypto15m_stats_ignores_dry_run_rows(fresh_db):
    with db.get_db() as c:
        _c15(c, dry_run=0, pnl=-2.0, ticker="0xreal")
        _c15(c, dry_run=1, pnl=50.0, ticker="0xfake")
        stats = db.crypto15m_stats(c, ENV)
    assert stats["total"] == 1
    assert stats["wins"] == 0 and stats["losses"] == 1
    assert stats["realizedPnlUsd"] == -2.0


def test_the_loss_breaker_ignores_dry_run_rows(fresh_db):
    with db.get_db() as c:
        _c15(c, dry_run=0, pnl=-30.0, ticker="0xreal")
        _c15(c, dry_run=1, pnl=1000.0, ticker="0xfake")
        assert db.lifetime_realized_pnl(c, "crypto15m", ENV) == -30.0


def test_the_signals_whale_read_walks_an_index(fresh_db):
    with db.get_db() as c:
        plan = " ".join(str(r[-1]) for r in c.execute(
            """EXPLAIN QUERY PLAN
               SELECT * FROM whale_trades WHERE confidence >= ?
               ORDER BY created_at DESC LIMIT ?""", (0.0, 200)))
    assert "idx_whale_time" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan
