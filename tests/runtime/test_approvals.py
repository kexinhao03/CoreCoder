from dataclasses import FrozenInstanceError

import pytest

from corecoder.runtime.approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    summarize_arguments,
)
from corecoder.runtime.state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus


def create_mutating_call(store, call_id="call-1"):
    return store.create_tool_call(
        run_id="run-1",
        tool_name="bash",
        arguments={"argv": ["pytest"]},
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False,
        idempotency_key=None,
        timeout_seconds=30,
        tool_call_id=call_id,
    )


def test_approval_record_is_frozen():
    approval = ApprovalRecord(
        id="approval-1",
        tool_call_id="call-1",
        status=ApprovalStatus.PENDING,
        decision=None,
        tool_name="bash",
        arguments_summary='{"argv": ["pytest"]}',
        workspace="/tmp/work",
        risk_reason="runs a command",
        requested_at="t0",
        resolved_at=None,
    )

    with pytest.raises(FrozenInstanceError):
        approval.status = ApprovalStatus.APPROVED


def test_argument_summary_redacts_sensitive_keys_and_is_bounded():
    arguments = {
        "argv": ["client", "--verbose"],
        "api_key": "secret-value",
        "nested": {"password": "hidden", "text": "x" * 2000},
    }
    original_nested = arguments["nested"].copy()

    summary = summarize_arguments(arguments)

    assert "secret-value" not in summary
    assert "hidden" not in summary
    assert "[REDACTED]" in summary
    assert len(summary) == 1000
    assert arguments == {
        "argv": ["client", "--verbose"],
        "api_key": "secret-value",
        "nested": original_nested,
    }


def test_argument_summary_redacts_sensitive_keys_nested_in_tuples():
    arguments = {"items": ({"password": "hidden"},)}

    summary = summarize_arguments(arguments)

    assert summary == '{"items": [{"password": "[REDACTED]"}]}'
    assert arguments == {"items": ({"password": "hidden"},)}


def test_argument_summary_normalizes_mixed_basic_key_types():
    arguments = {1: "integer", "1": "string"}

    summary = summarize_arguments(arguments)

    assert summary == '{"1": "string", "[int:1]": "integer"}'
    assert arguments == {1: "integer", "1": "string"}


def test_argument_summary_suffixes_colliding_typed_key_labels():
    arguments = {1: "integer", "[int:1]": "string"}

    summary = summarize_arguments(arguments)

    assert summary == '{"[int:1]": "string", "[int:1]#2": "integer"}'
    assert arguments == {1: "integer", "[int:1]": "string"}


def test_approval_values_are_stable():
    assert ApprovalStatus.PENDING.value == "pending"
    assert ApprovalDecision.ALLOW_ONCE.value == "allow_once"


def test_request_approval_updates_run_call_and_event(running_store):
    create_mutating_call(running_store)

    approval = running_store.request_approval(
        "call-1",
        arguments_summary='{"argv": ["pytest"]}',
        workspace="/tmp/work",
        risk_reason="runs a command",
        approval_id="approval-1",
    )

    assert approval.status is ApprovalStatus.PENDING
    assert running_store.get_approval("approval-1") == approval
    assert running_store.get_approval_for_tool_call("call-1") == approval
    assert running_store.get_approval_for_tool_call("missing") is None
    assert running_store.get_run("run-1").status is RunStatus.WAITING_APPROVAL
    assert (
        running_store.get_tool_call("call-1").status
        is ToolCallStatus.WAITING_APPROVAL
    )
    event = running_store.list_events("run-1")[-1]
    assert event.type == "approval.requested"
    assert event.payload == {
        "approval_id": "approval-1",
        "risk_reason": "runs a command",
        "tool_call_id": "call-1",
        "tool_name": "bash",
    }


def test_allow_once_resolves_approval_but_does_not_start_call(running_store):
    create_mutating_call(running_store)
    running_store.request_approval(
        "call-1",
        arguments_summary="pytest",
        workspace="/tmp/work",
        risk_reason="runs a command",
        approval_id="approval-1",
    )

    resolved = running_store.resolve_approval(
        "approval-1", ApprovalDecision.ALLOW_ONCE
    )

    assert resolved.status is ApprovalStatus.APPROVED
    assert resolved.decision is ApprovalDecision.ALLOW_ONCE
    assert resolved.resolved_at is not None
    assert running_store.get_run("run-1").status is RunStatus.RUNNING
    assert (
        running_store.get_tool_call("call-1").status
        is ToolCallStatus.WAITING_APPROVAL
    )
    event = running_store.list_events("run-1")[-1]
    assert event.type == "approval.resolved"
    assert event.payload == {
        "approval_id": "approval-1",
        "decision": "allow_once",
        "tool_call_id": "call-1",
    }


def test_denial_cancels_call_and_duplicate_resolution_is_atomic(running_store):
    create_mutating_call(running_store)
    running_store.request_approval(
        "call-1",
        arguments_summary="pytest",
        workspace="/tmp/work",
        risk_reason="runs a command",
        approval_id="approval-1",
    )
    denied = running_store.resolve_approval(
        "approval-1", ApprovalDecision.DENY
    )
    before = running_store.list_events("run-1")

    call = running_store.get_tool_call("call-1")
    assert denied.status is ApprovalStatus.DENIED
    assert call.status is ToolCallStatus.CANCELLED
    assert call.ended_at is not None
    with pytest.raises(ValueError, match="approval is already resolved"):
        running_store.resolve_approval(
            "approval-1", ApprovalDecision.ALLOW_ONCE
        )
    assert running_store.get_approval("approval-1") == denied
    assert running_store.list_events("run-1") == before


def test_missing_approval_id_raises_key_error(running_store):
    with pytest.raises(KeyError, match="missing"):
        running_store.get_approval("missing")
