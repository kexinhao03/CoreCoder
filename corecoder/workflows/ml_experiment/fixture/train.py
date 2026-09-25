from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path

_FIXTURE_DIR = Path(__file__).resolve().parent
_REGISTERED_DATASET = _FIXTURE_DIR / "data.csv"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_dataset(path: Path, expected_sha256: str) -> list[tuple[float, float]]:
    if _sha256(path) != expected_sha256:
        raise ValueError("dataset SHA-256 does not match")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["x", "y"]:
            raise ValueError("dataset must contain exactly x,y columns")
        rows = []
        for row in reader:
            x = float(row["x"])
            y = float(row["y"])
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("dataset values must be finite")
            rows.append((x, y))
    if len(rows) < 2:
        raise ValueError("dataset must contain at least two rows")
    return rows


def _ols(rows: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    x_mean = sum(x for x, _ in rows) / len(rows)
    y_mean = sum(y for _, y in rows) / len(rows)
    denominator = sum((x - x_mean) ** 2 for x, _ in rows)
    if denominator == 0:
        raise ValueError("dataset x variance must be non-zero")
    slope = sum((x - x_mean) * (y - y_mean) for x, y in rows) / denominator
    intercept = y_mean - slope * x_mean
    residuals = [y - (intercept + slope * x) for x, y in rows]
    mean_squared_error = sum(value**2 for value in residuals) / len(rows)
    total = sum((y - y_mean) ** 2 for _, y in rows)
    if total == 0:
        raise ValueError("dataset y variance must be non-zero")
    r2 = 1.0 - (sum(value**2 for value in residuals) / total)
    return intercept, slope, mean_squared_error, r2


def _fsync_directory(directory: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_effect(path: Path, marker: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _publish_metrics(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("metrics.tmp.json")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--workflow-version", required=True)
    parser.add_argument("--definition-hash", required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--effects", type=Path, required=True)
    arguments = parser.parse_args()

    workspace = arguments.workspace.resolve()
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    if arguments.dataset.resolve() != _REGISTERED_DATASET:
        raise ValueError("dataset is not the registered workflow fixture")
    for output in (arguments.metrics.resolve(), arguments.effects.resolve()):
        if not output.is_relative_to(workspace):
            raise ValueError("experiment output escapes workspace")
    rows = _read_dataset(arguments.dataset, arguments.dataset_sha256)
    intercept, slope, mean_squared_error, r2 = _ols(rows)
    marker = {
        "definition_hash": arguments.definition_hash,
        "experiment_id": arguments.experiment_id,
    }
    metrics = {
        "algorithm": "ordinary_least_squares",
        "dataset_sha256": arguments.dataset_sha256,
        "definition_hash": arguments.definition_hash,
        "experiment_id": arguments.experiment_id,
        "metrics": {"mse": mean_squared_error, "r2": r2},
        "parameters": {"intercept": intercept, "slope": slope},
        "sample_count": len(rows),
        "schema_version": "1.0",
        "workflow_version": arguments.workflow_version,
    }
    _append_effect(arguments.effects, marker)
    _publish_metrics(arguments.metrics, metrics)
    print(
        json.dumps(
            {
                "definition_hash": arguments.definition_hash,
                "experiment_id": arguments.experiment_id,
                "metrics": str(arguments.metrics.resolve()),
                "status": "succeeded",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
