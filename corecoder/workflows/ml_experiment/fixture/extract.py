from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def _finite_number(payload: dict[str, object], key: str) -> None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key} must be numeric")
    if not math.isfinite(float(value)):
        raise ValueError(f"{key} must be finite")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--workflow-version", required=True)
    parser.add_argument("--definition-hash", required=True)
    arguments = parser.parse_args()

    raw = arguments.metrics.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("metrics artifact must contain one JSON object")
    expected = {
        "algorithm": "ordinary_least_squares",
        "dataset_sha256": arguments.dataset_sha256,
        "definition_hash": arguments.definition_hash,
        "experiment_id": arguments.experiment_id,
        "schema_version": "1.0",
        "workflow_version": arguments.workflow_version,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"metrics {key} does not match workflow identity")
    if (
        isinstance(payload.get("sample_count"), bool)
        or not isinstance(payload.get("sample_count"), int)
        or payload["sample_count"] < 2
    ):
        raise ValueError("sample_count must be an integer of at least two")
    parameters = payload.get("parameters")
    metrics = payload.get("metrics")
    if not isinstance(parameters, dict) or not isinstance(metrics, dict):
        raise TypeError("parameters and metrics must be JSON objects")
    for container, key in (
        (parameters, "intercept"),
        (parameters, "slope"),
        (metrics, "mse"),
        (metrics, "r2"),
    ):
        _finite_number(container, key)
    normalized_metrics = {
        "algorithm": payload["algorithm"],
        "dataset_sha256": payload["dataset_sha256"],
        "definition_hash": payload["definition_hash"],
        "experiment_id": payload["experiment_id"],
        "intercept": parameters["intercept"],
        "mse": metrics["mse"],
        "r2": metrics["r2"],
        "sample_count": payload["sample_count"],
        "slope": parameters["slope"],
        "workflow_version": payload["workflow_version"],
    }
    expected_metrics = {
        "intercept": 1.0,
        "mse": 0.0,
        "r2": 1.0,
        "slope": 2.0,
    }
    if payload["sample_count"] != 10 or any(
        not math.isclose(
            float(normalized_metrics[key]), expected, rel_tol=0.0, abs_tol=1e-12
        )
        for key, expected in expected_metrics.items()
    ):
        raise ValueError("metrics do not match the fixed fixture contract")
    print(
        json.dumps(
            {
                "artifact_valid": True,
                "metrics_file_sha256": hashlib.sha256(raw).hexdigest(),
                "normalized_metrics": normalized_metrics,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
