import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corecoder.runtime.approvals import ApprovalDecision, ApprovalStatus
from corecoder.runtime.executor import ExecutionRefused, PendingApproval, RuntimeExecutor
from corecoder.runtime.policies import FailureKind, ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.processes import ProcessResult
from corecoder.runtime.state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus
from corecoder.runtime.store import SQLiteStore


class SpyRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, spec, cancel_event):
        self.calls.append(spec)
        return self.result


def success_result(text):
    return ProcessResult(
        exit_code=0, stdout=text, stderr="", duration_seconds=0.01,
        failure_kind=None, termination_confirmed=True,
    )


def probe_policy(**overrides):
    values = {
        "risk_level": RiskLevel.READ_ONLY,
        "execution_kind": ExecutionKind.SUBPROCESS,
        "timeout_seconds": 2,
        "max_attempts": 1,
        "idempotent": True,
        "auto_retry": False,
        "retryable_failures": frozenset(),
        "output_limit": 1000,
    }
    values.update(overrides)
    return ToolPolicy(**values)


def test_mutating_submission_persists_approval_without_spawning(running_store):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    pending = executor.submit_subprocess(
        "run-1", "bash", (sys.executable, "-c", "print('no')"),
        tool_call_id="call-1", approval_id="approval-1",
    )
    assert isinstance(pending, PendingApproval)
    assert pending.call == running_store.get_tool_call("call-1")
    assert pending.call.status is ToolCallStatus.WAITING_APPROVAL
    assert pending.call.idempotency_key is None
    assert pending.approval == running_store.get_approval("approval-1")
    assert pending.approval.status is ApprovalStatus.PENDING
    assert running_store.get_run("run-1").status is RunStatus.WAITING_APPROVAL
    assert running_store.list_events("run-1")[-1].type == "approval.requested"
    assert runner.calls == []


def test_approval_for_another_call_cannot_authorize_execution(running_store):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    executor.submit_subprocess(
        "run-1", "bash", (sys.executable, "-c", "print('no')"),
        tool_call_id="call-1", approval_id="approval-1",
    )
    running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    before = running_store.list_events("run-1")
    with pytest.raises(ExecutionRefused, match="approved allow-once decision required"):
        executor.execute_approved_subprocess("call-other")
    assert running_store.list_events("run-1") == before
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.WAITING_APPROVAL
    assert runner.calls == []


@pytest.mark.parametrize("decision", [None, ApprovalDecision.DENY])
def test_pending_or_denied_approval_never_spawns(running_store, decision):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    executor.submit_subprocess(
        "run-1", "bash", ("command",), tool_call_id="call-1", approval_id="approval-1"
    )
    if decision:
        running_store.resolve_approval("approval-1", decision)
    before = running_store.list_events("run-1")
    with pytest.raises(ExecutionRefused, match="approved allow-once decision required"):
        executor.execute_approved_subprocess("call-1")
    assert running_store.list_events("run-1") == before
    assert runner.calls == []


def test_read_only_subprocess_executes_and_persists_success(running_store):
    runner = SpyRunner(success_result("ok"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}), runner
    )
    result = executor.submit_subprocess(
        "run-1", "probe", (sys.executable, "-c", "print('ok')"),
        tool_call_id="call-1",
    )
    assert result.call.status is ToolCallStatus.SUCCEEDED
    assert result.call == running_store.get_tool_call("call-1")
    assert result.output == result.call.result_summary == "ok"
    assert result.failure_kind is None
    assert len(runner.calls) == 1
    assert [event.type for event in running_store.list_events("run-1")][-2:] == [
        "tool.started", "tool.completed"
    ]


def test_approved_execution_uses_persisted_argv_once(running_store):
    runner = SpyRunner(success_result("ok"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    argv = (sys.executable, "-c", "print('approved')")
    pending = executor.submit_subprocess(
        "run-1", "bash", argv, tool_call_id="call-1", approval_id="approval-1",
    )
    pending.call.arguments["argv"] = ["replacement"]
    running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    restarted = RuntimeExecutor(
        SQLiteStore(running_store.path), ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    result = restarted.execute_approved_subprocess("call-1")
    assert runner.calls[0].argv == argv
    assert result.call.arguments == {"argv": list(argv)}
    assert result.call.status is ToolCallStatus.SUCCEEDED
    with pytest.raises(ExecutionRefused):
        restarted.execute_approved_subprocess("call-1")
    assert len(runner.calls) == 1
    assert len([e for e in running_store.list_events("run-1") if e.type == "tool.started"]) == 1


def test_started_committed_and_cancellation_registered_before_runner(running_store):
    class InspectRunner:
        def run(self, spec, cancel_event):
            observer = SQLiteStore(running_store.path)
            assert observer.get_tool_call("call-1").status is ToolCallStatus.RUNNING
            assert observer.list_events("run-1")[-1].type == "tool.started"
            assert isinstance(cancel_event, threading.Event)
            assert executor._active_cancellations["call-1"] is cancel_event
            assert not cancel_event.is_set()
            assert spec.argv == ("probe-command",)
            assert spec.cwd == Path(observer.get_run("run-1").workspace)
            assert spec.timeout_seconds == 2
            assert spec.output_limit == 1000
            assert spec.environment is None
            assert spec.termination_grace_seconds > 0
            with pytest.raises(ValueError, match="active tool call"):
                executor.submit_subprocess("run-1", "probe", ("other",))
            return success_result("ok")

    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}), InspectRunner()
    )
    executor.submit_subprocess("run-1", "probe", ("probe-command",), tool_call_id="call-1")
    assert executor._active_cancellations == {}
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.SUCCEEDED


@pytest.mark.parametrize(("kind", "exit_code", "status", "event"), [
    (None, 0, ToolCallStatus.SUCCEEDED, "tool.completed"),
    (FailureKind.TIMED_OUT, -15, ToolCallStatus.TIMED_OUT, "tool.timed_out"),
    (FailureKind.CANCELLED, -15, ToolCallStatus.CANCELLED, "tool.cancelled"),
    (FailureKind.TERMINATION_UNKNOWN, None, ToolCallStatus.INTERRUPTED, "tool.interrupted"),
    (FailureKind.SPAWN_ERROR, None, ToolCallStatus.FAILED, "tool.failed"),
    (FailureKind.NONZERO_EXIT, 2, ToolCallStatus.FAILED, "tool.failed"),
    (FailureKind.EXECUTION_ERROR, None, ToolCallStatus.FAILED, "tool.failed"),
])
def test_process_result_mapping_and_safe_event_payload(
    running_store, kind, exit_code, status, event
):
    runner = SpyRunner(ProcessResult(
        exit_code, "output-secret", "error-secret", 0.01, kind,
        kind is not FailureKind.TERMINATION_UNKNOWN,
    ))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}), runner
    )
    result = executor.submit_subprocess(
        "run-1", "probe", ("argument-secret",), tool_call_id="call-1"
    )
    assert result.call == running_store.get_tool_call("call-1")
    assert result.call.status is status
    assert result.failure_kind is kind
    assert result.output == "output-secret\n[stderr]\nerror-secret"
    assert result.call.result_summary == result.output
    assert executor._active_cancellations == {}
    events = running_store.list_events("run-1")
    assert events[-2].payload == {"tool_call_id": "call-1"}
    assert events[-1].type == event
    assert events[-1].payload == {
        "tool_call_id": "call-1", "exit_code": exit_code,
        "failure_kind": kind.value if kind else None,
    }
    assert "secret" not in json.dumps([e.payload for e in events])


@pytest.mark.parametrize("limit", [20, 3000])
def test_output_and_store_summary_are_bounded(running_store, limit):
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy(output_limit=limit)}),
        SpyRunner(success_result("x" * 4000)),
    )
    result = executor.submit_subprocess("run-1", "probe", ("command",))
    assert len(result.output) == limit
    assert result.call.result_summary == result.output[:2000]


def test_idempotency_key_is_stable_and_sensitive_to_command(running_store):
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}),
        SpyRunner(success_result("ok")),
    )
    first = executor.submit_subprocess("run-1", "probe", ("a", "bc")).call
    same = executor.submit_subprocess("run-1", "probe", ("a", "bc")).call
    different = executor.submit_subprocess("run-1", "probe", ("ab", "c")).call
    assert first.idempotency_key == same.idempotency_key
    assert first.idempotency_key != different.idempotency_key
    assert len(bytes.fromhex(first.idempotency_key)) == 32
    assert running_store.get_tool_call(first.id).idempotency_key == first.idempotency_key


@pytest.mark.parametrize("state", [RunStatus.PAUSED, RunStatus.SUCCEEDED])
def test_nonrunning_run_is_refused_without_tool_creation(running_store, state):
    running_store.transition_run("run-1", state, "run.stopped")
    before = running_store.list_events("run-1")
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}), runner
    )
    with pytest.raises(ExecutionRefused, match="running"):
        executor.submit_subprocess("run-1", "probe", ("command",))
    assert running_store.list_events("run-1") == before
    assert runner.calls == []


def test_in_process_policy_is_refused_without_tool_creation(running_store):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry.with_builtin_defaults(), runner)
    before = running_store.list_events("run-1")
    with pytest.raises(ExecutionRefused, match="subprocess"):
        executor.submit_subprocess("run-1", "read_file", ("command",))
    assert running_store.list_events("run-1") == before
    assert runner.calls == []


def test_runner_exception_cleans_registration_without_claiming_completion(running_store):
    class BrokenRunner:
        def run(self, spec, cancel_event):
            raise RuntimeError("unexpected runner failure")

    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}), BrokenRunner()
    )
    with pytest.raises(RuntimeError, match="unexpected runner failure"):
        executor.submit_subprocess("run-1", "probe", ("command",), tool_call_id="call-1")
    assert executor._active_cancellations == {}
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.RUNNING
    assert running_store.list_events("run-1")[-1].type == "tool.started"


def test_default_runner_executes_real_subprocess(running_store):
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": probe_policy()}))
    result = executor.submit_subprocess("run-1", "probe", (sys.executable, "-c", "print('real')"))
    assert result.call.status is ToolCallStatus.SUCCEEDED
    assert result.output == "real\n"


def test_record_event_commits_returns_record_and_allocates_unique_sequences(running_store):
    def record(index):
        return running_store.record_event("run-1", "test.observed", {"index": index})

    with ThreadPoolExecutor(max_workers=4) as pool:
        recorded = list(pool.map(record, range(8)))
    events = SQLiteStore(running_store.path).list_events("run-1")
    assert [e.sequence for e in events] == list(range(1, 11))
    assert sorted(recorded, key=lambda e: e.sequence) == events[2:]
    assert {e.payload["index"] for e in events[2:]} == set(range(8))


def test_record_event_failure_rolls_back_sequence(running_store):
    before = running_store.list_events("run-1")
    with pytest.raises(TypeError):
        running_store.record_event("run-1", "test.invalid", {"invalid": object()})
    with pytest.raises(sqlite3.IntegrityError):
        running_store.record_event("missing", "test.invalid", {})
    assert running_store.list_events("run-1") == before
    event = running_store.record_event("run-1", "test.valid", {})
    assert event.sequence == before[-1].sequence + 1
