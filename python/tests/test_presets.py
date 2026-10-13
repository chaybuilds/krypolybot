"""Sanity tests for the strategy presets — and the guard that keeps the
backend mirror in `config.py` identical to the UI list in
`electron/system/strategies.ts`, which is the source of truth.

The two drifted once (the UI routed Edge Stack's whales to crypto/exotics/
entertainment while the backend said sports, and the UI badged a preset
"new" whose backend description said it did not hold up). This file fails
on the first divergent key."""
from __future__ import annotations

import json
import pathlib
import re

from config import (
    CRYPTO15M_PRESETS, STRATEGY_PRESETS, _camel_to_snake,
    crypto15m_preset_config, merge_with_defaults,
)

_TS = pathlib.Path(__file__).resolve().parents[2] / "electron" / "system" / "strategies.ts"


def _by_id(presets):
    return {p["id"]: p for p in presets}


def _ts_presets() -> list[dict]:
    src = _TS.read_text(encoding="utf-8")
    body = src.split("export const BUILTIN_STRATEGIES", 1)[1]
    pat = re.compile(
        r"\{\s*id: '([^']+)',\s*name: '((?:[^'\\]|\\.)*)',\s*tagline: '((?:[^'\\]|\\.)*)',"
        r"\s*description:\s*'((?:[^'\\]|\\.)*)',\s*riskLabel: '([^']+)',"
        r"(?:\s*measured: '((?:[^'\\]|\\.)*)',)?"
        r"(?:\s*badge: '([^']*)',)?\s*config: merge\(\{(.*?)\}\),", re.S)
    out = []
    for m in pat.finditer(body):
        cfg = {}
        for line in m.group(8).strip().splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith("//"):
                continue
            k, v = line.split(":", 1)
            cfg[_camel_to_snake(k.strip())] = json.loads(v.strip().replace("'", '"'))
        out.append({
            "id": m.group(1), "name": m.group(2).replace("\\'", "'"),
            "tagline": m.group(3).replace("\\'", "'"),
            "description": m.group(4).replace("\\'", "'"),
            "riskLabel": m.group(5), "measured": (m.group(6) or "").replace("\\'", "'"),
            "badge": m.group(7) or "", "config": cfg,
        })
    return out



def test_the_backend_mirror_matches_the_ui_presets_key_for_key():
    ts = _ts_presets()
    assert len(ts) >= 3, "could not parse electron/system/strategies.ts"
    assert [p["id"] for p in ts] == [p["id"] for p in STRATEGY_PRESETS]
    for t, p in zip(ts, STRATEGY_PRESETS):
        assert t["config"] == p["config"], f"{t['id']}: config drifted"
        assert t["riskLabel"] == p["riskLabel"], t["id"]
        assert t["badge"] == (p.get("badge") or ""), t["id"]
        assert t["name"] == p["name"] and t["tagline"] == p["tagline"], t["id"]
        assert t["description"] == p["description"], f"{t['id']}: description drifted"
        assert t["measured"] == (p.get("measured") or ""), f"{t['id']}: measured drifted"


def test_no_preset_carries_a_recommended_or_new_badge():
    """Every preset measured negative on the full corpus (2026-09-01). A
    badge that says otherwise is a small lie in an app built on not telling
    small lies."""
    for p in STRATEGY_PRESETS:
        assert (p.get("badge") or "") == "", p["id"]
        assert "example configuration" in p["description"].lower(), p["id"]
        assert "measured" in (p.get("measured") or "").lower(), p["id"]
        assert "−" in p["measured"], p["id"]


def test_crypto_whale_preset_is_crypto_only_whales():
    p = _by_id(STRATEGY_PRESETS)["krypt-crypto-whale"]
    cfg = merge_with_defaults(p["config"])
    assert cfg["trade_whales"] is True
    assert cfg["trade_momentum"] is False
    assert cfg["allowed_categories"] == ["crypto"]
    assert cfg["max_entry_price_cents"] == 98


def test_edge_stack_preset_runs_both_sources_in_sports():
    p = _by_id(STRATEGY_PRESETS)["krypt-edge"]
    cfg = merge_with_defaults(p["config"])
    assert cfg["trade_whales"] is True
    assert cfg["trade_momentum"] is True
    assert cfg["allowed_categories"] is None
    assert cfg["allowed_whale_categories"] == ["sports"]
    assert cfg["allowed_momentum_categories"] == ["sports"]
    assert cfg["max_entry_price_cents"] == 85


def test_aggressive_preset_only_loosens_gates_and_sizing():
    p = _by_id(STRATEGY_PRESETS)["krypt-aggressive"]
    cfg = merge_with_defaults(p["config"])
    assert cfg["min_edge_pts_whale"] == 3.0 and cfg["min_confidence_whale"] == 50.0
    assert cfg["hard_max_position_usd"] == 100.0
    assert "enable_trading" not in p["config"]
    assert cfg["enable_trading"] is False



def test_crypto15m_presets_exist():
    ids = {p["id"] for p in CRYPTO15M_PRESETS}
    assert {"c15-favorite", "c15-contrarian"} <= ids


def test_contrarian_preset_sets_contrarian_mode():
    cfg = crypto15m_preset_config("c15-contrarian")
    assert cfg is not None
    assert cfg["crypto15m_direction_mode"] == "contrarian"
    assert cfg["crypto15m_exit_threshold"] == 0.0


def test_deep_favorite_preset_uses_95c_band():
    cfg = crypto15m_preset_config("c15-favorite")
    assert cfg["crypto15m_entry_threshold"] == 0.95
    assert cfg["crypto15m_entry_max"] == 0.98


def test_crypto15m_preset_descriptions_carry_the_remeasurement():
    for p in CRYPTO15M_PRESETS:
        assert "2026-09-01" in p["description"], p["id"]


def test_unknown_preset_is_none():
    assert crypto15m_preset_config("nope") is None
