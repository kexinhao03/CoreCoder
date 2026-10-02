from __future__ import annotations

import json
import sqlite3

import pytest

from corecoder.runtime import ApprovalDecision
from corecoder.workflows.ml_experiment.report import MLExperimentReportService
from corecoder.workflows.ml_experiment.workflow import MLExperimentWorkflow


def _successful_workflow(tmp_path):
    workflow = MLExperimentWorkflow(tmp_path)
    waiting = workflow.create()
    experiment = workflow.store.list_tool_calls(waiting.id)[1]
    approval = workflow.store.get_approval_for_tool_call(experiment.id)
    workflow.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    workflow.resume(waiting.id)
    return workflow, waiting.id


def test_report_uses_persisted_results_and_writes_deterministic_files(tmp_path):
    workflow, run_id = _successful_workflow(tmp_path)
    service = MLExperimentReportService(workflow)
    events_before = workflow.store.list_events(run_id)

    raw = service.build(run_id)
    first_paths = service.write(raw)
    first_bytes = tuple(path.read_bytes() for path in first_paths)
    second_paths = service.write(service.build(run_id))

    assert first_paths == second_paths
    assert tuple(path.read_bytes() for path in second_paths) == first_bytes
    assert workflow.store.list_events(run_id) == events_before
    assert raw["report_schema_version"] == "1.0"
    assert raw["run"]["status"] == "succeeded"
    assert raw["experiment"]["normalized_metrics"]["slope"] == 2.0
    assert raw["experiment"]["effect_count"] == 1
    assert raw["experiment"]["duplicate_effect"] is False
    assert raw["trace_integrity"]["missing"] == []
    assert raw["runtime_metrics"]["retry_count"] == 0
    assert raw["token_cost"]["available"] is False
    assert raw["git"]["commit"]
    assert isinstance(raw["git"]["dirty"], bool)
    assert raw["conclusion_boundaries"]["effect_marker_scope"] == "fixture_only"
    experiment_call = next(
        call for call in raw["tool_calls"] if call["tool_name"].endswith("run_experiment")
    )
    approval = raw["approval_evidence"][0]
    definition = workflow.definition(run_id)
    assert approval["tool_call_id"] == experiment_call["id"]
    assert approval["attempt"] == experiment_call["attempt"] == 1
    assert approval["experiment_id"] == definition.experiment_id
    assert approval["definition_hash"] == definition.step("run_experiment").definition_hash
    assert json.loads(first_paths[0].read_text()) == raw


def test_report_rejects_current_artifact_hash_mismatch(tmp_path):
    workflow, run_id = _successful_workflow(tmp_path)
    artifacts = workflow.definition(run_id).artifacts
    payload = json.loads(artifacts.metrics_file.read_text())
    payload["parameters"]["slope"] = 999.0
    artifacts.metrics_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Artifact Integrity Failure"):
        MLExperimentReportService(workflow).build(run_id)


@pytest.mark.parametrize("column", ["attempt", "experiment_id", "definition_hash"])
def test_report_rejects_approval_identity_mismatch(tmp_path, column):
    workflow, run_id = _successful_workflow(tmp_path)
    with sqlite3.connect(workflow.store.path) as connection:
        connection.execute(f"UPDATE approvals SET {column} = ?", ("wrong",))

    with pytest.raises(ValueError, match="Approval Integrity Failure"):
        MLExperimentReportService(workflow).build(run_id)


def test_report_rejects_noncontiguous_trace(tmp_path):
    workflow, run_id = _successful_workflow(tmp_path)
    with sqlite3.connect(workflow.store.path) as connection:
        connection.execute(
            "DELETE FROM events WHERE run_id = ? AND type = 'run.created'", (run_id,)
        )

    with pytest.raises(ValueError, match="Trace Integrity Failure"):
        MLExperimentReportService(workflow).build(run_id)
