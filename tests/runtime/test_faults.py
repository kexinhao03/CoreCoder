from __future__ import annotations

from dataclasses import replace

import pytest

from corecoder.runtime import (
    ApprovalDecision,
    ExecutionKind,
    RiskLevel,
    RuntimeExecutor,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
)
from corecoder.runtime.faults import RuntimeFaultInjector
from corecoder.runtime.policies import ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.processes import ProcessEvidence, ProcessResult
from corecoder.runtime.state import RunStatus


@pytest.fixture
def store(tmp_path):
    result = SQLiteStore(tmp_path / "runtime.sqlite")
    result.initialize()
    result.create_run(
        goal="deterministic experiment",
        workflow="ml_experiment",
        workspace=tmp_path,
        model="deterministic",
        prompt_version="ml-experiment-v1",
        run_id="run-1",
    )
    return result


def test_fault_plan_triggers_once_with_atomic_event(store):
    plan = store.create_fault_plan(
        run_id="run-1",
        fault_type="process_loss",
        target_step_key="environment_check",
        checkpoint="environment_after_start",
        exit_code=86,
        fault_plan_id="fault-1",
    )

    assert plan.state == "armed"
    assert plan.triggered_at is None
    triggered = store.trigger_fault_plan(
        "run-1", "environment_check", "environment_after_start"
    )
    assert triggered is not None
    assert triggered.id == "fault-1"
    assert triggered.state == "triggered"
    assert triggered.triggered_at is not None
    events_after_first = store.list_events("run-1")
    assert events_after_first[-1].type == "fault.injected"
    assert events_after_first[-1].payload == {
        "checkpoint": "environment_after_start",
        "exit_code": 86,
        "fault_plan_id": "fault-1",
        "fault_type": "process_loss",
        "step_key": "environment_check",
    }

    assert (
        store.trigger_fault_plan(
            "run-1", "environment_check", "environment_after_start"
        )
        is None
    )
    assert store.list_events("run-1") == events_after_first
    assert store.list_fault_plans("run-1") == [triggered]


def test_process_evidence_round_trips_and_conflicts_are_rejected(store):
    call = store.create_tool_call(
        run_id="run-1",
        tool_name="ml_experiment.environment_check",
        arguments={"argv": ["python", "check.py"]},
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True,
        idempotency_key="environment-check",
        timeout_seconds=30,
        tool_call_id="call-1",
    )
    evidence = ProcessEvidence(
        pid=123,
        pgid=123,
        process_token="token-1",
        argv_sha256="b" * 64,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        termination_confirmed=True,
    )

    recorded = store.record_process_evidence(call.id, evidence)

    assert recorded.tool_call_id == call.id
    assert recorded.pid == 123
    assert recorded.pgid == 123
    assert recorded.process_token == "token-1"
    assert recorded.termination_confirmed is True
    assert store.get_process_evidence(call.id) == recorded
    assert store.record_process_evidence(call.id, evidence) == recorded
    with pytest.raises(ValueError, match="conflicting process evidence"):
        store.record_process_evidence(call.id, replace(evidence, pid=999))


class InjectedExit(BaseException):
    def __init__(self, code):
        self.code = code


class EvidenceRunner:
    def __init__(self):
        self.calls = []

    def run(self, spec, cancellation):
        self.calls.append(spec)
        return ProcessResult(
            0,
            "ok",
            "",
            0.01,
            None,
            True,
            ProcessEvidence(
                pid=123,
                pgid=123,
                process_token="token-1",
                argv_sha256="c" * 64,
                started_at="2026-01-01T00:00:00+00:00",
                ended_at="2026-01-01T00:00:01+00:00",
                termination_confirmed=True,
            ),
        )


def _policy(risk_level):
    return ToolPolicy(
        risk_level=risk_level,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=30,
        max_attempts=1,
        idempotent=risk_level is RiskLevel.READ_ONLY,
        auto_retry=False,
        retryable_failures=frozenset(),
        output_limit=1000,
    )


def _running_store_with_step(store, step_key):
    store.transition_run("run-1", RunStatus.RUNNING, "run.started")
    step = store.create_step(
        "run-1",
        sequence=1,
        title=step_key,
        step_key=step_key,
        definition_version="1",
        definition_hash="d" * 64,
        step_id="step-1",
    )
    store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    return step


def test_environment_checkpoint_exits_after_started_before_process(store):
    step = _running_store_with_step(store, "environment_check")
    store.create_fault_plan(
        run_id="run-1",
        fault_type="process_loss",
        target_step_key="environment_check",
        checkpoint="environment_after_start",
        exit_code=86,
    )
    runner = EvidenceRunner()
    injector = RuntimeFaultInjector(
        store, exit_process=lambda code: (_ for _ in ()).throw(InjectedExit(code))
    )
    executor = RuntimeExecutor(
        store,
        ToolPolicyRegistry({"environment": _policy(RiskLevel.READ_ONLY)}),
        runner,
        fault_injector=injector,
    )

    with pytest.raises(InjectedExit) as raised:
        executor.submit_subprocess(
            "run-1", "environment", ("check",), step_id=step.id, tool_call_id="call-1"
        )

    assert raised.value.code == 86
    assert runner.calls == []
    assert store.get_tool_call("call-1").status is ToolCallStatus.RUNNING
    assert [event.type for event in store.list_events("run-1")][-2:] == [
        "tool.started",
        "fault.injected",
    ]
    injector.checkpoint(store.get_tool_call("call-1"), "environment_after_start")


def test_experiment_checkpoint_persists_evidence_before_exit(store):
    step = _running_store_with_step(store, "run_experiment")
    store.create_fault_plan(
        run_id="run-1",
        fault_type="process_loss",
        target_step_key="run_experiment",
        checkpoint="experiment_after_effect",
        exit_code=87,
    )
    runner = EvidenceRunner()
    injector = RuntimeFaultInjector(
        store, exit_process=lambda code: (_ for _ in ()).throw(InjectedExit(code))
    )
    executor = RuntimeExecutor(
        store,
        ToolPolicyRegistry({"experiment": _policy(RiskLevel.EXTERNAL_EFFECT)}),
        runner,
        fault_injector=injector,
    )
    pending = executor.submit_subprocess(
        "run-1",
        "experiment",
        ("train",),
        step_id=step.id,
        tool_call_id="call-1",
        approval_id="approval-1",
    )
    store.resolve_approval(pending.approval.id, ApprovalDecision.ALLOW_ONCE)

    with pytest.raises(InjectedExit) as raised:
        executor.execute_approved_subprocess("call-1")

    assert raised.value.code == 87
    assert len(runner.calls) == 1
    assert store.get_process_evidence("call-1") is not None
    assert store.get_tool_call("call-1").status is ToolCallStatus.RUNNING
    assert [event.type for event in store.list_events("run-1")][-2:] == [
        "tool.started",
        "fault.injected",
    ]
    assert not any(event.type == "tool.completed" for event in store.list_events("run-1"))
