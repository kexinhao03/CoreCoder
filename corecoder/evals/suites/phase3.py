"""The fixed Phase 3 evidence cases from the approved design."""

from corecoder.evals.models import EvaluationCase, FaultSchedule

_CASES = (
    ("success", "none"), ("retry success", "before_effect"),
    ("retry exhaustion", "before_effect"), ("timeout", "after_tool_persist"),
    ("approval allow", "while_waiting_approval"), ("approval deny", "while_waiting_approval"),
    ("approval restart", "while_waiting_approval"), ("succeeded-step restart", "after_step_complete"),
    ("unknown high-risk effect", "after_effect_before_persist"), ("cancellation", "before_effect"),
    ("completion persist failure workflow", "before_step_complete"),
    ("repeated resume workflow", "after_step_complete"),
)


def phase3_suite() -> tuple[EvaluationCase, ...]:
    return tuple(EvaluationCase(f"E{index:02d}", description, "local_task", FaultSchedule(point), 3)
                 for index, (description, point) in enumerate(_CASES, start=1))
