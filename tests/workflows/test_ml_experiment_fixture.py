from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from corecoder.workflows.ml_experiment import (
    CONFIG_VERSION,
    DATASET_SHA256,
    MODEL,
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    ArtifactPaths,
    ArtifactVerifier,
    build_definition,
    resolve_workspace,
)

FIXTURE_DIR = (
    Path(__file__).parents[2]
    / "corecoder"
    / "workflows"
    / "ml_experiment"
    / "fixture"
)
DATASET = FIXTURE_DIR / "data.csv"
EXPECTED_DATASET_SHA256 = (
    "33383eeafabf9ba13fc7fed3075550d72846574c56fd1ac5e0d1afe1067aed20"
)


def _run(script: str, *arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FIXTURE_DIR / script), *(str(item) for item in arguments)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_fixed_dataset_and_definition_are_deterministic(tmp_path: Path) -> None:
    assert hashlib.sha256(DATASET.read_bytes()).hexdigest() == EXPECTED_DATASET_SHA256
    assert DATASET_SHA256 == EXPECTED_DATASET_SHA256

    definition = build_definition(tmp_path, "run-123")

    assert definition.workflow_name == WORKFLOW_NAME == "ml_experiment"
    assert definition.workflow_version == WORKFLOW_VERSION == "1"
    assert definition.model == MODEL == "deterministic"
    assert definition.config_version == CONFIG_VERSION == "ml-experiment-v1"
    assert [step.step_key for step in definition.steps] == [
        "environment_check",
        "run_experiment",
        "extract_metrics",
        "verify_effect",
    ]
    assert all(len(step.definition_hash) == 64 for step in definition.steps)
    experiment = definition.step("run_experiment")
    expected_experiment_id = hashlib.sha256(
        f"run-123{WORKFLOW_VERSION}{experiment.definition_hash}".encode()
    ).hexdigest()
    assert definition.experiment_id == expected_experiment_id
    assert definition.artifacts == ArtifactPaths.for_run(
        tmp_path, "run-123", expected_experiment_id
    )
    assert definition.artifacts.database == tmp_path / ".reliagent" / "runtime.sqlite"
    assert definition.artifacts.metrics_file.is_relative_to(tmp_path.resolve())
    assert definition.artifacts.raw_report.is_relative_to(tmp_path.resolve())
    experiment_arguments = definition.arguments_for("run_experiment")
    assert experiment_arguments == (
        "--workspace",
        str(tmp_path.resolve()),
        "--dataset",
        str(DATASET.resolve()),
        "--dataset-sha256",
        EXPECTED_DATASET_SHA256,
        "--experiment-id",
        definition.experiment_id,
        "--workflow-version",
        WORKFLOW_VERSION,
        "--definition-hash",
        experiment.definition_hash,
        "--metrics",
        str(definition.artifacts.metrics_file),
        "--effects",
        str(definition.artifacts.effects_file),
    )


def test_workspace_must_exist_and_be_a_directory(tmp_path: Path) -> None:
    assert resolve_workspace(tmp_path) == tmp_path.resolve()
    with pytest.raises(ValueError, match="existing directory"):
        resolve_workspace(tmp_path / "missing")
    file_path = tmp_path / "file"
    file_path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="existing directory"):
        resolve_workspace(file_path)


def test_train_extract_and_verify_execute_as_real_subprocesses(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    metrics = artifacts / "metrics.json"
    effects = artifacts / "effects.log"
    experiment_id = "experiment-123"
    definition_hash = "d" * 64

    trained = _run(
        "train.py",
        "--workspace",
        tmp_path,
        "--dataset",
        DATASET,
        "--dataset-sha256",
        EXPECTED_DATASET_SHA256,
        "--experiment-id",
        experiment_id,
        "--workflow-version",
        WORKFLOW_VERSION,
        "--definition-hash",
        definition_hash,
        "--metrics",
        metrics,
        "--effects",
        effects,
    )

    assert trained.returncode == 0, trained.stderr
    assert json.loads(trained.stdout) == {
        "definition_hash": definition_hash,
        "experiment_id": experiment_id,
        "metrics": str(metrics.resolve()),
        "status": "succeeded",
    }
    persisted = json.loads(metrics.read_text(encoding="utf-8"))
    assert persisted == {
        "algorithm": "ordinary_least_squares",
        "dataset_sha256": EXPECTED_DATASET_SHA256,
        "definition_hash": definition_hash,
        "experiment_id": experiment_id,
        "metrics": {"mse": 0.0, "r2": 1.0},
        "parameters": {"intercept": 1.0, "slope": 2.0},
        "sample_count": 10,
        "schema_version": "1.0",
        "workflow_version": WORKFLOW_VERSION,
    }
    assert not metrics.with_name("metrics.tmp.json").exists()
    assert [json.loads(line) for line in effects.read_text().splitlines()] == [
        {"definition_hash": definition_hash, "experiment_id": experiment_id}
    ]

    extracted = _run(
        "extract.py",
        "--metrics",
        metrics,
        "--dataset-sha256",
        EXPECTED_DATASET_SHA256,
        "--experiment-id",
        experiment_id,
        "--workflow-version",
        WORKFLOW_VERSION,
        "--definition-hash",
        definition_hash,
    )
    assert extracted.returncode == 0, extracted.stderr
    extracted_payload = json.loads(extracted.stdout)
    assert extracted_payload["artifact_valid"] is True
    assert extracted_payload["normalized_metrics"] == {
        "algorithm": "ordinary_least_squares",
        "dataset_sha256": EXPECTED_DATASET_SHA256,
        "definition_hash": definition_hash,
        "experiment_id": experiment_id,
        "intercept": 1.0,
        "mse": 0.0,
        "r2": 1.0,
        "sample_count": 10,
        "slope": 2.0,
        "workflow_version": WORKFLOW_VERSION,
    }
    assert extracted_payload["metrics_file_sha256"] == hashlib.sha256(
        metrics.read_bytes()
    ).hexdigest()

    verified = _run(
        "verify_effect.py",
        "--effects",
        effects,
        "--experiment-id",
        experiment_id,
        "--definition-hash",
        definition_hash,
    )
    assert verified.returncode == 0, verified.stderr
    verified_payload = json.loads(verified.stdout)
    assert verified_payload["effect_count"] == 1
    assert verified_payload["duplicate_effect"] is False
    assert verified_payload["effects_file_sha256"] == hashlib.sha256(
        effects.read_bytes()
    ).hexdigest()

    normalized, effect = ArtifactVerifier.verify_completed_experiment(
        metrics,
        effects,
        dataset_sha256=EXPECTED_DATASET_SHA256,
        experiment_id=experiment_id,
        workflow_version=WORKFLOW_VERSION,
        definition_hash=definition_hash,
    )
    assert normalized.slope == 2.0
    assert normalized.sample_count == 10
    assert effect.effect_count == 1
    assert effect.duplicate_effect is False


def test_environment_check_requires_registered_fixture_path(tmp_path: Path) -> None:
    valid = _run(
        "check.py",
        "--workspace",
        tmp_path,
        "--dataset",
        DATASET,
        "--dataset-sha256",
        EXPECTED_DATASET_SHA256,
        "--database",
        tmp_path / ".reliagent" / "runtime.sqlite",
        "--artifact-dir",
        tmp_path / "artifacts" / "experiment-123",
        "--report-dir",
        tmp_path / "reports" / "run-123",
    )
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout) == {
        "dataset_sha256": EXPECTED_DATASET_SHA256,
        "environment_valid": True,
        "workspace": str(tmp_path.resolve()),
    }

    copied_dataset = tmp_path / "copied-data.csv"
    copied_dataset.write_bytes(DATASET.read_bytes())
    unregistered = _run(
        "check.py",
        "--workspace",
        tmp_path,
        "--dataset",
        copied_dataset,
        "--dataset-sha256",
        EXPECTED_DATASET_SHA256,
        "--database",
        tmp_path / ".reliagent" / "runtime.sqlite",
        "--artifact-dir",
        tmp_path / "artifacts" / "experiment-123",
        "--report-dir",
        tmp_path / "reports" / "run-123",
    )
    assert unregistered.returncode != 0


def test_malformed_dataset_never_publishes_metrics(tmp_path: Path) -> None:
    dataset = tmp_path / "malformed.csv"
    dataset.write_text("x,y\n0,1\ninvalid,3\n", encoding="utf-8")
    dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
    metrics = tmp_path / "metrics.json"
    effects = tmp_path / "effects.log"

    result = _run(
        "train.py",
        "--workspace",
        tmp_path,
        "--dataset",
        dataset,
        "--dataset-sha256",
        dataset_sha256,
        "--experiment-id",
        "experiment-bad",
        "--workflow-version",
        WORKFLOW_VERSION,
        "--definition-hash",
        "e" * 64,
        "--metrics",
        metrics,
        "--effects",
        effects,
    )

    assert result.returncode != 0
    assert not metrics.exists()
    assert not metrics.with_name("metrics.tmp.json").exists()
    assert not effects.exists()


def test_extractor_rejects_metrics_outside_fixed_fixture_contract(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.json"
    metrics.write_text(
        json.dumps(
            {
                "algorithm": "ordinary_least_squares",
                "dataset_sha256": EXPECTED_DATASET_SHA256,
                "definition_hash": "d" * 64,
                "experiment_id": "experiment-tampered",
                "metrics": {"mse": 0.0, "r2": 1.0},
                "parameters": {"intercept": 1.0, "slope": 999.0},
                "sample_count": 10,
                "schema_version": "1.0",
                "workflow_version": WORKFLOW_VERSION,
            }
        ),
        encoding="utf-8",
    )

    extracted = _run(
        "extract.py",
        "--metrics",
        metrics,
        "--dataset-sha256",
        EXPECTED_DATASET_SHA256,
        "--experiment-id",
        "experiment-tampered",
        "--workflow-version",
        WORKFLOW_VERSION,
        "--definition-hash",
        "d" * 64,
    )

    assert extracted.returncode != 0
