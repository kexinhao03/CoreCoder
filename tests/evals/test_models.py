from dataclasses import asdict

from corecoder import evals
from corecoder.evals.models import EvaluationCase, EvaluationConfig, FaultSchedule
from corecoder.evals.suites.phase3 import phase3_configurations


def test_evaluation_models_are_serializable_and_keep_fault_schedule():
    fault = FaultSchedule("after_tool_persist")
    case = EvaluationCase("E01", "success", "normal_success", "input-v1", fault, 3)
    config = EvaluationConfig(
        "full", max_attempts=3, auto_retry=True, recovery_enabled=True
    )

    assert asdict(case)["fault_schedule"]["point"] == "after_tool_persist"
    assert case.input_id == "input-v1"
    assert config.recovery_enabled is True


def test_phase3_configurations_have_one_control_difference_per_ablation():
    baseline, full, no_recovery = phase3_configurations()

    assert [config.id for config in (baseline, full, no_recovery)] == [
        "baseline",
        "full",
        "no_recovery",
    ]
    assert (baseline.auto_retry, baseline.recovery_enabled) == (False, False)
    assert (full.auto_retry, full.recovery_enabled) == (True, True)
    assert (no_recovery.auto_retry, no_recovery.recovery_enabled) == (True, False)
    assert full.max_attempts == no_recovery.max_attempts == 3


def test_evaluation_public_api_exports_executable_harness():
    assert {
        "EvaluationRunner",
        "phase3_configurations",
        "phase3_suite",
        "write_raw_result",
        "write_markdown",
    } <= set(evals.__all__)


def test_evaluation_result_keeps_runtime_evidence_snapshots():
    from corecoder.evals.models import EvaluationResult

    result = EvaluationResult(
        "E01", "success", "normal_success", "full", 1, "input-v1",
        FaultSchedule("none"), True, True, None, None,
        {"status": "succeeded"}, {"sequence_contiguous": True},
        {"tool_success_rate": 1.0}, {"markers": []}, {"task": True},
    )

    assert asdict(result)["trace_integrity"]["sequence_contiguous"] is True
    assert result.passed is True
