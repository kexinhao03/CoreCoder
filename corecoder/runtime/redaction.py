"""Copying redaction for durable and exported runtime facts."""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "authorization",
    "cookie",
)
_TEXT_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s'\"]+"),
    re.compile(r"(?i)(https?://[^\s:@/]+:)[^\s@/]+@"),
)
_REDACTED = "[REDACTED]"


def redact(value: Any) -> Any:
    """Return a recursively redacted copy without mutating ``value``."""
    if isinstance(value, dict):
        used_keys = {key for key in value if isinstance(key, str)}
        copied = {}
        for key, item in value.items():
            exported_key = key if isinstance(key, str) else _unique_export_key(key, used_keys)
            used_keys.add(exported_key)
            copied[exported_key] = _REDACTED if _is_sensitive_key(key) else redact(item)
        return copied
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Redact common bearer tokens and URL user-info in free-form text."""
    redacted = text
    for pattern in _TEXT_SECRET_PATTERNS:
        redacted = pattern.sub(r"\1" + _REDACTED, redacted)
    return redacted


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS)


def _unique_export_key(key: object, used_keys: set[str]) -> str:
    preferred = f"[{type(key).__name__}:{key}]"
    candidate = preferred
    suffix = 2
    while candidate in used_keys:
        candidate = f"{preferred}#{suffix}"
        suffix += 1
    return candidate
