"""Read-only summaries derived from durable runtime facts."""

from dataclasses import dataclass
from datetime import datetime

from .state import RunStatus, ToolCallStatus
from .store import SQLiteStore


@dataclass(frozen=True)
class Availability:
    value: float | None
    available: bool
    reason: str | None


@dataclass(frozen=True)
class RunMetrics:
    final_status: str
    end_to_end_seconds: float | None
    execution_seconds: float | None
    terminal_tool_calls: int
    tool_success_rate: float | None
    timeout_count: int
    retry_count: int
    token_cost: Availability


class MetricsService:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def summarize_run(
        self, run_id: str, *, as_of: datetime | None = None
    ) -> RunMetrics:
        run = self._store.get_run(run_id)
        calls = self._store.list_tool_calls(run_id)
        terminal_statuses = {
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.FAILED,
            ToolCallStatus.TIMED_OUT,
            ToolCallStatus.CANCELLED,
        }
        terminal_calls = [call for call in calls if call.status in terminal_statuses]
        successful_calls = sum(
            call.status is ToolCallStatus.SUCCEEDED for call in terminal_calls
        )
        end_at = self._end_time(run.status, run.ended_at, as_of)
        return RunMetrics(
            final_status=run.status.value,
            end_to_end_seconds=self._duration(run.created_at, end_at),
            execution_seconds=self._duration(run.started_at, end_at),
            terminal_tool_calls=len(terminal_calls),
            tool_success_rate=(
                successful_calls / len(terminal_calls) if terminal_calls else None
            ),
            timeout_count=sum(
                call.status is ToolCallStatus.TIMED_OUT for call in terminal_calls
            ),
            retry_count=sum(call.retry_of is not None for call in calls),
            token_cost=Availability(None, False, "run_scoped_llm_telemetry_not_instrumented"),
        )

    @staticmethod
    def _end_time(
        status: RunStatus, ended_at: str | None, as_of: datetime | None
    ) -> datetime | None:
        if status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return MetricsService._parse_timestamp(ended_at)
        if as_of is None:
            return None
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a UTC offset")
        return as_of

    @staticmethod
    def _duration(started_at: str | None, ended_at: datetime | None) -> float | None:
        if started_at is None or ended_at is None:
            return None
        duration = (ended_at - MetricsService._parse_timestamp(started_at)).total_seconds()
        if duration < 0:
            raise ValueError("duration cannot be negative")
        return duration

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime:
        if value is None:
            raise ValueError("required timestamp is missing")
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must include a UTC offset")
        return timestamp
