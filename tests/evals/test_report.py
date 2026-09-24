import json
from dataclasses import replace

from corecoder.evals.report import write_markdown, write_raw_result
from corecoder.evals.runner import EvaluationRunner
from corecoder.evals.suites.phase3 import phase3_configurations, phase3_suite


def test_markdown_renderer_uses_raw_json_without_store(tmp_path):
    results = EvaluationRunner(tmp_path / "runs", phase3_configurations()).run(
        (phase3_suite()[0],)
    )
    raw_path = write_raw_result(results, tmp_path)
    markdown = write_markdown(raw_path, tmp_path).read_text()

    assert "E01" in markdown and "full" in markdown
    assert "Token/Cost: not available" in markdown
    assert "9 / 9" in markdown


def test_raw_report_contains_reproducible_results_and_null_zero_denominators(tmp_path):
    results = EvaluationRunner(tmp_path / "runs", phase3_configurations()).run(
        (phase3_suite()[0],)
    )

    raw_path = write_raw_result(results, tmp_path)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    assert raw["schema_version"] == "reliagent.eval.v1"
    assert raw["summary"]["total_repetitions"] == 9
    assert raw["summary"]["contract_passed_repetitions"] == 9
    assert raw["summary"]["by_configuration"]["full"] == {
        "duplicate_side_effect_rate": None,
        "error_count": 0,
        "contract_passed_repetitions": 3,
        "recovery_success_rate": None,
        "repetitions": 3,
        "task_success_rate": 1.0,
        "trace_complete_rate": 1.0,
    }
    assert len(raw["results"]) == 9
    assert raw["results"][0]["case_description"] == "normal success"
    assert raw["results"][0]["scenario"] == "normal_success"


def test_report_counts_evaluation_errors_in_rate_denominators(tmp_path):
    results = EvaluationRunner(tmp_path / "runs", phase3_configurations()).run(
        (phase3_suite()[0],)
    )
    failed = replace(
        results[0],
        passed=None,
        task_succeeded=None,
        error="injected evaluator error",
        runtime_summary=None,
        trace_integrity=None,
        metrics_snapshot=None,
        effect_observations=None,
        assertions=None,
    )

    raw_path = write_raw_result([failed, *results[1:]], tmp_path)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    baseline = raw["summary"]["by_configuration"]["baseline"]

    assert baseline["task_success_rate"] == 2 / 3
    assert baseline["trace_complete_rate"] == 2 / 3
    assert baseline["error_count"] == 1
    assert baseline["duplicate_side_effect_rate"] is None
