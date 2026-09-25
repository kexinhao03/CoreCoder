from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .models import EffectEvidence, NormalizedMetrics


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite_number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{key} must be finite")
    return result


class ArtifactVerifier:
    @staticmethod
    def verify_completed_experiment(
        metrics_path: Path,
        effects_path: Path,
        *,
        dataset_sha256: str,
        experiment_id: str,
        workflow_version: str,
        definition_hash: str,
    ) -> tuple[NormalizedMetrics, EffectEvidence]:
        metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        if not isinstance(metrics_payload, dict):
            raise TypeError("metrics artifact must contain one JSON object")
        expected = {
            "schema_version": "1.0",
            "dataset_sha256": dataset_sha256,
            "experiment_id": experiment_id,
            "workflow_version": workflow_version,
            "definition_hash": definition_hash,
            "algorithm": "ordinary_least_squares",
        }
        for key, value in expected.items():
            if metrics_payload.get(key) != value:
                raise ValueError(f"metrics {key} does not match workflow identity")
        sample_count = metrics_payload.get("sample_count")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int):
            raise TypeError("sample_count must be an integer")
        parameters = metrics_payload.get("parameters")
        metrics = metrics_payload.get("metrics")
        if not isinstance(parameters, dict) or not isinstance(metrics, dict):
            raise TypeError("parameters and metrics must be JSON objects")
        normalized = NormalizedMetrics(
            dataset_sha256=dataset_sha256,
            definition_hash=definition_hash,
            experiment_id=experiment_id,
            intercept=_finite_number(parameters, "intercept"),
            mse=_finite_number(metrics, "mse"),
            algorithm="ordinary_least_squares",
            r2=_finite_number(metrics, "r2"),
            sample_count=sample_count,
            slope=_finite_number(parameters, "slope"),
            workflow_version=workflow_version,
            metrics_file_sha256=sha256_file(metrics_path),
        )
        expected_metrics = {
            "intercept": (normalized.intercept, 1.0),
            "mse": (normalized.mse, 0.0),
            "r2": (normalized.r2, 1.0),
            "slope": (normalized.slope, 2.0),
        }
        if normalized.sample_count != 10 or any(
            not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12)
            for actual, expected in expected_metrics.values()
        ):
            raise ValueError("metrics do not match the fixed fixture contract")

        parsed_effects: list[dict[str, Any]] = []
        for line in effects_path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError("effect marker must be a JSON object")
            parsed_effects.append(value)
        marker = {"experiment_id": experiment_id, "definition_hash": definition_hash}
        if any(effect != marker for effect in parsed_effects):
            raise ValueError("effect marker does not match workflow identity")
        if len(parsed_effects) != 1:
            raise ValueError("completed experiment must have exactly one effect marker")
        effect = EffectEvidence(
            effect_count=len(parsed_effects),
            duplicate_effect=len(parsed_effects) > 1,
            effects_file_sha256=sha256_file(effects_path),
        )
        return normalized, effect
