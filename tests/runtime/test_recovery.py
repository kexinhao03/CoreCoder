import json
import sqlite3
import sys
from dataclasses import replace

import pytest

from corecoder.runtime.approvals import ApprovalDecision, ApprovalStatus
from corecoder.runtime.policies import ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.recovery import (
    RecoveryCandidate,
    RecoveryKind,
    RecoveryManager,
    RecoveryResolution,
)
from corecoder.runtime.state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus
from corecoder.runtime.store import SQLiteStore


@pytest.fixture
def recovery_store(tmp_path):
    store = SQLiteStore(tmp_path / "recovery.db")
    store.initialize()
    return store


def read_policy(**changes):
    return replace(ToolPolicy(
        risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=30, max_attempts=2, idempotent=True, auto_retry=True,
        retryable_failures=frozenset(), output_limit=1000,
    ), **changes)


def make_call(store, call_id, *, risk=RiskLevel.READ_ONLY, idempotent=True,
              tool="read-probe", status=ToolCallStatus.RUNNING, arguments=None):
    run_id = "run-" + call_id
    store.create_run(goal="recover", workflow="test", workspace=store.path.parent,
                     model="test", prompt_version="v1", run_id=run_id)
    store.transition_run(run_id, RunStatus.RUNNING, "run.started")
    store.create_tool_call(run_id=run_id, tool_name=tool,
                           arguments=arguments if arguments is not None else {"argv": ["probe"]},
                           risk_level=risk, execution_kind=ExecutionKind.SUBPROCESS,
                           idempotent=idempotent, idempotency_key="probe-key",
                           timeout_seconds=45, tool_call_id=call_id)
    if status is ToolCallStatus.WAITING_APPROVAL:
        store.request_approval(call_id, arguments_summary="probe", workspace=".",
                               risk_reason="effect", approval_id="approval-" + call_id)
    elif status is not ToolCallStatus.CREATED:
        store.transition_tool_call(call_id, ToolCallStatus.RUNNING, "tool.started")
        if status is not ToolCallStatus.RUNNING:
            store.transition_tool_call(call_id, status, "tool." + status.value)
    return store.get_tool_call(call_id)


@pytest.fixture
def retry_candidate(recovery_store):
    make_call(recovery_store, "read-call")
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": read_policy()}))
    return manager, manager.scan()[0]


def snapshot(store):
    return (store.list_runs(), store.list_tool_calls(),
            [store.list_events(run.id) for run in store.list_runs()])


def test_scan_requires_human_for_nonreplayable_read_only_orphan(recovery_store):
    make_call(recovery_store, "sensitive", arguments={"argv": ["probe", "--token", "original"]})
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": read_policy()}))
    candidate = manager.scan()[0]
    assert candidate.call.arguments_replayable is False
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    assert candidate.reason == "sensitive_arguments_not_replayable"
    before = snapshot(recovery_store)
    with pytest.raises(ValueError, match="not eligible"):
        manager.resume_retry(candidate)
    assert snapshot(recovery_store) == before


def test_store_atomically_refuses_nonreplayable_recovery_retry(recovery_store):
    make_call(recovery_store, "sensitive", arguments={"argv": ["probe", "--token", "original"]})
    registry = ToolPolicyRegistry({"read-probe": read_policy()})
    candidate = RecoveryManager(recovery_store, registry).scan()[0]
    before = snapshot(recovery_store)
    with pytest.raises(ValueError, match="recovery retry is not safe or permitted"):
        recovery_store.resume_recovery_retry(candidate.run, candidate.call, registry)
    assert snapshot(recovery_store) == before


def test_scan_classifies_safe_and_high_risk_orphans(recovery_store):
    make_call(recovery_store, "read-call")
    make_call(recovery_store, "effect-call", risk=RiskLevel.EXTERNAL_EFFECT,
              idempotent=False, tool="external-probe")
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": read_policy()}))
    candidates = manager.scan()
    assert [(c.call.id, c.kind) for c in candidates] == [
        ("read-call", RecoveryKind.RETRY_ALLOWED), ("effect-call", RecoveryKind.HUMAN_REQUIRED),
    ]
    for candidate in candidates:
        assert candidate.call.status is ToolCallStatus.INTERRUPTED
        assert candidate.run.status is RunStatus.RECOVERABLE
        event = recovery_store.list_events(candidate.run.id)[-1]
        assert (event.type, event.payload) == (
            "tool.interrupted", {"tool_call_id": candidate.call.id, "reason": "process_lost"},
        )
        assert candidate.call.ended_at is None
    before = snapshot(recovery_store)
    assert manager.scan() == candidates
    assert snapshot(recovery_store) == before


def test_scan_distinguishes_pending_and_approved_not_started(recovery_store):
    for call_id in ("pending-call", "ready-call"):
        make_call(recovery_store, call_id, risk=RiskLevel.MUTATING,
                  idempotent=False, status=ToolCallStatus.WAITING_APPROVAL)
    recovery_store.resolve_approval("approval-ready-call", ApprovalDecision.ALLOW_ONCE)
    make_call(recovery_store, "finished", status=ToolCallStatus.SUCCEEDED)
    make_call(recovery_store, "created", status=ToolCallStatus.CREATED)
    before = snapshot(recovery_store)
    candidates = RecoveryManager(recovery_store, ToolPolicyRegistry()).scan()
    assert {c.call.id: c.kind for c in candidates} == {
        "pending-call": RecoveryKind.APPROVAL_PENDING, "ready-call": RecoveryKind.READY_TO_START,
        "created": RecoveryKind.HUMAN_REQUIRED,
    }
    assert recovery_store.get_approval("approval-pending-call").status is ApprovalStatus.PENDING
    assert snapshot(recovery_store) == before


@pytest.mark.parametrize("status", [ToolCallStatus.CREATED, ToolCallStatus.WAITING_APPROVAL])
def test_scan_requires_human_for_nonreplayable_not_started_call(recovery_store, status):
    call_id = "sensitive-" + status.value
    make_call(
        recovery_store,
        call_id,
        risk=RiskLevel.MUTATING,
        idempotent=False,
        status=status,
        arguments={"argv": ["probe", "--token", "original"]},
    )
    if status is ToolCallStatus.WAITING_APPROVAL:
        recovery_store.resolve_approval(
            "approval-" + call_id, ApprovalDecision.ALLOW_ONCE
        )

    candidate = RecoveryManager(recovery_store, ToolPolicyRegistry()).scan()[0]

    assert candidate.call.arguments_replayable is False
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    assert candidate.reason == "sensitive_arguments_not_replayable"


@pytest.mark.parametrize("changes", [
    {"risk": RiskLevel.MUTATING}, {"risk": RiskLevel.EXTERNAL_EFFECT},
    {"idempotent": False}, {"tool": "unknown"},
])
def test_scan_never_trusts_policy_over_persisted_risk(recovery_store, changes):
    make_call(recovery_store, "unsafe", **changes)
    candidates = RecoveryManager(
        recovery_store, ToolPolicyRegistry({"read-probe": read_policy()})
    ).scan()
    assert candidates[0].kind is RecoveryKind.HUMAN_REQUIRED


@pytest.mark.parametrize("policy", [read_policy(auto_retry=False), read_policy(max_attempts=1)])
def test_scan_requires_policy_retry_permission_and_budget(recovery_store, policy):
    make_call(recovery_store, "read-call")
    assert RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": policy})).scan()[0].kind \
        is RecoveryKind.HUMAN_REQUIRED


def test_scan_preserves_cancelled_run_and_refuses_retry(retry_candidate):
    manager, candidate = retry_candidate
    manager.store.cancel_run(candidate.run.id)
    scanned = manager.scan()[0]
    assert scanned.run.status is RunStatus.CANCELLED
    assert scanned.kind is RecoveryKind.HUMAN_REQUIRED
    before = snapshot(manager.store)
    with pytest.raises(ValueError):
        manager.resume_retry(candidate)
    assert snapshot(manager.store) == before


def test_scan_orphans_running_call_even_when_run_cancelled(recovery_store):
    make_call(recovery_store, "cancelled")
    recovery_store.cancel_run("run-cancelled")
    candidate = RecoveryManager(recovery_store, ToolPolicyRegistry()).scan()[0]
    assert candidate.run.status is RunStatus.CANCELLED
    assert candidate.call.status is ToolCallStatus.INTERRUPTED
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED


def test_scan_unknown_termination_requires_human_and_normalizes_run(recovery_store):
    make_call(recovery_store, "unknown", status=ToolCallStatus.INTERRUPTED)
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": read_policy()}))
    candidate = manager.scan()[0]
    assert candidate.run.status is RunStatus.RECOVERABLE
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    before = snapshot(recovery_store)
    assert manager.scan() == [candidate]
    assert snapshot(recovery_store) == before


def test_resume_retry_resolves_source_before_new_attempt(retry_candidate):
    manager, candidate = retry_candidate
    retry = manager.resume_retry(candidate)
    source = manager.store.get_tool_call(candidate.call.id)
    run = manager.store.get_run(candidate.run.id)
    assert source.status is ToolCallStatus.FAILED
    assert source.result_summary == "process_lost"
    assert run.status is RunStatus.RUNNING
    assert source.ended_at == source.updated_at == run.updated_at == retry.created_at
    assert retry.status is ToolCallStatus.CREATED
    assert retry.retry_of == source.id
    assert retry.attempt == 2
    assert (retry.tool_name, retry.arguments, retry.risk_level, retry.execution_kind,
            retry.idempotent, retry.idempotency_key, retry.timeout_seconds) == (
                "read-probe", {"argv": ["probe"]}, RiskLevel.READ_ONLY,
                ExecutionKind.SUBPROCESS, True, "probe-key", 45,
            )
    assert retry.started_at is retry.ended_at is None
    events = manager.store.list_events(run.id)[-3:]
    assert [e.type for e in events] == ["recovery.resolved", "tool.retry_scheduled", "tool.created"]
    assert events[0].payload == {"tool_call_id": source.id, "reason": "process_lost",
                                 "resolution": "confirmed_failed"}
    assert events[1].payload == {"tool_call_id": retry.id, "retry_of": source.id, "attempt": 2}
    before = snapshot(manager.store)
    with pytest.raises(ValueError):
        manager.resume_retry(candidate)
    assert snapshot(manager.store) == before


@pytest.mark.parametrize("forgery", ["kind", "reason", "call", "run", "registry"])
def test_resume_retry_revalidates_supplied_candidate(retry_candidate, forgery):
    manager, candidate = retry_candidate
    if forgery == "kind":
        candidate = replace(candidate, kind=RecoveryKind.HUMAN_REQUIRED)
    elif forgery == "reason":
        candidate = replace(candidate, reason="termination_unknown")
    elif forgery == "call":
        candidate = replace(candidate, call=replace(candidate.call, arguments={"argv": ["other"]}))
    elif forgery == "run":
        candidate = replace(candidate, run=replace(candidate.run, id="other-run"))
    else:
        manager.registry = ToolPolicyRegistry()
    before = snapshot(manager.store)
    with pytest.raises(ValueError):
        manager.resume_retry(candidate)
    assert snapshot(manager.store) == before


@pytest.mark.parametrize("forge_metadata", [False, True])
def test_forged_safe_candidate_cannot_replay_high_risk_call(recovery_store, forge_metadata):
    make_call(recovery_store, "effect", risk=RiskLevel.EXTERNAL_EFFECT, tool="external-probe")
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"external-probe": read_policy()}))
    original = manager.scan()[0]
    call = replace(original.call, risk_level=RiskLevel.READ_ONLY) if forge_metadata else original.call
    forged = RecoveryCandidate(original.run, call,
                               RecoveryKind.RETRY_ALLOWED, "process_lost")
    before = snapshot(recovery_store)
    with pytest.raises(ValueError):
        manager.resume_retry(forged)
    assert snapshot(recovery_store) == before


def test_forged_process_lost_reason_cannot_replay_unknown_termination(recovery_store):
    make_call(recovery_store, "unknown", status=ToolCallStatus.INTERRUPTED)
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": read_policy()}))
    candidate = replace(manager.scan()[0], kind=RecoveryKind.RETRY_ALLOWED, reason="process_lost")
    before = snapshot(recovery_store)
    with pytest.raises(ValueError):
        manager.resume_retry(candidate)
    assert snapshot(recovery_store) == before


def test_scan_and_resume_never_execute_persisted_subprocess(recovery_store, tmp_path):
    make_call(recovery_store, "read-call")
    marker = tmp_path / "should-not-exist"
    arguments = {"argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]}
    with sqlite3.connect(recovery_store.path) as connection:
        connection.execute("UPDATE tool_calls SET arguments_json = ? WHERE id = ?",
                           (json.dumps(arguments), "read-call"))
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": read_policy()}))
    retry = manager.resume_retry(manager.scan()[0])
    assert retry.arguments == arguments
    assert retry.status is ToolCallStatus.CREATED
    assert not marker.exists()


def test_reconciliation_keeps_old_approval_bound_to_original_attempt(recovery_store):
    make_call(recovery_store, "effect", risk=RiskLevel.EXTERNAL_EFFECT, idempotent=False,
              tool="external-probe", status=ToolCallStatus.WAITING_APPROVAL)
    recovery_store.resolve_approval("approval-effect", ApprovalDecision.ALLOW_ONCE)
    approval = recovery_store.get_approval("approval-effect")
    recovery_store.transition_tool_call("effect", ToolCallStatus.RUNNING, "tool.started")
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry())
    candidate = manager.scan()[0]
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    manager.reconcile("effect", RecoveryResolution.CONFIRMED_FAILED)
    assert recovery_store.get_approval("approval-effect") == approval
    assert [c.id for c in recovery_store.list_tool_calls()] == ["effect"]


@pytest.mark.parametrize("policy", [read_policy(auto_retry=False), read_policy(max_attempts=1)])
def test_resume_checks_changed_current_policy(retry_candidate, policy):
    manager, candidate = retry_candidate
    manager.registry = ToolPolicyRegistry({"read-probe": policy})
    before = snapshot(manager.store)
    with pytest.raises(ValueError):
        manager.resume_retry(candidate)
    assert snapshot(manager.store) == before


@pytest.mark.parametrize(("resolution", "status"), [
    (RecoveryResolution.CONFIRMED_SUCCEEDED, ToolCallStatus.SUCCEEDED),
    (RecoveryResolution.CONFIRMED_FAILED, ToolCallStatus.FAILED),
    (RecoveryResolution.ABANDON, ToolCallStatus.CANCELLED),
])
@pytest.mark.parametrize("cancelled", [False, True])
def test_human_reconciliation_never_creates_retry(recovery_store, resolution, status, cancelled):
    make_call(recovery_store, "effect", risk=RiskLevel.EXTERNAL_EFFECT,
              idempotent=False, tool="external-probe")
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry())
    candidate = manager.scan()[0]
    if cancelled:
        recovery_store.cancel_run(candidate.run.id)
    before_ids = {c.id for c in recovery_store.list_tool_calls()}
    resolved = manager.reconcile(candidate.call.id, resolution)
    assert resolved.status is status
    assert resolved.ended_at == resolved.updated_at
    assert recovery_store.get_run(candidate.run.id).status is (
        RunStatus.CANCELLED if cancelled else RunStatus.RUNNING
    )
    assert {c.id for c in recovery_store.list_tool_calls()} == before_ids
    assert recovery_store.get_approval_for_tool_call(resolved.id) is None
    assert recovery_store.list_events(candidate.run.id)[-1].payload == {
        "tool_call_id": resolved.id, "resolution": resolution.value,
    }


@pytest.mark.parametrize("status", [ToolCallStatus.RUNNING, ToolCallStatus.WAITING_APPROVAL,
                                   ToolCallStatus.SUCCEEDED])
def test_reconcile_rejects_non_interrupted_without_writes(recovery_store, status):
    make_call(recovery_store, "call", status=status)
    before = snapshot(recovery_store)
    with pytest.raises(ValueError):
        RecoveryManager(recovery_store, ToolPolicyRegistry()).reconcile(
            "call", RecoveryResolution.ABANDON,
        )
    assert snapshot(recovery_store) == before


@pytest.mark.parametrize("operation", ["orphan", "resume", "reconcile"])
def test_recovery_transactions_roll_back_when_final_event_fails(recovery_store, operation):
    make_call(recovery_store, "call")
    manager = RecoveryManager(recovery_store, ToolPolicyRegistry({"read-probe": read_policy()}))
    candidate = manager.scan()[0] if operation != "orphan" else None
    event_type = {"orphan": "tool.interrupted", "resume": "tool.created",
                  "reconcile": "recovery.resolved"}[operation]
    before = snapshot(recovery_store)
    with sqlite3.connect(recovery_store.path) as connection:
        connection.execute(f"""
            CREATE TRIGGER fail_recovery BEFORE INSERT ON events
            WHEN NEW.type = '{event_type}'
            BEGIN SELECT RAISE(ABORT, 'injected recovery failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="injected recovery failure"):
        if operation == "orphan":
            recovery_store.mark_orphaned_tool_call("call")
        elif operation == "resume":
            manager.resume_retry(candidate)
        else:
            manager.reconcile("call", RecoveryResolution.ABANDON)
    assert snapshot(recovery_store) == before


def test_store_status_queries_filter_and_empty_sets_return_no_records(recovery_store):
    make_call(recovery_store, "running")
    make_call(recovery_store, "done", status=ToolCallStatus.SUCCEEDED)
    assert [r.id for r in recovery_store.list_runs([RunStatus.RUNNING])] == ["run-running", "run-done"]
    assert [c.id for c in recovery_store.list_tool_calls(statuses=[ToolCallStatus.RUNNING])] == ["running"]
    assert [c.id for c in recovery_store.list_tool_calls(run_id="run-done")] == ["done"]
    assert recovery_store.list_tool_calls(run_id="run-done", statuses=[ToolCallStatus.RUNNING]) == []
    assert recovery_store.list_tool_calls(statuses=[]) == []
    assert recovery_store.list_runs([]) == []


def test_orphan_transaction_requires_running_without_writes(retry_candidate):
    manager, candidate = retry_candidate
    before = snapshot(manager.store)
    with pytest.raises(ValueError):
        manager.store.mark_orphaned_tool_call(candidate.call.id)
    assert snapshot(manager.store) == before
