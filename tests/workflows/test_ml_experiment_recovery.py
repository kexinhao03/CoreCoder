from __future__ import annotations

import pytest

from corecoder.runtime import ApprovalDecision, RunStatus, StepStatus, ToolCallStatus
from corecoder.workflows.ml_experiment.workflow import MLExperimentWorkflow


class InjectedExit(BaseException):
    def __init__(self, code):
        self.code = code


def _exit(code):
    raise InjectedExit(code)


def test_environment_process_loss_recovers_with_retry_and_no_effect(tmp_path):
    crashing = MLExperimentWorkflow(tmp_path, exit_process=_exit)
    with pytest.raises(InjectedExit) as raised:
        crashing.create(fault="environment_after_start")
    assert raised.value.code == 86
    [run] = crashing.store.list_runs()

    restarted = MLExperimentWorkflow(tmp_path)
    status = restarted.resume(run.id)

    assert status.status == RunStatus.WAITING_APPROVAL.value
    assert status.pending_approval_id is not None
    calls = restarted.store.list_tool_calls(run.id)
    environment_calls = [
        call for call in calls if call.tool_name.endswith("environment_check")
    ]
    assert len(environment_calls) == 2
    assert environment_calls[0].status is ToolCallStatus.FAILED
    assert environment_calls[1].retry_of == environment_calls[0].id
    assert environment_calls[1].status is ToolCallStatus.SUCCEEDED
    assert restarted.store.list_steps(run.id)[0].status is StepStatus.SUCCEEDED
    assert restarted.store.list_steps(run.id)[0].attempt_count == 2
    assert not restarted.definition(run.id).artifacts.effects_file.exists()


def test_experiment_process_loss_requires_reconciliation_without_retry(tmp_path):
    crashing = MLExperimentWorkflow(tmp_path, exit_process=_exit)
    waiting = crashing.create(fault="experiment_after_effect")
    experiment = crashing.store.list_tool_calls(waiting.id)[1]
    approval = crashing.store.get_approval_for_tool_call(experiment.id)
    crashing.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    with pytest.raises(InjectedExit) as raised:
        crashing.resume(waiting.id)
    assert raised.value.code == 87

    restarted = MLExperimentWorkflow(tmp_path)
    status = restarted.resume(waiting.id)

    assert status.status == RunStatus.RECOVERABLE.value
    assert status.pending_reconciliation_tool_call_id == experiment.id
    assert status.next_action == "reconcile"
    assert restarted.store.get_tool_call(experiment.id).status is ToolCallStatus.INTERRUPTED
    assert restarted.store.list_steps(waiting.id)[1].status is StepStatus.RUNNING
    assert restarted.store.list_steps(waiting.id)[2].status is StepStatus.PENDING
    assert len(
        [call for call in restarted.store.list_tool_calls(waiting.id) if call.retry_of]
    ) == 0
    assert restarted.definition(waiting.id).artifacts.metrics_file.exists()
    assert restarted.definition(waiting.id).artifacts.effects_file.exists()


def test_successful_repeated_resume_adds_no_facts_or_effects(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    waiting = workflow.create()
    experiment = workflow.store.list_tool_calls(waiting.id)[1]
    approval = workflow.store.get_approval_for_tool_call(experiment.id)
    workflow.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    workflow.resume(waiting.id)
    before = (
        workflow.store.list_events(waiting.id),
        workflow.store.list_tool_calls(waiting.id),
        workflow.definition(waiting.id).artifacts.effects_file.read_bytes(),
        workflow.definition(waiting.id).artifacts.metrics_file.read_bytes(),
    )

    first = workflow.resume(waiting.id)
    second = workflow.resume(waiting.id)

    assert first == second
    assert (
        workflow.store.list_events(waiting.id),
        workflow.store.list_tool_calls(waiting.id),
        workflow.definition(waiting.id).artifacts.effects_file.read_bytes(),
        workflow.definition(waiting.id).artifacts.metrics_file.read_bytes(),
    ) == before
