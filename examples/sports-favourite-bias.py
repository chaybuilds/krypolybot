# krypt-script v1
# name: Sports Favourite Bias
# description: Buys deep favourites on sports markets and holds to settlement, exploiting the favourite-longshot bias.

FAV_MIN = 0.90
FAV_MAX = 0.99
MIN_VOLUME_24H = 5000.0
MAX_HOURS_TO_CLOSE = 36.0
SIZE_USD = 10.0

_EXCLUDE = ("-spread-", "-over-", "-under-", "-total-", "-handicap-", "-pt5")


def decide_market(market):
    if market["category"] != "sports":
        return None

    if market["volume24h"] < MIN_VOLUME_24H:
        return None

    hrs = market["hoursToClose"]
    if hrs is None or hrs <= 0 or hrs > MAX_HOURS_TO_CLOSE:
        return None

    slug = market["slug"]
    for bad in _EXCLUDE:
        if bad in slug:
            return None

    yes_ask = market["yesAsk"]
    no_ask = market["noAsk"]

    if yes_ask is not None and FAV_MIN <= yes_ask <= FAV_MAX:
        side, ask = "yes", yes_ask
    elif no_ask is not None and FAV_MIN <= no_ask <= FAV_MAX:
        side, ask = "no", no_ask
    else:
        return None

    return {
        "side": side,
        "price": "ask",
        "sizeUsd": SIZE_USD,
        "reason": f"fav {ask:.2f}",
    }
