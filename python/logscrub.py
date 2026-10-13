"""Keep credentials out of the logs.

The backend holds a wallet private key, the CLOB L2 API secret and
passphrase, a Perps signer, chat-bot tokens, AI provider keys and one bearer
token per AI agent. Users paste their logs into a chat window when something
breaks, so a log line is the most likely way a secret escapes this app — and
it escapes to exactly the people trying to help.

Two independent passes, because each catches what the other misses:

  * **By value.** The literal secrets currently stored are redacted wherever
    they appear, whatever the surrounding text. Exact, and it catches a value
    logged by a library we do not control.
  * **By shape.** Discord / Telegram token patterns, AI provider keys with a
    known prefix (sk-…, AIza…), our own agent token prefix (kp_…), raw
    32-byte hex private keys, PEM private-key blocks and Authorization headers
    are redacted even when the value is one we have never seen — a key pasted
    into the wrong box, or a second account's.

Deliberately NOT scrubbed: conditionIds (0x + 64 hex — the same shape as a
private key, so the hex rule only fires on a value labelled as a key), order
ids, wallet addresses and bare UUIDs. They are the most useful thing in a
trading log, and the real key is caught by value anyway.

This module runs inside the log pipeline: nothing here may raise, and nothing
here touches the network.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

REDACTED = "[redacted]"

_DISCORD = re.compile(r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}\b")
_TELEGRAM = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,45}\b")
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|x-api-key|poly_api_key|poly_passphrase|poly_signature)"
    r"\s*[:=]\s*\S.*")
_SCHEME_TOKEN = re.compile(r"(?i)\b(?:bearer|bot)\s+[A-Za-z0-9._\-]{8,}")
_TG_URL = re.compile(r"(api\.telegram\.org/bot)[^/\s]+")
_DISCORD_WEBHOOK_URL = re.compile(
    r"(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_\-]+")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{19,}")
_GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])")
_AGENT_TOKEN = re.compile(r"(?i)\bkp_[A-Za-z0-9_\-]{20,}")
_LABELLED_HEX_KEY = re.compile(
    r"(?i)((?:private[_ ]?key|priv[_ ]?key|secret|passphrase|seed)\W{0,4})"
    r"(?:0x)?[0-9a-f]{64}\b")

_SHAPES = (_PEM, _DISCORD, _TELEGRAM, _SK_KEY, _GOOGLE_KEY, _AGENT_TOKEN,
           _SCHEME_TOKEN, _AUTH_HEADER)

_values: list[str] = []


def set_known_secrets(values: Iterable[str]) -> None:
    """Register the literal secrets currently stored, longest first so a
    substring of one cannot mask a longer match. Values under 12 characters
    are ignored: redacting a short common string would shred every log line."""
    global _values
    _values = sorted({v.strip() for v in values if isinstance(v, str) and len(v.strip()) >= 12},
                     key=len, reverse=True)


def known_count() -> int:
    return len(_values)


def _read(path) -> str:
    import polymarket_auth as auth
    return auth._read_secret_bytes(path, upgrade=False).decode("utf-8", "replace").strip()


def _json_secrets(text: str) -> list[str]:
    """String values of a stored JSON credential (L2 creds, Perps signer)
    that are secrets, not identifiers: every value except addresses, ids and
    timestamps."""
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            kl = str(k).lower()
            if not isinstance(v, str) or "address" in kl or kl in ("owner", "expiry", "expires", "env"):
                continue
            out.append(v)
    return out


def refresh_known_secrets() -> None:
    """Re-read every secret the app holds from the credential store. Cheap;
    called at startup and after any credential change. Listed from the store
    rather than from config: the scrubber must cover a token whatever config
    says, including one whose agent was just deleted."""
    try:
        import polymarket_auth as auth
        d = auth._credentials_dir()
        found: list[str] = []
        if d.exists():
            for p in d.iterdir():
                name = p.name
                if not p.is_file() or name.endswith((".tmp", ".bak")):
                    continue
                try:
                    if name.startswith("wallet.") and name.endswith(".key"):
                        raw = _read(p)
                        found.append(raw)
                        found.append(raw[2:] if raw.lower().startswith("0x") else "0x" + raw)
                    elif name.startswith(("apicreds.", "perps.")) and name.endswith(".json"):
                        found += _json_secrets(_read(p))
                    elif name.endswith((".token", ".key")):
                        found.append(_read(p))
                except Exception:   # noqa: BLE001 — one unreadable file is not fatal
                    continue
        set_known_secrets(found)
    except Exception:   # noqa: BLE001 — never let scrubber upkeep break logging
        pass


def add_known_secret(value: str) -> None:
    """Register one new secret without re-reading the store (a token just
    minted)."""
    set_known_secrets([*_values, value])


def looks_secret(text: str) -> bool:
    """True when `text` carries any part of a known secret, in ANY case, or a
    secret's shape.

    scrub() matches known values literally, which is right for a log line we
    wrote ourselves. It is not enough for a string an AI agent chose: a tool
    may lower-case a conditionId, so an agent that echoed its own bearer token
    where an id goes would defeat a literal match. This folds case, and also
    catches a FRAGMENT (12+ chars) of a known value, because a display field
    gets clipped and a clipped token is still most of a token."""
    if not text:
        return False
    if scrub(text) != text:
        return True
    low = text.lower()
    for v in _values:
        vl = v.lower()
        if vl in low:
            return True
        if len(low) >= 12 and low in vl:
            return True
    return False


def scrub(text: str) -> str:
    if not text:
        return text
    out = text
    for v in _values:
        if v in out:
            out = out.replace(v, REDACTED)
    for pattern in _SHAPES:
        if pattern is _AUTH_HEADER:
            out = pattern.sub(lambda m: f"{m.group(1)}: {REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    out = _LABELLED_HEX_KEY.sub(lambda m: m.group(1) + REDACTED, out)
    out = _TG_URL.sub(r"\1" + REDACTED, out)
    out = _DISCORD_WEBHOOK_URL.sub(r"\1" + REDACTED, out)
    return out


class ScrubFilter(logging.Filter):
    """Attach to every HANDLER (a filter on a logger does not see records
    propagated from its children), so the file and the stdout stream are both
    covered by one thing rather than each remembering to scrub."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            cleaned = scrub(msg)
            if cleaned != msg:
                record.msg = cleaned
                record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = scrub(record.exc_text)
        except Exception:   # noqa: BLE001
            pass
        return True


def install(*loggers: logging.Logger) -> ScrubFilter:
    """Put one ScrubFilter on every handler of the given loggers (root by
    default). Idempotent."""
    f = ScrubFilter()
    for lg in loggers or (logging.getLogger(),):
        for h in lg.handlers:
            if not any(isinstance(x, ScrubFilter) for x in h.filters):
                h.addFilter(f)
    return f
