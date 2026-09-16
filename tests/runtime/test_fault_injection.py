import sqlite3
import sys

import pytest

from corecoder.runtime.approvals import ApprovalDecision
from corecoder.runtime.executor import RuntimeExecutor
from corecoder.runtime.policies import ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.recovery import RecoveryKind, RecoveryManager
from corecoder.runtime.state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus
from corecoder.runtime.store import SQLiteStore


def effect_policy():
    return ToolPolicy(
        risk_level=RiskLevel.EXTERNAL_EFFECT, execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=5, max_attempts=1, idempotent=False, auto_retry=False,
        retryable_failures=frozenset(), output_limit=1000,
    )


def effect_argv(marker):
    return (sys.executable, "-c", f"with open({str(marker)!r}, 'a') as f: f.write('effect\\n')")


def test_approval_event_failure_rolls_back_approval_call_run_and_events(running_store):
    # A missing transaction would leave a pending Approval or waiting state behind.
    running_store.create_tool_call(
        run_id="run-1", tool_name="mutation", arguments={"value": "new"},
        risk_level=RiskLevel.MUTATING, execution_kind=ExecutionKind.IN_PROCESS,
        idempotent=False, idempotency_key=None, timeout_seconds=5, tool_call_id="call-1",
    )
    events = running_store.list_events("run-1")
    with sqlite3.connect(running_store.path) as connection:
        connection.executescript("""
            CREATE TRIGGER fail_approval_event
            BEFORE INSERT ON events
            WHEN NEW.type = 'approval.requested'
            BEGIN
                SELECT RAISE(ABORT, 'injected approval event failure');
            END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="injected approval event failure"):
        running_store.request_approval(
            "call-1", arguments_summary="new", workspace=".", risk_reason="mutation",
            approval_id="approval-1",
        )
    assert running_store.get_approval_for_tool_call("call-1") is None
    with pytest.raises(KeyError, match="approval-1"):
        running_store.get_approval("approval-1")
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.CREATED
    assert running_store.get_run("run-1").status is RunStatus.RUNNING
    assert running_store.list_events("run-1") == events


def test_restart_scan_does_not_spawn_high_risk_orphan(running_store, tmp_path):
    # Replaying persisted argv would create this marker even before a prior effect.
    marker = tmp_path / "never-started"
    running_store.create_tool_call(
        run_id="run-1", tool_name="effect", arguments={"argv": list(effect_argv(marker))},
        risk_level=RiskLevel.EXTERNAL_EFFECT, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False, idempotency_key=None, timeout_seconds=5, tool_call_id="call-1",
    )
    running_store.transition_tool_call("call-1", ToolCallStatus.RUNNING, "tool.started")
    restarted = SQLiteStore(running_store.path)
    restarted.initialize()
    candidate, = RecoveryManager(restarted, ToolPolicyRegistry({"effect": effect_policy()})).scan()
    assert candidate.call.status is ToolCallStatus.INTERRUPTED
    assert candidate.run.status is RunStatus.RECOVERABLE
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    assert not marker.exists()


def test_completed_effect_with_failed_persistence_is_not_replayed_on_restart(
    running_store, tmp_path
):
    # Losing the completion commit cannot make an actual effect safe to repeat.
    marker = tmp_path / "effect-log"
    registry = ToolPolicyRegistry({"effect": effect_policy()})
    executor = RuntimeExecutor(running_store, registry)
    pending = executor.submit_subprocess(
        "run-1", "effect", effect_argv(marker), tool_call_id="call-1", approval_id="approval-1",
    )
    running_store.resolve_approval(pending.approval.id, ApprovalDecision.ALLOW_ONCE)
    with sqlite3.connect(running_store.path) as connection:
        connection.executescript("""
            CREATE TRIGGER fail_completion_event
            BEFORE INSERT ON events
            WHEN NEW.type = 'tool.completed'
            BEGIN
                SELECT RAISE(ABORT, 'injected completion event failure');
            END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="injected completion event failure"):
        executor.execute_approved_subprocess("call-1")
    assert marker.read_text() == "effect\n"
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.RUNNING
    assert "tool.completed" not in [event.type for event in running_store.list_events("run-1")]
    restarted = SQLiteStore(running_store.path)
    restarted.initialize()
    manager = RecoveryManager(restarted, registry)
    candidate, = manager.scan()
    assert candidate.call.status is ToolCallStatus.INTERRUPTED
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    assert candidate.reason == "process_lost"
    events = restarted.list_events("run-1")
    assert manager.scan() == [candidate]
    assert restarted.list_events("run-1") == events
    assert marker.read_text() == "effect\n"
    assert [call.id for call in restarted.list_tool_calls()] == ["call-1"]


def test_restart_scan_preserves_completed_call_and_its_events(running_store, tmp_path):
    # Scanning terminal calls must not overwrite a durable result or append events.
    running_store.create_run(
        goal="completed", workflow="test", workspace=tmp_path, model="test",
        prompt_version="v1", run_id="run-2",
    )
    running_store.transition_run("run-2", RunStatus.RUNNING, "run.started")
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry.with_builtin_defaults())
    pending = executor.submit_subprocess(
        "run-2", "bash", (sys.executable, "-c", "print('completed')"),
        tool_call_id="completed", approval_id="approval-completed",
    )
    running_store.resolve_approval(pending.approval.id, ApprovalDecision.ALLOW_ONCE)
    result = executor.execute_approved_subprocess("completed")
    assert result.call.status is ToolCallStatus.SUCCEEDED
    before = (running_store.get_run("run-2"), result.call, running_store.list_events("run-2"))
    restarted = SQLiteStore(running_store.path)
    restarted.initialize()
    assert RecoveryManager(restarted, ToolPolicyRegistry()).scan() == []
    assert (restarted.get_run("run-2"), restarted.get_tool_call("completed"),
            restarted.list_events("run-2")) == before
