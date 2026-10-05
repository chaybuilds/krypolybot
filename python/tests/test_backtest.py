from __future__ import annotations

import pytest

import backtest as bt


def sig(cost, correct, confidence=60.0, source="whale"):
    return {"source": source, "confidence": confidence, "cost": cost,
            "correct": correct, "category": ""}


def sig_ev(cost, correct, event, confidence=60.0, source="whale"):
    return {"source": source, "confidence": confidence, "cost": cost,
            "correct": correct, "category": "", "event": event}


def test_fee_is_rate_times_p_one_minus_p():
    assert bt.polymarket_fee_per_contract(0.50) == pytest.approx(0.0175)
    assert bt.polymarket_fee_per_contract(0.0) == 0.0
    assert bt.polymarket_fee_per_contract(1.0) == 0.0


def test_fee_is_symmetric_and_vanishes_at_extremes():
    assert bt.polymarket_fee_per_contract(0.9) == pytest.approx(bt.polymarket_fee_per_contract(0.1))
    assert bt.polymarket_fee_per_contract(0.9) < bt.polymarket_fee_per_contract(0.5)
    assert bt.polymarket_fee_per_contract(0.97) == pytest.approx(0.07 * 0.97 * 0.03)


def test_fee_respects_category_and_override():
    assert bt.polymarket_fee_per_contract(0.50, category="sports") == pytest.approx(0.0125)
    assert bt.polymarket_fee_per_contract(0.50, category="geopolitics") == 0.0
    assert bt.polymarket_fee_per_contract(0.50, fee_coeff=0.05) == pytest.approx(0.0125)


def test_net_pnl_win_subtracts_fee():
    assert bt.net_pnl_per_contract(0.30, True) == pytest.approx(0.6853)


def test_net_pnl_loss_adds_fee_to_loss():
    assert bt.net_pnl_per_contract(0.30, False) == pytest.approx(-0.3147)


def test_signal_cost_whale_uses_taker_price():
    assert bt.signal_cost({"price": 0.62}, "whale") == pytest.approx(0.62)


def test_signal_cost_momentum_no_side_inverts():
    assert bt.signal_cost({"price": 0.30, "direction": "no"}, "momentum") == pytest.approx(0.70)
    assert bt.signal_cost({"price": 0.30, "direction": "yes"}, "momentum") == pytest.approx(0.30)


def test_fair_odds_lose_exactly_the_fee():
    s = bt.summarize([sig(0.50, True), sig(0.50, False)])
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["gross_ev"] == pytest.approx(0.0)
    assert s["fee_ev"] == pytest.approx(0.0175)
    assert s["net_ev"] == pytest.approx(-0.0175)


def test_real_edge_survives_fees():
    signals = [sig(0.50, True) for _ in range(6)] + [sig(0.50, False) for _ in range(4)]
    s = bt.summarize(signals)
    assert s["n"] == 10
    assert s["gross_ev"] == pytest.approx(0.10)
    assert s["fee_ev"] == pytest.approx(0.0175)
    assert s["net_ev"] == pytest.approx(0.0825)
    assert s["t"] > 0


def test_empty_summary_is_zeroed():
    s = bt.summarize([])
    assert s["n"] == 0 and s["net_ev"] == 0.0


def test_threshold_sweep_filters_by_confidence():
    signals = [sig(0.5, True, confidence=60), sig(0.5, True, confidence=80)]
    rows = bt.threshold_sweep(signals, [50, 70, 90])
    by_th = {r["threshold"]: r["n"] for r in rows}
    assert by_th == {50: 2, 70: 1, 90: 0}


def test_solo_signals_have_per_event_t_equal_to_per_signal_t():
    signals = [sig(0.50, i < 6) for i in range(10)]
    s = bt.summarize(signals)
    assert s["n_events"] == 10
    assert s["t_event"] == pytest.approx(s["t"])


def test_clustering_deflates_correlated_significance():
    clustered, independent = [], []
    for e in range(10):
        won = e < 6
        for _ in range(5):
            clustered.append(sig_ev(0.50, won, f"E{e}"))
    for i, x in enumerate(clustered):
        independent.append(sig_ev(x["cost"], x["correct"], f"solo{i}"))

    c = bt.summarize(clustered)
    ind = bt.summarize(independent)
    assert c["n"] == 50 and c["n_events"] == 10
    assert ind["n_events"] == 50
    assert c["t"] == pytest.approx(ind["t"])
    assert c["net_ev"] == pytest.approx(ind["net_ev"])
    assert abs(c["t_event"]) < abs(ind["t_event"])
    assert ind["t_event"] == pytest.approx(ind["t"])


def test_verdict_inconclusive_when_too_few_events():
    signals = [sig_ev(0.50, e < 3, f"E{e}") for e in range(5) for _ in range(20)]
    rep = bt.build_report(signals, bt.DEFAULT_FEE_COEFF)
    assert "INCONCLUSIVE" in rep["verdict"]
    assert "events" in rep["verdict"]


def test_verdict_flags_small_samples_inconclusive():
    s = bt.summarize([sig(0.5, True)])
    assert "INCONCLUSIVE" in bt.verdict(s, [])


def test_verdict_calls_negative_when_net_negative():
    signals = [sig(0.5, i % 2 == 0) for i in range(200)]
    rep = bt.build_report(signals, bt.DEFAULT_FEE_COEFF)
    assert "NEGATIVE" in rep["verdict"]


def test_taker_rebate_tier_nets_fee_estimates():
    import backtest as bt
    import crypto15m
    base = bt.polymarket_fee_per_contract(0.5, category="crypto")
    try:
        bt.set_taker_rebate(18)
        crypto15m.set_taker_rebate(18)
        assert bt.polymarket_fee_per_contract(0.5, category="crypto") == pytest.approx(base * 0.82)
        assert crypto15m._fee_cents(50.0) == pytest.approx(0.07 * 0.25 * 100 * 0.82)
        bt.set_taker_rebate(99)
        assert bt.TAKER_REBATE == pytest.approx(0.5)
    finally:
        bt.set_taker_rebate(0)
        crypto15m.set_taker_rebate(0)
    assert bt.polymarket_fee_per_contract(0.5, category="crypto") == pytest.approx(base)
