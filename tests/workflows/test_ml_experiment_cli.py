from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from corecoder.reliagent_cli import main
from corecoder.workflows.ml_experiment.workflow import MLExperimentWorkflow


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_ml_cli_start_approve_resume_and_report(tmp_path, capsys):
    assert main(["workflow", "ml", "start", "--workspace", str(tmp_path)]) == 0
    created, waiting = _lines(capsys)
    assert created == {"event": "run_created", "run_id": waiting["run_id"]}
    assert waiting["status"] == "waiting_approval"
    assert waiting["pending_approval_id"]

    assert main([
        "approve", waiting["pending_approval_id"], "--workspace", str(tmp_path)
    ]) == 0
    _lines(capsys)
    assert main([
        "workflow", "ml", "resume", waiting["run_id"], "--workspace", str(tmp_path)
    ]) == 0
    [completed] = _lines(capsys)
    assert completed["status"] == "succeeded"
    assert completed["report_available"] is True

    assert main([
        "workflow", "ml", "report", waiting["run_id"], "--workspace", str(tmp_path)
    ]) == 0
    [report] = _lines(capsys)
    assert Path(report["raw_json"]).exists()
    assert Path(report["markdown"]).exists()


@pytest.mark.skipif(os.name == "nt", reason="real os._exit checkpoints are POSIX-only")
def test_ml_cli_real_environment_exit_and_new_process_recovery(tmp_path, capsys):
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "corecoder.reliagent_cli",
            "workflow",
            "ml",
            "start",
            "--workspace",
            str(tmp_path),
            "--inject-process-loss",
            "environment_after_start",
        ],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 86
    [created] = [json.loads(line) for line in process.stdout.splitlines()]

    assert main([
        "workflow", "ml", "resume", created["run_id"], "--workspace", str(tmp_path)
    ]) == 0
    [recovered] = _lines(capsys)
    assert recovered["status"] == "waiting_approval"
    workflow = MLExperimentWorkflow(tmp_path)
    calls = workflow.store.list_tool_calls(created["run_id"])
    assert [call.attempt for call in calls if call.tool_name.endswith("environment_check")] == [
        1,
        2,
    ]


@pytest.mark.skipif(os.name == "nt", reason="real os._exit checkpoints are POSIX-only")
def test_ml_cli_real_after_effect_exit_requires_reconcile(tmp_path, capsys):
    command = [sys.executable, "-m", "corecoder.reliagent_cli"]
    start = subprocess.run(
        [
            *command,
            "workflow",
            "ml",
            "start",
            "--workspace",
            str(tmp_path),
            "--inject-process-loss",
            "experiment_after_effect",
        ],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert start.returncode == 0
    created, waiting = [json.loads(line) for line in start.stdout.splitlines()]
    approval_id = waiting["pending_approval_id"]
    assert main(["approve", approval_id, "--workspace", str(tmp_path)]) == 0
    _lines(capsys)
    crashed = subprocess.run(
        [
            *command,
            "workflow",
            "ml",
            "resume",
            created["run_id"],
            "--workspace",
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 87

    assert main([
        "workflow", "ml", "resume", created["run_id"], "--workspace", str(tmp_path)
    ]) == 0
    [blocked] = _lines(capsys)
    assert blocked["status"] == "recoverable"
    assert blocked["pending_reconciliation_tool_call_id"]
    assert main([
        "reconcile",
        blocked["pending_reconciliation_tool_call_id"],
        "--decision",
        "completed",
        "--workspace",
        str(tmp_path),
    ]) == 0
    [reconciled] = _lines(capsys)
    assert reconciled["decision"] == "completed"


def test_ml_cli_evaluation_writes_twenty_four_real_results(tmp_path, capsys):
    output = tmp_path / "evaluation"

    assert main(["eval", "ml_workflow", "--output", str(output)]) == 0

    [result] = _lines(capsys)
    assert result["total_repetitions"] == 24
    assert result["contract_passed_repetitions"] == 24
    raw = json.loads(Path(result["raw_json"]).read_text())
    assert len(raw["results"]) == 24
    assert {item["config_id"] for item in raw["results"]} == {
        "baseline",
        "full",
        "no_recovery",
    }
