import sqlite3

from corecoder.runtime.state import RunStatus
from corecoder.runtime.store import SQLiteStore


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
