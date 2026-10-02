from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKFLOW_NAME = "ml_experiment"
WORKFLOW_VERSION = "1"
MODEL = "deterministic"
CONFIG_VERSION = "ml-experiment-v1"
DATASET_SHA256 = "33383eeafabf9ba13fc7fed3075550d72846574c56fd1ac5e0d1afe1067aed20"

_PACKAGE_DIR = Path(__file__).resolve().parent
_FIXTURE_DIR = _PACKAGE_DIR / "fixture"
_DATASET_PATH = _FIXTURE_DIR / "data.csv"


def resolve_workspace(workspace: Path) -> Path:
    resolved = workspace.resolve()
    if not resolved.is_dir():
        raise ValueError(f"workspace must be an existing directory: {resolved}")
    return resolved


def _workspace_path(workspace: Path, relative: Path) -> Path:
    path = (workspace / relative).resolve()
    if not path.is_relative_to(workspace):
        raise ValueError(f"workflow path escapes workspace: {path}")
    return path


@dataclass(frozen=True)
class ArtifactPaths:
    workspace: Path
    database: Path
    artifact_dir: Path
    effects_file: Path
    metrics_tmp_file: Path
    metrics_file: Path
    report_dir: Path
    raw_report: Path
    markdown_report: Path

    @classmethod
    def for_run(
        cls, workspace: Path, run_id: str, experiment_id: str
    ) -> ArtifactPaths:
        resolved = resolve_workspace(workspace)
        artifact_dir = _workspace_path(resolved, Path("artifacts") / experiment_id)
        report_dir = _workspace_path(resolved, Path("reports") / run_id)
        return cls(
            workspace=resolved,
            database=_workspace_path(resolved, Path(".reliagent/runtime.sqlite")),
            artifact_dir=artifact_dir,
            effects_file=artifact_dir / "effects.log",
            metrics_tmp_file=artifact_dir / "metrics.tmp.json",
            metrics_file=artifact_dir / "metrics.json",
            report_dir=report_dir,
            raw_report=report_dir / "ml-experiment-result.json",
            markdown_report=report_dir / "ml-experiment-report.md",
        )


@dataclass(frozen=True)
class StepDefinition:
    step_key: str
    title: str
    sequence: int
    definition_version: str
    definition_hash: str
    tool_name: str
    script_path: Path
    argv_template: tuple[str, ...]
    policy: tuple[tuple[str, Any], ...]
    output_templates: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_name: str
    workflow_version: str
    model: str
    config_version: str
    run_id: str
    experiment_id: str
    dataset_sha256: str
    artifacts: ArtifactPaths
    steps: tuple[StepDefinition, ...]

    def step(self, step_key: str) -> StepDefinition:
        for step in self.steps:
            if step.step_key == step_key:
                return step
        raise KeyError(step_key)

    def arguments_for(self, step_key: str) -> tuple[str, ...]:
        step = self.step(step_key)
        experiment_step = self.step("run_experiment")
        values = {
            "artifact_dir": str(self.artifacts.artifact_dir),
            "database": str(self.artifacts.database),
            "dataset": str(_DATASET_PATH),
            "dataset_sha256": self.dataset_sha256,
            "definition_hash": experiment_step.definition_hash,
            "effects": str(self.artifacts.effects_file),
            "experiment_id": self.experiment_id,
            "metrics": str(self.artifacts.metrics_file),
            "report_dir": str(self.artifacts.report_dir),
            "workflow_version": self.workflow_version,
            "workspace": str(self.artifacts.workspace),
        }
        return tuple(argument.format_map(values) for argument in step.argv_template)


@dataclass(frozen=True)
class NormalizedMetrics:
    dataset_sha256: str
    definition_hash: str
    experiment_id: str
    intercept: float
    mse: float
    algorithm: str
    r2: float
    sample_count: int
    slope: float
    workflow_version: str
    metrics_file_sha256: str


@dataclass(frozen=True)
class EffectEvidence:
    effect_count: int
    duplicate_effect: bool
    effects_file_sha256: str


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _definition_hash(
    *,
    step_key: str,
    script: str,
    argv_template: tuple[str, ...],
    policy: tuple[tuple[str, Any], ...],
    output_templates: tuple[str, ...],
) -> str:
    payload = {
        "argv_template": argv_template,
        "data_sha256": DATASET_SHA256,
        "definition_version": WORKFLOW_VERSION,
        "output_templates": output_templates,
        "policy": dict(policy),
        "script_sha256": _sha256_file(_FIXTURE_DIR / script),
        "step_key": step_key,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _step(
    sequence: int,
    step_key: str,
    title: str,
    script: str,
    argv_template: tuple[str, ...],
    policy: tuple[tuple[str, Any], ...],
    output_templates: tuple[str, ...] = (),
) -> StepDefinition:
    return StepDefinition(
        step_key=step_key,
        title=title,
        sequence=sequence,
        definition_version=WORKFLOW_VERSION,
        definition_hash=_definition_hash(
            step_key=step_key,
            script=script,
            argv_template=argv_template,
            policy=policy,
            output_templates=output_templates,
        ),
        tool_name=f"ml_experiment.{step_key}",
        script_path=_FIXTURE_DIR / script,
        argv_template=argv_template,
        policy=policy,
        output_templates=output_templates,
    )


def build_definition(workspace: Path, run_id: str) -> WorkflowDefinition:
    resolved = resolve_workspace(workspace)
    steps = (
        _step(
            1,
            "environment_check",
            "Check local experiment environment",
            "check.py",
            (
                "--workspace",
                "{workspace}",
                "--dataset",
                "{dataset}",
                "--dataset-sha256",
                "{dataset_sha256}",
                "--database",
                "{database}",
                "--artifact-dir",
                "{artifact_dir}",
                "--report-dir",
                "{report_dir}",
            ),
            (("idempotent", True), ("risk", "read_only")),
        ),
        _step(
            2,
            "run_experiment",
            "Run deterministic OLS experiment",
            "train.py",
            (
                "--workspace",
                "{workspace}",
                "--dataset",
                "{dataset}",
                "--dataset-sha256",
                "{dataset_sha256}",
                "--experiment-id",
                "{experiment_id}",
                "--workflow-version",
                "{workflow_version}",
                "--definition-hash",
                "{definition_hash}",
                "--metrics",
                "{metrics}",
                "--effects",
                "{effects}",
            ),
            (
                ("approval_required", True),
                ("idempotent", False),
                ("risk", "external_effect"),
            ),
            (
                "artifacts/{experiment_id}/effects.log",
                "artifacts/{experiment_id}/metrics.json",
            ),
        ),
        _step(
            3,
            "extract_metrics",
            "Extract normalized experiment metrics",
            "extract.py",
            (
                "--metrics",
                "{metrics}",
                "--dataset-sha256",
                "{dataset_sha256}",
                "--experiment-id",
                "{experiment_id}",
                "--workflow-version",
                "{workflow_version}",
                "--definition-hash",
                "{definition_hash}",
            ),
            (("idempotent", True), ("risk", "read_only")),
        ),
        _step(
            4,
            "verify_effect",
            "Verify exactly one experiment effect",
            "verify_effect.py",
            (
                "--effects",
                "{effects}",
                "--experiment-id",
                "{experiment_id}",
                "--definition-hash",
                "{definition_hash}",
            ),
            (("idempotent", True), ("risk", "read_only")),
        ),
    )
    experiment_step = next(step for step in steps if step.step_key == "run_experiment")
    experiment_id = hashlib.sha256(
        f"{run_id}{WORKFLOW_VERSION}{experiment_step.definition_hash}".encode()
    ).hexdigest()
    return WorkflowDefinition(
        workflow_name=WORKFLOW_NAME,
        workflow_version=WORKFLOW_VERSION,
        model=MODEL,
        config_version=CONFIG_VERSION,
        run_id=run_id,
        experiment_id=experiment_id,
        dataset_sha256=DATASET_SHA256,
        artifacts=ArtifactPaths.for_run(resolved, run_id, experiment_id),
        steps=steps,
    )
