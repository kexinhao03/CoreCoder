import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corecoder.runtime.approvals import ApprovalDecision, ApprovalStatus
from corecoder.runtime.executor import ExecutionRefused, PendingApproval, RuntimeExecutor
from corecoder.runtime.policies import FailureKind, ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.processes import ManagedProcessRunner, ProcessResult
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


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


def test_cancel_run_settles_waiting_call_without_spawn(running_store):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    executor.submit_subprocess(
        "run-1", "bash", (sys.executable, "-c", "print('no')"),
        tool_call_id="call-1", approval_id="approval-1",
    )
    executor.cancel_run("run-1")
    assert running_store.get_run("run-1").status is RunStatus.CANCELLED
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.CANCELLED
    assert runner.calls == []
    with pytest.raises(ExecutionRefused, match="run cannot accept work: cancelled"):
        executor.submit_subprocess(
            "run-1", "bash", (sys.executable, "-c", "print('no')")
        )


def test_cancel_run_stops_active_process(running_store, tmp_path):
    marker = tmp_path / "started"
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}),
        ManagedProcessRunner(),
    )
    result_box = []
    worker = threading.Thread(
        target=lambda: result_box.append(executor.submit_subprocess(
            "run-1", "probe",
            (sys.executable, "-c",
             f"open({str(marker)!r}, 'w').close(); import time; time.sleep(10)"),
            tool_call_id="call-1",
        ))
    )
    worker.start()
    try:
        wait_until(lambda: marker.exists(), timeout=1)
        executor.cancel_run("run-1")
        worker.join(timeout=2)
        assert worker.is_alive() is False
        assert result_box[0].call.status is ToolCallStatus.CANCELLED
        assert running_store.get_run("run-1").status is RunStatus.CANCELLED
    finally:
        # The policy's two-second deadline bounds cleanup even on a RED failure.
        worker.join(timeout=5)


@pytest.mark.parametrize("in_process", [False, True])
def test_cancel_between_started_commit_and_registration_never_invokes(
    running_store, monkeypatch, in_process
):
    runner = SpyRunner(success_result("must not run"))
    executor = RuntimeExecutor(
        running_store, inspect_registry() if in_process else
        ToolPolicyRegistry({"probe": probe_policy()}), runner,
    )
    original_transition = running_store.transition_tool_call

    def cancel_after_start(*args, **kwargs):
        call = original_transition(*args, **kwargs)
        if call.status is ToolCallStatus.RUNNING:
            executor.cancel_run("run-1")
        return call

    monkeypatch.setattr(running_store, "transition_tool_call", cancel_after_start)
    invoked = []
    if in_process:
        result = executor.submit_in_process(
            "run-1", "inspect", {}, lambda args, event: invoked.append(args),
            tool_call_id="call-1",
        )
    else:
        result = executor.submit_subprocess("run-1", "probe", ("unused",), tool_call_id="call-1")
    assert result.call.status is ToolCallStatus.CANCELLED
    assert result.failure_kind is FailureKind.CANCELLED
    assert invoked == runner.calls == []
    assert executor._active_cancellations == {}


@pytest.mark.parametrize("boundary", ["create_tool_call", "request_approval", "transition_tool_call"])
def test_submit_translates_cancellation_admission_race(running_store, monkeypatch, boundary):
    runner = SpyRunner(success_result("must not run"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy(),
                                         "bash": probe_policy(risk_level=RiskLevel.MUTATING)}),
        runner,
    )
    original = getattr(running_store, boundary)

    def cancel_before_admission(*args, **kwargs):
        executor.cancel_run("run-1")
        return original(*args, **kwargs)

    monkeypatch.setattr(running_store, boundary, cancel_before_admission)
    with pytest.raises(ExecutionRefused, match="run cannot accept work: cancelled"):
        executor.submit_subprocess(
            "run-1", "bash" if boundary == "request_approval" else "probe", ("unused",)
        )
    assert runner.calls == []


def test_cancel_run_signals_only_its_active_callable_after_commit(running_store):
    running_store.create_run(
        goal="other", workflow="test", workspace=running_store.path.parent,
        model="test", prompt_version="v1", run_id="run-2",
    )
    running_store.transition_run("run-2", RunStatus.RUNNING, "run.started")
    executor = RuntimeExecutor(running_store, inspect_registry())
    ready = {run_id: threading.Event() for run_id in ("run-1", "run-2")}
    release = threading.Event()
    events = {}

    def operation(arguments, cancellation):
        run_id = arguments["run_id"]
        events[run_id] = cancellation
        ready[run_id].set()
        assert release.wait(timeout=2)
        return "partial" if cancellation.is_set() else "done"

    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [pool.submit(executor.submit_in_process, run_id, "inspect",
                               {"run_id": run_id}, operation)
                   for run_id in ready]
        try:
            assert all(event.wait(timeout=1) for event in ready.values())
            executor.cancel_run("run-1")
            assert events["run-1"].is_set()
            assert not events["run-2"].is_set()
            assert running_store.get_run("run-1").status is RunStatus.CANCELLED
        finally:
            release.set()
        assert workers[0].result().call.status is ToolCallStatus.CANCELLED
        assert workers[1].result().call.status is ToolCallStatus.SUCCEEDED
    assert executor._active_cancellations == {}


@pytest.mark.parametrize(("process_result", "status"), [
    (success_result("confirmed effect"), ToolCallStatus.SUCCEEDED),
    (ProcessResult(None, "", "", 0.1, FailureKind.TERMINATION_UNKNOWN, False),
     ToolCallStatus.INTERRUPTED),
    (ProcessResult(None, "", "", 0.1, FailureKind.TIMED_OUT, True), ToolCallStatus.TIMED_OUT),
])
def test_cancel_preserves_process_outcome_without_retry(running_store, process_result, status):
    class CancelOnReturnRunner(SpyRunner):
        def run(self, spec, cancel_event):
            executor.cancel_run("run-1")
            assert cancel_event.is_set()
            return super().run(spec, cancel_event)

    runner = CancelOnReturnRunner(process_result)
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": probe_policy(
        max_attempts=2, auto_retry=True, retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )}), runner)
    result = executor.submit_subprocess("run-1", "probe", ("command",))
    assert result.call.status is status
    assert result.output == process_result.stdout
    assert result.failure_kind is process_result.failure_kind
    assert len(runner.calls) == 1
    assert "tool.retry_scheduled" not in [e.type for e in running_store.list_events("run-1")]


def test_cancel_before_retry_admission_is_refused_without_another_invocation(
    running_store, monkeypatch
):
    runner = SequenceRunner([timeout_result(), success_result("must not run")])
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": probe_policy(
        max_attempts=2, auto_retry=True, retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )}), runner)
    original_schedule = running_store.schedule_retry

    def cancel_before_retry(*args):
        executor.cancel_run("run-1")
        return original_schedule(*args)

    monkeypatch.setattr(running_store, "schedule_retry", cancel_before_retry)
    with pytest.raises(ExecutionRefused, match="run cannot accept work: cancelled"):
        executor.submit_subprocess("run-1", "probe", ("command",), tool_call_id="call-1")
    assert len(runner.calls) == 1
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.TIMED_OUT
    assert len([e for e in running_store.list_events("run-1") if e.type == "tool.created"]) == 1
    assert "tool.retry_scheduled" not in [e.type for e in running_store.list_events("run-1")]


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
            assert executor._active_cancellations["call-1"] == ("run-1", cancel_event)
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
    events = [e for e in running_store.list_events("run-1") if e.type.startswith("tool.")]
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
    with pytest.raises(ExecutionRefused, match=f"run cannot accept work: {state.value}"):
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


@pytest.mark.parametrize("argv", [(), [], (1,), ("bad\x00argument",), "command", {"a": "b"}])
def test_invalid_submission_argv_has_no_durable_or_process_side_effects(running_store, argv):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry.with_builtin_defaults(), runner)
    before = running_store.list_events("run-1")
    with pytest.raises(ValueError, match="argv"):
        executor.submit_subprocess(
            "run-1", "bash", argv, tool_call_id="call-invalid", approval_id="approval-invalid"
        )
    # Invalid argv is rejected before even looking up a Run.
    with pytest.raises(ValueError, match="argv"):
        executor.submit_subprocess("missing-run", "bash", argv)
    assert running_store.list_events("run-1") == before
    assert running_store.get_run("run-1").status is RunStatus.RUNNING
    with pytest.raises(KeyError):
        running_store.get_tool_call("call-invalid")
    assert running_store.get_approval_for_tool_call("call-invalid") is None
    assert executor._active_cancellations == {}
    assert runner.calls == []


@pytest.mark.parametrize("arguments", [
    [], None, "command", {}, {"argv": []}, {"argv": "command"},
    {"argv": {"command": "arg"}}, {"argv": [1]}, {"argv": ["bad\x00argument"]},
])
def test_corrupt_persisted_argv_is_refused_before_start(running_store, arguments):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry.with_builtin_defaults(), runner)
    executor.submit_subprocess(
        "run-1", "bash", ("valid-command",), tool_call_id="call-1", approval_id="approval-1"
    )
    running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    with sqlite3.connect(running_store.path) as connection:
        connection.execute(
            "UPDATE tool_calls SET arguments_json = ? WHERE id = ?",
            (json.dumps(arguments), "call-1"),
        )
    before = running_store.list_events("run-1")
    with pytest.raises(ValueError, match="argv"):
        executor.execute_approved_subprocess("call-1")
    call = running_store.get_tool_call("call-1")
    assert call.status is ToolCallStatus.WAITING_APPROVAL
    assert call.started_at is None
    assert running_store.get_approval("approval-1").status is ApprovalStatus.APPROVED
    assert running_store.get_run("run-1").status is RunStatus.RUNNING
    assert running_store.list_events("run-1") == before
    assert executor._active_cancellations == {}
    assert runner.calls == []


def inspect_registry():
    return ToolPolicyRegistry({
        "inspect": ToolPolicy(
            risk_level=RiskLevel.READ_ONLY,
            execution_kind=ExecutionKind.IN_PROCESS,
            timeout_seconds=2, max_attempts=1, idempotent=True,
            auto_retry=False, retryable_failures=frozenset(), output_limit=1000,
        )
    })


def test_in_process_operation_receives_persisted_arguments(running_store):
    seen = []
    executor = RuntimeExecutor(running_store, inspect_registry())

    def operation(arguments, cancel_event):
        observer = SQLiteStore(running_store.path)
        assert observer.get_tool_call("call-1").status is ToolCallStatus.RUNNING
        assert observer.list_events("run-1")[-1].type == "tool.started"
        assert executor._active_cancellations["call-1"] == ("run-1", cancel_event)
        seen.append((arguments, cancel_event.is_set()))
        return "inspected"

    result = executor.submit_in_process(
        "run-1", "inspect", {"path": "README.md"}, operation, tool_call_id="call-1"
    )
    assert result.call.status is ToolCallStatus.SUCCEEDED
    assert seen == [({"path": "README.md"}, False)]
    assert result.output == result.call.result_summary == "inspected"
    assert result.call == running_store.get_tool_call("call-1")
    assert result.failure_kind is None
    assert executor._active_cancellations == {}


def test_in_process_exception_is_a_failed_execution(running_store):
    def operation(arguments, cancel_event):
        raise RuntimeError("boom")

    executor = RuntimeExecutor(running_store, inspect_registry())
    result = executor.submit_in_process(
        "run-1", "inspect", {}, operation, tool_call_id="call-1"
    )
    assert result.call.status is ToolCallStatus.FAILED
    assert result.failure_kind is FailureKind.EXECUTION_ERROR
    assert result.output == result.call.result_summary == "boom"
    assert running_store.list_events("run-1")[-1].payload == {
        "tool_call_id": "call-1", "failure_kind": "execution_error"
    }
    assert executor._active_cancellations == {}


def test_blocking_in_process_callable_has_no_forced_timeout(running_store):
    registry = ToolPolicyRegistry.with_builtin_defaults()
    executor = RuntimeExecutor(running_store, registry)
    seen = []
    caller_thread = threading.get_ident()

    def operation(arguments, cancel_event):
        assert threading.get_ident() == caller_thread
        seen.append(arguments)
        return "written"

    arguments = {"path": "README.md", "content": "approved"}
    pending = executor.submit_in_process(
        "run-1", "write_file", arguments, operation,
        tool_call_id="call-1", approval_id="approval-1",
    )
    assert isinstance(pending, PendingApproval)
    assert pending.call.status is ToolCallStatus.WAITING_APPROVAL
    assert seen == []
    arguments["content"] = "replacement"
    pending.call.arguments["content"] = "replacement"
    running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    restarted = RuntimeExecutor(SQLiteStore(running_store.path), registry)
    result = restarted.execute_approved_in_process("call-1", operation)
    assert result.call.status is ToolCallStatus.SUCCEEDED
    assert seen == [{"path": "README.md", "content": "approved"}]
    with pytest.raises(ExecutionRefused):
        restarted.execute_approved_in_process("call-1", operation)
    assert len(seen) == 1


@pytest.mark.parametrize("decision", [None, ApprovalDecision.DENY])
def test_in_process_requires_approval_bound_to_call(running_store, decision):
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry.with_builtin_defaults())
    seen = []

    def operation(arguments, cancel_event):
        seen.append(arguments)
        return "unused"

    executor.submit_in_process(
        "run-1", "write_file", {}, operation,
        tool_call_id="call-1", approval_id="approval-1",
    )
    if decision:
        running_store.resolve_approval("approval-1", decision)
    before = running_store.list_events("run-1")
    for call_id in ("call-1", "other-call"):
        with pytest.raises(ExecutionRefused):
            executor.execute_approved_in_process(call_id, operation)
    assert running_store.list_events("run-1") == before
    assert seen == []


@pytest.mark.parametrize("cancel_before", [True, False])
def test_in_process_cooperative_cancellation(running_store, monkeypatch, cancel_before):
    cancellation = threading.Event()
    seen = []
    if cancel_before:
        cancellation.set()
        monkeypatch.setattr("corecoder.runtime.executor.threading.Event", lambda: cancellation)

    def operation(arguments, cancel_event):
        seen.append(arguments)
        cancel_event.set()
        return "partial"

    executor = RuntimeExecutor(running_store, inspect_registry())
    result = executor.submit_in_process(
        "run-1", "inspect", {}, operation, tool_call_id="call-1"
    )
    assert result.call.status is ToolCallStatus.CANCELLED
    assert result.failure_kind is FailureKind.CANCELLED
    assert seen == ([] if cancel_before else [{}])
    assert running_store.list_events("run-1")[-1].type == "tool.cancelled"
    assert executor._active_cancellations == {}


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_in_process_base_exception_propagates_and_cleans_registration(running_store, exception):
    def operation(arguments, cancel_event):
        raise exception()

    executor = RuntimeExecutor(running_store, inspect_registry())
    with pytest.raises(exception):
        executor.submit_in_process("run-1", "inspect", {}, operation, tool_call_id="call-1")
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.RUNNING
    assert running_store.list_events("run-1")[-1].type == "tool.started"
    assert executor._active_cancellations == {}


def test_in_process_output_is_bounded(running_store):
    executor = RuntimeExecutor(running_store, inspect_registry())
    result = executor.submit_in_process("run-1", "inspect", {}, lambda args, event: "x" * 1200)
    assert result.output == result.call.result_summary == "x" * 1000


@pytest.mark.parametrize("state", [RunStatus.PAUSED, RunStatus.SUCCEEDED])
def test_in_process_refuses_nonrunning_run(running_store, state):
    running_store.transition_run("run-1", state, "run.stopped")
    executor = RuntimeExecutor(running_store, inspect_registry())
    before = running_store.list_events("run-1")
    with pytest.raises(ExecutionRefused, match=f"run cannot accept work: {state.value}"):
        executor.submit_in_process("run-1", "inspect", {}, lambda args, event: "unused")
    assert running_store.list_events("run-1") == before


def test_in_process_refuses_subprocess_policy(running_store):
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry.with_builtin_defaults())
    before = running_store.list_events("run-1")
    with pytest.raises(ExecutionRefused, match="in-process"):
        executor.submit_in_process("run-1", "bash", {}, lambda args, event: "unused")
    assert running_store.list_events("run-1") == before


class SequenceRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, spec, cancel_event):
        self.calls.append(spec)
        return self.results.pop(0)


def timeout_result():
    return ProcessResult(
        exit_code=None, stdout="", stderr="timeout", duration_seconds=0.1,
        failure_kind=FailureKind.TIMED_OUT, termination_confirmed=True,
    )


def test_retryable_timeout_creates_a_new_attempt(running_store):
    runner = SequenceRunner([timeout_result(), success_result("ok")])
    policy = probe_policy(
        max_attempts=2, auto_retry=True,
        retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": policy}), runner)
    result = executor.submit_subprocess(
        "run-1", "probe", (sys.executable, "-c", "print('ok')"), tool_call_id="call-1"
    )
    assert len(runner.calls) == 2
    source = running_store.get_tool_call("call-1")
    assert source.status is ToolCallStatus.TIMED_OUT
    assert result.call.status is ToolCallStatus.SUCCEEDED
    assert result.call.attempt == 2
    assert result.call.retry_of == "call-1"
    assert result.call.id != source.id
    assert result.call.arguments == source.arguments
    assert result.call.idempotency_key == source.idempotency_key
    assert result.call.timeout_seconds == source.timeout_seconds
    assert runner.calls[0] == runner.calls[1]
    assert [event.type for event in running_store.list_events("run-1")][-5:] == [
        "tool.timed_out", "tool.retry_scheduled", "tool.created", "tool.started", "tool.completed"
    ]


def test_nonzero_exit_is_not_retried(running_store):
    runner = SequenceRunner([ProcessResult(
        1, "", "failed", 0.1, FailureKind.NONZERO_EXIT, True,
    )])
    policy = probe_policy(
        max_attempts=2, auto_retry=True,
        retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": policy}), runner)
    result = executor.submit_subprocess("run-1", "probe", ("command",))
    assert result.call.status is ToolCallStatus.FAILED
    assert len(runner.calls) == 1
    assert "tool.retry_scheduled" not in [e.type for e in running_store.list_events("run-1")]


def test_retry_stops_at_max_attempts(running_store):
    runner = SequenceRunner([timeout_result(), timeout_result()])
    policy = probe_policy(
        max_attempts=2, auto_retry=True,
        retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": policy}), runner)
    result = executor.submit_subprocess("run-1", "probe", ("command",))
    assert len(runner.calls) == 2
    assert result.call.attempt == 2
    assert result.call.status is ToolCallStatus.TIMED_OUT


@pytest.mark.parametrize("kind", [FailureKind.CANCELLED, FailureKind.TERMINATION_UNKNOWN])
def test_retry_refuses_cancelled_or_unknown_outcome(running_store, kind):
    runner = SequenceRunner([ProcessResult(None, "", "", 0.1, kind, False)])
    policy = probe_policy(max_attempts=2, auto_retry=True, retryable_failures=frozenset({kind}))
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": policy}), runner)
    result = executor.submit_subprocess("run-1", "probe", ("command",))
    assert result.call.attempt == 1
    assert len(runner.calls) == 1


def test_retry_requires_auto_retry_policy(running_store):
    runner = SequenceRunner([timeout_result()])
    policy = probe_policy(max_attempts=2, retryable_failures=frozenset({FailureKind.TIMED_OUT}))
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": policy}), runner)
    result = executor.submit_subprocess("run-1", "probe", ("command",))
    assert result.call.attempt == 1
    assert len(runner.calls) == 1


def test_retry_requires_persisted_source_idempotence(running_store):
    runner = SequenceRunner([timeout_result()])
    original = RuntimeExecutor(running_store, ToolPolicyRegistry.with_builtin_defaults(), runner)
    original.submit_subprocess(
        "run-1", "bash", ("command",), tool_call_id="call-1", approval_id="approval-1"
    )
    running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    policy = probe_policy(
        max_attempts=2, auto_retry=True, retryable_failures=frozenset({FailureKind.TIMED_OUT})
    )
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"bash": policy}), runner)
    result = executor.execute_approved_subprocess("call-1")
    assert not result.call.idempotent
    assert result.call.attempt == 1
    assert len(runner.calls) == 1


@pytest.mark.parametrize("risk", [RiskLevel.MUTATING, RiskLevel.EXTERNAL_EFFECT])
def test_policy_change_cannot_retry_approved_non_read_only_call(running_store, risk):
    runner = SequenceRunner([timeout_result(), success_result("unauthorized retry")])
    original = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy(risk_level=risk)}), runner
    )
    pending = original.submit_subprocess(
        "run-1", "probe", ("command",), tool_call_id="call-1", approval_id="approval-1"
    )
    assert isinstance(pending, PendingApproval)
    assert pending.call.idempotent
    assert runner.calls == []
    running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    read_only_retry_policy = probe_policy(
        max_attempts=2, auto_retry=True, retryable_failures=frozenset({FailureKind.TIMED_OUT})
    )
    restarted = RuntimeExecutor(
        SQLiteStore(running_store.path),
        ToolPolicyRegistry({"probe": read_only_retry_policy}), runner,
    )
    result = restarted.execute_approved_subprocess("call-1")
    assert len(runner.calls) == 1
    assert result.call.id == "call-1"
    assert result.call.risk_level is risk
    assert result.call.status is ToolCallStatus.TIMED_OUT
    assert result.call.attempt == 1
    assert "tool.retry_scheduled" not in [e.type for e in running_store.list_events("run-1")]


def test_retry_requires_running_run(running_store):
    class PausingRunner(SequenceRunner):
        def run(self, spec, cancel_event):
            running_store.transition_run("run-1", RunStatus.PAUSED, "run.paused")
            return super().run(spec, cancel_event)

    runner = PausingRunner([timeout_result()])
    policy = probe_policy(
        max_attempts=2, auto_retry=True, retryable_failures=frozenset({FailureKind.TIMED_OUT})
    )
    executor = RuntimeExecutor(running_store, ToolPolicyRegistry({"probe": policy}), runner)
    result = executor.submit_subprocess("run-1", "probe", ("command",))
    assert result.call.attempt == 1
    assert len(runner.calls) == 1
    assert "tool.retry_scheduled" not in [e.type for e in running_store.list_events("run-1")]
