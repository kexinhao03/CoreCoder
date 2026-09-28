from dataclasses import asdict

from corecoder import evals
from corecoder.evals.models import EvaluationCase, EvaluationConfig, FaultSchedule
from corecoder.evals.suites.ml_workflow import ml_workflow_configurations
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


def test_configurations_name_disabled_controls_and_preserve_attempt_limits():
    for configurations, attempts in (
        (phase3_configurations(), 3),
        (ml_workflow_configurations(), 2),
    ):
        assert [asdict(config) for config in configurations] == [
            {
                "id": "no_retry_no_recovery",
                "max_attempts": 1,
                "auto_retry": False,
                "recovery_enabled": False,
            },
            {
                "id": "full",
                "max_attempts": attempts,
                "auto_retry": True,
                "recovery_enabled": True,
            },
            {
                "id": "no_recovery",
                "max_attempts": attempts,
                "auto_retry": True,
                "recovery_enabled": False,
            },
        ]


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
