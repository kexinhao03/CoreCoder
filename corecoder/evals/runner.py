"""Isolated execution loop for deterministic evaluation evidence."""

from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from corecoder.runtime import MetricsService, SQLiteStore, TraceService

from .faults import FaultInjector
from .models import EvaluationCase, EvaluationConfig, EvaluationResult, RuntimeExecution

Executor = Callable[[EvaluationCase, EvaluationConfig, Path, FaultInjector], RuntimeExecution | tuple[bool, bool | None]]


def runtime_evidence(store: SQLiteStore, run_id: str) -> dict:
    """Return read-only runtime facts for one completed or in-progress run."""
    trace = TraceService(store).export_run(run_id)
    metrics = MetricsService(store).summarize_run(run_id)
    return {
        "runtime_summary": {
            "run": trace["run"],
            "steps": trace["steps"],
            "tool_calls": trace["tool_calls"],
            "approvals": trace["approvals"],
        },
        "trace_integrity": trace["integrity"],
        "metrics_snapshot": asdict(metrics),
    }


class EvaluationRunner:
    def __init__(self, root: Path, configurations: Sequence[EvaluationConfig], execute: Executor) -> None:
        self._root = root
        self._configurations = tuple(configurations)
        self._execute = execute

    def run(self, suite: Sequence[EvaluationCase]) -> list[EvaluationResult]:
        results = []
        self._root.mkdir(parents=True, exist_ok=True)
        for case in suite:
            for config in self._configurations:
                for repetition in range(1, case.repeat_count + 1):
                    with TemporaryDirectory(dir=self._root) as directory:
                        try:
                            execution = self._execute(
                                case, config, Path(directory), FaultInjector(case.fault_schedule)
                            )
                            if isinstance(execution, RuntimeExecution):
                                task_succeeded = execution.task_succeeded
                                recovery_succeeded = execution.recovery_succeeded
                                evidence = runtime_evidence(execution.store, execution.run_id)
                            else:
                                task_succeeded, recovery_succeeded = execution
                                evidence = {}
                            error = None
                        except Exception as error_value:  # noqa: BLE001
                            task_succeeded, recovery_succeeded, error = None, None, str(error_value)
                            evidence = {}
                        results.append(EvaluationResult(
                            case.id, config.id, repetition, case.fault_schedule,
                            task_succeeded, recovery_succeeded, error, **evidence,
                        ))
        return results
