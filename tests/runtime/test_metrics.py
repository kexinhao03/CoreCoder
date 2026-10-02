"""Metrics are derived from persisted facts without invented telemetry."""

from datetime import datetime, timezone

from corecoder.runtime.metrics import MetricsService
from corecoder.runtime.state import ExecutionKind, RiskLevel, ToolCallStatus


def test_metrics_marks_runtime_token_and_cost_as_unavailable(running_store):
    metrics = MetricsService(running_store).summarize_run("run-1")

    assert metrics.final_status == "running"
    assert metrics.token_cost.available is False
    assert metrics.token_cost.value is None


def test_metrics_derives_terminal_tool_counts_and_retry_count(running_store):
    call = running_store.create_tool_call(
        run_id="run-1", tool_name="probe", arguments={"argv": ["probe"]},
        risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True, idempotency_key="key", timeout_seconds=2,
    )
    running_store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    running_store.transition_tool_call(call.id, ToolCallStatus.TIMED_OUT, "tool.timed_out")
    retry = running_store.create_tool_call(
        run_id="run-1", retry_of=call.id, tool_name="probe", arguments={"argv": ["probe"]},
        risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True, idempotency_key="key", timeout_seconds=2,
    )
    running_store.transition_tool_call(retry.id, ToolCallStatus.RUNNING, "tool.started")
    running_store.transition_tool_call(retry.id, ToolCallStatus.TIMED_OUT, "tool.timed_out")
    final = running_store.create_tool_call(
        run_id="run-1", retry_of=retry.id, tool_name="probe", arguments={"argv": ["probe"]},
        risk_level=RiskLevel.READ_ONLY, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=True, idempotency_key="key", timeout_seconds=2,
    )
    running_store.transition_tool_call(final.id, ToolCallStatus.RUNNING, "tool.started")
    running_store.transition_tool_call(final.id, ToolCallStatus.SUCCEEDED, "tool.completed")

    metrics = MetricsService(running_store).summarize_run("run-1")

    assert (
        metrics.terminal_tool_calls,
        metrics.tool_success_rate,
        metrics.timeout_count,
        metrics.retry_count,
    ) == (3, 1 / 3, 2, 2)


def test_nonterminal_run_requires_as_of_for_latency(running_store):
    service = MetricsService(running_store)

    without_snapshot = service.summarize_run("run-1")
    with_snapshot = service.summarize_run(
        "run-1", as_of=datetime.now(timezone.utc)
    )

    assert without_snapshot.end_to_end_seconds is None
    assert with_snapshot.end_to_end_seconds is not None
