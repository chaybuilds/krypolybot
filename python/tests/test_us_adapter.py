"""Offline/isolated safety tests for the official US order adapter. No venue POSTs."""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

import pytest

import polymarket_us_adapter as us


SLUG = "cpc-btc-150k-12-31-2026"


class FakeMarkets:
    open = True
    quote = "0.05"
    bid = "0.04"
    tick = 0.01

    def retrieve_by_slug(self, slug):
        return {"market": {"slug": slug, "active": self.open,
                           "closed": not self.open,
                           "status": "MARKET_STATUS_OPEN" if self.open else "MARKET_STATUS_CLOSED",
                           "orderPriceMinTickSize": self.tick}}

    def bbo(self, slug):
        return {"marketData": {"marketSlug": slug,
                 "state": "MARKET_STATE_OPEN" if self.open else "MARKET_STATE_CLOSED",
                 "bestAsk": {"value": self.quote}, "bestBid": {"value": self.bid}}}

    def book(self, slug):
        return {"marketData": {"state": "MARKET_STATE_OPEN", "bids": [], "offers": []}}


class FakeOrders:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.cancelled = []
        self.remote_state = "ORDER_STATE_NEW"
        self.preview_fails = False

    def preview(self, params):
        if self.preview_fails:
            raise ValueError("venue rejected")
        return {"order": {"state": "ORDER_STATE_NEW", "id": "preview-test"}}

    def retrieve(self, oid):
        return {"order": {"id": oid, "state": self.remote_state,
                          "cumQuantity": 10 if self.remote_state == "ORDER_STATE_FILLED" else 0,
                          "leavesQuantity": 0 if self.remote_state == "ORDER_STATE_FILLED" else 10,
                          "avgPx": {"value": "0.05", "currency": "USD"}}}

    def create(self, params):
        self.sent.append(params)
        if self.fail:
            raise TimeoutError("uncertain")
        return {"id": "order-test-1"}

    def cancel(self, oid, opts):
        self.cancelled.append((oid, opts))
        return {"cancelRequested": True}

    def list(self):
        return {"orders": []}


class FakePortfolio:
    available = "12"

    def positions(self, params=None):
        return {"positions": {SLUG: {"qtyAvailableDecimal": self.available,
                                    "marketMetadata": {"slug": SLUG}}}}

    def activities(self):
        return {"activities": []}


class FakeAccount:
    def balances(self):
        return {"balances": []}


class FakeClient:
    def __init__(self):
        self.markets = FakeMarkets()
        self.orders = FakeOrders()
        self.portfolio = FakePortfolio()
        self.account = FakeAccount()
        self.search = type("S", (), {"query": lambda self, p: {"events": []}})()
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def safe_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(us, "_profile", lambda: tmp_path)
    monkeypatch.setattr(us, "_credentials", lambda: None)
    monkeypatch.delenv("ORACLE_US_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(us, "_session_spend", Decimal("0"))
    us.disarm()
    yield
    us.disarm()


@pytest.fixture
def venue(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(us, "_client", lambda *a, **k: c)
    monkeypatch.setattr(us, "_authenticated", lambda: c)
    return c


def order(price="0.05", quantity=10, intent="ORDER_INTENT_BUY_LONG", **kw):
    return {"marketSlug": SLUG, "intent": intent,
            "price": price, "quantity": quantity, **kw}


def test_starts_read_only_and_no_auto_trading(monkeypatch):
    monkeypatch.setattr(us, "_credentials", lambda: None)
    status = us.status()
    assert status["mode"] == "READ_ONLY"
    assert not status["armed"]
    assert not status["autoTrading"]
    assert not status["liveConfigured"]


def test_hard_gate_rejects_live(venue):
    with pytest.raises(us.USExchangeError, match="Deployment"):
        us.place(order(requestId=str(uuid.uuid4())))
    assert venue.orders.sent == []


def test_disarmed_rejects_even_with_deployment_gate(venue, monkeypatch):
    monkeypatch.setenv("ORACLE_US_ALLOW_LIVE", "1")
    with pytest.raises(us.USExchangeError, match="disarmed"):
        us.place(order(requestId=str(uuid.uuid4())))
    assert venue.orders.sent == []


@pytest.mark.parametrize("bad", [None, 0, -1, "-0.01", "NaN", "Infinity", "1", "1.01", True])
def test_invalid_limit_price_rejected(venue, bad):
    with pytest.raises(us.USExchangeError):
        us.preview(order(price=bad))


@pytest.mark.parametrize("bad", [None, 0, -2, "NaN", 1.5, True, 10001])
def test_invalid_quantity_rejected(venue, bad):
    with pytest.raises(us.USExchangeError):
        us.preview(order(quantity=bad))


def test_order_notional_cap(venue):
    with pytest.raises(us.USExchangeError, match="cap"):
        us.preview(order(price="0.50", quantity=11))


def test_market_closed_rejected(venue):
    venue.markets.open = False
    with pytest.raises(us.USExchangeError, match="inactive"):
        us.preview(order())


def test_market_bad_slug_rejected(venue):
    with pytest.raises(us.USExchangeError, match="slug"):
        us.preview(order(marketSlug="https://polymarket.com"))


def test_quote_slippage_limit(venue):
    with pytest.raises(us.USExchangeError, match="ask"):
        us.preview(order(price="0.08"))


def test_missing_live_ask_rejected(venue):
    venue.markets.quote = None
    with pytest.raises(us.USExchangeError):
        us.preview(order())


def test_tick_size_refused(venue):
    with pytest.raises(us.USExchangeError, match="tick"):
        us.preview(order(price="0.051"))


def test_unsupported_intent_rejected(venue):
    with pytest.raises(us.USExchangeError, match="Only"):
        us.preview(order(intent="ORDER_INTENT_BUY_SHORT"))


def test_sell_requires_confirmed_inventory(venue):
    venue.portfolio.available = "3"
    with pytest.raises(us.USExchangeError, match="Insufficient"):
        us.preview(order(price="0.04", quantity=10, intent="ORDER_INTENT_SELL_LONG"))


def test_sell_preview_with_inventory(venue):
    assert us.preview(order(price="0.04", quantity=10,
                            intent="ORDER_INTENT_SELL_LONG"))["grossUsd"] == "0.40"


def _enable(monkeypatch):
    monkeypatch.setenv("ORACLE_US_ALLOW_LIVE", "1")
    monkeypatch.setattr(us, "_armed_until", time.monotonic() + 60)


def test_real_submission_journals_uuid_and_no_fill_claim(venue, monkeypatch):
    _enable(monkeypatch)
    rid = str(uuid.uuid4())
    actual = us.place(order(requestId=rid))
    assert actual["requestId"] == rid
    assert actual["submissionAccepted"] is True
    assert actual["fillConfirmed"] is False
    assert len(venue.orders.sent) == 1
    assert us.journal()["orders"][0]["state"] == "submitted"
    with pytest.raises(us.USExchangeError, match="Duplicate"):
        us.place(order(requestId=rid))
    assert len(venue.orders.sent) == 1


def test_ambiguous_error_is_not_retried(venue, monkeypatch):
    _enable(monkeypatch)
    venue.orders.fail = True
    rid = str(uuid.uuid4())
    with pytest.raises(us.USExchangeError, match="uncertain"):
        us.place(order(requestId=rid))
    assert len(venue.orders.sent) == 1
    assert us.journal()["orders"][0]["state"] == "uncertain"
    with pytest.raises(us.USExchangeError, match="Duplicate"):
        us.place(order(requestId=rid))
    assert len(venue.orders.sent) == 1


def test_session_limit(venue, monkeypatch):
    _enable(monkeypatch)
    venue.markets.quote = "0.50"
    for _ in range(3):
        us.place(order(price="0.50", quantity=10, requestId=str(uuid.uuid4())))
    with pytest.raises(us.USExchangeError, match="Session"):
        us.place(order(price="0.50", quantity=1, requestId=str(uuid.uuid4())))


def test_sell_cancel_allowed_after_disarm(venue):
    output = us.cancel("order-1", SLUG)
    assert output["cancelRequested"] is True
    assert venue.orders.cancelled == [("order-1", {"marketSlug": SLUG})]


def test_account_and_reconciliation_from_venue(venue):
    assert "balances" in us.account()
    out = us.reconcile()
    assert "remoteOrders" in out
    assert "activities" in out
    assert "localOrders" in out
    assert "verified exchange order states" in out["notice"]


def test_preview_does_not_place_order(venue):
    report = us.preview(order())
    assert report["grossUsd"] == "0.50"
    assert report["feeAdjustedProfitUnknown"] is True
    assert venue.orders.sent == []


def test_arm_requires_deployment_flag_and_exact_phrase(venue):
    with pytest.raises(us.USExchangeError):
        us.arm("yes")
    with pytest.raises(us.USExchangeError):
        us.arm("I AUTHORIZE REAL MONEY US ORDERS")
    assert us._armed_until == 0.0


def test_authenticated_preview_validates_with_exchange(venue, monkeypatch):
    monkeypatch.setattr(us, "_credentials", lambda: ("key-id", "secret"))
    result = us.preview(order())
    assert result["exchangeValidated"] is True
    assert result["venuePreview"]["order"]["state"] == "ORDER_STATE_NEW"
    assert venue.orders.sent == []


def test_venue_preview_rejection_prevents_real_post(venue, monkeypatch):
    _enable(monkeypatch)
    venue.orders.preview_fails = True
    with pytest.raises(us.USExchangeError, match="preview rejected"):
        us.place(order(requestId=str(uuid.uuid4())))
    assert venue.orders.sent == []
    assert us.journal()["orders"] == []


def test_reconcile_real_fill_state_not_merely_submission(venue, monkeypatch):
    _enable(monkeypatch)
    rid = str(uuid.uuid4())
    us.place(order(requestId=rid))
    assert us.journal()["orders"][0]["state"] == "submitted"
    venue.orders.remote_state = "ORDER_STATE_FILLED"
    outcome = us.reconcile()
    assert outcome["reconciledOrders"][0]["venueState"] == "ORDER_STATE_FILLED"
    assert outcome["reconciledOrders"][0]["filledQuantity"] == 10
    assert us.journal()["orders"][0]["state"] == "filled"


def test_cancel_request_requires_remote_confirmation(venue, monkeypatch):
    _enable(monkeypatch)
    us.place(order(requestId=str(uuid.uuid4())))
    us.disarm()
    us.cancel("order-test-1", SLUG)
    assert us.journal()["orders"][0]["state"] == "cancel-requested"
    venue.orders.remote_state = "ORDER_STATE_CANCELED"
    us.reconcile()
    assert us.journal()["orders"][0]["state"] == "canceled"


def test_sell_not_blocked_by_buy_spend_limit(venue, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(us, "_session_spend", us.MAX_SESSION)
    venue.orders.remote_state = "ORDER_STATE_NEW"
    result = us.place(order(price="0.04", quantity=10,
                            intent="ORDER_INTENT_SELL_LONG",
                            requestId=str(uuid.uuid4())))
    assert result["submissionAccepted"] is True
    assert us._session_spend == us.MAX_SESSION
