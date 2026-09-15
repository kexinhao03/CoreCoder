import sqlite3

import pytest

from corecoder.runtime.state import (
    ExecutionKind,
    InvalidTransition,
    RiskLevel,
    RunStatus,
    ToolCallStatus,
)
from corecoder.runtime.store import SQLiteStore


@pytest.fixture
def store_with_run(tmp_path):
    store = SQLiteStore(tmp_path / "runs.db")
    store.initialize()
    store.create_run(
        goal="fix failing test",
        workflow="repo_maintenance",
        workspace=tmp_path,
        model="test-model",
        prompt_version="v1",
        run_id="run-1",
    )
    return store


def create_test_call(
    store,
    tool_call_id,
    *,
    idempotent=True,
    retry_of=None,
):
    return store.create_tool_call(
        run_id="run-1",
        tool_name="bash",
        arguments={"command": "pytest -q"},
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=idempotent,
        idempotency_key="tests" if idempotent else None,
        timeout_seconds=120,
        retry_of=retry_of,
        tool_call_id=tool_call_id,
    )


def test_initialize_creates_runtime_tables(tmp_path):
    db = tmp_path / "runs.db"

    SQLiteStore(db).initialize()

    with sqlite3.connect(db) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"runs", "tool_calls", "approvals", "events"} <= names


def test_create_run_persists_state_and_created_event(tmp_path):
    store = SQLiteStore(tmp_path / "runs.db")
    store.initialize()

    run = store.create_run(
        goal="fix failing test",
        workflow="repo_maintenance",
        workspace=tmp_path,
        model="test-model",
        prompt_version="v1",
        run_id="run-1",
    )

    assert run.status is RunStatus.CREATED
    assert store.get_run("run-1") == run
    events = store.list_events("run-1")
    assert [(event.sequence, event.type) for event in events] == [
        (1, "run.created")
    ]


def test_run_transition_updates_state_and_appends_event(store_with_run):
    updated = store_with_run.transition_run(
        "run-1",
        RunStatus.RUNNING,
        "run.started",
        {"source": "test"},
    )

    assert updated.status is RunStatus.RUNNING
    assert [
        (event.sequence, event.type)
        for event in store_with_run.list_events("run-1")
    ] == [(1, "run.created"), (2, "run.started")]


def test_invalid_transition_writes_neither_state_nor_event(store_with_run):
    store_with_run.transition_run("run-1", RunStatus.RUNNING, "run.started")
    store_with_run.transition_run(
        "run-1", RunStatus.SUCCEEDED, "run.completed"
    )
    before = store_with_run.list_events("run-1")

    with pytest.raises(InvalidTransition):
        store_with_run.transition_run(
            "run-1", RunStatus.RUNNING, "run.restarted"
        )

    assert store_with_run.get_run("run-1").status is RunStatus.SUCCEEDED
    assert store_with_run.list_events("run-1") == before


def test_create_tool_call_persists_metadata_and_event(store_with_run):
    call = store_with_run.create_tool_call(
        run_id="run-1",
        tool_name="bash",
        arguments={"command": "pytest -q"},
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False,
        idempotency_key=None,
        timeout_seconds=120,
        tool_call_id="call-1",
    )

    assert call.status is ToolCallStatus.CREATED
    assert call.attempt == 1
    assert store_with_run.get_tool_call("call-1") == call
    event = store_with_run.list_events("run-1")[-1]
    assert event.type == "tool.created"
    assert event.payload == {
        "attempt": 1,
        "retry_of": None,
        "tool_call_id": "call-1",
        "tool_name": "bash",
    }


def test_run_rejects_second_active_tool_call(store_with_run):
    create_test_call(store_with_run, "call-1")

    with pytest.raises(
        ValueError, match="run already has an active tool call"
    ):
        create_test_call(store_with_run, "call-2")

    assert store_with_run.list_events("run-1")[-1].payload[
        "tool_call_id"
    ] == "call-1"


def test_cancelled_run_rejects_new_tool_call(store_with_run):
    store_with_run.transition_run(
        "run-1", RunStatus.CANCELLED, "run.cancelled"
    )

    with pytest.raises(
        ValueError, match="run cannot accept tool calls: cancelled"
    ):
        create_test_call(store_with_run, "call-1")

    assert store_with_run.list_events("run-1")[-1].type == "run.cancelled"


def test_retry_is_a_new_tool_call_with_lineage(store_with_run):
    first = create_test_call(store_with_run, "call-1")
    store_with_run.transition_tool_call(
        "call-1", ToolCallStatus.RUNNING, "tool.started"
    )
    store_with_run.transition_tool_call(
        "call-1", ToolCallStatus.FAILED, "tool.failed"
    )

    retry = store_with_run.create_tool_call(
        run_id="run-1",
        tool_name=first.tool_name,
        arguments=first.arguments,
        risk_level=first.risk_level,
        execution_kind=first.execution_kind,
        idempotent=first.idempotent,
        idempotency_key=first.idempotency_key,
        timeout_seconds=first.timeout_seconds,
        retry_of="call-1",
        tool_call_id="call-2",
    )

    assert retry.retry_of == "call-1"
    assert retry.attempt == 2
    before = store_with_run.list_events("run-1")
    with pytest.raises(InvalidTransition):
        store_with_run.transition_tool_call(
            "call-1", ToolCallStatus.RUNNING, "tool.retry_started"
        )
    assert store_with_run.get_tool_call("call-1").status is ToolCallStatus.FAILED
    assert store_with_run.list_events("run-1") == before


def test_retry_must_reference_same_run(store_with_run, tmp_path):
    create_test_call(store_with_run, "call-1")
    store_with_run.create_run(
        goal="other",
        workflow="repo_maintenance",
        workspace=tmp_path,
        model="test-model",
        prompt_version="v1",
        run_id="run-2",
    )

    with pytest.raises(ValueError, match="retry_of belongs to another run"):
        store_with_run.create_tool_call(
            run_id="run-2",
            tool_name="bash",
            arguments={"command": "pytest"},
            risk_level=RiskLevel.MUTATING,
            execution_kind=ExecutionKind.SUBPROCESS,
            idempotent=False,
            idempotency_key=None,
            timeout_seconds=120,
            retry_of="call-1",
            tool_call_id="call-2",
        )


def test_non_idempotent_tool_call_cannot_be_retried(store_with_run):
    create_test_call(store_with_run, "call-1", idempotent=False)
    store_with_run.transition_tool_call(
        "call-1", ToolCallStatus.RUNNING, "tool.started"
    )
    store_with_run.transition_tool_call(
        "call-1", ToolCallStatus.FAILED, "tool.failed"
    )

    with pytest.raises(
        ValueError, match="non-idempotent tool call cannot be auto-retried"
    ):
        create_test_call(store_with_run, "call-2", retry_of="call-1")


def test_unfinished_tool_call_cannot_be_retried(store_with_run):
    create_test_call(store_with_run, "call-1")

    with pytest.raises(ValueError, match="retry source is not retryable"):
        create_test_call(store_with_run, "call-2", retry_of="call-1")


def test_tool_transition_tracks_lifecycle_and_safe_event_identity(store_with_run):
    create_test_call(store_with_run, "call-1")

    running = store_with_run.transition_tool_call(
        "call-1", ToolCallStatus.RUNNING, "tool.started"
    )
    failed = store_with_run.transition_tool_call(
        "call-1",
        ToolCallStatus.FAILED,
        "tool.failed",
        result_summary="x" * 2100,
        payload={"exit_code": 1},
    )

    assert running.started_at is not None
    assert running.ended_at is None
    assert failed.ended_at is not None
    assert failed.result_summary == "x" * 2000
    assert store_with_run.list_events("run-1")[-1].payload == {
        "exit_code": 1,
        "tool_call_id": "call-1",
    }


def test_interrupted_tool_call_has_no_end_time(store_with_run):
    create_test_call(store_with_run, "call-1")
    store_with_run.transition_tool_call(
        "call-1", ToolCallStatus.RUNNING, "tool.started"
    )

    interrupted = store_with_run.transition_tool_call(
        "call-1", ToolCallStatus.INTERRUPTED, "tool.interrupted"
    )

    assert interrupted.ended_at is None


def test_runtime_public_api():
    from corecoder.runtime import (
        EventRecord,
        ExecutionKind,
        InvalidTransition,
        RiskLevel,
        RunRecord,
        RunStatus,
        SQLiteStore,
        ToolCallRecord,
        ToolCallStatus,
    )

    assert SQLiteStore is not None
    assert RunStatus.CREATED.value == "created"
    assert ToolCallStatus.INTERRUPTED.value == "interrupted"
    assert all(
        value is not None
        for value in (
            EventRecord,
            ExecutionKind,
            InvalidTransition,
            RiskLevel,
            RunRecord,
            ToolCallRecord,
        )
    )
