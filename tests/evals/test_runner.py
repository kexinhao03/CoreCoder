from corecoder.evals.models import EvaluationCase, FaultSchedule
from corecoder.evals.runner import EvaluationRunner
from corecoder.evals.suites.phase3 import phase3_configurations, phase3_suite


def _by_config(results):
    return {result.config_id: result for result in results}


def test_runner_uses_same_case_input_and_real_runtime_for_every_configuration(tmp_path):
    case = phase3_suite()[0]

    results = EvaluationRunner(tmp_path, phase3_configurations()).run((case,))

    assert len(results) == 9
    assert {result.input_id for result in results} == {"normal_success-v1"}
    assert all(result.error is None and result.passed is True for result in results)
    assert all(result.runtime_summary["tool_calls"] for result in results)
    assert all(result.runtime_summary["events"] for result in results)
    assert all(result.metrics_snapshot["final_status"] == "succeeded" for result in results)


def test_retry_configuration_changes_attempts_without_changing_input(tmp_path):
    case = phase3_suite()[1]

    results = EvaluationRunner(tmp_path, phase3_configurations()).run((case,))
    first_repetition = _by_config([result for result in results if result.repetition == 1])

    assert first_repetition["no_retry_no_recovery"].task_succeeded is False
    assert first_repetition["no_retry_no_recovery"].metrics_snapshot["retry_count"] == 0
    assert first_repetition["full"].task_succeeded is True
    assert first_repetition["full"].metrics_snapshot["retry_count"] == 1
    assert first_repetition["no_recovery"].task_succeeded is True
    assert first_repetition["no_recovery"].metrics_snapshot["retry_count"] == 1
    assert len({result.input_id for result in first_repetition.values()}) == 1


def test_recovery_ablation_diverges_after_process_loss_and_repeat_resume_is_inert(
    tmp_path,
):
    case = phase3_suite()[7]

    results = EvaluationRunner(tmp_path, phase3_configurations()).run((case,))
    first_repetition = _by_config([result for result in results if result.repetition == 1])

    assert first_repetition["no_retry_no_recovery"].recovery_succeeded is False
    assert first_repetition["no_recovery"].recovery_succeeded is False
    full = first_repetition["full"]
    assert full.recovery_succeeded is True
    assert full.effect_observations == {
        "duplicate_effects": 0,
        "effect_count": 1,
        "events_unchanged_after_second_resume": True,
        "fault_triggered": True,
        "resume_count": 2,
        "tool_calls_after_first_resume": 2,
        "tool_calls_after_second_resume": 2,
    }
    assert full.metrics_snapshot["retry_count"] == 1


def test_runner_records_unknown_scenario_error_and_continues(tmp_path):
    broken = EvaluationCase(
        "BAD", "unknown", "missing_scenario", "missing-v1", FaultSchedule("none"), 1
    )
    good = EvaluationCase(
        "OK", "success", "normal_success", "normal_success-v1", FaultSchedule("none"), 1
    )

    results = EvaluationRunner(
        tmp_path, (phase3_configurations()[0],)
    ).run((broken, good))

    assert "unknown evaluation scenario" in results[0].error
    assert results[0].passed is None
    assert results[1].passed is True


def test_complete_suite_executes_ninety_real_runtime_repetitions(tmp_path):
    results = EvaluationRunner(tmp_path, phase3_configurations()).run(phase3_suite())

    assert len(results) == 90
    assert all(result.error is None for result in results)
    assert all(result.passed is True for result in results)
    assert all(result.runtime_summary["tool_calls"] for result in results)
    assert all(result.trace_integrity["sequence_contiguous"] is True for result in results)

    first = {
        (result.case_id, result.config_id): result
        for result in results
        if result.repetition == 1
    }
    assert first[("E03", "no_retry_no_recovery")].metrics_snapshot["retry_count"] == 0
    assert first[("E03", "full")].metrics_snapshot["retry_count"] == 2
    assert first[("E04", "full")].metrics_snapshot["timeout_count"] == 3
    assert first[("E05", "full")].metrics_snapshot["final_status"] == "cancelled"
    assert first[("E06", "full")].task_succeeded is True
    assert first[("E06", "no_recovery")].task_succeeded is False
    assert first[("E07", "full")].effect_observations["effect_count"] == 1
    assert first[("E07", "full")].effect_observations["duplicate_effects"] == 0
    assert first[("E07", "full")].task_succeeded is False
    assert first[("E07", "full")].recovery_succeeded is False
    assert first[("E07", "full")].metrics_snapshot["final_status"] == "recoverable"
    assert first[("E09", "full")].effect_observations["effect_count"] == 1
    assert first[("E09", "full")].effect_observations["duplicate_effects"] == 0
    assert first[("E09", "full")].task_succeeded is False
    assert first[("E09", "full")].recovery_succeeded is False
    assert first[("E09", "full")].metrics_snapshot["final_status"] == "recoverable"
    assert first[("E10", "full")].metrics_snapshot["final_status"] == "cancelled"
    assert all(
        result.effect_observations["fault_triggered"] is True
        for result in results
        if result.scenario != "normal_success"
    )
