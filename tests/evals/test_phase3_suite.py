from corecoder.evals.suites.phase3 import phase3_suite


def test_phase3_suite_has_ten_executable_cases_with_shared_inputs():
    suite = phase3_suite()

    assert [case.id for case in suite] == [f"E{number:02d}" for number in range(1, 11)]
    assert [case.scenario for case in suite] == [
        "normal_success",
        "transient_then_success",
        "retry_exhausted",
        "timeout",
        "approval_denied",
        "crash_after_step",
        "high_risk_interrupted",
        "repeated_resume",
        "effect_persist_failure",
        "cancel_run",
    ]
    assert all(case.repeat_count == 3 for case in suite)
    assert len({case.input_id for case in suite}) == len(suite)
