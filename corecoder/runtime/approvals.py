"""Immutable approval records and audit-safe argument summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from .redaction import redact


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
    attempt: int | None = None
    experiment_id: str | None = None
    definition_hash: str | None = None


def summarize_arguments(arguments: dict) -> str:
    """Return a bounded JSON summary with sensitive values redacted."""
    redacted = redact(arguments)
    return json.dumps(
        redacted,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )[:1000]
