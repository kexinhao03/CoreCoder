"""Immutable records returned by the durable runtime store."""

from __future__ import annotations

from dataclasses import dataclass

from .state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus


@dataclass(frozen=True)
class RunRecord:
    id: str
    goal: str
    workflow: str
    status: RunStatus
    workspace: str
    model: str
    prompt_version: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ToolCallRecord:
    id: str
    run_id: str
    retry_of: str | None
    tool_name: str
    arguments: dict
    risk_level: RiskLevel
    execution_kind: ExecutionKind
    idempotent: bool
    idempotency_key: str | None
    status: ToolCallStatus
    attempt: int
    timeout_seconds: int
    result_summary: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    ended_at: str | None


@dataclass(frozen=True)
class EventRecord:
    id: int
    run_id: str
    sequence: int
    type: str
    payload: dict
    created_at: str
