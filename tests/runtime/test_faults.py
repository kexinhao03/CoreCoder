from __future__ import annotations

from dataclasses import replace

import pytest

from corecoder.runtime import ExecutionKind, RiskLevel, SQLiteStore
from corecoder.runtime.processes import ProcessEvidence


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
