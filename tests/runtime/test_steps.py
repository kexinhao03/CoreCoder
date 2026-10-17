"""Durable, ordered workflow Steps."""

import pytest

from corecoder.runtime.state import ExecutionKind, InvalidTransition, RiskLevel, StepStatus, ensure_step_transition


def test_succeeded_step_cannot_return_to_running():
    with pytest.raises(InvalidTransition, match="succeeded -> running"):
        ensure_step_transition(StepStatus.SUCCEEDED, StepStatus.RUNNING)


def test_step_transition_commits_state_and_event_atomically(running_store):
    step = running_store.create_step("run-1", sequence=1, title="inspect")

    updated = running_store.transition_step(
        step.id,
        StepStatus.RUNNING,
        "step.started",
    )

    assert updated.status is StepStatus.RUNNING
    assert running_store.list_events("run-1")[-1].type == "step.started"


def test_step_attempt_count_increments_only_when_pending_step_starts(running_store):
    step = running_store.create_step("run-1", sequence=1, title="mutate")

    started = running_store.transition_step(
        step.id,
        StepStatus.RUNNING,
        "step.started",
    )
    waiting = running_store.transition_step(
        step.id,
        StepStatus.WAITING_APPROVAL,
        "step.waiting_approval",
    )
    resumed = running_store.transition_step(
        step.id,
        StepStatus.RUNNING,
        "step.resumed",
    )

    assert started.attempt_count == 1
    assert waiting.attempt_count == 1
    assert resumed.attempt_count == 1


def test_step_sequence_must_be_unique_within_a_run(running_store):
    running_store.create_step("run-1", sequence=1, title="inspect")

    with pytest.raises(ValueError, match="step sequence already exists"):
        running_store.create_step("run-1", sequence=1, title="execute")


def test_list_steps_is_ordered_and_preserves_completed_step(running_store):
    second = running_store.create_step("run-1", sequence=2, title="execute")
    first = running_store.create_step("run-1", sequence=1, title="inspect")
    running_store.transition_step(first.id, StepStatus.RUNNING, "step.started")
    running_store.transition_step(first.id, StepStatus.SUCCEEDED, "step.completed")

    assert [(step.id, step.status) for step in running_store.list_steps("run-1")] == [
        (first.id, StepStatus.SUCCEEDED),
        (second.id, StepStatus.PENDING),
    ]


def test_tool_call_step_must_belong_to_its_run(running_store, tmp_path):
    running_store.create_run(
        goal="other", workflow="test", workspace=tmp_path, model="test",
        prompt_version="v1", run_id="run-2",
    )
    step = running_store.create_step("run-2", sequence=1, title="other")

    with pytest.raises(ValueError, match="step belongs to another run"):
        running_store.create_tool_call(
            run_id="run-1", step_id=step.id, tool_name="probe", arguments={"argv": ["probe"]},
            risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
            idempotent=True, idempotency_key="key", timeout_seconds=2,
        )


def test_step_duplicate_and_illegal_events_do_not_change_history(running_store):
    step = running_store.create_step('run-1', sequence=1, title='execute')
    with pytest.raises(ValueError, match='event'):
        running_store.transition_step(step.id, StepStatus.RUNNING, 'step.completed')
    assert running_store.list_steps('run-1')[0].status is StepStatus.PENDING
    running_store.transition_step(step.id, StepStatus.RUNNING, 'step.started')
    running_store.transition_step(step.id, StepStatus.SUCCEEDED, 'step.completed')
    before = running_store.list_events('run-1')
    running_store.transition_step(step.id, StepStatus.SUCCEEDED, 'step.completed')
    with pytest.raises(ValueError):
        running_store.transition_step(step.id, StepStatus.FAILED, 'step.failed')
    assert running_store.list_events('run-1') == before


def test_step_ignored_update_cannot_emit_completion(running_store):
    step = running_store.create_step('run-1', sequence=1, title='execute')
    running_store.transition_step(step.id, StepStatus.RUNNING, 'step.started')
    before = running_store.list_events('run-1')
    with running_store._connect() as db:
        db.execute("CREATE TRIGGER reject_step BEFORE UPDATE OF status ON steps WHEN NEW.status = 'succeeded' BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(ValueError, match='did not apply'):
        running_store.transition_step(step.id, StepStatus.SUCCEEDED, 'step.completed')
    assert running_store.list_steps('run-1')[0].status is StepStatus.RUNNING
    assert running_store.list_events('run-1') == before
