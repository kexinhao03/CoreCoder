"""Opt-in live-model proof that an LLM-selected tool enters RuntimeExecutor."""

import os

import pytest

from corecoder import Agent
from corecoder.config import Config
from corecoder.llm import LLM
from corecoder.runtime import (
    RuntimeExecutor,
    RuntimeToolAdapter,
    SQLiteStore,
    ToolCallStatus,
    ToolPolicyRegistry,
)
from corecoder.runtime.state import RunStatus
from corecoder.tools.read import ReadFileTool

pytestmark = pytest.mark.skipif(
    os.getenv("CORECODER_RUN_LIVE_TESTS") != "1",
    reason="set CORECODER_RUN_LIVE_TESTS=1 with model credentials",
)


def test_live_llm_selects_runtime_backed_read_tool(tmp_path):
    config = Config.from_env()
    if not config.api_key:
        pytest.skip("live model credentials are not configured")
    source = tmp_path / "live-proof.txt"
    source.write_text("RELIAGENT_LIVE_RUNTIME_PROOF\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    store.initialize()
    run = store.create_run(
        goal="live LLM runtime tool call",
        workflow="corecoder_agent_live",
        workspace=tmp_path,
        model=config.model,
        prompt_version="runtime-adapter-live-v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    executor = RuntimeExecutor(store, ToolPolicyRegistry.with_builtin_defaults())
    tool = RuntimeToolAdapter(ReadFileTool(), store, executor, run.id)
    llm = LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=0,
        max_tokens=1024,
    )
    agent = Agent(llm=llm, tools=[tool], max_rounds=3)

    agent.chat(
        "Call read_file exactly once for this path, then report its marker: "
        f"{source}"
    )

    calls = store.list_tool_calls(run.id)
    assert len(calls) == 1, agent.messages
    assert calls[0].tool_name == "read_file"
    assert calls[0].status is ToolCallStatus.SUCCEEDED
    assert calls[0].step_id == store.list_steps(run.id)[0].id
