"""Approval-gated execution of persisted tool calls with bounded safe retries."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    summarize_arguments,
)
from .models import ToolCallRecord
from .policies import FailureKind, ToolPolicyRegistry
from .processes import ManagedProcessRunner, ProcessSpec
from .state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus
from .store import SQLiteStore


@dataclass(frozen=True)
class PendingApproval:
    call: ToolCallRecord
    approval: ApprovalRecord


@dataclass(frozen=True)
class RuntimeResult:
    call: ToolCallRecord
    output: str
    failure_kind: FailureKind | None


class ExecutionRefused(RuntimeError):
    pass


def _validated_argv(argv: object) -> tuple[str, ...]:
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
        raise ValueError("argv must be a nonempty sequence of strings")  # noqa: TRY004 - argv contract
    normalized = tuple(argv)
    if not normalized:
        raise ValueError("argv must not be empty")
    if any(not isinstance(argument, str) for argument in normalized):
        raise ValueError("argv elements must be strings")
    if any("\x00" in argument for argument in normalized):
        raise ValueError("argv elements must not contain NUL")
    return normalized


class RuntimeExecutor:
    def __init__(
        self,
        store: SQLiteStore,
        policies: ToolPolicyRegistry,
        process_runner: ManagedProcessRunner | None = None,
    ) -> None:
        self._store = store
        self._policies = policies
        self._process_runner = process_runner or ManagedProcessRunner()
        self._active_cancellations: dict[str, threading.Event] = {}

    def submit_subprocess(
        self,
        run_id: str,
        tool_name: str,
        argv: Sequence[str],
        *,
        tool_call_id: str | None = None,
        approval_id: str | None = None,
    ) -> PendingApproval | RuntimeResult:
        argv = _validated_argv(argv)
        call = self._submit(
            run_id, tool_name, {"argv": list(argv)}, ExecutionKind.SUBPROCESS,
            tool_call_id=tool_call_id, approval_id=approval_id,
        )
        if isinstance(call, PendingApproval):
            return call
        return self._execute_subprocess(call)

    def submit_in_process(
        self,
        run_id: str,
        tool_name: str,
        arguments: dict,
        operation: Callable[[dict, threading.Event], str],
        *,
        tool_call_id: str | None = None,
        approval_id: str | None = None,
    ) -> PendingApproval | RuntimeResult:
        call = self._submit(
            run_id, tool_name, arguments, ExecutionKind.IN_PROCESS,
            tool_call_id=tool_call_id, approval_id=approval_id,
        )
        if isinstance(call, PendingApproval):
            return call
        return self._execute_in_process(call, operation)

    def _submit(
        self,
        run_id: str,
        tool_name: str,
        arguments: dict,
        execution_kind: ExecutionKind,
        *,
        tool_call_id: str | None,
        approval_id: str | None,
    ) -> PendingApproval | ToolCallRecord:
        run = self._store.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ExecutionRefused("running run required")
        policy = self._policies.resolve(tool_name)
        if policy.execution_kind is not execution_kind:
            raise ExecutionRefused(f"{execution_kind.value.replace('_', '-')} policy required")
        idempotency_key = None
        if policy.idempotent:
            identity = json.dumps(
                {"run_id": run_id, "tool_name": tool_name, "arguments": arguments},
                sort_keys=True,
            )
            idempotency_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        call = self._store.create_tool_call(
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=policy.risk_level,
            execution_kind=policy.execution_kind,
            idempotent=policy.idempotent,
            idempotency_key=idempotency_key,
            timeout_seconds=policy.timeout_seconds,
            tool_call_id=tool_call_id,
        )
        if policy.requires_approval:
            approval = self._store.request_approval(
                call.id,
                arguments_summary=summarize_arguments(arguments),
                workspace=run.workspace,
                risk_reason=policy.risk_level.value,
                approval_id=approval_id,
            )
            return PendingApproval(self._store.get_tool_call(call.id), approval)
        return call

    def execute_approved_subprocess(self, call_id: str) -> RuntimeResult:
        return self._execute_subprocess(self._approved_call(call_id))

    def execute_approved_in_process(
        self, call_id: str, operation: Callable[[dict, threading.Event], str]
    ) -> RuntimeResult:
        return self._execute_in_process(self._approved_call(call_id), operation)

    def _approved_call(self, call_id: str) -> ToolCallRecord:
        approval = self._store.get_approval_for_tool_call(call_id)
        if (
            approval is None
            or approval.status is not ApprovalStatus.APPROVED
            or approval.decision is not ApprovalDecision.ALLOW_ONCE
        ):
            raise ExecutionRefused("approved allow-once decision required")
        call = self._store.get_tool_call(call_id)
        if call.status is not ToolCallStatus.WAITING_APPROVAL:
            raise ExecutionRefused("approved call is no longer waiting for execution")
        return call

    def _execute_in_process(
        self, call: ToolCallRecord, operation: Callable[[dict, threading.Event], str]
    ) -> RuntimeResult:
        """Invoke synchronously; cancellation requires the callable's cooperation."""
        if self._store.get_run(call.run_id).status is not RunStatus.RUNNING:
            raise ExecutionRefused("running run required")
        policy = self._policies.resolve(call.tool_name)
        if (
            call.execution_kind is not ExecutionKind.IN_PROCESS
            or policy.execution_kind is not ExecutionKind.IN_PROCESS
        ):
            raise ExecutionRefused("in-process policy required")
        call = self._store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
        cancellation = threading.Event()
        self._active_cancellations[call.id] = cancellation
        output = ""
        failure_kind = None
        try:
            if not cancellation.is_set():
                try:
                    output = operation(call.arguments, cancellation)
                except Exception as error:  # noqa: BLE001 - persist callable failures, not BaseException
                    output = str(error)
                    failure_kind = FailureKind.EXECUTION_ERROR
            if cancellation.is_set():
                failure_kind = FailureKind.CANCELLED
        finally:
            self._active_cancellations.pop(call.id, None)

        if failure_kind is FailureKind.CANCELLED:
            status, event = ToolCallStatus.CANCELLED, "tool.cancelled"
        elif failure_kind is FailureKind.EXECUTION_ERROR:
            status, event = ToolCallStatus.FAILED, "tool.failed"
        else:
            status, event = ToolCallStatus.SUCCEEDED, "tool.completed"
        output = output[:policy.output_limit]
        call = self._store.transition_tool_call(
            call.id, status, event, result_summary=output,
            payload={"failure_kind": failure_kind.value if failure_kind else None},
        )
        return RuntimeResult(call, output, failure_kind)

    def _execute_subprocess(self, call: ToolCallRecord) -> RuntimeResult:
        while True:
            result = self._execute_subprocess_once(call)
            if not self._should_retry(result):
                return result
            source = result.call
            self._store.record_event(
                source.run_id, "tool.retry_scheduled",
                {"tool_call_id": source.id, "attempt": source.attempt + 1},
            )
            call = self._store.create_tool_call(
                run_id=source.run_id,
                tool_name=source.tool_name,
                arguments=source.arguments,
                risk_level=source.risk_level,
                execution_kind=source.execution_kind,
                idempotent=source.idempotent,
                idempotency_key=source.idempotency_key,
                timeout_seconds=source.timeout_seconds,
                retry_of=source.id,
            )

    def _should_retry(self, result: RuntimeResult) -> bool:
        source = result.call
        policy = self._policies.resolve(source.tool_name)
        return (
            source.status in {ToolCallStatus.FAILED, ToolCallStatus.TIMED_OUT}
            and source.risk_level is RiskLevel.READ_ONLY
            and source.idempotent
            and policy.auto_retry
            and result.failure_kind in policy.retryable_failures
            and source.attempt < policy.max_attempts
            and self._store.get_run(source.run_id).status is RunStatus.RUNNING
        )

    def _execute_subprocess_once(self, call: ToolCallRecord) -> RuntimeResult:
        if not isinstance(call.arguments, dict) or not isinstance(call.arguments.get("argv"), list):
            raise ValueError("persisted arguments must contain an argv list")  # noqa: TRY004 - argv contract
        argv = _validated_argv(call.arguments["argv"])
        run = self._store.get_run(call.run_id)
        if run.status is not RunStatus.RUNNING:
            raise ExecutionRefused("running run required")
        policy = self._policies.resolve(call.tool_name)
        if (
            call.execution_kind is not ExecutionKind.SUBPROCESS
            or policy.execution_kind is not ExecutionKind.SUBPROCESS
        ):
            raise ExecutionRefused("subprocess policy required")
        # Validate persisted argv before claiming the call has started.
        call = self._store.transition_tool_call(
            call.id, ToolCallStatus.RUNNING, "tool.started"
        )
        cancellation = threading.Event()
        self._active_cancellations[call.id] = cancellation
        try:
            spec = ProcessSpec(
                argv=argv,
                cwd=Path(run.workspace),
                timeout_seconds=call.timeout_seconds,
                output_limit=policy.output_limit,
                termination_grace_seconds=1.0,
            )
            result = self._process_runner.run(spec, cancellation)
        finally:
            self._active_cancellations.pop(call.id, None)

        failure_kind = result.failure_kind
        if failure_kind is FailureKind.TIMED_OUT:
            status, event = ToolCallStatus.TIMED_OUT, "tool.timed_out"
        elif failure_kind is FailureKind.CANCELLED:
            status, event = ToolCallStatus.CANCELLED, "tool.cancelled"
        elif failure_kind is FailureKind.TERMINATION_UNKNOWN:
            status, event = ToolCallStatus.INTERRUPTED, "tool.interrupted"
        elif failure_kind is None and result.exit_code == 0:
            status, event = ToolCallStatus.SUCCEEDED, "tool.completed"
        else:
            status, event = ToolCallStatus.FAILED, "tool.failed"

        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        output = output[:policy.output_limit]
        call = self._store.transition_tool_call(
            call.id,
            status,
            event,
            result_summary=output,
            payload={
                "exit_code": result.exit_code,
                "failure_kind": failure_kind.value if failure_kind else None,
            },
        )
        return RuntimeResult(call, output, failure_kind)
