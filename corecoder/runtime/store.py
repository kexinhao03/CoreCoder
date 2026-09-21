"""SQLite-backed durable storage for runtime facts and audit events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .approvals import ApprovalDecision, ApprovalRecord, ApprovalStatus
from .models import EventRecord, RunRecord, ToolCallRecord
from .policies import FailureKind, ToolPolicyRegistry
from .redaction import redact
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
        with closing(self._connect()) as connection:
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

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_calls(id),
                    status TEXT NOT NULL,
                    decision TEXT,
                    tool_name TEXT NOT NULL,
                    arguments_summary TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    risk_reason TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    resolved_at TEXT
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
                CREATE INDEX IF NOT EXISTS idx_approvals_tool_call_id
                    ON approvals(tool_call_id);
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
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
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

    def list_runs(
        self, statuses: Iterable[RunStatus] | None = None
    ) -> list[RunRecord]:
        values = None if statuses is None else [RunStatus(s).value for s in statuses]
        if values == []:
            return []
        query = "SELECT * FROM runs"
        if values is not None:
            query += " WHERE status IN (" + ",".join("?" for _ in values) + ")"
        with closing(self._connect()) as connection:
            rows = connection.execute(query + " ORDER BY created_at, id", values or []).fetchall()
        return [self._run_from_row(row) for row in rows]

    def list_tool_calls(
        self,
        run_id: str | None = None,
        statuses: Iterable[ToolCallStatus] | None = None,
    ) -> list[ToolCallRecord]:
        values = None if statuses is None else [ToolCallStatus(s).value for s in statuses]
        if values == []:
            return []
        clauses = []
        parameters = []
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if values is not None:
            clauses.append("status IN (" + ",".join("?" for _ in values) + ")")
            parameters.extend(values)
        query = "SELECT * FROM tool_calls"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with closing(self._connect()) as connection:
            rows = connection.execute(query + " ORDER BY created_at, id", parameters).fetchall()
        return [self._tool_call_from_row(row) for row in rows]

    def _recovery_records(
        self, connection: sqlite3.Connection, call_id: str
    ) -> tuple[RunRecord, ToolCallRecord]:
        row = connection.execute("SELECT * FROM tool_calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            raise KeyError(f"tool call not found: {call_id}")
        run_row = connection.execute("SELECT * FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
        return self._run_from_row(run_row), self._tool_call_from_row(row)

    @staticmethod
    def _interruption_reason(
        connection: sqlite3.Connection, call: ToolCallRecord
    ) -> str | None:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE run_id = ? AND type = ? ORDER BY sequence DESC",
            (call.run_id, "tool.interrupted"),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("tool_call_id") == call.id:
                return payload.get("reason")
        return None

    def get_interruption_reason(self, call_id: str) -> str | None:
        with closing(self._connect()) as connection:
            _, call = self._recovery_records(connection, call_id)
            return self._interruption_reason(connection, call)

    def mark_orphaned_tool_call(self, call_id: str) -> tuple[RunRecord, ToolCallRecord]:
        """Atomically record a startup orphan without reviving a cancelled Run."""
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run, call = self._recovery_records(connection, call_id)
            if call.status is not ToolCallStatus.RUNNING:
                raise ValueError("orphaned tool call must be running")
            ensure_tool_call_transition(call.status, ToolCallStatus.INTERRUPTED)
            if run.status not in {RunStatus.CANCELLED, RunStatus.RECOVERABLE}:
                ensure_run_transition(run.status, RunStatus.RECOVERABLE)
                connection.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                    (RunStatus.RECOVERABLE.value, timestamp, run.id),
                )
            connection.execute(
                "UPDATE tool_calls SET status = ?, updated_at = ? WHERE id = ?",
                (ToolCallStatus.INTERRUPTED.value, timestamp, call.id),
            )
            self._insert_event(
                connection, run_id=run.id, sequence=self._next_event_sequence(connection, run.id),
                event_type="tool.interrupted",
                payload={"tool_call_id": call.id, "reason": "process_lost"}, created_at=timestamp,
            )
            updated = self._recovery_records(connection, call_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return updated

    def mark_interrupted_run_recoverable(self, call_id: str) -> tuple[RunRecord, ToolCallRecord]:
        """Normalize a previously interrupted call's Run once, preserving its cause."""
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run, call = self._recovery_records(connection, call_id)
            if call.status is not ToolCallStatus.INTERRUPTED:
                raise ValueError("recovery requires an interrupted tool call")
            if run.status in {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}:
                ensure_run_transition(run.status, RunStatus.RECOVERABLE)
                connection.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                    (RunStatus.RECOVERABLE.value, timestamp, run.id),
                )
                self._insert_event(
                    connection, run_id=run.id, sequence=self._next_event_sequence(connection, run.id),
                    event_type="run.recoverable", payload={"tool_call_id": call.id},
                    created_at=timestamp,
                )
            updated = self._recovery_records(connection, call_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return updated

    def _resolve_recovery(
        self, connection: sqlite3.Connection, run: RunRecord, call: ToolCallRecord,
        status: ToolCallStatus, resolution: str, timestamp: str, reason: str | None = None,
    ) -> None:
        created = call.status is ToolCallStatus.CREATED
        if created and (status is not ToolCallStatus.CANCELLED or resolution != "abandon"):
            raise ValueError("created reservation only supports abandonment")
        if not created and call.status is not ToolCallStatus.INTERRUPTED:
            raise ValueError("recovery requires an interrupted tool call")
        ensure_tool_call_transition(call.status, status)
        if not created and run.status is not RunStatus.CANCELLED and run.status is not RunStatus.RUNNING:
            ensure_run_transition(run.status, RunStatus.RUNNING)
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (RunStatus.RUNNING.value, timestamp, run.id),
            )
        connection.execute(
            "UPDATE tool_calls SET status = ?, updated_at = ?, ended_at = ?, result_summary = ? WHERE id = ?",
            (status.value, timestamp, timestamp, reason or call.result_summary, call.id),
        )
        payload = {"tool_call_id": call.id, "resolution": resolution}
        if created:
            payload["reason"] = "created_not_started"
        if reason is not None:
            payload["reason"] = reason
        self._insert_event(
            connection, run_id=run.id, sequence=self._next_event_sequence(connection, run.id),
            event_type="recovery.resolved", payload=payload, created_at=timestamp,
        )

    def resume_recovery_retry(
        self, expected_run: RunRecord, expected_call: ToolCallRecord, registry: ToolPolicyRegistry,
    ) -> ToolCallRecord:
        """Validate, resolve, and reserve a safe new attempt in one transaction."""
        timestamp = datetime.now(timezone.utc).isoformat()
        retry_id = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run, call = self._recovery_records(connection, expected_call.id)
            if run != expected_run or call != expected_call:
                raise ValueError("stale or mismatched recovery candidate")
            policy = registry.resolve(call.tool_name)
            if not (
                run.status is RunStatus.RECOVERABLE
                and call.status is ToolCallStatus.INTERRUPTED
                and call.risk_level is RiskLevel.READ_ONLY and call.idempotent
                and policy.risk_level is RiskLevel.READ_ONLY and policy.idempotent
                and policy.auto_retry and call.attempt < policy.max_attempts
                and self._interruption_reason(connection, call) == "process_lost"
            ):
                raise ValueError("recovery retry is not safe or permitted")
            active = connection.execute(
                "SELECT id FROM tool_calls WHERE run_id = ? AND status IN (?, ?, ?) LIMIT 1",
                (run.id, ToolCallStatus.CREATED.value, ToolCallStatus.WAITING_APPROVAL.value,
                 ToolCallStatus.RUNNING.value),
            ).fetchone()
            if active is not None:
                raise ValueError("run already has an active tool call")
            self._resolve_recovery(
                connection, run, call, ToolCallStatus.FAILED, "confirmed_failed", timestamp,
                reason="process_lost",
            )
            self._insert_event(
                connection, run_id=run.id, sequence=self._next_event_sequence(connection, run.id),
                event_type="tool.retry_scheduled",
                payload={"tool_call_id": retry_id, "retry_of": call.id, "attempt": call.attempt + 1},
                created_at=timestamp,
            )
            connection.execute(
                """
                INSERT INTO tool_calls (
                    id, run_id, retry_of, tool_name, arguments_json, risk_level,
                    execution_kind, idempotent, idempotency_key, status, attempt,
                    timeout_seconds, created_at, updated_at
                ) SELECT ?, run_id, id, tool_name, arguments_json, risk_level,
                    execution_kind, idempotent, idempotency_key, ?, attempt + 1,
                    timeout_seconds, ?, ? FROM tool_calls WHERE id = ?
                """,
                (retry_id, ToolCallStatus.CREATED.value, timestamp, timestamp, call.id),
            )
            self._insert_event(
                connection, run_id=run.id, sequence=self._next_event_sequence(connection, run.id),
                event_type="tool.created",
                payload={"tool_call_id": retry_id, "retry_of": call.id, "attempt": call.attempt + 1,
                         "tool_name": call.tool_name}, created_at=timestamp,
            )
            _, retry = self._recovery_records(connection, retry_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return retry

    def reconcile_interrupted_tool_call(
        self, call_id: str, status: ToolCallStatus, resolution: str,
    ) -> ToolCallRecord:
        """Resolve interrupted work or abandon a never-started reservation atomically."""
        resolutions = {"confirmed_succeeded": ToolCallStatus.SUCCEEDED,
                       "confirmed_failed": ToolCallStatus.FAILED, "abandon": ToolCallStatus.CANCELLED}
        if resolutions.get(resolution) is not status:
            raise ValueError("invalid recovery resolution")
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run, call = self._recovery_records(connection, call_id)
            self._resolve_recovery(connection, run, call, status, resolution, timestamp)
            _, updated = self._recovery_records(connection, call_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return updated

    def schedule_retry(
        self, expected_call: ToolCallRecord, failure_kind: FailureKind,
        registry: ToolPolicyRegistry,
    ) -> ToolCallRecord:
        """Reserve an ordinary safe retry together with both attempt audit events."""
        timestamp = datetime.now(timezone.utc).isoformat()
        retry_id = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run, call = self._recovery_records(connection, expected_call.id)
            policy = registry.resolve(call.tool_name)
            if not (
                call == expected_call and run.status is RunStatus.RUNNING
                and call.status in {ToolCallStatus.FAILED, ToolCallStatus.TIMED_OUT}
                and call.risk_level is RiskLevel.READ_ONLY and call.idempotent
                and policy.risk_level is RiskLevel.READ_ONLY and policy.idempotent
                and policy.auto_retry and failure_kind in policy.retryable_failures
                and failure_kind not in {FailureKind.CANCELLED, FailureKind.TERMINATION_UNKNOWN}
                and call.attempt < policy.max_attempts
            ):
                raise ValueError("retry is not safe or permitted")
            active = connection.execute(
                "SELECT id FROM tool_calls WHERE run_id = ? AND status IN (?, ?, ?) LIMIT 1",
                (run.id, ToolCallStatus.CREATED.value, ToolCallStatus.WAITING_APPROVAL.value,
                 ToolCallStatus.RUNNING.value),
            ).fetchone()
            if active is not None:
                raise ValueError("run already has an active tool call")
            previous_retry = connection.execute(
                "SELECT id FROM tool_calls WHERE retry_of = ? LIMIT 1", (call.id,),
            ).fetchone()
            if previous_retry is not None:
                raise ValueError("retry source already has a subsequent attempt")
            self._insert_event(
                connection, run_id=run.id, sequence=self._next_event_sequence(connection, run.id),
                event_type="tool.retry_scheduled",
                payload={"tool_call_id": retry_id, "retry_of": call.id, "attempt": call.attempt + 1},
                created_at=timestamp,
            )
            connection.execute(
                """
                INSERT INTO tool_calls (
                    id, run_id, retry_of, tool_name, arguments_json, risk_level,
                    execution_kind, idempotent, idempotency_key, status, attempt,
                    timeout_seconds, created_at, updated_at
                ) SELECT ?, run_id, id, tool_name, arguments_json, risk_level,
                    execution_kind, idempotent, idempotency_key, ?, attempt + 1,
                    timeout_seconds, ?, ? FROM tool_calls WHERE id = ?
                """,
                (retry_id, ToolCallStatus.CREATED.value, timestamp, timestamp, call.id),
            )
            self._insert_event(
                connection, run_id=run.id, sequence=self._next_event_sequence(connection, run.id),
                event_type="tool.created",
                payload={"tool_call_id": retry_id, "retry_of": call.id, "attempt": call.attempt + 1,
                         "tool_name": call.tool_name}, created_at=timestamp,
            )
            _, retry = self._recovery_records(connection, retry_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return retry

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
            run_row = connection.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(f"run not found: {run_id}")
            run_status = RunStatus(run_row["status"])
            if run_status not in {RunStatus.CREATED, RunStatus.RUNNING}:
                raise ValueError(
                    f"run cannot accept tool calls: {run_status.value}"
                )

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

            active_call = connection.execute(
                """
                SELECT id FROM tool_calls
                WHERE run_id = ? AND status IN (?, ?, ?)
                LIMIT 1
                """,
                (
                    run_id,
                    ToolCallStatus.CREATED.value,
                    ToolCallStatus.WAITING_APPROVAL.value,
                    ToolCallStatus.RUNNING.value,
                ),
            ).fetchone()
            if active_call is not None:
                raise ValueError("run already has an active tool call")

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
                            "tool_call_id": call.id,
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

    def request_approval(
        self,
        tool_call_id: str,
        *,
        arguments_summary: str,
        workspace: str,
        risk_reason: str,
        approval_id: str | None = None,
    ) -> ApprovalRecord:
        resolved_id = approval_id or uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            call_row = connection.execute(
                "SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)
            ).fetchone()
            if call_row is None:
                raise KeyError(f"tool call not found: {tool_call_id}")
            run_row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (call_row["run_id"],)
            ).fetchone()
            if run_row is None:
                raise KeyError(f"run not found: {call_row['run_id']}")

            ensure_run_transition(
                RunStatus(run_row["status"]), RunStatus.WAITING_APPROVAL
            )
            ensure_tool_call_transition(
                ToolCallStatus(call_row["status"]),
                ToolCallStatus.WAITING_APPROVAL,
            )
            approval = ApprovalRecord(
                id=resolved_id,
                tool_call_id=tool_call_id,
                status=ApprovalStatus.PENDING,
                decision=None,
                tool_name=call_row["tool_name"],
                arguments_summary=arguments_summary,
                workspace=workspace,
                risk_reason=risk_reason,
                requested_at=timestamp,
                resolved_at=None,
            )
            connection.execute(
                """
                INSERT INTO approvals (
                    id, tool_call_id, status, decision, tool_name,
                    arguments_summary, workspace, risk_reason,
                    requested_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.tool_call_id,
                    approval.status.value,
                    approval.decision,
                    approval.tool_name,
                    approval.arguments_summary,
                    approval.workspace,
                    approval.risk_reason,
                    approval.requested_at,
                    approval.resolved_at,
                ),
            )
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (
                    RunStatus.WAITING_APPROVAL.value,
                    timestamp,
                    call_row["run_id"],
                ),
            )
            connection.execute(
                """
                UPDATE tool_calls SET status = ?, updated_at = ? WHERE id = ?
                """,
                (
                    ToolCallStatus.WAITING_APPROVAL.value,
                    timestamp,
                    tool_call_id,
                ),
            )
            sequence = self._next_event_sequence(
                connection, call_row["run_id"]
            )
            self._insert_event(
                connection,
                run_id=call_row["run_id"],
                sequence=sequence,
                event_type="approval.requested",
                payload={
                    "approval_id": approval.id,
                    "tool_call_id": tool_call_id,
                    "tool_name": approval.tool_name,
                    "risk_reason": risk_reason,
                },
                created_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return approval

    def resolve_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalRecord:
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            approval_row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if approval_row is None:
                raise KeyError(f"approval not found: {approval_id}")
            if ApprovalStatus(approval_row["status"]) is not ApprovalStatus.PENDING:
                raise ValueError("approval is already resolved")

            call_row = connection.execute(
                "SELECT * FROM tool_calls WHERE id = ?",
                (approval_row["tool_call_id"],),
            ).fetchone()
            if call_row is None:
                raise KeyError(
                    f"tool call not found: {approval_row['tool_call_id']}"
                )
            run_row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (call_row["run_id"],)
            ).fetchone()
            if run_row is None:
                raise KeyError(f"run not found: {call_row['run_id']}")

            run_status = RunStatus(run_row["status"])
            call_status = ToolCallStatus(call_row["status"])
            if (
                run_status is not RunStatus.WAITING_APPROVAL
                or call_status is not ToolCallStatus.WAITING_APPROVAL
            ):
                raise ValueError(
                    "approval state mismatch: "
                    f"run={run_status.value}, tool_call={call_status.value}"
                )

            ensure_run_transition(run_status, RunStatus.RUNNING)
            if decision is ApprovalDecision.DENY:
                ensure_tool_call_transition(
                    call_status,
                    ToolCallStatus.CANCELLED,
                )
                approval_status = ApprovalStatus.DENIED
            else:
                approval_status = ApprovalStatus.APPROVED

            connection.execute(
                """
                UPDATE approvals
                SET status = ?, decision = ?, resolved_at = ?
                WHERE id = ?
                """,
                (
                    approval_status.value,
                    decision.value,
                    timestamp,
                    approval_id,
                ),
            )
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (RunStatus.RUNNING.value, timestamp, call_row["run_id"]),
            )
            if decision is ApprovalDecision.DENY:
                connection.execute(
                    """
                    UPDATE tool_calls
                    SET status = ?, updated_at = ?, ended_at = ?
                    WHERE id = ?
                    """,
                    (
                        ToolCallStatus.CANCELLED.value,
                        timestamp,
                        timestamp,
                        call_row["id"],
                    ),
                )
            sequence = self._next_event_sequence(
                connection, call_row["run_id"]
            )
            self._insert_event(
                connection,
                run_id=call_row["run_id"],
                sequence=sequence,
                event_type="approval.resolved",
                payload={
                    "approval_id": approval_id,
                    "tool_call_id": call_row["id"],
                    "decision": decision.value,
                },
                created_at=timestamp,
            )
            updated_row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._approval_from_row(updated_row)

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return self._approval_from_row(row)

    def get_approval_for_tool_call(
        self, tool_call_id: str
    ) -> ApprovalRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
        if row is None:
            return None
        return self._approval_from_row(row)

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord:
        with closing(self._connect()) as connection:
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
            event_payload = dict(payload or {})
            event_payload["tool_call_id"] = tool_call_id
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
                    json.dumps(event_payload, sort_keys=True),
                    timestamp,
                ),
            )
            if to_status is ToolCallStatus.INTERRUPTED:
                run, _ = self._recovery_records(connection, tool_call_id)
                if run.status in {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}:
                    ensure_run_transition(run.status, RunStatus.RECOVERABLE)
                    connection.execute(
                        "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                        (RunStatus.RECOVERABLE.value, timestamp, run.id),
                    )
                    self._insert_event(
                        connection, run_id=run.id,
                        sequence=self._next_event_sequence(connection, run.id),
                        event_type="run.recoverable", payload={"tool_call_id": tool_call_id},
                        created_at=timestamp,
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

    def cancel_run(self, run_id: str) -> RunRecord:
        """Cancel admission and unstarted calls; active work must settle separately."""
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            ensure_run_transition(RunStatus(row["status"]), RunStatus.CANCELLED)
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (RunStatus.CANCELLED.value, timestamp, run_id),
            )
            calls = connection.execute(
                """
                SELECT id FROM tool_calls
                WHERE run_id = ? AND status IN (?, ?)
                ORDER BY created_at, id
                """,
                (run_id, ToolCallStatus.CREATED.value, ToolCallStatus.WAITING_APPROVAL.value),
            ).fetchall()
            sequence = self._next_event_sequence(connection, run_id)
            for call in calls:
                connection.execute(
                    """
                    UPDATE tool_calls SET status = ?, updated_at = ?, ended_at = ?
                    WHERE id = ?
                    """,
                    (ToolCallStatus.CANCELLED.value, timestamp, timestamp, call["id"]),
                )
                self._insert_event(
                    connection, run_id=run_id, sequence=sequence,
                    event_type="tool.cancelled", payload={"tool_call_id": call["id"]},
                    created_at=timestamp,
                )
                sequence += 1
            self._insert_event(
                connection, run_id=run_id, sequence=sequence,
                event_type="run.cancelled", payload={}, created_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_run(run_id)

    def record_event(
        self, run_id: str, event_type: str, payload: dict
    ) -> EventRecord:
        timestamp = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            sequence = self._next_event_sequence(connection, run_id)
            self._insert_event(
                connection,
                run_id=run_id,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND sequence = ?",
                (run_id, sequence),
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return EventRecord(
            id=row["id"],
            run_id=row["run_id"],
            sequence=row["sequence"],
            type=row["type"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )

    def list_events(self, run_id: str) -> list[EventRecord]:
        with closing(self._connect()) as connection:
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
    def _next_event_sequence(
        connection: sqlite3.Connection, run_id: str
    ) -> int:
        return connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM events
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        payload: dict,
        created_at: str,
    ) -> None:
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
                json.dumps(redact(payload), sort_keys=True),
                created_at,
            ),
        )

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
        decision = row["decision"]
        return ApprovalRecord(
            id=row["id"],
            tool_call_id=row["tool_call_id"],
            status=ApprovalStatus(row["status"]),
            decision=ApprovalDecision(decision) if decision is not None else None,
            tool_name=row["tool_name"],
            arguments_summary=row["arguments_summary"],
            workspace=row["workspace"],
            risk_reason=row["risk_reason"],
            requested_at=row["requested_at"],
            resolved_at=row["resolved_at"],
        )

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
