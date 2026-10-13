"""Every realised-fee function charges what the venue charges.

Audit 2026-10-11: there are ~seven fee implementations and nothing checked
them against each other. With a taker-rebate tier set, the main engine's
realised fees (trader._position_fee_usd / _sell_fee_usd, which copy and
script positions also use) were netted of the rebate while the crypto
engine's (crypto15m_trader._order_fee_usd) were not, so the same fill booked
two different fees. Polymarket charges the full fee at match time and pays
the rebate back separately, once a day, so realised accounting books the
full fee everywhere; only forward-looking estimates net the tier.

The bot's own categories (climate, entertainment, world) were also missing
from the fee table: the backtests charged them crypto's 0.07, the live
accounting 0.05, and the Terminal called the fee unknown.

Pure functions only: no network, no database.
"""
from __future__ import annotations

import pytest

import backtest
import crypto15m_trader
import script_engine
import terminal_trade
import trader

VENUE_RATE = {
    "crypto": 0.07, "sports": 0.05, "finance": 0.04, "politics": 0.04,
    "mentions": 0.04, "tech": 0.04, "economics": 0.05, "culture": 0.05,
    "weather": 0.05, "other": 0.05, "geopolitics": 0.0,
    "climate": 0.05, "entertainment": 0.05, "world": 0.05,
}
PRICES = [0.03, 0.25, 0.5, 0.8, 0.97]
CONTRACTS = 100


@pytest.fixture
def rebate_tier():
    """A Gold-tier rebate (18%) is set, the case where the engines split."""
    backtest.set_taker_rebate(18)
    try:
        yield
    finally:
        backtest.set_taker_rebate(0)


def _realised_fees_per_contract(cat: str, price: float) -> dict[str, float]:
    cents = price * 100.0
    pos = {"category": cat, "filled_contracts": CONTRACTS,
           "avg_fill_price_cents": cents, "cost_usd": price * CONTRACTS}
    out = {
        "trader._position_fee_usd": trader._position_fee_usd(pos) / CONTRACTS,
        "trader._sell_fee_usd": trader._sell_fee_usd(pos, CONTRACTS, cents) / CONTRACTS,
        "script_engine._shadow_fee": script_engine._shadow_fee(price, cat),
        "backtest.venue_fee_per_contract":
            backtest.venue_fee_per_contract(price, category=cat),
        "terminal_trade._fee_per_share": terminal_trade._fee_per_share(cents, cat, None),
    }
    if cat == "crypto":
        out["crypto15m_trader._order_fee_usd"] = (
            crypto15m_trader._order_fee_usd({}, CONTRACTS, cents) / CONTRACTS)
    return out


@pytest.mark.parametrize("price", PRICES)
@pytest.mark.parametrize("cat", sorted(VENUE_RATE))
def test_every_realised_fee_function_charges_the_venue_fee(cat, price, rebate_tier):
    want = VENUE_RATE[cat] * price * (1.0 - price)
    got = _realised_fees_per_contract(cat, price)
    off = {k: v for k, v in got.items() if v is None or abs(v - want) > 1e-6}
    assert not off, f"{cat} @ {price}: want {want:.6f}, got {off}"


@pytest.mark.parametrize("cat", ["", "not-a-real-category"])
def test_an_unknown_category_has_one_shared_fallback(cat):
    """trader.py and the backtests rate an unknown category the same way."""
    other = VENUE_RATE["other"] * 0.25
    assert backtest.polymarket_fee_per_contract(0.5, category=cat) == pytest.approx(other)
    pos = {"category": cat, "filled_contracts": 1, "avg_fill_price_cents": 50.0}
    assert trader._position_fee_usd(pos) == pytest.approx(other)


def test_forward_estimates_still_net_the_rebate_tier(rebate_tier):
    """Edge gates and backtests are where the tier belongs."""
    full = backtest.venue_fee_per_contract(0.5, category="crypto")
    assert backtest.polymarket_fee_per_contract(0.5, category="crypto") == \
        pytest.approx(full * 0.82)
