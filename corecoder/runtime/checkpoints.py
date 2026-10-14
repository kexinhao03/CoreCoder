"""Authenticated, identity-bound encryption for private recovery context."""

from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet, InvalidToken

_UNSET = object()


class CheckpointCipher:
    """Encrypt JSON records without placing recovery keys in durable storage."""

    def __init__(self, key: str | bytes | None = _UNSET) -> None:
        if key is _UNSET:
            key = os.environ.get("RELIAGENT_CHECKPOINT_KEY")
        if not isinstance(key, (str, bytes)) or not key:
            raise ValueError("a checkpoint key is required")
        try:
            self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("invalid checkpoint key") from error

    def seal(self, identity: dict, payload: dict) -> bytes:
        return self._fernet.encrypt(
            json.dumps(
                {"schema_version": 1, "identity": identity, "payload": payload},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def open(self, identity: dict, ciphertext: bytes) -> dict:
        try:
            record = json.loads(self._fernet.decrypt(ciphertext))
        except (InvalidToken, ValueError, TypeError, UnicodeError) as error:
            raise ValueError("checkpoint authentication failed") from error
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 1
            or record.get("identity") != identity
            or not isinstance(record.get("payload"), dict)
        ):
            raise ValueError("checkpoint identity or schema mismatch")
        return record["payload"]
