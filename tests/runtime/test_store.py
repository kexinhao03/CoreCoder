import sqlite3

import pytest

from corecoder.runtime.state import (
    ExecutionKind,
    InvalidTransition,
    RiskLevel,
    RunStatus,
    StepStatus,
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


def test_tool_call_raw_cells_are_redacted_and_not_replayable(store_with_run):
    call = store_with_run.create_tool_call(
        run_id="run-1",
        tool_name="bash",
        arguments={"api_key": "AUDIT_PLAINTEXT_SECRET"},
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True,
        idempotency_key="audit",
        timeout_seconds=120,
        tool_call_id="call-redacted",
    )
    store_with_run.transition_tool_call(
        call.id, ToolCallStatus.RUNNING, "tool.started"
    )
    store_with_run.transition_tool_call(
        call.id,
        ToolCallStatus.SUCCEEDED,
        "tool.completed",
        "output_secret=AUDIT_OUTPUT_SECRET",
    )

    with sqlite3.connect(store_with_run.path) as connection:
        arguments_json, result_summary, replayable = connection.execute(
            "SELECT arguments_json, result_summary, arguments_replayable "
            "FROM tool_calls WHERE id = ?",
            (call.id,),
        ).fetchone()
    assert "AUDIT_PLAINTEXT_SECRET" not in arguments_json
    assert "AUDIT_OUTPUT_SECRET" not in result_summary
    assert replayable == 0


def test_initialize_scrubs_legacy_tool_call_raw_cells(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, workflow TEXT NOT NULL,
                status TEXT NOT NULL, workspace TEXT NOT NULL, model TEXT NOT NULL,
                prompt_version TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, started_at TEXT, ended_at TEXT
            );
            CREATE TABLE tool_calls (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                retry_of TEXT REFERENCES tool_calls(id),
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                execution_kind TEXT NOT NULL,
                idempotent INTEGER NOT NULL,
                idempotency_key TEXT,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                result_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT
            );
            INSERT INTO runs VALUES (
                'legacy-run', 'goal', 'repo_maintenance', 'created', '/tmp',
                'model', 'v1', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', NULL, NULL
            );
            INSERT INTO tool_calls VALUES (
                'legacy-call', 'legacy-run', NULL, 'bash',
                '{"api_key": "AUDIT_PLAINTEXT_SECRET"}', 'read_only',
                'subprocess', 1, 'legacy', 'succeeded', 1, 120,
                'output_secret=AUDIT_OUTPUT_SECRET',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', NULL, NULL
            );
            """
        )

    store = SQLiteStore(database)
    store.initialize()

    with sqlite3.connect(store.path) as connection:
        arguments_json, result_summary, replayable = connection.execute(
            "SELECT arguments_json, result_summary, arguments_replayable "
            "FROM tool_calls WHERE id = ?",
            ("legacy-call",),
        ).fetchone()
    assert "AUDIT_PLAINTEXT_SECRET" not in arguments_json
    assert "AUDIT_OUTPUT_SECRET" not in result_summary
    assert replayable == 0


def test_initialize_migrates_legacy_steps_without_inventing_identity(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, goal TEXT NOT NULL, workflow TEXT NOT NULL,
                status TEXT NOT NULL, workspace TEXT NOT NULL, model TEXT NOT NULL,
                prompt_version TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, started_at TEXT, ended_at TEXT
            );
            CREATE TABLE steps (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                sequence INTEGER NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
                UNIQUE (run_id, sequence)
            );
            INSERT INTO runs VALUES (
                'legacy-run', 'goal', 'fixed_task', 'created', '/tmp', 'model',
                'v1', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', NULL, NULL
            );
            INSERT INTO steps VALUES (
                'legacy-step', 'legacy-run', 1, 'legacy', 'pending', 0,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', NULL, NULL
            );
            """
        )

    store = SQLiteStore(database)
    store.initialize()

    [step] = store.list_steps("legacy-run")
    assert step.step_key is None
    assert step.definition_version is None
    assert step.definition_hash is None


def test_versioned_step_identity_is_durable_and_unique(store_with_run):
    step = store_with_run.create_step(
        "run-1",
        sequence=1,
        title="Run experiment",
        step_key="run_experiment",
        definition_version="1",
        definition_hash="a" * 64,
        step_id="step-1",
    )

    assert step.step_key == "run_experiment"
    assert step.definition_version == "1"
    assert step.definition_hash == "a" * 64
    assert store_with_run.list_steps("run-1") == [step]
    with pytest.raises(ValueError, match="step key already exists"):
        store_with_run.create_step(
            "run-1",
            sequence=2,
            title="Duplicate",
            step_key="run_experiment",
            definition_version="1",
            definition_hash="a" * 64,
        )


def test_completed_workflow_reconciliation_rolls_back_on_event_failure(tmp_path):
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    store.initialize()
    store.create_run(
        goal="experiment",
        workflow="ml_experiment",
        workspace=tmp_path,
        model="deterministic",
        prompt_version="ml-experiment-v1",
        run_id="run-ml",
    )
    store.transition_run("run-ml", RunStatus.RUNNING, "run.started")
    step = store.create_step(
        "run-ml",
        sequence=1,
        title="experiment",
        step_key="run_experiment",
        definition_version="1",
        definition_hash="a" * 64,
    )
    store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    call = store.create_tool_call(
        run_id="run-ml",
        step_id=step.id,
        tool_name="ml_experiment.run_experiment",
        arguments={"argv": ["train"]},
        risk_level=RiskLevel.EXTERNAL_EFFECT,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False,
        idempotency_key=None,
        timeout_seconds=30,
    )
    store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    store.transition_tool_call(call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted")
    before = (
        store.get_run("run-ml"),
        store.get_tool_call(call.id),
        store.list_steps("run-ml"),
        store.list_events("run-ml"),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_resumed_event BEFORE INSERT ON events
            WHEN NEW.type = 'run.resumed'
            BEGIN SELECT RAISE(ABORT, 'injected reconciliation failure'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected reconciliation failure"):
        store.reconcile_workflow_completed(call.id, {"verified": True})

    assert (
        store.get_run("run-ml"),
        store.get_tool_call(call.id),
        store.list_steps("run-ml"),
        store.list_events("run-ml"),
    ) == before
    assert store.list_reconciliation_evidence(call.id) == []
    with pytest.raises(ValueError, match="dedicated reconciliation"):
        store.transition_tool_call(call.id, ToolCallStatus.SUCCEEDED, "tool.completed")
    with pytest.raises(ValueError, match="dedicated reconciliation"):
        store.reconcile_interrupted_tool_call(
            call.id, ToolCallStatus.SUCCEEDED, "confirmed_succeeded"
        )


@pytest.mark.parametrize("status", [ToolCallStatus.CREATED, ToolCallStatus.WAITING_APPROVAL,
                                   ToolCallStatus.RUNNING, ToolCallStatus.SUCCEEDED])
def test_cancel_run_settles_only_unstarted_calls_atomically(store_with_run, status):
    store = store_with_run
    store.transition_run("run-1", RunStatus.RUNNING, "run.started")
    create_test_call(store, "call-1")
    if status is ToolCallStatus.WAITING_APPROVAL:
        store.request_approval("call-1", arguments_summary="safe", workspace=".", risk_reason="test")
    elif status in {ToolCallStatus.RUNNING, ToolCallStatus.SUCCEEDED}:
        store.transition_tool_call("call-1", ToolCallStatus.RUNNING, "tool.started")
        if status is ToolCallStatus.SUCCEEDED:
            store.transition_tool_call("call-1", status, "tool.completed")
    before = store.get_tool_call("call-1")
    events_before = store.list_events("run-1")
    store.cancel_run("run-1")
    run = store.get_run("run-1")
    call = store.get_tool_call("call-1")
    events = store.list_events("run-1")[len(events_before):]
    assert run.status is RunStatus.CANCELLED
    if status in {ToolCallStatus.CREATED, ToolCallStatus.WAITING_APPROVAL}:
        assert call.status is ToolCallStatus.CANCELLED
        assert call.ended_at == call.updated_at == run.updated_at
        assert call.started_at is None
        assert [(e.type, e.payload) for e in events] == [
            ("tool.cancelled", {"tool_call_id": "call-1"}), ("run.cancelled", {}),
        ]
    else:
        assert call == before
        assert [(e.type, e.payload) for e in events] == [("run.cancelled", {})]
    assert [e.sequence for e in store.list_events("run-1")] == list(
        range(1, len(events_before) + len(events) + 1)
    )


def test_cancel_run_rolls_back_all_changes_if_event_insert_fails(store_with_run):
    store = store_with_run
    create_test_call(store, "call-1")
    before = (store.get_run("run-1"), store.get_tool_call("call-1"), store.list_events("run-1"))
    with sqlite3.connect(store.path) as connection:
        connection.execute("""
            CREATE TRIGGER fail_cancel_event BEFORE INSERT ON events
            WHEN NEW.type = 'run.cancelled'
            BEGIN SELECT RAISE(ABORT, 'injected event failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        store.cancel_run("run-1")
    assert (store.get_run("run-1"), store.get_tool_call("call-1"), store.list_events("run-1")) == before


def test_cancel_run_validates_transition_without_writes(store_with_run):
    store = store_with_run
    store.transition_run("run-1", RunStatus.RUNNING, "run.started")
    store.transition_run("run-1", RunStatus.SUCCEEDED, "run.completed")
    before = store.list_events("run-1")
    with pytest.raises(InvalidTransition):
        store.cancel_run("run-1")
    assert store.get_run("run-1").status is RunStatus.SUCCEEDED
    assert store.list_events("run-1") == before


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


def test_run_records_start_and_end_timestamps(store_with_run):
    created = store_with_run.get_run("run-1")

    running = store_with_run.transition_run(
        "run-1", RunStatus.RUNNING, "run.started"
    )
    completed = store_with_run.transition_run(
        "run-1", RunStatus.SUCCEEDED, "run.completed"
    )

    assert created.started_at is None
    assert created.ended_at is None
    assert running.started_at is not None
    assert running.ended_at is None
    assert completed.started_at == running.started_at
    assert completed.ended_at is not None


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
