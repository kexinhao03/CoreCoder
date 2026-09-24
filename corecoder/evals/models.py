"""Serializable contracts for deterministic evaluation repetitions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultSchedule:
    point: str


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    description: str
    task_spec: str
    fault_schedule: FaultSchedule
    repeat_count: int


@dataclass(frozen=True)
class EvaluationConfig:
    id: str
    persistence_enabled: bool
    recovery_enabled: bool


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    config_id: str
    repetition: int
    fault_schedule: FaultSchedule
    task_succeeded: bool | None
    recovery_succeeded: bool | None
    error: str | None
    runtime_summary: dict | None = None
    trace_integrity: dict | None = None
    metrics_snapshot: dict | None = None
    effect_observations: dict | None = None
    assertions: dict | None = None


@dataclass(frozen=True)
class RuntimeExecution:
    """Result returned by an evaluator that actually invoked the runtime."""

    task_succeeded: bool
    recovery_succeeded: bool | None
    store: object
    run_id: str
