"""The minimal CLI loads a fixed JSON task into the production runtime."""

import json
import sys

from corecoder.reliagent_cli import main


def test_cli_runs_a_json_task_and_persists_a_run(tmp_path, capsys):
    task_path = tmp_path / "task.json"
    database = tmp_path / "runtime.sqlite"
    task_path.write_text(json.dumps({
        "goal": "print a marker",
        "workspace": str(tmp_path),
        "steps": [{
            "tool_name": "local_read_only",
            "argv": [sys.executable, "-c", "print('ok')"],
        }],
    }))

    assert main(["run", str(task_path), "--database", str(database)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "succeeded"
    assert output["run_id"]
