"""Conservative startup discovery and explicit resolution of interrupted work."""

from dataclasses import dataclass
from enum import Enum

from .approvals import ApprovalStatus
from .models import RunRecord, ToolCallRecord
from .policies import ToolPolicyRegistry
from .state import RiskLevel, RunStatus, ToolCallStatus
from .store import SQLiteStore


class RecoveryKind(str, Enum):
    RETRY_ALLOWED = "retry_allowed"
    HUMAN_REQUIRED = "human_required"
    APPROVAL_PENDING = "approval_pending"
    READY_TO_START = "ready_to_start"


class RecoveryResolution(str, Enum):
    CONFIRMED_SUCCEEDED = "confirmed_succeeded"
    CONFIRMED_FAILED = "confirmed_failed"
    ABANDON = "abandon"


@dataclass(frozen=True)
class RecoveryCandidate:
    run: RunRecord
    call: ToolCallRecord
    kind: RecoveryKind
    reason: str


class RecoveryManager:
    """Discover recovery decisions without accepting any execution capability."""

    def __init__(self, store: SQLiteStore, registry: ToolPolicyRegistry) -> None:
        self.store = store
        self.registry = registry

    def scan(self) -> list[RecoveryCandidate]:
        terminal_runs = {RunStatus.SUCCEEDED, RunStatus.FAILED}
        for call in self.store.list_tool_calls(statuses=[ToolCallStatus.RUNNING]):
            if self.store.get_run(call.run_id).status not in terminal_runs:
                self.store.mark_orphaned_tool_call(call.id)

        candidates = []
        for call in self.store.list_tool_calls(
            statuses=[ToolCallStatus.INTERRUPTED, ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.CREATED]
        ):
            run = self.store.get_run(call.run_id)
            if run.status in terminal_runs:
                continue
            if call.status is ToolCallStatus.INTERRUPTED:
                run, call = self.store.mark_interrupted_run_recoverable(call.id)
                reason = self.store.get_interruption_reason(call.id) or "termination_unknown"
                policy = self.registry.resolve(call.tool_name)
                safe = (
                    run.status is RunStatus.RECOVERABLE and reason == "process_lost"
                    and call.risk_level is RiskLevel.READ_ONLY and call.idempotent
                    and policy.risk_level is RiskLevel.READ_ONLY and policy.idempotent
                    and policy.auto_retry and call.attempt < policy.max_attempts
                )
                kind = RecoveryKind.RETRY_ALLOWED if safe else RecoveryKind.HUMAN_REQUIRED
            elif call.status is ToolCallStatus.CREATED:
                kind, reason = RecoveryKind.HUMAN_REQUIRED, "created_not_started"
            else:
                approval = self.store.get_approval_for_tool_call(call.id)
                if run.status is RunStatus.CANCELLED:
                    continue
                if approval is not None and approval.status is ApprovalStatus.PENDING:
                    kind, reason = RecoveryKind.APPROVAL_PENDING, "approval_pending"
                elif approval is not None and approval.status is ApprovalStatus.APPROVED:
                    kind, reason = RecoveryKind.READY_TO_START, "approved_not_started"
                else:
                    kind, reason = RecoveryKind.HUMAN_REQUIRED, "approval_missing_or_invalid"
            candidates.append(RecoveryCandidate(run, call, kind, reason))
        return sorted(candidates, key=lambda c: (c.run.created_at, c.call.created_at, c.call.id))

    def resume_retry(self, candidate: RecoveryCandidate) -> ToolCallRecord:
        if candidate.kind is not RecoveryKind.RETRY_ALLOWED or candidate.reason != "process_lost":
            raise ValueError("candidate is not eligible for a safe recovery retry")
        return self.store.resume_recovery_retry(candidate.run, candidate.call, self.registry)

    def reconcile(self, call_id: str, resolution: RecoveryResolution) -> ToolCallRecord:
        statuses = {
            RecoveryResolution.CONFIRMED_SUCCEEDED: ToolCallStatus.SUCCEEDED,
            RecoveryResolution.CONFIRMED_FAILED: ToolCallStatus.FAILED,
            RecoveryResolution.ABANDON: ToolCallStatus.CANCELLED,
        }
        resolution = RecoveryResolution(resolution)
        return self.store.reconcile_interrupted_tool_call(call_id, statuses[resolution], resolution.value)
