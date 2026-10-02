"""Regression proofs for execution/recovery persistence and lifetime boundaries."""

import os
import signal
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from corecoder.runtime import processes
from corecoder.runtime.executor import ExecutionRefused, RuntimeExecutor
from corecoder.runtime.policies import FailureKind, ToolPolicyRegistry
from corecoder.runtime.processes import ManagedProcessRunner, ProcessResult, ProcessSpec
from corecoder.runtime.recovery import RecoveryKind, RecoveryManager, RecoveryResolution
from corecoder.runtime.state import RiskLevel, RunStatus, ToolCallStatus
from tests.runtime.test_executor import SequenceRunner, probe_policy, success_result, timeout_result


def executor_with_results(store, results, **policy_changes):
    registry = ToolPolicyRegistry({"probe": probe_policy(**policy_changes)})
    return RuntimeExecutor(store, registry, SequenceRunner(results))


def reserve_call(store):
    policy = probe_policy()
    return store.create_tool_call(
        run_id="run-1", tool_name="probe", arguments={"argv": ["probe"]},
        risk_level=policy.risk_level, execution_kind=policy.execution_kind,
        idempotent=True, idempotency_key="key", timeout_seconds=2, tool_call_id="call-1",
    )


def test_unknown_termination_blocks_admission_until_reconciled(running_store):
    executor = executor_with_results(running_store, [
        ProcessResult(None, "partial", "", 0.1, FailureKind.TERMINATION_UNKNOWN, False),
        success_result("fresh"),
    ])
    result = executor.submit_subprocess("run-1", "probe", ("probe",))
    assert result.call.status is ToolCallStatus.INTERRUPTED
    assert running_store.get_run("run-1").status is RunStatus.RECOVERABLE
    events = running_store.list_events("run-1")[-2:]
    assert [e.type for e in events] == ["tool.interrupted", "run.recoverable"]
    assert all(e.payload["tool_call_id"] == result.call.id for e in events)
    with pytest.raises(ExecutionRefused):
        executor.submit_subprocess("run-1", "probe", ("probe",))
    RecoveryManager(running_store, ToolPolicyRegistry()).reconcile(
        result.call.id, RecoveryResolution.CONFIRMED_FAILED,
    )
    assert executor.submit_subprocess("run-1", "probe", ("probe",)).output == "fresh"


def test_interruption_run_event_failure_rolls_back_both_states(running_store):
    reserve_call(running_store)
    running_store.transition_tool_call("call-1", ToolCallStatus.RUNNING, "tool.started")
    before = running_store.list_events("run-1")
    with sqlite3.connect(running_store.path) as connection:
        connection.execute("""CREATE TRIGGER fail_recoverable BEFORE INSERT ON events
            WHEN NEW.type = 'run.recoverable'
            BEGIN SELECT RAISE(ABORT, 'injected run event failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected run event failure"):
        running_store.transition_tool_call("call-1", ToolCallStatus.INTERRUPTED, "tool.interrupted")
    assert running_store.get_run("run-1").status is RunStatus.RUNNING
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.RUNNING
    assert running_store.list_events("run-1") == before


@pytest.mark.parametrize("failure_at", ["insert", "scheduled", "created"])
def test_retry_reservation_failure_leaves_no_phantom_schedule(running_store, failure_at):
    trigger = {
        "insert": "ON tool_calls WHEN NEW.attempt > 1",
        "scheduled": "ON events WHEN NEW.type = 'tool.retry_scheduled'",
        "created": "ON events WHEN NEW.type = 'tool.created' AND NEW.payload_json LIKE '%\"attempt\": 2%'",
    }[failure_at]
    with sqlite3.connect(running_store.path) as connection:
        connection.execute(f"""CREATE TRIGGER fail_retry BEFORE INSERT {trigger}
            BEGIN SELECT RAISE(ABORT, 'injected retry failure'); END""")
    executor = executor_with_results(
        running_store, [timeout_result(), success_result("unused")], max_attempts=2,
        auto_retry=True, retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected retry failure"):
        executor.submit_subprocess("run-1", "probe", ("probe",))
    calls = running_store.list_tool_calls()
    assert len(calls) == 1
    assert calls[0].status is ToolCallStatus.TIMED_OUT
    assert "tool.retry_scheduled" not in [e.type for e in running_store.list_events("run-1")]


@pytest.mark.parametrize("invalid", ["stale", "cancelled", "occupied", "risk", "idempotence",
                                    "policy_risk", "budget", "disabled", "failure"])
def test_retry_transaction_revalidates_admission(running_store, invalid):
    source = executor_with_results(running_store, [timeout_result()]).submit_subprocess(
        "run-1", "probe", ("probe",),
    ).call
    policy = probe_policy(max_attempts=2, auto_retry=True,
                          retryable_failures=frozenset({FailureKind.TIMED_OUT}))
    if invalid == "stale":
        source = replace(source, arguments={"argv": ["forged"]})
    elif invalid == "cancelled":
        running_store.cancel_run("run-1")
    elif invalid == "occupied":
        reserve_call(running_store)
    elif invalid in {"risk", "idempotence"}:
        with sqlite3.connect(running_store.path) as connection:
            if invalid == "risk":
                connection.execute("UPDATE tool_calls SET risk_level = 'mutating' WHERE id = ?", (source.id,))
            else:
                connection.execute("UPDATE tool_calls SET idempotent = 0 WHERE id = ?", (source.id,))
        source = running_store.get_tool_call(source.id)
    elif invalid == "policy_risk":
        policy = replace(policy, risk_level=RiskLevel.MUTATING, auto_retry=False)
    elif invalid == "budget":
        policy = replace(policy, max_attempts=1)
    elif invalid == "disabled":
        policy = replace(policy, auto_retry=False)
    else:
        policy = replace(policy, retryable_failures=frozenset())
    before = (running_store.list_tool_calls(), running_store.list_events("run-1"))
    with pytest.raises(ValueError):
        running_store.schedule_retry(source, FailureKind.TIMED_OUT, ToolPolicyRegistry({"probe": policy}))
    assert (running_store.list_tool_calls(), running_store.list_events("run-1")) == before


def test_retry_audit_identifies_new_attempt_and_cannot_repeat_source(running_store):
    source = executor_with_results(running_store, [timeout_result()]).submit_subprocess(
        "run-1", "probe", ("probe",),
    ).call
    registry = ToolPolicyRegistry({"probe": probe_policy(
        max_attempts=2, auto_retry=True, retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )})
    retry = running_store.schedule_retry(source, FailureKind.TIMED_OUT, registry)
    events = running_store.list_events("run-1")[-2:]
    assert [e.type for e in events] == ["tool.retry_scheduled", "tool.created"]
    assert events[0].payload == {"tool_call_id": retry.id, "retry_of": source.id, "attempt": 2}
    assert events[1].payload["tool_call_id"] == retry.id
    assert retry.arguments == source.arguments
    running_store.transition_tool_call(retry.id, ToolCallStatus.CANCELLED, "tool.cancelled")
    before = running_store.list_events("run-1")
    with pytest.raises(ValueError):
        running_store.schedule_retry(source, FailureKind.TIMED_OUT, registry)
    assert running_store.list_events("run-1") == before


@pytest.mark.parametrize("run_status", [RunStatus.PAUSED, RunStatus.CANCELLED])
def test_interruption_preserves_non_running_admission_barrier(running_store, run_status):
    reserve_call(running_store)
    running_store.transition_tool_call("call-1", ToolCallStatus.RUNNING, "tool.started")
    running_store.transition_run("run-1", run_status, "run." + run_status.value)
    running_store.transition_tool_call("call-1", ToolCallStatus.INTERRUPTED, "tool.interrupted")
    assert running_store.get_run("run-1").status is run_status
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.INTERRUPTED
    with pytest.raises(ExecutionRefused):
        executor_with_results(running_store, [success_result("unused")]).submit_subprocess(
            "run-1", "probe", ("probe",),
        )


def test_created_reservation_is_repeatable_and_abandonable(running_store):
    call = reserve_call(running_store)
    manager = RecoveryManager(running_store, ToolPolicyRegistry())
    before = running_store.list_events("run-1")
    candidates = manager.scan()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert (candidate.call, candidate.kind, candidate.reason) == (
        call, RecoveryKind.HUMAN_REQUIRED, "created_not_started",
    )
    assert manager.scan() == candidates
    assert running_store.list_events("run-1") == before
    for resolution in (RecoveryResolution.CONFIRMED_FAILED, RecoveryResolution.CONFIRMED_SUCCEEDED):
        with pytest.raises(ValueError):
            manager.reconcile(call.id, resolution)
    assert manager.reconcile(call.id, RecoveryResolution.ABANDON).status is ToolCallStatus.CANCELLED
    event = running_store.list_events("run-1")[-1]
    assert event.type == "recovery.resolved"
    assert event.payload == {"tool_call_id": call.id, "resolution": "abandon", "reason": "created_not_started"}
    assert executor_with_results(running_store, [success_result("fresh")]).submit_subprocess(
        "run-1", "probe", ("probe",),
    ).output == "fresh"


@pytest.mark.parametrize("cancelled", [False, True])
def test_created_abandonment_revalidates_and_rolls_back(running_store, cancelled):
    call = reserve_call(running_store)
    manager = RecoveryManager(running_store, ToolPolicyRegistry())
    assert manager.scan()
    if cancelled:
        running_store.cancel_run("run-1")
    else:
        running_store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    before = running_store.list_events("run-1")
    with pytest.raises(ValueError):
        manager.reconcile(call.id, RecoveryResolution.ABANDON)
    assert running_store.list_events("run-1") == before
    assert running_store.get_run("run-1").status is (RunStatus.CANCELLED if cancelled else RunStatus.RUNNING)


def test_created_abandon_event_failure_keeps_reservation(running_store):
    reserve_call(running_store)
    with sqlite3.connect(running_store.path) as connection:
        connection.execute("""CREATE TRIGGER fail_abandon BEFORE INSERT ON events
            WHEN NEW.type = 'recovery.resolved'
            BEGIN SELECT RAISE(ABORT, 'injected abandon failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected abandon failure"):
        RecoveryManager(running_store, ToolPolicyRegistry()).reconcile("call-1", RecoveryResolution.ABANDON)
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.CREATED


@pytest.mark.parametrize("reader", [
    lambda s: s.get_run("run-1"), lambda s: s.list_runs(), lambda s: s.list_tool_calls(),
    lambda s: s.get_tool_call("call-1"), lambda s: s.list_events("run-1"),
    lambda s: s.get_interruption_reason("call-1"),
    lambda s: s.get_approval("approval-1"), lambda s: s.get_approval_for_tool_call("call-1"),
])
def test_store_read_releases_database_handle(running_store, monkeypatch, reader):
    reserve_call(running_store)
    running_store.request_approval("call-1", arguments_summary="probe", workspace=".",
                                   risk_reason="test", approval_id="approval-1")
    connections = []
    connect = running_store._connect

    def retain_connection():
        connection = connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(running_store, "_connect", retain_connection)
    try:
        reader(running_store)
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.skipif(os.name != "posix", reason="original POSIX process group")
@pytest.mark.parametrize("boundary", ["cancel", "deadline", "normal"])
def test_redirected_child_cannot_be_reported_as_confirmed_completion(tmp_path, monkeypatch, boundary):
    cancelled = threading.Event()
    real_communicate = subprocess.Popen.communicate
    real_killpg = os.killpg
    real_clock = processes.monotonic
    parents = []
    boundary_reached = False

    def finish_parent(process, *args, **kwargs):
        nonlocal boundary_reached
        result = real_communicate(process, *args, **kwargs)
        if not boundary_reached:
            parents.append(process)
            boundary_reached = True
            if boundary == "cancel":
                cancelled.set()
            if boundary != "normal":
                raise subprocess.TimeoutExpired(process.args, 0.05)
        return result

    def deny_signal(pgid, sig):
        if sig:
            raise PermissionError("injected signal refusal")
        return real_killpg(pgid, sig)

    def deadline_clock():
        return real_clock() + (10 if boundary_reached and boundary == "deadline" else 0)

    monkeypatch.setattr(subprocess.Popen, "communicate", finish_parent)
    monkeypatch.setattr(os, "killpg", deny_signal)
    monkeypatch.setattr(processes, "monotonic", deadline_clock)
    child = "import time; time.sleep(10)"
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    try:
        result = ManagedProcessRunner().run(
            ProcessSpec((sys.executable, "-c", parent), tmp_path, 0.3, 1000, 0), cancelled,
        )
        real_killpg(parents[0].pid, 0)  # The child really remains in the original group.
        assert result.failure_kind is FailureKind.TERMINATION_UNKNOWN
        assert result.termination_confirmed is False
        assert result.exit_code is None
    finally:
        for process in parents:
            try:
                real_killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
