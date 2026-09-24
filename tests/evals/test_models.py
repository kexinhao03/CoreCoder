from dataclasses import asdict

from corecoder.evals.models import EvaluationCase, EvaluationConfig, FaultSchedule


def test_evaluation_models_are_serializable_and_keep_fault_schedule():
    fault = FaultSchedule("after_tool_persist")
    case = EvaluationCase("E01", "success", "task.json", fault, 3)
    config = EvaluationConfig("full", persistence_enabled=True, recovery_enabled=True)

    assert asdict(case)["fault_schedule"]["point"] == "after_tool_persist"
    assert config.recovery_enabled is True


def test_evaluation_result_keeps_runtime_evidence_snapshots():
    from corecoder.evals.models import EvaluationResult

    result = EvaluationResult("E01", "full", 1, FaultSchedule("none"), True, None, None,
                              {"status": "succeeded"}, {"sequence_contiguous": True},
                              {"tool_success_rate": 1.0}, {"markers": []}, {"task": True})

    assert asdict(result)["trace_integrity"]["sequence_contiguous"] is True
