from __future__ import annotations

import base64
import binascii

from cryptography.fernet import Fernet, InvalidToken

from agent.core.config import e3_encryption_key


_FERNET_PREFIX = "fernet:"


def _fernet() -> Fernet:
    return Fernet(e3_encryption_key())


def encrypt_secret(text: str) -> str:
    token = _fernet().encrypt(text.encode("utf-8")).decode("ascii")
    return f"{_FERNET_PREFIX}{token}"


def decrypt_secret(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""

    if raw.startswith(_FERNET_PREFIX):
        encrypted = raw[len(_FERNET_PREFIX) :]
        return _fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")

    # Backward-compatible migration path for old base64-only rows.
    try:
        return base64.b64decode(raw.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidToken("Unsupported secret format") from exc
