"""SQLite-backed durable storage for runtime facts and audit events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import EventRecord, RunRecord, ToolCallRecord
from .state import (
    ExecutionKind,
    RiskLevel,
    RunStatus,
    ToolCallStatus,
    ensure_run_transition,
    ensure_tool_call_transition,
)

_ENDED_TOOL_CALL_STATUSES = frozenset({
    ToolCallStatus.SUCCEEDED,
    ToolCallStatus.FAILED,
    ToolCallStatus.TIMED_OUT,
    ToolCallStatus.CANCELLED,
})


class SQLiteStore:
    """Persist runtime state and its audit trail in SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    workflow TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    retry_of TEXT REFERENCES tool_calls(id),
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    execution_kind TEXT NOT NULL,
                    idempotent INTEGER NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL CHECK (attempt >= 1),
                    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds > 0),
                    result_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_tool_calls_run_id
                    ON tool_calls(run_id);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_retry_of
                    ON tool_calls(retry_of);
                CREATE INDEX IF NOT EXISTS idx_events_run_sequence
                    ON events(run_id, sequence);
                """
            )

    def create_run(
        self,
        *,
        goal: str,
        workflow: str,
        workspace: str | Path,
        model: str,
        prompt_version: str,
        run_id: str | None = None,
    ) -> RunRecord:
        resolved_id = run_id or uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        run = RunRecord(
            id=resolved_id,
            goal=goal,
            workflow=workflow,
            status=RunStatus.CREATED,
            workspace=str(Path(workspace).resolve()),
            model=model,
            prompt_version=prompt_version,
            created_at=timestamp,
            updated_at=timestamp,
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs (
                    id, goal, workflow, status, workspace, model,
                    prompt_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.goal,
                    run.workflow,
                    run.status.value,
                    run.workspace,
                    run.model,
                    run.prompt_version,
                    run.created_at,
                    run.updated_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO events (
                    run_id, sequence, type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    1,
                    "run.created",
                    json.dumps({"workflow": workflow}, sort_keys=True),
                    timestamp,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return run

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord(
            id=row["id"],
            goal=row["goal"],
            workflow=row["workflow"],
            status=RunStatus(row["status"]),
            workspace=row["workspace"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_tool_call(
        self,
        *,
        run_id: str,
        tool_name: str,
        arguments: dict,
        risk_level: RiskLevel,
        execution_kind: ExecutionKind,
        idempotent: bool,
        idempotency_key: str | None,
        timeout_seconds: int,
        retry_of: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolCallRecord:
        resolved_id = tool_call_id or uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = 1
            if retry_of is not None:
                source = connection.execute(
                    "SELECT * FROM tool_calls WHERE id = ?", (retry_of,)
                ).fetchone()
                if source is None:
                    raise KeyError(f"retry source not found: {retry_of}")
                if source["run_id"] != run_id:
                    raise ValueError("retry_of belongs to another run")
                if ToolCallStatus(source["status"]) not in {
                    ToolCallStatus.FAILED,
                    ToolCallStatus.TIMED_OUT,
                }:
                    raise ValueError("retry source is not retryable")
                if not bool(source["idempotent"]):
                    raise ValueError(
                        "non-idempotent tool call cannot be auto-retried"
                    )
                attempt = source["attempt"] + 1

            call = ToolCallRecord(
                id=resolved_id,
                run_id=run_id,
                retry_of=retry_of,
                tool_name=tool_name,
                arguments=arguments,
                risk_level=risk_level,
                execution_kind=execution_kind,
                idempotent=idempotent,
                idempotency_key=idempotency_key,
                status=ToolCallStatus.CREATED,
                attempt=attempt,
                timeout_seconds=timeout_seconds,
                result_summary=None,
                created_at=timestamp,
                updated_at=timestamp,
                started_at=None,
                ended_at=None,
            )
            connection.execute(
                """
                INSERT INTO tool_calls (
                    id, run_id, retry_of, tool_name, arguments_json,
                    risk_level, execution_kind, idempotent, idempotency_key,
                    status, attempt, timeout_seconds, result_summary,
                    created_at, updated_at, started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call.id,
                    call.run_id,
                    call.retry_of,
                    call.tool_name,
                    json.dumps(call.arguments, sort_keys=True),
                    call.risk_level.value,
                    call.execution_kind.value,
                    int(call.idempotent),
                    call.idempotency_key,
                    call.status.value,
                    call.attempt,
                    call.timeout_seconds,
                    call.result_summary,
                    call.created_at,
                    call.updated_at,
                    call.started_at,
                    call.ended_at,
                ),
            )
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM events
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO events (
                    run_id, sequence, type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    "tool.created",
                    json.dumps(
                        {
                            "attempt": attempt,
                            "retry_of": retry_of,
                            "tool_name": tool_name,
                        },
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return call

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)
            ).fetchone()
        if row is None:
            raise KeyError(tool_call_id)
        return self._tool_call_from_row(row)

    def transition_tool_call(
        self,
        tool_call_id: str,
        to_status: ToolCallStatus,
        event_type: str,
        result_summary: str | None = None,
        payload: dict | None = None,
    ) -> ToolCallRecord:
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"tool call not found: {tool_call_id}")

            ensure_tool_call_transition(
                ToolCallStatus(row["status"]), to_status
            )
            started_at = row["started_at"]
            if to_status is ToolCallStatus.RUNNING and started_at is None:
                started_at = timestamp
            ended_at = row["ended_at"]
            if to_status in _ENDED_TOOL_CALL_STATUSES:
                ended_at = timestamp
            summary = result_summary[:2000] if result_summary else None
            connection.execute(
                """
                UPDATE tool_calls
                SET status = ?, result_summary = ?, updated_at = ?,
                    started_at = ?, ended_at = ?
                WHERE id = ?
                """,
                (
                    to_status.value,
                    summary,
                    timestamp,
                    started_at,
                    ended_at,
                    tool_call_id,
                ),
            )
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM events
                WHERE run_id = ?
                """,
                (row["run_id"],),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO events (
                    run_id, sequence, type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["run_id"],
                    sequence,
                    event_type,
                    json.dumps(payload or {}, sort_keys=True),
                    timestamp,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._tool_call_from_row(updated_row)

    def transition_run(
        self,
        run_id: str,
        to_status: RunStatus,
        event_type: str,
        payload: dict | None = None,
    ) -> RunRecord:
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")

            ensure_run_transition(RunStatus(row["status"]), to_status)
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM events
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (to_status.value, timestamp, run_id),
            )
            connection.execute(
                """
                INSERT INTO events (
                    run_id, sequence, type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    event_type,
                    json.dumps(payload or {}, sort_keys=True),
                    timestamp,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return RunRecord(
            id=row["id"],
            goal=row["goal"],
            workflow=row["workflow"],
            status=to_status,
            workspace=row["workspace"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            created_at=row["created_at"],
            updated_at=timestamp,
        )

    def list_events(self, run_id: str) -> list[EventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [
            EventRecord(
                id=row["id"],
                run_id=row["run_id"],
                sequence=row["sequence"],
                type=row["type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _tool_call_from_row(row: sqlite3.Row) -> ToolCallRecord:
        return ToolCallRecord(
            id=row["id"],
            run_id=row["run_id"],
            retry_of=row["retry_of"],
            tool_name=row["tool_name"],
            arguments=json.loads(row["arguments_json"]),
            risk_level=RiskLevel(row["risk_level"]),
            execution_kind=ExecutionKind(row["execution_kind"]),
            idempotent=bool(row["idempotent"]),
            idempotency_key=row["idempotency_key"],
            status=ToolCallStatus(row["status"]),
            attempt=row["attempt"],
            timeout_seconds=row["timeout_seconds"],
            result_summary=row["result_summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )
