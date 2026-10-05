from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CHAIN_ID = 137
NETWORK = "mainnet"

EXCHANGE_VERSION = "2"
EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
COLLATERAL_PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + "00" * 32

SIGNATURE_TYPE_EOA = 0


_DPAPI_MARKER = b"#KRYPT-DPAPI-v1\n"
_KEYRING_MARKER = b"#KRYPT-KEYRING-v1\n"
_KEYRING_SERVICE = "krypt-polybot"
_KEYRING_MASTER_ACCOUNT = "cred-master-key"
_warned_plaintext = False
_plaintext_error_logged = False
_keyring_master_cache: Optional[bytes] = None


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: "_DATA_BLOB") -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def _dpapi_encrypt(data: bytes) -> bytes:
        out = _DATA_BLOB()
        blob_in = _to_blob(data)
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptProtectData failed")
        return _from_blob(out)

    def _dpapi_decrypt(data: bytes) -> bytes:
        out = _DATA_BLOB()
        blob_in = _to_blob(data)
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptUnprotectData failed")
        return _from_blob(out)
else:
    def _dpapi_encrypt(data: bytes) -> bytes:
        raise OSError("DPAPI not available")

    def _dpapi_decrypt(data: bytes) -> bytes:
        raise OSError("DPAPI not available")


def _keyring_set(account: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, account, value)
        return True
    except Exception:
        return False


def _keyring_get(account: str) -> Optional[str]:
    try:
        import keyring
        return keyring.get_password(_KEYRING_SERVICE, account)
    except Exception:
        return None


def _fernet(key: bytes):
    from cryptography.fernet import Fernet
    return Fernet(key)


def _get_master_key() -> Optional[bytes]:
    global _keyring_master_cache
    if _keyring_master_cache is not None:
        return _keyring_master_cache
    existing = _keyring_get(_KEYRING_MASTER_ACCOUNT)
    if existing:
        key = existing.encode("ascii")
        try:
            _fernet(key)
            _keyring_master_cache = key
            return key
        except Exception:
            logger.warning("Stored keychain master key is corrupt; regenerating.")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    if not _keyring_set(_KEYRING_MASTER_ACCOUNT, key.decode("ascii")):
        return None
    if _keyring_get(_KEYRING_MASTER_ACCOUNT) != key.decode("ascii"):
        return None
    _keyring_master_cache = key
    return key


def _keyring_available() -> bool:
    return _get_master_key() is not None


def _keyring_encrypt(data: bytes) -> bytes:
    key = _get_master_key()
    if key is None:
        raise OSError("no OS keychain backend available")
    return _fernet(key).encrypt(data)


def _keyring_decrypt(token: bytes) -> bytes:
    key = _get_master_key()
    if key is None:
        raise OSError("no OS keychain backend available")
    return _fernet(key).decrypt(token)


def _chmod_600(path: Path) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _atomic_write_600(path: Path, tmp: Path, data: bytes) -> None:
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    if os.name == "posix":
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    else:
        tmp.write_bytes(data)
    _chmod_600(tmp)
    tmp.replace(path)


def _write_secret_bytes(path: Path, data: bytes) -> None:
    global _warned_plaintext, _plaintext_error_logged
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    if _dpapi_available():
        try:
            _atomic_write_600(path, tmp, _DPAPI_MARKER + base64.b64encode(_dpapi_encrypt(data)))
            return
        except Exception as e:
            logger.warning(f"DPAPI encrypt failed, trying OS keychain: {e}")
    try:
        if _keyring_available():
            _atomic_write_600(path, tmp, _KEYRING_MARKER + _keyring_encrypt(data))
            return
    except Exception as e:
        logger.warning(f"Keychain encrypt failed, storing plaintext: {e}")
    if not _warned_plaintext:
        logger.error(
            "SECURITY: credentials stored UNENCRYPTED (no DPAPI or OS-keychain "
            "backend on this platform). The file is chmod 600 but NOT encrypted "
            "at rest. Surfaced to the UI as credentials_status().keyStoredUnencrypted."
        )
        _warned_plaintext = True
    _plaintext_error_logged = False
    _atomic_write_600(path, tmp, data)


def _read_secret_bytes(path: Path, upgrade: bool = True) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(_DPAPI_MARKER):
        return _dpapi_decrypt(base64.b64decode(raw[len(_DPAPI_MARKER):]))
    if raw.startswith(_KEYRING_MARKER):
        return _keyring_decrypt(raw[len(_KEYRING_MARKER):])
    if upgrade and (_dpapi_available() or _keyring_available()):
        try:
            _write_secret_bytes(path, raw)
        except Exception:
            pass
    return raw


def _is_plaintext_secret_file(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        with open(path, "rb") as fh:
            head = fh.read(max(len(_DPAPI_MARKER), len(_KEYRING_MARKER)))
        return not (head.startswith(_DPAPI_MARKER) or head.startswith(_KEYRING_MARKER))
    except OSError:
        return False


def key_stored_unencrypted(env: Optional[str] = None) -> bool:
    try:
        return _is_plaintext_secret_file(_wallet_key_file(env or _current_env))
    except Exception:
        return False


def _log_plaintext_at_rest_once() -> None:
    global _plaintext_error_logged
    if _plaintext_error_logged:
        return
    try:
        if key_stored_unencrypted():
            _plaintext_error_logged = True
            logger.error(
                "SECURITY: wallet private key is stored UNENCRYPTED on disk "
                "(no DPAPI or OS-keychain backend available on this platform). "
                "The file is chmod 600 but NOT encrypted at rest. Anyone who can "
                "read this account's files can steal the key. "
                "credentials_status().keyStoredUnencrypted == true."
            )
    except Exception:
        pass


def _credentials_dir() -> Path:
    base = os.environ.get("KRYPT_POLYBOT_USERDATA")
    if base:
        return Path(base) / "credentials"
    return Path(__file__).resolve().parent / "credentials"


_VALID_ENVS = ("mainnet",)


def _validate_env(env: str) -> str:
    e = env if isinstance(env, str) else ""
    e = e.strip()
    if (e not in _VALID_ENVS) or ("/" in e) or ("\\" in e) or (".." in e) or (os.sep in e):
        raise ValueError(
            f"invalid network env {env!r}: only {_VALID_ENVS} are allowed "
            f"(no path separators or '..')"
        )
    return e


def _wallet_key_file(env: str = NETWORK) -> Path:
    return _credentials_dir() / f"wallet.{_validate_env(env)}.key"


def _api_creds_file(env: str = NETWORK) -> Path:
    return _credentials_dir() / f"apicreds.{_validate_env(env)}.json"


def _wallet_meta_file(env: str = NETWORK) -> Path:
    return _credentials_dir() / f"wallet.{_validate_env(env)}.meta.json"


_current_env: str = NETWORK
_cached_account = None
_cached_api_creds: Optional[dict] = None


def set_env(env: str) -> None:
    global _current_env
    _current_env = NETWORK


def get_env() -> str:
    return _current_env


def reset_credential_cache() -> None:
    global _cached_account, _cached_api_creds
    _cached_account = None
    _cached_api_creds = None


def _eth_account():
    try:
        from eth_account import Account
        return Account
    except Exception as e:
        raise RuntimeError(
            "eth-account is required for Polymarket signing. "
            "Install it with `pip install eth-account`."
        ) from e


def _normalize_priv(text: str) -> str:
    pk = (text or "").strip()
    for line in pk.splitlines():
        line = line.strip().strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("0x"):
            _, _, line = line.partition("=")
            line = line.strip().strip('"').strip("'")
        pk = line
        break
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return pk


def _load_account():
    global _cached_account
    if _cached_account is not None:
        return _cached_account
    f = _wallet_key_file(_current_env)
    if not f.exists():
        raise FileNotFoundError(f"Polymarket wallet key not configured ({f})")
    _log_plaintext_at_rest_once()
    pk = _normalize_priv(_read_secret_bytes(f).decode("utf-8", "replace"))
    acct = _eth_account().from_key(pk)
    _cached_account = acct
    return acct


def get_address() -> str:
    return _load_account().address


def _load_api_creds() -> Optional[dict]:
    global _cached_api_creds
    if _cached_api_creds is not None:
        return _cached_api_creds
    f = _api_creds_file(_current_env)
    if not f.exists():
        return None
    try:
        data = json.loads(_read_secret_bytes(f).decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("apiKey"):
            _cached_api_creds = data
            return data
    except Exception:
        return None
    return None


def get_api_creds() -> Optional[dict]:
    return _load_api_creds()


def has_api_creds(env: Optional[str] = None) -> bool:
    return _api_creds_file(env or _current_env).exists()


def set_api_creds(creds: dict, env: Optional[str] = None) -> None:
    global _cached_api_creds
    e = env or _current_env
    _write_secret_bytes(
        _api_creds_file(e), json.dumps(creds).encode("utf-8")
    )
    if e == _current_env:
        _cached_api_creds = creds


def clear_api_creds(env: Optional[str] = None) -> None:
    global _cached_api_creds
    e = env or _current_env
    f = _api_creds_file(e)
    if f.exists():
        try:
            f.unlink()
        except Exception:
            pass
    if e == _current_env:
        _cached_api_creds = None


def _perps_creds_file(env: str = NETWORK) -> Path:
    return _credentials_dir() / f"perps.{_validate_env(env)}.json"


def load_perps_creds(env: Optional[str] = None) -> Optional[dict]:
    f = _perps_creds_file(env or _current_env)
    if not f.exists():
        return None
    try:
        data = json.loads(_read_secret_bytes(f).decode("utf-8", "replace"))
    except Exception as e:
        logger.warning(f"perps credentials unreadable: {e}")
        return None
    return data if isinstance(data, dict) else None


def save_perps_creds(creds: dict, env: Optional[str] = None) -> None:
    _write_secret_bytes(
        _perps_creds_file(env or _current_env), json.dumps(creds).encode("utf-8"),
    )


def clear_perps_creds(env: Optional[str] = None) -> None:
    f = _perps_creds_file(env or _current_env)
    if f.exists():
        try:
            f.unlink()
        except Exception:
            pass


def sign_typed_data(typed_data: dict) -> str:
    return _eip712_sign(typed_data)


SIGNATURE_TYPE_POLY_PROXY = 1
SIGNATURE_TYPE_POLY_GNOSIS_SAFE = 2
SIGNATURE_TYPE_POLY_1271 = 3


class WalletMetaError(RuntimeError):
    pass


_wallet_meta_cache: dict[str, dict] = {}


def get_wallet_meta(env: Optional[str] = None) -> dict:
    e = env or _current_env
    f = _wallet_meta_file(e)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        cached = _wallet_meta_cache.get(e)
        if cached is not None:
            return dict(cached)
        raise WalletMetaError(f"wallet meta {f.name} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        cached = _wallet_meta_cache.get(e)
        if cached is not None:
            return dict(cached)
        raise WalletMetaError(f"wallet meta {f.name} is not a JSON object")
    _wallet_meta_cache[e] = dict(data)
    return data


def set_wallet_meta(
    funder: str = "", signature_type: int = 0, env: Optional[str] = None
) -> None:
    e = env or _current_env
    f = _wallet_meta_file(e)
    f.parent.mkdir(parents=True, exist_ok=True)
    payload = {"funder": (funder or "").strip(), "signatureType": int(signature_type or 0)}
    tmp = f.with_suffix(f.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(f)
    _wallet_meta_cache[e] = dict(payload)


def get_funder(env: Optional[str] = None) -> str:
    return (get_wallet_meta(env).get("funder") or "").strip()


def get_signature_type(env: Optional[str] = None) -> int:
    try:
        return int(get_wallet_meta(env).get("signatureType") or 0)
    except (TypeError, ValueError):
        return 0


def trading_address(env: Optional[str] = None) -> str:
    return get_funder(env) or get_address()


def credentials_present(env: Optional[str] = None) -> bool:
    return _wallet_key_file(env or _current_env).exists()


def credentials_status(env: Optional[str] = None) -> dict:
    e = env or _current_env
    wf = _wallet_key_file(e)
    meta_error = ""
    try:
        funder = get_funder(e)
        sig_type = get_signature_type(e)
    except WalletMetaError as exc:
        funder, sig_type, meta_error = "", 0, str(exc)
    _log_plaintext_at_rest_once()
    info = {
        "env": e,
        "hasWalletKey": wf.exists(),
        "hasApiCreds": _api_creds_file(e).exists(),
        "address": "",
        "addressPreview": "",
        "funder": funder,
        "signatureType": sig_type,
        "metaError": meta_error,
        "walletMode": "error" if meta_error else ("deposit" if funder else "eoa"),
        "keyStoredUnencrypted": _is_plaintext_secret_file(wf),
    }
    if wf.exists():
        try:
            pk = _normalize_priv(_read_secret_bytes(wf).decode("utf-8", "replace"))
            addr = _eth_account().from_key(pk).address
            info["address"] = addr
            info["addressPreview"] = addr[:6] + "…" + addr[-4:]
        except Exception:
            pass
    return info


def credentials_status_all() -> dict:
    return {
        "current": _current_env,
        "mainnet": credentials_status(NETWORK),
    }


def save_credentials(
    private_key: str, api_creds: Optional[dict] = None, env: Optional[str] = None,
    funder: Optional[str] = None, signature_type: Optional[int] = None,
) -> None:
    e = env or _current_env
    pk = _normalize_priv(private_key)
    if not pk:
        raise ValueError("private key is empty")
    try:
        _eth_account().from_key(pk)
    except Exception as ex:
        raise ValueError(f"private key did not parse: {ex}") from ex
    f = (funder or "").strip()
    if f and (not f.startswith("0x") or len(f) != 42):
        raise ValueError("deposit wallet address must be a 0x… 42-char address")
    _write_secret_bytes(_wallet_key_file(e), (pk + "\n").encode("utf-8"))
    if api_creds:
        set_api_creds(api_creds, e)
    else:
        acf = _api_creds_file(e)
        if acf.exists():
            try:
                acf.unlink()
            except Exception:
                pass
    if signature_type is None:
        signature_type = SIGNATURE_TYPE_POLY_1271 if f else 0
    set_wallet_meta(funder=f, signature_type=int(signature_type), env=e)
    if e == _current_env:
        reset_credential_cache()


def clear_credentials(env: Optional[str] = None) -> None:
    e = env or _current_env
    for p in (_wallet_key_file(e), _api_creds_file(e), _perps_creds_file(e)):
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    if e == _current_env:
        reset_credential_cache()


def migrate_legacy_credentials(target_env: str) -> bool:
    return False


_time_offset: float = 0.0


def set_time_offset(offset: float) -> None:
    global _time_offset
    _time_offset = float(offset)


def get_time_offset() -> float:
    return _time_offset


def sync_server_time(force: bool = False) -> int:
    return int(_time_offset)


def now_ts() -> int:
    return int(time.time() + _time_offset)


_CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"


def _eip712_sign(typed_data: dict) -> str:
    from eth_account.messages import encode_typed_data
    acct = _load_account()
    signable = encode_typed_data(full_message=typed_data)
    signed = acct.sign_message(signable)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def l1_headers(nonce: int = 0) -> dict:
    ts = str(now_ts())
    addr = get_address()
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": CHAIN_ID},
        "message": {
            "address": addr,
            "timestamp": ts,
            "nonce": int(nonce),
            "message": _CLOB_AUTH_MESSAGE,
        },
    }
    return {
        "POLY_ADDRESS": addr,
        "POLY_SIGNATURE": _eip712_sign(typed),
        "POLY_TIMESTAMP": ts,
        "POLY_NONCE": str(int(nonce)),
    }


def _b64url_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _b64url_encode(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii").replace("+", "-").replace("/", "_")


def l2_headers(method: str, path: str, body: str = "") -> dict:
    creds = _load_api_creds()
    if not creds:
        raise RuntimeError("Polymarket API credentials not derived yet")
    ts = str(now_ts())
    msg = f"{ts}{method.upper()}{path}{body or ''}"
    sig = hmac.new(
        _b64url_decode(creds["secret"]), msg.encode("utf-8"), hashlib.sha256
    ).digest()
    return {
        "POLY_ADDRESS": get_address(),
        "POLY_SIGNATURE": _b64url_encode(sig),
        "POLY_TIMESTAMP": ts,
        "POLY_API_KEY": creds["apiKey"],
        "POLY_PASSPHRASE": creds["passphrase"],
    }


_DECIMALS = 1_000_000


def _round_amt(x: float) -> int:
    return int(round(x))


def _load_private_key() -> str:
    f = _wallet_key_file(_current_env)
    if not f.exists():
        raise FileNotFoundError(f"Polymarket wallet key not configured ({f})")
    return _normalize_priv(_read_secret_bytes(f).decode("utf-8", "replace"))


def verify_signer() -> str:
    from eth_account import Account
    from py_clob_client_v2.signer import Signer
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2
    from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2
    from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
    from py_clob_client_v2.order_utils.model.side import Side

    test_key = "0x" + "11" * 32
    addr = Account.from_key(test_key).address
    signer = Signer(test_key, CHAIN_ID)
    builder = ExchangeOrderBuilderV2(EXCHANGE_ADDRESS, CHAIN_ID, signer)
    od = OrderDataV2(
        maker=addr, signer=addr, tokenId="1",
        makerAmount="1000000", takerAmount="2000000",
        side=Side.BUY, signatureType=SignatureTypeV2.POLY_1271, expiration="0",
    )
    s = builder.build_signed_order(od)
    sig = getattr(s, "signature", None)
    if not sig or not str(sig).startswith("0x") or len(str(sig)) < 132:
        raise RuntimeError(f"signer produced no/short signature: {sig!r}")
    return str(sig)


SESSION_SIGNER_MAGIC = bytes.fromhex(
    "6492649264926492649264926492649264926492649264926492649264926492")
_signer_role = "OWNER"


def set_signer_role(role: str) -> None:
    global _signer_role
    _signer_role = "SESSION_KEY" if str(role).upper() == "SESSION_KEY" else "OWNER"


def get_signer_role() -> str:
    return _signer_role


def wrap_session_signer_signature(session_signer: str, signature: str) -> str:
    from eth_abi import encode as abi_encode
    signer_id = bytes.fromhex(session_signer[2:] if session_signer.startswith("0x") else session_signer)
    sig = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)
    payload = abi_encode(["bytes32", "bytes32", "bytes"],
                         [signer_id.rjust(32, b"\x00"), b"\x00" * 32, sig])
    return "0x" + (payload + SESSION_SIGNER_MAGIC).hex()


def _create_signed_order_1271(
    *, token_id: str, side: str, price: float, size: float,
    neg_risk: bool, expiration: int,
) -> dict:
    funder = get_funder()
    if not funder:
        raise RuntimeError("POLY_1271 order requested but no funder (deposit wallet) configured")
    from py_clob_client_v2.signer import Signer
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2
    from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2
    from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
    from py_clob_client_v2.order_utils.model.side import Side

    side_u = side.upper()
    if side_u == "BUY":
        maker_amount = _round_amt(price * size * _DECIMALS)
        taker_amount = _round_amt(size * _DECIMALS)
    else:
        maker_amount = _round_amt(size * _DECIMALS)
        taker_amount = _round_amt(price * size * _DECIMALS)

    exchange = NEG_RISK_EXCHANGE_ADDRESS if neg_risk else EXCHANGE_ADDRESS
    signer = Signer(_load_private_key(), CHAIN_ID)
    builder = ExchangeOrderBuilderV2(exchange, CHAIN_ID, signer)
    od = OrderDataV2(
        maker=funder, signer=funder, tokenId=str(int(token_id)),
        makerAmount=str(int(maker_amount)), takerAmount=str(int(taker_amount)),
        side=Side.BUY if side_u == "BUY" else Side.SELL,
        signatureType=SignatureTypeV2.POLY_1271, expiration=str(int(expiration)),
    )
    s = builder.build_signed_order(od)
    signature = s.signature
    if _signer_role == "SESSION_KEY":
        signature = wrap_session_signer_signature(get_address(), str(signature))
    return {
        "salt": int(s.salt),
        "maker": s.maker,
        "signer": s.signer,
        "tokenId": str(s.tokenId),
        "makerAmount": str(s.makerAmount),
        "takerAmount": str(s.takerAmount),
        "side": side_u,
        "expiration": str(s.expiration),
        "signatureType": int(s.signatureType),
        "signature": signature,
        "timestamp": str(s.timestamp),
        "metadata": s.metadata,
        "builder": s.builder,
    }


def create_signed_order(
    *,
    token_id: str,
    side: str,
    price: float,
    size: float,
    neg_risk: bool = False,
    expiration: int = 0,
    salt: Optional[int] = None,
    ts_ms: Optional[int] = None,
) -> dict:
    if get_signature_type() == SIGNATURE_TYPE_POLY_1271:
        return _create_signed_order_1271(
            token_id=token_id, side=side, price=price, size=size,
            neg_risk=neg_risk, expiration=expiration,
        )
    acct = _load_account()
    addr = acct.address
    sig_type = get_signature_type()
    maker = get_funder() or addr
    side_u = side.upper()
    side_int = 0 if side_u == "BUY" else 1

    if side_u == "BUY":
        maker_amount = _round_amt(price * size * _DECIMALS)
        taker_amount = _round_amt(size * _DECIMALS)
    else:
        maker_amount = _round_amt(size * _DECIMALS)
        taker_amount = _round_amt(price * size * _DECIMALS)

    if salt is None:
        salt = int(hashlib.sha256(
            f"{addr}{token_id}{time.time_ns()}".encode()
        ).hexdigest()[:16], 16) & ((1 << 48) - 1)
    if ts_ms is None:
        ts_ms = time.time_ns() // 1_000_000

    verifying = NEG_RISK_EXCHANGE_ADDRESS if neg_risk else EXCHANGE_ADDRESS

    order_msg = {
        "salt": int(salt),
        "maker": maker,
        "signer": addr,
        "tokenId": int(token_id),
        "makerAmount": int(maker_amount),
        "takerAmount": int(taker_amount),
        "side": int(side_int),
        "signatureType": int(sig_type),
        "timestamp": int(ts_ms),
        "metadata": ZERO_BYTES32,
        "builder": ZERO_BYTES32,
    }

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": EXCHANGE_VERSION,
            "chainId": CHAIN_ID,
            "verifyingContract": verifying,
        },
        "message": order_msg,
    }
    signature = _eip712_sign(typed)

    return {
        "salt": int(salt),
        "maker": maker,
        "signer": addr,
        "taker": ZERO_ADDRESS,
        "tokenId": str(int(token_id)),
        "makerAmount": str(int(maker_amount)),
        "takerAmount": str(int(taker_amount)),
        "expiration": str(int(expiration)),
        "side": side_u,
        "signatureType": int(sig_type),
        "signature": signature,
        "timestamp": str(int(ts_ms)),
        "metadata": ZERO_BYTES32,
        "builder": ZERO_BYTES32,
    }


def prime_credentials(sync_time: bool = True) -> bool:
    _load_account()
    _load_api_creds()
    return True
