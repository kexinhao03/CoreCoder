import pytest

from corecoder.evals.runner import EvaluationRunner
from corecoder.evals.suites.ml_workflow import (
    ml_workflow_configurations,
    ml_workflow_suite,
)


@pytest.mark.parametrize("case_index", [1, 2])
def test_process_loss_without_retry_or_recovery_remains_recoverable(tmp_path, case_index):
    [result] = EvaluationRunner(tmp_path, ml_workflow_configurations()[:1]).run(
        (ml_workflow_suite()[case_index],)
    )

    assert result.error is None
    assert result.metrics_snapshot["final_status"] == "recoverable"
    assert all(
        call["execution_kind"] == "subprocess"
        for call in result.runtime_summary["tool_calls"]
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
    assert first[("ML02", "no_retry_no_recovery")].recovery_succeeded is False
    assert first[("ML04", "full")].task_succeeded is True
    assert first[("ML05", "full")].task_succeeded is False
    assert first[("ML07", "full")].metrics_snapshot["final_status"] == "failed"
    assert first[("ML08", "full")].assertions["scenario_contract"] is True
    for result in results:
        assert result.assertions["configuration_applied"] is True
        if result.case_id in {"ML02", "ML03", "ML04", "ML05"}:
            assert result.effect_observations["process_exit_code"] in {86, 87}
            assert result.assertions["real_process_exit_observed"] is True
            events = result.runtime_summary["events"]
            event_types = [event["type"] for event in events]
            assert "tool.interrupted" in event_types
            assert any(
                event["type"] == "tool.interrupted"
                and event["payload"]["reason"] == "process_lost"
                for event in events
            )
            calls = result.runtime_summary["tool_calls"]
            assert all(
                call["result_summary"] not in {"PROCESS_EXIT_86", "PROCESS_EXIT_87"}
                for call in calls
            )
            if result.config_id != "full":
                assert result.metrics_snapshot["final_status"] == "recoverable"
                assert result.recovery_succeeded is False
                assert any(call["status"] == "interrupted" for call in calls)
                assert all(call["attempt"] == 1 for call in calls)
                assert "recovery.resolved" not in event_types
                assert "tool.retry_scheduled" not in event_types
                assert "tool.reconciled" not in event_types
                assert "run.failed" not in event_types
            elif result.case_id == "ML02":
                assert "recovery.resolved" in event_types
                assert "tool.retry_scheduled" in event_types
            elif result.case_id == "ML04":
                assert "tool.reconciled" in event_types
                assert "run.resumed" in event_types
            elif result.case_id == "ML05":
                assert "reconciliation.unresolved" in event_types
