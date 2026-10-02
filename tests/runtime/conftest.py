import pytest

from corecoder.runtime.state import RunStatus
from corecoder.runtime.store import SQLiteStore


@pytest.fixture
def running_store(tmp_path):
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
    store.transition_run("run-1", RunStatus.RUNNING, "run.started")
    return store
