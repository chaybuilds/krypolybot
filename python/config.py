"""Default Krypt PolyBot configuration.

This dict is the source of truth for what the Python backend understands.
The Electron renderer's `TraderConfig` mirrors these keys (camelCase on
the JS side, snake_case here). Conversion is handled by `_camel_keys` in
the IPC layer.

Every knob the user sees in the UI is here. Add a new one? Add it here
first, mirror it in `shared/types.ts`, then surface it in
`src/pages/SettingsPage.tsx`.
"""
from __future__ import annotations
from typing import Any

import re as _re

import rules as rules_engine

# pylint: disable=invalid-name

CRYPTO15M_ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB", "ZEC"]

C15_RULE_FIELDS = (
    "favoritePrice", "entryCost", "upProb", "bookImbalance",
    "arbEdgeCents", "deltaPct", "minsLeft",
    "hourUtc", "peersAgree", "marketBias",
    "macd", "macdSignal", "macdHist", "macdCross", "rsi",
    "deltaSignedPct", "upAsk", "downAsk", "sigma1m", "modelProb", "edgeNetCents",
    "vwap1h", "ema12", "sma20", "sma50", "priceVsVwapPct",
    "ema12VsSma20Pct", "ema1VsSma5Pct", "velocity1mPct",
    "change5mPct", "change15mPct",
)
C15_RULE_OPS = (">=", "<=", ">", "<")

BOT_RULE_FIELDS = ("confidence", "edge", "costCents")

DEFAULT_CONFIG: dict[str, Any] = {
    "network": "mainnet",
    "enable_trading": False,

    "trade_whales": True,
    "trade_momentum": True,
    "trade_convergence": False,

    "min_edge_pts_whale": 5.0,
    "min_edge_pts_momentum": 5.0,
    "min_confidence_whale": 55.0,
    "min_confidence_momentum": 55.0,
    "min_entry_price_cents": 15,
    "max_entry_price_cents": 85,
    "max_resolution_days": 0,
    "allowed_momentum_signal_types": ["trade_cluster"],
    "allowed_categories": None,
    "allowed_whale_categories": None,
    "allowed_momentum_categories": None,
    "contrarian_only": True,

    "use_rules": False,
    "rules": [],

    "sizing_mode": "percent",
    "base_size_fraction": 0.03,
    "min_size_fraction": 0.02,
    "max_size_fraction": 0.06,
    "min_contracts": 5,
    "max_contracts": 20,
    "sizing_base_edge": 5.0,
    "sizing_max_edge": 20.0,
    "hard_max_position_usd": 50.0,
    "min_cash_reserve_fraction": 0.05,

    "order_style": "limit_cross",
    "cross_spread_fallback_offset": 2,
    "order_expiration_sec": 300,

    "max_open_positions": 25,
    "max_positions_per_event": 1,
    "max_daily_new_positions": 40,
    "unlimited_daily_new_positions": False,
    "max_total_exposure_fraction": 0.75,

    "trade_scan_interval": 20,
    "position_poll_interval": 30,
    "balance_poll_interval": 60,
    "resolution_check_interval": 300,
    "whale_scan_interval": 120,
    "whale_activity_ws": True,
    "momentum_scan_interval": 90,
    "market_refresh_interval": 300,
    "db_cleanup_interval": 3600,

    "max_signal_age_sec": 120,

    "start_bankroll_usd": 0.0,
    "stop_loss_on_day": -50.0,
    "take_profit_on_day": 0.0,
    "take_profit_pct": 0.0,
    "flatten_on_daily_stop": False,
    "lifetime_loss_limit_pct": 0.5,
    "lifetime_loss_limit_usd": 0.0,

    "trading_hours_enabled": False,
    "trading_hours_start": "00:00",
    "trading_hours_end": "23:59",
    "trading_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "trading_timezone_offset_min": 0,

    "min_whale_usd": 2500.0,
    "min_whale_confidence": 30.0,
    "min_whale_edge": 2.0,
    "min_momentum_confidence": 0.0,
    "min_momentum_edge": 5.0,
    "min_entry_price_frac": 0.50,

    "crypto15m_interval": "15m",
    "crypto15m_assets": None,
    "crypto15m_arb_detect": True,
    "crypto15m_arb_min_edge_cents": 1.0,
    "crypto15m_imbalance_detect": True,
    "crypto15m_imbalance_levels": 3,
    "crypto15m_imbalance_gate": False,
    "crypto15m_imbalance_gate_min": 0.2,
    "crypto15m_indicator_detect": True,
    "crypto15m_min_rsi": 0.0,
    "crypto15m_min_macd_hist": 0.0,
    "crypto15m_ws_book": True,
    "crypto15m_use_rules": False,
    "crypto15m_rules": [],
    "crypto15m_rules_no": [],
    "crypto15m_time_delay_min": 8.0,
    "crypto15m_entry_threshold": 0.95,
    "crypto15m_entry_max": 0.98,
    "crypto15m_exit_threshold": 0.40,
    "crypto15m_take_profit": 0.0,
    "crypto15m_stop_loss_pct": 0.0,
    "crypto15m_take_profit_pct": 0.0,
    "crypto15m_min_delta_pct": 0.0,
    "crypto15m_entry_diff": 0.02,
    "crypto15m_entry_style": "taker",
    "crypto15m_taker_fak": True,
    "crypto15m_maker_cancel_min": 1.0,
    "crypto15m_maker_escalate": True,
    "crypto15m_maker_fill_sec": 4.0,
    "crypto15m_hours_start_utc": 0,
    "crypto15m_hours_end_utc": 24,
    "crypto15m_enabled": False,
    "crypto15m_sizing_mode": "fixed",
    "crypto15m_order_size": 5,
    "crypto15m_balance_pct": 0.02,
    "crypto15m_max_loss_pct": 0.0,
    "crypto15m_streak_sizing": False,
    "crypto15m_streak_loss_pct": 20.0,
    "crypto15m_streak_win_pct": 0.0,
    "crypto15m_streak_max_mult": 4.0,
    "crypto15m_max_concurrent": 7,
    "crypto15m_daily_loss_limit": -50.0,
    "crypto15m_lifetime_loss_limit_pct": 0.5,
    "crypto15m_lifetime_loss_limit_usd": 0.0,
    "crypto15m_take_profit_total": 0.0,
    "crypto15m_poll_sec": 4,
    "crypto15m_direction_mode": "favorite",
    "crypto15m_model_min_prob": 0.97,
    "crypto15m_model_min_edge_cents": 2.0,
    "crypto15m_model_final_minute": True,
    "crypto15m_model_autopause": True,
    "crypto15m_model_max_book_gap_cents": 25.0,
    "crypto15m_model_fm_max_book_gap_cents": 75.0,
    "crypto15m_autosize_to_min_notional": False,
    "crypto15m_spot_ws": True,
    "crypto15m_rtds_ws": True,
    "crypto15m_paired_mode": False,
    "crypto15m_paired_max_combined_cents": 100.0,
    "crypto15m_paired_tilt1_cents": 3.0,
    "crypto15m_paired_tilt2_cents": 6.0,
    "crypto15m_paired_tilt3_cents": 10.0,
    "crypto15m_sell_into_strength": False,
    "crypto15m_sell_strength_cents": 80.0,
    "crypto15m_record_signals": True,
    "main_record_signals": True,
    "taker_rebate_pct": 0.0,

    "perps_enabled": False,
    "perps_select_mode": "rewards",
    "perps_markets": [],
    "perps_market_count": 4,
    "perps_categories": ["crypto"],
    "perps_quote_bps": 3.0,
    "perps_levels": 1,
    "perps_level_step_bps": 3.0,
    "perps_size_usd": 100.0,
    "perps_max_inventory_usd": 300.0,
    "perps_skew_bps": 4.0,
    "perps_requote_bps": 1.5,
    "perps_vol_pull_bps": 20.0,
    "perps_vol_window_sec": 10,
    "perps_pull_cooldown_sec": 20,
    "perps_max_index_dev_bps": 40.0,
    "perps_leverage": 3,
    "perps_cross_margin": False,
    "perps_daily_loss_limit": -50.0,
    "perps_lifetime_loss_limit_usd": 0.0,
    "perps_markout_kill_bps": 15.0,
    "perps_markout_min_fills": 20,
    "perps_daily_volume_target_usd": 0.0,
    "perps_poll_sec": 2,
    "perps_realtime": True,
    "perps_book_refresh_sec": 10,

    "copy_enabled": False,
    "copy_wallets": [],
    "copy_sizing_mode": "fixed",
    "copy_fixed_usd": 10.0,
    "copy_balance_pct": 0.02,
    "copy_min_trade_usd": 25.0,
    "copy_max_concurrent": 10,
    "copy_entry_max_cents": 95,
    "copy_entry_min_cents": 0,
    "copy_daily_loss_limit": -50.0,
    "copy_lifetime_loss_limit_pct": 0.5,
    "copy_lifetime_loss_limit_usd": 0.0,
    "copy_poll_sec": 10,
    "copy_fast_poll_sec": 5,
    "copy_activity_ws": True,
    "copy_mirror_reductions": True,
    "copy_reduce_threshold": 0.25,
    "copy_only_new_entries": True,
    "copy_allow_reentries": False,

    "scripts_live_enabled": False,
    "script_poll_sec": 5,
    "script_hook_timeout_sec": 1.0,
    "script_max_entry_cents": 97,
    "script_max_contracts": 20,
    "script_max_open": 2,
    "script_daily_loss_usd": 25.0,
    "script_max_enabled": 10,
    "terminal_offline_mode": False,
    "terminal_notifications": True,
    "ai_enabled": False,
    "ai_provider": "anthropic",
    "ai_model": "claude-opus-5",
    "ai_web_search": False,
    "autopilot_enabled": False,
    "autopilot_interval_min": 60,
    "autopilot_max_runs_per_day": 12,
    "autopilot_daily_token_budget": 1_500_000,
    "autopilot_max_steps": 15,
    "autopilot_mission": "",
    "autopilot_agent_id": "default",
    "remote_enabled": False,
    "remote_discord_enabled": False,
    "remote_telegram_enabled": False,
    "remote_trading_enabled": False,
    "remote_max_order_usd": 100.0,
    "account_mode": "paper",
    "terminal_paper_mode": True,
    "terminal_paper_bankroll_usd": 1000.0,
    "mcp_enabled": False,
    "mcp_port": 47615,
    "mcp_trade_mode": "paper",
    "mcp_max_order_usd": 25.0,
    "mcp_daily_spend_usd": 100.0,
    "mcp_max_positions": 10,
    "mcp_min_edge_cents": 3.0,
    "mcp_daily_loss_usd": 50.0,
    "mcp_live_approval": True,
    "mcp_agents": None,
    "mcp_allow_live_switches": False,
    "mcp_scripts_enabled": False,
    "mcp_paper_bankroll_usd": 1000.0,
    "mcp_http_enabled": False,
    "script_market_limit": 150,
    "script_market_min_volume": 5000.0,
    "script_market_max_spread_cents": 2.0,

    "stats_chart_window_hours": 168,
}


STRATEGY_PRESETS: list[dict[str, Any]] = [
    {
        "id": "krypt-edge",
        "name": "Edge Stack",
        "tagline": "Two sources at once — sports whales + sports momentum.",
        "description": (
            "Runs both signal sources together, both restricted to SPORTS: "
            "whale-following (the crypto-whale leg was dropped after it "
            "reversed to −3.9¢/contract on a bigger sample) and contrarian "
            "trade-cluster momentum (confidence ≥ 40, since momentum scores "
            "run low), with an 85¢ entry cap to skip near-decided favorites. "
            "Example configuration, not an edge: on the full recorded corpus "
            "(2026-09-01) whale-follow measured −2.42¢/contract and momentum "
            "−1.78¢ before spread. Start with a small balance you can afford "
            "to lose."
        ),
        "riskLabel": "experimental",
        "measured": "Measured 2026-09-01 on the full recorded corpus: whale-follow −2.42¢/contract (n=8,360), momentum −1.78¢ (n=1,391), before spread.",
        "badge": "",
        "config": {
            "trade_whales": True,
            "trade_momentum": True,
            "contrarian_only": True,
            "allowed_categories": None,
            "allowed_whale_categories": ["sports"],
            "allowed_momentum_categories": ["sports"],
            "allowed_momentum_signal_types": ["trade_cluster"],
            "min_confidence_whale": 55.0,
            "min_edge_pts_whale": 5.0,
            "min_confidence_momentum": 40.0,
            "min_entry_price_cents": 15,
            "max_entry_price_cents": 85,
        },
    },
    {
        "id": "krypt-crypto-whale",
        "name": "Crypto Whale",
        "tagline": "Whale-following, crypto markets only.",
        "description": (
            "Follows $2.5k+ taker orders in CRYPTO markets only, momentum "
            "off, with the entry cap raised to 98¢ to allow the high-price "
            "favorites crypto whales tend to back. Did not hold up: a June "
            "sample (+9.3¢/contract, n=36) reversed to −3.9¢ on new data "
            "(n=143), and on the full recorded corpus (2026-09-01) "
            "whale-follow measures −2.42¢/contract before spread. Kept as an "
            "example configuration so existing users can see the update — "
            "not recommended."
        ),
        "riskLabel": "experimental",
        "measured": "Measured: crypto whale-follow −3.9¢/contract (n=143); all whale-follow −2.42¢ (n=8,360, 2026-09-01), before spread.",
        "badge": "",
        "config": {
            "trade_whales": True,
            "trade_momentum": False,
            "allowed_categories": ["crypto"],
            "min_confidence_whale": 55.0,
            "min_edge_pts_whale": 5.0,
            "min_entry_price_cents": 15,
            "max_entry_price_cents": 98,
        },
    },
    {
        "id": "krypt-aggressive",
        "name": "Krypt Aggressive",
        "tagline": "More signals, larger sizing, higher variance.",
        "description": (
            "Loosens edge gates to 3pts and confidence to 50%. Sizing scales "
            "4-10% of bankroll, $100 cap, higher max-open count. High "
            "variance — use only with a bankroll you can stand to drop 30% "
            "on a bad day. Example configuration, not an edge: it loosens "
            "the same gates that measure negative on the full recorded "
            "corpus (2026-09-01)."
        ),
        "riskLabel": "aggressive",
        "measured": "Measured 2026-09-01: it loosens gates that lose −2.42¢ (whale) and −1.78¢ (momentum) per contract before spread, at larger size.",
        "badge": "",
        "config": {
            "min_edge_pts_whale": 3.0,
            "min_edge_pts_momentum": 3.0,
            "min_confidence_whale": 50.0,
            "min_confidence_momentum": 50.0,
            "base_size_fraction": 0.06,
            "min_size_fraction": 0.04,
            "max_size_fraction": 0.1,
            "hard_max_position_usd": 100.0,
            "max_open_positions": 40,
            "max_daily_new_positions": 80,
            "max_total_exposure_fraction": 0.85,
            "stop_loss_on_day": -100.0,
        },
    },
]


CRYPTO15M_PRESETS: list[dict[str, Any]] = [
    {
        "id": "c15-favorite",
        "name": "Deep Favorite",
        "tagline": "Only the deepest favorites: buy at 95-98c.",
        "description": (
            "Buy the favorite only when it is already >=95c. The June "
            "replay of 531 markets showed +2.8c/contract on 16 wins; the "
            "full corpus (2026-09-01, n=10,373) measures favorite-follow "
            "at -2.06c/contract and the 95-100c band at +0.10c (t=0.3, "
            "unclustered) — statistically zero, and negative once the "
            "half-spread is added. One loss at 97c wipes out ~35 wins. Example "
            "configuration, not an edge. Paper-trade first."
        ),
        "config": {
            "crypto15m_direction_mode": "favorite",
            "crypto15m_entry_threshold": 0.95,
            "crypto15m_entry_max": 0.98,
            "crypto15m_exit_threshold": 0.40,
            "crypto15m_entry_style": "taker",
        },
    },
    {
        "id": "c15-contrarian",
        "name": "Contrarian Fade",
        "tagline": "Fade extreme favorites — buy the cheap underdog.",
        "description": (
            "When a side is an extreme favorite (>=90c), buy the CHEAP "
            "opposite side, betting the 15-minute move reverts before "
            "close. Low win rate, high payoff (longshot); holds to "
            "settlement, no stop. An early, small sample read break-even "
            "(+1.0c/contract, n=64); the full corpus (2026-09-01, n=10,373) "
            "measures -3.80c/contract (t=-9.7), the worst number in the "
            "corpus. Not recommended. Paper-trade hard."
        ),
        "config": {
            "crypto15m_direction_mode": "contrarian",
            "crypto15m_entry_threshold": 0.90,
            "crypto15m_entry_max": 0.98,
            "crypto15m_exit_threshold": 0.0,
            "crypto15m_entry_style": "taker",
        },
    },
]


def crypto15m_preset_config(preset_id: str) -> dict[str, Any] | None:
    for p in CRYPTO15M_PRESETS:
        if p["id"] == preset_id:
            return dict(p["config"])
    return None


def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch.lower() if ch.isupper() else ch)
    return "".join(out)


def _as_float(v: Any, default: float) -> float:
    """Best-effort float, falling back to `default` on junk/NaN."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def _clampf(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:
        return default
    return max(lo, min(hi, f))


def _clampi(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


_FRACTION_KEYS = [
    "base_size_fraction", "min_size_fraction", "max_size_fraction",
    "min_cash_reserve_fraction", "max_total_exposure_fraction",
]
_UNIT_KEYS = [
    "crypto15m_entry_threshold", "crypto15m_entry_max", "crypto15m_exit_threshold",
    "crypto15m_take_profit", "crypto15m_stop_loss_pct", "crypto15m_take_profit_pct",
    "crypto15m_min_delta_pct", "crypto15m_entry_diff", "min_entry_price_frac",
    "take_profit_pct",
]


def _validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Clamp money-critical knobs to sane ranges BEFORE they can drive an
    order. The renderer is trusted-ish, but a malformed value (e.g. a
    size fraction of 5.0) must never reach the sizing path uncorrected."""
    d = DEFAULT_CONFIG

    if "dry_run" in cfg:
        if bool(cfg.get("dry_run")):
            cfg["enable_trading"] = False
        cfg.pop("dry_run", None)
    if "crypto15m_live" in cfg:
        if cfg.get("crypto15m_enabled") and not cfg.get("crypto15m_live"):
            cfg["crypto15m_enabled"] = False
        cfg.pop("crypto15m_live", None)
    if "copy_live" in cfg:
        if cfg.get("copy_enabled") and not cfg.get("copy_live"):
            cfg["copy_enabled"] = False
        cfg.pop("copy_live", None)

    for k in _FRACTION_KEYS:
        cfg[k] = _clampf(cfg.get(k), 0.0, 1.0, d[k])
    for k in _UNIT_KEYS:
        cfg[k] = _clampf(cfg.get(k), 0.0, 1.0, d[k])
    if cfg["min_size_fraction"] > cfg["max_size_fraction"]:
        cfg["min_size_fraction"] = cfg["max_size_fraction"]
    if cfg.get("sizing_mode") not in ("percent", "contracts"):
        cfg["sizing_mode"] = d["sizing_mode"]
    cfg["min_contracts"] = _clampi(cfg.get("min_contracts"), 1, 1_000_000, d["min_contracts"])
    cfg["max_contracts"] = _clampi(cfg.get("max_contracts"), 1, 1_000_000, d["max_contracts"])
    cfg["max_contracts"] = max(cfg["min_contracts"], cfg["max_contracts"])

    cfg["hard_max_position_usd"] = _clampf(cfg.get("hard_max_position_usd"), 0.0, 1e9, d["hard_max_position_usd"])
    cfg["min_entry_price_cents"] = _clampi(cfg.get("min_entry_price_cents"), 1, 99, d["min_entry_price_cents"])
    cfg["max_entry_price_cents"] = _clampi(cfg.get("max_entry_price_cents"), 1, 99, d["max_entry_price_cents"])
    if cfg["min_entry_price_cents"] > cfg["max_entry_price_cents"]:
        cfg["min_entry_price_cents"], cfg["max_entry_price_cents"] = (
            cfg["max_entry_price_cents"], cfg["min_entry_price_cents"],
        )
    cfg["use_rules"] = bool(cfg.get("use_rules", False))
    cfg["rules"] = rules_engine.sanitize_rules(cfg.get("rules"), BOT_RULE_FIELDS)
    cfg["max_open_positions"] = _clampi(cfg.get("max_open_positions"), 0, 100_000, d["max_open_positions"])
    cfg["max_resolution_days"] = _clampi(cfg.get("max_resolution_days"), 0, 100_000, d["max_resolution_days"])
    cfg["max_daily_new_positions"] = _clampi(cfg.get("max_daily_new_positions"), 0, 100_000, d["max_daily_new_positions"])
    cfg["max_positions_per_event"] = _clampi(cfg.get("max_positions_per_event"), 1, 100_000, d["max_positions_per_event"])
    cfg["stop_loss_on_day"] = _clampf(
        -abs(_as_float(cfg.get("stop_loss_on_day"), d["stop_loss_on_day"])),
        -1e9, 0.0, d["stop_loss_on_day"],
    )
    cfg["take_profit_on_day"] = _clampf(cfg.get("take_profit_on_day"), 0.0, 1e9, d["take_profit_on_day"])
    cfg["flatten_on_daily_stop"] = bool(cfg.get("flatten_on_daily_stop", d["flatten_on_daily_stop"]))

    for _k, _lo, _hi in [
        ("crypto15m_poll_sec", 2, 3600),
        ("trade_scan_interval", 5, 86_400),
        ("position_poll_interval", 5, 86_400),
        ("balance_poll_interval", 5, 86_400),
        ("max_signal_age_sec", 10, 86_400),
        ("order_expiration_sec", 10, 86_400),
        ("resolution_check_interval", 10, 86_400),
        ("whale_scan_interval", 10, 86_400),
        ("momentum_scan_interval", 10, 86_400),
        ("market_refresh_interval", 30, 86_400),
        ("db_cleanup_interval", 60, 604_800),
    ]:
        cfg[_k] = _clampi(cfg.get(_k), _lo, _hi, d[_k])

    cfg["crypto15m_enabled"] = bool(cfg.get("crypto15m_enabled", False))
    cfg["crypto15m_order_size"] = _clampi(cfg.get("crypto15m_order_size"), 1, 10_000, d["crypto15m_order_size"])
    cfg["crypto15m_max_concurrent"] = _clampi(cfg.get("crypto15m_max_concurrent"), 1, 50, d["crypto15m_max_concurrent"])
    cfg["crypto15m_daily_loss_limit"] = _clampf(
        -abs(_as_float(cfg.get("crypto15m_daily_loss_limit"), d["crypto15m_daily_loss_limit"])),
        -1e9, 0.0, d["crypto15m_daily_loss_limit"],
    )
    cfg["crypto15m_lifetime_loss_limit_pct"] = _clampf(
        cfg.get("crypto15m_lifetime_loss_limit_pct"), 0.0, 1.0,
        d["crypto15m_lifetime_loss_limit_pct"],
    )
    cfg["crypto15m_lifetime_loss_limit_usd"] = _clampf(
        cfg.get("crypto15m_lifetime_loss_limit_usd"), 0.0, 1e9,
        d["crypto15m_lifetime_loss_limit_usd"],
    )
    cfg["lifetime_loss_limit_pct"] = _clampf(
        cfg.get("lifetime_loss_limit_pct"), 0.0, 1.0, d["lifetime_loss_limit_pct"],
    )
    cfg["lifetime_loss_limit_usd"] = _clampf(
        cfg.get("lifetime_loss_limit_usd"), 0.0, 1e9, d["lifetime_loss_limit_usd"],
    )
    cfg["crypto15m_take_profit_total"] = _clampf(
        cfg.get("crypto15m_take_profit_total"), 0.0, 1e9, d["crypto15m_take_profit_total"],
    )
    if cfg.get("crypto15m_sizing_mode") not in ("fixed", "balance_pct"):
        cfg["crypto15m_sizing_mode"] = d["crypto15m_sizing_mode"]
    cfg["crypto15m_balance_pct"] = _clampf(cfg.get("crypto15m_balance_pct"), 0.0, 1.0, d["crypto15m_balance_pct"])
    cfg["crypto15m_max_loss_pct"] = _clampf(cfg.get("crypto15m_max_loss_pct"), 0.0, 1.0, d["crypto15m_max_loss_pct"])
    cfg["crypto15m_streak_sizing"] = bool(
        cfg.get("crypto15m_streak_sizing", d["crypto15m_streak_sizing"]))
    cfg["crypto15m_streak_loss_pct"] = _clampf(
        cfg.get("crypto15m_streak_loss_pct"), -90.0, 300.0, d["crypto15m_streak_loss_pct"])
    cfg["crypto15m_streak_win_pct"] = _clampf(
        cfg.get("crypto15m_streak_win_pct"), -90.0, 300.0, d["crypto15m_streak_win_pct"])
    cfg["crypto15m_streak_max_mult"] = _clampf(
        cfg.get("crypto15m_streak_max_mult"), 1.0, 100.0, d["crypto15m_streak_max_mult"])
    _iv_raw = str(cfg.get("crypto15m_interval", d["crypto15m_interval"]))
    _iv_max_min = {"5m": 5.0, "15m": 15.0, "hourly": 60.0, "4h": 240.0}.get(_iv_raw, 15.0)
    cfg["crypto15m_time_delay_min"] = _clampf(
        cfg.get("crypto15m_time_delay_min"), 0.0, _iv_max_min,
        min(float(d["crypto15m_time_delay_min"]), _iv_max_min))
    if cfg.get("crypto15m_direction_mode") not in ("favorite", "contrarian", "model"):
        cfg["crypto15m_direction_mode"] = d["crypto15m_direction_mode"]
    cfg["crypto15m_model_min_prob"] = _clampf(
        cfg.get("crypto15m_model_min_prob"), 0.5, 1.0, d["crypto15m_model_min_prob"])
    cfg["crypto15m_model_min_edge_cents"] = _clampf(
        cfg.get("crypto15m_model_min_edge_cents"), 0.0, 50.0,
        d["crypto15m_model_min_edge_cents"])
    cfg["crypto15m_model_final_minute"] = bool(
        cfg.get("crypto15m_model_final_minute", d["crypto15m_model_final_minute"]))
    cfg["crypto15m_model_max_book_gap_cents"] = _clampf(
        cfg.get("crypto15m_model_max_book_gap_cents"), 0.0, 100.0,
        d["crypto15m_model_max_book_gap_cents"])
    cfg["crypto15m_model_fm_max_book_gap_cents"] = _clampf(
        cfg.get("crypto15m_model_fm_max_book_gap_cents"), 0.0, 100.0,
        d["crypto15m_model_fm_max_book_gap_cents"])
    cfg["crypto15m_model_autopause"] = bool(
        cfg.get("crypto15m_model_autopause", d["crypto15m_model_autopause"]))
    cfg["crypto15m_spot_ws"] = bool(cfg.get("crypto15m_spot_ws", d["crypto15m_spot_ws"]))
    cfg["crypto15m_rtds_ws"] = bool(cfg.get("crypto15m_rtds_ws", d["crypto15m_rtds_ws"]))
    cfg["crypto15m_paired_mode"] = bool(
        cfg.get("crypto15m_paired_mode", d["crypto15m_paired_mode"]))
    cfg["crypto15m_paired_max_combined_cents"] = _clampf(
        cfg.get("crypto15m_paired_max_combined_cents"), 50.0, 100.0,
        d["crypto15m_paired_max_combined_cents"])
    _t1 = _clampf(cfg.get("crypto15m_paired_tilt1_cents"), 0.0, 50.0,
                  d["crypto15m_paired_tilt1_cents"])
    _t2 = _clampf(cfg.get("crypto15m_paired_tilt2_cents"), 0.0, 50.0,
                  d["crypto15m_paired_tilt2_cents"])
    _t3 = _clampf(cfg.get("crypto15m_paired_tilt3_cents"), 0.0, 50.0,
                  d["crypto15m_paired_tilt3_cents"])
    _t2 = max(_t1, _t2)
    _t3 = max(_t2, _t3)
    cfg["crypto15m_paired_tilt1_cents"] = _t1
    cfg["crypto15m_paired_tilt2_cents"] = _t2
    cfg["crypto15m_paired_tilt3_cents"] = _t3
    cfg["crypto15m_sell_into_strength"] = bool(
        cfg.get("crypto15m_sell_into_strength", d["crypto15m_sell_into_strength"]))
    cfg["crypto15m_sell_strength_cents"] = _clampf(
        cfg.get("crypto15m_sell_strength_cents"), 50.0, 99.0,
        d["crypto15m_sell_strength_cents"])
    cfg["main_record_signals"] = bool(cfg.get("main_record_signals", d["main_record_signals"]))
    cfg["ai_enabled"] = bool(cfg.get("ai_enabled", d["ai_enabled"]))
    cfg["ai_web_search"] = bool(cfg.get("ai_web_search", d["ai_web_search"]))
    import ai_providers as _aip
    _p = str(cfg.get("ai_provider") or "").strip().lower()
    cfg["ai_provider"] = _p if _p in _aip.PROVIDERS else d["ai_provider"]
    cfg["ai_model"] = _aip.clean_model(cfg["ai_provider"], cfg.get("ai_model"))
    cfg["autopilot_enabled"] = cfg.get("autopilot_enabled", False) is True
    cfg["autopilot_interval_min"] = _clampi(
        cfg.get("autopilot_interval_min"), 15, 1440, d["autopilot_interval_min"])
    cfg["autopilot_max_runs_per_day"] = _clampi(
        cfg.get("autopilot_max_runs_per_day"), 1, 96, d["autopilot_max_runs_per_day"])
    cfg["autopilot_daily_token_budget"] = _clampi(
        cfg.get("autopilot_daily_token_budget"), 50_000, 50_000_000,
        d["autopilot_daily_token_budget"])
    cfg["autopilot_max_steps"] = _clampi(
        cfg.get("autopilot_max_steps"), 3, 40, d["autopilot_max_steps"])
    cfg["autopilot_mission"] = str(cfg.get("autopilot_mission") or "")[:2000]
    import mcp_agents as _mag
    _aid = str(cfg.get("autopilot_agent_id") or "").strip().lower()
    cfg["autopilot_agent_id"] = (_aid if (_aid == _mag.DEFAULT_ID or _mag.valid_id(_aid))
                                 else _mag.DEFAULT_ID)
    if cfg.get("crypto15m_entry_style") not in ("maker", "taker"):
        cfg["crypto15m_entry_style"] = d["crypto15m_entry_style"]
    cfg["crypto15m_taker_fak"] = bool(cfg.get("crypto15m_taker_fak", True))
    if cfg.get("crypto15m_interval") not in ("5m", "15m", "hourly", "4h"):
        cfg["crypto15m_interval"] = d["crypto15m_interval"]
    aw = cfg.get("crypto15m_assets")
    if isinstance(aw, list):
        valid = {a.upper() for a in CRYPTO15M_ASSETS}
        cfg["crypto15m_assets"] = [
            a.upper() for a in aw if isinstance(a, str) and a.upper() in valid
        ]
    elif aw is not None:
        cfg["crypto15m_assets"] = None
    cfg["crypto15m_arb_detect"] = bool(cfg.get("crypto15m_arb_detect", True))
    cfg["crypto15m_arb_min_edge_cents"] = _clampf(
        cfg.get("crypto15m_arb_min_edge_cents"), 0.0, 100.0, d["crypto15m_arb_min_edge_cents"],
    )
    cfg["crypto15m_imbalance_detect"] = bool(cfg.get("crypto15m_imbalance_detect", True))
    cfg["crypto15m_imbalance_levels"] = _clampi(
        cfg.get("crypto15m_imbalance_levels"), 1, 20, d["crypto15m_imbalance_levels"],
    )
    cfg["crypto15m_imbalance_gate"] = bool(cfg.get("crypto15m_imbalance_gate", False))
    cfg["crypto15m_imbalance_gate_min"] = _clampf(
        cfg.get("crypto15m_imbalance_gate_min"), 0.0, 1.0, d["crypto15m_imbalance_gate_min"],
    )
    if cfg["crypto15m_imbalance_gate"]:
        cfg["crypto15m_imbalance_detect"] = True
    cfg["crypto15m_indicator_detect"] = bool(cfg.get("crypto15m_indicator_detect", True))
    cfg["crypto15m_min_rsi"] = _clampf(cfg.get("crypto15m_min_rsi"), 0.0, 100.0, d["crypto15m_min_rsi"])
    cfg["crypto15m_min_macd_hist"] = _clampf(cfg.get("crypto15m_min_macd_hist"), 0.0, 1e9, d["crypto15m_min_macd_hist"])
    if cfg["crypto15m_min_rsi"] > 0 or cfg["crypto15m_min_macd_hist"] > 0:
        cfg["crypto15m_indicator_detect"] = True
    cfg["crypto15m_ws_book"] = bool(cfg.get("crypto15m_ws_book", True))
    cfg["crypto15m_use_rules"] = bool(cfg.get("crypto15m_use_rules", False))
    clean_rules = rules_engine.sanitize_rules(cfg.get("crypto15m_rules"), C15_RULE_FIELDS)
    cfg["crypto15m_rules"] = clean_rules
    clean_rules_no = rules_engine.sanitize_rules(cfg.get("crypto15m_rules_no"), C15_RULE_FIELDS)
    cfg["crypto15m_rules_no"] = clean_rules_no
    if cfg["crypto15m_use_rules"]:
        rule_fields = {c["field"] for c in clean_rules} | {c["field"] for c in clean_rules_no}
        if "bookImbalance" in rule_fields:
            cfg["crypto15m_imbalance_detect"] = True
        if "arbEdgeCents" in rule_fields:
            cfg["crypto15m_arb_detect"] = True
        if rule_fields & {
            "macd", "macdSignal", "macdHist", "macdCross", "rsi", "sigma1m",
            "vwap1h", "ema12", "sma20", "sma50", "priceVsVwapPct",
            "ema12VsSma20Pct", "ema1VsSma5Pct", "velocity1mPct",
            "change5mPct", "change15mPct",
        }:
            cfg["crypto15m_indicator_detect"] = True
    cfg["crypto15m_maker_cancel_min"] = _clampf(cfg.get("crypto15m_maker_cancel_min"), 0.0, 15.0, d["crypto15m_maker_cancel_min"])
    cfg["crypto15m_maker_escalate"] = bool(cfg.get("crypto15m_maker_escalate", d["crypto15m_maker_escalate"]))
    cfg["crypto15m_maker_fill_sec"] = _clampf(cfg.get("crypto15m_maker_fill_sec"), 0.0, 900.0, d["crypto15m_maker_fill_sec"])
    cfg["crypto15m_hours_start_utc"] = _clampi(cfg.get("crypto15m_hours_start_utc"), 0, 24, d["crypto15m_hours_start_utc"])
    cfg["crypto15m_hours_end_utc"] = _clampi(cfg.get("crypto15m_hours_end_utc"), 0, 24, d["crypto15m_hours_end_utc"])

    cfg["taker_rebate_pct"] = _clampf(cfg.get("taker_rebate_pct"), 0.0, 50.0, 0.0)

    cfg["perps_enabled"] = bool(cfg.get("perps_enabled", False))
    if cfg.get("perps_select_mode") not in ("rewards", "volume", "manual"):
        cfg["perps_select_mode"] = d["perps_select_mode"]
    pm = cfg.get("perps_markets")
    cfg["perps_markets"] = list(dict.fromkeys(
        m.strip().upper() for m in pm if isinstance(m, str) and m.strip()
    ))[:50] if isinstance(pm, list) else []
    pc = cfg.get("perps_categories")
    valid_cats = ("crypto", "equity", "index", "commodity")
    cfg["perps_categories"] = (
        [c for c in valid_cats if c in pc] if isinstance(pc, list) else list(d["perps_categories"])
    )
    cfg["perps_market_count"] = _clampi(cfg.get("perps_market_count"), 1, 30, d["perps_market_count"])
    cfg["perps_quote_bps"] = _clampf(cfg.get("perps_quote_bps"), 0.0, 100.0, d["perps_quote_bps"])
    cfg["perps_levels"] = _clampi(cfg.get("perps_levels"), 1, 3, d["perps_levels"])
    cfg["perps_level_step_bps"] = _clampf(cfg.get("perps_level_step_bps"), 0.5, 50.0, d["perps_level_step_bps"])
    cfg["perps_size_usd"] = _clampf(cfg.get("perps_size_usd"), 10.0, 100_000.0, d["perps_size_usd"])
    cfg["perps_max_inventory_usd"] = _clampf(cfg.get("perps_max_inventory_usd"), 10.0, 1_000_000.0, d["perps_max_inventory_usd"])
    cfg["perps_skew_bps"] = _clampf(cfg.get("perps_skew_bps"), 0.0, 100.0, d["perps_skew_bps"])
    cfg["perps_requote_bps"] = _clampf(cfg.get("perps_requote_bps"), 0.1, 50.0, d["perps_requote_bps"])
    cfg["perps_vol_pull_bps"] = _clampf(cfg.get("perps_vol_pull_bps"), 0.0, 1000.0, d["perps_vol_pull_bps"])
    cfg["perps_vol_window_sec"] = _clampi(cfg.get("perps_vol_window_sec"), 2, 300, d["perps_vol_window_sec"])
    cfg["perps_pull_cooldown_sec"] = _clampi(cfg.get("perps_pull_cooldown_sec"), 0, 3600, d["perps_pull_cooldown_sec"])
    cfg["perps_max_index_dev_bps"] = _clampf(cfg.get("perps_max_index_dev_bps"), 0.0, 1000.0, d["perps_max_index_dev_bps"])
    cfg["perps_leverage"] = _clampi(cfg.get("perps_leverage"), 1, 50, d["perps_leverage"])
    cfg["perps_cross_margin"] = bool(cfg.get("perps_cross_margin", False))
    cfg["perps_daily_loss_limit"] = -abs(_as_float(cfg.get("perps_daily_loss_limit"), d["perps_daily_loss_limit"]))
    cfg["perps_lifetime_loss_limit_usd"] = abs(_as_float(cfg.get("perps_lifetime_loss_limit_usd"), 0.0))
    cfg["perps_markout_kill_bps"] = _clampf(cfg.get("perps_markout_kill_bps"), 0.0, 1000.0, d["perps_markout_kill_bps"])
    cfg["perps_markout_min_fills"] = _clampi(cfg.get("perps_markout_min_fills"), 5, 500, d["perps_markout_min_fills"])
    cfg["perps_daily_volume_target_usd"] = _clampf(cfg.get("perps_daily_volume_target_usd"), 0.0, 1e12, 0.0)
    cfg["perps_poll_sec"] = _clampi(cfg.get("perps_poll_sec"), 1, 30, d["perps_poll_sec"])
    cfg["perps_realtime"] = bool(cfg.get("perps_realtime", True))
    cfg["perps_book_refresh_sec"] = _clampi(cfg.get("perps_book_refresh_sec"), 2, 300, d["perps_book_refresh_sec"])

    cfg["copy_enabled"] = bool(cfg.get("copy_enabled", False))
    seen: set[str] = set()
    clean_wallets: list[str] = []
    for w in (cfg.get("copy_wallets") or []):
        if not isinstance(w, str):
            continue
        a = w.strip().lower()
        if _re.fullmatch(r"0x[0-9a-f]{40}", a) and a not in seen:
            seen.add(a)
            clean_wallets.append(a)
    cfg["copy_wallets"] = clean_wallets
    if cfg.get("copy_sizing_mode") not in ("fixed", "balance_pct"):
        cfg["copy_sizing_mode"] = d["copy_sizing_mode"]
    cfg["copy_fixed_usd"] = _clampf(cfg.get("copy_fixed_usd"), 0.0, 100_000.0, d["copy_fixed_usd"])
    cfg["copy_balance_pct"] = _clampf(cfg.get("copy_balance_pct"), 0.0, 1.0, d["copy_balance_pct"])
    cfg["copy_min_trade_usd"] = _clampf(cfg.get("copy_min_trade_usd"), 0.0, 1e9, d["copy_min_trade_usd"])
    cfg["copy_max_concurrent"] = _clampi(cfg.get("copy_max_concurrent"), 1, 200, d["copy_max_concurrent"])
    cfg["copy_entry_max_cents"] = _clampi(cfg.get("copy_entry_max_cents"), 1, 99, d["copy_entry_max_cents"])
    cfg["copy_entry_min_cents"] = _clampi(cfg.get("copy_entry_min_cents"), 0, 99, d["copy_entry_min_cents"])
    if cfg["copy_entry_min_cents"] >= cfg["copy_entry_max_cents"]:
        cfg["copy_entry_min_cents"] = max(0, cfg["copy_entry_max_cents"] - 1)
    cfg["copy_daily_loss_limit"] = _clampf(
        -abs(_as_float(cfg.get("copy_daily_loss_limit"), d["copy_daily_loss_limit"])),
        -1e9, 0.0, d["copy_daily_loss_limit"],
    )
    cfg["copy_lifetime_loss_limit_pct"] = _clampf(
        cfg.get("copy_lifetime_loss_limit_pct"), 0.0, 1.0,
        d["copy_lifetime_loss_limit_pct"],
    )
    cfg["copy_lifetime_loss_limit_usd"] = _clampf(
        cfg.get("copy_lifetime_loss_limit_usd"), 0.0, 1e9,
        d["copy_lifetime_loss_limit_usd"],
    )
    cfg["copy_poll_sec"] = _clampi(cfg.get("copy_poll_sec"), 5, 3600, d["copy_poll_sec"])
    cfg["copy_fast_poll_sec"] = _clampi(
        cfg.get("copy_fast_poll_sec"), 2, 3600, d["copy_fast_poll_sec"])
    cfg["copy_activity_ws"] = bool(cfg.get("copy_activity_ws", True))
    cfg["copy_mirror_reductions"] = bool(cfg.get("copy_mirror_reductions", True))
    cfg["copy_reduce_threshold"] = _clampf(
        cfg.get("copy_reduce_threshold"), 0.05, 0.95, d["copy_reduce_threshold"])
    cfg["copy_only_new_entries"] = bool(cfg.get("copy_only_new_entries", True))
    cfg["copy_allow_reentries"] = bool(cfg.get("copy_allow_reentries", False))
    cfg["scripts_live_enabled"] = bool(cfg.get("scripts_live_enabled", False))
    cfg["script_poll_sec"] = _clampi(cfg.get("script_poll_sec"), 2, 3600, d["script_poll_sec"])
    cfg["script_hook_timeout_sec"] = _clampf(
        cfg.get("script_hook_timeout_sec"), 0.05, 30.0,
        d["script_hook_timeout_sec"])
    cfg["script_max_entry_cents"] = _clampi(
        cfg.get("script_max_entry_cents"), 1, 99, d["script_max_entry_cents"])
    cfg["script_max_contracts"] = _clampi(
        cfg.get("script_max_contracts"), 1, 10_000, d["script_max_contracts"])
    cfg["script_max_open"] = _clampi(cfg.get("script_max_open"), 1, 100, d["script_max_open"])
    cfg["script_daily_loss_usd"] = _clampf(
        cfg.get("script_daily_loss_usd"), 0.0, 1e6, d["script_daily_loss_usd"])
    cfg["script_max_enabled"] = _clampi(
        cfg.get("script_max_enabled"), 1, 50, d["script_max_enabled"])
    cfg["terminal_offline_mode"] = bool(cfg.get("terminal_offline_mode", False))
    cfg["terminal_notifications"] = bool(cfg.get("terminal_notifications", True))
    cfg["remote_enabled"] = bool(cfg.get("remote_enabled", False))
    cfg["remote_discord_enabled"] = bool(cfg.get("remote_discord_enabled", False))
    cfg["remote_telegram_enabled"] = bool(cfg.get("remote_telegram_enabled", False))
    cfg["remote_trading_enabled"] = bool(cfg.get("remote_trading_enabled", False))
    cfg["remote_max_order_usd"] = _clampf(
        cfg.get("remote_max_order_usd"), 0.0, 10_000.0, d["remote_max_order_usd"])
    cfg["mcp_enabled"] = bool(cfg.get("mcp_enabled", False))
    cfg["mcp_port"] = _clampi(cfg.get("mcp_port"), 1024, 65535, d["mcp_port"])
    tm = str(cfg.get("mcp_trade_mode") or "").lower()
    cfg["mcp_trade_mode"] = tm if tm in ("off", "paper", "live") else d["mcp_trade_mode"]
    cfg["mcp_max_order_usd"] = _clampf(
        cfg.get("mcp_max_order_usd"), 0.0, 100_000.0, d["mcp_max_order_usd"])
    cfg["mcp_daily_spend_usd"] = _clampf(
        cfg.get("mcp_daily_spend_usd"), 0.0, 1_000_000.0, d["mcp_daily_spend_usd"])
    cfg["mcp_max_positions"] = _clampi(
        cfg.get("mcp_max_positions"), 0, 500, d["mcp_max_positions"])
    cfg["mcp_min_edge_cents"] = _clampf(
        cfg.get("mcp_min_edge_cents"), 0.0, 50.0, d["mcp_min_edge_cents"])
    cfg["mcp_daily_loss_usd"] = _clampf(
        cfg.get("mcp_daily_loss_usd"), 0.0, 1_000_000.0, d["mcp_daily_loss_usd"])
    cfg["mcp_live_approval"] = cfg.get("mcp_live_approval") is not False
    import mcp_agents
    cfg["mcp_agents"] = mcp_agents.validate_agents(
        cfg.get("mcp_agents"), cfg["mcp_trade_mode"])
    cfg["mcp_allow_live_switches"] = cfg.get("mcp_allow_live_switches") is True
    cfg["mcp_scripts_enabled"] = bool(cfg.get("mcp_scripts_enabled", False))
    cfg["mcp_paper_bankroll_usd"] = _clampf(
        cfg.get("mcp_paper_bankroll_usd"), 1.0, 1e7, d["mcp_paper_bankroll_usd"])
    cfg["mcp_http_enabled"] = cfg.get("mcp_http_enabled") is True
    cfg["account_mode"] = "live" if str(cfg.get("account_mode") or "").lower() == "live" else "paper"
    cfg["terminal_paper_mode"] = cfg["account_mode"] == "paper"
    cfg["terminal_paper_bankroll_usd"] = _clampf(
        cfg.get("terminal_paper_bankroll_usd"), 1.0, 1e7,
        d["terminal_paper_bankroll_usd"])
    cfg["script_market_limit"] = _clampi(
        cfg.get("script_market_limit"), 0, 200, d["script_market_limit"])
    cfg["script_market_min_volume"] = _clampf(
        cfg.get("script_market_min_volume"), 0.0, 1e12,
        d["script_market_min_volume"])
    cfg["script_market_max_spread_cents"] = _clampf(
        cfg.get("script_market_max_spread_cents"), 0.0, 99.0,
        d["script_market_max_spread_cents"])
    return cfg


def merge_with_defaults(user: dict[str, Any]) -> dict[str, Any]:
    """Apply user overrides on top of DEFAULT_CONFIG.

    Accepts either snake_case or camelCase keys (the Electron renderer
    sends camelCase; the Python codebase reads snake_case). Always
    returns a fully populated config dict — missing keys back-fill from
    defaults. Unknown keys are preserved (forward-compat) but ignored by
    the running code.
    """
    out = dict(DEFAULT_CONFIG)
    given: set[str] = set()
    for k, v in (user or {}).items():
        if k in out:
            out[k] = v
            given.add(k)
            continue
        sk = _camel_to_snake(k)
        out[sk] = v
        given.add(sk)
    _migrate_mcp(out, given)
    _migrate_account(out, given)
    _migrate_ai(out, given)
    return _validate_config(out)


def _migrate_ai(out: dict[str, Any], given: set[str]) -> None:
    """Configs from before the six providers had one model key per SDK
    provider (`ai_anthropic_model`, `ai_openai_model`). Carried into the one
    `ai_model` for the provider the user has selected — only when `ai_model`
    was not given, so a config that already says what it wants is never
    rewritten — and the legacy keys are dropped."""
    if "ai_model" not in given:
        prov = str(out.get("ai_provider") or "anthropic").strip().lower()
        legacy = out.get(f"ai_{prov}_model") if prov in ("anthropic", "openai") else None
        if isinstance(legacy, str) and legacy.strip():
            out["ai_model"] = legacy.strip()
        elif prov != "anthropic":
            out["ai_model"] = ""
    out.pop("ai_anthropic_model", None)
    out.pop("ai_openai_model", None)


def _migrate_account(out: dict[str, Any], given: set[str]) -> None:
    """A config from before account_mode existed. Paper if it had paper mode
    on; otherwise an EXISTING config (one that carries any setting at all)
    is live — its engines sent real orders yesterday and must not silently
    stop today — and a fresh one (nothing given) is paper.

    The renderer's settings store owns the stricter half of this rule (it
    knows whether the disclaimer was accepted) and always sends accountMode
    once it knows the key; this is what a config WITHOUT it means here.
    Never applied when account_mode was given."""
    if "account_mode" in given:
        return
    if not given:
        out["account_mode"] = "paper"
    elif "terminal_paper_mode" in given:
        out["account_mode"] = "paper" if out.get("terminal_paper_mode") is True else "live"
    else:
        out["account_mode"] = "live"


def account_live(cfg: dict[str, Any]) -> bool:
    """True when the ACCOUNT is live: real orders may leave this app at all.

    Narrowing only: a validated config says it twice (account_mode, and the
    derived terminal_paper_mode), and either one saying paper is paper. A raw
    dict that never went through merge_with_defaults and names neither key
    (tests, CLI tools) keeps the pre-account-mode meaning: live."""
    m = cfg.get("account_mode")
    if m is not None and str(m).lower() != "live":
        return False
    return not cfg.get("terminal_paper_mode")


def engine_on(cfg: dict[str, Any], switch: str) -> bool:
    """The EFFECTIVE engine switch: the user's own switch AND a live account.
    In Paper the engines send nothing, and the user's switch is left exactly
    as they set it — going live again restores what they had."""
    return bool(cfg.get(switch)) and account_live(cfg)


def _migrate_mcp(out: dict[str, Any], given: set[str]) -> None:
    """Configs from before named agents had one live switch
    (`mcp_live_trading_enabled`) and let the agent pick paper or live per
    call. Carried forward, never widened: a user who had live trading on keeps
    agents able to trade live (now behind approvals by default) and keeps the
    whole-account powers that switch used to grant; everyone else is paper.
    Only applied when the new key was not given — a config that already says
    what it wants is never rewritten."""
    legacy_live = out.get("mcp_live_trading_enabled") is True
    if "mcp_trade_mode" not in given:
        out["mcp_trade_mode"] = "live" if legacy_live else DEFAULT_CONFIG["mcp_trade_mode"]
    if "mcp_allow_live_switches" not in given and legacy_live:
        out["mcp_allow_live_switches"] = True
    if "mcp_daily_spend_usd" not in given and "mcp_daily_limit_usd" in given:
        out["mcp_daily_spend_usd"] = out.get("mcp_daily_limit_usd")
    out.pop("mcp_live_trading_enabled", None)
    out.pop("mcp_daily_limit_usd", None)


def strategy_full_config(strategy_id: str) -> dict[str, Any] | None:
    for s in STRATEGY_PRESETS:
        if s["id"] == strategy_id:
            return merge_with_defaults(s["config"])
    return None
