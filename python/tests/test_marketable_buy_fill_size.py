"""A marketable buy takes MORE shares than its size when it fills below the
limit — read from the live reply of 2026-10-12 (6 @ 20¢ filled 7.058824 at 17¢)."""
import mcp_rails
import terminal_trade as tt


def _resp(**raw):
    return {"raw": {"status": "matched", **raw}}


def test_buy_fill_above_size_is_shares_not_base_units():
    r = _resp(makingAmount="1.2", takingAmount="7.058824")
    assert abs(tt._matched_size(r, "buy", 6, 20.0) - 7.058824) < 1e-9


def test_base_units_still_convert():
    r = _resp(makingAmount="1200000", takingAmount="7058824")
    assert abs(tt._matched_size(r, "buy", 6, 20.0) - 7.058824) < 1e-9
    assert abs(tt._matched_size(_resp(makingAmount="7000000"), "sell", 7, 16.0) - 7.0) < 1e-9


def test_sell_reads_making_amount_and_never_exceeds_size():
    assert tt._matched_size(_resp(makingAmount="7", takingAmount="1.12"), "sell", 7, 16.0) == 7.0


def test_without_a_limit_the_old_bound_holds():
    assert tt._matched_size(_resp(takingAmount="7000000"), "buy", 7) == 7.0


def test_ledger_books_the_full_spend_and_the_extra_shares():
    assert mcp_rails._fak_status(7.058824, 6.0) == "filled"
