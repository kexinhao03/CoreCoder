"""Trace export is stable, read-only, and safe for sharing."""

import sqlite3

from corecoder.runtime.approvals import ApprovalDecision
from corecoder.runtime.state import ExecutionKind, RiskLevel, ToolCallStatus
from corecoder.runtime.tracing import TraceService


def test_trace_export_orders_events_and_redacts_legacy_payload(running_store):
    running_store.record_event("run-1", "test.payload", {"safe": "visible", "token": "plain"})

    export = TraceService(running_store).export_run("run-1")

    assert export["schema_version"] == "1.0"
    assert [event["sequence"] for event in export["events"]] == list(range(1, len(export["events"]) + 1))
    assert export["events"][-1]["payload"] == {"safe": "visible", "token": "[REDACTED]"}


def test_trace_reports_sequence_gap_as_incomplete(running_store):
    running_store.record_event("run-1", "test.first", {})
    running_store.record_event("run-1", "test.second", {})
    with sqlite3.connect(running_store.path) as connection:
        connection.execute("DELETE FROM events WHERE run_id = ? AND sequence = ?", ("run-1", 2))

    integrity = TraceService(running_store).export_run("run-1")["integrity"]

    assert integrity["sequence_contiguous"] is False


def test_trace_lists_missing_terminal_tool_event(running_store):
    call = running_store.create_tool_call(
        run_id="run-1", tool_name="probe", arguments={"argv": ["probe"]},
        risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True, idempotency_key="key", timeout_seconds=2,
    )
    running_store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    with sqlite3.connect(running_store.path) as connection:
        connection.execute("UPDATE tool_calls SET status = 'succeeded' WHERE id = ?", (call.id,))

    integrity = TraceService(running_store).export_run("run-1")["integrity"]

    assert integrity["missing"] == [{"tool_call_id": call.id, "event": "tool.completed"}]


def test_trace_accepts_confirmed_failed_recovery_as_terminal_evidence(running_store):
    call = running_store.create_tool_call(
        run_id="run-1", tool_name="probe", arguments={"argv": ["probe"]},
        risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True, idempotency_key="key", timeout_seconds=2,
    )
    running_store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    running_store.transition_tool_call(
        call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted"
    )
    running_store.reconcile_interrupted_tool_call(
        call.id, ToolCallStatus.FAILED, "confirmed_failed"
    )

    integrity = TraceService(running_store).export_run("run-1")["integrity"]

    assert integrity["missing"] == []


def test_trace_accepts_approval_denial_as_cancelled_terminal_evidence(running_store):
    call = running_store.create_tool_call(
        run_id="run-1", tool_name="mutate", arguments={"argv": ["mutate"]},
        risk_level=RiskLevel.MUTATING, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False, idempotency_key=None, timeout_seconds=2,
    )
    approval = running_store.request_approval(
        call.id,
        arguments_summary="mutate",
        workspace=".",
        risk_reason="mutation",
    )
    running_store.resolve_approval(approval.id, ApprovalDecision.DENY)

    integrity = TraceService(running_store).export_run("run-1")["integrity"]

    assert integrity["missing"] == []


def test_trace_exports_approval_records(running_store):
    call = running_store.create_tool_call(
        run_id="run-1", tool_name="edit-config", arguments={"argv": ["edit"]},
        risk_level=RiskLevel.MUTATING, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True, idempotency_key="config", timeout_seconds=2,
    )
    approval = running_store.request_approval(
        call.id, arguments_summary="safe", workspace=".", risk_reason="mutates config"
    )

    export = TraceService(running_store).export_run("run-1")

    assert export["approvals"] == [{
        "id": approval.id,
        "tool_call_id": call.id,
        "status": "pending",
        "decision": None,
        "tool_name": "edit-config",
        "arguments_summary": "safe",
        "workspace": ".",
        "risk_reason": "mutates config",
        "requested_at": approval.requested_at,
        "resolved_at": None,
    }]
