from corecoder.evals.runner import EvaluationRunner
from corecoder.evals.suites.ml_workflow import (
    ml_workflow_configurations,
    ml_workflow_suite,
)


def test_ml_workflow_matrix_executes_eight_real_runtime_cases(tmp_path):
    suite = ml_workflow_suite()
    results = EvaluationRunner(tmp_path, ml_workflow_configurations()).run(suite)

    assert len(suite) == 8
    assert len(results) == 24
    assert all(result.error is None for result in results)
    assert all(result.passed is True for result in results)
    assert all(result.runtime_summary["tool_calls"] for result in results)
    for case in suite:
        selected = [result for result in results if result.case_id == case.id]
        assert {result.input_id for result in selected} == {case.input_id}

    first = {(result.case_id, result.config_id): result for result in results}
    assert first[("ML02", "full")].recovery_succeeded is True
    assert first[("ML02", "baseline")].recovery_succeeded is False
    assert first[("ML04", "full")].task_succeeded is True
    assert first[("ML05", "full")].task_succeeded is False
    assert first[("ML07", "full")].metrics_snapshot["final_status"] == "cancelled"
    assert first[("ML08", "full")].assertions["scenario_contract"] is True
