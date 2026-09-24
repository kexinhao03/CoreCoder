"""Command-line entry point for fixed, persisted ReliAgent tasks."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .reliagent import ReliAgentRuntime, TaskStep
from .runtime import (
    ExecutionKind,
    RiskLevel,
    RuntimeExecutor,
    SQLiteStore,
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
        if not isinstance(raw_step, dict) or raw_step.get("tool_name") != "local_read_only":
            raise ValueError("each task step must use tool_name local_read_only")
        argv = raw_step.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("each task step must contain a nonempty argv string list")
        steps.append(TaskStep("local_read_only", tuple(argv)))
    return value["goal"], Path(value["workspace"]), tuple(steps)


def _runtime(database: Path) -> ReliAgentRuntime:
    store = SQLiteStore(database)
    store.initialize()
    policy = ToolPolicy(
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=30,
        max_attempts=1,
        idempotent=True,
        auto_retry=False,
        retryable_failures=frozenset(),
        output_limit=15_000,
    )
    return ReliAgentRuntime(
        store, RuntimeExecutor(store, ToolPolicyRegistry({"local_read_only": policy}))
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reliagent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume"):
        command = subparsers.add_parser(name)
        if name == "resume":
            command.add_argument("run_id")
        command.add_argument("task_file", type=Path)
        command.add_argument("--database", type=Path, required=True)
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
    goal, workspace, steps = _task_from_file(args.task_file)
    runtime = _runtime(args.database)
    if args.command == "run":
        run = runtime.run_task(goal=goal, workspace=workspace, steps=steps)
    else:
        run = runtime.resume(args.run_id, steps=steps).run
    print(json.dumps({"run_id": run.id, "status": run.status.value}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
