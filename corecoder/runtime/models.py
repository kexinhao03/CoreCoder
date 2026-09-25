"""Immutable records returned by the durable runtime store."""

from __future__ import annotations

from dataclasses import dataclass

from .state import ExecutionKind, RiskLevel, RunStatus, StepStatus, ToolCallStatus


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
    started_at: str | None = None
    ended_at: str | None = None


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
    step_id: str | None = None


@dataclass(frozen=True)
class StepRecord:
    id: str
    run_id: str
    sequence: int
    title: str
    status: StepStatus
    attempt_count: int
    created_at: str
    updated_at: str
    started_at: str | None
    ended_at: str | None
    step_key: str | None = None
    definition_version: str | None = None
    definition_hash: str | None = None


@dataclass(frozen=True)
class FaultPlanRecord:
    id: str
    run_id: str
    fault_type: str
    target_step_key: str
    checkpoint: str
    exit_code: int
    state: str
    created_at: str
    triggered_at: str | None


@dataclass(frozen=True)
class ProcessEvidenceRecord:
    tool_call_id: str
    pid: int
    pgid: int | None
    process_token: str
    argv_sha256: str
    started_at: str
    ended_at: str
    termination_confirmed: bool


@dataclass(frozen=True)
class ReconciliationEvidenceRecord:
    id: str
    tool_call_id: str
    decision: str
    evidence: dict
    created_at: str


@dataclass(frozen=True)
class EventRecord:
    id: int
    run_id: str
    sequence: int
    type: str
    payload: dict
    created_at: str
