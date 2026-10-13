"""The trading-readiness funding floor.

What a Polymarket order costs is PRICE-DEPENDENT:

    cost = max(orderMinSize shares, ceil($1 / price) shares) x price

so at <=20c the $1 notional binds ($1.00 minimum order) and at 99c the
5-share minimum binds (~$4.95). A flat "you need $5" is the 99c worst case
and wrongly tells a small but perfectly tradable wallet that it cannot trade
— the bug these tests pin (reported against a $1.64 balance, 2026-07-25).
"""
from __future__ import annotations

import asyncio

import pytest

import polymarket_api


MIN_SHARES = 5
MIN_NOTIONAL = 1.00


def order_cost_usd(price_cents: int, min_shares: int = MIN_SHARES) -> float:
    """The cheapest order the exchange will accept at this price."""
    need_for_notional = -(-100 // price_cents)
    shares = max(min_shares, need_for_notional)
    return round(shares * price_cents / 100.0, 4)


@pytest.mark.parametrize("cents,expected", [
    (1, 1.00),
    (5, 1.00),
    (10, 1.00),
    (20, 1.00),
    (33, 1.65),
    (50, 2.50),
    (90, 4.50),
    (99, 4.95),
])
def test_minimum_order_cost_is_price_dependent(cents, expected):
    assert order_cost_usd(cents) == pytest.approx(expected)


def test_cheapest_possible_order_is_one_dollar_not_five():
    """The floor across ALL prices is $1, not $5 — the whole point."""
    cheapest = min(order_cost_usd(c) for c in range(1, 100))
    assert cheapest == pytest.approx(1.00)


def test_worst_case_is_just_under_five_dollars():
    dearest = max(order_cost_usd(c) for c in range(1, 100))
    assert dearest == pytest.approx(4.95)



def _issues_for(balance: float, monkeypatch) -> list[str]:
    """Run check_trading_ready with everything stubbed except the balance,
    and return the issues it reports."""
    async def _fake_creds():
        return None

    monkeypatch.setattr(polymarket_api, "ensure_api_creds", _fake_creds)
    monkeypatch.setattr(polymarket_api.auth, "trading_address",
                        lambda: "0x" + "ab" * 20)
    monkeypatch.setattr(polymarket_api.auth, "get_address",
                        lambda: "0x" + "cd" * 20)
    monkeypatch.setattr(polymarket_api.auth, "get_funder",
                        lambda: "0x" + "ef" * 20)
    monkeypatch.setattr(polymarket_api.auth, "get_signature_type",
                        lambda: polymarket_api.auth.SIGNATURE_TYPE_POLY_1271)

    async def _bal():
        return {"balance": int(round(balance * 100))}

    monkeypatch.setattr(polymarket_api, "get_balance", _bal)

    async def _probe(*_a, **_k):
        return {"tested": False}

    monkeypatch.setattr(polymarket_api, "verify_order_signer_live", _probe,
                        raising=False)

    async def _get(*_a, **_k):
        return [{"id": "1"}]

    monkeypatch.setattr(polymarket_api, "_get", _get)

    async def _rpc(*_a, **_k):
        return None

    monkeypatch.setattr(polymarket_api, "_rpc_first", _rpc)
    res = asyncio.run(polymarket_api.check_trading_ready())
    return res.get("issues") or []


def _funding_issue(issues: list[str]) -> str | None:
    return next((i for i in issues if "balance is $" in i), None)


def test_balance_that_can_trade_cheap_markets_is_not_told_it_cannot(monkeypatch):
    """$1.64 buys 5 shares at up to 32c. The message must say so rather than
    claim the wallet needs $5 before it can place any order."""
    msg = _funding_issue(_issues_for(1.64, monkeypatch))
    assert msg is not None
    assert "up to" in msg, msg
    assert "add at least $5" not in msg.lower(), msg
    assert "32c" in msg, msg


def test_balance_below_one_dollar_is_a_hard_stop(monkeypatch):
    msg = _funding_issue(_issues_for(0.40, monkeypatch))
    assert msg is not None
    assert "no order can be placed" in msg, msg


def test_balance_below_the_probe_threshold_is_flagged(monkeypatch):
    """$1.00-$1.50 can place an order but can't run the live signer probe, so
    a mispaired wallet would otherwise show a false green."""
    msg = _funding_issue(_issues_for(1.20, monkeypatch))
    assert msg is not None
    assert "verify" in msg.lower(), msg


def test_well_funded_wallet_gets_no_funding_issue(monkeypatch):
    assert _funding_issue(_issues_for(25.0, monkeypatch)) is None


def test_the_advertised_ceiling_matches_the_real_worst_case(monkeypatch):
    """Whatever figure the message tells users to add must actually be enough
    to trade at ANY price — otherwise we send them back for more twice."""
    msg = _funding_issue(_issues_for(2.00, monkeypatch))
    assert msg is not None
    assert "$5" in msg, msg
    assert max(order_cost_usd(c) for c in range(1, 100)) <= 5.0
