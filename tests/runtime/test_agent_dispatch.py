"""Real Agent dispatch through encrypted Runtime boundaries (provider double only)."""

import os
import shlex
import sqlite3
import subprocess
import sys
from copy import deepcopy
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet

from corecoder import agent_runtime
from corecoder.agent import Agent
from corecoder.llm import LLM, LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.runtime.state import RunStatus, ToolCallStatus
from corecoder.tools.base import Tool
from corecoder.tools.bash import BashTool
from corecoder.tools.todo import TodoWriteTool


class CountingLLM(LLM):
    def __init__(self, responses):
        self.model = "counting-model"
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append(deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class EffectTool(Tool):
    name = "write_file"
    description = "Record one effect"
    parameters: ClassVar[dict] = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    def __init__(self):
        self.effects = []
        self.interrupt = False

    def execute(self, value):
        self.effects.append(value)
        if self.interrupt:
            raise KeyboardInterrupt
        return "full result: " + value


def open_session(tmp_path, key, run_id=None):
    session_type = getattr(agent_runtime, "DurableAgentSession", None)
    assert session_type is not None, "DurableAgentSession must own real Agent dispatch"
    return session_type.open(tmp_path, "counting-model", Permission(allow_all=True), key=key, run_id=run_id)


def response(name="write_file", args=None, call_id="provider-A"):
    return LLMResponse(tool_calls=[ToolCall(call_id, name, args or {"value": "once"})])


def test_cached_call_is_not_reexecuted(tmp_path):
    key, tool = Fernet.generate_key(), EffectTool()
    agent = Agent(CountingLLM([response(), LLMResponse(content="done")]), tools=[tool])
    session = open_session(tmp_path, key)
    session.attach(agent)
    # Crash the turn boundary after the full result transaction committed.
    with session.store._connect() as connection:
        connection.execute("CREATE TRIGGER crash_turn BEFORE UPDATE OF status ON agent_turns WHEN NEW.status = 'complete' BEGIN SELECT RAISE(ABORT, 'crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="crash"):
        session.chat(agent, "do it")
    assert tool.effects == ["once"]
    run_id = session.run_id
    session.close()
    with session.store._connect() as connection:
        connection.execute("DROP TRIGGER crash_turn")
    restored = Agent(CountingLLM([LLMResponse(content="done")]), tools=[tool])
    session = open_session(tmp_path, key, run_id)
    try:
        session.attach(restored)
        assert session.resume(restored) == "done"
        assert tool.effects == ["once"]
        assert restored.messages[2] == {"role": "tool", "tool_call_id": "provider-A", "content": "full result: once"}
    finally:
        session.close()


@pytest.mark.parametrize("kind", ["unknown", "invalid", "malformed", "plan", "denied"])
def test_every_refusal_is_journaled(tmp_path, kind):
    key, tool = Fernet.generate_key(), EffectTool()
    tc = ToolCall("exact-provider-id", "missing" if kind == "unknown" else tool.name, {"wrong": 1} if kind == "invalid" else {"value": "no"})
    if kind == "malformed":
        tc.raw_arguments = "{invalid"
    model = CountingLLM([LLMResponse(tool_calls=[tc]), LLMResponse(content="settled")])
    agent = Agent(model, tools=[tool])
    session = open_session(tmp_path, key)
    session.attach(agent)
    agent.plan_mode = kind == "plan"
    if kind == "denied":
        session.permission = Permission()
    try:
        assert session.chat(agent, "try") == "settled"
        invocation = session.journal.list_invocations(session.run_id)[0]
        assert invocation["state"] == "completed"
        assert invocation["provider_call_id"] == "exact-provider-id"
        assert invocation["result"] == agent.messages[2]["content"]
        assert tool.effects == []
    finally:
        session.close()


def test_started_unknown_blocks_batch_new_input_and_model(tmp_path):
    key, tool = Fernet.generate_key(), EffectTool()
    tool.interrupt = True
    model = CountingLLM([LLMResponse(tool_calls=[ToolCall("first", tool.name, {"value": "first"}), ToolCall("second", tool.name, {"value": "second"})])])
    agent = Agent(model, tools=[tool])
    session = open_session(tmp_path, key)
    session.attach(agent)
    try:
        with pytest.raises(KeyboardInterrupt):
            session.chat(agent, "go")
        assert tool.effects == ["first"]
        with pytest.raises(ValueError, match="unknown|blocked|unfinished"):
            session.chat(agent, "try again")
        with pytest.raises(ValueError, match="unknown|blocked"):
            session.resume(agent)
        assert len(model.requests) == 1
        assert not any(m.get("content") == "[interrupted]" for m in agent.messages)
        invocations = session.journal.list_invocations(session.run_id)
        assert invocations[0]["state"] == "blocked"
        assert invocations[1]["state"] == "queued"
    finally:
        session.close()


def test_lost_inference_needs_explicit_authorization(tmp_path):
    model = CountingLLM([ConnectionError("response lost"), LLMResponse(content="authorized")])
    agent = Agent(model, tools=[])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        with pytest.raises(ConnectionError):
            session.chat(agent, "go")
        with pytest.raises(ValueError, match="inference|unknown|blocked"):
            session.resume(agent)
        with pytest.raises(ValueError):
            session.chat(agent, "bypass")
        assert len(model.requests) == 1
        session.journal.authorize_llm_retry(session.run_id, evidence={"operator": "explicit retry"})
        assert session.resume(agent) == "authorized"
        assert agent.messages[0]["content"] == "go"
    finally:
        session.close()


def test_bash_uses_original_arguments_and_persists_full_formatted_output(tmp_path):
    argv = [sys.executable, "-c", "import sys; sys.stdout.write('error'); sys.exit(7)"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    model = CountingLLM([response("bash", {"command": command}), LLMResponse(content="done")])
    agent = Agent(model, tools=[BashTool()])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        session.chat(agent, "shell")
        invocation = session.journal.list_invocations(session.run_id)[0]
        assert invocation["result"] == "error\n[exit code: 7]"
        assert agent.messages[2]["content"] == invocation["result"]
        assert session.store.get_tool_call(invocation["attempt_id"]).status is ToolCallStatus.FAILED
    finally:
        session.close()


def test_todo_and_cwd_survive_result_commit_crash(tmp_path):
    key = Fernet.generate_key()
    subdir = tmp_path / "child"
    subdir.mkdir()
    todo = TodoWriteTool()
    model = CountingLLM([response("todo_write", {"tasks": [{"content": "retain", "status": "done"}]}), response("bash", {"command": "cd child"})])
    agent = Agent(model, tools=[todo, BashTool()])
    session = open_session(tmp_path, key)
    session.attach(agent)
    with session.store._connect() as connection:
        connection.execute("CREATE TRIGGER crash_turn BEFORE UPDATE OF status ON agent_turns WHEN NEW.status = 'complete' AND NEW.sequence = 2 BEGIN SELECT RAISE(ABORT, 'crash'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            session.chat(agent, "update")
        saved = session.journal.load(session.run_id)["state"]
        assert saved["todo"] == [{"content": "retain", "status": "done"}]
        assert saved["cwd"] == str(subdir)
    finally:
        session.close()


def test_wrong_key_metadata_and_concurrent_ownership_reject_before_effects(tmp_path):
    key = Fernet.generate_key()
    session = open_session(tmp_path, key)
    agent = Agent(CountingLLM([]), tools=[])
    session.attach(agent)
    run_id = session.run_id
    with pytest.raises(ValueError, match="owned"):
        other = open_session(tmp_path, key, run_id)
        other.attach(agent)
    session.close()
    other = open_session(tmp_path, Fernet.generate_key(), run_id)
    with pytest.raises(ValueError, match="authentication"):
        other.attach(agent)
    changed = open_session(tmp_path, key, run_id)
    agent.llm.model = "different-model"
    with pytest.raises(ValueError, match="model|signature|metadata"):
        changed.attach(agent)


def test_final_assistant_allows_another_user_turn(tmp_path):
    agent = Agent(CountingLLM([LLMResponse(content="one"), LLMResponse(content="two")]), tools=[])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        assert session.chat(agent, "first") == "one"
        assert session.chat(agent, "second") == "two"
        assert session.store.get_run(session.run_id).status is RunStatus.RUNNING
    finally:
        session.close()


@pytest.mark.parametrize("approval_state", ["created", "pending", "approved"])
def test_restore_unstarted_attempt_reuses_identity_and_approval(tmp_path, approval_state):
    key, tool = Fernet.generate_key(), EffectTool()
    session = open_session(tmp_path, key)
    agent = Agent(CountingLLM([]), tools=[tool])
    session.attach(agent)
    session.journal.save_state(session.run_id, {**session.journal.load(session.run_id)["state"], "messages": [{"role": "user", "content": "go"}], "effective_messages": [{"role": "user", "content": "go"}], "effective_count": 1})
    seq = session.journal.begin_turn(session.run_id, {"messages": agent._full_messages()})
    invocation = session.journal.record_response(session.run_id, seq, response().message)[0]
    from corecoder.runtime.approvals import ApprovalDecision
    from corecoder.runtime.state import ExecutionKind, RiskLevel
    call = session.store.create_tool_call(run_id=session.run_id, tool_name=tool.name,
                                         arguments={"value": "once"}, risk_level=RiskLevel.MUTATING,
                                         execution_kind=ExecutionKind.IN_PROCESS, idempotent=False, idempotency_key=None,
                                         timeout_seconds=30, agent_invocation_id=invocation["id"])
    approval = None
    if approval_state != "created":
        approval = session.store.request_approval(call.id, workspace=str(tmp_path), arguments_summary="once", risk_reason="mutates")
    if approval_state == "approved":
        session.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    session.close()
    session = open_session(tmp_path, key, session.run_id)
    restored = Agent(CountingLLM([LLMResponse(content="done")]), tools=[tool])
    approvals_seen = []
    session.permission = Permission(ask=lambda *args: approvals_seen.append(args) or "once")
    session.attach(restored)
    try:
        assert session.resume(restored) == "done"
        assert tool.effects == ["once"]
        calls = session.store.list_tool_calls(run_id=session.run_id)
        assert [item.id for item in calls] == [call.id]
        assert len(approvals_seen) == (0 if approval_state == "approved" else 1)
        if approval is not None:
            assert session.store.get_approval_for_tool_call(call.id).id == approval.id
    finally:
        session.close()


def test_response_transaction_failure_executes_nothing(tmp_path):
    tool = EffectTool()
    agent = Agent(CountingLLM([response()]), tools=[tool])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    with session.store._connect() as connection:
        connection.execute("CREATE TRIGGER fail_batch BEFORE INSERT ON agent_invocations BEGIN SELECT RAISE(ABORT, 'batch failed'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="batch failed"):
            session.chat(agent, "go")
        assert tool.effects == []
        assert session.journal.load(session.run_id)["phase"] == "llm_inflight"
        assert session.journal.list_invocations(session.run_id) == []
        with pytest.raises(ValueError):
            session.resume(agent)
    finally:
        session.close()


def test_compression_keeps_raw_history_and_has_no_hidden_inference(tmp_path):
    model = CountingLLM([LLMResponse(content="long reply " * 100)] * 10)
    agent = Agent(model, tools=[], max_context_tokens=120)
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        for i in range(8):
            session.chat(agent, f"message {i}")
        state = session.journal.load(session.run_id)["state"]
        assert len(state["messages"]) == 16
        assert state["messages"][0] == {"role": "user", "content": "message 0"}
        assert len(state["effective_messages"]) < 16
        assert len(model.requests) == 8
        assert "context reset" in str(model.requests[-1]).lower() or "compressed" in str(model.requests[-1]).lower()
    finally:
        session.close()


def test_mutated_tool_schema_blocks_before_model(tmp_path):
    agent = Agent(CountingLLM([LLMResponse(content="should not run")]), tools=[EffectTool()])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    agent.tools[0].parameters = {"type": "object", "properties": {"changed": {"type": "string"}}}
    try:
        with pytest.raises(ValueError, match="metadata|signature"):
            session.chat(agent, "go")
        assert agent.llm.requests == []
    finally:
        session.close()


def test_terminal_state_updates_are_narrow_and_rollback_with_result(tmp_path):
    todo = TodoWriteTool()
    agent = Agent(CountingLLM([response("todo_write", {"tasks": [{"content": "retained", "status": "done"}]})]), tools=[todo])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    with session.store._connect() as connection:
        connection.execute("CREATE TRIGGER fail_terminal BEFORE UPDATE OF status ON tool_calls WHEN NEW.status = 'succeeded' BEGIN SELECT RAISE(ABORT, 'terminal failed'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            session.chat(agent, "go")
        state = session.journal.load(session.run_id)["state"]
        invocation = session.journal.list_invocations(session.run_id)[0]
        assert state["todo"] == []
        assert invocation["result"] is None
        assert not any(m["role"] == "tool" for m in state["messages"])
        with pytest.raises(ValueError, match="only todo and cwd"):
            session.journal.result_token(invocation["id"], "fake", state_updates={"phase": "ready"})
        with pytest.raises(ValueError, match="unknown"):
            session.resume(agent)
    finally:
        session.close()


def test_ready_reset_export_and_cancel_gates(tmp_path):
    agent = Agent(CountingLLM([ConnectionError("lost")]), tools=[])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        exported = session.export()
        assert exported["run_id"] == session.run_id
        assert "messages" not in exported
        with pytest.raises(ConnectionError):
            session.chat(agent, "secret")
        for action in [lambda: session.reset(agent), session.export]:
            with pytest.raises(ValueError, match="unfinished|unknown"):
                action()
        session.executor.cancel_run(session.run_id)
        with pytest.raises(ValueError, match="cancelled"):
            session.resume(agent)
    finally:
        session.close()


def test_finish_preserves_unknown_inference_for_explicit_retry(tmp_path):
    key = Fernet.generate_key()
    agent = Agent(CountingLLM([ConnectionError("lost")]), tools=[])
    session = open_session(tmp_path, key)
    session.attach(agent)
    with pytest.raises(ConnectionError):
        session.chat(agent, "original")
    run = session.finish(RunStatus.FAILED)
    assert run.status not in {RunStatus.FAILED, RunStatus.SUCCEEDED}
    session.close()
    restored = Agent(CountingLLM([LLMResponse(content="retried")]), tools=[])
    session = open_session(tmp_path, key, run.id)
    session.attach(restored)
    try:
        session.journal.authorize_llm_retry(run.id, evidence={"operator": "authorized"})
        assert session.resume(restored) == "retried"
    finally:
        session.close()


def test_callback_boundaries_cover_subprocess_and_cached_post_operation(tmp_path):
    key = Fernet.generate_key()
    marker = tmp_path / "effect"
    model = CountingLLM([response("bash", {"command": "printf once > effect"}), LLMResponse(content="done")])
    agent = Agent(model, tools=[BashTool()])
    session = open_session(tmp_path, key)
    session.attach(agent)
    observed = []

    def before(agent, invocation, cancellation):
        assert session.store.get_tool_call(invocation["attempt_id"]).status is ToolCallStatus.RUNNING
        assert not marker.exists()
        observed.append("before")

    def after(agent, invocation, output):
        assert marker.read_text() == "once"
        assert invocation["state"] == "completed"
        observed.append("after")
        raise ConnectionError("post hook requires recovery")

    session.before_execution = before
    session.after_result = after
    with pytest.raises(ConnectionError, match="post hook"):
        session.chat(agent, "go")
    assert observed == ["before", "after"]
    session.close()
    restored = Agent(CountingLLM([LLMResponse(content="done")]), tools=[BashTool()])
    session = open_session(tmp_path, key, session.run_id)
    session.attach(restored)
    session.before_inference = lambda agent: (_ for _ in ()).throw(ValueError("pending post hook"))
    try:
        with pytest.raises(ValueError, match="post hook"):
            session.resume(restored)
        with pytest.raises(ValueError):
            session.chat(restored, "bypass")
        assert restored.llm.requests == []
        assert marker.read_text() == "once"
        session.before_inference = None
        assert session.resume(restored) == "done"
    finally:
        session.close()


def test_inconsistent_ready_checkpoint_cannot_bypass_queued_invocation(tmp_path):
    tool = EffectTool()
    agent = Agent(CountingLLM([]), tools=[tool])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    seq = session.journal.begin_turn(session.run_id, {"messages": agent._full_messages()})
    session.journal.record_response(session.run_id, seq, response().message)
    with session.store._connect() as connection:
        connection.execute("UPDATE agent_sessions SET phase = 'ready' WHERE run_id = ?", (session.run_id,))
    try:
        with pytest.raises(ValueError, match="unfinished|unknown|invocation"):
            session.chat(agent, "bypass")
        with pytest.raises(ValueError, match="unfinished|unknown|invocation"):
            session.resume(agent)
        assert agent.llm.requests == []
        assert tool.effects == []
    finally:
        session.close()


def test_tool_exception_has_failed_attempt_and_exact_model_error(tmp_path):
    class BrokenTool(EffectTool):
        def execute(self, value):
            raise RuntimeError("specific failure")

    agent = Agent(CountingLLM([response(), LLMResponse(content="handled")]), tools=[BrokenTool()])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        assert session.chat(agent, "go") == "handled"
        invocation = session.journal.list_invocations(session.run_id)[0]
        assert invocation["result"] == "Error executing write_file: specific failure"
        assert session.store.get_tool_call(invocation["attempt_id"]).status is ToolCallStatus.FAILED
    finally:
        session.close()


def test_restored_running_read_only_blocks_instead_of_replaying(tmp_path):
    from corecoder.runtime.state import ExecutionKind, RiskLevel

    tool = EffectTool()
    tool.name = "read_file"
    agent = Agent(CountingLLM([]), tools=[tool])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    seq = session.journal.begin_turn(session.run_id, {"messages": agent._full_messages()})
    invocation = session.journal.record_response(session.run_id, seq, response("read_file").message)[0]
    call = session.store.create_tool_call(run_id=session.run_id, tool_name=tool.name, arguments={"value": "once"},
                                         risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.IN_PROCESS,
                                         idempotent=True, idempotency_key="one", timeout_seconds=30,
                                         agent_invocation_id=invocation["id"])
    session.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    try:
        with pytest.raises(ValueError, match="unknown"):
            session.resume(agent)
        assert session.store.get_tool_call(call.id).status is ToolCallStatus.INTERRUPTED
        assert agent.llm.requests == []
        assert tool.effects == []
    finally:
        session.close()


def test_executor_rejects_changed_original_arguments_for_existing_attempt(tmp_path):
    from corecoder.runtime.state import ExecutionKind, RiskLevel

    tool = EffectTool()
    agent = Agent(CountingLLM([]), tools=[tool])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    seq = session.journal.begin_turn(session.run_id, {"messages": agent._full_messages()})
    invocation = session.journal.record_response(session.run_id, seq, response().message)[0]
    call = session.store.create_tool_call(run_id=session.run_id, tool_name=tool.name, arguments={"value": "once"},
                                         risk_level=RiskLevel.MUTATING, execution_kind=ExecutionKind.IN_PROCESS,
                                         idempotent=False, idempotency_key=None, timeout_seconds=30,
                                         agent_invocation_id=invocation["id"])
    invocation = session.journal.list_invocations(session.run_id)[0]
    try:
        with pytest.raises(ValueError, match="arguments|identity"):
            session.executor.execute_agent_invocation(
                session.journal, invocation, {"value": "changed"},
                operation=lambda args, cancellation: tool.execute(**args),
                decide_approval=lambda approval, args: None,
                finalize=lambda output, failure, exit_code: (output, {}),
            )
        assert session.store.get_tool_call(call.id).status is ToolCallStatus.CREATED
        assert tool.effects == []
    finally:
        session.close()


def test_durable_attempt_never_uses_legacy_automatic_retry(tmp_path):
    from corecoder.runtime.policies import FailureKind, ToolPolicy, ToolPolicyRegistry
    from corecoder.runtime.state import ExecutionKind, RiskLevel

    agent = Agent(CountingLLM([response("bash", {"command": "exit 9"}), LLMResponse(content="handled")]), tools=[BashTool()])
    session = open_session(tmp_path, Fernet.generate_key())
    policies = ToolPolicyRegistry({"bash": ToolPolicy(RiskLevel.READ_ONLY, ExecutionKind.SUBPROCESS, 120,
                                                     2, True, True, frozenset({FailureKind.NONZERO_EXIT}), 15_000)})
    session.policies = policies
    session.executor._policies = policies
    session.attach(agent)
    try:
        assert session.chat(agent, "go") == "handled"
        assert len(session.store.list_tool_calls(run_id=session.run_id)) == 1
        assert agent.messages[2]["content"] == "[exit code: 9]"
    finally:
        session.close()


def test_round_limit_does_not_abandon_last_committed_batch(tmp_path):
    tool = EffectTool()
    agent = Agent(CountingLLM([response()]), tools=[tool], max_rounds=1)
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        assert "maximum" in session.chat(agent, "go")
        assert tool.effects == ["once"]
        assert session.journal.list_invocations(session.run_id)[0]["state"] == "completed"
    finally:
        session.close()


def test_finish_requires_ownership_and_explicit_cancel_handles_unknown(tmp_path):
    key = Fernet.generate_key()
    tool = EffectTool()
    tool.interrupt = True
    agent = Agent(CountingLLM([response()]), tools=[tool])
    session = open_session(tmp_path, key)
    session.attach(agent)
    other = open_session(tmp_path, key, session.run_id)
    try:
        with pytest.raises(ValueError, match="own"):
            other.finish(RunStatus.FAILED)
        with pytest.raises(KeyboardInterrupt):
            session.chat(agent, "go")
        assert session.finish(RunStatus.CANCELLED).status is RunStatus.CANCELLED
    finally:
        session.close()
        other.close()


@pytest.mark.parametrize("failure", ["connection", "timeout", "server"])
def test_real_llm_durable_request_does_not_retry_uncertainty(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    from openai import APIConnectionError, APITimeoutError, InternalServerError

    from corecoder.llm import LLM

    request = httpx.Request("POST", "https://model.invalid/v1/chat/completions")
    errors = {
        "connection": APIConnectionError(request=request),
        "timeout": APITimeoutError(request=request),
        "server": InternalServerError("unavailable", response=httpx.Response(503, request=request), body=None),
    }
    attempts = []
    retry_options = []

    def create(**params):
        attempts.append(params)
        if len(attempts) == 1:
            raise errors[failure]
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="authorized"))], usage=None)])

    class Client:
        base_url = "https://model.invalid/v1"
        max_retries = 2
        chat = SimpleNamespace(completions=SimpleNamespace(create=create))

        def with_options(self, **options):
            retry_options.append(options)
            return self

    llm = object.__new__(LLM)
    llm.model, llm.client, llm.extra = "counting-model", Client(), {}
    llm.total_prompt_tokens = llm.total_completion_tokens = 0
    monkeypatch.setattr("corecoder.llm.time.sleep", lambda seconds: None)
    agent = Agent(llm, tools=[])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        with pytest.raises(type(errors[failure])):
            session.chat(agent, "go")
        assert len(attempts) == 1
        assert retry_options == [{"max_retries": 0}]
        assert session.journal.load(session.run_id)["phase"] == "llm_inflight"
        with pytest.raises(ValueError):
            session.resume(agent)
        assert len(attempts) == 1
        session.journal.authorize_llm_retry(session.run_id, evidence={"operator": "explicit authorization"})
        assert session.resume(agent) == "authorized"
        assert len(attempts) == 2
    finally:
        session.close()


@pytest.mark.parametrize("backend", ["openai", "litellm"])
@pytest.mark.parametrize("mutation", ["restore", "live"])
def test_supported_backend_endpoint_changes_block_before_model(tmp_path, backend, mutation):
    from types import SimpleNamespace

    from corecoder.llm import LiteLLM

    class CountingLiteLLM(LiteLLM):
        def __init__(self, endpoint):
            super().__init__("counting-model", api_key="synthetic-credential", base_url=endpoint)
            self.requests = []

        def chat(self, messages, tools=None, on_token=None):
            self.requests.append(messages)
            return LLMResponse(content="should not run")

    def make_agent(endpoint):
        llm = CountingLiteLLM(endpoint) if backend == "litellm" else CountingLLM([LLMResponse(content="should not run")])
        if backend == "openai":
            llm.client = SimpleNamespace(base_url=endpoint)
        return Agent(llm, tools=[])

    key = Fernet.generate_key()
    session = open_session(tmp_path, key)
    agent = make_agent("https://original.invalid/v1")
    session.attach(agent)
    if mutation == "restore":
        session.close()
        session = open_session(tmp_path, key, session.run_id)
        agent = make_agent("https://changed.invalid/v1")
        with pytest.raises(ValueError, match="metadata|signature"):
            session.attach(agent)
    else:
        if backend == "litellm":
            agent.llm.base_url = "https://changed.invalid/v1"
        else:
            agent.llm.client.base_url = "https://changed.invalid/v1"
        try:
            with pytest.raises(ValueError, match="metadata|signature"):
                session.chat(agent, "go")
        finally:
            session.close()
    assert agent.llm.requests == []


def test_backend_identity_is_part_of_reasoning_signature(tmp_path):
    from types import SimpleNamespace

    from corecoder.llm import LiteLLM

    key = Fernet.generate_key()
    session = open_session(tmp_path, key)
    agent = Agent(CountingLLM([]), tools=[])
    agent.llm.client = SimpleNamespace(base_url="")
    session.attach(agent)
    session.close()
    changed = Agent(LiteLLM("counting-model"), tools=[])
    session = open_session(tmp_path, key, session.run_id)
    with pytest.raises(ValueError, match="metadata|signature"):
        session.attach(changed)


@pytest.mark.parametrize("backend", ["openai", "litellm"])
def test_transport_credentials_are_excluded_and_rotation_preserves_identity(tmp_path, backend):
    import json
    from types import SimpleNamespace

    from corecoder.llm import LiteLLM

    endpoint = "https://synthetic-user:synthetic-password@model.invalid/v1"
    extra = {"temperature": 0.2, "extra_headers": {"Authorization": "Bearer synthetic-key", "X-Route": "region-a"}}
    if backend == "litellm":
        llm = LiteLLM("counting-model", api_key="synthetic-provider-key", base_url=endpoint, **extra)
    else:
        llm = CountingLLM([])
        llm.client = SimpleNamespace(base_url=endpoint)
        llm.extra = extra
    agent = Agent(llm, tools=[])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        metadata = session.journal.load(session.run_id)["state"]["metadata"]
        serialized = json.dumps(metadata)
        assert all(value not in serialized for value in ["synthetic-user", "synthetic-password", "synthetic-key", "synthetic-provider-key"])
        assert metadata["model_options"]["temperature"] == 0.2
        assert metadata["model_options"]["extra_headers"] == {"X-Route": "region-a"}
        assert metadata["base_url"] == "https://model.invalid/v1"
        llm.extra["extra_headers"]["Authorization"] = "Bearer rotated-synthetic-key"
        rotated = "https://other-user:rotated-password@model.invalid/v1"
        if backend == "litellm":
            llm.api_key, llm.base_url = "rotated-provider-key", rotated
        else:
            llm.client.base_url = rotated
        assert session.ready(agent)
        assert all(value not in json.dumps(session.export()) for value in ["synthetic-key", "synthetic-password", "rotated-password"])
        llm.extra["temperature"] = 0.7
        with pytest.raises(ValueError, match="metadata|signature"):
            session.ready(agent)
    finally:
        session.close()


def test_supported_body_credentials_are_omitted_but_inference_options_remain(tmp_path):
    import json

    from corecoder.llm import LiteLLM

    llm = LiteLLM("counting-model", api_key="synthetic-provider-key", base_url="https://model.invalid/v1",
                  azure_ad_token="synthetic-ad-token", aws_secret_access_key="synthetic-cloud-key",
                  extra_body={"azure_ad_token": "synthetic-body-token", "thinking": {"type": "enabled"}})
    llm.chat = lambda **kwargs: LLMResponse(content="done")
    agent = Agent(llm, tools=[])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        session.chat(agent, "go")
        state = session.journal.load(session.run_id)["state"]
        metadata = state["metadata"]
        assert metadata["model_options"] == {"extra_body": {"thinking": {"type": "enabled"}}}
        with session.store._connect() as connection:
            turn = connection.execute("SELECT * FROM agent_turns WHERE run_id = ?", (session.run_id,)).fetchone()
        request = session.journal.cipher.open(session.journal._turn_identity(turn, "request"), turn["request_ciphertext"])
        assert all(secret not in json.dumps({"state": state, "request": request, "export": session.export()})
                   for secret in ["synthetic-provider-key", "synthetic-ad-token", "synthetic-cloud-key", "synthetic-body-token"])
    finally:
        session.close()


def test_cached_earlier_post_operation_gates_later_queued_tools(tmp_path):
    key, tool = Fernet.generate_key(), EffectTool()
    model = CountingLLM([LLMResponse(tool_calls=[ToolCall("first", tool.name, {"value": "first"}),
                                                ToolCall("second", tool.name, {"value": "second"})])])
    agent = Agent(model, tools=[tool])
    session = open_session(tmp_path, key)
    session.attach(agent)
    session.after_result = lambda *args: (_ for _ in ()).throw(ConnectionError("post operation needs recovery"))
    with pytest.raises(ConnectionError):
        session.chat(agent, "go")
    session.close()
    session = open_session(tmp_path, key, session.run_id)
    restored = Agent(CountingLLM([LLMResponse(content="done")]), tools=[tool])
    session.attach(restored)
    session.before_inference = lambda agent: (_ for _ in ()).throw(ValueError("pending linked post operation"))
    try:
        with pytest.raises(ValueError, match="pending linked"):
            session.resume(restored)
        assert tool.effects == ["first"]
        assert restored.llm.requests == []
        assert session.journal.list_invocations(session.run_id)[1]["state"] == "queued"
    finally:
        session.close()


def test_supported_provider_credential_headers_are_removed_case_insensitively(tmp_path):
    from types import SimpleNamespace

    llm = CountingLLM([])
    llm.client = SimpleNamespace(base_url="https://model.invalid/v1")
    llm.extra = {"extra_headers": {"X-Goog-Api-Key": "synthetic-google-credential", "X-Api-Key": "synthetic-header-credential",
                                    "Proxy-Authorization": "synthetic-proxy-credential", "X-Route": "region-a"}}
    agent = Agent(llm, tools=[])
    session = open_session(tmp_path, Fernet.generate_key())
    session.attach(agent)
    try:
        metadata = session.journal.load(session.run_id)["state"]["metadata"]
        assert metadata["model_options"]["extra_headers"] == {"X-Route": "region-a"}
    finally:
        session.close()
