from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from corecoder.evals.report import render_markdown
from corecoder.evals.suites.ml_workflow import ml_workflow_configurations, ml_workflow_suite
from corecoder.evals.suites.phase3 import phase3_configurations, phase3_suite

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence" / "reliagent" / "df76ce3"
EVALUATED_COMMIT = "df76ce390796a59636300b789682d9a63312f9fd"
SUPERSEDED_EVIDENCE = ROOT / "evidence" / "reliagent" / "980cd5d"
SUPERSEDED_EVALUATED_COMMIT = "980cd5dea37146d499e14cdab96aea7bd42083ac"
LEGACY_EVIDENCE = ROOT / "evidence" / "reliagent" / "c724f8a"
LEGACY_EVALUATED_COMMIT = "c724f8a4bc45c8ad900040f46fd306d78039279a"


def test_current_evidence_manifest_binds_exact_runtime_and_generated_files():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "reliagent.evidence.v1"
    assert manifest["evaluated_commit"] == EVALUATED_COMMIT
    assert manifest["generated_with"] == [
        ".venv/bin/python -m corecoder.reliagent_cli eval ml_workflow --output /tmp/reliagent-p0-final.MaEJan/ml_workflow",
        ".venv/bin/python -m corecoder.reliagent_cli eval phase3 --output /tmp/reliagent-p0-final.MaEJan/phase3",
    ]
    assert set(manifest["sha256"]) == {
        "ml-workflow-results.json", "ml-workflow-report.md",
        "phase3-results.json", "phase3-report.md",
    }
    for filename, expected in manifest["sha256"].items():
        assert hashlib.sha256((EVIDENCE / filename).read_bytes()).hexdigest() == expected


def _assert_trace_complete(runtime):
    events = runtime["events"]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))

    def has(event_type, identity_key=None, identity=None):
        return any(
            event["type"] == event_type
            and (identity_key is None or event["payload"].get(identity_key) == identity)
            for event in events
        )

    run = runtime["run"]
    assert has("run.created")
    if run["started_at"] is not None:
        assert has("run.started")
    run_endings = {"succeeded": "completed", "failed": "failed", "cancelled": "cancelled"}
    if run["status"] in run_endings:
        assert has(f"run.{run_endings[run['status']]}")
    for step in runtime["steps"]:
        assert has("step.created", "step_id", step["id"])
        if step["started_at"] is not None:
            assert has("step.started", "step_id", step["id"])
        ending = {**run_endings, "skipped": "skipped"}.get(step["status"])
        if ending:
            assert has(f"step.{ending}", "step_id", step["id"])
    for call in runtime["tool_calls"]:
        assert has("tool.created", "tool_call_id", call["id"])
        if call["started_at"] is not None:
            assert has("tool.started", "tool_call_id", call["id"])
        endings = {
            "succeeded": {"tool.completed", "tool.reconciled"},
            "failed": {"tool.failed"}, "cancelled": {"tool.cancelled"},
            "timed_out": {"tool.timed_out"}, "interrupted": {"tool.interrupted"},
        }.get(call["status"])
        if endings:
            resolutions = {"succeeded": "confirmed_succeeded", "failed": "confirmed_failed", "cancelled": "abandon"}
            assert any(
                event["payload"].get("tool_call_id") == call["id"]
                and (
                    event["type"] in endings
                    or (event["type"] == "recovery.resolved"
                        and event["payload"].get("resolution") == resolutions.get(call["status"]))
                    or (call["status"] == "cancelled" and event["type"] == "approval.resolved"
                        and event["payload"].get("decision") == "deny")
                )
                for event in events
            )
    for approval in runtime["approvals"]:
        assert has("approval.requested", "approval_id", approval["id"])
        if approval["status"] != "pending":
            assert has("approval.resolved", "approval_id", approval["id"])


@pytest.mark.parametrize("prefix,suite,configurations,total", [
    ("ml-workflow", ml_workflow_suite, ml_workflow_configurations, 24),
    ("phase3", phase3_suite, phase3_configurations, 90),
])
def test_current_evidence_recomputes_configuration_input_trace_and_effect_contracts(
    prefix, suite, configurations, total,
):
    payload = json.loads((EVIDENCE / f"{prefix}-results.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "reliagent.eval.v1"
    results = payload["results"]
    configs = {config.id for config in configurations()}
    assert configs == {"no_retry_no_recovery", "full", "no_recovery"}
    assert {result["config_id"] for result in results} == configs
    cases = {case.id: case for case in suite()}
    expected_runs = {
        (case.id, config_id, repetition)
        for case in cases.values() for config_id in configs
        for repetition in range(1, case.repeat_count + 1)
    }
    assert len(results) == len(expected_runs) == total
    assert {(r["case_id"], r["config_id"], r["repetition"]) for r in results} == expected_runs
    for result in results:
        case = cases[result["case_id"]]
        assert result["input_id"] == case.input_id
        assert result["scenario"] == case.scenario
        assert result["fault_schedule"] == asdict(case.fault_schedule)
        assert result["error"] is None
        assert result["assertions"] and all(result["assertions"].values())
        assert result["passed"] is True
        _assert_trace_complete(result["runtime_summary"])
        assert result["trace_integrity"] == {"sequence_contiguous": True, "missing": []}
        effects = result["effect_observations"]
        assert effects["duplicate_effects"] == max(0, effects["effect_count"] - 1) == 0
        if prefix == "ml-workflow" and case.id in {"ML02", "ML03", "ML04", "ML05"}:
            assert effects["process_exit_code"] == (86 if case.id == "ML02" else 87)
            if result["config_id"] != "full":
                assert result["runtime_summary"]["run"]["status"] == "recoverable"
                assert result["recovery_succeeded"] is False
    summary = payload["summary"]
    assert summary["total_repetitions"] == len(results)
    assert summary["contract_passed_repetitions"] == sum(r["passed"] is True for r in results) == total
    assert summary["error_count"] == sum(r["error"] is not None for r in results) == 0
    assert set(summary["by_configuration"]) == configs
    for config_id, values in summary["by_configuration"].items():
        selected = [r for r in results if r["config_id"] == config_id]
        assert values["repetitions"] == values["contract_passed_repetitions"] == len(selected)
        assert values["trace_complete_rate"] == 1.0
        assert values["duplicate_side_effect_rate"] == 0.0
        assert values["error_count"] == 0
        assert values["task_success_rate"] == sum(r["task_succeeded"] is True for r in selected) / len(selected)
    assert (EVIDENCE / f"{prefix}-report.md").read_text(encoding="utf-8") == render_markdown(payload)


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


def test_superseded_evidence_snapshot_retains_integrity():
    manifest = json.loads(
        (SUPERSEDED_EVIDENCE / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "reliagent.evidence.v1"
    assert manifest["evaluated_commit"] == SUPERSEDED_EVALUATED_COMMIT
    for filename, expected_sha256 in manifest["sha256"].items():
        assert hashlib.sha256(
            (SUPERSEDED_EVIDENCE / filename).read_bytes()
        ).hexdigest() == expected_sha256
    for prefix, total in (("ml-workflow", 24), ("phase3", 90)):
        payload = json.loads(
            (SUPERSEDED_EVIDENCE / f"{prefix}-results.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["summary"]["total_repetitions"] == total
        assert payload["summary"]["contract_passed_repetitions"] == total
        assert {result["config_id"] for result in payload["results"]} == {
            "no_retry_no_recovery",
            "full",
            "no_recovery",
        }


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
