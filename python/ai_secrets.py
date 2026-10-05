from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import polymarket_auth as auth

logger = logging.getLogger("krypt.ai.secrets")

_VALID = ("openai", "anthropic")


def _path(provider: str) -> Path:
    if provider not in _VALID:
        raise ValueError(f"unknown provider {provider!r}")
    return auth._credentials_dir() / f"ai.{provider}.key"


def save_key(provider: str, key: str) -> None:
    k = (key or "").strip()
    if not k:
        clear_key(provider)
        return
    auth._write_secret_bytes(_path(provider), k.encode("utf-8"))
    logger.info("ai: stored %s api key", provider)


def load_key(provider: str) -> Optional[str]:
    p = _path(provider)
    if not p.exists():
        return None
    try:
        return auth._read_secret_bytes(p).decode("utf-8").strip() or None
    except Exception as e:
        logger.warning("ai: could not read %s key: %s", provider, e)
        return None


def has_key(provider: str) -> bool:
    return load_key(provider) is not None


def clear_key(provider: str) -> None:
    p = _path(provider)
    try:
        if p.exists():
            p.unlink()
            logger.info("ai: cleared %s api key", provider)
    except OSError as e:
        logger.warning("ai: could not clear %s key: %s", provider, e)


def masked(provider: str) -> Optional[str]:
    k = load_key(provider)
    if not k:
        return None
    return f"{k[:6]}…{k[-4:]}" if len(k) > 14 else "…"
