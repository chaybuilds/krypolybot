from __future__ import annotations

import asyncio
import json
import sys
import time
import types

import pytest

import ai_analysis as ai
import config
import terminal


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fresh(host: str):
    gate = terminal._gates[host]
    gate._lock = None
    return gate


def test_schema_has_no_numeric_field():
    for name, spec in ai._SCHEMA["properties"].items():
        assert spec.get("type") != "number", f"{name} is numeric"
        assert spec.get("type") != "integer", f"{name} is numeric"
        if spec.get("type") == "array":
            assert spec.get("items", {}).get("type") == "string", name


def test_schema_is_closed():
    assert ai._SCHEMA["additionalProperties"] is False
    assert set(ai._SCHEMA["required"]) == set(ai._SCHEMA["properties"])


def test_lean_is_a_closed_vocabulary():
    assert ai._SCHEMA["properties"]["lean"]["enum"] == ["cheap", "rich", "fair", "no_view"]


def test_reasoning_is_generated_before_the_conclusion():
    order = list(ai._SCHEMA["properties"])
    assert order.index("reasoning") < order.index("lean")
    assert order.index("unknowns") < order.index("lean")


def test_system_prompt_forbids_a_numeric_estimate():
    assert "NEVER state a numeric probability" in ai._SYSTEM


@pytest.mark.parametrize("text", [
    "I'd put it at around 70%.",
    "My estimate is 68c on this one.",
    "Fair value is closer to 55 cents.",
    "The probability is about 30%.",
    "There is roughly a 65% chance of that.",
    "It should be trading at 40c, not 25c.",
    "I would say 80% likely.",
])
def test_scrubber_removes_model_estimates(text):
    cleaned, removed = ai._strip_numeric_estimates(text)
    assert removed == 1
    assert cleaned == ""


@pytest.mark.parametrize("text", [
    "CPI came in at 2.4% in August.",
    "This resolves YES only above $110,000.",
    "The Fed meets on September 17.",
    "Three of the last four cuts were 25bps.",
    "The poll had him at 47 points with a 3 point margin of error.",
    "Turnout was 62% in the 2024 cycle.",
])
def test_scrubber_keeps_facts_about_the_world(text):
    cleaned, removed = ai._strip_numeric_estimates(text)
    assert removed == 0
    assert cleaned == text


def test_scrubber_is_sentence_scoped():
    text = "The Fed meets on September 17. I'd put it at 70%. CPI was 2.4%."
    cleaned, removed = ai._strip_numeric_estimates(text)
    assert removed == 1
    assert "September 17" in cleaned
    assert "2.4%" in cleaned
    assert "70%" not in cleaned


def test_clean_list_counts_every_removal():
    items = ["My estimate is 60c.", "The FOMC meets Sept 17.", "Fair value is 40."]
    out, removed = ai._clean_list(items)
    assert removed == 2
    assert out == ["The FOMC meets Sept 17."]


_ACCOUNT_LEAKS = (
    "position", "wallet", "address", "balance", "cost", "pnl",
    "size", "contracts", "order", "fill", "key", "private",
)


def test_payload_carries_no_account_data():
    market = {
        "question": "Will the Fed cut in September?",
        "probability": 61.0,
        "_raw": {"description": "Resolves YES if..."},
        "myPosition": {"contracts": 500, "costBasisCents": 44.0},
        "walletAddress": "0xdeadbeef",
        "balanceUsd": 1234.56,
    }
    payload = ai.build_payload(market)
    blob = json.dumps(payload).lower()
    for token in _ACCOUNT_LEAKS:
        assert token not in blob, f"payload leaked {token!r}"
    assert payload["question"] == "Will the Fed cut in September?"


def test_payload_reads_rules_from_the_raw_gamma_row():
    payload = ai.build_payload({
        "question": "Q?", "_raw": {"description": "The real rules."},
    })
    assert payload["rulesText"] == "The real rules."


def test_payload_states_truncation_rather_than_hiding_it():
    payload = ai.build_payload({"question": "Q?", "_raw": {"description": "x" * 9000}})
    assert "TRUNCATED" in payload["rulesText"]


def test_prompt_names_missing_fields_instead_of_blanking_them():
    text = ai._render_prompt(ai.build_payload({"question": "Q?"}), "2026-08-28T00:00:00Z")
    assert "(not available)" in text
    assert "(none published" in text


def _ok_payload(**over):
    base = {
        "summary": "It turns on the September FOMC meeting.",
        "resolutionNotes": ["A 50bps cut does not settle this leg YES."],
        "drivers": ["August CPI print on September 11."],
        "unknowns": ["Current CME FedWatch pricing."],
        "reasoning": ["Three of the last four cuts followed a soft print."],
        "lean": "cheap",
        "confidence": "medium",
    }
    base.update(over)
    return base


def test_shape_passes_a_good_response_through():
    out = ai._shape(_ok_payload(), "anthropic", "claude-opus-5", "T", False, [], 900.0)
    assert out["ok"] is True
    assert out["lean"] == "cheap"
    assert out["redactions"] == 0
    assert out["reason"] is None


def test_a_lean_with_no_surviving_argument_becomes_no_view():
    out = ai._shape(
        _ok_payload(reasoning=["I'd put it at 70%."]),
        "anthropic", "m", "T", False, [], 1.0)
    assert out["redactions"] == 1
    assert out["lean"] == "no_view"
    assert out["confidence"] == "low"


def test_an_unknown_lean_falls_back_to_no_view():
    out = ai._shape(_ok_payload(lean="definitely_buy"), "openai", "m", "T", False, [], 1.0)
    assert out["lean"] == "no_view"


def test_failure_and_success_share_one_shape():
    ok = ai._shape(_ok_payload(), "anthropic", "m", "T", False, [], 1.0)
    bad = ai._fail("anthropic", "m", "T", "No key stored.", "Add one.")
    assert set(ok) == set(bad)
    assert bad["ok"] is False
    assert bad["reason"] and bad["fix"]


def test_both_providers_are_declared_hosts():
    for p in ai.PROVIDERS:
        assert p in terminal._HOSTS
        assert p in terminal._gates
        assert terminal._HOSTS[p]["host"]
        assert terminal._HOSTS[p]["purpose"]


def test_providers_appear_in_the_privacy_panel():
    hosts = {row["host"] for row in terminal.health()["providers"]}
    assert {"api.anthropic.com", "api.openai.com"} <= hosts


def test_offline_mode_refuses_before_any_call():
    terminal.set_offline(True)
    try:
        with pytest.raises(ai._Refused) as e:
            _run(ai._gate_call("anthropic").__aenter__())
        assert "Offline mode" in e.value.reason
    finally:
        terminal.set_offline(False)


def test_a_parked_host_fails_fast():
    gate = terminal._gates["anthropic"]
    gate.park(30.0)
    try:
        with pytest.raises(ai._Refused) as e:
            _run(ai._gate_call("anthropic").__aenter__())
        assert "rate limit" in e.value.reason
    finally:
        gate.parked_until = 0.0


def test_a_cancelled_wait_does_not_strand_the_host_lock():
    gate = _fresh("anthropic")
    old_gap, old_last = gate.min_gap, gate.last

    async def scenario():
        gate.min_gap = 5.0
        gate.last = time.monotonic()
        task = asyncio.ensure_future(ai._gate_call("anthropic").__aenter__())
        for _ in range(3):
            await asyncio.sleep(0)
        assert gate.lock().locked(), "the test never reached the min-gap wait"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.lock().locked(), "a cancelled wait stranded the host lock"

        gate.min_gap = 0.0
        async with ai._gate_call("anthropic"):
            pass
        assert not gate.lock().locked()

    try:
        _run(scenario())
    finally:
        gate.min_gap, gate.last = old_gap, old_last
        gate._lock = None


def _fake_anthropic(monkeypatch, closed: list, *, fail: bool = False):
    class _Block:
        type = "text"
        text = json.dumps(_ok_payload())

    class _Resp:
        stop_reason = None
        model = "claude-test"
        content = [_Block()]

    class _Messages:
        async def create(self, **kwargs):
            if fail:
                raise RuntimeError("provider exploded")
            return _Resp()

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

        async def close(self):
            closed.append(True)

    mod = types.ModuleType("anthropic")
    mod.AsyncAnthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setattr(ai.ai_secrets, "load_key", lambda p: "sk-test", raising=False)


@pytest.mark.parametrize("fail", [False, True])
def test_the_sdk_client_is_closed_on_both_paths(monkeypatch, fail):
    closed: list = []
    _fake_anthropic(monkeypatch, closed, fail=fail)
    _fresh("anthropic")
    try:
        if fail:
            with pytest.raises(RuntimeError):
                _run(ai._call_anthropic("prompt", "claude-test", False))
        else:
            got = _run(ai._call_anthropic("prompt", "claude-test", False))
            assert json.loads(got["raw"])["lean"] == "cheap"
        assert closed == [True], "the SDK client was never closed"
    finally:
        terminal._gates["anthropic"]._lock = None


@pytest.mark.parametrize("raw", ["null", '"just a sentence"', "[]", "42", "{oops"])
def test_a_reply_that_is_not_an_object_fails_honestly(monkeypatch, raw):
    async def fake_call(prompt, model, web_search):
        return {"raw": raw, "model": "claude-test", "sources": []}

    monkeypatch.setattr(ai, "_call_anthropic", fake_call)
    out = _run(ai.analyze(
        {"question": "Will the Fed cut in September?"},
        {"ai_enabled": True, "ai_provider": "anthropic"}))
    assert out["ok"] is False
    assert "not valid JSON" in out["reason"]
    assert out["fix"]
    assert set(out) == set(ai._fail("anthropic", "m", "T", "r", None))


def test_rate_limit_detection_needs_no_sdk_import():
    class RateLimitError(Exception):
        pass

    class Other(Exception):
        status_code = 429

    assert ai._is_rate_limit(RateLimitError())
    assert ai._is_rate_limit(Other())
    assert not ai._is_rate_limit(ValueError("nope"))


def test_ships_off_and_keyless():
    d = config.DEFAULT_CONFIG
    assert d["ai_enabled"] is False
    assert d["ai_web_search"] is False
    assert d["ai_openai_model"] == ""


def test_unknown_provider_falls_back():
    cfg = config._validate_config({**config.DEFAULT_CONFIG, "ai_provider": "../../etc"})
    assert cfg["ai_provider"] == "anthropic"


def test_model_id_is_bounded():
    cfg = config._validate_config({**config.DEFAULT_CONFIG, "ai_anthropic_model": "x" * 500})
    assert len(cfg["ai_anthropic_model"]) <= 120


def test_analysis_is_refused_while_disabled():
    out = _run(ai.analyze({"question": "Q?"}, {"ai_enabled": False}))
    assert out["ok"] is False
