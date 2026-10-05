from __future__ import annotations

import ast

import pytest

import script_audit as sa
import script_sandbox


def _under_the_cap(src: str) -> str:
    assert len(src.encode()) < script_sandbox.MAX_CODE_BYTES, "not a fair test"
    return src


def _assert_refused(r: dict) -> None:
    assert r["ok"] is False, "unreadable source was not refused"
    assert r["parsed"] is False
    assert r["critical"] == 1, "a refusal with no critical finding reads as clean"
    assert "not audited" in r["summary"]
    assert r["findings"] and r["findings"][0]["severity"] == "critical"
    assert "refused" in r["findings"][0]["message"]


def test_a_deeply_nested_expression_is_refused_not_raised():
    _assert_refused(sa.audit(_under_the_cap("(" * 1500 + "1" + ")" * 1500)))


def test_a_nul_byte_is_refused_not_raised():
    _assert_refused(sa.audit("x = 1\x00\ndef decide(ctx):\n    return None\n"))


def test_a_tree_too_deep_to_WALK_is_refused_too():
    _assert_refused(sa.audit(_under_the_cap("x = 1" + "+1" * 20000 + "\n")))


@pytest.mark.parametrize("exc", [
    RecursionError("maximum recursion depth exceeded"),
    ValueError("source code string cannot contain null bytes"),
    MemoryError(),
])
def test_every_parser_failure_mode_is_a_refusal(monkeypatch, exc):
    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(ast, "parse", boom)
    _assert_refused(sa.audit("def decide(ctx):\n    return None\n"))


def test_a_syntax_error_still_names_the_line():
    r = sa.audit("def decide(ctx)\n    return None\n")
    _assert_refused(r)
    assert "line 1" in r["summary"]
    assert r["findings"][0]["line"] == 1


def test_readable_source_is_still_audited_normally():
    clean = sa.audit("def decide(ctx):\n    return None\n")
    assert clean["ok"] is True and clean["parsed"] is True
    assert "not proof it is safe" in clean["summary"]

    dirty = sa.audit(
        "import requests\n"
        "def decide(ctx):\n    requests.post('http://x.example')\n    return None\n")
    assert dirty["ok"] is False and dirty["parsed"] is True
    assert "network" in dirty["categories"]
