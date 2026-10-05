from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import polymarket_auth as auth

logger = logging.getLogger("krypt.remote.secrets")

_VALID = ("discord", "telegram")


def _path(platform: str) -> Path:
    if platform not in _VALID:
        raise ValueError(f"unknown platform {platform!r}")
    return auth._credentials_dir() / f"bot.{platform}.token"


def save_token(platform: str, token: str) -> None:
    t = (token or "").strip()
    if not t:
        clear_token(platform)
        return
    auth._write_secret_bytes(_path(platform), t.encode("utf-8"))
    logger.info("remote: stored %s bot token", platform)


def load_token(platform: str) -> Optional[str]:
    p = _path(platform)
    if not p.exists():
        return None
    try:
        return auth._read_secret_bytes(p).decode("utf-8").strip() or None
    except Exception as e:
        logger.warning("remote: could not read %s token: %s", platform, e)
        return None


def has_token(platform: str) -> bool:
    return load_token(platform) is not None


def clear_token(platform: str) -> None:
    p = _path(platform)
    try:
        if p.exists():
            p.unlink()
            logger.info("remote: cleared %s bot token", platform)
    except OSError as e:
        logger.warning("remote: could not clear %s token: %s", platform, e)


def masked(platform: str) -> Optional[str]:
    t = load_token(platform)
    if not t:
        return None
    return f"{t[:4]}…{t[-4:]}" if len(t) > 12 else "…"
