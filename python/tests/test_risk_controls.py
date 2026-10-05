from __future__ import annotations

import asyncio
import itertools

import pytest

import db
import trader


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "risk-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


@pytest.fixture
def env_net(monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    return "mainnet"


@pytest.fixture
def cfg():
    from config import merge_with_defaults
    c = merge_with_defaults({})
    c["network"] = "mainnet"
    return c


def run_async(coro):
    return asyncio.run(coro)


_ids = itertools.count(1)


def seed_position(**over) -> int:
    n = next(_ids)
    row = {
        "signal_source": over.get("signal_source", "whale"),
        "signal_id": over.get("signal_id", n),
        "ticker": over.get("ticker", f"TCK-{n}"),
        "event_ticker": over.get("event_ticker", ""),
        "direction": over.get("direction", "yes"),
        "target_contracts": over.get("target_contracts", 10),
        "limit_price_cents": over.get("limit_price_cents", 50),
        "filled_contracts": over.get("filled_contracts", 0),
        "cost_usd": over.get("cost_usd", 0.0),
        "client_order_id": over.get("client_order_id", f"rc-{n}"),
        "order_id": over.get("order_id"),
        "status": over.get("status", "filled"),
        "network": over.get("network", "mainnet"),
    }
    with db.get_db() as conn:
        return db.insert_bot_position(conn, row)


def fetch(pid: int) -> dict:
    with db.get_db() as conn:
        return db.fetch_position_by_id(conn, pid)


def test_daily_cap_ignores_terminal_failed_orders(fresh_db):
    seed_position(status="submitted")
    seed_position(status="partial", filled_contracts=3)
    seed_position(status="filled", filled_contracts=10, cost_usd=5.0)
    seed_position(status="canceled")
    seed_position(status="error")
    seed_position(status="gone")
    seed_position(status="expired")
    with db.get_db() as conn:
        assert db.count_new_positions_today(conn, "mainnet") == 3


def test_daily_cap_burst_of_cancels_does_not_halt_entries(fresh_db):
    for _ in range(50):
        seed_position(status="canceled")
    with db.get_db() as conn:
        assert db.count_new_positions_today(conn, "mainnet") == 0


def test_daily_cap_still_excludes_external_imports(fresh_db):
    seed_position(status="filled", filled_contracts=5, cost_usd=2.0)
    seed_position(status="filled", filled_contracts=5, cost_usd=2.0,
                  signal_source="external")
    with db.get_db() as conn:
        assert db.count_new_positions_today(conn, "mainnet") == 1


def test_daily_cap_counts_resolved_today_position(fresh_db):
    pid = seed_position(status="filled", filled_contracts=5, cost_usd=2.0)
    with db.get_db() as conn:
        db.update_bot_position(conn, pid, resolved=1)
        assert db.count_new_positions_today(conn, "mainnet") == 1


def test_daily_cap_env_scoped(fresh_db):
    seed_position(status="filled", network="mainnet")
    seed_position(status="filled", network="amoy")
    with db.get_db() as conn:
        assert db.count_new_positions_today(conn, "mainnet") == 1
        assert db.count_new_positions_today(conn) == 2


async def _no_positions(*_a, **_k):
    return []


def test_poll_collapses_target_when_partial_remainder_canceled(
    fresh_db, env_net, cfg, monkeypatch
):
    trader._poll_failures.clear()
    pid = seed_position(status="submitted", order_id="OID-CP",
                        target_contracts=5, limit_price_cents=60)

    async def _canceled_partial(_oid):
        return {"order": {
            "status": "CANCELED", "size_matched": "2", "original_size": "5",
            "price": "0.60",
        }}

    monkeypatch.setattr(trader, "get_positions", _no_positions)
    monkeypatch.setattr(trader, "get_order", _canceled_partial)

    run_async(trader.poll_open_orders(cfg))
    row = fetch(pid)
    assert row["status"] == "partial"
    assert row["target_contracts"] == 2
    assert row["filled_contracts"] == 2
    with db.get_db() as conn:
        assert db.current_total_exposure_usd(conn, "mainnet") == pytest.approx(1.20)


def test_poll_keeps_target_for_resting_partial(fresh_db, env_net, cfg, monkeypatch):
    trader._poll_failures.clear()
    pid = seed_position(status="submitted", order_id="OID-RP",
                        target_contracts=5, limit_price_cents=60)

    async def _resting_partial(_oid):
        return {"order": {
            "status": "LIVE", "size_matched": "2", "original_size": "5",
            "price": "0.60",
        }}

    monkeypatch.setattr(trader, "get_positions", _no_positions)
    monkeypatch.setattr(trader, "get_order", _resting_partial)

    run_async(trader.poll_open_orders(cfg))
    row = fetch(pid)
    assert row["status"] == "partial"
    assert row["target_contracts"] == 5


def test_crypto15m_strategy_stats_groups_and_sums(fresh_db):
    def _c15(strategy, pnl, fees=0.0, exit_fees=0.0, n=[0]):
        n[0] += 1
        with db.get_db() as conn:
            pid = db.insert_crypto15m_position(conn, {
                "asset": "BTC", "series": "S", "ticker": f"T-{n[0]}",
                "side": "up", "direction": "yes", "target_contracts": 5,
                "filled_contracts": 5, "entry_limit_cents": 60,
                "client_order_id": f"cs-{n[0]}", "status": "settled",
                "network": "mainnet", "strategy": strategy,
            })
            db.update_crypto15m_position(
                conn, pid, resolved=1, pnl_usd=pnl,
                fees_usd=fees, exit_fees_usd=exit_fees,
            )

    _c15("favorite", 2.0, fees=0.10)
    _c15("favorite", -1.0, exit_fees=0.05)
    _c15("rules", 3.0)
    _c15(None, 1.0)

    with db.get_db() as conn:
        rows = {r["strategy"]: r for r in db.crypto15m_strategy_stats(conn, "mainnet")}

    assert rows["favorite"]["n"] == 2
    assert rows["favorite"]["wins"] == 1 and rows["favorite"]["losses"] == 1
    assert rows["favorite"]["pnl_usd"] == pytest.approx(1.0)
    assert rows["favorite"]["fees_usd"] == pytest.approx(0.15)
    assert rows["rules"]["pnl_usd"] == pytest.approx(3.0)
    assert rows["directional"]["n"] == 1
