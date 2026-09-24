"""Isolated execution loop for deterministic evaluation evidence."""

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from corecoder.runtime import MetricsService, SQLiteStore, TraceService

from .faults import FaultInjector
from .models import EvaluationCase, EvaluationConfig, EvaluationResult
from .scenarios import execute_scenario


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
            "events": trace["events"],
            "instrumentation_availability": trace[
                "instrumentation_availability"
            ],
        },
        "trace_integrity": trace["integrity"],
        "metrics_snapshot": asdict(metrics),
    }


class EvaluationRunner:
    def __init__(
        self, root: Path, configurations: Sequence[EvaluationConfig]
    ) -> None:
        self._root = root
        self._configurations = tuple(configurations)

    def run(self, suite: Sequence[EvaluationCase]) -> list[EvaluationResult]:
        results = []
        self._root.mkdir(parents=True, exist_ok=True)
        for case in suite:
            for config in self._configurations:
                for repetition in range(1, case.repeat_count + 1):
                    with TemporaryDirectory(dir=self._root) as directory:
                        try:
                            execution = execute_scenario(
                                case, config, Path(directory), FaultInjector(case.fault_schedule)
                            )
                            task_succeeded = execution.task_succeeded
                            recovery_succeeded = execution.recovery_succeeded
                            evidence = runtime_evidence(execution.store, execution.run_id)
                            evidence["effect_observations"] = execution.effect_observations
                            evidence["assertions"] = execution.assertions
                            passed = all(execution.assertions.values())
                            error = None
                        except Exception as error_value:  # noqa: BLE001
                            task_succeeded = recovery_succeeded = passed = None
                            error = str(error_value)
                            evidence = {}
                        results.append(EvaluationResult(
                            case_id=case.id,
                            case_description=case.description,
                            scenario=case.scenario,
                            config_id=config.id,
                            repetition=repetition,
                            input_id=case.input_id,
                            fault_schedule=case.fault_schedule,
                            passed=passed,
                            task_succeeded=task_succeeded,
                            recovery_succeeded=recovery_succeeded,
                            error=error,
                            **evidence,
                        ))
        return results
