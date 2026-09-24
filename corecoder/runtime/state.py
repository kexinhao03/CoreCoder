
"""Runtime-owned state machines for durable agent execution."""

from enum import Enum


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    RECOVERABLE = "recoverable"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(str, Enum):
    CREATED = "created"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    EXTERNAL_EFFECT = "external_effect"


class ExecutionKind(str, Enum):
    IN_PROCESS = "in_process"
    SUBPROCESS = "subprocess"


class InvalidTransition(ValueError):
    """Raised when a runtime entity attempts an illegal state change."""


_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({
        RunStatus.WAITING_APPROVAL,
        RunStatus.PAUSED,
        RunStatus.RECOVERABLE,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.WAITING_APPROVAL: frozenset({
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.RECOVERABLE,
        RunStatus.CANCELLED,
    }),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RECOVERABLE: frozenset({
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

_TOOL_CALL_TRANSITIONS: dict[ToolCallStatus, frozenset[ToolCallStatus]] = {
    ToolCallStatus.CREATED: frozenset({
        ToolCallStatus.WAITING_APPROVAL,
        ToolCallStatus.RUNNING,
        ToolCallStatus.CANCELLED,
    }),
    ToolCallStatus.WAITING_APPROVAL: frozenset({
        ToolCallStatus.RUNNING,
        ToolCallStatus.CANCELLED,
    }),
    ToolCallStatus.RUNNING: frozenset({
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.TIMED_OUT,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.INTERRUPTED,
    }),
    ToolCallStatus.INTERRUPTED: frozenset({
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.CANCELLED,
    }),
    ToolCallStatus.SUCCEEDED: frozenset(),
    ToolCallStatus.FAILED: frozenset(),
    ToolCallStatus.TIMED_OUT: frozenset(),
    ToolCallStatus.CANCELLED: frozenset(),
}

_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.RUNNING, StepStatus.CANCELLED, StepStatus.SKIPPED}),
    StepStatus.RUNNING: frozenset({StepStatus.WAITING_APPROVAL, StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}),
    StepStatus.WAITING_APPROVAL: frozenset({StepStatus.RUNNING, StepStatus.CANCELLED}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Validate a Run state change without mutating any state."""
    if target not in _RUN_TRANSITIONS[current]:
        raise InvalidTransition(
            f"invalid run transition: {current.value} -> {target.value}"
        )


def ensure_tool_call_transition(
    current: ToolCallStatus,
    target: ToolCallStatus,
) -> None:
    """Validate a ToolCall state change without mutating any state."""
    if target not in _TOOL_CALL_TRANSITIONS[current]:
        raise InvalidTransition(
            f"invalid tool call transition: {current.value} -> {target.value}"
        )


def ensure_step_transition(current: StepStatus, target: StepStatus) -> None:
    if target not in _STEP_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid step transition: {current.value} -> {target.value}")
