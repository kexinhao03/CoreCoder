from __future__ import annotations

import json

import pytest

from corecoder.runtime import (
    ApprovalDecision,
    ProcessEvidence,
    RunStatus,
    StepStatus,
    ToolCallStatus,
)
from corecoder.workflows.ml_experiment.reconcile import (
    MLReconciliationService,
    ReconciliationDecision,
)
from corecoder.workflows.ml_experiment.workflow import MLExperimentWorkflow


class InjectedExit(BaseException):
    pass


def _exit(code):
    raise InjectedExit(code)


def _interrupted_experiment(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path, exit_process=_exit)
    waiting = workflow.create(fault="experiment_after_effect")
    call = workflow.store.list_tool_calls(waiting.id)[1]
    approval = workflow.store.get_approval_for_tool_call(call.id)
    workflow.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    with pytest.raises(InjectedExit):
        workflow.resume(waiting.id)
    restarted = MLExperimentWorkflow(tmp_path)
    restarted.resume(waiting.id)
    return restarted, waiting.id, call.id


def test_completed_reconciliation_is_atomic_and_does_not_replay_effect(tmp_path):
    workflow, run_id, call_id = _interrupted_experiment(tmp_path)
    service = MLReconciliationService(workflow)

    reconciled = service.reconcile(call_id, ReconciliationDecision.COMPLETED)

    assert reconciled.decision is ReconciliationDecision.COMPLETED
    assert workflow.store.get_tool_call(call_id).status is ToolCallStatus.SUCCEEDED
    assert workflow.store.list_steps(run_id)[1].status is StepStatus.SUCCEEDED
    assert workflow.store.get_run(run_id).status is RunStatus.RUNNING
    assert [item.decision for item in workflow.store.list_reconciliation_evidence(call_id)] == [
        "completed"
    ]
    assert [event.type for event in workflow.store.list_events(run_id)][-3:] == [
        "tool.reconciled",
        "step.completed",
        "run.resumed",
    ]
    effects_before = workflow.definition(run_id).artifacts.effects_file.read_bytes()

    completed = workflow.resume(run_id)

    assert completed.status == RunStatus.SUCCEEDED.value
    assert workflow.definition(run_id).artifacts.effects_file.read_bytes() == effects_before
    assert len(effects_before.splitlines()) == 1


def test_damaged_artifact_records_unresolved_without_state_change(tmp_path):
    workflow, run_id, call_id = _interrupted_experiment(tmp_path)
    artifacts = workflow.definition(run_id).artifacts
    payload = json.loads(artifacts.metrics_file.read_text())
    payload["parameters"]["slope"] = 999.0
    artifacts.metrics_file.write_text(json.dumps(payload), encoding="utf-8")
    before = (
        workflow.store.get_run(run_id),
        workflow.store.get_tool_call(call_id),
        workflow.store.list_steps(run_id),
    )

    result = MLReconciliationService(workflow).reconcile(
        call_id, ReconciliationDecision.COMPLETED
    )

    assert result.decision is ReconciliationDecision.UNRESOLVED
    assert result.reason == "artifact_invalid"
    assert (
        workflow.store.get_run(run_id),
        workflow.store.get_tool_call(call_id),
        workflow.store.list_steps(run_id),
    ) == before
    [evidence] = workflow.store.list_reconciliation_evidence(call_id)
    assert evidence.decision == "unresolved"
    assert evidence.evidence["reason"] == "artifact_invalid"


def test_retry_requires_no_effect_and_creates_fresh_attempt_and_approval(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    waiting = workflow.create()
    call = workflow.store.list_tool_calls(waiting.id)[1]
    approval = workflow.store.get_approval_for_tool_call(call.id)
    workflow.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    workflow.store.transition_step(
        workflow.store.list_steps(waiting.id)[1].id, StepStatus.RUNNING, "step.resumed"
    )
    workflow.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    workflow.store.record_process_evidence(
        call.id,
        ProcessEvidence(
            pid=123,
            pgid=123,
            process_token="token",
            argv_sha256="a" * 64,
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:01+00:00",
            termination_confirmed=True,
        ),
    )
    workflow.store.transition_tool_call(
        call.id,
        ToolCallStatus.INTERRUPTED,
        "tool.interrupted",
        payload={"reason": "process_lost"},
    )

    result = MLReconciliationService(workflow).reconcile(
        call.id, ReconciliationDecision.RETRY
    )

    assert result.decision is ReconciliationDecision.RETRY
    assert result.new_tool_call_id is not None
    assert result.new_approval_id is not None
    retry = workflow.store.get_tool_call(result.new_tool_call_id)
    assert retry.retry_of == call.id
    assert retry.attempt == 2
    assert retry.status is ToolCallStatus.WAITING_APPROVAL
    assert workflow.store.get_tool_call(call.id).status is ToolCallStatus.INTERRUPTED
    new_approval = workflow.store.get_approval(result.new_approval_id)
    assert new_approval.tool_call_id == retry.id
    assert new_approval.id != approval.id
    assert workflow.store.get_run(waiting.id).status is RunStatus.WAITING_APPROVAL
    assert workflow.store.list_steps(waiting.id)[1].status is StepStatus.WAITING_APPROVAL

    workflow.store.resolve_approval(new_approval.id, ApprovalDecision.ALLOW_ONCE)
    completed = workflow.resume(waiting.id)

    assert completed.status == RunStatus.SUCCEEDED.value
    assert retry.status is ToolCallStatus.WAITING_APPROVAL
    assert workflow.store.get_tool_call(retry.id).status is ToolCallStatus.SUCCEEDED
    effects = workflow.definition(waiting.id).artifacts.effects_file.read_text().splitlines()
    assert len(effects) == 1


def test_unconfirmed_process_cannot_be_reconciled_completed(tmp_path):
    workflow, _run_id, call_id = _interrupted_experiment(tmp_path)
    with workflow.store._connect() as connection:
        connection.execute(
            "UPDATE process_evidence SET termination_confirmed = 0 WHERE tool_call_id = ?",
            (call_id,),
        )

    result = MLReconciliationService(workflow).reconcile(
        call_id, ReconciliationDecision.COMPLETED
    )

    assert result.decision is ReconciliationDecision.UNRESOLVED
    assert result.reason == "process_not_confirmed_terminated"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("temporary", "temporary_metrics_present"),
        ("duplicate_marker", "artifact_invalid"),
        ("missing_metrics", "artifact_invalid"),
        ("wrong_definition", "artifact_invalid"),
    ],
)
def test_completed_reconciliation_rejects_ambiguous_artifacts(
    tmp_path, mutation, reason
):
    workflow, run_id, call_id = _interrupted_experiment(tmp_path)
    artifacts = workflow.definition(run_id).artifacts
    if mutation == "temporary":
        artifacts.metrics_tmp_file.write_text("{}", encoding="utf-8")
    elif mutation == "duplicate_marker":
        with artifacts.effects_file.open("a", encoding="utf-8") as handle:
            handle.write(artifacts.effects_file.read_text().splitlines()[0] + "\n")
    elif mutation == "missing_metrics":
        artifacts.metrics_file.unlink()
    else:
        payload = json.loads(artifacts.metrics_file.read_text())
        payload["definition_hash"] = "f" * 64
        artifacts.metrics_file.write_text(json.dumps(payload), encoding="utf-8")

    result = MLReconciliationService(workflow).reconcile(
        call_id, ReconciliationDecision.COMPLETED
    )

    assert result.decision is ReconciliationDecision.UNRESOLVED
    assert result.reason == reason
    assert workflow.store.get_run(run_id).status is RunStatus.RECOVERABLE
    assert workflow.store.get_tool_call(call_id).status is ToolCallStatus.INTERRUPTED
