"""Deterministic scenarios that exercise production RuntimeExecutor paths."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from corecoder.reliagent import ReliAgentRuntime, TaskStep
from corecoder.runtime import (
    ApprovalDecision,
    ExecutionKind,
    FailureKind,
    RecoveryKind,
    RecoveryManager,
    RiskLevel,
    RunStatus,
    RuntimeExecutor,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
    ToolPolicy,
    ToolPolicyRegistry,
)
from corecoder.runtime.processes import ProcessResult, ProcessSpec

from .faults import EffectMarker, FaultInjected, FaultInjector
from .models import EvaluationCase, EvaluationConfig, RuntimeExecution


def _read_policy(config: EvaluationConfig, *, timeout: float = 2) -> ToolPolicy:
    return ToolPolicy(
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=timeout,
        max_attempts=config.max_attempts,
        idempotent=True,
        auto_retry=config.auto_retry,
        retryable_failures=frozenset({
            FailureKind.NONZERO_EXIT,
            FailureKind.TIMED_OUT,
            FailureKind.SPAWN_ERROR,
        }),
        output_limit=1_000,
    )


def _components(
    workdir: Path,
    config: EvaluationConfig,
    *,
    process_runner=None,
    timeout: float = 2,
):
    store = SQLiteStore(workdir / "runtime.sqlite")
    store.initialize()
    registry = ToolPolicyRegistry({"probe": _read_policy(config, timeout=timeout)})
    executor = RuntimeExecutor(store, registry, process_runner)
    return store, registry, executor, ReliAgentRuntime(store, executor)


def _fixed_task(
    workdir: Path,
    config: EvaluationConfig,
    argv: tuple[str, ...],
    *,
    process_runner=None,
    timeout: float = 2,
) -> tuple[SQLiteStore, object]:
    store, _registry, _executor, runtime = _components(
        workdir, config, process_runner=process_runner, timeout=timeout
    )
    run = runtime.run_task(
        goal="evaluation",
        workspace=workdir,
        steps=(TaskStep("probe", argv),),
    )
    return store, run


def _normal_success(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    store, run = _fixed_task(
        workdir,
        config,
        (sys.executable, "-c", "print('ok')"),
    )
    succeeded = run.status is RunStatus.SUCCEEDED
    return RuntimeExecution(
        succeeded,
        None,
        store,
        run.id,
        {"duplicate_effects": 0, "effect_count": 0, "fault_triggered": False},
        {"task_succeeded": succeeded},
    )


def _transient_then_success(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    runner = _TransientFailureRunner(fault, case.fault_schedule.point)
    store, run = _fixed_task(
        workdir,
        config,
        (sys.executable, "-c", "print('unused')"),
        process_runner=runner,
    )
    succeeded = run.status is RunStatus.SUCCEEDED
    expected = config.auto_retry
    return RuntimeExecution(
        succeeded,
        None,
        store,
        run.id,
        {
            "duplicate_effects": 0,
            "effect_count": 0,
            "fault_triggered": fault.triggered,
        },
        {
            "fault_triggered": fault.triggered,
            "task_outcome_matches_configuration": succeeded is expected,
        },
    )


def _trigger(fault: FaultInjector, point: str) -> None:
    try:
        fault.checkpoint(point)
    except FaultInjected:
        pass


def _injected_result(failure: FailureKind) -> ProcessResult:
    exit_code = 75 if failure is FailureKind.NONZERO_EXIT else None
    return ProcessResult(exit_code, "", failure.value, 0.01, failure, True)


class _TransientFailureRunner:
    def __init__(self, fault: FaultInjector, point: str) -> None:
        self.fault = fault
        self.point = point
        self.calls = 0

    def run(
        self, spec: ProcessSpec, cancellation_event: threading.Event
    ) -> ProcessResult:
        self.calls += 1
        if self.calls == 1:
            try:
                self.fault.checkpoint(self.point)
            except FaultInjected:
                return _injected_result(FailureKind.NONZERO_EXIT)
            raise AssertionError("scheduled transient fault did not fire")
        return ProcessResult(0, "ok", "", 0.01, None, True)


class _PersistentFailureRunner:
    def __init__(
        self, fault: FaultInjector, point: str, failure: FailureKind
    ) -> None:
        self.fault = fault
        self.point = point
        self.failure = failure
        self.calls = 0

    def run(
        self, spec: ProcessSpec, cancellation_event: threading.Event
    ) -> ProcessResult:
        self.calls += 1
        if self.calls == 1:
            try:
                self.fault.checkpoint(self.point)
            except FaultInjected:
                pass
            else:
                raise AssertionError("scheduled persistent fault did not fire")
        return _injected_result(self.failure)


def _retry_exhausted(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    runner = _PersistentFailureRunner(
        fault, case.fault_schedule.point, FailureKind.NONZERO_EXIT
    )
    store, run = _fixed_task(
        workdir,
        config,
        (sys.executable, "-c", "print('unused')"),
        process_runner=runner,
    )
    calls = store.list_tool_calls(run.id)
    expected_attempts = config.max_attempts if config.auto_retry else 1
    return RuntimeExecution(
        False,
        None,
        store,
        run.id,
        {"duplicate_effects": 0, "effect_count": 0, "fault_triggered": fault.triggered},
        {
            "attempt_budget_enforced": len(calls) == expected_attempts,
            "fault_triggered": fault.triggered,
            "run_failed": run.status is RunStatus.FAILED,
        },
    )


def _timeout(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    runner = _PersistentFailureRunner(
        fault, case.fault_schedule.point, FailureKind.TIMED_OUT
    )
    store, run = _fixed_task(
        workdir,
        config,
        (sys.executable, "-c", "print('unused')"),
        process_runner=runner,
        timeout=0.03,
    )
    calls = store.list_tool_calls(run.id)
    expected_attempts = config.max_attempts if config.auto_retry else 1
    return RuntimeExecution(
        False,
        None,
        store,
        run.id,
        {"duplicate_effects": 0, "effect_count": 0, "fault_triggered": fault.triggered},
        {
            "all_attempts_timed_out": all(
                call.status is ToolCallStatus.TIMED_OUT for call in calls
            ),
            "attempt_budget_enforced": len(calls) == expected_attempts,
            "fault_triggered": fault.triggered,
        },
    )


def _effect_policy() -> ToolPolicy:
    return ToolPolicy(
        risk_level=RiskLevel.EXTERNAL_EFFECT,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=2,
        max_attempts=1,
        idempotent=False,
        auto_retry=False,
        retryable_failures=frozenset(),
        output_limit=1_000,
    )


def _effect_components(workdir: Path, process_runner=None):
    store = SQLiteStore(workdir / "runtime.sqlite")
    store.initialize()
    registry = ToolPolicyRegistry({"effect": _effect_policy()})
    executor = RuntimeExecutor(store, registry, process_runner)
    return store, registry, executor, ReliAgentRuntime(store, executor)


def _approval_denied(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    marker = workdir / "denied-effect.txt"
    store, _registry, _executor, runtime = _effect_components(workdir)
    steps = (TaskStep(
        "effect",
        (sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),
    ),)
    run = runtime.run_task(goal="approval denial", workspace=workdir, steps=steps)
    _trigger(fault, case.fault_schedule.point)
    approval, = store.list_approvals(run.id)
    store.resolve_approval(approval.id, ApprovalDecision.DENY)
    final = runtime.resume(run.id, steps=steps).run
    return RuntimeExecution(
        False,
        None,
        store,
        run.id,
        {"duplicate_effects": 0, "effect_count": int(marker.exists()), "fault_triggered": fault.triggered},
        {
            "effect_not_executed": not marker.exists(),
            "fault_triggered": fault.triggered,
            "run_cancelled": final.status is RunStatus.CANCELLED,
        },
    )


def _crash_after_step(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    store, _registry, executor, runtime = _components(workdir, config)
    run = store.create_run(
        goal="crash after step",
        workflow="evaluation",
        workspace=workdir,
        model="deterministic",
        prompt_version="eval-v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    step = store.create_step(run.id, sequence=1, title="probe")
    store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    result = executor.submit_subprocess(
        run.id, "probe", (sys.executable, "-c", "print('done')"), step_id=step.id
    )
    store.transition_step(step.id, StepStatus.SUCCEEDED, "step.completed")
    _trigger(fault, case.fault_schedule.point)
    if config.recovery_enabled:
        final = runtime.resume(
            run.id,
            steps=(TaskStep("probe", (sys.executable, "-c", "print('done')")),),
        ).run
    else:
        final = store.get_run(run.id)
    calls = store.list_tool_calls(run.id)
    succeeded = final.status is RunStatus.SUCCEEDED
    return RuntimeExecution(
        succeeded,
        succeeded,
        store,
        run.id,
        {"duplicate_effects": 0, "effect_count": 0, "fault_triggered": fault.triggered},
        {
            "completed_call_not_replayed": len(calls) == 1,
            "fault_triggered": fault.triggered,
            "recovery_matches_configuration": succeeded is config.recovery_enabled,
            "tool_succeeded_before_crash": result.call.status is ToolCallStatus.SUCCEEDED,
        },
    )


class _InterruptedEffectRunner:
    def __init__(self, fault: FaultInjector, marker: EffectMarker) -> None:
        self.fault = fault
        self.marker = marker

    def run(
        self, spec: ProcessSpec, cancellation_event: threading.Event
    ) -> ProcessResult:
        self.marker.record()
        try:
            self.fault.checkpoint("after_effect")
        except FaultInjected:
            return ProcessResult(
                None,
                "",
                "termination unknown",
                0.01,
                FailureKind.TERMINATION_UNKNOWN,
                False,
            )
        raise AssertionError("scheduled interruption fault did not fire")


def _high_risk_interrupted(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    marker = EffectMarker(workdir / "effect-marker.txt")
    runner = _InterruptedEffectRunner(fault, marker)
    store, registry, executor, _runtime = _effect_components(workdir, runner)
    run = store.create_run(
        goal="high-risk interruption", workflow="evaluation", workspace=workdir,
        model="deterministic", prompt_version="eval-v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    step = store.create_step(run.id, sequence=1, title="effect")
    store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    pending = executor.submit_subprocess(
        run.id, "effect", (sys.executable, "-c", "print('effect')"), step_id=step.id
    )
    store.resolve_approval(pending.approval.id, ApprovalDecision.ALLOW_ONCE)
    result = executor.execute_approved_subprocess(pending.call.id)
    assert result.call.status is ToolCallStatus.INTERRUPTED
    manager = RecoveryManager(store, registry)
    candidate, = manager.scan()
    human_required = candidate.kind is RecoveryKind.HUMAN_REQUIRED
    effect_count = marker.count
    return RuntimeExecution(
        False,
        False,
        store,
        run.id,
        {
            "duplicate_effects": marker.duplicate_count,
            "effect_count": effect_count,
            "fault_triggered": fault.triggered,
        },
        {
            "fault_triggered": fault.triggered,
            "human_reconciliation_required": human_required,
            "no_automatic_replay": len(store.list_tool_calls(run.id)) == 1,
            "single_effect": effect_count == 1,
        },
    )


class _RecoveryRunner:
    def __init__(self, fault: FaultInjector, marker: EffectMarker) -> None:
        self.fault = fault
        self.marker = marker
        self.calls = 0

    def run(
        self, spec: ProcessSpec, cancellation_event: threading.Event
    ) -> ProcessResult:
        self.calls += 1
        if self.calls == 1:
            self.fault.checkpoint("process_lost")
            raise AssertionError("process_lost fault did not fire")
        self.marker.record()
        return ProcessResult(0, "recovered", "", 0.01, None, True)


def _resume_safe_recovery(
    store: SQLiteStore,
    registry: ToolPolicyRegistry,
    executor: RuntimeExecutor,
    run_id: str,
    step_id: str,
) -> bool:
    """Perform one real resume request; terminal runs are an inert no-op."""
    manager = RecoveryManager(store, registry)
    candidates = [
        candidate for candidate in manager.scan() if candidate.run.id == run_id
    ]
    if not candidates:
        return False
    candidate, = candidates
    if candidate.kind is not RecoveryKind.RETRY_ALLOWED:
        return False
    retry = manager.resume_retry(candidate)
    resumed = executor.execute_recovery_subprocess(retry.id)
    if resumed.call.status is not ToolCallStatus.SUCCEEDED:
        return False
    store.transition_step(step_id, StepStatus.SUCCEEDED, "step.completed")
    store.transition_run(run_id, RunStatus.SUCCEEDED, "run.completed")
    return True


def _repeated_resume(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    marker = EffectMarker(workdir / "effect-marker.txt")
    process_runner = _RecoveryRunner(fault, marker)
    store, registry, executor, _runtime = _components(
        workdir, config, process_runner=process_runner
    )
    run = store.create_run(
        goal="repeated resume",
        workflow="evaluation",
        workspace=workdir,
        model="deterministic",
        prompt_version="eval-v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    step = store.create_step(run.id, sequence=1, title="probe")
    store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    try:
        executor.submit_subprocess(
            run.id,
            "probe",
            (sys.executable, "-c", "print('recovery')"),
            step_id=step.id,
        )
    except FaultInjected:
        pass
    assert store.list_tool_calls(run.id)[0].status is ToolCallStatus.RUNNING

    recovery_succeeded = False
    resume_count = 0
    calls_after_first = len(store.list_tool_calls(run.id))
    calls_after_second = calls_after_first
    events_unchanged = True
    if config.recovery_enabled:
        recovery_succeeded = _resume_safe_recovery(
            store, registry, executor, run.id, step.id
        )
        resume_count = 1
        calls_after_first = len(store.list_tool_calls(run.id))
        events_after_first = store.list_events(run.id)
        second_recovered = _resume_safe_recovery(
            store, registry, executor, run.id, step.id
        )
        resume_count += 1
        calls_after_second = len(store.list_tool_calls(run.id))
        events_unchanged = store.list_events(run.id) == events_after_first
        assert second_recovered is False

    effect_count = marker.count
    expected_count = 1 if config.recovery_enabled else 0
    return RuntimeExecution(
        recovery_succeeded,
        recovery_succeeded,
        store,
        run.id,
        {
            "duplicate_effects": marker.duplicate_count,
            "effect_count": effect_count,
            "events_unchanged_after_second_resume": events_unchanged,
            "fault_triggered": fault.triggered,
            "resume_count": resume_count,
            "tool_calls_after_first_resume": calls_after_first,
            "tool_calls_after_second_resume": calls_after_second,
        },
        {
            "effect_count_matches_configuration": effect_count == expected_count,
            "fault_triggered": fault.triggered,
            "no_duplicate_effect": effect_count <= 1,
            "recovery_matches_configuration": (
                recovery_succeeded is config.recovery_enabled
            ),
            "second_resume_inert": events_unchanged
            and calls_after_second == calls_after_first,
        },
    )


def _effect_persist_failure(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    marker = EffectMarker(workdir / "effect-marker.txt")
    store, registry, executor, _runtime = _effect_components(workdir)
    run = store.create_run(
        goal="effect persistence failure", workflow="evaluation", workspace=workdir,
        model="deterministic", prompt_version="eval-v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    step = store.create_step(run.id, sequence=1, title="effect")
    store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    pending = executor.submit_subprocess(
        run.id,
        "effect",
        (
            sys.executable,
            "-c",
            f"open({str(marker.path)!r}, 'a').write('effect\\n')",
        ),
        step_id=step.id,
    )
    store.resolve_approval(pending.approval.id, ApprovalDecision.ALLOW_ONCE)
    try:
        fault.checkpoint(case.fault_schedule.point)
    except FaultInjected:
        with sqlite3.connect(store.path) as connection:
            connection.executescript("""
                CREATE TRIGGER fail_eval_completion
                BEFORE INSERT ON events
                WHEN NEW.type = 'tool.completed'
                BEGIN
                    SELECT RAISE(ABORT, 'injected completion persistence failure');
                END;
            """)
    else:
        raise AssertionError("scheduled persistence fault did not fire")
    try:
        executor.execute_approved_subprocess(pending.call.id)
    except sqlite3.IntegrityError:
        pass
    manager = RecoveryManager(store, registry)
    candidate, = manager.scan()
    human_required = candidate.kind is RecoveryKind.HUMAN_REQUIRED
    effect_count = marker.count
    return RuntimeExecution(
        False,
        False,
        store,
        run.id,
        {
            "duplicate_effects": marker.duplicate_count,
            "effect_count": effect_count,
            "fault_triggered": fault.triggered,
        },
        {
            "fault_triggered": fault.triggered,
            "human_reconciliation_required": human_required,
            "no_replay_after_persist_failure": len(store.list_tool_calls(run.id)) == 1,
            "single_effect": effect_count == 1,
        },
    )


class _CancellableRunner:
    def __init__(self) -> None:
        self.started = threading.Event()

    def run(
        self, spec: ProcessSpec, cancellation_event: threading.Event
    ) -> ProcessResult:
        self.started.set()
        deadline = time.monotonic() + 2
        while not cancellation_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        failure = FailureKind.CANCELLED if cancellation_event.is_set() else FailureKind.TIMED_OUT
        return ProcessResult(None, "", "", 0.01, failure, True)


def _cancel_run(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    runner = _CancellableRunner()
    store, _registry, executor, _runtime = _components(
        workdir, config, process_runner=runner
    )
    run = store.create_run(
        goal="cancel", workflow="evaluation", workspace=workdir,
        model="deterministic", prompt_version="eval-v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    step = store.create_step(run.id, sequence=1, title="probe")
    store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    results = []
    worker = threading.Thread(target=lambda: results.append(executor.submit_subprocess(
        run.id, "probe", (sys.executable, "-c", "print('unused')"), step_id=step.id
    )))
    worker.start()
    if not runner.started.wait(timeout=1):
        raise RuntimeError("cancellation scenario did not start")
    _trigger(fault, case.fault_schedule.point)
    executor.cancel_run(run.id)
    worker.join(timeout=2)
    if worker.is_alive():
        raise RuntimeError("cancellation scenario did not stop")
    store.transition_step(step.id, StepStatus.CANCELLED, "step.cancelled")
    call = store.list_tool_calls(run.id)[0]
    return RuntimeExecution(
        False,
        None,
        store,
        run.id,
        {"duplicate_effects": 0, "effect_count": 0, "fault_triggered": fault.triggered},
        {
            "call_cancelled": call.status is ToolCallStatus.CANCELLED,
            "fault_triggered": fault.triggered,
            "run_cancelled": store.get_run(run.id).status is RunStatus.CANCELLED,
            "worker_stopped": not worker.is_alive(),
        },
    )


_SCENARIOS: dict[str, Callable[..., RuntimeExecution]] = {
    "normal_success": _normal_success,
    "transient_then_success": _transient_then_success,
    "retry_exhausted": _retry_exhausted,
    "timeout": _timeout,
    "approval_denied": _approval_denied,
    "crash_after_step": _crash_after_step,
    "high_risk_interrupted": _high_risk_interrupted,
    "repeated_resume": _repeated_resume,
    "effect_persist_failure": _effect_persist_failure,
    "cancel_run": _cancel_run,
}


def execute_scenario(
    case: EvaluationCase,
    config: EvaluationConfig,
    workdir: Path,
    fault: FaultInjector,
) -> RuntimeExecution:
    try:
        scenario = _SCENARIOS[case.scenario]
    except KeyError as error:
        raise ValueError(f"unknown evaluation scenario: {case.scenario}") from error
    return scenario(case, config, workdir, fault)
