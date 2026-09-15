from dataclasses import FrozenInstanceError

import pytest

from corecoder.runtime.models import EventRecord, RunRecord, ToolCallRecord
from corecoder.runtime.state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus


def test_run_record_is_immutable():
    run = RunRecord(
        "r1",
        "fix tests",
        "repo_maintenance",
        RunStatus.CREATED,
        "/tmp/work",
        "test-model",
        "v1",
        "t0",
        "t0",
    )

    with pytest.raises(FrozenInstanceError):
        run.status = RunStatus.RUNNING


def test_tool_call_record_keeps_retry_lineage():
    call = ToolCallRecord(
        id="c2",
        run_id="r1",
        retry_of="c1",
        tool_name="bash",
        arguments={"command": "pytest"},
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False,
        idempotency_key=None,
        status=ToolCallStatus.CREATED,
        attempt=2,
        timeout_seconds=120,
        result_summary=None,
        created_at="t1",
        updated_at="t1",
        started_at=None,
        ended_at=None,
    )

    assert call.retry_of == "c1"
    assert call.attempt == 2


def test_event_payload_is_decoded_data():
    event = EventRecord(1, "r1", 1, "run.created", {"source": "cli"}, "t0")

    assert event.payload == {"source": "cli"}
