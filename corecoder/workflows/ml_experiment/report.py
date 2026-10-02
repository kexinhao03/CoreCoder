"""Deterministic evidence reports for completed ML experiment runs."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from corecoder.runtime import MetricsService, RunStatus, StepStatus, TraceService

from .artifacts import sha256_file
from .workflow import MLExperimentWorkflow


class MLExperimentReportService:
    def __init__(self, workflow: MLExperimentWorkflow) -> None:
        self.workflow = workflow
        self.store = workflow.store

    def build(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        steps = self.store.list_steps(run_id)
        if run.status is not RunStatus.SUCCEEDED or any(
            step.status is not StepStatus.SUCCEEDED for step in steps
        ):
            raise ValueError("report requires one fully succeeded ML workflow Run")
        definition = self.workflow._validate_definition(run)
        calls = self.store.list_tool_calls(run_id)
        extraction_call = self._succeeded_call(calls, "extract_metrics")
        effect_call = self._succeeded_call(calls, "verify_effect")
        extraction = json.loads(extraction_call.result_summary)
        effect = json.loads(effect_call.result_summary)
        metrics_hash = sha256_file(definition.artifacts.metrics_file)
        effects_hash = sha256_file(definition.artifacts.effects_file)
        if (
            metrics_hash != extraction.get("metrics_file_sha256")
            or effects_hash != effect.get("effects_file_sha256")
            or effect.get("effect_count") != 1
            or effect.get("duplicate_effect") is not False
        ):
            raise ValueError("Artifact Integrity Failure")
        approvals = self.store.list_approvals(run_id)
        experiment_call = self._succeeded_call(calls, "run_experiment")
        experiment_step = definition.step("run_experiment")
        matching_approvals = [
            approval
            for approval in approvals
            if approval.tool_call_id == experiment_call.id
            and approval.status.value == "approved"
            and approval.decision is not None
            and approval.decision.value == "allow_once"
            and approval.attempt == experiment_call.attempt
            and approval.workspace == run.workspace
            and approval.experiment_id == definition.experiment_id
            and approval.definition_hash == experiment_step.definition_hash
        ]
        if len(matching_approvals) != 1:
            raise ValueError("Approval Integrity Failure")
        trace = TraceService(self.store).export_run(run_id)
        if (
            trace["integrity"]["missing"]
            or not trace["integrity"]["sequence_contiguous"]
        ):
            raise ValueError("Trace Integrity Failure")
        metrics = asdict(MetricsService(self.store).summarize_run(run_id))
        root = Path(__file__).resolve().parents[3]
        git_commit = self._git(root, "rev-parse", "HEAD")
        git_dirty = bool(self._git(root, "status", "--porcelain"))
        reconciliation = [
            asdict(item)
            for call in calls
            for item in self.store.list_reconciliation_evidence(call.id)
        ]
        return {
            "approval_evidence": [self._record(item) for item in approvals],
            "conclusion_boundaries": {
                "effect_marker_scope": "fixture_only",
                "exactly_once_claim": "not_generalized_beyond_fixture",
                "network_services": "not_used",
            },
            "environment": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
            },
            "experiment": {
                "duplicate_effect": effect["duplicate_effect"],
                "effect_count": effect["effect_count"],
                "effects_file_sha256": effects_hash,
                "experiment_id": definition.experiment_id,
                "metrics_file_sha256": metrics_hash,
                "normalized_metrics": extraction["normalized_metrics"],
            },
            "git": {"commit": git_commit, "dirty": git_dirty},
            "reconciliation_evidence": reconciliation,
            "report_schema_version": "1.0",
            "run": self._record(run),
            "runtime_metrics": metrics,
            "steps": [self._record(step) for step in steps],
            "token_cost": metrics["token_cost"],
            "tool_calls": [self._record(call) for call in calls],
            "trace_integrity": trace["integrity"],
            "workflow_version": definition.workflow_version,
        }

    def write(self, raw: dict) -> tuple[Path, Path]:
        run_id = raw["run"]["id"]
        artifacts = self.workflow.definition(run_id).artifacts
        raw_text = json.dumps(raw, indent=2, sort_keys=True) + "\n"
        metrics = raw["experiment"]["normalized_metrics"]
        markdown = (
            "# ReliAgent ML Experiment Report\n\n"
            f"- Run: `{run_id}`\n"
            f"- Status: `{raw['run']['status']}`\n"
            f"- Workflow version: `{raw['workflow_version']}`\n"
            f"- Slope: `{metrics['slope']}`\n"
            f"- Intercept: `{metrics['intercept']}`\n"
            f"- MSE: `{metrics['mse']}`\n"
            f"- R²: `{metrics['r2']}`\n"
            f"- Effect count: `{raw['experiment']['effect_count']}`\n"
            f"- Duplicate effect: `{str(raw['experiment']['duplicate_effect']).lower()}`\n\n"
            "The effect marker proves non-duplication only for this local fixture.\n"
        )
        self._atomic_write(artifacts.raw_report, raw_text)
        self._atomic_write(artifacts.markdown_report, markdown)
        return artifacts.raw_report, artifacts.markdown_report

    @staticmethod
    def _succeeded_call(calls, suffix):
        matches = [
            call
            for call in calls
            if call.tool_name.endswith(suffix) and call.status.value == "succeeded"
        ]
        if len(matches) != 1 or not matches[0].result_summary:
            raise ValueError(f"report requires one persisted {suffix} result")
        return matches[0]

    @staticmethod
    def _record(record):
        value = asdict(record)
        for key, item in tuple(value.items()):
            if hasattr(item, "value"):
                value[key] = item.value
        return value

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY | directory_flag)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
