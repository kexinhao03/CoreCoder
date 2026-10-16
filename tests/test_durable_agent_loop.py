"""Production Agent/CLI integration; only the external model boundary is scripted."""
import os
import shlex
import subprocess
import sys
from copy import deepcopy

import pytest
from cryptography.fernet import Fernet

from corecoder.agent import Agent
from corecoder.agent_runtime import DurableAgentSession
from corecoder.cli import _build_agent
from corecoder.config import Config
from corecoder.llm import LLM, LLMResponse, ToolCall
from corecoder.permissions import Permission


class ScriptedLLM(LLM):
    def __init__(self, responses):
        self.model = "integration-model"
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append(deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def python_command(code):
    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def append_marker(marker):
    return python_command(f"from pathlib import Path; p = Path({str(marker)!r}); p.open('a').write('x')")


def call(name, args, identity="provider-1"):
    return LLMResponse(tool_calls=[ToolCall(identity, name, args)])


def build(tmp_path, monkeypatch, responses, *, key=None, run_id=None, permission=None):
    monkeypatch.setenv("RELIAGENT_CHECKPOINT_KEY", (key or Fernet.generate_key()).decode())
    monkeypatch.setattr("corecoder.cli.load_mcp_tools", list)
    monkeypatch.setattr("corecoder.cli.load_hooks", lambda: None)
    return _build_agent(ScriptedLLM(responses), Config(model="integration-model"),
                        permission or Permission(allow_all=True), runtime_workspace=tmp_path,
                        runtime_enabled=True, resume_run=run_id)


def test_production_builder_delegates_and_restores(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    agent, session = build(tmp_path, monkeypatch, [call("write_file", {"file_path": str(tmp_path / "a"), "content": "one"}), LLMResponse(content="done")], key=key)
    try:
        assert isinstance(session, DurableAgentSession)
        assert agent.chat("write") == "done"
        assert session.journal.list_invocations(session.run_id)[0]["result"].startswith("Wrote")
        run_id = session.run_id
    finally:
        session.close()
    restored, session = build(tmp_path, monkeypatch, [], key=key, run_id=run_id)
    try:
        assert restored.resume() == "done"
        assert restored.messages == agent.messages
        assert restored.llm.requests == []
    finally:
        session.close()


def test_key_validated_before_mcp_starts(tmp_path, monkeypatch):
    monkeypatch.delenv("RELIAGENT_CHECKPOINT_KEY", raising=False)
    started = []
    monkeypatch.setattr("corecoder.cli.load_mcp_tools", lambda: started.append(True) or [])
    with pytest.raises(ValueError, match="key"):
        _build_agent(ScriptedLLM([]), Config(model="integration-model"), Permission(),
                     runtime_workspace=tmp_path, runtime_enabled=True)
    assert started == []


def test_agents_have_independent_mutable_tools():
    one, two = Agent(ScriptedLLM([])), Agent(ScriptedLLM([]))
    assert one._todo is not two._todo
    assert one._tool_by_name["agent"]._parent_agent is one
    assert two._tool_by_name["agent"]._parent_agent is two


def test_child_has_linked_run_and_independent_todo(tmp_path, monkeypatch):
    agent, session = build(tmp_path, monkeypatch, [
        call("todo_write", {"tasks": [{"content": "parent", "status": "pending"}]}),
        call("agent", {"task": "child work"}),
        call("todo_write", {"tasks": [{"content": "child", "status": "done"}]}),
        LLMResponse(content="child done"), LLMResponse(content="parent done"),
    ])
    try:
        assert agent.chat("go") == "parent done"
        state = session.journal.load(session.run_id)["state"]
        link = next(link for link in state["linked_operations"].values() if link["kind"] == "child")
        child = session.journal.load(link["run_id"])
        assert child["state"]["todo"][0]["content"] == "child"
        assert agent._todo._tasks[0]["content"] == "parent"
        assert len(session.journal.list_invocations(link["run_id"])) == 1
        assert session.journal.list_invocations(session.run_id)[1]["result"] == "[Sub-agent completed]\nchild done"
        with session.store._connect() as connection:
            assert "child work" not in "\n".join(connection.iterdump())
    finally:
        session.close()


def test_hooks_run_after_approval_as_linked_operations(tmp_path, monkeypatch):
    from corecoder.hooks import Hooks
    marker = tmp_path / "hook"
    agent, session = build(tmp_path, monkeypatch, [])
    session.close()
    hooks = Hooks([{"matcher": "write_file", "command": python_command(f"from pathlib import Path; import sys; Path({str(marker)!r}).write_bytes(sys.stdin.buffer.read())")}], [])
    monkeypatch.setattr("corecoder.cli.load_hooks", lambda: hooks)
    asked = []
    permission = Permission(ask=lambda name, args: asked.append(name) or "once")
    agent, session = _build_agent(ScriptedLLM([
        call("write_file", {"file_path": str(tmp_path / "a"), "content": "one"}), LLMResponse(content="done")
    ]), Config(model="integration-model"), permission, runtime_workspace=tmp_path, runtime_enabled=True)
    try:
        assert agent.chat("go") == "done"
        assert asked == ["write_file"]
        assert '"tool_name": "write_file"' in marker.read_text()
        links = session.journal.load(session.run_id)["state"]["linked_operations"]
        hook = next(iter(links.values()))
        assert session.journal.list_invocations(hook["run_id"])[0]["state"] == "completed"
        with session.store._connect() as connection:
            assert hooks.pre[0]["command"] not in "\n".join(connection.iterdump())
    finally:
        session.close()


def test_managed_process_never_inherits_checkpoint_key(tmp_path, monkeypatch):
    import threading

    from corecoder.runtime.processes import ManagedProcessRunner, ProcessSpec
    monkeypatch.setenv("RELIAGENT_CHECKPOINT_KEY", "secret")
    platform_env = {k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}
    for env in (None, {**platform_env, "RELIAGENT_CHECKPOINT_KEY": "override"}):
        result = ManagedProcessRunner().run(ProcessSpec((sys.executable, "-c", "import os; print(os.environ.get('RELIAGENT_CHECKPOINT_KEY', 'unset'), end='')"), tmp_path, 2, 100, .1, env), threading.Event())
        assert result.exit_code == 0, (result.failure_kind, result.stderr)
        assert result.stdout == "unset"


def test_durable_audit_contains_no_ordinary_private_arguments(tmp_path, monkeypatch):
    private = "quarterly acquisition confidential text"
    seen = []
    agent, session = build(tmp_path, monkeypatch, [call("bash", {"command": python_command(f"print({private!r}, end='')")}), LLMResponse(content="done")],
                           permission=Permission(ask=lambda name, args: seen.append(args) or "once"))
    try:
        agent.chat("go")
        with session.store._connect() as connection:
            dump = "\n".join(connection.iterdump())
        assert private not in dump
        attempt = session.store.list_tool_calls(session.run_id)[0]
        assert not attempt.arguments_replayable
        assert set(attempt.arguments) == {"sha256", "bytes"}
        approval = session.store.get_approval_for_tool_call(attempt.id)
        assert attempt.arguments["sha256"] in approval.arguments_summary
        assert private in seen[0]["command"]
    finally:
        session.close()


@pytest.fixture
def local_mcp(tmp_path):
    import sys

    from corecoder.mcp import MCPClient
    script = tmp_path / "server.py"
    script.write_text('''import json, os, sys
from pathlib import Path
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req: continue
    if req["method"] == "initialize": result = {}
    elif req["method"] == "tools/list": result = {"tools": [{"name": "action", "inputSchema": {"type": "object"}}]}
    else:
        Path(sys.argv[1]).open("a").write("1")
        if req["params"]["arguments"].get("crash"): os._exit(9)
        result = {"content": [{"type": "text", "text": next((v for k, v in os.environ.items() if k.upper() == "RELIAGENT_CHECKPOINT_KEY"), "unset")}]}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
''')
    clients = []
    def connect(env=None):
        client = MCPClient("local", sys.executable, [str(script), str(tmp_path / "remote-marker")],
                           env if env is not None else {"RELIAGENT_CHECKPOINT_KEY": "config-override"})
        clients.append(client)
        return client
    yield connect
    for client in clients:
        client.close()


def test_mcp_key_override_filtered(local_mcp):
    assert local_mcp().call_tool("action", {}) == "unset"


def test_mcp_after_send_crash_blocks_restart(tmp_path, monkeypatch, local_mcp):
    from corecoder.runtime.executor import UnknownEffect
    key = Fernet.generate_key()
    agent, session = build(tmp_path, monkeypatch, [], key=key)
    session.close()
    client = local_mcp()
    monkeypatch.setattr("corecoder.cli.load_mcp_tools", lambda: client.tools)
    model = ScriptedLLM([call("mcp__local__action", {"crash": True, "notes": "private acquisition MCP notes"}), LLMResponse(content="must not")])
    agent, session = _build_agent(model, Config(model=model.model), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True)
    try:
        with pytest.raises(UnknownEffect):
            agent.chat("remote")
        assert len(model.requests) == 1
        with session.store._connect() as connection:
            assert "private acquisition MCP notes" not in "\n".join(connection.iterdump())
        run_id = session.run_id
    finally:
        session.close()
    client = local_mcp()
    restored, session = _build_agent(ScriptedLLM([]), Config(model=model.model), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
    try:
        for action in (restored.resume, lambda: restored.chat("new bypass"), restored.reset):
            with pytest.raises(ValueError, match="unknown"):
                action()
        assert (tmp_path / "remote-marker").read_text() == "1"
        assert restored.llm.requests == []
    finally:
        session.close()


def test_resume_original_child_after_response_crash(tmp_path, monkeypatch):
    from corecoder.runtime.agent_store import AgentJournal
    key = Fernet.generate_key()
    marker = tmp_path / "child-marker"
    agent, session = build(tmp_path, monkeypatch, [call("agent", {"task": "child task"}),
                         call("bash", {"command": append_marker(marker)})], key=key)
    original = AgentJournal.record_response
    def crash_child(journal, run_id, sequence, message):
        result = original(journal, run_id, sequence, message)
        if run_id != session.run_id:
            raise KeyboardInterrupt
        return result
    with monkeypatch.context() as patch:
        patch.setattr(AgentJournal, "record_response", crash_child)
        with pytest.raises(KeyboardInterrupt):
            agent.chat("go")
    links = session.journal.load(session.run_id)["state"]["linked_operations"]
    child_id = next(iter(links.values()))["run_id"]
    run_id = session.run_id
    session.close()
    restored, session = build(tmp_path, monkeypatch, [LLMResponse(content="child result"), LLMResponse(content="parent result")], key=key, run_id=run_id)
    try:
        assert restored.resume() == "parent result"
        assert marker.read_text() == "x"
        assert len(restored.llm.requests) == 2
        assert next(iter(session.journal.load(run_id)["state"]["linked_operations"].values()))["run_id"] == child_id
        assert session.journal.list_invocations(run_id)[0]["result"] == "[Sub-agent completed]\nchild result"
    finally:
        session.close()


@pytest.mark.parametrize("later_call", [False, True])
def test_cached_main_unknown_post_hook_blocks_later_queued_tool(tmp_path, monkeypatch, later_call):
    import sqlite3

    from corecoder.hooks import Hooks
    key = Fernet.generate_key()
    _unused, unused_session = build(tmp_path, monkeypatch, [], key=key)
    unused_session.close()
    marker, later = tmp_path / "hook-marker", tmp_path / "later"
    hooks = Hooks([], [{"matcher": "read_file", "command": append_marker(marker)}])
    monkeypatch.setattr("corecoder.cli.load_hooks", lambda: hooks)
    source = tmp_path / "source"
    source.write_text("source")
    response = LLMResponse(tool_calls=[ToolCall("first", "read_file", {"file_path": str(source)}),
                                      ToolCall("second", "write_file", {"file_path": str(later), "content": "bad"})])
    if not later_call:
        response.tool_calls.pop()
    agent, session = _build_agent(ScriptedLLM([response]), Config(model="integration-model"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True)
    run_id = session.run_id
    with session.store._connect() as connection:
        connection.execute(f"CREATE TRIGGER crash_hook BEFORE UPDATE OF status ON tool_calls WHEN NEW.run_id != '{run_id}' AND NEW.status = 'succeeded' BEGIN SELECT RAISE(ABORT, 'hook result crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="hook result crash"):
        agent.chat("go")
    assert marker.read_text() == "x"
    assert not later.exists()
    session.close()
    with session.store._connect() as connection:
        connection.execute("DROP TRIGGER crash_hook")
    restored, session = _build_agent(ScriptedLLM([LLMResponse(content="done")]), Config(model="integration-model"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
    try:
        with pytest.raises(ValueError, match="unknown"):
            restored.resume()
        assert restored.llm.requests == []
        assert not later.exists()
        assert marker.read_text() == "x"
        from corecoder.runtime import RunStatus
        assert session.finish(RunStatus.FAILED).status is RunStatus.RUNNING
        restored, session = _build_agent(ScriptedLLM([LLMResponse(content="done")]), Config(model="integration-model"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
        hook = next(iter(session.journal.load(run_id)["state"]["linked_operations"].values()))
        invocation = session.journal.list_invocations(hook["run_id"])[0]
        session.journal.reconcile(invocation["id"], "confirmed_completed", output="done", evidence={"marker": "checked"})
        assert restored.resume() == "done"
        if later_call:
            assert later.read_text() == "bad"
        assert marker.read_text() == "x"
    finally:
        session.close()


def test_undo_restores_persisted_bytes_after_restart(tmp_path, monkeypatch):
    from corecoder import checkpoints
    key = Fernet.generate_key()
    target = tmp_path / "file"
    target.write_bytes(b"original\xff")
    agent, session = build(tmp_path, monkeypatch, [call("write_file", {"file_path": str(target), "content": "new"}), LLMResponse(content="done")], key=key)
    agent.chat("write")
    run_id = session.run_id
    session.close()
    checkpoints.clear()
    restored, session = build(tmp_path, monkeypatch, [], key=key, run_id=run_id)
    try:
        assert restored.undo().startswith("Restored")
        assert target.read_bytes() == b"original\xff"
        assert restored.llm.requests == []
        invocation = session.journal.list_invocations(run_id)[-1]
        assert invocation["tool_name"] == "_undo"
        assert invocation["state"] == "completed"
    finally:
        session.close()


def test_cli_status_and_retry_inference_need_no_model(tmp_path, monkeypatch, capsys):
    from corecoder.cli import main
    key = Fernet.generate_key()
    agent, session = build(tmp_path, monkeypatch, [RuntimeError("lost inference")], key=key)
    with pytest.raises(RuntimeError):
        agent.chat("private prompt")
    run_id = session.run_id
    session.close()
    for name in ("OPENAI_API_KEY", "CORECODER_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("corecoder.cli.LLM", lambda **kwargs: pytest.fail("model constructed"))
    monkeypatch.setattr("sys.argv", ["corecoder", "--runtime-workspace", str(tmp_path), "--run-status", run_id])
    main()
    out = capsys.readouterr().out
    assert "llm_inflight" in out and "private prompt" not in out
    monkeypatch.setattr("sys.argv", ["corecoder", "--runtime-workspace", str(tmp_path), "--retry-inference", run_id, "--evidence", "provider checked; explicit repeat accepted"])
    main()
    assert session.journal.load(run_id)["phase"] == "ready"


def test_parent_cancel_at_child_approval_closes_admission(tmp_path, monkeypatch):
    from corecoder.runtime.executor import UnknownEffect
    target = tmp_path / "must-not-exist"
    parent_session = None
    def approve(name, arguments):
        if name == "write_file":
            parent_session.executor.cancel_run(parent_session.run_id)
        return "once"
    agent, parent_session = build(tmp_path, monkeypatch, [call("agent", {"task": "child"}),
                  call("write_file", {"file_path": str(target), "content": "no"}), LLMResponse(content="bad")],
                  permission=Permission(ask=approve))
    try:
        with pytest.raises((ValueError, UnknownEffect)):
            agent.chat("go")
        assert not target.exists()
        assert len(agent.llm.requests) == 2
    finally:
        parent_session.close()


def test_eight_builtins_all_use_durable_attempts_and_real_approval(tmp_path, monkeypatch):
    target = tmp_path / "source.py"
    calls = [
        ("write_file", {"file_path": str(target), "content": "original"}),
        ("edit_file", {"file_path": str(target), "old_string": "original", "new_string": "edited"}),
        ("read_file", {"file_path": str(target)}),
        ("glob", {"path": str(tmp_path), "pattern": "*.py"}),
        ("grep", {"path": str(target), "pattern": "edited"}),
        ("todo_write", {"tasks": [{"content": "complete", "status": "done"}]}),
        ("bash", {"command": python_command("print('tested', end='')")}),
        ("agent", {"task": "child inspection"}),
    ]
    asks = []
    response = LLMResponse(tool_calls=[ToolCall(str(i), name, arguments) for i, (name, arguments) in enumerate(calls)])
    agent, session = build(tmp_path, monkeypatch, [response, LLMResponse(content="child complete"), LLMResponse(content="done")],
                           permission=Permission(ask=lambda name, arguments: asks.append(name) or "once"))
    try:
        assert agent.chat("all tools") == "done"
        assert asks == ["write_file", "edit_file", "bash", "agent"]
        invocations = session.journal.list_invocations(session.run_id)
        assert [item["tool_name"] for item in invocations] == [name for name, _ in calls]
        assert all(item["attempt_id"] and item["state"] == "completed" for item in invocations)
        assert target.read_text() == "edited"
        assert "edited" in invocations[2]["result"]
        assert invocations[6]["result"] == "tested"
    finally:
        session.close()


def test_denied_undo_remains_available_and_queued_undo_keeps_exact_target(tmp_path, monkeypatch):
    from corecoder.runtime.agent_store import AgentJournal
    key = Fernet.generate_key()
    target = tmp_path / "target"
    target.write_text("before")
    agent, session = build(tmp_path, monkeypatch, [call("write_file", {"file_path": str(target), "content": "after"}), LLMResponse(content="done")], key=key)
    agent.chat("write")
    session.permission = Permission()
    assert agent.undo().startswith("Permission denied")
    assert target.read_text() == "after"
    session.permission = Permission(allow_all=True)
    original = AgentJournal.record_response
    def queued(journal, run_id, sequence, message):
        original(journal, run_id, sequence, message)
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(AgentJournal, "record_response", queued)
        with pytest.raises(KeyboardInterrupt):
            agent.undo()
    run_id = session.run_id
    session.close()
    from corecoder import checkpoints
    checkpoints.clear()
    other = tmp_path / "other"
    other.write_text("other before")
    checkpoints.record(other)
    other.write_text("other after")
    restored, session = build(tmp_path, monkeypatch, [LLMResponse(content="undo resumed")], key=key, run_id=run_id)
    try:
        assert restored.resume() == "undo resumed"
        assert target.read_text() == "before"
        assert other.read_text() == "other after"
    finally:
        session.close()


def test_changed_mcp_server_binding_rejects_restore(tmp_path, monkeypatch, local_mcp):
    key = Fernet.generate_key()
    _unused, session = build(tmp_path, monkeypatch, [], key=key)
    session.close()
    client = local_mcp()
    monkeypatch.setattr("corecoder.cli.load_mcp_tools", lambda: client.tools)
    agent, session = _build_agent(ScriptedLLM([LLMResponse(content="done")]), Config(model="integration-model"), Permission(), runtime_workspace=tmp_path, runtime_enabled=True)
    agent.chat("hi")
    run_id = session.run_id
    session.close()
    client.binding_digest = "another executable binding"
    with pytest.raises(ValueError, match="signature"):
        _build_agent(ScriptedLLM([]), Config(model="integration-model"), Permission(), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)


def test_plain_json_import_cannot_claim_durable_identity(tmp_path, monkeypatch):
    from corecoder import cli
    from corecoder import session as legacy
    key = Fernet.generate_key()
    agent, durable = build(tmp_path, monkeypatch, [RuntimeError("lost")], key=key)
    with pytest.raises(RuntimeError):
        agent.chat("unknown prompt")
    run_id = durable.run_id
    durable.close()
    monkeypatch.setattr(legacy, "SESSIONS_DIR", tmp_path / "legacy")
    sid = legacy.save_session([{"role": "user", "content": "old context"}], "integration-model")
    path = legacy._session_path(sid)
    import json
    data = json.loads(path.read_text())
    data.update(run_id=run_id, durable=True)
    path.write_text(json.dumps(data))
    monkeypatch.setattr(cli.Config, "from_env", lambda: Config(model="integration-model", api_key="test"))
    monkeypatch.setattr(cli, "LLM", lambda **kwargs: ScriptedLLM([LLMResponse(content="new independent run")]))
    monkeypatch.setattr("sys.argv", ["corecoder", "-r", sid, "--runtime-workspace", str(tmp_path), "-p", "continue"])
    cli.main()
    assert durable.journal.load(run_id)["phase"] == "llm_inflight"
    runs = durable.store.list_runs()
    assert len(runs) == 2
    imported = next(run for run in runs if run.id != run_id)
    assert durable.journal.load(imported.id)["state"]["messages"][0]["content"] == "old context"


def test_legacy_shell_and_hooks_also_filter_checkpoint_key(tmp_path, monkeypatch):

    from corecoder.hooks import Hooks
    from corecoder.tools.bash import BashTool
    monkeypatch.setenv("RELIAGENT_CHECKPOINT_KEY", "must stay in host")
    assert BashTool().execute(python_command("import os; print(os.environ.get('RELIAGENT_CHECKPOINT_KEY', 'unset'), end='')")) == "unset"
    marker = tmp_path / "legacy-hook"
    hooks = Hooks([{"matcher": "*", "command": python_command(f"from pathlib import Path; import os; Path({str(marker)!r}).write_text(os.environ.get('RELIAGENT_CHECKPOINT_KEY', 'unset'))")}], [])
    hooks.run_pre("write_file", {})
    assert marker.read_text() == "unset"


def test_managed_stdin_survives_poll_timeout_once(tmp_path):
    import sys
    import threading

    from corecoder.runtime.processes import ManagedProcessRunner, ProcessSpec
    payload = b"quoted ' unicode \xe4\xb8\xad\n" * 100
    spec = ProcessSpec((sys.executable, "-c", "import sys,time; data=sys.stdin.buffer.read(); time.sleep(.15); sys.stdout.buffer.write(data)"), tmp_path, 2, 5000, .1, stdin=payload)
    result = ManagedProcessRunner().run(spec, threading.Event())
    assert result.stdout == payload.decode()
    assert result.exit_code == 0


def test_post_hook_recovers_pre_dispatch_crash_without_model(tmp_path, monkeypatch):
    from corecoder.hooks import Hooks
    from corecoder.runtime.agent_store import AgentJournal
    key = Fernet.generate_key()
    _unused, session = build(tmp_path, monkeypatch, [], key=key)
    session.close()
    marker = tmp_path / "hook"
    hooks = Hooks([], [{"matcher": "write_file", "command": append_marker(marker)}])
    monkeypatch.setattr("corecoder.cli.load_hooks", lambda: hooks)
    agent, session = _build_agent(ScriptedLLM([call("write_file", {"file_path": str(tmp_path / "file"), "content": "once"})]), Config(model="integration-model"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True)
    original = AgentJournal.record_response
    def crash_before_response(journal, run_id, sequence, message):
        if run_id != session.run_id:
            raise KeyboardInterrupt
        return original(journal, run_id, sequence, message)
    with monkeypatch.context() as patch:
        patch.setattr(AgentJournal, "record_response", crash_before_response)
        with pytest.raises(KeyboardInterrupt):
            agent.chat("go")
    run_id = session.run_id
    session.close()
    assert not marker.exists()
    restored, session = _build_agent(ScriptedLLM([LLMResponse(content="done")]), Config(model="integration-model"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
    try:
        assert restored.resume() == "done"
        assert marker.read_text() == "x"
        assert len(restored.llm.requests) == 1
    finally:
        session.close()


@pytest.mark.skipif(os.name != "posix", reason="descendant termination guarantee requires POSIX process groups; Windows is best-effort")
def test_parent_cancellation_terminates_active_post_hook(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from corecoder.hooks import Hooks
    ready, late = tmp_path / "hook-ready", tmp_path / "hook-late"
    _unused, initial = build(tmp_path, monkeypatch, [])
    initial.close()
    code = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(1.5); Path({str(late)!r}).touch()"
    hooks = Hooks([], [{"matcher": "write_file", "command": python_command(code)}])
    monkeypatch.setattr("corecoder.cli.load_hooks", lambda: hooks)
    agent, session = _build_agent(ScriptedLLM([call("write_file", {"file_path": str(tmp_path / "target"), "content": "done"})]), Config(model="integration-model"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(agent.chat, "go")
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert ready.exists()
            session.executor.cancel_run(session.run_id)
            with pytest.raises((ValueError, RuntimeError)):
                work.result(timeout=5)
        time.sleep(1.6)
        assert not late.exists()
        assert (tmp_path / "target").read_text() == "done"
        assert len(agent.llm.requests) == 1
    finally:
        session.close()


def test_unknown_pre_hook_blocks_after_main_retry_denied(tmp_path, monkeypatch):
    from corecoder.hooks import Hooks
    from corecoder.runtime import ApprovalDecision, RunStatus, ToolCallStatus
    from corecoder.runtime.executor import UnknownEffect

    key = Fernet.generate_key()
    _unused, initial = build(tmp_path, monkeypatch, [], key=key)
    initial.close()
    marker, target, post_marker = tmp_path / "pre-marker", tmp_path / "target", tmp_path / "post-marker"
    monkeypatch.setattr("corecoder.cli.load_hooks", lambda: Hooks([{"matcher": "write_file", "command": append_marker(marker)}], [{"matcher": "write_file", "command": append_marker(post_marker)}]))
    agent, session = _build_agent(ScriptedLLM([call("write_file", {"file_path": str(target), "content": "never"})]), Config(model="integration-model"), Permission(allow_all=True), runtime_workspace=tmp_path, runtime_enabled=True)
    run_id = session.run_id
    with session.store._connect() as connection:
        connection.execute(f"CREATE TRIGGER lose_pre_result BEFORE UPDATE OF status ON tool_calls WHEN NEW.run_id != '{run_id}' AND NEW.status = 'succeeded' BEGIN SELECT RAISE(ABORT, 'lost pre result'); END")
    with pytest.raises(UnknownEffect):
        agent.chat("go")
    assert marker.read_text() == "x" and not target.exists()
    with session.store._connect() as connection:
        connection.execute("DROP TRIGGER lose_pre_result")
    main = session.journal.list_invocations(run_id)[0]
    session.journal.reconcile(main["id"], "confirmed_not_executed", evidence={"target": "absent; pre hook prevented main dispatch"})
    retry = session.journal.list_invocations(run_id)[0]
    approval = session.store.get_approval_for_tool_call(retry["attempt_id"])
    # A fresh explicit Runtime denial settles only the main invocation.
    session.store.resolve_approval(approval.id, ApprovalDecision.DENY,
                                   agent_result=session.journal.result_token(main["id"], "Permission denied"))
    session.close()
    restored, session = _build_agent(ScriptedLLM([LLMResponse(content="done")]), Config(model="integration-model"), Permission(), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
    try:
        for action in (restored.resume, lambda: restored.chat("bypass"), restored.reset, session.export):
            with pytest.raises(ValueError, match="unknown"):
                action()
        assert restored.llm.requests == []
        assert not post_marker.exists()
        assert session.finish(RunStatus.SUCCEEDED).status is RunStatus.RUNNING
        restored, session = _build_agent(ScriptedLLM([LLMResponse(content="done")]), Config(model="integration-model"), Permission(), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
        link = next(link for link in session.journal.load(run_id)["state"]["linked_operations"].values() if link["kind"] == "pre")
        hook = session.journal.list_invocations(link["run_id"])[0]
        if session.store.get_tool_call(hook["attempt_id"]).status is ToolCallStatus.RUNNING:
            session.store.transition_tool_call(hook["attempt_id"], ToolCallStatus.INTERRUPTED, "tool.interrupted")
        session.journal.reconcile(hook["id"], "confirmed_completed", output="", evidence={"marker": "checked exactly one append"})
        assert restored.resume() == "done"
        assert marker.read_text() == "x" and not target.exists()
    finally:
        session.close()


@pytest.mark.parametrize("damage", ["wrong_key", "context", "arguments"])
def test_resume_authenticates_before_mcp_start(tmp_path, monkeypatch, damage):
    key = Fernet.generate_key()
    agent, session = build(tmp_path, monkeypatch, [call("todo_write", {"tasks": []}), LLMResponse(content="done")], key=key)
    agent.chat("go")
    run_id = session.run_id
    session.close()
    if damage == "wrong_key":
        monkeypatch.setenv("RELIAGENT_CHECKPOINT_KEY", Fernet.generate_key().decode())
    else:
        table, column = ("agent_sessions", "context_ciphertext") if damage == "context" else ("agent_invocations", "arguments_ciphertext")
        with session.store._connect() as connection:
            connection.execute(f"UPDATE {table} SET {column} = ? WHERE run_id = ?", (b"damaged", run_id))
    started = []
    monkeypatch.setattr("corecoder.cli.load_mcp_tools", lambda: started.append(True) or [])
    with pytest.raises(ValueError, match="authentication"):
        _build_agent(ScriptedLLM([]), Config(model="integration-model"), Permission(), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
    assert started == []
    from corecoder.runtime.agent_store import RunLease
    with RunLease(tmp_path, run_id):
        pass  # failed preflight released ownership


def test_mixed_case_checkpoint_keys_filtered_in_every_subprocess(tmp_path, monkeypatch, local_mcp):
    import threading

    from corecoder.hooks import Hooks
    from corecoder.runtime.processes import ManagedProcessRunner, ProcessSpec
    from corecoder.tools.bash import BashTool

    inspect_env = "import os; print(','.join(k for k in os.environ if k.upper() == 'RELIAGENT_CHECKPOINT_KEY'), end='')"
    env = {"reliagent_checkpoint_key": "lower", "ReliAgent_Checkpoint_Key": "mixed", "KEPT_VALUE": "kept"}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    platform_env = {k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}
    result = ManagedProcessRunner().run(ProcessSpec((sys.executable, "-c", inspect_env + "; print(os.environ['KEPT_VALUE'], end='')"), tmp_path, 2, 100, .1, {**platform_env, **env}), threading.Event())
    assert result.exit_code == 0, (result.failure_kind, result.stderr)
    assert result.stdout == "kept"
    assert BashTool().execute(python_command(inspect_env)) == "(no output)"
    marker = tmp_path / "env-hook"
    hook_code = f"import os; from pathlib import Path; Path({str(marker)!r}).write_text(','.join(k for k in os.environ if k.upper() == 'RELIAGENT_CHECKPOINT_KEY') + os.environ['KEPT_VALUE'])"
    Hooks([{"matcher": "*", "command": python_command(hook_code)}], []).run_pre("write_file", {})
    assert marker.read_text() == "kept"
    client = local_mcp(env)
    assert client.call_tool("action", {}) == "unset"
    clean_client = local_mcp({"KEPT_VALUE": "kept"})
    assert client.binding_digest == clean_client.binding_digest


def test_unsettled_child_blocks_independently_of_main_reconciliation(tmp_path, monkeypatch):
    from corecoder.runtime import RunStatus
    from corecoder.runtime.executor import UnknownEffect

    key = Fernet.generate_key()
    agent, session = build(tmp_path, monkeypatch, [call("agent", {"task": "child"}), RuntimeError("child inference lost")], key=key)
    with pytest.raises(UnknownEffect):
        agent.chat("go")
    parent = session.journal.list_invocations(session.run_id)[0]
    session.journal.reconcile(parent["id"], "confirmed_completed", output="operator parent result", evidence={"parent": "inspected"})
    run_id = session.run_id
    session.close()
    restored, session = build(tmp_path, monkeypatch, [], key=key, run_id=run_id)
    try:
        for action in (restored.resume, lambda: restored.chat("bypass"), restored.reset, session.export):
            with pytest.raises(ValueError, match="linked child"):
                action()
        assert restored.llm.requests == []
        assert session.finish(RunStatus.SUCCEEDED).status is RunStatus.RUNNING
    finally:
        session.close()


def test_builder_holds_preflight_lease_and_releases_it_on_loader_failure(tmp_path, monkeypatch):
    from corecoder.runtime.agent_store import RunLease

    key = Fernet.generate_key()
    agent, session = build(tmp_path, monkeypatch, [LLMResponse(content="done")], key=key)
    agent.chat("go")
    run_id = session.run_id
    session.close()
    def fail_loader():
        with pytest.raises(ValueError, match="owned"), RunLease(tmp_path, run_id):
            pass
        raise RuntimeError("loader failed")
    monkeypatch.setattr("corecoder.cli.load_mcp_tools", fail_loader)
    with pytest.raises(RuntimeError, match="loader failed"):
        _build_agent(ScriptedLLM([]), Config(model="integration-model"), Permission(), runtime_workspace=tmp_path, runtime_enabled=True, resume_run=run_id)
    with RunLease(tmp_path, run_id):
        pass
    restored, session = build(tmp_path, monkeypatch, [], key=key, run_id=run_id)
    try:
        assert restored.resume() == "done"
    finally:
        session.close()
