"""The minimal CLI loads a fixed JSON task into the production runtime."""

import json
import os
import subprocess
import sys
import time

import pytest

from corecoder.reliagent_cli import main
from corecoder.runtime import (
    ApprovalDecision,
    ApprovalStatus,
    ExecutionKind,
    RiskLevel,
    RunStatus,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
)


def _write_task(path, workspace, steps):
    path.write_text(json.dumps({
        "goal": "exercise recovery CLI",
        "workspace": str(workspace),
        "steps": steps,
    }))


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


def test_cli_lists_approves_resumes_and_does_not_repeat_effect(tmp_path, capsys):
    task_path = tmp_path / "task.json"
    database = tmp_path / "runtime.sqlite"
    marker = tmp_path / "effect-marker.txt"
    _write_task(task_path, tmp_path, [{
        "tool_name": "local_mutating",
        "argv": [
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'a', encoding='utf-8').write('effect\\n')",
        ],
    }])

    assert main(["run", str(task_path), "--database", str(database)]) == 0
    waiting = json.loads(capsys.readouterr().out)
    assert waiting["status"] == "waiting_approval"
    assert not marker.exists()

    assert main(["list", "--database", str(database)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["runs"] == [{"run_id": waiting["run_id"], "status": "waiting_approval"}]
    assert listing["recovery"] == [{
        "kind": "approval_pending",
        "reason": "approval_pending",
        "run_id": waiting["run_id"],
        "tool_call_id": listing["approvals"][0]["tool_call_id"],
    }]
    approval_id = listing["approvals"][0]["approval_id"]

    assert main(["approve", approval_id, "--database", str(database)]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved == {
        "approval_id": approval_id,
        "decision": "allow_once",
        "status": "approved",
    }

    resume_args = [
        "resume", waiting["run_id"], str(task_path), "--database", str(database)
    ]
    assert main(resume_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert marker.read_text(encoding="utf-8") == "effect\n"
    store = SQLiteStore(database)
    events_after_success = store.list_events(waiting["run_id"])

    assert main(resume_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert marker.read_text(encoding="utf-8") == "effect\n"
    assert store.list_events(waiting["run_id"]) == events_after_success


def test_cli_denies_approval_and_cancels_run_without_effect(tmp_path, capsys):
    task_path = tmp_path / "task.json"
    database = tmp_path / "runtime.sqlite"
    marker = tmp_path / "denied-marker.txt"
    _write_task(task_path, tmp_path, [{
        "tool_name": "local_mutating",
        "argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
    }])
    main(["run", str(task_path), "--database", str(database)])
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    store = SQLiteStore(database)
    approval = store.list_approvals(run_id)[0]

    assert main(["deny", approval.id, "--database", str(database)]) == 0
    denied = store.get_approval(approval.id)
    assert denied.status is ApprovalStatus.DENIED
    assert denied.decision is ApprovalDecision.DENY
    capsys.readouterr()

    assert main([
        "resume", run_id, str(task_path), "--database", str(database)
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert store.get_run(run_id).status is RunStatus.CANCELLED
    assert not marker.exists()


def test_cli_reconciles_unknown_effect_and_cancels_run(tmp_path, capsys):
    database = tmp_path / "runtime.sqlite"
    task_path = tmp_path / "task.json"
    _write_task(task_path, tmp_path, [{
        "tool_name": "local_mutating",
        "argv": [sys.executable, "-c", "raise SystemExit('must not replay')"],
    }])
    store = SQLiteStore(database)
    store.initialize()
    run = store.create_run(
        goal="reconcile",
        workflow="test",
        workspace=tmp_path,
        model="test",
        prompt_version="v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    call = store.create_tool_call(
        run_id=run.id,
        tool_name="local_mutating",
        arguments={"argv": ["effect"]},
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False,
        idempotency_key=None,
        timeout_seconds=30,
    )
    store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")

    assert main(["list", "--database", str(database)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["recovery"] == [{
        "kind": "human_required",
        "reason": "process_lost",
        "run_id": run.id,
        "tool_call_id": call.id,
    }]

    assert main([
        "resume",
        run.id,
        str(task_path),
        "--database",
        str(database),
    ]) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked == {
        "recovery": [{
            "kind": "human_required",
            "reason": "process_lost",
            "tool_call_id": call.id,
        }],
        "run_id": run.id,
        "status": "recoverable",
    }
    assert len(store.list_tool_calls(run.id)) == 1

    assert main([
        "reconcile",
        call.id,
        "confirmed_succeeded",
        "--database",
        str(database),
    ]) == 0
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled == {
        "run_id": run.id,
        "status": "succeeded",
        "tool_call_id": call.id,
    }
    assert store.get_tool_call(call.id).status is ToolCallStatus.SUCCEEDED

    assert main(["cancel", run.id, "--database", str(database)]) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled == {"run_id": run.id, "status": "cancelled"}
    assert store.get_run(run.id).status is RunStatus.CANCELLED


@pytest.mark.skipif(os.name == "nt", reason="process-exit recovery demo is POSIX-only")
def test_cli_recovers_after_process_exit_and_repeat_resume_is_inert(tmp_path, capsys):
    task_path = tmp_path / "task.json"
    database = tmp_path / "runtime.sqlite"
    _write_task(task_path, tmp_path, [{
        "tool_name": "local_read_only",
        "argv": [
            sys.executable,
            "-c",
            "import time; time.sleep(0.4); print('recovered')",
        ],
    }])
    command = [
        sys.executable,
        "-m",
        "corecoder.reliagent_cli",
        "run",
        str(task_path),
        "--database",
        str(database),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": os.getcwd(), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if database.exists():
                store = SQLiteStore(database)
                calls = store.list_tool_calls(statuses=[ToolCallStatus.RUNNING])
                if calls:
                    break
            time.sleep(0.02)
        else:
            raise AssertionError("CLI did not persist a running ToolCall before timeout")
    finally:
        process.terminate()
        process.wait(timeout=5)

    run_id = store.list_runs()[0].id
    resume_args = ["resume", run_id, str(task_path), "--database", str(database)]
    assert main(resume_args) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed == {"run_id": run_id, "status": "succeeded"}
    persisted_calls = store.list_tool_calls(run_id)
    assert [call.status for call in persisted_calls] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCEEDED,
    ]
    assert persisted_calls[1].retry_of == persisted_calls[0].id
    assert store.list_steps(run_id)[0].status is StepStatus.SUCCEEDED
    events_after_recovery = store.list_events(run_id)

    assert main(resume_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert store.list_tool_calls(run_id) == persisted_calls
    assert store.list_events(run_id) == events_after_recovery
