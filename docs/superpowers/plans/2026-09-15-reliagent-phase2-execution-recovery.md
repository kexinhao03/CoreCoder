# ReliAgent Phase 2 Execution and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement durable approval, controlled subprocess execution, bounded retry, Run cancellation, and conservative restart recovery on top of the Phase 1 runtime store.

**Architecture:** Add a standalone `RuntimeExecutor` that coordinates a deny-safe tool policy registry, purpose-specific SQLite transactions, and a managed process runner. The existing Agent loop remains unchanged; one Run reserves one ToolCall slot at a time, and recovery reports decisions without automatically executing work.

**Tech Stack:** Python 3.10 standard library, `dataclasses`, `enum`, `sqlite3`, `subprocess`, `threading`, `signal`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-15-reliagent-phase2-execution-recovery-design.md`

## Global Constraints

- Python minimum version remains 3.10; do not use `enum.StrEnum` or Python 3.11-only APIs.
- Do not modify `corecoder.agent.Agent`, `corecoder.permissions.Permission`, or the current `BashTool` execution path in this phase.
- Do not add a third-party runtime dependency or ORM.
- Treat `created`, `waiting_approval`, and `running` ToolCalls as occupying the single Phase 2 execution slot for a Run.
- Unknown tools default to non-idempotent `external_effect` and require approval.
- Permanent approval, distributed workers, generic exactly-once, and forced Python-thread termination are out of scope.
- Persist a ToolCall as `running` before invoking an OS process or in-process callable.
- Every state change and its audit Event must commit in one SQLite transaction.
- Never persist raw environment variables or raw tool arguments in Event payloads.
- An `interrupted` ToolCall must first resolve to `succeeded`, `failed`, or `cancelled`; re-execution creates a new attempt.
- Automatic retry is limited to read-only, idempotent policies with an explicitly retryable `FailureKind` and an unexhausted attempt budget.
- Use `sys.executable -c ...` for subprocess tests so tests do not depend on a shell or network.
- Preserve and separately report the two pre-existing full-repository Ruff findings.

---

## File Map

| File | Responsibility |
|---|---|
| `corecoder/runtime/policies.py` | Failure classification, immutable ToolPolicy, deny-safe registry |
| `corecoder/runtime/approvals.py` | Approval enums/record and bounded argument summary |
| `corecoder/runtime/processes.py` | Cross-platform subprocess lifecycle and result mapping |
| `corecoder/runtime/executor.py` | Approval gate, execution, retry, and cancellation coordination |
| `corecoder/runtime/recovery.py` | Startup scan, recovery candidates, and explicit reconciliation |
| `corecoder/runtime/store.py` | Approval schema and purpose-specific atomic transactions/queries |
| `corecoder/runtime/__init__.py` | Intentional Phase 2 public API |
| `tests/runtime/conftest.py` | Shared running Run fixture |
| `tests/runtime/test_policies.py` | Policy defaults and validation |
| `tests/runtime/test_approvals.py` | Summary redaction and Approval transactions |
| `tests/runtime/test_processes.py` | Process exit/output/timeout/cancellation/process-group behavior |
| `tests/runtime/test_executor.py` | No-approval-no-spawn, execution, retry, Run cancellation |
| `tests/runtime/test_recovery.py` | Restart classification and replay-safety |
| `tests/runtime/test_fault_injection.py` | Rollback and crash-boundary proofs |
| `tests/runtime/test_public_api.py` | Stable Phase 1 and Phase 2 import surface |

## Task 1: Define Tool Policies and Failure Classification

**Files:**

- Create: `corecoder/runtime/policies.py`
- Create: `tests/runtime/test_policies.py`

**Interfaces:**

- Produces: `FailureKind`, `ToolPolicy`, `ToolPolicyRegistry`, `requires_approval`.
- Consumes: `ExecutionKind` and `RiskLevel` from `corecoder.runtime.state`.

- [ ] **Step 1: Write failing policy tests**

```python
import pytest

from corecoder.runtime.policies import FailureKind, ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.state import ExecutionKind, RiskLevel


def test_unknown_tool_uses_deny_safe_policy():
    policy = ToolPolicyRegistry().resolve("unregistered")
    assert policy.risk_level is RiskLevel.EXTERNAL_EFFECT
    assert policy.execution_kind is ExecutionKind.IN_PROCESS
    assert policy.idempotent is False
    assert policy.auto_retry is False
    assert policy.requires_approval is True


def test_builtin_reads_are_automatic_but_bash_requires_approval():
    registry = ToolPolicyRegistry.with_builtin_defaults()
    assert registry.resolve("read_file").requires_approval is False
    assert registry.resolve("grep").requires_approval is False
    assert registry.resolve("bash").requires_approval is True
    assert registry.resolve("bash").execution_kind is ExecutionKind.SUBPROCESS


def test_auto_retry_requires_read_only_idempotent_policy():
    with pytest.raises(ValueError, match="auto_retry requires read-only idempotent policy"):
        ToolPolicy(
            risk_level=RiskLevel.MUTATING,
            execution_kind=ExecutionKind.SUBPROCESS,
            timeout_seconds=10,
            max_attempts=2,
            idempotent=True,
            auto_retry=True,
            retryable_failures=frozenset({FailureKind.TIMED_OUT}),
            output_limit=1000,
        )
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_policies.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'corecoder.runtime.policies'`.

- [ ] **Step 3: Implement the policy contracts**

```python
class FailureKind(str, Enum):
    NONZERO_EXIT = "nonzero_exit"
    TIMED_OUT = "timed_out"
    SPAWN_ERROR = "spawn_error"
    EXECUTION_ERROR = "execution_error"
    CANCELLED = "cancelled"
    TERMINATION_UNKNOWN = "termination_unknown"


@dataclass(frozen=True)
class ToolPolicy:
    risk_level: RiskLevel
    execution_kind: ExecutionKind
    timeout_seconds: int
    max_attempts: int
    idempotent: bool
    auto_retry: bool
    retryable_failures: frozenset[FailureKind]
    output_limit: int

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.output_limit <= 0:
            raise ValueError("output_limit must be positive")
        if self.auto_retry and (
            not self.idempotent or self.risk_level is not RiskLevel.READ_ONLY
        ):
            raise ValueError(
                "auto_retry requires read-only idempotent policy"
            )

    @property
    def requires_approval(self) -> bool:
        return self.risk_level is not RiskLevel.READ_ONLY
```

`ToolPolicyRegistry` copies an optional mapping on construction. Its default
policy is `EXTERNAL_EFFECT`, `IN_PROCESS`, timeout 30, one attempt,
non-idempotent, no retry, and output limit 15,000. `with_builtin_defaults()`
registers `read_file`, `glob`, and `grep` as read-only in-process policies;
`write_file` and `edit_file` as mutating in-process policies; and `bash` as a
mutating subprocess policy. All built-ins default to one attempt and no retry.

- [ ] **Step 4: Run policy tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_policies.py -v`

Expected: 3 passed.

Run: `.venv/bin/python -m ruff check corecoder/runtime/policies.py tests/runtime/test_policies.py`

Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add corecoder/runtime/policies.py tests/runtime/test_policies.py
git commit -m "feat(runtime): define deny-safe tool policies"
```

## Task 2: Define Durable Approval Records and Safe Summaries

**Files:**

- Create: `corecoder/runtime/approvals.py`
- Create: `tests/runtime/test_approvals.py`
- Create: `tests/runtime/conftest.py`

**Interfaces:**

- Produces: `ApprovalStatus`, `ApprovalDecision`, `ApprovalRecord`, `summarize_arguments()`.
- Consumes: JSON-serializable tool arguments.

- [ ] **Step 1: Write failing model and summary tests**

```python
from dataclasses import FrozenInstanceError

import pytest

from corecoder.runtime.approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    summarize_arguments,
)


def test_approval_record_is_frozen():
    approval = ApprovalRecord(
        id="approval-1", tool_call_id="call-1",
        status=ApprovalStatus.PENDING, decision=None,
        tool_name="bash", arguments_summary='{"argv": ["pytest"]}',
        workspace="/tmp/work", risk_reason="runs a command",
        requested_at="t0", resolved_at=None,
    )
    with pytest.raises(FrozenInstanceError):
        approval.status = ApprovalStatus.APPROVED


def test_argument_summary_redacts_sensitive_keys_and_is_bounded():
    summary = summarize_arguments({
        "argv": ["client", "--verbose"],
        "api_key": "secret-value",
        "nested": {"password": "hidden", "text": "x" * 2000},
    })
    assert "secret-value" not in summary
    assert "hidden" not in summary
    assert "[REDACTED]" in summary
    assert len(summary) == 1000


def test_approval_values_are_stable():
    assert ApprovalStatus.PENDING.value == "pending"
    assert ApprovalDecision.ALLOW_ONCE.value == "allow_once"
```

Create `tests/runtime/conftest.py` with a `running_store(tmp_path)` fixture that
initializes a Store, creates `run-1`, and transitions it to `RunStatus.RUNNING`.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_approvals.py -v`

Expected: import fails because `corecoder.runtime.approvals` does not exist.

- [ ] **Step 3: Implement approval records and recursive key redaction**

```python
class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    tool_call_id: str
    status: ApprovalStatus
    decision: ApprovalDecision | None
    tool_name: str
    arguments_summary: str
    workspace: str
    risk_reason: str
    requested_at: str
    resolved_at: str | None
```

`summarize_arguments(arguments)` recursively replaces values whose lowercase key
contains `api_key`, `apikey`, `token`, `password`, or `secret` with
`"[REDACTED]"`; serializes with `json.dumps(..., sort_keys=True,
ensure_ascii=False, default=str)`; and returns at most the first 1,000
characters. It never mutates the caller's dictionary.

- [ ] **Step 4: Run tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_approvals.py -v`

Expected: 3 passed.

Run: `.venv/bin/python -m ruff check corecoder/runtime/approvals.py tests/runtime/test_approvals.py tests/runtime/conftest.py`

Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add corecoder/runtime/approvals.py tests/runtime/test_approvals.py tests/runtime/conftest.py
git commit -m "feat(runtime): define durable approval records"
```

## Task 3: Persist Approval Requests and Decisions Atomically

**Files:**

- Modify: `corecoder/runtime/store.py`
- Modify: `tests/runtime/test_approvals.py`
- Modify: `tests/runtime/test_store.py`

**Interfaces:**

- Produces: `request_approval()`, `resolve_approval()`, `get_approval()`,
  `get_approval_for_tool_call()`, and active-call admission checks.
- Consumes: Task 2 Approval enums and record.

- [ ] **Step 1: Write failing approval transaction tests**

```python
def create_mutating_call(store, call_id="call-1"):
    return store.create_tool_call(
        run_id="run-1", tool_name="bash", arguments={"argv": ["pytest"]},
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.SUBPROCESS, idempotent=False,
        idempotency_key=None, timeout_seconds=30, tool_call_id=call_id,
    )


def test_request_approval_updates_run_call_and_event(running_store):
    create_mutating_call(running_store)
    approval = running_store.request_approval(
        "call-1", arguments_summary='{"argv": ["pytest"]}',
        workspace="/tmp/work", risk_reason="runs a command",
        approval_id="approval-1",
    )
    assert approval.status is ApprovalStatus.PENDING
    assert running_store.get_run("run-1").status is RunStatus.WAITING_APPROVAL
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.WAITING_APPROVAL
    assert running_store.list_events("run-1")[-1].payload["approval_id"] == "approval-1"


def test_allow_once_resolves_approval_but_does_not_start_call(running_store):
    create_mutating_call(running_store)
    running_store.request_approval(
        "call-1", arguments_summary="pytest", workspace="/tmp/work",
        risk_reason="runs a command", approval_id="approval-1",
    )
    resolved = running_store.resolve_approval(
        "approval-1", ApprovalDecision.ALLOW_ONCE
    )
    assert resolved.status is ApprovalStatus.APPROVED
    assert running_store.get_run("run-1").status is RunStatus.RUNNING
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.WAITING_APPROVAL


def test_denial_cancels_call_and_duplicate_resolution_is_atomic(running_store):
    create_mutating_call(running_store)
    running_store.request_approval(
        "call-1", arguments_summary="pytest", workspace="/tmp/work",
        risk_reason="runs a command", approval_id="approval-1",
    )
    running_store.resolve_approval("approval-1", ApprovalDecision.DENY)
    before = running_store.list_events("run-1")
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.CANCELLED
    with pytest.raises(ValueError, match="approval is already resolved"):
        running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    assert running_store.list_events("run-1") == before
```

Add a Store test that creates one `created` ToolCall and asserts a second active
call for the same Run raises `ValueError("run already has an active tool call")`.
Add another that cancels the Run and asserts creation raises
`ValueError("run cannot accept tool calls: cancelled")`.

Also extend `test_initialize_creates_runtime_tables` to require
`{"runs", "tool_calls", "approvals", "events"}`.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_approvals.py tests/runtime/test_store.py -v`

Expected: failures report missing Store approval methods and missing admission guard.

- [ ] **Step 3: Add the Approval table and row conversion**

Add to `initialize()`:

```sql
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

CREATE INDEX IF NOT EXISTS idx_approvals_tool_call_id
    ON approvals(tool_call_id);
```

Add `_approval_from_row(row)`, `get_approval(id)`, and
`get_approval_for_tool_call(call_id)`. The latter returns `None` when no Approval
exists. Missing explicit-ID getters continue to raise `KeyError`.

- [ ] **Step 4: Implement purpose-specific approval transactions**

Both methods open their own connection, use `BEGIN IMMEDIATE`, validate current
states before writing, compute one next Event sequence, commit, and roll back every
exception. `request_approval` inserts the pending row, updates Run and ToolCall,
and writes `approval.requested` with only `approval_id`, `tool_call_id`,
`tool_name`, and `risk_reason`. `resolve_approval` updates the Approval and Run;
denial also updates ToolCall status, `updated_at`, and `ended_at`; it writes
`approval.resolved` with the Store-owned IDs and decision.

Add private `_next_event_sequence(connection, run_id)` and `_insert_event(...)`
helpers, then route new transactions through them. Do not refactor Phase 1 methods
in this task beyond replacing exact duplicate sequence/insert code where tests
prove no behavior change.

- [ ] **Step 5: Enforce Store admission in `create_tool_call`**

Inside its existing `BEGIN IMMEDIATE`, load the Run and reject missing Runs. Reject
terminal, paused, waiting-approval, or recoverable Runs with
`"run cannot accept tool calls: <status>"`; allow `created` for Phase 1-compatible
pre-execution reservation and `running` for RuntimeExecutor. Query for another
ToolCall in `created`, `waiting_approval`, or `running`; exclude the retry source
only after it is terminal. Reject a match with
`"run already has an active tool call"`.

- [ ] **Step 6: Run tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_approvals.py tests/runtime/test_store.py -v`

Expected: all selected tests pass.

Run: `.venv/bin/python -m ruff check corecoder/runtime tests/runtime`

Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add corecoder/runtime/store.py tests/runtime/test_approvals.py tests/runtime/test_store.py
git commit -m "feat(runtime): persist approval decisions atomically"
```

## Task 4: Run Subprocesses and Bound Their Results

**Files:**

- Create: `corecoder/runtime/processes.py`
- Create: `tests/runtime/test_processes.py`

**Interfaces:**

- Produces: `ProcessSpec`, `ProcessResult`, `ManagedProcessRunner.run()`.
- Consumes: `FailureKind` from Task 1.

- [ ] **Step 1: Write failing normal-exit, truncation, and spawn-error tests**

```python
import sys
import threading

from corecoder.runtime.policies import FailureKind
from corecoder.runtime.processes import ManagedProcessRunner, ProcessSpec


def test_process_runner_captures_success(tmp_path):
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", "print('hello')"), cwd=tmp_path,
            timeout_seconds=2, output_limit=1000, termination_grace_seconds=0.2,
        ),
        threading.Event(),
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.failure_kind is None
    assert result.termination_confirmed is True


def test_process_runner_bounds_stdout_and_stderr(tmp_path):
    code = "import sys; print('a' * 200); print('b' * 200, file=sys.stderr)"
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", code), cwd=tmp_path,
            timeout_seconds=2, output_limit=50, termination_grace_seconds=0.2,
        ),
        threading.Event(),
    )
    assert len(result.stdout) <= 50
    assert len(result.stderr) <= 50


def test_process_runner_classifies_spawn_error(tmp_path):
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(str(tmp_path / "missing-command"),), cwd=tmp_path,
            timeout_seconds=2, output_limit=1000, termination_grace_seconds=0.2,
        ),
        threading.Event(),
    )
    assert result.exit_code is None
    assert result.failure_kind is FailureKind.SPAWN_ERROR
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_processes.py -v`

Expected: import fails because `corecoder.runtime.processes` does not exist.

- [ ] **Step 3: Implement immutable process input/output models**

Add `from __future__ import annotations` at the top of `processes.py` so the
annotations remain valid on the project's supported Python versions.
`ProcessSpec` is frozen with `argv: tuple[str, ...]`, `cwd: Path`,
`timeout_seconds: float`, `output_limit: int`,
`termination_grace_seconds: float`, and `environment: dict[str, str] | None = None`.
Its `__post_init__` rejects empty argv, non-positive timeout/output limit, and
negative grace period. `ProcessResult` is frozen with `exit_code`, `stdout`,
`stderr`, `duration_seconds`, `failure_kind`, and `termination_confirmed`.

- [ ] **Step 4: Implement normal execution and spawn-error mapping**

Use `subprocess.Popen` with `shell=False`, binary pipes, `cwd`, and the explicit
environment when present. POSIX passes `start_new_session=True`; Windows passes
`creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`. Call `communicate()` through
the polling loop introduced in Task 5; decode bytes with UTF-8 replacement and
truncate each stream with a shared `_bound_output(text, limit)` function that
keeps the first 60% and last 40% with a truncation marker. A spawn `OSError`
returns `SPAWN_ERROR`; exit zero has no failure; nonzero maps to `NONZERO_EXIT`.

- [ ] **Step 5: Run tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_processes.py -v`

Expected: 3 passed.

Run: `.venv/bin/python -m ruff check corecoder/runtime/processes.py tests/runtime/test_processes.py`

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add corecoder/runtime/processes.py tests/runtime/test_processes.py
git commit -m "feat(runtime): add managed subprocess results"
```

## Task 5: Enforce Timeout, Cancellation, and Process-Group Termination

**Files:**

- Modify: `corecoder/runtime/processes.py`
- Modify: `tests/runtime/test_processes.py`

**Interfaces:**

- Extends: `ManagedProcessRunner.run(spec, cancel_event)` from Task 4.
- Produces: confirmed `TIMED_OUT`/`CANCELLED` or conservative
  `TERMINATION_UNKNOWN`.

- [ ] **Step 1: Write failing timeout and pre-cancel tests**

```python
def test_timeout_is_bounded_and_confirmed(tmp_path):
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(10)"),
            cwd=tmp_path, timeout_seconds=0.1, output_limit=1000,
            termination_grace_seconds=0.1,
        ),
        threading.Event(),
    )
    assert result.failure_kind is FailureKind.TIMED_OUT
    assert result.termination_confirmed is True
    assert result.duration_seconds < 2


def test_pre_cancelled_request_never_spawns(tmp_path):
    marker = tmp_path / "started"
    cancelled = threading.Event()
    cancelled.set()
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),
            cwd=tmp_path, timeout_seconds=2, output_limit=1000,
            termination_grace_seconds=0.1,
        ),
        cancelled,
    )
    assert result.failure_kind is FailureKind.CANCELLED
    assert marker.exists() is False
```

- [ ] **Step 2: Write a POSIX-only descendant termination test**

```python
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_timeout_kills_descendant_process(tmp_path):
    survived = tmp_path / "child-survived"
    child = (
        "import time; "
        "time.sleep(0.5); "
        f"open({str(survived)!r}, 'w').close()"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(10)"
    )
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", parent), cwd=tmp_path,
            timeout_seconds=0.1, output_limit=1000,
            termination_grace_seconds=0.1,
        ),
        threading.Event(),
    )
    time.sleep(0.7)
    assert result.failure_kind is FailureKind.TIMED_OUT
    assert survived.exists() is False
```

This marker proves the descendant did not outlive the timed-out parent. Never use
a shell command or fixed external binary in this test.

- [ ] **Step 3: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_processes.py -v`

Expected: timeout/cancellation tests fail because Task 4 does not yet poll or
terminate a process group.

- [ ] **Step 4: Implement polling and two-stage termination**

Before `Popen`, return `CANCELLED` when `cancel_event.is_set()`. After spawn, use
`time.monotonic()` and retry `process.communicate(timeout=min(0.05, remaining))`.
On timeout or cancellation, call `_terminate(process, grace)`:

```python
def _terminate(process: subprocess.Popen, grace: float) -> bool:
    if process.poll() is not None:
        return True
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        try:
            process.wait(timeout=max(grace, 0.1))
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None
```

Ignore `ProcessLookupError` only when the process already exited. After termination,
call `communicate()` once to drain pipes. Confirmed timeout returns `TIMED_OUT`;
confirmed cancellation returns `CANCELLED`; failure to confirm returns
`TERMINATION_UNKNOWN` with exit code `None`.

- [ ] **Step 5: Run process tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_processes.py -v`

Expected: all process tests pass; the POSIX group test is skipped only on Windows.

Run: `.venv/bin/python -m ruff check corecoder/runtime/processes.py tests/runtime/test_processes.py`

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add corecoder/runtime/processes.py tests/runtime/test_processes.py
git commit -m "feat(runtime): terminate timed-out process groups"
```

## Task 6: Gate and Execute Subprocess ToolCalls

**Files:**

- Create: `corecoder/runtime/executor.py`
- Create: `tests/runtime/test_executor.py`
- Modify: `corecoder/runtime/store.py`

**Interfaces:**

- Produces: `RuntimeExecutor`, `PendingApproval`, `RuntimeResult`,
  `ExecutionRefused`, `submit_subprocess()`, and `execute_approved_subprocess()`.
- Consumes: Store, registry, Approval, ProcessSpec, ManagedProcessRunner.

`RuntimeExecutor` uses this constructor; omitting the runner creates a real
`ManagedProcessRunner`, while tests inject a spy or sequence runner:

```python
def __init__(
    self,
    store: RuntimeStore,
    policies: ToolPolicyRegistry,
    process_runner: ManagedProcessRunner | None = None,
) -> None:
    self._store = store
    self._policies = policies
    self._process_runner = process_runner or ManagedProcessRunner()
```

- [ ] **Step 1: Write a spy runner and failing approval-gate tests**

```python
class SpyRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, spec, cancel_event):
        self.calls.append(spec)
        return self.result


def test_mutating_submission_persists_approval_without_spawning(running_store):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    pending = executor.submit_subprocess(
        "run-1", "bash", (sys.executable, "-c", "print('no')"),
        tool_call_id="call-1", approval_id="approval-1",
    )
    assert isinstance(pending, PendingApproval)
    assert pending.call.status is ToolCallStatus.WAITING_APPROVAL
    assert runner.calls == []


def test_approval_for_another_call_cannot_authorize_execution(running_store):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    executor.submit_subprocess(
        "run-1", "bash", (sys.executable, "-c", "print('no')"),
        tool_call_id="call-1", approval_id="approval-1",
    )
    with pytest.raises(ExecutionRefused, match="approved allow-once decision required"):
        executor.execute_approved_subprocess("call-other")
    assert runner.calls == []
```

Define the helper exactly as:

```python
def success_result(text):
    return ProcessResult(
        exit_code=0, stdout=text, stderr="", duration_seconds=0.01,
        failure_kind=None, termination_confirmed=True,
    )
```

- [ ] **Step 2: Write failing automatic and approved execution tests**

```python
def probe_policy(**overrides):
    values = {
        "risk_level": RiskLevel.READ_ONLY,
        "execution_kind": ExecutionKind.SUBPROCESS,
        "timeout_seconds": 2,
        "max_attempts": 1,
        "idempotent": True,
        "auto_retry": False,
        "retryable_failures": frozenset(),
        "output_limit": 1000,
    }
    values.update(overrides)
    return ToolPolicy(**values)


def test_read_only_subprocess_executes_and_persists_success(running_store):
    runner = SpyRunner(success_result("ok"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}), runner
    )
    result = executor.submit_subprocess(
        "run-1", "probe", (sys.executable, "-c", "print('ok')"),
        tool_call_id="call-1",
    )
    assert result.call.status is ToolCallStatus.SUCCEEDED
    assert result.output == "ok"
    assert len(runner.calls) == 1
    assert [event.type for event in running_store.list_events("run-1")][-2:] == [
        "tool.started", "tool.completed"
    ]


def test_approved_execution_uses_persisted_argv(running_store):
    runner = SpyRunner(success_result("ok"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    argv = (sys.executable, "-c", "print('approved')")
    executor.submit_subprocess(
        "run-1", "bash", argv, tool_call_id="call-1",
        approval_id="approval-1",
    )
    running_store.resolve_approval("approval-1", ApprovalDecision.ALLOW_ONCE)
    executor.execute_approved_subprocess("call-1")
    assert runner.calls[0].argv == argv
```

- [ ] **Step 3: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_executor.py -v`

Expected: import fails because `corecoder.runtime.executor` does not exist.

- [ ] **Step 4: Add `record_event()` and approved-call lookup support**

`record_event(run_id, event_type, payload)` uses `BEGIN IMMEDIATE`, allocates the
next sequence, inserts one Event, commits, and returns the `EventRecord`.
`get_approval_for_tool_call` from Task 3 is the only approval lookup used by the
executor; no caller-supplied Approval object is trusted.

- [ ] **Step 5: Implement executor result types and submission**

```python
@dataclass(frozen=True)
class PendingApproval:
    call: ToolCallRecord
    approval: ApprovalRecord


@dataclass(frozen=True)
class RuntimeResult:
    call: ToolCallRecord
    output: str
    failure_kind: FailureKind | None


class ExecutionRefused(RuntimeError):
    pass
```

`submit_subprocess(run_id, tool_name, argv, *, tool_call_id=None,
approval_id=None)` requires a running Run and a subprocess policy, persists
arguments as `{"argv": list(argv)}`, derives a SHA-256 idempotency key only when
the policy is idempotent, and creates the ToolCall. A policy requiring approval
calls `request_approval` with `summarize_arguments` and returns
`PendingApproval`; a read-only policy calls the private execution method.

- [ ] **Step 6: Implement approved execution and result mapping**

`execute_approved_subprocess(call_id)` loads the persisted Approval and requires
`APPROVED/ALLOW_ONCE`. It accepts no replacement argv. `_execute_subprocess(call)`
transitions to running, registers a cancellation Event, constructs `ProcessSpec`
from persisted argv, Run workspace, and policy, calls the runner, removes the
active registration, then maps:

| Process result | ToolCall state | Event |
|---|---|---|
| exit 0 | `succeeded` | `tool.completed` |
| `TIMED_OUT` | `timed_out` | `tool.timed_out` |
| `CANCELLED` | `cancelled` | `tool.cancelled` |
| `TERMINATION_UNKNOWN` | `interrupted` | `tool.interrupted` |
| other failure | `failed` | `tool.failed` |

Build bounded output from stdout and stderr; persist it through
`transition_tool_call(result_summary=...)`; include exit code and failure kind in
the Event payload. Return `RuntimeResult` with the persisted terminal record.

- [ ] **Step 7: Run executor tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_executor.py tests/runtime/test_approvals.py -v`

Expected: all selected tests pass.

Run: `.venv/bin/python -m ruff check corecoder/runtime tests/runtime`

Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add corecoder/runtime/executor.py corecoder/runtime/store.py tests/runtime/test_executor.py
git commit -m "feat(runtime): gate subprocess execution on approval"
```

## Task 7: Add Cooperative In-process Execution and Bounded Retry

**Files:**

- Modify: `corecoder/runtime/executor.py`
- Modify: `tests/runtime/test_executor.py`

**Interfaces:**

- Produces: `submit_in_process()`, `execute_approved_in_process()`, automatic
  retry scheduling for safe failures.
- Consumes: persisted ToolCall arguments and Task 6 result mapping.

- [ ] **Step 1: Write failing in-process tests**

```python
def test_in_process_operation_receives_persisted_arguments(running_store):
    seen = []

    def operation(arguments, cancel_event):
        seen.append((arguments, cancel_event.is_set()))
        return "inspected"

    registry = ToolPolicyRegistry({
        "inspect": ToolPolicy(
            risk_level=RiskLevel.READ_ONLY,
            execution_kind=ExecutionKind.IN_PROCESS,
            timeout_seconds=2, max_attempts=1, idempotent=True,
            auto_retry=False, retryable_failures=frozenset(), output_limit=1000,
        )
    })
    result = RuntimeExecutor(running_store, registry).submit_in_process(
        "run-1", "inspect", {"path": "README.md"}, operation,
        tool_call_id="call-1",
    )
    assert result.call.status is ToolCallStatus.SUCCEEDED
    assert seen == [({"path": "README.md"}, False)]


def test_in_process_exception_is_a_failed_execution(running_store):
    def operation(arguments, cancel_event):
        raise RuntimeError("boom")

    executor = RuntimeExecutor(running_store, inspect_registry())
    result = executor.submit_in_process(
        "run-1", "inspect", {}, operation, tool_call_id="call-1"
    )
    assert result.call.status is ToolCallStatus.FAILED
    assert result.failure_kind is FailureKind.EXECUTION_ERROR
```

Add `inspect_registry()` as a test helper returning the exact read-only policy
shown above. Add a mutating in-process policy test that asserts submission returns
`PendingApproval`; after `resolve_approval(..., ALLOW_ONCE)`, call
`execute_approved_in_process("call-1", operation)` and assert one invocation. Name
the test `test_blocking_in_process_callable_has_no_forced_timeout` and use a
non-blocking callable so the test documents the contract without hanging.

- [ ] **Step 2: Write failing retry tests with the SpyRunner**

```python
def test_retryable_timeout_creates_a_new_attempt(running_store):
    timed_out = ProcessResult(
        exit_code=None, stdout="", stderr="timeout", duration_seconds=0.1,
        failure_kind=FailureKind.TIMED_OUT, termination_confirmed=True,
    )
    runner = SequenceRunner([timed_out, success_result("ok")])
    policy = probe_policy(
        max_attempts=2, auto_retry=True,
        retryable_failures=frozenset({FailureKind.TIMED_OUT}),
    )
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": policy}), runner
    )
    result = executor.submit_subprocess(
        "run-1", "probe", (sys.executable, "-c", "print('ok')"),
        tool_call_id="call-1",
    )
    assert len(runner.calls) == 2
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.TIMED_OUT
    assert result.call.attempt == 2
    assert result.call.retry_of == "call-1"
    assert "tool.retry_scheduled" in [
        event.type for event in running_store.list_events("run-1")
    ]
```

`SequenceRunner` pops one `ProcessResult` per call and records each `ProcessSpec`.
Add `test_nonzero_exit_is_not_retried` with `retryable_failures={TIMED_OUT}` and
`test_retry_stops_at_max_attempts` with two timeout results; assert runner call
counts are one and two respectively.

- [ ] **Step 3: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_executor.py -v`

Expected: failures report missing in-process methods and no retry scheduling.

- [ ] **Step 4: Implement cooperative in-process execution**

Persist before invocation, check the per-call cancellation Event before and after
the callable, and pass the Event to the callable. Catch `Exception` but not
`BaseException`; persist `EXECUTION_ERROR` as failed. A pre-cancelled call settles
as cancelled without invocation. Do not create a thread or claim a hard timeout.

- [ ] **Step 5: Implement retry decision and new-attempt creation**

After a subprocess settles as `failed` or `timed_out`, `_should_retry` verifies all
five policy conditions from the design. When true, call `record_event` with
`tool.retry_scheduled`, create a new ToolCall using the source metadata and
`retry_of`, then execute that new record. Use a loop, not recursion, and stop when
the returned result is not retryable or the attempt reaches `max_attempts`.

- [ ] **Step 6: Run executor tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_executor.py -v`

Expected: all executor tests pass.

Run: `.venv/bin/python -m ruff check corecoder/runtime/executor.py tests/runtime/test_executor.py`

Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add corecoder/runtime/executor.py tests/runtime/test_executor.py
git commit -m "feat(runtime): retry bounded idempotent failures"
```

## Task 8: Cancel Runs Without Starting New Work

**Files:**

- Modify: `corecoder/runtime/store.py`
- Modify: `corecoder/runtime/executor.py`
- Modify: `tests/runtime/test_executor.py`

**Interfaces:**

- Produces: `SQLiteStore.cancel_run()` and `RuntimeExecutor.cancel_run()`.
- Consumes: the active-call admission guard and executor cancellation Events.

- [ ] **Step 1: Write failing queued-call cancellation test**

```python
def test_cancel_run_settles_waiting_call_without_spawn(running_store):
    runner = SpyRunner(success_result("unused"))
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry.with_builtin_defaults(), runner
    )
    executor.submit_subprocess(
        "run-1", "bash", (sys.executable, "-c", "print('no')"),
        tool_call_id="call-1", approval_id="approval-1",
    )
    executor.cancel_run("run-1")
    assert running_store.get_run("run-1").status is RunStatus.CANCELLED
    assert running_store.get_tool_call("call-1").status is ToolCallStatus.CANCELLED
    assert runner.calls == []
    with pytest.raises(ExecutionRefused, match="run cannot accept work: cancelled"):
        executor.submit_subprocess(
            "run-1", "bash", (sys.executable, "-c", "print('no')")
        )
```

- [ ] **Step 2: Write failing active-process cancellation test**

```python
def test_cancel_run_stops_active_process(running_store, tmp_path):
    marker = tmp_path / "started"
    executor = RuntimeExecutor(
        running_store, ToolPolicyRegistry({"probe": probe_policy()}),
        ManagedProcessRunner(),
    )
    result_box = []
    worker = threading.Thread(
        target=lambda: result_box.append(executor.submit_subprocess(
            "run-1", "probe",
            (sys.executable, "-c", f"open({str(marker)!r}, 'w').close(); import time; time.sleep(10)"),
            tool_call_id="call-1",
        ))
    )
    worker.start()
    wait_until(lambda: marker.exists(), timeout=1)
    executor.cancel_run("run-1")
    worker.join(timeout=2)
    assert worker.is_alive() is False
    assert result_box[0].call.status is ToolCallStatus.CANCELLED
    assert running_store.get_run("run-1").status is RunStatus.CANCELLED
```

`wait_until(predicate, timeout)` polls every 0.01 seconds using
`time.monotonic()` and raises `AssertionError("condition not reached")` at the
deadline.

- [ ] **Step 3: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_executor.py -v`

Expected: failures report missing `cancel_run` methods.

- [ ] **Step 4: Implement the Store cancellation transaction**

`SQLiteStore.cancel_run(run_id)` uses one `BEGIN IMMEDIATE` transaction to validate
the Run transition, set Run cancelled, set its `created` and `waiting_approval`
ToolCalls cancelled with end timestamps, append one `tool.cancelled` Event per
settled call, append `run.cancelled`, and commit. It leaves `running` calls for the
executor or recovery manager to settle after OS termination.

- [ ] **Step 5: Implement executor cancellation coordination**

Maintain a lock-protected mapping from ToolCall ID to `(run_id, Event)` while a
callable or process is active. `RuntimeExecutor.cancel_run` first calls Store
cancellation, then sets every matching Event. Submission converts Store admission
errors for a non-running Run to `ExecutionRefused`; it must not call the runner.

- [ ] **Step 6: Run tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_executor.py tests/runtime/test_store.py -v`

Expected: all selected tests pass.

Run: `.venv/bin/python -m ruff check corecoder/runtime tests/runtime`

Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add corecoder/runtime/store.py corecoder/runtime/executor.py tests/runtime/test_executor.py tests/runtime/test_store.py
git commit -m "feat(runtime): cancel runs and active execution"
```

## Task 9: Scan and Reconcile Interrupted Work Conservatively

**Files:**

- Create: `corecoder/runtime/recovery.py`
- Create: `tests/runtime/test_recovery.py`
- Modify: `corecoder/runtime/store.py`

**Interfaces:**

- Produces: `RecoveryKind`, `RecoveryCandidate`, `RecoveryResolution`,
  `RecoveryManager.scan()`, `resume_retry()`, and `reconcile()`.
- Consumes: policies, Store status queries, and Phase 1 interrupted transitions.

- [ ] **Step 1: Write failing orphaned-call scan tests**

```python
def test_scan_classifies_safe_and_high_risk_orphans(store_with_two_running_calls):
    store, registry = store_with_two_running_calls
    candidates = RecoveryManager(store, registry).scan()
    by_call = {candidate.call.id: candidate for candidate in candidates}
    assert by_call["read-call"].kind is RecoveryKind.RETRY_ALLOWED
    assert by_call["effect-call"].kind is RecoveryKind.HUMAN_REQUIRED
    assert store.get_tool_call("read-call").status is ToolCallStatus.INTERRUPTED
    assert store.get_tool_call("effect-call").status is ToolCallStatus.INTERRUPTED
    assert store.get_run("run-read").status is RunStatus.RECOVERABLE
    assert store.get_run("run-effect").status is RunStatus.RECOVERABLE
```

The fixture creates separate Runs, transitions both calls to running, and registers
`read-probe` as read-only/idempotent/auto-retry with two attempts while
`external-probe` uses the deny-safe default. `RecoveryManager` accepts only Store
and registry; its constructor has no operation callback or process runner.

- [ ] **Step 2: Write failing waiting-approval classification tests**

```python
def test_scan_distinguishes_pending_and_approved_not_started(recovery_store):
    make_waiting_call(recovery_store, "run-pending", "pending-call", "approval-1")
    make_waiting_call(recovery_store, "run-ready", "ready-call", "approval-2")
    recovery_store.resolve_approval("approval-2", ApprovalDecision.ALLOW_ONCE)
    candidates = RecoveryManager(
        recovery_store, ToolPolicyRegistry.with_builtin_defaults()
    ).scan()
    kinds = {candidate.call.id: candidate.kind for candidate in candidates}
    assert kinds["pending-call"] is RecoveryKind.APPROVAL_PENDING
    assert kinds["ready-call"] is RecoveryKind.READY_TO_START
```

`make_waiting_call` creates and starts a separate Run, creates one mutating call,
and requests Approval. Add a succeeded ToolCall in a third Run and assert its ID
is absent from `kinds`.

- [ ] **Step 3: Write failing reconciliation tests**

```python
def test_resume_retry_resolves_source_before_new_attempt(retry_candidate):
    manager, candidate = retry_candidate
    retry = manager.resume_retry(candidate)
    assert manager.store.get_tool_call(candidate.call.id).status is ToolCallStatus.FAILED
    assert manager.store.get_run(candidate.run.id).status is RunStatus.RUNNING
    assert retry.status is ToolCallStatus.CREATED
    assert retry.retry_of == candidate.call.id
    assert retry.attempt == candidate.call.attempt + 1


@pytest.mark.parametrize(
    ("resolution", "status"),
    [
        (RecoveryResolution.CONFIRMED_SUCCEEDED, ToolCallStatus.SUCCEEDED),
        (RecoveryResolution.CONFIRMED_FAILED, ToolCallStatus.FAILED),
        (RecoveryResolution.ABANDON, ToolCallStatus.CANCELLED),
    ],
)
def test_human_reconciliation_never_creates_retry(
    human_candidate, resolution, status
):
    manager, candidate = human_candidate
    before_ids = {call.id for call in manager.store.list_tool_calls()}
    resolved = manager.reconcile(candidate.call.id, resolution)
    assert resolved.status is status
    assert {call.id for call in manager.store.list_tool_calls()} == before_ids
```

- [ ] **Step 4: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_recovery.py -v`

Expected: import fails because `corecoder.runtime.recovery` does not exist.

- [ ] **Step 5: Add Store recovery queries and atomic orphan handling**

Add `list_runs(statuses=None)`, `list_tool_calls(run_id=None, statuses=None)`, and
`mark_orphaned_tool_call(call_id)`. The last method uses one transaction to require
the current call be running, set it interrupted, set a non-cancelled Run
recoverable, append `tool.interrupted` with reason `process_lost`, and return the
updated records. Dynamic `IN` clauses use generated `?` placeholders and enum
values only; an empty status set returns an empty list.

- [ ] **Step 6: Implement recovery types and deterministic scan**

```python
class RecoveryKind(str, Enum):
    RETRY_ALLOWED = "retry_allowed"
    HUMAN_REQUIRED = "human_required"
    APPROVAL_PENDING = "approval_pending"
    READY_TO_START = "ready_to_start"


class RecoveryResolution(str, Enum):
    CONFIRMED_SUCCEEDED = "confirmed_succeeded"
    CONFIRMED_FAILED = "confirmed_failed"
    ABANDON = "abandon"


@dataclass(frozen=True)
class RecoveryCandidate:
    run: RunRecord
    call: ToolCallRecord
    kind: RecoveryKind
    reason: str
```

`scan()` first converts every running call through the Store orphan transaction,
then classifies interrupted and waiting-approval records. It sorts candidates by
Run creation time, ToolCall creation time, and ID. It does not import executor or
process modules and cannot start work.

- [ ] **Step 7: Implement explicit retry and human reconciliation**

`resume_retry` accepts only `RETRY_ALLOWED`; transitions the interrupted source to
failed with `recovery.resolved`, transitions its Run recoverable to running,
records `tool.retry_scheduled`, and creates a new ToolCall from source metadata.
`reconcile` accepts only interrupted calls and maps the three resolutions to
succeeded, failed, or cancelled with `recovery.resolved`; it returns the Run to
running unless the Run was already cancelled. Neither method executes the new
attempt.

- [ ] **Step 8: Run recovery tests and Ruff**

Run: `.venv/bin/python -m pytest tests/runtime/test_recovery.py tests/runtime/test_store.py -v`

Expected: all selected tests pass.

Run: `.venv/bin/python -m ruff check corecoder/runtime tests/runtime`

Expected: exit 0.

- [ ] **Step 9: Commit**

```bash
git add corecoder/runtime/recovery.py corecoder/runtime/store.py tests/runtime/test_recovery.py
git commit -m "feat(runtime): recover interrupted tool calls safely"
```

## Task 10: Fault Injection, Public API, and Phase Verification

**Files:**

- Create: `tests/runtime/test_fault_injection.py`
- Create: `tests/runtime/test_public_api.py`
- Modify: `corecoder/runtime/__init__.py`
- Modify: `tests/runtime/test_store.py`
- Modify: `README.md`
- Modify: `README_CN.md`

**Interfaces:**

- Consumes: all Phase 2 modules.
- Produces: intentional stable imports and final verification evidence.

- [ ] **Step 1: Write a failing approval rollback test**

Create a running Run and mutating ToolCall. Install this SQLite trigger after
initialization:

```sql
CREATE TRIGGER fail_approval_event
BEFORE INSERT ON events
WHEN NEW.type = 'approval.requested'
BEGIN
    SELECT RAISE(ABORT, 'injected approval event failure');
END;
```

Assert `request_approval` raises `sqlite3.IntegrityError`; Approval lookup returns
`None`; ToolCall remains created; Run remains running; and the Event list is
unchanged. This proves Approval, two state updates, and Event roll back together.

- [ ] **Step 2: Write crash-boundary and zero-replay tests**

Persist a high-risk ToolCall as running without invoking a runner, then call
`RecoveryManager.scan()`. Assert it becomes interrupted and human-required. Pass a
Spy operation counter only to the test harness—not to RecoveryManager—and assert
the counter remains zero. Add a completed ToolCall in another Run and assert scan
does not modify it or append events.

- [ ] **Step 3: Write a failing public API test**

Create `tests/runtime/test_public_api.py`. Move the existing
`test_runtime_public_api` from `test_store.py` into it, retain its nine Phase 1
imports, then add these imports: `ApprovalDecision`, `ApprovalRecord`,
`ApprovalStatus`, `ExecutionRefused`, `FailureKind`, `ManagedProcessRunner`,
`PendingApproval`, `ProcessResult`, `ProcessSpec`, `RecoveryCandidate`,
`RecoveryKind`, `RecoveryManager`, `RecoveryResolution`, `RuntimeExecutor`,
`RuntimeResult`, `ToolPolicy`, and `ToolPolicyRegistry`. Assert every imported
symbol is not `None`.

```python
def test_runtime_public_api():
    exported = (
        ApprovalDecision, ApprovalRecord, ApprovalStatus, ExecutionRefused,
        FailureKind, ManagedProcessRunner, PendingApproval, ProcessResult,
        ProcessSpec, RecoveryCandidate, RecoveryKind, RecoveryManager,
        RecoveryResolution, RuntimeExecutor, RuntimeResult, ToolPolicy,
        ToolPolicyRegistry,
    )
    assert all(symbol is not None for symbol in exported)
```

- [ ] **Step 4: Run new tests and verify RED**

Run: `.venv/bin/python -m pytest tests/runtime/test_fault_injection.py tests/runtime/test_public_api.py -v`

Expected: public imports fail until `runtime/__init__.py` is updated;
fault-injection behavior must already pass.

- [ ] **Step 5: Expose the intentional Phase 2 API**

Import the exact 17 Phase 2 symbols listed in Step 3 and append them to `__all__`.
Do not change top-level `corecoder/__init__.py`.

- [ ] **Step 6: Run all runtime tests**

Run: `.venv/bin/python -m pytest tests/runtime/ -v`

Expected: zero failures.

- [ ] **Step 7: Run the full repository verification**

Run: `.venv/bin/python -m pytest tests/ -q`

Expected: zero failures. Recompute README package file/physical/net counts with
the same logic used by `test_readme_line_counts_are_current`, update both READMEs,
and rerun until the count test passes. Update the documented test count to the
actual collected total.

Run: `.venv/bin/python -m compileall -q corecoder tests`

Expected: exit 0.

Run: `.venv/bin/python -m ruff check corecoder/runtime tests/runtime`

Expected: exit 0.

Run: `.venv/bin/python -m ruff check corecoder tests`

Expected: only the two recorded pre-existing findings may remain:
`corecoder/context.py` I001 and `tests/test_safety_matrix.py` RUF012.

Run: `git diff --check`

Expected: exit 0.

- [ ] **Step 8: Run the Phase 2 demonstration**

Using temporary directories and `sys.executable`, demonstrate in one script:

1. an approved subprocess succeeds;
2. a denied mutation starts zero processes;
3. a timeout is confirmed and settled;
4. a retryable read-only timeout creates attempt 2;
5. a simulated orphaned external-effect call becomes human-required;
6. Event sequence and ToolCall lineage identify every result.

Print records and Event `(sequence, type, tool_call_id)` tuples. Do not use network,
the user's real repositories, or destructive commands.

- [ ] **Step 9: Review the complete Phase 2 diff**

Review from commit `b64a042` through HEAD against the approved design. Fix every
Critical or Important finding with a failing test and a separate commit. Record
minor deferred items without changing unrelated CoreCoder code.

- [ ] **Step 10: Commit public API and verification updates**

```bash
git add corecoder/runtime/__init__.py tests/runtime/test_fault_injection.py tests/runtime/test_public_api.py tests/runtime/test_store.py README.md README_CN.md
git commit -m "feat(runtime): complete execution recovery foundation"
```

## Phase 2 Learning Checkpoint

Before Phase 3, explain and demonstrate:

1. why Approval is durable and bound to one ToolCall attempt;
2. why policy classification is not a security sandbox;
3. why process-group termination is stronger than `subprocess.run(timeout=...)`;
4. what POSIX can guarantee and why Windows is best-effort with the standard library;
5. why SQLite and external side effects cannot form one atomic transaction;
6. how conservative `interrupted` handling avoids unsafe replay without claiming exactly-once;
7. why retry requires both idempotency and an explicitly retryable failure class;
8. how Run cancellation closes the new-call race and settles active work;
9. the exact limitations that remain for Trace, metrics, Eval, and Agent-loop integration.
