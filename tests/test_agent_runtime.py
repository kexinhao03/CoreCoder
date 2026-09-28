"""The production CLI builder records real Agent tool execution offline."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier, Event, Lock
from typing import ClassVar

import pytest

from corecoder import cli
from corecoder.config import Config
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.runtime import RunStatus, RuntimeToolAdapter
from corecoder.tools import ALL_TOOLS
from corecoder.tools.base import Tool


@pytest.fixture(autouse=True)
def isolate_user_integrations(monkeypatch):
    # User MCP servers and shell hooks are external to this offline test.
    monkeypatch.setattr(cli, "load_mcp_tools", list)
    monkeypatch.setattr(cli, "load_hooks", lambda: None)


def _read_write_script(source, target):
    return [
        LLMResponse(tool_calls=[ToolCall(
            id="read-1", name="read_file", arguments={"file_path": str(source)},
        )]),
        LLMResponse(tool_calls=[ToolCall(
            id="write-1", name="write_file",
            arguments={"file_path": str(target), "content": "written through runtime\n"},
        )]),
        LLMResponse(content="Read and write completed."),
    ]


def _database(workspace):
    path = workspace / ".reliagent" / "agent-runtime.sqlite"
    assert path.is_file()
    return closing(sqlite3.connect(path))


def test_production_builder_records_read_and_approved_write(tmp_path):
    source, target = tmp_path / "source.txt", tmp_path / "target.txt"
    source.write_text("runtime evidence\n", encoding="utf-8")
    agent, session = cli._build_agent(
        ScriptedLLM(_read_write_script(source, target)),
        Config(model="scripted-demo"), Permission(allow_all=True),
        runtime_workspace=tmp_path, runtime_enabled=True,
    )

    assert agent.chat("Read the source and write the target.") == "Read and write completed."
    assert session.finish(RunStatus.SUCCEEDED).status is RunStatus.SUCCEEDED
    assert target.read_text(encoding="utf-8") == "written through runtime\n"
    results = [message for message in agent.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in results] == ["read-1", "write-1"]
    assert "runtime evidence" in results[0]["content"]
    assert results[1]["content"] == f"Wrote 1 lines to {target}"

    with _database(tmp_path) as db:
        assert db.execute("SELECT id, status, workflow, model, workspace FROM runs").fetchall() == [
            (session.run_id, "succeeded", "corecoder_agent", "scripted-demo", str(tmp_path.resolve())),
        ]
        assert db.execute("SELECT status, attempt_count FROM steps ORDER BY sequence").fetchall() == [
            ("succeeded", 1), ("succeeded", 1),
        ]
        assert db.execute(
            "SELECT t.tool_name, t.status, t.run_id = s.run_id FROM tool_calls t "
            "JOIN steps s ON s.id = t.step_id ORDER BY s.sequence"
        ).fetchall() == [("read_file", "succeeded", 1), ("write_file", "succeeded", 1)]
        assert db.execute("SELECT COUNT(*) FROM tool_calls").fetchone() == (2,)
        assert db.execute("SELECT tool_name, status, decision FROM approvals").fetchall() == [
            ("write_file", "approved", "allow_once"),
        ]
        events = db.execute("SELECT sequence, type FROM events ORDER BY sequence").fetchall()
        assert [row[0] for row in events] == list(range(1, len(events) + 1))
        types = [row[1] for row in events]
        assert types[:2] == ["run.created", "run.started"]
        assert types[-1] == "run.completed"
        for event_type in ("step.started", "step.completed", "tool.started", "tool.completed"):
            assert types.count(event_type) == 2
        assert types.count("approval.requested") == types.count("approval.resolved") == 1
        stored_arguments = db.execute(
            "SELECT arguments_json, arguments_replayable FROM tool_calls WHERE tool_name = 'write_file'"
        ).fetchone()
        assert "written through runtime" not in stored_arguments[0]
        assert stored_arguments[1] == 0


@pytest.mark.parametrize("permission_mode,expected_prompts", [("once", 2), ("mixed", 2), ("always", 1), ("allow_all", 0)])
def test_parallel_writes_serialize_approval_and_preserve_decisions(
    tmp_path, monkeypatch, permission_mode, expected_prompts,
):
    allowed, other = tmp_path / "allowed.txt", tmp_path / "other.txt"
    arguments = [
        {"file_path": str(allowed), "content": "approved raw body\n"},
        {"file_path": str(other), "content": "other raw body\n"},
    ]
    # Align both worker calls before the real Runtime adapter lifecycle.
    # This controls scheduling only; Permission, adapters and persistence are real.
    ready = Barrier(2)
    execute = RuntimeToolAdapter.execute

    def simultaneous_requests(self, **raw_arguments):
        ready.wait(timeout=5)
        return execute(self, **raw_arguments)

    monkeypatch.setattr(RuntimeToolAdapter, "execute", simultaneous_requests)
    state_lock = Lock()
    overlapped = Event()
    active = peak = 0
    prompts = []

    def ask(name, raw_arguments):
        nonlocal active, peak
        with state_lock:
            prompts.append((name, raw_arguments.copy()))
            first = len(prompts) == 1
            active += 1
            peak = max(peak, active)
            if active > 1:
                overlapped.set()
        try:
            if first:
                # Keep the first prompt open while the second worker attempts
                # approval; the timeout bounds a correctly serialized prompt.
                overlapped.wait(timeout=1)
            if permission_mode == "always":
                return "always"
            if permission_mode == "mixed" and raw_arguments["file_path"] == str(other):
                return "deny"
            return "once"
        finally:
            with state_lock:
                active -= 1

    agent, session = cli._build_agent(
        ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(id="write-allowed", name="write_file", arguments=arguments[0]),
                ToolCall(id="write-other", name="write_file", arguments=arguments[1]),
            ]),
            LLMResponse(content="Both decisions handled."),
        ]),
        Config(), Permission(ask=ask, allow_all=permission_mode == "allow_all"),
        runtime_workspace=tmp_path, runtime_enabled=True,
    )

    assert agent.chat("Handle both writes.") == "Both decisions handled."
    session.finish(RunStatus.SUCCEEDED)

    assert peak == (1 if expected_prompts else 0)
    assert len(prompts) == expected_prompts
    assert all(name == "write_file" and raw in arguments for name, raw in prompts)
    if expected_prompts == 2:
        assert sorted(raw["file_path"] for _, raw in prompts) == [str(allowed), str(other)]
    assert allowed.read_text(encoding="utf-8") == "approved raw body\n"
    if permission_mode == "mixed":
        assert not other.exists()
    else:
        assert other.read_text(encoding="utf-8") == "other raw body\n"
    calls = session.store.list_tool_calls(session.run_id)
    approvals = session.store.list_approvals(session.run_id)
    assert len(calls) == len(approvals) == 2
    expected_statuses = ["cancelled", "succeeded"] if permission_mode == "mixed" else ["succeeded", "succeeded"]
    assert sorted(call.status.value for call in calls) == expected_statuses
    assert sorted(step.status.value for step in session.store.list_steps(session.run_id)) == expected_statuses
    paths = {call.id: call.arguments["file_path"] for call in calls}
    assert {paths[a.tool_call_id]: (a.status.value, a.decision.value) for a in approvals} == {
        str(allowed): ("approved", "allow_once"),
        str(other): ("denied", "deny") if permission_mode == "mixed" else ("approved", "allow_once"),
    }
    results = {m["tool_call_id"]: m["content"] for m in agent.messages if m["role"] == "tool"}
    assert results["write-allowed"] == f"Wrote 1 lines to {allowed}"
    assert results["write-other"] == (
        "Approval denied for write_file" if permission_mode == "mixed" else f"Wrote 1 lines to {other}"
    )


def test_read_execution_does_not_acquire_write_gate(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("read without write gate\n", encoding="utf-8")
    agent, session = cli._build_agent(
        ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="read-1", name="read_file", arguments={"file_path": str(source)})]),
            LLMResponse(content="Read completed."),
        ]),
        Config(), Permission(), runtime_workspace=tmp_path, runtime_enabled=True,
    )

    with ThreadPoolExecutor(max_workers=1) as pool, session._write_gate:
        assert pool.submit(agent.chat, "Read the source.").result(timeout=5) == "Read completed."
    session.finish(RunStatus.SUCCEEDED)

    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert len(results) == 1
    assert "read without write gate" in results[0]
    assert [call.status.value for call in session.store.list_tool_calls(session.run_id)] == ["succeeded"]
    assert session.store.list_approvals(session.run_id) == []


def test_builder_preserves_unwrapped_tools_and_tool_order(tmp_path, monkeypatch):
    class MCPTool(Tool):
        name = "mcp__test__echo"
        description = "An external tool"
        parameters: ClassVar[dict] = {"type": "object", "properties": {}}

        def execute(self, **kwargs):
            return "mcp output"

    mcp_tool = MCPTool()
    monkeypatch.setattr(cli, "load_mcp_tools", lambda: [mcp_tool])
    agent, session = cli._build_agent(
        ScriptedLLM([]), Config(), Permission(),
        runtime_workspace=tmp_path, runtime_enabled=True,
    )
    originals = [*ALL_TOOLS, mcp_tool]
    assert [tool.name for tool in agent.tools] == [tool.name for tool in originals]
    for original, wrapped in zip(originals, agent.tools):
        if original.name in {"read_file", "write_file"}:
            assert isinstance(wrapped, RuntimeToolAdapter)
            assert wrapped.schema() == original.schema()
        else:
            assert wrapped is original
    session.finish(RunStatus.SUCCEEDED)


def test_disabled_runtime_keeps_original_tools_and_creates_no_database(tmp_path):
    source, target = tmp_path / "source.txt", tmp_path / "target.txt"
    source.write_text("original read\n", encoding="utf-8")
    agent, session = cli._build_agent(
        ScriptedLLM(_read_write_script(source, target)), Config(), Permission(allow_all=True),
        runtime_workspace=tmp_path, runtime_enabled=False,
    )

    assert session is None
    assert all(tool is original for tool, original in zip(agent.tools, ALL_TOOLS))
    assert agent.chat("Read and write.") == "Read and write completed."
    assert target.read_text(encoding="utf-8") == "written through runtime\n"
    assert not (tmp_path / ".reliagent").exists()


@pytest.mark.parametrize("status,event", [
    (RunStatus.SUCCEEDED, "run.completed"),
    (RunStatus.FAILED, "run.failed"),
    (RunStatus.CANCELLED, "run.cancelled"),
])
def test_session_finish_is_terminal_and_idempotent(tmp_path, status, event):
    from corecoder.agent_runtime import AgentRuntimeSession

    session = AgentRuntimeSession.open(tmp_path, "scripted-demo", Permission())
    assert session.store.get_run(session.run_id).status is RunStatus.RUNNING
    completed = session.finish(status)
    assert completed.status is status
    assert completed.started_at is not None and completed.ended_at is not None
    assert session.finish(RunStatus.SUCCEEDED) == completed
    assert session.finish(RunStatus.FAILED) == completed
    assert session.finish(RunStatus.CANCELLED) == completed
    assert [e.type for e in session.store.list_events(session.run_id)] == [
        "run.created", "run.started", event,
    ]


def _configure_cli(monkeypatch, tmp_path, llm, *arguments):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["corecoder", *arguments])
    monkeypatch.setattr(cli.Config, "from_env", lambda: Config(model="scripted-demo", api_key="offline"))
    monkeypatch.setattr(cli, "LLM", lambda **kwargs: llm)


def test_main_uses_production_builder_and_default_workspace(tmp_path, monkeypatch):
    source, target = tmp_path / "source.txt", tmp_path / "target.txt"
    source.write_text("main source\n", encoding="utf-8")
    llm = ScriptedLLM(_read_write_script(source, target))
    _configure_cli(monkeypatch, tmp_path, llm, "--yes", "-p", "read and write")

    cli.main()

    assert target.read_text(encoding="utf-8") == "written through runtime\n"
    with _database(tmp_path) as db:
        assert db.execute("SELECT status FROM runs").fetchall() == [("succeeded",)]
        assert db.execute("SELECT COUNT(*) FROM tool_calls").fetchone() == (2,)
        assert db.execute("SELECT type FROM events ORDER BY sequence DESC LIMIT 1").fetchone() == ("run.completed",)


def test_main_honors_runtime_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "audit-workspace"
    _configure_cli(monkeypatch, tmp_path, ScriptedLLM([LLMResponse(content="done")]),
                   "--runtime-workspace", str(workspace), "-p", "hello")

    cli.main()

    with _database(workspace) as db:
        assert db.execute("SELECT workspace, status FROM runs").fetchall() == [(str(workspace), "succeeded")]
    assert not (tmp_path / ".reliagent").exists()


def test_main_no_runtime_creates_no_database(tmp_path, monkeypatch):
    _configure_cli(monkeypatch, tmp_path, ScriptedLLM([LLMResponse(content="done")]),
                   "--no-runtime", "-p", "hello")

    cli.main()

    assert not (tmp_path / ".reliagent").exists()


def test_resumed_model_is_recorded_in_runtime(tmp_path, monkeypatch):
    llm = ScriptedLLM([LLMResponse(content="resumed")])
    _configure_cli(monkeypatch, tmp_path, llm, "--resume", "saved", "-p", "continue")
    monkeypatch.setattr(cli, "load_session", lambda sid: ([], "saved-model"))

    cli.main()

    assert llm.model == "saved-model"
    with _database(tmp_path) as db:
        assert db.execute("SELECT model, status FROM runs").fetchall() == [("saved-model", "succeeded")]


@pytest.mark.parametrize("error,code,status,event", [
    (RuntimeError("offline failure"), 1, "failed", "run.failed"),
    (KeyboardInterrupt(), 130, "cancelled", "run.cancelled"),
])
def test_one_shot_failure_finishes_run_and_preserves_exit_code(tmp_path, monkeypatch, error, code, status, event):
    class FailingLLM(ScriptedLLM):
        def chat(self, *args, **kwargs):
            raise error

    _configure_cli(monkeypatch, tmp_path, FailingLLM([]), "-p", "hello")

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == code
    with _database(tmp_path) as db:
        assert db.execute("SELECT status FROM runs").fetchall() == [(status,)]
        assert db.execute("SELECT type FROM events ORDER BY sequence DESC LIMIT 1").fetchone() == (event,)


@pytest.mark.parametrize("first_error", [None, RuntimeError("try again"), KeyboardInterrupt()])
def test_interactive_session_stays_open_between_turns_and_finishes_on_exit(tmp_path, monkeypatch, first_error):
    class RecoveringLLM(ScriptedLLM):
        def chat(self, *args, **kwargs):
            nonlocal first_error
            if first_error is not None:
                error, first_error = first_error, None
                raise error
            return super().chat(*args, **kwargs)

    llm = RecoveringLLM([LLMResponse(content="one"), LLMResponse(content="two")])
    _configure_cli(monkeypatch, tmp_path, llm)
    inputs = iter(["first turn", "second turn", "quit"])

    def prompt(*args, **kwargs):
        with _database(tmp_path) as db:
            assert db.execute("SELECT status FROM runs").fetchall() == [("running",)]
        return next(inputs)

    monkeypatch.setattr(cli, "pt_prompt", prompt)

    cli.main()

    with _database(tmp_path) as db:
        assert db.execute("SELECT status FROM runs").fetchall() == [("succeeded",)]
        assert db.execute("SELECT COUNT(*) FROM events WHERE type = 'run.completed'").fetchone() == (1,)
