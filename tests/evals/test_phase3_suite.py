import time

from corecoder.evals.faults import FaultInjector
from corecoder.evals.scenarios import phase3 as phase3_scenarios
from corecoder.evals.suites.phase3 import phase3_configurations, phase3_suite


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


def test_cancel_scenario_waits_for_a_delayed_worker_start(tmp_path, monkeypatch):
    class SlowStartingRunner(phase3_scenarios._CancellableRunner):
        def run(self, spec, cancellation_event):
            time.sleep(1.1)
            return super().run(spec, cancellation_event)

    monkeypatch.setattr(
        phase3_scenarios, "_CancellableRunner", SlowStartingRunner
    )
    case = phase3_suite()[9]

    execution = phase3_scenarios.execute_scenario(
        case,
        phase3_configurations()[0],
        tmp_path,
        FaultInjector(case.fault_schedule),
    )

    assert execution.assertions["worker_stopped"] is True
