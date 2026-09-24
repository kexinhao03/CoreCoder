import sys

from corecoder.evals.models import EvaluationCase, EvaluationConfig, FaultSchedule, RuntimeExecution
from corecoder.evals.runner import EvaluationRunner
from corecoder.reliagent import ReliAgentRuntime, TaskStep
from corecoder.runtime import (
    ExecutionKind,
    RiskLevel,
    RuntimeExecutor,
    SQLiteStore,
    ToolPolicy,
    ToolPolicyRegistry,
)


def test_runner_isolates_each_repetition_and_keeps_running_after_errors(tmp_path):
    case = EvaluationCase("E01", "test", "task", FaultSchedule("never"), 2)
    configs = (EvaluationConfig("full", True, True),)
    observed = []

    def execute(case, config, workdir, fault):
        observed.append(workdir)
        if len(observed) == 1:
            raise RuntimeError("injected")
        return True, True

    results = EvaluationRunner(tmp_path, configs, execute).run((case,))

    assert [result.error for result in results] == ["injected", None]
    assert observed[0] != observed[1]


def test_runner_attaches_trace_and_metrics_from_a_runtime_store(tmp_path):
    case = EvaluationCase("E01", "test", "task", FaultSchedule("never"), 1)
    config = EvaluationConfig("full", True, True)

    def execute(case, config, workdir, fault):
        store = SQLiteStore(workdir / "runtime.sqlite")
        store.initialize()
        policy = ToolPolicy(RiskLevel.READ_ONLY, ExecutionKind.SUBPROCESS, 5, 1, True, False, frozenset(), 1000)
        runtime = ReliAgentRuntime(store, RuntimeExecutor(store, ToolPolicyRegistry({"probe": policy})))
        run = runtime.run_task(
            goal="test", workspace=workdir,
            steps=(TaskStep("probe", (sys.executable, "-c", "print('ok')")),),
        )
        return RuntimeExecution(True, None, store, run.id)

    result = EvaluationRunner(tmp_path, (config,), execute).run((case,))[0]

    assert result.runtime_summary["run"]["status"] == "succeeded"
    assert result.trace_integrity["sequence_contiguous"] is True
    assert result.metrics_snapshot["final_status"] == "succeeded"
