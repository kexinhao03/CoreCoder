"""Real file edits and test processes through the production Agent builder."""

import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corecoder import cli
from corecoder.config import Config
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.runtime import RecoveryKind, RecoveryManager, RunStatus, SQLiteStore, ToolCallStatus, ToolPolicyRegistry, processes
from corecoder.tools.edit import EditFileTool


def command(*argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def build(monkeypatch, workspace, calls, permission=None):
    monkeypatch.setattr(cli, "load_mcp_tools", list)
    monkeypatch.setattr(cli, "load_hooks", lambda: None)
    responses = [LLMResponse(tool_calls=batch) for batch in calls]
    responses.append(LLMResponse(content="done"))
    return cli._build_agent(
        ScriptedLLM(responses), Config(), permission or Permission(allow_all=True),
        runtime_workspace=workspace, runtime_enabled=True,
    )


def test_read_edit_test_chain_records_real_outcomes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8",
    )
    # Avoid timestamp/size based bytecode reuse during a same-second, same-size edit.
    test_command = command(sys.executable, "-B", "-m", "unittest", "-v")
    prompts = []
    agent, session = build(monkeypatch, tmp_path, [
        [ToolCall(id="read", name="read_file", arguments={"file_path": str(source)})],
        [ToolCall(id="before", name="bash", arguments={"command": test_command})],
        [ToolCall(id="edit", name="edit_file", arguments={
            "file_path": str(source), "old_string": "return a - b", "new_string": "return a + b",
        })],
        [ToolCall(id="after", name="bash", arguments={"command": test_command})],
    ], Permission(ask=lambda name, args: prompts.append((name, args.copy())) or "once"))

    assert agent.chat("Read, reproduce, fix, and test.") == "done"
    session.finish(RunStatus.SUCCEEDED)
    assert "return a + b" in source.read_text(encoding="utf-8")
    calls = session.store.list_tool_calls(session.run_id)
    assert [c.tool_name for c in calls] == ["read_file", "bash", "edit_file", "bash"]
    assert [c.status.value for c in calls] == ["succeeded", "failed", "succeeded", "succeeded"]
    assert [s.status.value for s in session.store.list_steps(session.run_id)] == [
        "succeeded", "failed", "succeeded", "succeeded",
    ]
    assert [name for name, _ in prompts] == ["bash", "edit_file", "bash"]
    assert prompts[0][1] == {"command": test_command}
    results = {m["tool_call_id"]: m["content"] for m in agent.messages if m["role"] == "tool"}
    assert "FAILED" in results["before"] and "exit code: 1" in results["before"]
    assert "OK" in results["after"]
    for c in (calls[1], calls[3]):
        assert c.execution_kind.value == "subprocess" and c.attempt == 1
        assert session.store.get_process_evidence(c.id).termination_confirmed
    terminal = [e for e in session.store.list_events(session.run_id) if e.type in {"tool.failed", "tool.completed"}]
    assert [e.payload.get("exit_code") for e in terminal if "exit_code" in e.payload] == [1, 0]
    assert not calls[2].arguments_replayable
    assert "return a + b" not in str(calls[2].arguments)


@pytest.mark.parametrize("name", ["edit_file", "bash"])
def test_denial_records_call_without_effect(tmp_path, monkeypatch, name):
    source = tmp_path / "effect.txt"
    source.write_text("original", encoding="utf-8")
    args = {"file_path": str(source), "old_string": "original", "new_string": "changed"}
    if name == "bash":
        args = {"command": command(sys.executable, "-c", f"from pathlib import Path; Path({str(source)!r}).write_text('changed')")}
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="deny", name=name, arguments=args)]], Permission())
    agent.chat("Try the operation.")
    assert source.read_text(encoding="utf-8") == "original"
    [call] = session.store.list_tool_calls(session.run_id)
    assert call.status is ToolCallStatus.CANCELLED
    assert session.store.list_approvals(session.run_id)[0].decision.value == "deny"
    assert session.store.get_process_evidence(call.id) is None


def test_edit_mismatch_is_persisted_failure(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="edit", name="edit_file", arguments={
        "file_path": str(source), "old_string": "missing", "new_string": "replacement",
    })]])
    agent.chat("Edit.")
    [call] = session.store.list_tool_calls(session.run_id)
    assert call.status is ToolCallStatus.FAILED
    assert "old_string not found" in call.result_summary
    assert source.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name != "posix", reason="Process-group termination guarantee is POSIX only")
def test_bash_timeout_terminates_process_without_retry(tmp_path, monkeypatch):
    marker = tmp_path / "late.txt"
    code = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="slow", name="bash", arguments={
        "command": command(sys.executable, "-c", code), "timeout": 1,
    })]])
    agent.chat("Run bounded command.")
    [call] = session.store.list_tool_calls(session.run_id)
    assert call.status is ToolCallStatus.TIMED_OUT and call.timeout_seconds == 1
    assert session.store.get_process_evidence(call.id).termination_confirmed
    time.sleep(1.2)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="Process-group termination guarantee is POSIX only")
def test_cancellation_stops_bash_and_refuses_next_edit(tmp_path, monkeypatch):
    ready, late, source = tmp_path / "ready", tmp_path / "late", tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    code = (
        f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); "
        f"time.sleep(2); Path({str(late)!r}).touch()"
    )
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="slow", name="bash", arguments={
        "command": command(sys.executable, "-c", code),
    })]])
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(agent.chat, "Run then cancel.")
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        session.executor.cancel_run(session.run_id)
        assert work.result(timeout=5) == "done"
    [call] = session.store.list_tool_calls(session.run_id)
    assert call.status is ToolCallStatus.CANCELLED
    adapter = next(t for t in agent.tools if t.name == "edit_file")
    with pytest.raises((ValueError, RuntimeError)):
        adapter.execute(file_path=str(source), old_string="original", new_string="changed")
    assert source.read_text(encoding="utf-8") == "original"
    time.sleep(2)
    assert not late.exists()


def test_interrupted_edit_requires_human_and_is_not_replayed(tmp_path, monkeypatch):
    source = tmp_path / "effect.txt"
    source.write_text("original", encoding="utf-8")
    execute = EditFileTool.execute

    def edit_then_interrupt(self, **args):
        execute(self, **args)
        raise SystemExit(87)

    monkeypatch.setattr(EditFileTool, "execute", edit_then_interrupt)
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="edit", name="edit_file", arguments={
        "file_path": str(source), "old_string": "original", "new_string": "changed",
    })]])
    with pytest.raises(SystemExit):
        agent.chat("Edit and interrupt.")
    [candidate] = RecoveryManager(session.store, ToolPolicyRegistry.with_builtin_defaults()).scan(session.run_id)
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    assert session.finish(RunStatus.SUCCEEDED).status is RunStatus.RECOVERABLE
    assert source.read_text(encoding="utf-8") == "changed"
    assert len(session.store.list_tool_calls(session.run_id)) == 1


def test_same_batch_reads_and_commands_are_serialized(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("value", encoding="utf-8")
    agent, session = build(monkeypatch, tmp_path, [[
        ToolCall(id="read-a", name="read_file", arguments={"file_path": str(source)}),
        ToolCall(id="bash", name="bash", arguments={"command": command(sys.executable, "-c", "print('test ran')")}),
        ToolCall(id="read-b", name="read_file", arguments={"file_path": str(source)}),
    ]])
    agent.chat("Handle the batch.")
    calls = session.store.list_tool_calls(session.run_id)
    assert len(calls) == 3 and all(c.status is ToolCallStatus.SUCCEEDED for c in calls)


def test_bash_tracks_directory_across_agent_turns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    agent, session = build(monkeypatch, tmp_path, [
        [ToolCall(id="cd", name="bash", arguments={"command": "cd subdir"})],
        [ToolCall(id="pwd", name="bash", arguments={"command": command(sys.executable, "-c", "import os; print(os.getcwd())")})],
    ])
    agent.chat("Change directory then inspect.")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert str(subdir) in results[-1]
    calls = session.store.list_tool_calls(session.run_id)
    assert calls[-1].arguments["cwd"] == str(subdir)


@pytest.mark.parametrize("name", ["edit_file", "bash"])
def test_real_process_crash_after_effect_never_replays(tmp_path, name):
    source = tmp_path / "effect.txt"
    source.write_text("original", encoding="utf-8")
    script = r'''
import os
import sys
from pathlib import Path
from corecoder import cli
from corecoder.config import Config
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.edit import EditFileTool
from corecoder.runtime.processes import ManagedProcessRunner
workspace, name, shell_command = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
source = workspace / "effect.txt"
cli.load_mcp_tools = list
cli.load_hooks = lambda: None
original_edit, original_run = EditFileTool.execute, ManagedProcessRunner.run
def edit_then_crash(self, **args):
    original_edit(self, **args)
    os._exit(87)
def run_then_crash(self, spec, cancellation):
    original_run(self, spec, cancellation)
    os._exit(87)
EditFileTool.execute = edit_then_crash
ManagedProcessRunner.run = run_then_crash
args = {"file_path": str(source), "old_string": "original", "new_string": "changed"}
if name == "bash":
    args = {"command": shell_command}
agent, session = cli._build_agent(
    ScriptedLLM([LLMResponse(tool_calls=[ToolCall(id="effect", name=name, arguments=args)])]),
    Config(), Permission(allow_all=True), runtime_workspace=workspace, runtime_enabled=True,
)
agent.chat("Produce effect then crash before terminal persistence.")
'''
    effect_command = command(
        sys.executable, "-c",
        f"from pathlib import Path; p=Path({str(source)!r}); p.write_text(p.read_text() + '|effect')",
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), name, effect_command],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    assert crashed.returncode == 87, crashed.stderr
    store = SQLiteStore(tmp_path / ".reliagent" / "agent-runtime.sqlite")
    store.initialize()
    [call] = store.list_tool_calls()
    assert call.status is ToolCallStatus.RUNNING
    recovery = RecoveryManager(store, ToolPolicyRegistry.with_builtin_defaults())
    [candidate] = recovery.scan(call.run_id)
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    assert store.get_run(call.run_id).status is RunStatus.RECOVERABLE
    with pytest.raises(ValueError):
        recovery.resume_retry(candidate)
    assert source.read_text(encoding="utf-8") == ("changed" if name == "edit_file" else "original|effect")
    assert len(store.list_tool_calls()) == 1


@pytest.mark.skipif(os.name != "posix", reason="Process-group termination guarantee is POSIX only")
def test_keyboard_interrupt_terminates_bash_and_preserves_unknown_result(tmp_path, monkeypatch):
    late = tmp_path / "late"
    code = f"import time; from pathlib import Path; time.sleep(1); Path({str(late)!r}).touch()"
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="bash", name="bash", arguments={
        "command": command(sys.executable, "-c", code),
    })]])
    communicate = subprocess.Popen.communicate
    children = []

    def interrupt_once(process, *args, **kwargs):
        if not children:
            children.append(process)
            raise KeyboardInterrupt
        return communicate(process, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupt_once)
    try:
        with pytest.raises(KeyboardInterrupt):
            agent.chat("Run and interrupt.")
        assert processes._termination_confirmed(children[0])
        [call] = session.store.list_tool_calls(session.run_id)
        assert call.status is ToolCallStatus.INTERRUPTED
        assert session.store.get_run(session.run_id).status is RunStatus.RECOVERABLE
        assert session.store.list_steps(session.run_id)[0].status.value == "failed"
        assert agent.messages[-1]["content"] == "[interrupted]"
        time.sleep(1.1)
        assert not late.exists()
    finally:
        # Clean up even when testing the pre-fix leak.
        for child in children:
            processes._terminate(child, 0.2)
            child.communicate(timeout=2)


@pytest.mark.parametrize("timeout", [0, -1, True, 0.5, 121])
def test_bash_invalid_timeout_never_starts_process(tmp_path, monkeypatch, timeout):
    marker = tmp_path / "effect"
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="bash", name="bash", arguments={
        "command": command(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
        "timeout": timeout,
    })]])
    agent.chat("Invalid timeout.")
    assert session.store.list_tool_calls(session.run_id) == []
    assert session.store.list_steps(session.run_id)[0].status.value == "failed"
    assert not marker.exists()


def test_bash_dangerous_command_remains_blocked(tmp_path, monkeypatch):
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="bash", name="bash", arguments={"command": "rm -rf /"})]])
    agent.chat("Blocked command.")
    assert session.store.list_tool_calls(session.run_id) == []
    assert "Blocked" in agent.messages[-2]["content"]


@pytest.mark.parametrize("name", ["edit_file", "bash"])
def test_plan_mode_blocks_new_runtime_tools(tmp_path, monkeypatch, name):
    source = tmp_path / "effect.txt"
    source.write_text("original", encoding="utf-8")
    args = {"file_path": str(source), "old_string": "original", "new_string": "changed"}
    if name == "bash":
        args = {"command": "echo should not execute"}
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="plan", name=name, arguments=args)]])
    agent.plan_mode = True
    agent.chat("Investigate only.")
    assert session.store.list_tool_calls(session.run_id) == []
    assert session.store.list_steps(session.run_id) == []
    assert "Plan mode" in agent.messages[-2]["content"]
    assert source.read_text(encoding="utf-8") == "original"


def test_opaque_shell_command_is_not_stored_as_replayable_plaintext(tmp_path, monkeypatch):
    raw_command = command(sys.executable, "-c", "pass", "--password", "opaque-credential-example")
    prompts = []
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="bash", name="bash", arguments={
        "command": raw_command,
    })]], Permission(ask=lambda name, args: prompts.append(args.copy()) or "once"))
    agent.chat("Run with opaque shell arguments.")
    [call] = session.store.list_tool_calls(session.run_id)
    assert call.status is ToolCallStatus.SUCCEEDED
    assert not call.arguments_replayable
    assert prompts == [{"command": raw_command}]
    with sqlite3.connect(session.store.path) as db:
        stored = "\n".join(db.iterdump())
    assert "opaque-credential-example" not in stored


def test_verbose_failed_test_preserves_stderr_tail(tmp_path, monkeypatch):
    code = "import sys; print('x' * 16000); sys.stderr.write('ASSERTION FAILED AT END'); sys.exit(2)"
    agent, session = build(monkeypatch, tmp_path, [[ToolCall(id="bash", name="bash", arguments={
        "command": command(sys.executable, "-c", code),
    })]])
    agent.chat("Run verbose failing test.")
    [call] = session.store.list_tool_calls(session.run_id)
    assert call.status is ToolCallStatus.FAILED
    assert "ASSERTION FAILED AT END" in call.result_summary
    assert len(call.result_summary) <= 15000
    assert "ASSERTION FAILED AT END" in agent.messages[-2]["content"]


@pytest.mark.skipif(os.name != "posix", reason="Real SIGINT / process-group verification is POSIX only")
def test_real_sigint_during_batch_stops_command_and_never_starts_queued_edit(tmp_path):
    source, ready, late = tmp_path / "source.txt", tmp_path / "ready", tmp_path / "late"
    source.write_text("original", encoding="utf-8")
    code = (
        f"import time; from pathlib import Path; Path({str(ready)!r}).touch(); "
        f"time.sleep(2); Path({str(late)!r}).touch()"
    )
    script = r'''
import sys
from pathlib import Path
from corecoder import cli
from corecoder.config import Config
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
workspace, command = Path(sys.argv[1]), sys.argv[2]
cli.load_mcp_tools = list
cli.load_hooks = lambda: None
agent, session = cli._build_agent(
    ScriptedLLM([LLMResponse(tool_calls=[
        ToolCall(id="bash", name="bash", arguments={"command": command}),
        ToolCall(id="edit", name="edit_file", arguments={
            "file_path": str(workspace / "source.txt"), "old_string": "original", "new_string": "changed",
        }),
    ])]), Config(), Permission(allow_all=True), runtime_workspace=workspace, runtime_enabled=True,
)
try:
    agent.chat("Run a batch.")
except KeyboardInterrupt:
    raise SystemExit(130)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), command(sys.executable, "-c", code)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as child:
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            child.send_signal(signal.SIGINT)
            _, stderr = child.communicate(timeout=8)
            assert child.returncode == 130, stderr
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=2)
    store = SQLiteStore(tmp_path / ".reliagent" / "agent-runtime.sqlite")
    [call] = store.list_tool_calls()
    assert call.tool_name == "bash" and call.status is ToolCallStatus.INTERRUPTED
    assert source.read_text(encoding="utf-8") == "original"
    time.sleep(2.1)
    assert not late.exists()
