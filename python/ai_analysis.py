from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

import ai_secrets
import terminal as term
from terminal import num, _now_iso

logger = logging.getLogger("krypt.ai")

PROVIDERS = ("anthropic", "openai")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_OPENAI_MODEL = ""

_TIMEOUT_SEC = 90.0

LEANS = ("cheap", "rich", "fair", "no_view")
CONFIDENCES = ("low", "medium", "high")

MAX_BULLETS = 6
MAX_RULES_CHARS = 6000


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "Two or three sentences: what this market actually turns on, "
                "and what event decides it."
            ),
        },
        "resolutionNotes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Traps in the resolution wording — ambiguity, an edge case "
                "that settles counter-intuitively, a source that may not "
                "report in time. Empty if the wording is clean."
            ),
        },
        "drivers": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Concrete, dated events between now and close that would move "
                "this market. Name the date where there is one."
            ),
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What you do NOT know that bears on this. Be specific and be "
                "honest; this list is rendered to the user verbatim and is "
                "the most useful thing on the panel."
            ),
        },
        "reasoning": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The argument behind your lean, one step per item. No "
                "probability estimates, no fair values."
            ),
        },
        "lean": {
            "type": "string",
            "enum": list(LEANS),
            "description": (
                "Your directional read RELATIVE TO THE QUOTED PRICE. 'cheap' "
                "= the market looks too low, 'rich' = too high, 'fair' = "
                "about right, 'no_view' = you genuinely cannot tell. "
                "'no_view' is a respected answer; guessing is not."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": list(CONFIDENCES),
            "description": "How much weight your own lean deserves.",
        },
    },
    "required": [
        "summary", "resolutionNotes", "drivers",
        "unknowns", "reasoning", "lean", "confidence",
    ],
    "additionalProperties": False,
}


_SYSTEM = """\
You are reading a single prediction market on behalf of a trader who is \
looking at its price, its order book and its resolution rules on screen \
right now. Your job is reading comprehension, not forecasting.

Do these things:
  - Explain what the market actually turns on, in plain language.
  - Read the resolution criteria adversarially. Prediction markets are lost \
far more often to wording than to being wrong about the world: a market can \
settle against you on a technicality you did not read. Name those.
  - Name the concrete, dated events between now and close that would move it.
  - State honestly what you do not know. You are a language model with a \
training cutoff, you cannot see the order book beyond the single quote you \
were given, and you may be reading a market about something that happened \
after you were trained. Say so, specifically.
  - Then give a directional lean relative to the quoted price, with the \
argument that produced it.

NEVER state a numeric probability, percentage likelihood, fair value or \
edge for this market. Not in any field, not in passing, not hedged. You have \
no calibration for this question and a number from you would read as \
authoritative when it is not. Give the direction and the argument; the \
person reading decides the size. Quoting a figure that is a FACT about the \
world is fine and expected — an inflation print, a strike price, a poll \
number, a date. The prohibition is on a figure that represents YOUR estimate \
of this market's outcome.

If the rules text is thin, or the question turns on something after your \
cutoff, prefer "no_view" and say why in unknowns. An honest "I cannot tell" \
is worth more here than a confident guess, and the person reading this can \
tell the difference.\
"""


_ESTIMATE_PATTERNS = [
    r"\b(?:i\s*(?:'|’)?d|i\s+would|i)\s+(?:estimate|put|price|say|peg)\b[^.!?]*?\d",
    r"\bmy\s+(?:estimate|read|number|guess|view)\b[^.!?]*?\d",
    r"\bfair\s+(?:value|price)\b[^.!?]*?\d",
    r"\b(?:probability|likelihood|chance|odds)\b[^.!?]{0,40}?"
    r"(?:is|are|of|at|around|about|near|roughly|~)\s*~?\s*\d",
    r"\d{1,3}\s*(?:%|¢|\bcents?\b)\s*(?:likely|probable|chance|fair|value)\b",
    r"\bshould\s+be\s+(?:trading|priced|quoted)\b[^.!?]*?\d",
]
_ESTIMATE_RE = re.compile("|".join(_ESTIMATE_PATTERNS), re.IGNORECASE)

_SENTENCE_RE = re.compile(r"[^.!?]*[.!?]|[^.!?]+$")


def _strip_numeric_estimates(text: str) -> tuple[str, int]:
    if not text or not _ESTIMATE_RE.search(text):
        return text, 0
    kept: list[str] = []
    removed = 0
    for sent in _SENTENCE_RE.findall(text):
        if sent.strip() and _ESTIMATE_RE.search(sent):
            removed += 1
            continue
        kept.append(sent)
    cleaned = "".join(kept).strip()
    if not cleaned and removed:
        return "", removed
    return cleaned, removed


def _clean_list(items: Any, limit: int = MAX_BULLETS) -> tuple[list[str], int]:
    out: list[str] = []
    removed = 0
    for raw in (items or [])[:limit]:
        if not isinstance(raw, str):
            continue
        cleaned, n = _strip_numeric_estimates(raw.strip())
        removed += n
        if cleaned:
            out.append(cleaned)
    return out, removed


def build_payload(market: dict) -> dict:
    m = market or {}
    raw = m.get("_raw") or {}
    rules = str(
        m.get("resolutionCriteria") or raw.get("description") or m.get("description") or ""
    ).strip()
    if len(rules) > MAX_RULES_CHARS:
        rules = rules[:MAX_RULES_CHARS] + "\n\n[TRUNCATED — rules text continues]"
    return {
        "question": m.get("question") or m.get("title") or raw.get("question"),
        "outcomes": m.get("outcomes"),
        "priceCents": num(m.get("probability")),
        "bidCents": num(m.get("bid")),
        "askCents": num(m.get("ask")),
        "volume24hUsd": num(m.get("volume24hUsd")),
        "liquidityUsd": num(m.get("liquidityUsd")),
        "closeTime": m.get("closeTime"),
        "status": m.get("status"),
        "resolverName": m.get("resolverName") or raw.get("resolvedBy"),
        "resolutionSource": m.get("resolutionSource") or raw.get("resolutionSource"),
        "rulesText": rules or None,
        "negRisk": bool(m.get("negRisk") or raw.get("negRisk")),
    }


def _render_prompt(payload: dict, asked_at: str) -> str:
    lines = [
        f"Analysis requested at {asked_at} (UTC).",
        "",
        "MARKET",
        f"  Question: {payload.get('question') or '(not available)'}",
    ]
    outcomes = payload.get("outcomes")
    if outcomes:
        lines.append(f"  Outcomes: {', '.join(str(o) for o in outcomes)}")

    price = payload.get("priceCents")
    if price is None:
        lines.append("  Current price: (not available — no quote could be read)")
    else:
        lines.append(
            f"  Current price: {price:.1f}c "
            f"(i.e. the market is paying {price:.1f}c for a contract that "
            f"settles at 100c if this resolves YES)"
        )
    bid, ask = payload.get("bidCents"), payload.get("askCents")
    if bid is not None and ask is not None:
        lines.append(f"  Best bid / ask: {bid:.1f}c / {ask:.1f}c")

    for label, key, fmt in (
        ("24h volume", "volume24hUsd", "${:,.0f}"),
        ("Liquidity", "liquidityUsd", "${:,.0f}"),
    ):
        v = payload.get(key)
        lines.append(f"  {label}: {fmt.format(v) if v is not None else '(not available)'}")

    lines += [
        f"  Closes: {payload.get('closeTime') or '(not available)'}",
        f"  Status: {payload.get('status') or '(not available)'}",
        f"  Resolver: {payload.get('resolverName') or '(not stated)'}",
        f"  Resolution source: {payload.get('resolutionSource') or '(not stated)'}",
    ]
    if payload.get("negRisk"):
        lines.append(
            "  This market is one leg of a mutually-exclusive group: exactly "
            "one leg in the event resolves YES."
        )

    rules = payload.get("rulesText")
    lines += ["", "RESOLUTION RULES (verbatim)", rules or "  (none published — say so in unknowns)"]
    return "\n".join(lines)


class _Refused(Exception):

    def __init__(self, reason: str, fix: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


class _gate_call:

    def __init__(self, host: str) -> None:
        self.host = host
        self.gate = term._gates.get(host)
        self.started = 0.0

    async def __aenter__(self) -> "_gate_call":
        if term.is_offline():
            raise _Refused(
                term.OFFLINE_REASON,
                "Turn off offline mode in Settings → Data & privacy.",
            )
        g = self.gate
        if g is None:
            raise _Refused(f"No host gate is registered for {self.host}.")
        if g.parked():
            raise _Refused(
                f"{self.host} returned a rate limit recently, so it is parked "
                f"for a few seconds rather than retried into a harder limit.",
                "Wait a moment and ask again.",
            )
        await g.lock().acquire()
        try:
            wait = g.min_gap - (time.monotonic() - g.last)
            if wait > 0:
                await asyncio.sleep(wait)
            self.started = time.monotonic()
            g.calls += 1
        except BaseException:
            g.lock().release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        g = self.gate
        if g is not None:
            g.last = time.monotonic()
            g.note_latency((g.last - self.started) * 1000.0)
            if exc is not None:
                g.errors += 1
                if _is_rate_limit(exc):
                    g.park()
            g.lock().release()
        return False


def _is_rate_limit(exc: BaseException) -> bool:
    if type(exc).__name__ == "RateLimitError":
        return True
    return getattr(exc, "status_code", None) == 429


async def _close(client: Any) -> None:
    closer = getattr(client, "close", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as e:
        logger.debug("ai: closing client failed: %s: %s", type(e).__name__, e)


def _require_key(provider: str) -> str:
    key = ai_secrets.load_key(provider)
    if not key:
        raise _Refused(
            f"No {provider} API key is stored.",
            f"Add one in Settings → AI analysis.",
        )
    return key


async def _call_anthropic(prompt: str, model: str, web_search: bool) -> dict:
    try:
        import anthropic
    except ImportError as e:
        raise _Refused(
            "The anthropic package is not installed in this build.",
            "Reinstall the app, or `pip install anthropic` when running from source.",
        ) from e

    client = anthropic.AsyncAnthropic(
        api_key=_require_key("anthropic"),
        timeout=_TIMEOUT_SEC,
        max_retries=0,
    )
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_ANTHROPIC_MODEL,
        "max_tokens": 8000,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "high",
            "format": {"type": "json_schema", "schema": _SCHEMA},
        },
    }
    if web_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search"}]

    try:
        async with _gate_call("anthropic"):
            resp = await client.messages.create(**kwargs)
    finally:
        await _close(client)

    if getattr(resp, "stop_reason", None) == "refusal":
        detail = getattr(resp, "stop_details", None)
        raise _Refused(
            "The model declined to answer this one"
            + (f" ({detail.category})" if getattr(detail, "category", None) else "")
            + ".",
            "This is the provider's own safety filter, not the app.",
        )

    text = next(
        (b.text for b in resp.content if getattr(b, "type", None) == "text"), None
    )
    if not text:
        raise _Refused("The model returned no text to read.")
    sources = _anthropic_sources(resp)
    return {"raw": text, "model": getattr(resp, "model", model), "sources": sources}


def _anthropic_sources(resp: Any) -> list[dict]:
    out: list[dict] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        if not isinstance(content, list):
            continue
        for item in content:
            url = getattr(item, "url", None)
            if url:
                out.append({"url": url, "title": getattr(item, "title", None)})
    return out[:8]


async def _call_openai(prompt: str, model: str, web_search: bool) -> dict:
    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        raise _Refused(
            "The openai package is not installed in this build.",
            "Reinstall the app, or `pip install openai` when running from source.",
        ) from e

    if not model:
        raise _Refused(
            "No OpenAI model is selected.",
            "Pick one in Settings → AI analysis (press Load models).",
        )

    client = AsyncOpenAI(
        api_key=_require_key("openai"),
        timeout=_TIMEOUT_SEC,
        max_retries=0,
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "market_analysis",
                "strict": True,
                "schema": _SCHEMA,
            }
        },
    }
    if web_search:
        kwargs["tools"] = [{"type": "web_search"}]

    try:
        async with _gate_call("openai"):
            resp = await client.responses.create(**kwargs)
    finally:
        await _close(client)

    text = getattr(resp, "output_text", None)
    if not text:
        raise _Refused("The model returned no text to read.")
    return {"raw": text, "model": getattr(resp, "model", model), "sources": []}


async def list_models(provider: str) -> list[str]:
    if provider not in PROVIDERS:
        raise _Refused(f"Unknown provider {provider!r}.")
    key = _require_key(provider)

    if provider == "anthropic":
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key, timeout=30.0, max_retries=0)
        try:
            async with _gate_call("anthropic"):
                page = await client.models.list(limit=50)
        finally:
            await _close(client)
        return [m.id for m in page.data]

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, timeout=30.0, max_retries=0)
    try:
        async with _gate_call("openai"):
            page = await client.models.list()
    finally:
        await _close(client)
    return sorted(m.id for m in page.data)


def is_enabled(cfg: dict) -> bool:
    return bool((cfg or {}).get("ai_enabled", False))


async def analyze(market: dict, cfg: dict) -> dict:
    asked_at = _now_iso()
    cfg = cfg or {}
    if not cfg.get("ai_enabled", False):
        return _fail(
            str(cfg.get("ai_provider") or "anthropic"), "", asked_at,
            "AI analysis is turned off.",
            "Turn it on in Settings → AI analysis.")
    provider = str((cfg or {}).get("ai_provider") or "anthropic").lower()
    if provider not in PROVIDERS:
        provider = "anthropic"
    web_search = bool((cfg or {}).get("ai_web_search", False))
    model = str(
        (cfg or {}).get(f"ai_{provider}_model")
        or (DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else DEFAULT_OPENAI_MODEL)
    )

    payload = build_payload(market)
    if not payload.get("question"):
        return _fail(provider, model, asked_at,
                     "That market has no question text to read.", None)

    prompt = _render_prompt(payload, asked_at)
    started = time.monotonic()
    try:
        caller = _call_anthropic if provider == "anthropic" else _call_openai
        got = await caller(prompt, model, web_search)
    except _Refused as e:
        return _fail(provider, model, asked_at, e.reason, e.fix)
    except asyncio.TimeoutError:
        return _fail(provider, model, asked_at,
                     f"{provider} did not answer within {_TIMEOUT_SEC:.0f}s.",
                     "Try again, or pick a faster model.")
    except Exception as e:
        logger.warning("ai: %s call failed: %s: %s", provider, type(e).__name__, e)
        return _fail(provider, model, asked_at,
                     f"{provider} returned an error: {type(e).__name__}.",
                     _hint_for(e))

    try:
        data = json.loads(got["raw"])
    except Exception:
        data = None
    if not isinstance(data, dict):
        logger.warning("ai: %s returned unparseable JSON", provider)
        return _fail(provider, model, asked_at,
                     "The model's reply was not valid JSON.",
                     "Try again — this is usually transient.")

    return _shape(data, provider, got.get("model") or model, asked_at,
                  web_search, got.get("sources") or [],
                  round((time.monotonic() - started) * 1000.0))


def _hint_for(exc: BaseException) -> Optional[str]:
    name = type(exc).__name__
    if name == "AuthenticationError":
        return "The stored API key was rejected. Re-enter it in Settings → AI analysis."
    if name in ("APIConnectionError", "APITimeoutError"):
        return "Check your connection, then try again."
    if _is_rate_limit(exc):
        return "You hit the provider's rate limit. Wait a moment and try again."
    return None


def _fail(provider: str, model: str, asked_at: str,
          reason: str, fix: Optional[str]) -> dict:
    return {
        "ok": False, "provider": provider, "model": model,
        "askedAt": asked_at, "reason": reason, "fix": fix,
        "webSearch": False, "sources": [], "redactions": 0, "latencyMs": None,
        "summary": None, "resolutionNotes": [], "drivers": [],
        "unknowns": [], "reasoning": [], "lean": None, "confidence": None,
    }


def _shape(data: dict, provider: str, model: str, asked_at: str,
           web_search: bool, sources: list[dict], latency_ms: float) -> dict:
    summary, r1 = _strip_numeric_estimates(str(data.get("summary") or "").strip())
    notes, r2 = _clean_list(data.get("resolutionNotes"))
    drivers, r3 = _clean_list(data.get("drivers"))
    unknowns, r4 = _clean_list(data.get("unknowns"))
    reasoning, r5 = _clean_list(data.get("reasoning"))

    lean = data.get("lean")
    if lean not in LEANS:
        lean = "no_view"
    conf = data.get("confidence")
    if conf not in CONFIDENCES:
        conf = "low"

    if lean != "no_view" and not reasoning:
        lean, conf = "no_view", "low"

    return {
        "ok": True,
        "provider": provider,
        "model": model,
        "askedAt": asked_at,
        "latencyMs": latency_ms,
        "webSearch": bool(web_search),
        "sources": sources,
        "summary": summary or None,
        "resolutionNotes": notes,
        "drivers": drivers,
        "unknowns": unknowns,
        "reasoning": reasoning,
        "lean": lean,
        "confidence": conf,
        "redactions": r1 + r2 + r3 + r4 + r5,
        "reason": None,
        "fix": None,
    }


def status(cfg: dict) -> dict:
    cfg = cfg or {}
    provider = str(cfg.get("ai_provider") or "anthropic").lower()
    return {
        "enabled": bool(cfg.get("ai_enabled", False)),
        "provider": provider if provider in PROVIDERS else "anthropic",
        "webSearch": bool(cfg.get("ai_web_search", False)),
        "offline": term.is_offline(),
        "models": {
            "anthropic": str(cfg.get("ai_anthropic_model") or DEFAULT_ANTHROPIC_MODEL),
            "openai": str(cfg.get("ai_openai_model") or ""),
        },
        "keys": {
            p: {"present": ai_secrets.has_key(p), "masked": ai_secrets.masked(p)}
            for p in PROVIDERS
        },
    }
