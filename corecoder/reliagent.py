"""Minimal persisted-step adapter over the ReliAgent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .runtime import (
    PendingApproval,
    RunRecord,
    RunStatus,
    RuntimeExecutor,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
)


@dataclass(frozen=True)
class TaskStep:
    tool_name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class AdapterResult:
    run: RunRecord
    executed_step_ids: tuple[str, ...]


class ReliAgentRuntime:
    """Execute a fixed ordered task while deriving progress from SQLite."""

    def __init__(self, store: SQLiteStore, executor: RuntimeExecutor) -> None:
        self._store = store
        self._executor = executor

    def run_task(self, *, goal: str, workspace: Path, steps: tuple[TaskStep, ...]) -> RunRecord:
        run = self._store.create_run(
            goal=goal, workflow="fixed_task", workspace=workspace,
            model="deterministic", prompt_version="phase3",
        )
        self._store.transition_run(run.id, RunStatus.RUNNING, "run.started")
        for sequence, task_step in enumerate(steps, start=1):
            self._store.create_step(run.id, sequence=sequence, title=task_step.tool_name)
        return self._execute_pending(run.id, steps).run

    def resume(self, run_id: str, *, steps: tuple[TaskStep, ...]) -> AdapterResult:
        return self._execute_pending(run_id, steps)

    def _execute_pending(self, run_id: str, task_steps: tuple[TaskStep, ...]) -> AdapterResult:
        run = self._store.get_run(run_id)
        persisted_steps = self._store.list_steps(run_id)
        if len(persisted_steps) != len(task_steps):
            raise ValueError("task step count does not match persisted steps")
        if run.status is RunStatus.CREATED:
            run = self._store.transition_run(run_id, RunStatus.RUNNING, "run.started")
        if run.status is not RunStatus.RUNNING:
            return AdapterResult(run, ())
        executed = []
        for step, task_step in zip(persisted_steps, task_steps, strict=True):
            if step.status is StepStatus.SUCCEEDED:
                continue
            if step.status is StepStatus.WAITING_APPROVAL:
                call = self._step_call(run_id, step.id)
                if call.status is ToolCallStatus.CANCELLED:
                    self._store.transition_step(step.id, StepStatus.CANCELLED, "step.cancelled")
                    return AdapterResult(
                        self._store.transition_run(run_id, RunStatus.CANCELLED, "run.cancelled"),
                        tuple(executed),
                    )
                self._store.transition_step(step.id, StepStatus.RUNNING, "step.resumed")
                result = self._executor.execute_approved_subprocess(call.id)
            else:
                self._store.transition_step(step.id, StepStatus.RUNNING, "step.started")
                result = self._executor.submit_subprocess(
                    run_id, task_step.tool_name, task_step.argv, step_id=step.id
                )
            if isinstance(result, PendingApproval):
                self._store.transition_step(
                    step.id, StepStatus.WAITING_APPROVAL, "step.waiting_approval"
                )
                return AdapterResult(self._store.get_run(run_id), tuple(executed))
            if result.call.status is not ToolCallStatus.SUCCEEDED:
                status = (
                    StepStatus.CANCELLED
                    if result.call.status is ToolCallStatus.CANCELLED
                    else StepStatus.FAILED
                )
                self._store.transition_step(
                    step.id, status, f"step.{status.value}"
                )
                run_status = (
                    RunStatus.CANCELLED if status is StepStatus.CANCELLED else RunStatus.FAILED
                )
                return AdapterResult(
                    self._store.transition_run(run_id, run_status, f"run.{run_status.value}"),
                    tuple(executed),
                )
            self._store.transition_step(step.id, StepStatus.SUCCEEDED, "step.completed")
            executed.append(step.id)
        return AdapterResult(self._store.transition_run(run_id, RunStatus.SUCCEEDED, "run.completed"), tuple(executed))

    def _step_call(self, run_id: str, step_id: str):
        calls = [
            call for call in self._store.list_tool_calls(run_id) if call.step_id == step_id
        ]
        if len(calls) != 1:
            raise ValueError("waiting step must have exactly one persisted tool call")
        return calls[0]
