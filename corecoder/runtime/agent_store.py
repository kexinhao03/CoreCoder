"""Encrypted reasoning journal and same-host, per-Run execution ownership."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .checkpoints import CheckpointCipher
from .redaction import redact
from .state import RunStatus, StepStatus, ToolCallStatus
from .store import SQLiteStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentJournal:
    """Persist exact model inputs and outputs separately from redacted audit facts."""

    def __init__(self, store: SQLiteStore, cipher: CheckpointCipher) -> None:
        self.store = store
        self.cipher = cipher
        with closing(store._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id),
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    phase TEXT NOT NULL, revision INTEGER NOT NULL,
                    turn_sequence INTEGER NOT NULL DEFAULT 0,
                    context_ciphertext BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_turns (
                    run_id TEXT NOT NULL REFERENCES agent_sessions(run_id),
                    sequence INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE,
                    request_ciphertext BLOB NOT NULL, response_ciphertext BLOB,
                    status TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS agent_invocations (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    turn_sequence INTEGER NOT NULL, provider_call_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, tool_name TEXT NOT NULL,
                    arguments_ciphertext BLOB NOT NULL, arguments_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL, current_attempt_id TEXT REFERENCES tool_calls(id),
                    result_ciphertext BLOB,
                    FOREIGN KEY (run_id, turn_sequence) REFERENCES agent_turns(run_id, sequence),
                    UNIQUE (run_id, turn_sequence, provider_call_id),
                    UNIQUE (run_id, turn_sequence, ordinal)
                );
            """)
        store._agent_journal = self

    @contextmanager
    def _transaction(self):
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _context_identity(run_id: str, revision: int) -> dict:
        return {"record": "context", "run_id": run_id, "revision": revision}

    @staticmethod
    def _invocation_identity(row, record: str) -> dict:
        identity = {
            "record": record,
            "run_id": row["run_id"],
            "turn_sequence": row["turn_sequence"],
            "invocation_id": row["id"],
            "provider_call_id": row["provider_call_id"],
            "ordinal": row["ordinal"],
            "tool_name": row["tool_name"],
        }
        if record == "result":
            identity["attempt_id"] = row["current_attempt_id"]
        return identity

    @staticmethod
    def _turn_identity(row, record: str) -> dict:
        return {"record": record, "run_id": row["run_id"], "turn_sequence": row["sequence"], "request_id": row["request_id"]}

    def _authenticate_turn(self, connection, run_id: str, sequence: int):
        turn = connection.execute("SELECT * FROM agent_turns WHERE run_id = ? AND sequence = ?", (run_id, sequence)).fetchone()
        if turn is None:
            raise ValueError("agent turn checkpoint not found")
        self.cipher.open(self._turn_identity(turn, "request"), turn["request_ciphertext"])
        requires_response = turn["status"] in {"responded", "complete"}
        if requires_response and turn["response_ciphertext"] is None:
            raise ValueError("agent turn is missing its committed response")
        if turn["response_ciphertext"] is not None:
            self.cipher.open(self._turn_identity(turn, "response"), turn["response_ciphertext"])
        return turn

    def _load(self, connection, run_id: str):
        row = connection.execute("SELECT * FROM agent_sessions WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("agent checkpoint not found")
        if row["schema_version"] != 1:
            raise ValueError("unsupported checkpoint schema")
        state = self.cipher.open(self._context_identity(run_id, row["revision"]), row["context_ciphertext"])
        if row["turn_sequence"]:
            turn = self._authenticate_turn(connection, run_id, row["turn_sequence"])
            if row["phase"] in {"dispatching", "complete"} and turn["response_ciphertext"] is None:
                raise ValueError("checkpoint is missing its committed response")
        return row, state

    def _write_state(self, connection, row, state: dict, phase: str) -> None:
        revision = row["revision"] + 1
        token = self.cipher.seal(self._context_identity(row["run_id"], revision), state)
        connection.execute(
            """UPDATE agent_sessions SET context_ciphertext = ?, phase = ?, revision = ?
                              WHERE run_id = ?""",
            (token, phase, revision, row["run_id"]),
        )

    def create(self, run_id: str, state: dict) -> None:
        token = self.cipher.seal(self._context_identity(run_id, 0), state)
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO agent_sessions
                (run_id, phase, revision, context_ciphertext) VALUES (?, 'ready', 0, ?)""",
                (run_id, token),
            )

    def load(self, run_id: str) -> dict:
        with closing(self.store._connect()) as connection:
            row, state = self._load(connection, run_id)
        return {"state": state, "phase": row["phase"], "revision": row["revision"], "turn_sequence": row["turn_sequence"]}

    def save_state(self, run_id: str, state: dict, *, phase: str = "ready") -> None:
        if phase not in {"ready", "complete"}:
            raise ValueError("save_state requires a settled checkpoint phase")
        with self._transaction() as connection:
            row, _ = self._load(connection, run_id)
            if row["phase"] not in {"ready", "complete"}:
                raise ValueError("cannot replace an unfinished checkpoint")
            self._write_state(connection, row, state, phase)

    def begin_turn(self, run_id: str, request: dict) -> int:
        with self._transaction() as connection:
            row, state = self._load(connection, run_id)
            if row["phase"] not in {"ready", "complete"}:
                raise ValueError("agent checkpoint is not ready for inference")
            run = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run["status"] not in {RunStatus.CREATED.value, RunStatus.RUNNING.value}:
                raise ValueError("run cannot accept inference")
            sequence = row["turn_sequence"] + 1
            turn = {"run_id": run_id, "sequence": sequence, "request_id": uuid.uuid4().hex}
            token = self.cipher.seal(self._turn_identity(turn, "request"), request)
            connection.execute(
                """INSERT INTO agent_turns
                (run_id, sequence, request_id, request_ciphertext, status) VALUES (?, ?, ?, ?, 'requesting')""",
                (run_id, sequence, turn["request_id"], token),
            )
            connection.execute("UPDATE agent_sessions SET turn_sequence = ? WHERE run_id = ?", (sequence, run_id))
            self._write_state(connection, row, state, "llm_inflight")
        return sequence

    def authorize_llm_retry(self, run_id: str, *, evidence: dict) -> None:
        """Authorize a new inference after an unknown response, retaining the old request."""
        if not evidence:
            raise ValueError("inference retry requires explicit evidence")
        with self._transaction() as connection:
            row, state = self._load(connection, run_id)
            turn = connection.execute(
                "SELECT * FROM agent_turns WHERE run_id = ? AND sequence = ?", (run_id, row["turn_sequence"])
            ).fetchone()
            invocation = connection.execute(
                "SELECT 1 FROM agent_invocations WHERE run_id = ? AND turn_sequence = ?", (run_id, row["turn_sequence"])
            ).fetchone()
            if (
                row["phase"] not in {"llm_inflight", "blocked"}
                or turn is None
                or turn["status"] != "requesting"
                or turn["response_ciphertext"] is not None
                or invocation
            ):
                raise ValueError("only an unfinished inference can be explicitly retried")
            self.cipher.open(self._turn_identity(turn, "request"), turn["request_ciphertext"])
            connection.execute(
                "UPDATE agent_turns SET status = 'outcome_unknown' WHERE run_id = ? AND sequence = ?",
                (run_id, row["turn_sequence"]),
            )
            state.pop("blocked_reason", None)
            self._write_state(connection, row, state, "ready")
            self.store._insert_event(
                connection,
                run_id=run_id,
                sequence=self.store._next_event_sequence(connection, run_id),
                event_type="agent.inference_retry_authorized",
                payload={"turn_sequence": row["turn_sequence"], "evidence": redact(evidence)},
                created_at=_now(),
            )

    def record_response(self, run_id: str, turn_sequence: int, message: dict) -> list[dict]:
        with self._transaction() as connection:
            row, state = self._load(connection, run_id)
            turn = connection.execute(
                "SELECT * FROM agent_turns WHERE run_id = ? AND sequence = ?", (run_id, turn_sequence)
            ).fetchone()
            if turn is None or row["turn_sequence"] != turn_sequence:
                raise ValueError("response turn does not match checkpoint")
            if turn["response_ciphertext"] is not None:
                saved = self.cipher.open(self._turn_identity(turn, "response"), turn["response_ciphertext"])
                if saved != message:
                    raise ValueError("response differs from committed response")
            else:
                if row["phase"] != "llm_inflight":
                    raise ValueError("response requires an inflight turn")
                if message.get("role") != "assistant":
                    raise ValueError("response must be an assistant message")
                token = self.cipher.seal(self._turn_identity(turn, "response"), message)
                connection.execute(
                    """UPDATE agent_turns SET response_ciphertext = ?, status = 'responded'
                    WHERE run_id = ? AND sequence = ?""",
                    (token, run_id, turn_sequence),
                )
                for ordinal, call in enumerate(message.get("tool_calls") or []):
                    function = call["function"]
                    if (
                        not isinstance(call.get("id"), str)
                        or not call["id"]
                        or not isinstance(function.get("name"), str)
                        or not function["name"]
                    ):
                        raise ValueError("provider call identity and tool name must be nonempty strings")
                    invocation = {
                        "id": uuid.uuid4().hex,
                        "run_id": run_id,
                        "turn_sequence": turn_sequence,
                        "provider_call_id": call["id"],
                        "ordinal": ordinal,
                        "tool_name": function["name"],
                    }
                    arguments = function["arguments"]
                    digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
                    encrypted = self.cipher.seal(self._invocation_identity(invocation, "arguments"), {"arguments": arguments})
                    connection.execute(
                        """INSERT INTO agent_invocations
                        (id, run_id, turn_sequence, provider_call_id, ordinal, tool_name,
                         arguments_ciphertext, arguments_sha256, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
                        (invocation["id"], run_id, turn_sequence, call["id"], ordinal, function["name"], encrypted, digest),
                    )
                state.setdefault("messages", []).append(message)
                self._write_state(connection, row, state, "dispatching")
        return self.list_invocations(run_id, turn_sequence)

    def _get_invocation(self, connection, invocation_id: str):
        row = connection.execute("SELECT * FROM agent_invocations WHERE id = ?", (invocation_id,)).fetchone()
        if row is None:
            raise ValueError("agent invocation not found")
        return row

    def list_invocations(self, run_id: str, turn_sequence: int | None = None) -> list[dict]:
        with closing(self.store._connect()) as connection:
            self._load(connection, run_id)
            query = "SELECT * FROM agent_invocations WHERE run_id = ?"
            params = [run_id]
            if turn_sequence is not None:
                query += " AND turn_sequence = ?"
                params.append(turn_sequence)
            rows = connection.execute(query + " ORDER BY turn_sequence, ordinal", params).fetchall()
            results = []
            for sequence in {row["turn_sequence"] for row in rows}:
                turn = self._authenticate_turn(connection, run_id, sequence)
                if turn["response_ciphertext"] is None:
                    raise ValueError("invocation is missing its committed response")
            for row in rows:
                arguments = self.cipher.open(self._invocation_identity(row, "arguments"), row["arguments_ciphertext"])
                result = None
                if row["result_ciphertext"] is not None:
                    result = self.cipher.open(self._invocation_identity(row, "result"), row["result_ciphertext"])["output"]
                if row["state"] == "completed" and result is None:
                    raise ValueError("completed invocation is missing its full result")
                results.append(
                    {
                        "id": row["id"],
                        "run_id": row["run_id"],
                        "turn_sequence": row["turn_sequence"],
                        "provider_call_id": row["provider_call_id"],
                        "ordinal": row["ordinal"],
                        "tool_name": row["tool_name"],
                        "arguments": arguments["arguments"],
                        "state": row["state"],
                        "attempt_id": row["current_attempt_id"],
                        "result": result,
                    }
                )
        return results

    def complete_turn(self, run_id: str, state: dict) -> None:
        with self._transaction() as connection:
            row, _ = self._load(connection, run_id)
            if row["phase"] != "dispatching":
                raise ValueError("turn is not dispatching")
            unfinished = connection.execute(
                """SELECT 1 FROM agent_invocations WHERE run_id = ?
                AND turn_sequence = ? AND (state != 'completed' OR result_ciphertext IS NULL)""",
                (run_id, row["turn_sequence"]),
            ).fetchone()
            if unfinished:
                raise ValueError("turn has unfinished invocations")
            turn = connection.execute(
                "SELECT * FROM agent_turns WHERE run_id = ? AND sequence = ?", (run_id, row["turn_sequence"])
            ).fetchone()
            response = self.cipher.open(self._turn_identity(turn, "response"), turn["response_ciphertext"])
            connection.execute(
                "UPDATE agent_turns SET status = 'complete' WHERE run_id = ? AND sequence = ?", (run_id, row["turn_sequence"])
            )
            self._write_state(connection, row, state, "ready" if response.get("tool_calls") else "complete")

    def block(self, run_id: str, reason: str) -> None:
        with self._transaction() as connection:
            row, state = self._load(connection, run_id)
            state["blocked_reason"] = reason
            self._write_state(connection, row, state, "blocked")

    def result_token(self, invocation_id: str, output: str, *, state_updates: dict | None = None) -> bytes:
        if not isinstance(output, str):
            raise TypeError("tool output must be a string")
        updates = state_updates or {}
        if set(updates) - {"todo", "cwd"}:
            raise ValueError("only todo and cwd terminal state updates are allowed")
        if "todo" in updates and not isinstance(updates["todo"], list):
            raise ValueError("todo snapshot must be a list")
        if "cwd" in updates and not isinstance(updates["cwd"], str):
            raise ValueError("cwd snapshot must be a string")
        with closing(self.store._connect()) as connection:
            row = self._get_invocation(connection, invocation_id)
            self._load(connection, row["run_id"])
            return self.cipher.seal(
                self._invocation_identity(row, "result"),
                {"output": output, "message": {"role": "tool", "tool_call_id": row["provider_call_id"], "content": output},
                 "state_updates": updates},
            )

    def _complete(self, connection, invocation, token: bytes, *, reconciled: bool = False) -> None:
        if invocation["state"] not in {"queued", "bound", "blocked"} or invocation["result_ciphertext"] is not None:
            raise ValueError("invocation is already complete")
        payload = self.cipher.open(self._invocation_identity(invocation, "result"), token)
        message = {"role": "tool", "tool_call_id": invocation["provider_call_id"], "content": payload.get("output")}
        if not isinstance(payload.get("output"), str) or payload.get("message") != message:
            raise ValueError("invalid model tool result")
        row, state = self._load(connection, invocation["run_id"])
        if row["phase"] != "dispatching" and not (reconciled and row["phase"] == "blocked"):
            raise ValueError("invocation does not belong to an active turn")
        earlier = connection.execute(
            """SELECT 1 FROM agent_invocations WHERE run_id = ? AND turn_sequence = ?
            AND ordinal < ? AND state != 'completed'""",
            (invocation["run_id"], invocation["turn_sequence"], invocation["ordinal"]),
        ).fetchone()
        if earlier:
            raise ValueError("earlier invocations are unfinished")
        connection.execute(
            "UPDATE agent_invocations SET state = 'completed', result_ciphertext = ? WHERE id = ?", (token, invocation["id"])
        )
        updates = payload.get("state_updates", {})
        if not isinstance(updates, dict) or set(updates) - {"todo", "cwd"}:
            raise ValueError("invalid terminal state updates")
        state.update(updates)
        state.setdefault("messages", []).append(message)
        self._write_state(connection, row, state, row["phase"])

    def complete_without_attempt(self, invocation_id: str, output: str) -> None:
        token = self.result_token(invocation_id, output)
        with self._transaction() as connection:
            invocation = self._get_invocation(connection, invocation_id)
            if invocation["state"] != "queued" or invocation["current_attempt_id"] is not None:
                raise ValueError("only a queued invocation can complete without an attempt")
            self._complete(connection, invocation, token)
            self.store._insert_event(
                connection,
                run_id=invocation["run_id"],
                sequence=self.store._next_event_sequence(connection, invocation["run_id"]),
                event_type="agent.invocation_completed",
                payload={"invocation_id": invocation_id},
                created_at=_now(),
            )

    def _bind_attempt(self, connection, invocation_id, run_id, tool_name, arguments, call_id, timestamp):
        invocation = self._get_invocation(connection, invocation_id)
        row, _ = self._load(connection, run_id)
        if (
            invocation["run_id"] != run_id
            or invocation["tool_name"] != tool_name
            or invocation["state"] != "queued"
            or invocation["current_attempt_id"] is not None
            or row["phase"] != "dispatching"
        ):
            raise ValueError("invocation cannot bind this attempt")
        raw = self.cipher.open(self._invocation_identity(invocation, "arguments"), invocation["arguments_ciphertext"])[
            "arguments"
        ]
        try:
            expected = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as error:
            raise ValueError("invalid invocation arguments") from error
        if expected != arguments:
            raise ValueError("attempt arguments differ from invocation")
        earlier = connection.execute(
            """SELECT 1 FROM agent_invocations WHERE run_id = ? AND turn_sequence = ?
            AND ordinal < ? AND state != 'completed'""",
            (run_id, invocation["turn_sequence"], invocation["ordinal"]),
        ).fetchone()
        if earlier:
            raise ValueError("earlier invocations are unfinished")
        step_id = uuid.uuid4().hex
        sequence = connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM steps WHERE run_id = ?", (run_id,)).fetchone()[
            0
        ]
        connection.execute(
            """INSERT INTO steps
            (id, run_id, sequence, title, status, attempt_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)""",
            (step_id, run_id, sequence, tool_name, timestamp, timestamp),
        )
        # The attempt foreign key is assigned after its ToolCall row has been inserted.
        return step_id

    def _transition_attempt(self, connection, call, status, token, timestamp):
        invocation = self.store._agent_invocation_for_attempt(connection, call["id"])
        if invocation is None:
            if token is not None:
                raise ValueError("result token requires an agent invocation")
            return False
        if invocation["current_attempt_id"] != call["id"]:
            raise ValueError("agent attempt is not bound to its journal invocation")
        checkpoint, _ = self._load(connection, invocation["run_id"])
        self.cipher.open(self._invocation_identity(invocation, "arguments"), invocation["arguments_ciphertext"])
        if status in {ToolCallStatus.RUNNING, ToolCallStatus.WAITING_APPROVAL} and checkpoint["phase"] != "dispatching":
            raise ValueError("blocked checkpoint cannot start a tool")
        if ToolCallStatus(call["status"]) is ToolCallStatus.INTERRUPTED:
            raise ValueError("agent interruption requires journal reconciliation")
        if status is ToolCallStatus.INTERRUPTED:
            if token is not None:
                raise ValueError("interrupted effects cannot have a model result")
            connection.execute("UPDATE agent_invocations SET state = 'blocked' WHERE id = ?", (invocation["id"],))
            row, state = self._load(connection, invocation["run_id"])
            state["blocked_reason"] = "tool outcome is unknown"
            self._write_state(connection, row, state, "blocked")
            return True
        terminal = status in {ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED, ToolCallStatus.TIMED_OUT, ToolCallStatus.CANCELLED}
        if terminal:
            if token is None:
                raise ValueError("durable terminal attempt requires its full result")
            self._complete(connection, invocation, token)
        elif token is not None:
            raise ValueError("only terminal attempts accept a result")
        step_status = {
            ToolCallStatus.RUNNING: StepStatus.RUNNING,
            ToolCallStatus.WAITING_APPROVAL: StepStatus.WAITING_APPROVAL,
            ToolCallStatus.SUCCEEDED: StepStatus.SUCCEEDED,
            ToolCallStatus.FAILED: StepStatus.FAILED,
            ToolCallStatus.TIMED_OUT: StepStatus.FAILED,
            ToolCallStatus.CANCELLED: StepStatus.CANCELLED,
        }.get(status)
        if step_status is not None:
            connection.execute(
                """UPDATE steps SET status = ?, updated_at = ?,
                started_at = CASE WHEN ? = 'running' THEN COALESCE(started_at, ?) ELSE started_at END,
                ended_at = CASE WHEN ? THEN ? ELSE ended_at END WHERE id = ?""",
                (step_status.value, timestamp, status.value, timestamp, terminal, timestamp, call["step_id"]),
            )
        return True

    def reconcile(self, invocation_id: str, decision: str, *, output: str | None = None, evidence: dict) -> dict:
        """Resolve an unknown effect from explicit operator evidence, never by guessing."""
        if decision not in {"confirmed_completed", "confirmed_not_executed", "unresolved"} or not evidence:
            raise ValueError("reconciliation requires a valid decision and evidence")
        if decision == "confirmed_completed" and not isinstance(output, str):
            raise ValueError("confirmed completion requires the full model result")
        if decision != "confirmed_completed" and output is not None:
            raise ValueError("only confirmed completion accepts a model result")
        timestamp = _now()
        with self._transaction() as connection:
            invocation = self._get_invocation(connection, invocation_id)
            row, state = self._load(connection, invocation["run_id"])
            run, call = self.store._recovery_records(connection, invocation["current_attempt_id"])
            if (
                invocation["state"] != "blocked"
                or row["phase"] != "blocked"
                or call.status is not ToolCallStatus.INTERRUPTED
                or run.status is RunStatus.CANCELLED
            ):
                raise ValueError("reconciliation requires a blocked interrupted invocation")
            connection.execute(
                """INSERT INTO reconciliation_evidence
                (id, tool_call_id, decision, evidence_json, created_at) VALUES (?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, call.id, decision, json.dumps(redact(evidence), sort_keys=True), timestamp),
            )
            if decision == "confirmed_completed":
                token = self.cipher.seal(
                    self._invocation_identity(invocation, "result"),
                    {
                        "output": output,
                        "message": {"role": "tool", "tool_call_id": invocation["provider_call_id"], "content": output},
                    },
                )
                self.store._resolve_recovery(
                    connection, run, call, ToolCallStatus.SUCCEEDED, decision, timestamp, reason="operator confirmed completion"
                )
                self._complete(connection, invocation, token, reconciled=True)
                connection.execute(
                    """UPDATE steps SET status = 'succeeded', updated_at = ?, ended_at = ? WHERE id = ?""",
                    (timestamp, timestamp, call.step_id),
                )
            elif decision == "confirmed_not_executed":
                self.store._resolve_recovery(
                    connection, run, call, ToolCallStatus.FAILED, decision, timestamp, reason="operator confirmed no execution"
                )
                retry_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO tool_calls (
                    id, run_id, retry_of, tool_name, arguments_json, arguments_replayable, risk_level,
                    execution_kind, idempotent, idempotency_key, status, attempt, timeout_seconds,
                    created_at, updated_at, step_id
                ) SELECT ?, run_id, id, tool_name, arguments_json, arguments_replayable, risk_level,
                    execution_kind, idempotent, idempotency_key, 'waiting_approval', attempt + 1,
                    timeout_seconds, ?, ?, step_id FROM tool_calls WHERE id = ?""",
                    (retry_id, timestamp, timestamp, call.id),
                )
                connection.execute(
                    """UPDATE agent_invocations SET state = 'bound', current_attempt_id = ? WHERE id = ?""",
                    (retry_id, invocation_id),
                )
                connection.execute(
                    """UPDATE steps SET status = 'waiting_approval', attempt_count = attempt_count + 1,
                    updated_at = ?, ended_at = NULL WHERE id = ?""",
                    (timestamp, call.step_id),
                )
                connection.execute(
                    "UPDATE runs SET status = 'waiting_approval', updated_at = ? WHERE id = ?", (timestamp, run.id)
                )
                approval_id = uuid.uuid4().hex
                summary = self.store._approval_summary_from_stored_arguments(
                    call.tool_name,
                    connection.execute("SELECT arguments_json FROM tool_calls WHERE id = ?", (call.id,)).fetchone()[0],
                )
                connection.execute(
                    """INSERT INTO approvals
                    (id, tool_call_id, status, tool_name, arguments_summary, workspace, risk_reason, requested_at, attempt)
                    VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
                    (
                        approval_id,
                        retry_id,
                        call.tool_name,
                        summary,
                        run.workspace,
                        "operator reconciled retry requires fresh approval",
                        timestamp,
                        call.attempt + 1,
                    ),
                )
                for event_type, payload in [
                    ("tool.retry_scheduled", {"tool_call_id": retry_id, "retry_of": call.id, "attempt": call.attempt + 1}),
                    (
                        "tool.created",
                        {"tool_call_id": retry_id, "retry_of": call.id, "attempt": call.attempt + 1, "tool_name": call.tool_name},
                    ),
                    ("approval.requested", {"tool_call_id": retry_id, "approval_id": approval_id}),
                ]:
                    self.store._insert_event(
                        connection,
                        run_id=run.id,
                        sequence=self.store._next_event_sequence(connection, run.id),
                        event_type=event_type,
                        payload=payload,
                        created_at=timestamp,
                    )
            if decision != "unresolved":
                row, state = self._load(connection, run.id)
                state.pop("blocked_reason", None)
                self._write_state(connection, row, state, "dispatching")
            self.store._insert_event(
                connection,
                run_id=run.id,
                sequence=self.store._next_event_sequence(connection, run.id),
                event_type="agent.reconciled",
                payload={"invocation_id": invocation_id, "decision": decision},
                created_at=timestamp,
            )
        return next(item for item in self.list_invocations(run.id) if item["id"] == invocation_id)


class RunLease:
    """A nonblocking OS lock. This provides ownership on one host only."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        self.path = Path(workspace) / ".reliagent" / "locks" / f"{digest}.lock"
        self._file = None
        self._pid = None

    def acquire(self) -> None:
        if self._file is not None:
            raise ValueError("run lease is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise ValueError("run is already owned by another executor") from error
        self._file = handle
        self._pid = os.getpid()

    def release(self) -> None:
        if self._file is None:
            return
        if self._pid != os.getpid():
            raise ValueError("run lease belongs to another process")
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._pid = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
