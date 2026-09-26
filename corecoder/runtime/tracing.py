"""Read-only, JSON-ready exports of persisted runtime facts."""

from __future__ import annotations

from dataclasses import asdict

from .approvals import ApprovalStatus
from .redaction import redact
from .state import RunStatus, StepStatus, ToolCallStatus
from .store import SQLiteStore


class TraceService:
    """Export a stable, redacted view without changing stored facts."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def export_run(self, run_id: str) -> dict:
        run = self._store.get_run(run_id)
        events = self._store.list_events(run_id)
        calls = self._store.list_tool_calls(run_id)
        steps = self._store.list_steps(run_id)
        approvals = self._store.list_approvals(run_id)
        required_events = {
            ToolCallStatus.SUCCEEDED: ("tool.completed", "tool.reconciled"),
            ToolCallStatus.FAILED: ("tool.failed",),
            ToolCallStatus.TIMED_OUT: ("tool.timed_out",),
            ToolCallStatus.CANCELLED: ("tool.cancelled",),
            ToolCallStatus.INTERRUPTED: ("tool.interrupted",),
        }
        recovery_resolutions = {
            ToolCallStatus.SUCCEEDED: "confirmed_succeeded",
            ToolCallStatus.FAILED: "confirmed_failed",
            ToolCallStatus.CANCELLED: "abandon",
        }
        missing = []
        if not any(event.type == "run.created" for event in events):
            missing.append({"run_id": run.id, "event": "run.created"})
        if run.started_at is not None and not any(
            event.type == "run.started" for event in events
        ):
            missing.append({"run_id": run.id, "event": "run.started"})
        for call in calls:
            if not any(
                event.type == "tool.created"
                and event.payload.get("tool_call_id") == call.id
                for event in events
            ):
                missing.append({"tool_call_id": call.id, "event": "tool.created"})
            if call.started_at is not None and not any(
                event.type == "tool.started"
                and event.payload.get("tool_call_id") == call.id
                for event in events
            ):
                missing.append({"tool_call_id": call.id, "event": "tool.started"})
            required = required_events.get(call.status)
            if required and not any(
                event.payload.get("tool_call_id") == call.id
                and (
                    event.type in required
                    or (
                        event.type == "recovery.resolved"
                        and event.payload.get("resolution")
                        == recovery_resolutions.get(call.status)
                    )
                    or (
                        call.status is ToolCallStatus.CANCELLED
                        and event.type == "approval.resolved"
                        and event.payload.get("decision") == "deny"
                    )
                )
                for event in events
            ):
                missing.append({"tool_call_id": call.id, "event": required[0]})
        run_events = {
            RunStatus.SUCCEEDED: "run.completed",
            RunStatus.FAILED: "run.failed",
            RunStatus.CANCELLED: "run.cancelled",
        }
        required_run_event = run_events.get(run.status)
        if required_run_event and not any(
            event.type == required_run_event for event in events
        ):
            missing.append({"run_id": run.id, "event": required_run_event})
        step_events = {
            StepStatus.SUCCEEDED: "step.completed",
            StepStatus.FAILED: "step.failed",
            StepStatus.CANCELLED: "step.cancelled",
            StepStatus.SKIPPED: "step.skipped",
        }
        for step in steps:
            if not any(
                event.type == "step.created"
                and event.payload.get("step_id") == step.id
                for event in events
            ):
                missing.append({"step_id": step.id, "event": "step.created"})
            if step.started_at is not None and not any(
                event.type == "step.started"
                and event.payload.get("step_id") == step.id
                for event in events
            ):
                missing.append({"step_id": step.id, "event": "step.started"})
            required_step_event = step_events.get(step.status)
            if required_step_event and not any(
                event.type == required_step_event
                and event.payload.get("step_id") == step.id
                for event in events
            ):
                missing.append(
                    {"step_id": step.id, "event": required_step_event}
                )
        for approval in approvals:
            if not any(
                event.type == "approval.requested"
                and event.payload.get("approval_id") == approval.id
                for event in events
            ):
                missing.append(
                    {"approval_id": approval.id, "event": "approval.requested"}
                )
            if approval.status is not ApprovalStatus.PENDING and not any(
                event.type == "approval.resolved"
                and event.payload.get("approval_id") == approval.id
                for event in events
            ):
                missing.append(
                    {"approval_id": approval.id, "event": "approval.resolved"}
                )
        return {
            "schema_version": "1.0",
            "run": _record(run),
            "steps": [_record(step) for step in steps],
            "tool_calls": [_record(call) for call in calls],
            "approvals": [
                _record(approval) for approval in approvals
            ],
            "events": [
                {"sequence": event.sequence, "type": event.type, "payload": redact(event.payload), "created_at": event.created_at}
                for event in events
            ],
            "integrity": {"sequence_contiguous": [event.sequence for event in events] == list(range(1, len(events) + 1)), "missing": missing},
            "instrumentation_availability": {"model_lifecycle": "not_instrumented", "token_cost": "not_instrumented"},
        }


def _record(record: object) -> dict:
    value = asdict(record)
    for key, item in list(value.items()):
        if hasattr(item, "value"):
            value[key] = item.value
    return redact(value)
