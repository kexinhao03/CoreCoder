"""Copying redaction for durable and exported runtime facts."""

from __future__ import annotations

import re
import hashlib
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
    re.compile(r"(?i)\bsk-[a-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:[a-z0-9]+(?:[_-][a-z0-9]+)*[_-](?:secret|token|password|api[-_]key)(?:[_-][a-z0-9]+)*|"
        r"(?:secret|token|password|api[-_]key)[_-][a-z0-9]+|"
        r"(?:secret|token|password|api[-_]key)\s*[:=]\s*\S+)\b"
    ),
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
            if _is_sensitive_key(key):
                copied[exported_key] = _REDACTED
            elif isinstance(key, str) and key.lower() == "argv" and isinstance(item, (list, tuple)):
                copied[exported_key] = _redact_argv(item)
            else:
                copied[exported_key] = redact(item)
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
        redacted = pattern.sub(
            lambda match: (match.group(1) if pattern.groups else "") + _REDACTED,
            redacted,
        )
    return redacted


def redact_for_storage(tool_name: str, arguments: dict) -> tuple[dict, bool]:
    """Return redacted arguments and whether they remain replayable."""
    stored = redact(arguments)
    if tool_name == "write_file" and isinstance(arguments.get("content"), str):
        raw = arguments["content"].encode("utf-8")
        stored["content"] = {
            "redacted": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return stored, stored == arguments


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS)


def _redact_argv(argv: list[Any] | tuple[Any, ...]) -> list[Any] | tuple[Any, ...]:
    redacted = []
    redact_next = False
    for argument in argv:
        if redact_next:
            redacted.append(_REDACTED)
            redact_next = False
            continue
        if not isinstance(argument, str):
            redacted.append(redact(argument))
            continue
        name, separator, _value = argument.partition("=")
        if separator and _is_sensitive_argv_name(name):
            redacted.append(f"{name}={_REDACTED}")
            continue
        redacted.append(redact_text(argument))
        redact_next = argument.startswith("-") and _is_sensitive_argv_name(argument)
    return tuple(redacted) if isinstance(argv, tuple) else redacted


def _is_sensitive_argv_name(name: str) -> bool:
    normalized = name.lstrip("-").replace("-", "_")
    return _is_sensitive_key(normalized)


def _unique_export_key(key: object, used_keys: set[str]) -> str:
    preferred = f"[{type(key).__name__}:{key}]"
    candidate = preferred
    suffix = 2
    while candidate in used_keys:
        candidate = f"{preferred}#{suffix}"
        suffix += 1
    return candidate
