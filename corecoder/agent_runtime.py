"""One durable Runtime session for the production CoreCoder CLI Agent."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from .permissions import Permission
from .runtime import (
    ApprovalDecision,
    ApprovalRecord,
    RunRecord,
    RunStatus,
    RuntimeExecutor,
    RuntimeToolAdapter,
    SQLiteStore,
    ToolPolicyRegistry,
)
from .tools.base import Tool


@dataclass
class AgentRuntimeSession:
    store: SQLiteStore
    executor: RuntimeExecutor
    run_id: str
    permission: Permission
    # A Run admits one active ToolCall; finish each write before starting the next.
    _write_gate: AbstractContextManager = field(default_factory=Lock, init=False, repr=False)

    @classmethod
    def open(cls, workspace: Path, model: str, permission: Permission) -> AgentRuntimeSession:
        workspace = workspace.resolve()
        runtime_dir = workspace / ".reliagent"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        store = SQLiteStore(runtime_dir / "agent-runtime.sqlite")
        store.initialize()
        run = store.create_run(
            goal="CoreCoder agent session",
            workflow="corecoder_agent",
            workspace=workspace,
            model=model,
            prompt_version="corecoder-agent-v1",
        )
        store.transition_run(run.id, RunStatus.RUNNING, "run.started")
        executor = RuntimeExecutor(store, ToolPolicyRegistry.with_builtin_defaults())
        return cls(store, executor, run.id, permission)

    def wrap_tools(self, tools: Sequence[Tool]) -> list[Tool]:
        return [
            RuntimeToolAdapter(
                tool, self.store, self.executor, self.run_id,
                approval_handler=self._decide_approval,
                execution_gate=self._write_gate if tool.name == "write_file" else None,
            )
            if tool.name in {"read_file", "write_file"} else tool
            for tool in tools
        ]

    def _decide_approval(self, approval: ApprovalRecord, arguments: dict) -> ApprovalDecision:
        refusal = self.permission.check(approval.tool_name, arguments)
        return ApprovalDecision.ALLOW_ONCE if refusal is None else ApprovalDecision.DENY

    def finish(self, status: RunStatus) -> RunRecord:
        run = self.store.get_run(self.run_id)
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        if status is RunStatus.CANCELLED:
            return self.executor.cancel_run(self.run_id)
        event = {RunStatus.SUCCEEDED: "run.completed", RunStatus.FAILED: "run.failed"}[status]
        return self.store.transition_run(self.run_id, status, event)
