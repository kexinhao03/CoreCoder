"""Read-only, JSON-ready exports of persisted runtime facts."""

from __future__ import annotations

from dataclasses import asdict

from .redaction import redact
from .state import ToolCallStatus
from .store import SQLiteStore


class TraceService:
    """Export a stable, redacted view without changing stored facts."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def export_run(self, run_id: str) -> dict:
        run = self._store.get_run(run_id)
        events = self._store.list_events(run_id)
        calls = self._store.list_tool_calls(run_id)
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
        for call in calls:
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
        return {
            "schema_version": "1.0",
            "run": _record(run),
            "steps": [_record(step) for step in self._store.list_steps(run_id)],
            "tool_calls": [_record(call) for call in calls],
            "approvals": [
                _record(approval) for approval in self._store.list_approvals(run_id)
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
