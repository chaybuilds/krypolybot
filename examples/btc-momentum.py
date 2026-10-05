# krypt-script v1
# name: BTC Price Momentum
# description: Buys the moving side of the BTC up/down window on a 1-minute velocity impulse, while the ask is still cheap.

VELOCITY_THRESHOLD = 0.1
ASK_MIN = 0.01
ASK_MAX = 0.65
ORDER_SIZE = 5


def decide(ctx):
    if ctx["asset"] != "BTC":
        return None

    vel = ctx["velocity1mPct"]
    if vel is None:
        return None

    if vel > VELOCITY_THRESHOLD:
        side = "up"
        ask = ctx["upAsk"]
    elif vel < -VELOCITY_THRESHOLD:
        side = "down"
        ask = ctx["downAsk"]
    else:
        return None

    if ask is None:
        return None
    if not (ASK_MIN <= ask <= ASK_MAX):
        return None

    return {
        "side": side,
        "price": "ask",
        "size": ORDER_SIZE,
        "reason": f"vel1m {vel:+.2f}% @ {ask:.2f}",
    }
