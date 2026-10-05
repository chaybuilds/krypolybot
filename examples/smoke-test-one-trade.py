# krypt-script v1
# name: Smoke Test - One Real Trade
# description: Places ONE real, minimum-size crypto trade to prove the script engine works end to end, then stops.

BAND_MIN = 0.20
BAND_MAX = 0.35
CONTRACTS = 5
MAX_ATTEMPTS = 1
MIN_MINS_LEFT = 1.5
CASH_BUFFER = 0.25


def on_start(state):
    state.setdefault("attempts", 0)
    state.setdefault("filled", 0)
    log("smoke test armed - will place ONE real order of "
        f"{CONTRACTS} contracts in the {BAND_MIN:.0%}-{BAND_MAX:.0%} band")


def decide(ctx):
    if state.get("filled"):
        return None
    if state.get("attempts", 0) >= MAX_ATTEMPTS:
        return None

    mins = ctx["minsLeft"]
    if mins is None or mins < MIN_MINS_LEFT:
        return None

    bal = ctx["portfolio"]["balanceUsd"]

    for side, ask in (("up", ctx["upAsk"]), ("down", ctx["downAsk"])):
        if ask is None or not (BAND_MIN <= ask <= BAND_MAX):
            continue

        cost = CONTRACTS * ask
        if bal is not None and cost > bal - CASH_BUFFER:
            log(f"skip {ctx['asset']} {side} @ {ask:.2f}: needs ${cost:.2f}, "
                f"balance ${bal:.2f}")
            continue

        state["attempts"] = state.get("attempts", 0) + 1
        log(f"ATTEMPT {state['attempts']}/{MAX_ATTEMPTS}: {ctx['asset']} {side} "
            f"x{CONTRACTS} @ ~{ask:.2f} (~${cost:.2f}), {mins:.1f}m left")
        return {
            "side": side,
            "price": "ask",
            "size": CONTRACTS,
            "reason": f"smoke test @ {ask:.2f}",
        }
    return None


def on_fill(position, state):
    state["filled"] = state.get("filled", 0) + 1
    log(f"FILLED: {position['contracts']} x {position['asset']} "
        f"{position['side']} @ {position['avgEntryCents']}c "
        "- the script engine placed a real order. Test passed.")


def on_settle(position, state):
    outcome = "WON" if position["won"] else "lost"
    log(f"SETTLED {outcome}: {position['asset']} {position['side']}, "
        f"P&L ${position['pnlUsd']:.2f}. Disable this script now.")
