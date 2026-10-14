"""Approval-gated execution of persisted tool calls with bounded safe retries."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    summarize_arguments,
)
from .faults import FaultCheckpoint, RuntimeFaultInjector
from .models import RunRecord, ToolCallRecord
from .policies import FailureKind, ToolPolicyRegistry
from .processes import ManagedProcessRunner, ProcessResult, ProcessSpec, _bound_output
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
    exit_code: int | None = None


class ExecutionRefused(RuntimeError):
    pass


class UnknownEffect(RuntimeError):
    """An operation cannot establish whether its external effect completed."""


@dataclass(frozen=True)
class AgentExecution:
    journal: object
    invocation_id: str
    finalize: Callable[[str, FailureKind | None, int | None], tuple[str, dict]]
    before_execute: Callable[[threading.Event], str | None] | None = None


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
        fault_injector: RuntimeFaultInjector | None = None,
    ) -> None:
        self._store = store
        self._policies = policies
        self._process_runner = process_runner or ManagedProcessRunner()
        self._fault_injector = fault_injector
        self._execution_arguments: dict[str, dict] = {}
        self._agent_executions: dict[str, AgentExecution] = {}
        self._active_cancellations: dict[str, tuple[str, threading.Event]] = {}
        self._cancellation_monitor_stops: dict[str, threading.Event] = {}
        self._cancellation_monitor_threads: dict[str, threading.Thread] = {}
        self._cancellation_lock = threading.Lock()
        self._store.register_terminal_tool_call_listener(
            self._discard_execution_arguments
        )

    def _discard_execution_arguments(self, call_id: str) -> None:
        self._execution_arguments.pop(call_id, None)
        self._agent_executions.pop(call_id, None)

    def execute_agent_invocation(
        self, journal, invocation: dict, arguments: dict, *, operation=None,
        operation_arguments: dict | None = None, timeout_seconds: int | None = None,
        decide_approval: Callable[[ApprovalRecord, dict], str | None],
        finalize: Callable[[str, FailureKind | None, int | None], tuple[str, dict]],
        before_execute: Callable[[threading.Event], str | None] | None = None,
    ) -> RuntimeResult:
        """Execute or restore one bound invocation; provider args never become argv.

        The caller holds the Run lease and has validated its checkpoint metadata.
        ``operation`` can run independently journaled child work; UnknownEffect
        must be raised whenever its remote/child outcome cannot be established.
        """
        saved = next((item for item in journal.list_invocations(invocation["run_id"])
                      if item["id"] == invocation["id"]), None)
        if saved is None or any(saved[field] != invocation[field] for field in (
            "run_id", "turn_sequence", "provider_call_id", "ordinal", "tool_name", "attempt_id",
        )):
            raise ValueError("agent invocation identity mismatch")
        original = json.loads(saved["arguments"]) if isinstance(saved["arguments"], str) else saved["arguments"]
        if original != arguments:
            raise ValueError("execution arguments differ from original provider arguments")
        invocation = saved
        policy = self._policies.resolve(invocation["tool_name"])
        attempt_id = invocation["attempt_id"]
        if attempt_id is None:
            submitted = self._submit(
                invocation["run_id"], invocation["tool_name"], arguments, policy.execution_kind,
                tool_call_id=None, step_id=None, approval_id=None,
                approval_experiment_id=None, approval_definition_hash=None,
                timeout_seconds=timeout_seconds, agent_invocation_id=invocation["id"],
            )
            call = submitted.call if isinstance(submitted, PendingApproval) else submitted
        else:
            call = self._store.get_tool_call(attempt_id)
            if call.status not in {ToolCallStatus.CREATED, ToolCallStatus.WAITING_APPROVAL}:
                raise ExecutionRefused("agent invocation is not unstarted")
            if (call.risk_level is not policy.risk_level or call.execution_kind is not policy.execution_kind
                    or call.idempotent != policy.idempotent
                    or call.timeout_seconds != (policy.timeout_seconds if timeout_seconds is None else timeout_seconds)):
                raise ValueError("restored attempt policy differs from original execution policy")
        self._execution_arguments[call.id] = deepcopy(operation_arguments if operation_arguments is not None else arguments)
        self._agent_executions[call.id] = AgentExecution(journal, invocation["id"], finalize, before_execute)
        try:
            approval = self._store.get_approval_for_tool_call(call.id)
            if approval is None and policy.requires_approval:
                approval = self._store.request_approval(
                    call.id, arguments_summary=summarize_arguments(call.arguments),
                    workspace=self._store.get_run(call.run_id).workspace, risk_reason=policy.risk_level.value,
                )
            if approval is not None and approval.status is ApprovalStatus.PENDING:
                refusal = decide_approval(approval, arguments)
                if refusal is not None:
                    token = journal.result_token(invocation["id"], refusal)
                    self._store.resolve_approval(approval.id, ApprovalDecision.DENY, agent_result=token)
                    return RuntimeResult(self._store.get_tool_call(call.id), refusal, None)
                self._store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
            if approval is not None:
                call = self._approved_call(call.id)
            if policy.execution_kind is ExecutionKind.SUBPROCESS:
                return self._execute_subprocess(call)
            if operation is None:
                raise ExecutionRefused("in-process operation is required")
            return self._execute_in_process(call, operation)
        finally:
            self._discard_execution_arguments(call.id)

    def _before_agent_execution(self, call, cancellation):
        context = self._agent_executions.get(call.id)
        if context is None or context.before_execute is None:
            return None
        try:
            return context.before_execute(cancellation)
        except Exception as error:
            raise UnknownEffect("pre-operation outcome requires reconciliation") from error

    def _agent_result(self, call, output, failure_kind, exit_code=None):
        context = self._agent_executions.get(call.id)
        if context is None:
            return output, None
        output, updates = context.finalize(output, failure_kind, exit_code)
        return output, context.journal.result_token(context.invocation_id, output, state_updates=updates)

    def cancel_run(self, run_id: str) -> RunRecord:
        run = self._store.cancel_run(run_id)
        with self._cancellation_lock:
            for call in self._store.list_tool_calls(run_id=run_id):
                self._execution_arguments.pop(call.id, None)
            for active_run_id, cancellation in self._active_cancellations.values():
                if active_run_id == run_id:
                    cancellation.set()
        return run

    def _require_running(self, run_id: str) -> RunRecord:
        run = self._store.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ExecutionRefused(f"run cannot accept work: {run.status.value}")
        return run

    def _start_call(self, call: ToolCallRecord) -> ToolCallRecord:
        try:
            return self._store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
        except ValueError:
            self._require_running(call.run_id)
            raise

    def _register_cancellation(self, call: ToolCallRecord) -> threading.Event:
        cancellation = threading.Event()
        monitor_stop = threading.Event()
        monitor = threading.Thread(
            target=self._monitor_persisted_cancellation,
            args=(call.run_id, cancellation, monitor_stop),
            daemon=True,
        )
        with self._cancellation_lock:
            self._active_cancellations[call.id] = (call.run_id, cancellation)
            self._cancellation_monitor_stops[call.id] = monitor_stop
            self._cancellation_monitor_threads[call.id] = monitor
            # Cancellation may commit after tool.started but before registration.
            # Register first so a later cancellation either finds us or is seen here.
            if self._store.get_run(call.run_id).status is RunStatus.CANCELLED:
                cancellation.set()
        monitor.start()
        return cancellation

    def _monitor_persisted_cancellation(
        self,
        run_id: str,
        cancellation: threading.Event,
        monitor_stop: threading.Event,
    ) -> None:
        while not monitor_stop.wait(0.05):
            if self._store.get_run(run_id).status is RunStatus.CANCELLED:
                cancellation.set()
                return

    def _unregister_cancellation(self, call_id: str) -> None:
        with self._cancellation_lock:
            self._active_cancellations.pop(call_id, None)
            monitor_stop = self._cancellation_monitor_stops.pop(call_id, None)
            monitor = self._cancellation_monitor_threads.pop(call_id, None)
        if monitor_stop is not None:
            monitor_stop.set()
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join()

    def submit_subprocess(
        self,
        run_id: str,
        tool_name: str,
        argv: Sequence[str],
        *,
        tool_call_id: str | None = None,
        step_id: str | None = None,
        approval_id: str | None = None,
        approval_experiment_id: str | None = None,
        approval_definition_hash: str | None = None,
        cwd: Path | None = None,
        timeout_seconds: int | None = None,
    ) -> PendingApproval | RuntimeResult:
        argv = _validated_argv(argv)
        arguments = {"argv": list(argv)}
        if cwd is not None:
            arguments["cwd"] = str(cwd.resolve())
        call = self._submit(
            run_id, tool_name, arguments, ExecutionKind.SUBPROCESS,
            tool_call_id=tool_call_id, step_id=step_id, approval_id=approval_id,
            approval_experiment_id=approval_experiment_id,
            approval_definition_hash=approval_definition_hash,
            timeout_seconds=timeout_seconds,
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
        step_id: str | None = None,
        approval_id: str | None = None,
    ) -> PendingApproval | RuntimeResult:
        call = self._submit(
            run_id, tool_name, arguments, ExecutionKind.IN_PROCESS,
            tool_call_id=tool_call_id, step_id=step_id, approval_id=approval_id,
            approval_experiment_id=None, approval_definition_hash=None,
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
        step_id: str | None,
        approval_id: str | None,
        approval_experiment_id: str | None,
        approval_definition_hash: str | None,
        timeout_seconds: int | None = None,
        agent_invocation_id: str | None = None,
    ) -> PendingApproval | ToolCallRecord:
        run = self._require_running(run_id)
        policy = self._policies.resolve(tool_name)
        if policy.execution_kind is not execution_kind:
            raise ExecutionRefused(f"{execution_kind.value.replace('_', '-')} policy required")
        if timeout_seconds is not None and (
            type(timeout_seconds) is not int or not 0 < timeout_seconds <= policy.timeout_seconds
        ):
            raise ValueError("timeout must be a positive integer within the tool policy limit")
        idempotency_key = None
        if policy.idempotent:
            identity = json.dumps(
                {"run_id": run_id, "tool_name": tool_name, "arguments": arguments},
                sort_keys=True,
            )
            idempotency_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        call = None
        try:
            call = self._store.create_tool_call(
                run_id=run_id,
                tool_name=tool_name,
                arguments=arguments,
                risk_level=policy.risk_level,
                execution_kind=policy.execution_kind,
                idempotent=policy.idempotent,
                idempotency_key=idempotency_key,
                timeout_seconds=policy.timeout_seconds if timeout_seconds is None else timeout_seconds,
                step_id=step_id,
                tool_call_id=tool_call_id,
                agent_invocation_id=agent_invocation_id,
            )
            with self._cancellation_lock:
                self._require_running(run_id)
                self._execution_arguments[call.id] = deepcopy(arguments)
            if policy.requires_approval:
                approval = self._store.request_approval(
                    call.id,
                    arguments_summary=summarize_arguments(call.arguments),
                    workspace=run.workspace,
                    risk_reason=policy.risk_level.value,
                    approval_id=approval_id,
                    experiment_id=approval_experiment_id,
                    definition_hash=approval_definition_hash,
                )
                return PendingApproval(self._store.get_tool_call(call.id), approval)
        except ValueError:
            if call is not None:
                self._execution_arguments.pop(call.id, None)
            self._require_running(run_id)
            raise
        return call

    def execute_approved_subprocess(self, call_id: str) -> RuntimeResult:
        return self._execute_subprocess(self._approved_call(call_id))

    def execute_recovery_subprocess(self, call_id: str) -> RuntimeResult:
        call = self._store.get_tool_call(call_id)
        if call.status is not ToolCallStatus.CREATED or call.retry_of is None:
            raise ExecutionRefused("created recovery retry required")
        return self._execute_subprocess(call)

    def execute_approved_in_process(
        self, call_id: str, operation: Callable[[dict, threading.Event], str]
    ) -> RuntimeResult:
        return self._execute_in_process(self._approved_call(call_id), operation)

    def _approved_call(self, call_id: str) -> ToolCallRecord:
        approval = self._store.get_approval_for_tool_call(call_id)
        if approval is not None:
            call = self._store.get_tool_call(call_id)
            if call.status not in {
                ToolCallStatus.CREATED, ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.RUNNING,
            }:
                self._execution_arguments.pop(call_id, None)
        if (
            approval is None
            or approval.status is not ApprovalStatus.APPROVED
            or approval.decision is not ApprovalDecision.ALLOW_ONCE
        ):
            raise ExecutionRefused("approved allow-once decision required")
        if call.status is not ToolCallStatus.WAITING_APPROVAL:
            raise ExecutionRefused("approved call is no longer waiting for execution")
        return call

    def _execute_in_process(
        self, call: ToolCallRecord, operation: Callable[[dict, threading.Event], str]
    ) -> RuntimeResult:
        try:
            return self._execute_in_process_once(call, operation)
        finally:
            self._execution_arguments.pop(call.id, None)

    def _arguments_for_execution(self, call: ToolCallRecord) -> dict:
        raw = self._execution_arguments.get(call.id)
        if raw is not None:
            return raw
        if call.arguments_replayable:
            return call.arguments
        raise ExecutionRefused("sensitive_arguments_not_replayable")

    def _execute_in_process_once(
        self, call: ToolCallRecord, operation: Callable[[dict, threading.Event], str]
    ) -> RuntimeResult:
        """Invoke synchronously; cancellation requires the callable's cooperation."""
        self._require_running(call.run_id)
        policy = self._policies.resolve(call.tool_name)
        if (
            call.execution_kind is not ExecutionKind.IN_PROCESS
            or policy.execution_kind is not ExecutionKind.IN_PROCESS
        ):
            raise ExecutionRefused("in-process policy required")
        arguments = self._arguments_for_execution(call)
        call = self._start_call(call)
        cancellation = self._register_cancellation(call)
        output = ""
        failure_kind = None
        try:
            if not cancellation.is_set():
                try:
                    refusal = self._before_agent_execution(call, cancellation)
                    output = refusal if refusal is not None else operation(arguments, cancellation)
                except UnknownEffect as error:
                    self._store.transition_tool_call(
                        call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted",
                        result_summary=type(error).__name__,
                        payload={"failure_kind": FailureKind.TERMINATION_UNKNOWN.value},
                    )
                    raise
                except Exception as error:  # noqa: BLE001 - persist callable failures, not BaseException
                    output = str(error)
                    failure_kind = FailureKind.EXECUTION_ERROR
                except BaseException as error:
                    if policy.risk_level is RiskLevel.READ_ONLY and call.id not in self._agent_executions:
                        interruption_kind = FailureKind.CANCELLED
                        status, event = ToolCallStatus.CANCELLED, "tool.cancelled"
                    else:
                        interruption_kind = FailureKind.TERMINATION_UNKNOWN
                        status, event = ToolCallStatus.INTERRUPTED, "tool.interrupted"
                    self._store.transition_tool_call(
                        call.id,
                        status,
                        event,
                        result_summary=type(error).__name__,
                        payload={
                            "failure_kind": interruption_kind.value,
                            "reason": "execution_interrupted",
                        },
                    )
                    raise
            if cancellation.is_set():
                failure_kind = FailureKind.CANCELLED
        finally:
            self._unregister_cancellation(call.id)

        if failure_kind is FailureKind.CANCELLED:
            status, event = ToolCallStatus.CANCELLED, "tool.cancelled"
        elif failure_kind is FailureKind.EXECUTION_ERROR:
            status, event = ToolCallStatus.FAILED, "tool.failed"
        else:
            status, event = ToolCallStatus.SUCCEEDED, "tool.completed"
        output = output[:policy.output_limit]
        output, agent_result = self._agent_result(call, output, failure_kind)
        call = self._store.transition_tool_call(
            call.id, status, event, result_summary=output,
            payload={"failure_kind": failure_kind.value if failure_kind else None},
            agent_result=agent_result,
        )
        return RuntimeResult(call, output, failure_kind)

    def _execute_subprocess(self, call: ToolCallRecord) -> RuntimeResult:
        while True:
            source_id = call.id
            try:
                result = self._execute_subprocess_once(call)
                if not self._should_retry(result):
                    return result
                source = result.call
                try:
                    call = self._store.schedule_retry(source, result.failure_kind, self._policies)
                except ValueError:
                    self._require_running(source.run_id)
                    raise
                with self._cancellation_lock:
                    raw = self._execution_arguments.get(source_id)
                    if raw is not None:
                        self._execution_arguments[call.id] = deepcopy(raw)
            finally:
                self._execution_arguments.pop(source_id, None)

    def _should_retry(self, result: RuntimeResult) -> bool:
        source = result.call
        policy = self._policies.resolve(source.tool_name)
        return (
            source.id not in self._agent_executions
            and source.status in {ToolCallStatus.FAILED, ToolCallStatus.TIMED_OUT}
            and source.risk_level is RiskLevel.READ_ONLY
            and source.idempotent
            and policy.auto_retry
            and result.failure_kind in policy.retryable_failures
            and source.attempt < policy.max_attempts
            and self._store.get_run(source.run_id).status is RunStatus.RUNNING
        )

    def _execute_subprocess_once(self, call: ToolCallRecord) -> RuntimeResult:
        arguments = self._arguments_for_execution(call)
        if not isinstance(arguments, dict) or not isinstance(arguments.get("argv"), list):
            raise ValueError("persisted arguments must contain an argv list")  # noqa: TRY004 - argv contract
        argv = _validated_argv(arguments["argv"])
        run = self._require_running(call.run_id)
        policy = self._policies.resolve(call.tool_name)
        if (
            call.execution_kind is not ExecutionKind.SUBPROCESS
            or policy.execution_kind is not ExecutionKind.SUBPROCESS
        ):
            raise ExecutionRefused("subprocess policy required")
        # Validate persisted argv before claiming the call has started.
        call = self._start_call(call)
        if self._fault_injector is not None:
            self._fault_injector.checkpoint(
                call, FaultCheckpoint.ENVIRONMENT_AFTER_START.value
            )
        cancellation = self._register_cancellation(call)
        try:
            spec = ProcessSpec(
                argv=argv,
                cwd=Path(arguments.get("cwd", run.workspace)),
                timeout_seconds=call.timeout_seconds,
                output_limit=policy.output_limit,
                termination_grace_seconds=1.0,
            )
            if cancellation.is_set():
                result = ProcessResult(None, "", "", 0.0, FailureKind.CANCELLED, True)
            else:
                try:
                    refusal = self._before_agent_execution(call, cancellation)
                    result = (ProcessResult(0, refusal, "", 0.0, None, True) if refusal is not None
                              else self._process_runner.run(spec, cancellation))
                except (KeyboardInterrupt, SystemExit, UnknownEffect) as error:
                    self._store.transition_tool_call(
                        call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted",
                        result_summary=type(error).__name__,
                        payload={"failure_kind": FailureKind.TERMINATION_UNKNOWN.value, "reason": "execution_interrupted"},
                    )
                    raise
            if result.process_evidence is not None:
                self._store.record_process_evidence(call.id, result.process_evidence)
            if (
                self._fault_injector is not None
                and result.failure_kind is None
                and result.exit_code == 0
            ):
                self._fault_injector.checkpoint(
                    call, FaultCheckpoint.EXPERIMENT_AFTER_EFFECT.value
                )
        finally:
            self._unregister_cancellation(call.id)

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
        output = _bound_output(output, policy.output_limit)
        agent_result = None
        if status is not ToolCallStatus.INTERRUPTED:
            output, agent_result = self._agent_result(call, output, failure_kind, result.exit_code)
        call = self._store.transition_tool_call(
            call.id,
            status,
            event,
            result_summary=_bound_output(output, 2000),
            payload={
                "exit_code": result.exit_code,
                "failure_kind": failure_kind.value if failure_kind else None,
            },
            agent_result=agent_result,
        )
        return RuntimeResult(call, output, failure_kind, result.exit_code)
