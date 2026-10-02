"""Consent gating for mutating tools: the Permission layer and its wiring."""

from pathlib import Path

import pytest

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.agent import AgentTool
from corecoder.tools.write import WriteFileTool
from tests.conftest import get_tool


def _write_call(call_id, path):
    return ToolCall(id=call_id, name="write_file",
                    arguments={"file_path": str(path), "content": "x\n"})


def _two_writes_then_text(tmp_path):
    return [
        LLMResponse(tool_calls=[_write_call("c1", tmp_path / "a.txt")]),
        LLMResponse(tool_calls=[_write_call("c2", tmp_path / "b.txt")]),
        LLMResponse(content="done"),
    ]


def test_read_only_tools_run_without_consent(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello", encoding="utf-8")
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="c1", name="read_file", arguments={"file_path": str(f)})]),
            LLMResponse(content="read it"),
        ]),
        tools=[get_tool("read_file")],
        permission=Permission(),  # nobody to ask, and it doesn't matter
    )

    assert agent.chat("go") == "read it"
    assert "hello" in agent.messages[2]["content"]


def test_allow_once_asks_again_for_the_next_call(tmp_path):
    asked = []
    agent = Agent(
        llm=ScriptedLLM(_two_writes_then_text(tmp_path)),
        tools=[WriteFileTool()],
        permission=Permission(ask=lambda name, args: asked.append(name) or "once"),
    )

    assert agent.chat("go") == "done"
    assert asked == ["write_file", "write_file"]  # every call asks
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_always_allow_is_remembered_for_the_session(tmp_path):
    asked = []
    agent = Agent(
        llm=ScriptedLLM(_two_writes_then_text(tmp_path)),
        tools=[WriteFileTool()],
        permission=Permission(ask=lambda name, args: asked.append(name) or "always"),
    )

    assert agent.chat("go") == "done"
    assert asked == ["write_file"]  # the second call went straight through
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_deny_skips_execution_and_reports_back(tmp_path):
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[_write_call("c1", tmp_path / "a.txt")]),
            LLMResponse(content="understood"),
        ]),
        tools=[WriteFileTool()],
        permission=Permission(ask=lambda name, args: "deny"),
    )

    assert agent.chat("go") == "understood"  # the loop survived the refusal
    assert not (tmp_path / "a.txt").exists()
    refusal = agent.messages[2]
    assert refusal["role"] == "tool" and refusal["tool_call_id"] == "c1"
    assert "Permission denied" in refusal["content"]


def test_no_callback_means_auto_deny(tmp_path):
    # one-shot -p mode wires Permission() with no ask: refuse, never hang
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[_write_call("c1", tmp_path / "a.txt")]),
            LLMResponse(content="ok"),
        ]),
        tools=[WriteFileTool()],
        permission=Permission(),
    )

    assert agent.chat("go") == "ok"
    assert not (tmp_path / "a.txt").exists()
    assert "non-interactive" in agent.messages[2]["content"]


def test_allow_all_approves_without_any_callback(tmp_path):
    # what --yes wires in
    agent = Agent(
        llm=ScriptedLLM(_two_writes_then_text(tmp_path)),
        tools=[WriteFileTool()],
        permission=Permission(allow_all=True),
    )

    assert agent.chat("go") == "done"
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()


def test_parallel_calls_each_get_their_own_decision(tmp_path):
    marker = tmp_path / "touched"
    calls = [
        ToolCall(id="c1", name="bash", arguments={"command": f"touch {marker}"}),
        _write_call("c2", tmp_path / "ok.txt"),
    ]
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(tool_calls=calls), LLMResponse(content="done")]),
        tools=[get_tool("bash"), WriteFileTool()],
        permission=Permission(ask=lambda name, args: "deny" if name == "bash" else "once"),
    )

    assert agent.chat("go") == "done"
    assert not marker.exists()              # the denied bash never ran
    assert (tmp_path / "ok.txt").exists()   # the allowed write did
    results = {m["tool_call_id"]: m["content"] for m in agent.messages if m.get("role") == "tool"}
    assert "Permission denied" in results["c1"]
    assert results["c2"].startswith("Wrote")


def test_sub_agent_inherits_the_permission_layer(tmp_path):
    seen = []

    def ask(name, args):
        seen.append(name)
        return "once" if name == "agent" else "deny"

    target = tmp_path / "sub.txt"
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="c1", name="agent", arguments={"task": "write the file"})]),
            LLMResponse(tool_calls=[_write_call("c2", target)]),  # the sub-agent's move
            LLMResponse(content="could not write"),               # the sub-agent's reply
            LLMResponse(content="parent done"),
        ]),
        tools=[AgentTool(), WriteFileTool()],
        permission=Permission(ask=ask),
    )

    assert agent.chat("go") == "parent done"
    assert seen == ["agent", "write_file"]  # consent followed into the sub-agent
    assert not target.exists()


# --- the CLI side of the layer ---

def test_yes_flag_parses(monkeypatch):
    from corecoder.cli import _parse_args
    monkeypatch.setattr("sys.argv", ["corecoder", "--yes"])
    assert _parse_args().yes


def test_runtime_flags_default_to_enabled(monkeypatch):
    from corecoder.cli import _parse_args
    monkeypatch.setattr("sys.argv", ["corecoder"])

    args = _parse_args()

    assert args.no_runtime is False
    assert args.runtime_workspace is None


def test_runtime_flags_parse_workspace_and_opt_out(monkeypatch, tmp_path):
    from corecoder.cli import _parse_args
    monkeypatch.setattr("sys.argv", ["corecoder", "--runtime-workspace", str(tmp_path), "--no-runtime"])

    args = _parse_args()

    assert args.no_runtime is True
    assert args.runtime_workspace == Path(tmp_path)


@pytest.mark.parametrize("answer,ask_count,status,decision,written", [
    ("once", 2, "approved", "allow_once", True),
    ("always", 1, "approved", "allow_once", True),
    ("deny", 2, "denied", "deny", False),
    (None, 0, "denied", "deny", False),
])
def test_runtime_builder_maps_permission_to_durable_approval(
    tmp_path, monkeypatch, answer, ask_count, status, decision, written,
):
    from corecoder import cli
    from corecoder.config import Config
    from corecoder.runtime import RunStatus

    monkeypatch.setattr(cli, "load_mcp_tools", list)
    monkeypatch.setattr(cli, "load_hooks", lambda: None)
    asked = []
    permission = Permission(
        ask=(lambda name, args: asked.append((name, args)) or answer) if answer is not None else None,
    )
    agent, session = cli._build_agent(
        ScriptedLLM(_two_writes_then_text(tmp_path)), Config(), permission,
        runtime_workspace=tmp_path, runtime_enabled=True,
    )

    assert agent.chat("go") == "done"
    session.finish(RunStatus.SUCCEEDED)

    assert len(asked) == ask_count
    if asked:
        assert asked[0] == ("write_file", {"file_path": str(tmp_path / "a.txt"), "content": "x\n"})
    assert (tmp_path / "a.txt").exists() is written
    assert (tmp_path / "b.txt").exists() is written
    approvals = session.store.list_approvals(session.run_id)
    assert [(a.status.value, a.decision.value) for a in approvals] == [(status, decision), (status, decision)]
    results = [message["content"] for message in agent.messages if message["role"] == "tool"]
    assert len(results) == 2
    assert all(result.startswith("Wrote" if written else "Approval denied") for result in results)


def test_ask_prompt_maps_answers(monkeypatch):
    from corecoder import cli
    answers = iter(["y", "a", "n", "garbage"])
    monkeypatch.setattr(cli, "pt_prompt", lambda *a, **k: next(answers))

    assert cli._ask_permission("bash", {"command": "ls"}) == "once"
    assert cli._ask_permission("bash", {}) == "always"
    assert cli._ask_permission("bash", {}) == "deny"
    assert cli._ask_permission("bash", {}) == "deny"  # junk input is a no


def test_ask_prompt_eof_denies(monkeypatch):
    from corecoder import cli

    def eof(*a, **k):
        raise EOFError

    monkeypatch.setattr(cli, "pt_prompt", eof)
    assert cli._ask_permission("bash", {}) == "deny"
