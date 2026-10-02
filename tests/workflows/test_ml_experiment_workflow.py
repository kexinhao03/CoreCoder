from __future__ import annotations

import json
import sqlite3

import pytest

from corecoder.runtime import ApprovalDecision, RunStatus, StepStatus, ToolCallStatus
from corecoder.workflows.ml_experiment.workflow import MLExperimentWorkflow


def test_workflow_stops_for_approval_then_completes_real_fixture(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)

    waiting = workflow.create()

    assert waiting.status is RunStatus.WAITING_APPROVAL
    steps = workflow.store.list_steps(waiting.id)
    assert [step.status for step in steps] == [
        StepStatus.SUCCEEDED,
        StepStatus.WAITING_APPROVAL,
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]
    calls = workflow.store.list_tool_calls(waiting.id)
    assert [call.status for call in calls] == [
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.WAITING_APPROVAL,
    ]
    experiment_call = calls[1]
    approval = workflow.store.get_approval_for_tool_call(experiment_call.id)
    assert approval is not None
    assert workflow.definition(waiting.id).experiment_id in approval.arguments_summary
    assert not workflow.definition(waiting.id).artifacts.effects_file.exists()

    workflow.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    completed = workflow.resume(waiting.id)

    assert completed.status == RunStatus.SUCCEEDED.value
    assert completed.report_available is True
    assert completed.pending_approval_id is None
    assert completed.pending_reconciliation_tool_call_id is None
    assert [step.status for step in workflow.store.list_steps(waiting.id)] == [
        StepStatus.SUCCEEDED,
        StepStatus.SUCCEEDED,
        StepStatus.SUCCEEDED,
        StepStatus.SUCCEEDED,
    ]
    completed_calls = workflow.store.list_tool_calls(waiting.id)
    assert completed_calls[1].id == experiment_call.id
    assert len(completed_calls) == 4
    extraction = json.loads(completed_calls[2].result_summary)
    effect = json.loads(completed_calls[3].result_summary)
    assert extraction["normalized_metrics"]["slope"] == 2.0
    assert extraction["normalized_metrics"]["intercept"] == 1.0
    assert effect["effect_count"] == 1
    assert effect["duplicate_effect"] is False
    assert len(
        workflow.definition(waiting.id).artifacts.effects_file.read_text().splitlines()
    ) == 1


def test_approval_denial_fails_workflow_atomically_without_artifacts(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    waiting = workflow.create()
    experiment_call = workflow.store.list_tool_calls(waiting.id)[1]
    approval = workflow.store.get_approval_for_tool_call(experiment_call.id)

    workflow.resolve_approval(approval.id, ApprovalDecision.DENY)
    denied = workflow.status(waiting.id)

    assert denied.status == RunStatus.FAILED.value
    denied_call = workflow.store.get_tool_call(experiment_call.id)
    assert denied_call.status is ToolCallStatus.FAILED
    assert denied_call.result_summary == "APPROVAL_DENIED"
    assert workflow.store.list_steps(waiting.id)[1].status is StepStatus.FAILED
    assert [event.type for event in workflow.store.list_events(waiting.id)[-4:]] == [
        "approval.resolved",
        "tool.failed",
        "step.failed",
        "run.failed",
    ]
    assert not workflow.definition(waiting.id).artifacts.artifact_dir.exists()


def test_approval_denial_rolls_back_every_state_when_audit_write_fails(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    waiting = workflow.create()
    experiment_call = workflow.store.list_tool_calls(waiting.id)[1]
    approval = workflow.store.get_approval_for_tool_call(experiment_call.id)
    events_before = workflow.store.list_events(waiting.id)
    with sqlite3.connect(workflow.store.path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_denial BEFORE INSERT ON events
               WHEN NEW.type = 'run.failed'
               BEGIN SELECT RAISE(ABORT, 'injected denial failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected denial failure"):
        workflow.resolve_approval(approval.id, ApprovalDecision.DENY)

    assert workflow.store.get_run(waiting.id).status is RunStatus.WAITING_APPROVAL
    assert workflow.store.get_tool_call(experiment_call.id).status is (
        ToolCallStatus.WAITING_APPROVAL
    )
    assert workflow.store.list_steps(waiting.id)[1].status is (
        StepStatus.WAITING_APPROVAL
    )
    assert workflow.store.get_approval(approval.id).decision is None
    assert workflow.store.list_events(waiting.id) == events_before


def test_resume_finishes_step_after_crash_between_tool_and_step_commit(
    tmp_path, monkeypatch
):
    workflow = MLExperimentWorkflow(tmp_path)
    waiting = workflow.create()
    approval_id = workflow.status(waiting.id).pending_approval_id
    workflow.resolve_approval(approval_id, ApprovalDecision.ALLOW_ONCE)
    experiment_step = workflow.store.list_steps(waiting.id)[1]
    transition_step = workflow.store.transition_step

    def crash_before_step_commit(step_id, status, event_type):
        if step_id == experiment_step.id and status is StepStatus.SUCCEEDED:
            raise SystemExit("injected crash after tool completion")
        return transition_step(step_id, status, event_type)

    monkeypatch.setattr(workflow.store, "transition_step", crash_before_step_commit)
    with pytest.raises(SystemExit, match="after tool completion"):
        workflow.resume(waiting.id)

    assert workflow.store.list_tool_calls(waiting.id)[1].status is (
        ToolCallStatus.SUCCEEDED
    )
    assert workflow.store.list_steps(waiting.id)[1].status is StepStatus.RUNNING
    restarted = MLExperimentWorkflow(tmp_path)
    completed = restarted.resume(waiting.id)

    assert completed.status == RunStatus.SUCCEEDED.value
    assert len(
        restarted.definition(waiting.id).artifacts.effects_file.read_text().splitlines()
    ) == 1


def test_resume_executes_approved_call_after_crash_before_step_waiting_commit(
    tmp_path, monkeypatch
):
    workflow = MLExperimentWorkflow(tmp_path)
    transition_step = workflow.store.transition_step

    def crash_before_waiting_commit(step_id, status, event_type):
        if status is StepStatus.WAITING_APPROVAL:
            raise SystemExit("injected crash after approval persistence")
        return transition_step(step_id, status, event_type)

    monkeypatch.setattr(workflow.store, "transition_step", crash_before_waiting_commit)
    with pytest.raises(SystemExit, match="after approval persistence"):
        workflow.create()

    [run] = workflow.store.list_runs()
    experiment_call = workflow.store.list_tool_calls(run.id)[1]
    approval = workflow.store.get_approval_for_tool_call(experiment_call.id)
    assert workflow.store.list_steps(run.id)[1].status is StepStatus.RUNNING
    restarted = MLExperimentWorkflow(tmp_path)
    restarted.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)

    completed = restarted.resume(run.id)

    assert completed.status == RunStatus.SUCCEEDED.value
    assert len(
        restarted.definition(run.id).artifacts.effects_file.read_text().splitlines()
    ) == 1


def test_normal_workflow_has_no_fault_plan(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    run = workflow.create()

    assert workflow.store.list_fault_plans(run.id) == []
