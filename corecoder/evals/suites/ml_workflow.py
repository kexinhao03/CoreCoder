"""Eight executable deterministic ML workflow evaluation cases."""

from corecoder.evals.models import EvaluationCase, EvaluationConfig, FaultSchedule

_CASES = (
    ("normal approval success", "ml_normal_success", "none"),
    ("environment process loss recovery", "ml_environment_recovery", "environment_after_start"),
    ("experiment after-effect interruption", "ml_experiment_after_effect", "experiment_after_effect"),
    ("valid completed reconciliation", "ml_valid_reconciliation", "experiment_after_effect"),
    ("damaged artifact reconciliation refusal", "ml_damaged_reconciliation", "experiment_after_effect"),
    ("repeated successful resume", "ml_repeated_resume", "none"),
    ("approval denial", "ml_approval_denial", "while_waiting_approval"),
    ("report refusal after artifact tamper", "ml_report_tamper", "after_success"),
)


def ml_workflow_configurations() -> tuple[EvaluationConfig, ...]:
    return (
        EvaluationConfig("baseline", 1, False, False),
        EvaluationConfig("full", 2, True, True),
        EvaluationConfig("no_recovery", 2, True, False),
    )


def ml_workflow_suite() -> tuple[EvaluationCase, ...]:
    return tuple(
        EvaluationCase(
            f"ML{index:02d}",
            description,
            scenario,
            f"{scenario}-input-v1",
            FaultSchedule(fault),
            1,
        )
        for index, (description, scenario, fault) in enumerate(_CASES, start=1)
    )
