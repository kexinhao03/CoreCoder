from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effects", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--definition-hash", required=True)
    arguments = parser.parse_args()

    raw = arguments.effects.read_bytes()
    effects = [json.loads(line) for line in raw.decode().splitlines()]
    marker = {
        "definition_hash": arguments.definition_hash,
        "experiment_id": arguments.experiment_id,
    }
    if any(effect != marker for effect in effects):
        raise ValueError("effect marker does not match workflow identity")
    if len(effects) != 1:
        raise ValueError("completed experiment must have exactly one effect marker")
    print(
        json.dumps(
            {
                "duplicate_effect": len(effects) > 1,
                "effect_count": len(effects),
                "effects_file_sha256": hashlib.sha256(raw).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
