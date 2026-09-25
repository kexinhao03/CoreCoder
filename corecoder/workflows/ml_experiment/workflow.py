"""Persisted orchestration for the deterministic ML experiment."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from corecoder.reliagent import ReliAgentRuntime, StepMetadata, TaskStep
from corecoder.runtime import (
    ManagedProcessRunner,
    RecoveryKind,
    RecoveryManager,
    RunRecord,
    RunStatus,
    RuntimeExecutor,
    RuntimeFaultInjector,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
)

from .models import (
    CONFIG_VERSION,
    MODEL,
    WORKFLOW_NAME,
    WorkflowDefinition,
    build_definition,
    resolve_workspace,
)
from .policies import build_policy_registry

_FAULTS = {
    "environment_after_start": ("environment_check", 86),
    "experiment_after_effect": ("run_experiment", 87),
}


@dataclass(frozen=True)
class WorkflowStatus:
    run_id: str
    status: str
    current_step: str | None
    pending_approval_id: str | None
    pending_reconciliation_tool_call_id: str | None
    next_action: str
    report_available: bool


class MLExperimentWorkflow:
    def __init__(
        self,
        workspace: Path,
        *,
        process_runner: ManagedProcessRunner | None = None,
        exit_process: Callable[[int], NoReturn] = os._exit,
    ) -> None:
        self.workspace = resolve_workspace(workspace)
        runtime_dir = self.workspace / ".reliagent"
        runtime_dir.mkdir(exist_ok=True)
        self.store = SQLiteStore(runtime_dir / "runtime.sqlite")
        self.store.initialize()
        self.policies = build_policy_registry()
        injector = RuntimeFaultInjector(self.store, exit_process=exit_process)
        self.executor = RuntimeExecutor(
            self.store,
            self.policies,
            process_runner,
            fault_injector=injector,
        )
        self.runtime = ReliAgentRuntime(self.store, self.executor)
        self.recovery = RecoveryManager(self.store, self.policies)

    def definition(self, run_id: str) -> WorkflowDefinition:
        return build_definition(self.workspace, run_id)

    def create(
        self,
        fault: str | None = None,
        on_created: Callable[[RunRecord], None] | None = None,
    ) -> RunRecord:
        if fault is not None and fault not in _FAULTS:
            raise ValueError(f"unknown ML workflow fault: {fault}")
        run_id = uuid.uuid4().hex
        definition = self.definition(run_id)
        tasks = self._tasks(definition)
        run = self.runtime.create_task(
            goal="Run deterministic local ML experiment",
            workspace=self.workspace,
            steps=tasks,
            workflow=WORKFLOW_NAME,
            model=MODEL,
            prompt_version=CONFIG_VERSION,
            step_metadata=self._metadata(definition),
            run_id=run_id,
        )
        if fault is not None:
            step_key, exit_code = _FAULTS[fault]
            self.store.create_fault_plan(
                run_id=run.id,
                fault_type="process_loss",
                target_step_key=step_key,
                checkpoint=fault,
                exit_code=exit_code,
            )
        if on_created is not None:
            on_created(self.store.get_run(run.id))
        self.runtime.resume(
            run.id, steps=tasks, step_metadata=self._metadata(definition)
        )
        return self.store.get_run(run.id)

    def status(self, run_id: str) -> WorkflowStatus:
        return self._status(run_id)

    def resume(self, run_id: str) -> WorkflowStatus:
        run = self.store.get_run(run_id)
        if run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return self._status(run_id)
        definition = self._validate_definition(run)
        candidates = self.recovery.scan(run_id=run_id)
        for candidate in candidates:
            if candidate.kind is RecoveryKind.HUMAN_REQUIRED:
                return self._status(
                    run_id, pending_reconciliation=candidate.call.id
                )
            if candidate.kind is RecoveryKind.RETRY_ALLOWED:
                retry = self.recovery.resume_retry(candidate)
                result = self.executor.execute_recovery_subprocess(retry.id)
                step = self._step_for_call(run_id, retry.step_id)
                if result.call.status is ToolCallStatus.SUCCEEDED:
                    self.store.transition_step(
                        step.id, StepStatus.SUCCEEDED, "step.completed"
                    )
                else:
                    self.store.transition_step(step.id, StepStatus.FAILED, "step.failed")
                    self.store.transition_run(run_id, RunStatus.FAILED, "run.failed")
                    return self._status(run_id)
        self.runtime.resume(
            run_id,
            steps=self._tasks(definition),
            step_metadata=self._metadata(definition),
        )
        return self._status(run_id)

    def _validate_definition(self, run: RunRecord) -> WorkflowDefinition:
        if run.workflow != WORKFLOW_NAME or Path(run.workspace) != self.workspace:
            raise ValueError("run does not belong to this ML workflow workspace")
        definition = self.definition(run.id)
        steps = self.store.list_steps(run.id)
        metadata = self._metadata(definition)
        actual = tuple(
            (
                step.sequence,
                step.step_key,
                step.title,
                step.definition_version,
                step.definition_hash,
            )
            for step in steps
        )
        expected = tuple(
            (
                sequence,
                item.step_key,
                item.title,
                item.definition_version,
                item.definition_hash,
            )
            for sequence, item in enumerate(metadata, start=1)
        )
        if actual != expected:
            raise ValueError("workflow Step identity does not match current definition")
        step_by_id = {step.id: step for step in steps}
        tasks = {item.step_key: task for item, task in zip(metadata, self._tasks(definition), strict=True)}
        for call in self.store.list_tool_calls(run.id):
            step = step_by_id.get(call.step_id)
            if step is None or call.arguments != {"argv": list(tasks[step.step_key].argv)}:
                raise ValueError("workflow ToolCall arguments do not match current definition")
        return definition

    @staticmethod
    def _metadata(definition: WorkflowDefinition) -> tuple[StepMetadata, ...]:
        return tuple(
            StepMetadata(
                step.step_key,
                step.title,
                step.definition_version,
                step.definition_hash,
            )
            for step in definition.steps
        )

    @staticmethod
    def _tasks(definition: WorkflowDefinition) -> tuple[TaskStep, ...]:
        return tuple(
            TaskStep(
                step.tool_name,
                (sys.executable, str(step.script_path), *definition.arguments_for(step.step_key)),
            )
            for step in definition.steps
        )

    def _step_for_call(self, run_id: str, step_id: str | None):
        for step in self.store.list_steps(run_id):
            if step.id == step_id:
                return step
        raise ValueError("recovery ToolCall has no persisted Step")

    def _status(
        self, run_id: str, pending_reconciliation: str | None = None
    ) -> WorkflowStatus:
        run = self.store.get_run(run_id)
        steps = self.store.list_steps(run_id)
        current = next(
            (step.step_key for step in steps if step.status is not StepStatus.SUCCEEDED),
            None,
        )
        pending_approval = None
        for approval in self.store.list_approvals(run_id):
            if approval.status.value == "pending":
                pending_approval = approval.id
                break
        if pending_reconciliation is not None or run.status is RunStatus.RECOVERABLE:
            next_action = "reconcile"
        elif run.status is RunStatus.WAITING_APPROVAL:
            next_action = "approve_or_deny"
        elif run.status is RunStatus.SUCCEEDED:
            next_action = "report"
        elif run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
            next_action = "none"
        else:
            next_action = "resume"
        return WorkflowStatus(
            run_id=run.id,
            status=run.status.value,
            current_step=current,
            pending_approval_id=pending_approval,
            pending_reconciliation_tool_call_id=pending_reconciliation,
            next_action=next_action,
            report_available=run.status is RunStatus.SUCCEEDED,
        )
