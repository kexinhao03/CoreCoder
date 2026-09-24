"""Command-line entry point for fixed, persisted ReliAgent tasks."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

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
    store = SQLiteStore(database)
    store.initialize()
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
    listing = subparsers.add_parser("list")
    listing.add_argument("--database", type=Path, required=True)
    for name in ("approve", "deny"):
        approval = subparsers.add_parser(name)
        approval.add_argument("approval_id")
        approval.add_argument("--database", type=Path, required=True)
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("tool_call_id")
    reconcile.add_argument(
        "resolution",
        choices=tuple(resolution.value for resolution in RecoveryResolution),
    )
    reconcile.add_argument("--database", type=Path, required=True)
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("run_id")
    cancel.add_argument("--database", type=Path, required=True)
    trace = subparsers.add_parser("trace")
    trace.add_argument("run_id")
    trace.add_argument("--database", type=Path, required=True)
    trace.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    if args.command == "trace":
        store = SQLiteStore(args.database)
        store.initialize()
        print(json.dumps(TraceService(store).export_run(args.run_id), sort_keys=True))
        return 0
    store, _executor, runtime, recovery = _components(args.database)
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
