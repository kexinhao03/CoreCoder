"""Adapter that routes existing CoreCoder tools through RuntimeExecutor."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import ClassVar

from corecoder.tools.base import Tool

from .approvals import ApprovalDecision, ApprovalRecord
from .executor import PendingApproval, RuntimeExecutor, RuntimeResult
from .state import StepStatus, ToolCallStatus
from .store import SQLiteStore

ApprovalHandler = Callable[[ApprovalRecord, dict], ApprovalDecision]


class RuntimeToolAdapter(Tool):
    """Preserve a CoreCoder tool's schema while durably executing each call."""

    manages_approval: ClassVar[bool] = True
    parameters: ClassVar[dict]

    def __init__(
        self,
        tool: Tool,
        store: SQLiteStore,
        executor: RuntimeExecutor,
        run_id: str,
        *,
        approval_handler: ApprovalHandler | None = None,
        execution_gate: AbstractContextManager | None = None,
    ) -> None:
        self._tool = tool
        self._store = store
        self._executor = executor
        self._run_id = run_id
        self._approval_handler = approval_handler
        self._execution_gate = execution_gate if execution_gate is not None else nullcontext()
        self.name = tool.name
        self.description = tool.description
        self.parameters = tool.parameters

    def execute(self, **kwargs) -> str:
        with self._execution_gate:
            return self._execute(**kwargs)

    def _execute(self, **kwargs) -> str:
        step = self._store.create_step(
            self._run_id,
            sequence=None,
            title=self.name,
        )
        self._store.transition_step(step.id, StepStatus.RUNNING, "step.started")
        try:
            result = self._executor.submit_in_process(
                self._run_id,
                self.name,
                kwargs,
                self._execute_tool,
                step_id=step.id,
            )
        except BaseException:
            self._settle_interrupted_step(step.id)
            raise
        if isinstance(result, PendingApproval):
            self._store.transition_step(
                step.id,
                StepStatus.WAITING_APPROVAL,
                "step.waiting_approval",
            )
            if self._approval_handler is None:
                return f"Approval required: {result.approval.id}"
            decision = self._approval_handler(result.approval, kwargs)
            self._store.resolve_approval(result.approval.id, decision)
            if decision is ApprovalDecision.DENY:
                self._store.transition_step(
                    step.id,
                    StepStatus.CANCELLED,
                    "step.cancelled",
                )
                return f"Approval denied for {self.name}"
            self._store.transition_step(
                step.id,
                StepStatus.RUNNING,
                "step.resumed",
            )
            try:
                result = self._executor.execute_approved_in_process(
                    result.call.id,
                    self._execute_tool,
                )
            except BaseException:
                self._settle_interrupted_step(step.id)
                raise
        return self._finish_step(step.id, result)

    def _execute_tool(self, arguments: dict, _cancellation) -> str:
        return self._tool.execute(**arguments)

    def _settle_interrupted_step(self, step_id: str) -> None:
        calls = [
            call for call in self._store.list_tool_calls(run_id=self._run_id)
            if call.step_id == step_id
        ]
        if not calls:
            return
        if calls[-1].status is ToolCallStatus.CANCELLED:
            status = StepStatus.CANCELLED
        elif calls[-1].status is ToolCallStatus.INTERRUPTED:
            status = StepStatus.FAILED
        else:
            return
        self._store.transition_step(step_id, status, f"step.{status.value}")

    def _finish_step(self, step_id: str, result: RuntimeResult) -> str:
        if result.call.status is ToolCallStatus.SUCCEEDED:
            self._store.transition_step(
                step_id,
                StepStatus.SUCCEEDED,
                "step.completed",
            )
            return result.output
        status = (
            StepStatus.CANCELLED
            if result.call.status is ToolCallStatus.CANCELLED
            else StepStatus.FAILED
        )
        self._store.transition_step(step_id, status, f"step.{status.value}")
        detail = result.output or result.call.status.value
        return f"Error executing {self.name}: {detail}"
