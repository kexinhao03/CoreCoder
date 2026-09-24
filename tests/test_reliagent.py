"""The minimal task adapter uses persisted Steps and the production Runtime."""

import sys

import pytest

from corecoder.reliagent import ReliAgentRuntime, TaskStep
from corecoder.runtime import (
    ApprovalDecision,
    ExecutionKind,
    RiskLevel,
    RunStatus,
    RuntimeExecutor,
    SQLiteStore,
    StepStatus,
    ToolPolicy,
    ToolPolicyRegistry,
)


def test_adapter_executes_a_step_through_runtime_and_skips_it_on_resume(tmp_path):
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    store.initialize()
    policy = ToolPolicy(
        risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=5, max_attempts=1, idempotent=True, auto_retry=False,
        retryable_failures=frozenset(), output_limit=1000,
    )
    runtime = ReliAgentRuntime(store, RuntimeExecutor(store, ToolPolicyRegistry({"probe": policy})))
    task = (TaskStep("probe", (sys.executable, "-c", "print('ok')")),)

    run = runtime.run_task(goal="test", workspace=tmp_path, steps=task)
    resumed = runtime.resume(run.id, steps=task)

    assert store.get_run(run.id).status is RunStatus.SUCCEEDED
    assert [step.status for step in store.list_steps(run.id)] == [StepStatus.SUCCEEDED]
    assert [call.step_id for call in store.list_tool_calls(run.id)] == [
        store.list_steps(run.id)[0].id
    ]
    assert resumed.executed_step_ids == ()


def test_adapter_waits_for_approval_without_advancing_next_step(tmp_path):
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    store.initialize()
    policies = ToolPolicyRegistry({
        "edit": ToolPolicy(
            risk_level=RiskLevel.MUTATING,
            execution_kind=ExecutionKind.SUBPROCESS,
            timeout_seconds=5,
            max_attempts=1,
            idempotent=False,
            auto_retry=False,
            retryable_failures=frozenset(),
            output_limit=1000,
        ),
        "probe": ToolPolicy(
            risk_level=RiskLevel.READ_ONLY,
            execution_kind=ExecutionKind.SUBPROCESS,
            timeout_seconds=5,
            max_attempts=1,
            idempotent=True,
            auto_retry=False,
            retryable_failures=frozenset(),
            output_limit=1000,
        ),
    })
    runtime = ReliAgentRuntime(store, RuntimeExecutor(store, policies))
    task = (
        TaskStep("edit", (sys.executable, "-c", "print('changed')")),
        TaskStep("probe", (sys.executable, "-c", "print('checked')")),
    )

    waiting = runtime.run_task(goal="test", workspace=tmp_path, steps=task)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert [step.status for step in store.list_steps(waiting.id)] == [
        StepStatus.WAITING_APPROVAL,
        StepStatus.PENDING,
    ]
    approval = store.get_approval_for_tool_call(store.list_tool_calls(waiting.id)[0].id)
    store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)

    resumed = runtime.resume(waiting.id, steps=task)

    assert resumed.run.status is RunStatus.SUCCEEDED
    assert [step.status for step in store.list_steps(waiting.id)] == [
        StepStatus.SUCCEEDED,
        StepStatus.SUCCEEDED,
    ]


def test_resume_rejects_task_step_count_mismatch_without_side_effects(tmp_path):
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    store.initialize()
    policy = ToolPolicy(
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=5,
        max_attempts=1,
        idempotent=True,
        auto_retry=False,
        retryable_failures=frozenset(),
        output_limit=1000,
    )
    runtime = ReliAgentRuntime(
        store,
        RuntimeExecutor(store, ToolPolicyRegistry({"probe": policy})),
    )
    run = store.create_run(
        goal="test",
        workflow="fixed_task",
        workspace=tmp_path,
        model="deterministic",
        prompt_version="phase3",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    store.create_step(run.id, sequence=1, title="probe")
    original_events = store.list_events(run.id)

    with pytest.raises(ValueError, match="task step count does not match persisted steps"):
        runtime.resume(run.id, steps=())

    assert store.list_events(run.id) == original_events
    assert store.list_tool_calls(run.id) == []
    assert store.get_run(run.id).status is RunStatus.RUNNING
