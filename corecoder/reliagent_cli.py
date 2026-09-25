"""Command-line entry point for fixed, persisted ReliAgent tasks."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from .evals.report import write_markdown, write_raw_result
from .evals.runner import EvaluationRunner
from .evals.suites.phase3 import phase3_configurations, phase3_suite
from .reliagent import ReliAgentRuntime, TaskStep
from .runtime import (
    ApprovalDecision,
    ExecutionKind,
    FailureKind,
    RecoveryKind,
    RecoveryManager,
    RecoveryResolution,
    RiskLevel,
    RunStatus,
    RuntimeExecutor,
    SQLiteStore,
    StepStatus,
    ToolCallStatus,
    ToolPolicy,
    ToolPolicyRegistry,
    TraceService,
)
from .workflows.ml_experiment import MLExperimentWorkflow
from .workflows.ml_experiment.reconcile import (
    MLReconciliationService,
    ReconciliationDecision,
)
from .workflows.ml_experiment.report import MLExperimentReportService


def _add_storage_arguments(parser: argparse.ArgumentParser) -> None:
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--database", type=Path)
    storage.add_argument("--workspace", type=Path)


def _task_from_file(path: Path) -> tuple[str, Path, tuple[TaskStep, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("goal"), str):
        raise TypeError("task must contain a string goal")
    if not isinstance(value.get("workspace"), str):
        raise TypeError("task must contain a string workspace")
    raw_steps = value.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("task must contain a nonempty steps list")
    steps = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict) or raw_step.get("tool_name") not in {
            "local_read_only",
            "local_mutating",
        }:
            raise ValueError(
                "each task step must use tool_name local_read_only or local_mutating"
            )
        argv = raw_step.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("each task step must contain a nonempty argv string list")
        steps.append(TaskStep(raw_step["tool_name"], tuple(argv)))
    return value["goal"], Path(value["workspace"]), tuple(steps)


def _components(database: Path):
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        store = SQLiteStore(database)
        store.initialize()
    else:
        initializing = database.with_name(f".{database.name}.{uuid.uuid4().hex}.initializing")
        store = SQLiteStore(initializing)
        store.initialize()
        os.replace(initializing, database)
        store = SQLiteStore(database)
    read_only = ToolPolicy(
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=30,
        max_attempts=2,
        idempotent=True,
        auto_retry=True,
        retryable_failures=frozenset({
            FailureKind.NONZERO_EXIT,
            FailureKind.SPAWN_ERROR,
            FailureKind.TIMED_OUT,
        }),
        output_limit=15_000,
    )
    mutating = ToolPolicy(
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=30,
        max_attempts=1,
        idempotent=False,
        auto_retry=False,
        retryable_failures=frozenset(),
        output_limit=15_000,
    )
    policies = ToolPolicyRegistry({
        "local_read_only": read_only,
        "local_mutating": mutating,
    })
    executor = RuntimeExecutor(store, policies)
    return store, executor, ReliAgentRuntime(store, executor), RecoveryManager(store, policies)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reliagent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume"):
        command = subparsers.add_parser(name)
        if name == "resume":
            command.add_argument("run_id")
        command.add_argument("task_file", type=Path)
        command.add_argument("--database", type=Path, required=True)
    workflow_command = subparsers.add_parser("workflow")
    workflow_names = workflow_command.add_subparsers(dest="workflow_name", required=True)
    ml = workflow_names.add_parser("ml")
    ml_actions = ml.add_subparsers(dest="workflow_action", required=True)
    ml_start = ml_actions.add_parser("start")
    ml_start.add_argument("--workspace", type=Path, required=True)
    ml_start.add_argument("--inject-process-loss", choices=(
        "environment_after_start", "experiment_after_effect"
    ))
    for action in ("resume", "report"):
        action_parser = ml_actions.add_parser(action)
        action_parser.add_argument("run_id")
        action_parser.add_argument("--workspace", type=Path, required=True)
    listing = subparsers.add_parser("list")
    _add_storage_arguments(listing)
    for name in ("approve", "deny"):
        approval = subparsers.add_parser(name)
        approval.add_argument("approval_id")
        _add_storage_arguments(approval)
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("tool_call_id")
    reconcile.add_argument(
        "resolution", nargs="?",
        choices=tuple(resolution.value for resolution in RecoveryResolution),
    )
    reconcile.add_argument(
        "--decision", choices=tuple(item.value for item in ReconciliationDecision)
    )
    _add_storage_arguments(reconcile)
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("run_id")
    _add_storage_arguments(cancel)
    trace = subparsers.add_parser("trace")
    trace.add_argument("run_id")
    _add_storage_arguments(trace)
    trace.add_argument("--format", choices=("json",), default="json")
    evaluation = subparsers.add_parser("eval")
    evaluation.add_argument("suite", choices=("phase3",))
    evaluation.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "workflow":
        workflow = MLExperimentWorkflow(args.workspace)
        if args.workflow_action == "start":
            def announce(run):
                print(
                    json.dumps({"event": "run_created", "run_id": run.id}, sort_keys=True),
                    flush=True,
                )

            run = workflow.create(args.inject_process_loss, on_created=announce)
            print(json.dumps(asdict(workflow.status(run.id)), sort_keys=True))
            return 0
        if args.workflow_action == "resume":
            print(json.dumps(asdict(workflow.resume(args.run_id)), sort_keys=True))
            return 0
        report = MLExperimentReportService(workflow)
        raw_path, markdown_path = report.write(report.build(args.run_id))
        print(json.dumps({
            "markdown": str(markdown_path),
            "raw_json": str(raw_path),
            "run_id": args.run_id,
            "status": "succeeded",
        }, sort_keys=True))
        return 0
    database = (
        args.workspace / ".reliagent" / "runtime.sqlite"
        if getattr(args, "workspace", None) is not None
        else getattr(args, "database", None)
    )
    if args.command == "trace":
        store = SQLiteStore(database)
        store.initialize()
        print(json.dumps(TraceService(store).export_run(args.run_id), sort_keys=True))
        return 0
    if args.command == "eval":
        with TemporaryDirectory() as directory:
            results = EvaluationRunner(
                Path(directory), phase3_configurations()
            ).run(phase3_suite())
        raw_path = write_raw_result(results, args.output)
        markdown_path = write_markdown(raw_path, args.output)
        passed = sum(result.passed is True for result in results)
        print(json.dumps({
            "contract_passed_repetitions": passed,
            "markdown": str(markdown_path),
            "raw_json": str(raw_path),
            "total_repetitions": len(results),
        }, sort_keys=True))
        return 0
    if getattr(args, "workspace", None) is not None:
        ml_workflow = MLExperimentWorkflow(args.workspace)
        store = ml_workflow.store
        _executor = ml_workflow.executor
        recovery = ml_workflow.recovery
        runtime = ml_workflow.runtime
    else:
        ml_workflow = None
        store, _executor, runtime, recovery = _components(database)
    if args.command == "list":
        candidates = recovery.scan()
        approvals = [
            approval
            for run in store.list_runs()
            for approval in store.list_approvals(run.id)
        ]
        print(json.dumps({
            "approvals": [
                {
                    "approval_id": approval.id,
                    "run_id": store.get_tool_call(approval.tool_call_id).run_id,
                    "status": approval.status.value,
                    "tool_call_id": approval.tool_call_id,
                }
                for approval in approvals
            ],
            "recovery": [
                {
                    "kind": candidate.kind.value,
                    "reason": candidate.reason,
                    "run_id": candidate.run.id,
                    "tool_call_id": candidate.call.id,
                }
                for candidate in candidates
            ],
            "runs": [
                {"run_id": run.id, "status": run.status.value}
                for run in store.list_runs()
            ],
        }, sort_keys=True))
        return 0
    if args.command in {"approve", "deny"}:
        decision = (
            ApprovalDecision.ALLOW_ONCE
            if args.command == "approve"
            else ApprovalDecision.DENY
        )
        approval = store.resolve_approval(args.approval_id, decision)
        print(json.dumps({
            "approval_id": approval.id,
            "decision": approval.decision.value,
            "status": approval.status.value,
        }, sort_keys=True))
        return 0
    if args.command == "reconcile":
        if ml_workflow is not None:
            if args.decision is None:
                parser.error("--decision is required with --workspace")
            result = MLReconciliationService(ml_workflow).reconcile(
                args.tool_call_id, ReconciliationDecision(args.decision)
            )
            print(json.dumps(asdict(result), sort_keys=True, default=lambda item: item.value))
            return 0
        if args.resolution is None:
            parser.error("resolution is required with --database")
        call = recovery.reconcile(
            args.tool_call_id,
            RecoveryResolution(args.resolution),
        )
        print(json.dumps({
            "run_id": call.run_id,
            "status": call.status.value,
            "tool_call_id": call.id,
        }, sort_keys=True))
        return 0
    if args.command == "cancel":
        run = _executor.cancel_run(args.run_id)
        print(json.dumps({"run_id": run.id, "status": run.status.value}, sort_keys=True))
        return 0
    goal, workspace, steps = _task_from_file(args.task_file)
    if args.command == "run":
        run = runtime.run_task(goal=goal, workspace=workspace, steps=steps)
    else:
        candidates = [
            candidate
            for candidate in recovery.scan()
            if candidate.run.id == args.run_id
        ]
        blockers = [
            candidate
            for candidate in candidates
            if candidate.kind in {
                RecoveryKind.APPROVAL_PENDING,
                RecoveryKind.HUMAN_REQUIRED,
            }
        ]
        if blockers:
            run = store.get_run(args.run_id)
            print(json.dumps({
                "recovery": [
                    {
                        "kind": candidate.kind.value,
                        "reason": candidate.reason,
                        "tool_call_id": candidate.call.id,
                    }
                    for candidate in blockers
                ],
                "run_id": run.id,
                "status": run.status.value,
            }, sort_keys=True))
            return 0
        for candidate in candidates:
            if candidate.kind is RecoveryKind.RETRY_ALLOWED:
                retry = recovery.resume_retry(candidate)
                result = _executor.execute_recovery_subprocess(retry.id)
                step_status = (
                    StepStatus.SUCCEEDED
                    if result.call.status is ToolCallStatus.SUCCEEDED
                    else StepStatus.CANCELLED
                    if result.call.status is ToolCallStatus.CANCELLED
                    else StepStatus.FAILED
                )
                store.transition_step(
                    result.call.step_id,
                    step_status,
                    f"step.{step_status.value}",
                )
                if step_status is not StepStatus.SUCCEEDED:
                    run_status = (
                        RunStatus.CANCELLED
                        if step_status is StepStatus.CANCELLED
                        else RunStatus.FAILED
                    )
                    store.transition_run(
                        args.run_id,
                        run_status,
                        f"run.{run_status.value}",
                    )
        run = runtime.resume(args.run_id, steps=steps).run
    print(json.dumps({"run_id": run.id, "status": run.status.value}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
