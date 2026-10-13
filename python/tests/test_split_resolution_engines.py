"""A UMA 50-50 split settles at 50c a share for the engines, not as a full win or loss."""
import polymarket_api as api
import trader


def _raw(prices, uma="resolved", closed=True):
    return {"conditionId": "0xsplit", "question": "Game 3 winner?", "closed": closed,
            "umaResolutionStatus": uma, "outcomes": '["Yes","No"]',
            "outcomePrices": prices, "clobTokenIds": '["1","2"]'}


def test_split_settles_at_half_for_both_sides():
    m = api._normalize_market(_raw('["0.5","0.5"]'))
    assert m["result"] == ""
    assert m["settlement_value_dollars"] == 0.5
    assert m["status"] == "settled"
    assert trader._market_yes_payout(m) == 0.5


def test_decided_markets_unchanged():
    assert api._normalize_market(_raw('["1","0"]'))["result"] == "yes"
    assert api._normalize_market(_raw('["0","1"]'))["result"] == "no"
    near = api._normalize_market(_raw('["0.9995","0.0005"]'))
    assert near["result"] == "yes" and near["settlement_value_dollars"] == 1.0


def test_unresolved_half_prices_are_not_a_split():
    m = api._normalize_market(_raw('["0.5","0.5"]', uma="proposed"))
    assert m["result"] == "" and m["settlement_value_dollars"] is None
    assert trader._market_yes_payout(m) is None
