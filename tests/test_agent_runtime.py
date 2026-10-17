"""The production CLI builder records real Agent tool execution offline."""

import json
import sqlite3
from contextlib import closing
from threading import Event, Lock
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet

from corecoder import cli
from corecoder.config import Config
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.runtime import (
    ExecutionKind,
    RecoveryKind,
    RecoveryManager,
    RiskLevel,
    RunStatus,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
    ToolPolicyRegistry,
)
from corecoder.tools import ALL_TOOLS
from corecoder.tools.base import Tool


@pytest.fixture(autouse=True)
def isolate_user_integrations(monkeypatch):
    monkeypatch.setenv("RELIAGENT_CHECKPOINT_KEY", Fernet.generate_key().decode())
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
        for event_type in ("tool.started", "tool.completed"):
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
        Config(model="scripted-demo"), Permission(ask=ask, allow_all=permission_mode == "allow_all"),
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
    paths = {item["attempt_id"]: json.loads(item["arguments"])["file_path"]
             for item in session.journal.list_invocations(session.run_id)}
    assert {paths[a.tool_call_id]: (a.status.value, a.decision.value) for a in approvals} == {
        str(allowed): ("approved", "allow_once"),
        str(other): ("denied", "deny") if permission_mode == "mixed" else ("approved", "allow_once"),
    }
    results = {m["tool_call_id"]: m["content"] for m in agent.messages if m["role"] == "tool"}
    assert results["write-allowed"] == f"Wrote 1 lines to {allowed}"
    assert results["write-other"] == (
        Permission(ask=lambda *_: "deny").check("write_file", arguments[1]) if permission_mode == "mixed" else f"Wrote 1 lines to {other}"
    )


@pytest.mark.parametrize("second_tool", ["read_file", "write_file"])
def test_parallel_runtime_reads_and_writes_complete_serially(tmp_path, monkeypatch, second_tool):
    source = tmp_path / "source.txt"
    source.write_text("read evidence", encoding="utf-8")
    target = source if second_tool == "read_file" else tmp_path / "target.txt"
    second_args = {"file_path": str(target)}
    if second_tool == "write_file":
        second_args["content"] = "write evidence"
    agent, session = cli._build_agent(
        ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(id="read", name="read_file", arguments={"file_path": str(source)}),
                ToolCall(id="second", name=second_tool, arguments=second_args),
            ]),
            LLMResponse(content="done"),
        ]), Config(model="scripted-demo"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True,
    )
    assert agent.chat("Handle both calls.") == "done"
    assert [call.status for call in session.store.list_tool_calls(session.run_id)] == [
        ToolCallStatus.SUCCEEDED, ToolCallStatus.SUCCEEDED,
    ]
    assert all(step.status is StepStatus.SUCCEEDED for step in session.store.list_steps(session.run_id))
    assert session.finish(RunStatus.SUCCEEDED).status is RunStatus.SUCCEEDED


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
        ScriptedLLM([]), Config(model="scripted-demo"), Permission(),
        runtime_workspace=tmp_path, runtime_enabled=True,
    )
    originals = [*ALL_TOOLS, mcp_tool]
    assert [tool.name for tool in agent.tools] == [tool.name for tool in originals]
    for original, actual in zip(originals, agent.tools):
        assert actual.schema() == original.schema()
        assert actual is not original or actual is mcp_tool
    assert agent.runtime_session is session
    session.finish(RunStatus.SUCCEEDED)


def test_disabled_runtime_keeps_original_tools_and_creates_no_database(tmp_path):
    source, target = tmp_path / "source.txt", tmp_path / "target.txt"
    source.write_text("original read\n", encoding="utf-8")
    agent, session = cli._build_agent(
        ScriptedLLM(_read_write_script(source, target)), Config(model="scripted-demo"), Permission(allow_all=True),
        runtime_workspace=tmp_path, runtime_enabled=False,
    )

    assert session is None
    assert all(tool is not original and tool.schema() == original.schema() for tool, original in zip(agent.tools, ALL_TOOLS))
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


def test_session_finish_refuses_success_with_active_work(tmp_path):
    from corecoder.agent_runtime import AgentRuntimeSession

    session = AgentRuntimeSession.open(tmp_path, "scripted-demo", Permission())
    step = session.store.create_step(session.run_id, sequence=1, title="read_file")
    session.store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    call = session.store.create_tool_call(
        run_id=session.run_id,
        tool_name="read_file",
        arguments={"file_path": str(tmp_path / "source.txt")},
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.IN_PROCESS,
        idempotent=True,
        idempotency_key="active-read",
        timeout_seconds=30,
        step_id=step.id,
    )
    session.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")

    with pytest.raises(ValueError, match="active work"):
        session.finish(RunStatus.SUCCEEDED)

    assert session.store.get_run(session.run_id).status is RunStatus.RUNNING
    assert session.store.get_tool_call(call.id).status is ToolCallStatus.RUNNING
    assert session.store.list_steps(session.run_id)[0].status is StepStatus.RUNNING


@pytest.mark.parametrize(
    "requested_status",
    [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED],
)
def test_session_finish_preserves_recoverable_run(tmp_path, requested_status):
    from corecoder.agent_runtime import AgentRuntimeSession

    session = AgentRuntimeSession.open(tmp_path, "scripted-demo", Permission())
    step = session.store.create_step(session.run_id, sequence=1, title="write_file")
    session.store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    call = session.store.create_tool_call(
        run_id=session.run_id,
        tool_name="write_file",
        arguments={"file_path": str(tmp_path / "effect.txt"), "content": "effect\n"},
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.IN_PROCESS,
        idempotent=False,
        idempotency_key=None,
        timeout_seconds=30,
        step_id=step.id,
    )
    session.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    session.store.transition_tool_call(
        call.id,
        ToolCallStatus.INTERRUPTED,
        "tool.interrupted",
        payload={"failure_kind": "termination_unknown", "reason": "execution_interrupted"},
    )
    session.store.transition_step(step.id, StepStatus.FAILED, "step.failed")
    events = session.store.list_events(session.run_id)

    finished = session.finish(requested_status)

    assert finished.status is RunStatus.RECOVERABLE
    assert session.store.list_events(session.run_id) == events


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
    (RuntimeError("offline failure"), 1, "running", "run.started"),
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


def test_one_shot_mutating_system_exit_preserves_recovery_and_effect_once(
    tmp_path, monkeypatch,
):
    class EffectThenExitTool(Tool):
        name = "write_file"
        description = "Create one effect, then simulate an abrupt process exit"
        parameters: ClassVar[dict] = {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        }

        def execute(self, file_path: str, content: str) -> str:
            with open(file_path, "a", encoding="utf-8") as marker:
                marker.write(content)
            raise SystemExit(87)

    marker = tmp_path / "effect-marker.txt"
    llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall(
        id="write-exit-87",
        name="write_file",
        arguments={"file_path": str(marker), "content": "effect\n"},
    )])])
    _configure_cli(monkeypatch, tmp_path, llm, "--yes", "-p", "create one effect")
    monkeypatch.setattr(cli, "builtin_tools", lambda: [EffectThenExitTool()])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 87
    assert marker.read_text(encoding="utf-8") == "effect\n"
    store = SQLiteStore(tmp_path / ".reliagent" / "agent-runtime.sqlite")
    store.initialize()
    [run] = store.list_runs()
    [step] = store.list_steps(run.id)
    [call] = store.list_tool_calls(run_id=run.id)
    [candidate] = RecoveryManager(
        store, ToolPolicyRegistry.with_builtin_defaults()
    ).scan(run.id)
    assert run.status is RunStatus.RECOVERABLE
    assert step.status is StepStatus.RUNNING
    assert call.status is ToolCallStatus.INTERRUPTED
    assert candidate.kind is RecoveryKind.HUMAN_REQUIRED
    assert candidate.call.id == call.id
    assert store.list_tool_calls(run_id=run.id) == [call]
    assert marker.read_text(encoding="utf-8").count("effect\n") == 1


@pytest.mark.parametrize("first_error", [None, RuntimeError("try again"), KeyboardInterrupt()])
def test_interactive_session_stays_open_between_turns_and_finishes_on_exit(tmp_path, monkeypatch, first_error):
    class RecoveringLLM(ScriptedLLM):
        def chat(self, *args, **kwargs):
            nonlocal first_error
            if first_error is not None:
                error, first_error = first_error, None
                raise error
            return super().chat(*args, **kwargs)

    had_error = first_error is not None
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
        assert db.execute("SELECT status FROM runs").fetchall() == [("running" if had_error else "succeeded",)]
        assert db.execute("SELECT COUNT(*) FROM events WHERE type = 'run.completed'").fetchone() == (0 if had_error else 1,)
        assert db.execute("SELECT COUNT(*) FROM agent_turns").fetchone() == (1 if had_error else 2,)


def test_interactive_read_tool_interrupt_blocks_new_input(
    tmp_path, monkeypatch,
):
    class InterruptOnceReadTool(Tool):
        name = "read_file"
        description = "Interrupt once, then return a read result"
        parameters: ClassVar[dict] = {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        }

        def __init__(self):
            self.calls = 0

        def execute(self, file_path: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return f"read recovered: {file_path}"

    source = tmp_path / "source.txt"
    source.write_text("runtime recovery\n", encoding="utf-8")
    tool = InterruptOnceReadTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall(
            id="read-interrupted", name="read_file",
            arguments={"file_path": str(source)},
        )]),
        LLMResponse(tool_calls=[ToolCall(
            id="read-retried-by-user", name="read_file",
            arguments={"file_path": str(source)},
        )]),
        LLMResponse(content="second turn completed"),
    ])
    _configure_cli(monkeypatch, tmp_path, llm)
    monkeypatch.setattr(cli, "builtin_tools", lambda: [tool])
    inputs = iter(["first turn", "second turn", "quit"])
    monkeypatch.setattr(cli, "pt_prompt", lambda *args, **kwargs: next(inputs))

    cli.main()

    with _database(tmp_path) as db:
        assert db.execute("SELECT status FROM runs").fetchall() == [("recoverable",)]
        assert db.execute("SELECT status FROM tool_calls").fetchall() == [("interrupted",)]
        assert db.execute("SELECT status FROM steps").fetchall() == [("running",)]
        assert db.execute("SELECT COUNT(*) FROM agent_turns").fetchone() == (1,)
    assert tool.calls == 1
