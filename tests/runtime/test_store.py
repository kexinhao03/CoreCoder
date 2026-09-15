import sqlite3

import pytest

from corecoder.runtime.state import InvalidTransition, RunStatus
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
    assert {"runs", "tool_calls", "events"} <= names


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
