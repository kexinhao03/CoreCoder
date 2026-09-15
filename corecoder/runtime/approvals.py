"""Immutable approval records and audit-safe argument summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "token", "password", "secret")


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    tool_call_id: str
    status: ApprovalStatus
    decision: ApprovalDecision | None
    tool_name: str
    arguments_summary: str
    workspace: str
    risk_reason: str
    requested_at: str
    resolved_at: str | None


def summarize_arguments(arguments: dict) -> str:
    """Return a bounded JSON summary with sensitive values redacted."""
    redacted = _redact_sensitive_values(arguments)
    return json.dumps(
        redacted,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )[:1000]


def _redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        used_keys = {key for key in value if isinstance(key, str)}
        redacted = {}
        for key, item in value.items():
            normalized_key = (
                key if isinstance(key, str) else _unique_typed_key(key, used_keys)
            )
            used_keys.add(normalized_key)
            redacted[normalized_key] = (
                "[REDACTED]"
                if isinstance(key, str)
                and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS)
                else _redact_sensitive_values(item)
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_values(item) for item in value]
    return value


def _unique_typed_key(key: Any, used_keys: set[str]) -> str:
    preferred = f"[{type(key).__name__}:{key}]"
    candidate = preferred
    suffix = 2
    while candidate in used_keys:
        candidate = f"{preferred}#{suffix}"
        suffix += 1
    return candidate
