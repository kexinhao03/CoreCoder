from corecoder.evals.suites.phase3 import phase3_suite


def test_phase3_suite_has_twelve_unique_cases_and_two_workflows():
    suite = phase3_suite()

    assert [case.id for case in suite] == [f"E{number:02d}" for number in range(1, 13)]
    assert all(case.repeat_count == 3 for case in suite)
    assert sum("workflow" in case.description for case in suite) >= 2
