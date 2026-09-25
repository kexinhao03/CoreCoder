"""Evidence-driven reconciliation for interrupted ML experiment effects."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from corecoder.runtime import ToolCallStatus

from .artifacts import ArtifactVerifier
from .workflow import MLExperimentWorkflow


class ReconciliationDecision(str, Enum):
    COMPLETED = "completed"
    RETRY = "retry"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ReconciliationResult:
    decision: ReconciliationDecision
    reason: str | None = None
    new_tool_call_id: str | None = None
    new_approval_id: str | None = None


class MLReconciliationService:
    def __init__(self, workflow: MLExperimentWorkflow) -> None:
        self.workflow = workflow
        self.store = workflow.store

    def reconcile(
        self, call_id: str, decision: ReconciliationDecision
    ) -> ReconciliationResult:
        decision = ReconciliationDecision(decision)
        call = self.store.get_tool_call(call_id)
        run = self.store.get_run(call.run_id)
        definition = self.workflow._validate_definition(run)
        if (
            call.status is not ToolCallStatus.INTERRUPTED
            or call.tool_name != "ml_experiment.run_experiment"
        ):
            raise ValueError("reconciliation requires an interrupted ML experiment")
        process = self.store.get_process_evidence(call.id)
        if process is None or not process.termination_confirmed:
            return self._unresolved(call.id, "process_not_confirmed_terminated")
        artifacts = definition.artifacts
        if decision is ReconciliationDecision.UNRESOLVED:
            return self._unresolved(call.id, "user_requested_unresolved")
        if artifacts.metrics_tmp_file.exists():
            return self._unresolved(call.id, "temporary_metrics_present")
        if decision is ReconciliationDecision.COMPLETED:
            try:
                metrics, effect = ArtifactVerifier.verify_completed_experiment(
                    artifacts.metrics_file,
                    artifacts.effects_file,
                    dataset_sha256=definition.dataset_sha256,
                    experiment_id=definition.experiment_id,
                    workflow_version=definition.workflow_version,
                    definition_hash=definition.step("run_experiment").definition_hash,
                )
            except (OSError, TypeError, ValueError):
                return self._unresolved(call.id, "artifact_invalid")
            evidence = {
                "definition_hash": definition.step("run_experiment").definition_hash,
                "effect": asdict(effect),
                "experiment_id": definition.experiment_id,
                "metrics": asdict(metrics),
                "process": asdict(process),
                "workspace": str(definition.artifacts.workspace),
            }
            self.store.reconcile_workflow_completed(call.id, evidence)
            return ReconciliationResult(ReconciliationDecision.COMPLETED)
        if artifacts.metrics_file.exists():
            return self._unresolved(call.id, "final_metrics_present")
        if artifacts.effects_file.exists() and artifacts.effects_file.read_text().strip():
            return self._unresolved(call.id, "effect_marker_present")
        evidence = {
            "definition_hash": definition.step("run_experiment").definition_hash,
            "experiment_id": definition.experiment_id,
            "process": asdict(process),
            "reason": "no_final_metrics_or_effect_marker",
            "workspace": str(definition.artifacts.workspace),
        }
        retry, approval = self.store.approve_workflow_retry(call.id, evidence)
        return ReconciliationResult(
            ReconciliationDecision.RETRY,
            new_tool_call_id=retry.id,
            new_approval_id=approval.id,
        )

    def _unresolved(self, call_id: str, reason: str) -> ReconciliationResult:
        self.store.record_unresolved_reconciliation(call_id, {"reason": reason})
        return ReconciliationResult(ReconciliationDecision.UNRESOLVED, reason=reason)
