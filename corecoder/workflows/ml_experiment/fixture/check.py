from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

_FIXTURE_DIR = Path(__file__).resolve().parent
_REGISTERED_DATASET = _FIXTURE_DIR / "data.csv"
_REGISTERED_DATASET_SHA256 = (
    "33383eeafabf9ba13fc7fed3075550d72846574c56fd1ac5e0d1afe1067aed20"
)


def _within(workspace: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(workspace)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    arguments = parser.parse_args()

    workspace = arguments.workspace.resolve()
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    if arguments.dataset.resolve() != _REGISTERED_DATASET:
        raise ValueError("dataset is not the registered workflow fixture")
    if arguments.dataset_sha256 != _REGISTERED_DATASET_SHA256:
        raise ValueError("dataset SHA-256 is not the registered workflow digest")
    if hashlib.sha256(arguments.dataset.read_bytes()).hexdigest() != arguments.dataset_sha256:
        raise ValueError("dataset SHA-256 does not match")
    checked_paths = tuple(
        path.resolve()
        for path in (arguments.database, arguments.artifact_dir, arguments.report_dir)
    )
    if not all(_within(workspace, path) for path in checked_paths):
        raise ValueError("workflow path escapes workspace")
    if any(path == _FIXTURE_DIR or _FIXTURE_DIR in path.parents for path in checked_paths):
        raise ValueError("workflow outputs must not overwrite the fixture")
    print(
        json.dumps(
            {
                "dataset_sha256": arguments.dataset_sha256,
                "environment_valid": True,
                "workspace": str(workspace),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
