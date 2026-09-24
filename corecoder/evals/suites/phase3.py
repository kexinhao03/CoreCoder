"""The fixed Priority 4 configurations and executable scenario contracts."""

from corecoder.evals.models import EvaluationCase, EvaluationConfig, FaultSchedule

_CASES = (
    ("normal success", "normal_success", "none"),
    ("temporary error then success", "transient_then_success", "before_effect"),
    ("retry exhaustion", "retry_exhausted", "before_effect"),
    ("timeout", "timeout", "after_tool_persist"),
    ("approval denial", "approval_denied", "while_waiting_approval"),
    ("crash after Step completion", "crash_after_step", "after_step_complete"),
    ("high-risk operation interrupted", "high_risk_interrupted", "after_effect"),
    ("repeated resume", "repeated_resume", "process_lost"),
    ("effect completed before persistence failure", "effect_persist_failure", "before_completion_persist"),
    ("cancel Run", "cancel_run", "while_running"),
)


def phase3_configurations() -> tuple[EvaluationConfig, ...]:
    return (
        EvaluationConfig("baseline", 1, False, False),
        EvaluationConfig("full", 3, True, True),
        EvaluationConfig("no_recovery", 3, True, False),
    )


def phase3_suite() -> tuple[EvaluationCase, ...]:
    return tuple(
        EvaluationCase(
            f"E{index:02d}",
            description,
            scenario,
            f"{scenario}-v1",
            FaultSchedule(point),
            3,
        )
        for index, (description, scenario, point) in enumerate(_CASES, start=1)
    )
