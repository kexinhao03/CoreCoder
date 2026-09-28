from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_EVIDENCE = ROOT / "evidence" / "reliagent" / "c724f8a"
LEGACY_EVALUATED_COMMIT = "c724f8a4bc45c8ad900040f46fd306d78039279a"


def test_legacy_evidence_snapshot_retains_integrity_and_historical_results():
    # This immutable snapshot predates honest ablation ids and orphan discovery;
    # current behavior is exercised by tests/evals, not claimed from this archive.
    manifest = json.loads((LEGACY_EVIDENCE / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "reliagent.evidence.v1"
    assert manifest["evaluated_commit"] == LEGACY_EVALUATED_COMMIT
    for filename, expected_sha256 in manifest["sha256"].items():
        payload = (LEGACY_EVIDENCE / filename).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256

    ml = json.loads((LEGACY_EVIDENCE / "ml-workflow-results.json").read_text(encoding="utf-8"))
    phase3 = json.loads((LEGACY_EVIDENCE / "phase3-results.json").read_text(encoding="utf-8"))
    ml_results = ml["results"]
    phase3_results = phase3["results"]

    assert ml["summary"]["contract_passed_repetitions"] == 24
    assert ml["summary"]["total_repetitions"] == 24
    assert phase3["summary"]["contract_passed_repetitions"] == 90
    assert phase3["summary"]["total_repetitions"] == 90
    for payload, results in ((ml, ml_results), (phase3, phase3_results)):
        assert {result["config_id"] for result in results} == {
            "baseline",
            "full",
            "no_recovery",
        }
        assert all(
            len({result["input_id"] for result in results if result["case_id"] == case_id})
            == 1
            for case_id in {result["case_id"] for result in results}
        )
        assert all(
            result["trace_integrity"]["sequence_contiguous"]
            and not result["trace_integrity"]["missing"]
            for result in results
        )
        assert sum(
            result["effect_observations"]["duplicate_effects"] for result in results
        ) == 0
        assert payload["summary"]["by_configuration"]["full"][
            "recovery_success_rate"
        ] == 0.5
    assert {
        result["effect_observations"]["process_exit_code"]
        for result in ml_results
        if result["effect_observations"]["process_exit_code"] is not None
    } == {86, 87}
    assert all(
        result["metrics_snapshot"]["final_status"] == "failed"
        for result in ml_results
        if result["config_id"] == "baseline"
        and result["case_id"] in {"ML02", "ML03", "ML04", "ML05"}
    )


def test_demo_runs_real_after_effect_recovery_without_duplicate_effect(tmp_path):
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "reliagent_ml_demo.sh"), str(tmp_path)],
        cwd=ROOT,
        env={**os.environ, "PYTHON_BIN": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["process_exit_code"] == 87
    assert summary["status"] == "succeeded"
    assert summary["effect_count"] == 1
    assert summary["duplicate_effect"] is False
    assert summary["trace_missing"] == []
    assert summary["trace_sequence_contiguous"] is True
    assert Path(summary["raw_json"]).is_file()
    assert Path(summary["markdown"]).is_file()
