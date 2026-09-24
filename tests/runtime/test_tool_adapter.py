"""CoreCoder Agent tool calls can execute through the durable Runtime."""

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.runtime import (
    ApprovalDecision,
    ApprovalStatus,
    RuntimeExecutor,
    RuntimeToolAdapter,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
    ToolPolicyRegistry,
)
from corecoder.runtime.state import RunStatus
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool


def test_agent_read_and_approved_write_calls_flow_through_runtime(tmp_path):
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("runtime evidence\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    store.initialize()
    run = store.create_run(
        goal="read then write",
        workflow="corecoder_agent",
        workspace=tmp_path,
        model="scripted-demo",
        prompt_version="runtime-adapter-v1",
    )
    store.transition_run(run.id, RunStatus.RUNNING, "run.started")
    executor = RuntimeExecutor(store, ToolPolicyRegistry.with_builtin_defaults())
    approval_requests = []

    def approve_once(approval):
        approval_requests.append(approval)
        return ApprovalDecision.ALLOW_ONCE

    tools = [
        RuntimeToolAdapter(ReadFileTool(), store, executor, run.id),
        RuntimeToolAdapter(
            WriteFileTool(),
            store,
            executor,
            run.id,
            approval_handler=approve_once,
        ),
    ]
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall(
            id="read-1",
            name="read_file",
            arguments={"file_path": str(source)},
        )]),
        LLMResponse(tool_calls=[ToolCall(
            id="write-1",
            name="write_file",
            arguments={"file_path": str(target), "content": "written through runtime\n"},
        )]),
        LLMResponse(content="Both runtime-backed tools completed."),
    ])

    agent = Agent(llm=llm, tools=tools)

    result = agent.chat("Read the source and write the target.")

    assert result == "Both runtime-backed tools completed."
    assert target.read_text(encoding="utf-8") == "written through runtime\n"
    tool_messages = [message for message in agent.messages if message["role"] == "tool"]
    assert "runtime evidence" in tool_messages[0]["content"]
    assert tool_messages[1]["content"] == f"Wrote 1 lines to {target}"

    steps = store.list_steps(run.id)
    calls = store.list_tool_calls(run.id)
    approvals = store.list_approvals(run.id)
    assert [step.status for step in steps] == [StepStatus.SUCCEEDED, StepStatus.SUCCEEDED]
    assert [step.attempt_count for step in steps] == [1, 1]
    assert [call.status for call in calls] == [ToolCallStatus.SUCCEEDED, ToolCallStatus.SUCCEEDED]
    assert [call.step_id for call in calls] == [step.id for step in steps]
    assert len(approval_requests) == 1
    assert len(approvals) == 1
    assert approvals[0].status is ApprovalStatus.APPROVED
    assert approvals[0].decision is ApprovalDecision.ALLOW_ONCE
    event_types = [event.type for event in store.list_events(run.id)]
    assert "tool.completed" in event_types
    assert "approval.requested" in event_types
    assert "approval.resolved" in event_types
