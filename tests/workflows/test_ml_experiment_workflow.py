from __future__ import annotations

import json

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

    workflow.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
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


def test_approval_denial_cancels_without_experiment_artifacts(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    waiting = workflow.create()
    experiment_call = workflow.store.list_tool_calls(waiting.id)[1]
    approval = workflow.store.get_approval_for_tool_call(experiment_call.id)

    workflow.store.resolve_approval(approval.id, ApprovalDecision.DENY)
    denied = workflow.resume(waiting.id)

    assert denied.status == RunStatus.CANCELLED.value
    assert workflow.store.get_tool_call(experiment_call.id).status is ToolCallStatus.CANCELLED
    assert workflow.store.list_steps(waiting.id)[1].status is StepStatus.CANCELLED
    assert not workflow.definition(waiting.id).artifacts.artifact_dir.exists()


def test_normal_workflow_has_no_fault_plan(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    run = workflow.create()

    assert workflow.store.list_fault_plans(run.id) == []
