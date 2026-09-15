# ReliAgent Phase 1 Runtime Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不修改现有Agent Loop的前提下，实现Run、ToolCall和Event的状态契约及SQLite事务化持久化，为后续审批、执行和恢复提供可信事实层。

**Architecture:** 新增`corecoder.runtime`包。状态转换规则集中在`state.py`，不可变记录模型放在`models.py`，`SQLiteStore`每次操作使用独立连接和`BEGIN IMMEDIATE`事务，状态表作为恢复事实源，Event作为同一事务写入的审计记录。

**Tech Stack:** Python 3.10标准库、`dataclasses`、`enum`、`sqlite3`、`json`、`uuid`、pytest、ruff。

**Spec:** `ReliAgent-需求文档-v1.1.md`

## Global Constraints

- Python最低版本为3.10。
- P0使用Run、ToolCall两级核心状态；结构化Step属于P1。
- 不修改现有`corecoder.agent.Agent`执行逻辑。
- 不新增第三方运行时依赖。
- 状态表是恢复事实源，Event是append-only审计记录；同一状态变化必须在一个SQLite事务内提交。
- ToolCall终态不可回到执行态；一次重试创建一条新ToolCall并通过`retry_of`关联。
- 结果未知的外部副作用不在本阶段自动恢复或重放。
- 所有生产行为严格遵循RED→GREEN→REFACTOR。
- 不改动现有`fetch_url`相关用户变更。

---

## File Map

| File | Responsibility |
|---|---|
| `corecoder/runtime/__init__.py` | 暴露第一阶段稳定公共API |
| `corecoder/runtime/state.py` | 状态枚举、转换表和转换校验 |
| `corecoder/runtime/models.py` | Run、ToolCall、Event不可变记录模型 |
| `corecoder/runtime/store.py` | SQLite schema、事务、CRUD、状态与事件原子更新 |
| `tests/runtime/__init__.py` | 测试包标记 |
| `tests/runtime/test_state.py` | 状态转换契约测试 |
| `tests/runtime/test_models.py` | 行到领域模型的字段约束测试 |
| `tests/runtime/test_store.py` | schema、创建、查询、事务、sequence和retry关系测试 |

## Task 1: Define State Transition Contracts

**Files:**

- Create: `corecoder/runtime/state.py`
- Create: `tests/runtime/__init__.py`
- Create: `tests/runtime/test_state.py`

**Interfaces:**

- Produces: `RunStatus`, `ToolCallStatus`, `RiskLevel`, `ExecutionKind`, `InvalidTransition`, `ensure_run_transition()`, `ensure_tool_call_transition()`.
- Consumes: Python标准库`enum.StrEnum`不可用于Python 3.10，因此使用`class X(str, Enum)`。

- [x] **Step 1: Write failing tests for allowed and rejected transitions**

```python
import pytest

from corecoder.runtime.state import (
    InvalidTransition,
    RunStatus,
    ToolCallStatus,
    ensure_run_transition,
    ensure_tool_call_transition,
)


def test_run_can_start_and_finish():
    ensure_run_transition(RunStatus.CREATED, RunStatus.RUNNING)
    ensure_run_transition(RunStatus.RUNNING, RunStatus.SUCCEEDED)


def test_terminal_run_cannot_return_to_running():
    with pytest.raises(InvalidTransition, match="succeeded -> running"):
        ensure_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)


def test_running_tool_call_can_be_interrupted():
    ensure_tool_call_transition(ToolCallStatus.RUNNING, ToolCallStatus.INTERRUPTED)


def test_terminal_tool_call_cannot_be_retried_in_place():
    with pytest.raises(InvalidTransition, match="failed -> running"):
        ensure_tool_call_transition(ToolCallStatus.FAILED, ToolCallStatus.RUNNING)
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/runtime/test_state.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'corecoder.runtime'`.

- [x] **Step 3: Implement the minimal state module**

Use these exact enum values:

```python
class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    RECOVERABLE = "recoverable"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(str, Enum):
    CREATED = "created"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    EXTERNAL_EFFECT = "external_effect"


class ExecutionKind(str, Enum):
    IN_PROCESS = "in_process"
    SUBPROCESS = "subprocess"
```

Define explicit transition maps. `INTERRUPTED` may move only to`SUCCEEDED`,`FAILED`or`CANCELLED` after a later recovery policy or human reconciliation resolves the unknown outcome. Re-execution always creates a newToolCall attempt; it does not move the interrupted record back to`RUNNING`.`SUCCEEDED`,`FAILED`,`TIMED_OUT`and`CANCELLED`have no outgoing transitions.

`InvalidTransition` must subclass`ValueError`; both guard functions return`None` for an allowed transition and raise with text`"invalid <entity> transition: <from> -> <to>"` otherwise.

- [x] **Step 4: Run state tests and verify GREEN**

Run: `python -m pytest tests/runtime/test_state.py -v`

Expected: 4 passed.

- [x] **Step 5: Commit the state contract**

```bash
git add corecoder/runtime/state.py tests/runtime/__init__.py tests/runtime/test_state.py
git commit -m "feat(runtime): define run and tool-call states"
```

## Task 2: Define Immutable Runtime Records

**Files:**

- Create: `corecoder/runtime/models.py`
- Create: `tests/runtime/test_models.py`

**Interfaces:**

- Consumes: enum types from Task 1.
- Produces: frozen dataclasses `RunRecord`, `ToolCallRecord`, `EventRecord`.

- [x] **Step 1: Write failing model tests**

```python
from dataclasses import FrozenInstanceError

import pytest

from corecoder.runtime.models import EventRecord, RunRecord, ToolCallRecord
from corecoder.runtime.state import ExecutionKind, RiskLevel, RunStatus, ToolCallStatus


def test_run_record_is_immutable():
    run = RunRecord("r1", "fix tests", "repo_maintenance", RunStatus.CREATED,
                    "/tmp/work", "test-model", "v1", "t0", "t0")
    with pytest.raises(FrozenInstanceError):
        run.status = RunStatus.RUNNING


def test_tool_call_record_keeps_retry_lineage():
    call = ToolCallRecord(
        id="c2", run_id="r1", retry_of="c1", tool_name="bash",
        arguments={"command": "pytest"}, risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.SUBPROCESS, idempotent=False,
        idempotency_key=None, status=ToolCallStatus.CREATED, attempt=2,
        timeout_seconds=120, result_summary=None, created_at="t1",
        updated_at="t1", started_at=None, ended_at=None,
    )
    assert call.retry_of == "c1"
    assert call.attempt == 2


def test_event_payload_is_decoded_data():
    event = EventRecord(1, "r1", 1, "run.created", {"source": "cli"}, "t0")
    assert event.payload == {"source": "cli"}
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/runtime/test_models.py -v`

Expected: import fails because `corecoder.runtime.models` does not exist.

- [x] **Step 3: Implement frozen dataclasses with exact fields**

```python
@dataclass(frozen=True)
class RunRecord:
    id: str
    goal: str
    workflow: str
    status: RunStatus
    workspace: str
    model: str
    prompt_version: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ToolCallRecord:
    id: str
    run_id: str
    retry_of: str | None
    tool_name: str
    arguments: dict
    risk_level: RiskLevel
    execution_kind: ExecutionKind
    idempotent: bool
    idempotency_key: str | None
    status: ToolCallStatus
    attempt: int
    timeout_seconds: int
    result_summary: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    ended_at: str | None


@dataclass(frozen=True)
class EventRecord:
    id: int
    run_id: str
    sequence: int
    type: str
    payload: dict
    created_at: str
```

- [x] **Step 4: Run model tests and verify GREEN**

Run: `python -m pytest tests/runtime/test_models.py -v`

Expected: 3 passed.

- [x] **Step 5: Commit the record models**

```bash
git add corecoder/runtime/models.py tests/runtime/test_models.py
git commit -m "feat(runtime): add immutable runtime records"
```

## Task 3: Create SQLite Schema and Run Persistence

**Files:**

- Create: `corecoder/runtime/store.py`
- Create: `tests/runtime/test_store.py`

**Interfaces:**

- Consumes: `RunRecord`, `RunStatus`.
- Produces: `SQLiteStore(path)`, `initialize()`, `create_run()`, `get_run()`.

- [x] **Step 1: Write failing tests for schema and Run creation**

```python
import sqlite3

from corecoder.runtime.state import RunStatus
from corecoder.runtime.store import SQLiteStore


def test_initialize_creates_runtime_tables(tmp_path):
    db = tmp_path / "runs.db"
    SQLiteStore(db).initialize()
    with sqlite3.connect(db) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert {"runs", "tool_calls", "events"} <= names


def test_create_run_persists_state_and_created_event(tmp_path):
    store = SQLiteStore(tmp_path / "runs.db")
    store.initialize()
    run = store.create_run(
        goal="fix failing test", workflow="repo_maintenance",
        workspace=tmp_path, model="test-model", prompt_version="v1",
        run_id="run-1",
    )
    assert run.status is RunStatus.CREATED
    assert store.get_run("run-1") == run
    events = store.list_events("run-1")
    assert [(event.sequence, event.type) for event in events] == [(1, "run.created")]
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/runtime/test_store.py -v`

Expected: import fails because`corecoder.runtime.store`does not exist.

- [x] **Step 3: Implement the schema**

`SQLiteStore`stores only`Path(path)`. Every public operation opens its own connection through`_connect()`with`row_factory = sqlite3.Row`,`PRAGMA foreign_keys = ON`, and a five-second busy timeout.

Create these tables:

```sql
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
```

`initialize()`also creates indexes on`tool_calls(run_id)`,`tool_calls(retry_of)`and`events(run_id, sequence)`.

- [x] **Step 4: Implement Run creation as one transaction**

`create_run()`accepts keyword-only arguments shown in the test plus optional`run_id`. Normalize`workspace`with`Path(workspace).resolve()`, generate absent IDs with`uuid.uuid4().hex`, and timestamps with UTC ISO-8601.

Within`BEGIN IMMEDIATE`:

1. insert the`runs`row with`created`status;
2. insert`run.created`with sequence 1 and payload`{"workflow": workflow}`;
3. commit;
4. on any exception rollback and re-raise.

`list_events()`orders by`sequence`; JSON uses`sort_keys=True`when persisted and is decoded into`dict`when returned.

- [x] **Step 5: Run store tests and verify GREEN**

Run: `python -m pytest tests/runtime/test_store.py -v`

Expected: 2 passed.

- [x] **Step 6: Commit schema and Run persistence**

```bash
git add corecoder/runtime/store.py tests/runtime/test_store.py
git commit -m "feat(runtime): persist runs and audit events"
```

## Task 4: Make Run Transitions Atomic and Auditable

**Files:**

- Modify: `corecoder/runtime/store.py`
- Modify: `tests/runtime/test_store.py`

**Interfaces:**

- Consumes: `ensure_run_transition()` from Task 1 and Run persistence from Task 3.
- Produces: `transition_run(run_id, to_status, event_type, payload=None) -> RunRecord`.

- [x] **Step 1: Write failing transition tests**

```python
import pytest

from corecoder.runtime.state import InvalidTransition, RunStatus


def test_run_transition_updates_state_and_appends_event(store_with_run):
    updated = store_with_run.transition_run(
        "run-1", RunStatus.RUNNING, "run.started", {"source": "test"}
    )
    assert updated.status is RunStatus.RUNNING
    assert [(e.sequence, e.type) for e in store_with_run.list_events("run-1")] == [
        (1, "run.created"), (2, "run.started")
    ]


def test_invalid_transition_writes_neither_state_nor_event(store_with_run):
    store_with_run.transition_run("run-1", RunStatus.RUNNING, "run.started")
    store_with_run.transition_run("run-1", RunStatus.SUCCEEDED, "run.completed")
    before = store_with_run.list_events("run-1")
    with pytest.raises(InvalidTransition):
        store_with_run.transition_run("run-1", RunStatus.RUNNING, "run.restarted")
    assert store_with_run.get_run("run-1").status is RunStatus.SUCCEEDED
    assert store_with_run.list_events("run-1") == before
```

Add a local`store_with_run`fixture to this test module; it initializes a temporary DB and creates`run-1`with the same fixed fields as Task 3.

- [x] **Step 2: Run the new tests and verify RED**

Run: `python -m pytest tests/runtime/test_store.py -v`

Expected: both new tests fail because`transition_run`is absent.

- [x] **Step 3: Implement the atomic transition**

Within one`BEGIN IMMEDIATE`transaction:

1. select the current Run row and raise`KeyError("run not found: <id>")`if absent;
2. call`ensure_run_transition(current, to_status)`before writing;
3. compute next sequence using`SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id = ?`;
4. update`runs.status`and`updated_at`;
5. insert the caller-provided event type and payload;
6. commit and return the updated record.

Rollback on all exceptions. Do not catch`InvalidTransition`or convert it to text.

- [x] **Step 4: Run store tests and verify GREEN**

Run: `python -m pytest tests/runtime/test_store.py -v`

Expected: 4 passed.

- [x] **Step 5: Commit atomic Run transitions**

```bash
git add corecoder/runtime/store.py tests/runtime/test_store.py
git commit -m "feat(runtime): make run transitions atomic"
```

## Task 5: Persist Tool Calls and Retry Lineage

**Files:**

- Modify: `corecoder/runtime/store.py`
- Modify: `tests/runtime/test_store.py`

**Interfaces:**

- Consumes: ToolCall state and model contracts from Tasks 1—2.
- Produces: `create_tool_call()`, `get_tool_call()`, `transition_tool_call()`.

- [x] **Step 1: Write failing ToolCall creation test**

```python
from corecoder.runtime.state import ExecutionKind, RiskLevel, ToolCallStatus


def test_create_tool_call_persists_metadata_and_event(store_with_run):
    call = store_with_run.create_tool_call(
        run_id="run-1", tool_name="bash", arguments={"command": "pytest -q"},
        risk_level=RiskLevel.MUTATING, execution_kind=ExecutionKind.SUBPROCESS,
        idempotent=False, idempotency_key=None, timeout_seconds=120,
        tool_call_id="call-1",
    )
    assert call.status is ToolCallStatus.CREATED
    assert call.attempt == 1
    assert store_with_run.get_tool_call("call-1") == call
    assert store_with_run.list_events("run-1")[-1].type == "tool.created"
```

- [x] **Step 2: Write failing ToolCall transition and retry tests**

```python
def test_retry_is_a_new_tool_call_with_lineage(store_with_run):
    first = create_test_call(store_with_run, "call-1")
    store_with_run.transition_tool_call("call-1", ToolCallStatus.RUNNING, "tool.started")
    store_with_run.transition_tool_call("call-1", ToolCallStatus.FAILED, "tool.failed")
    retry = store_with_run.create_tool_call(
        run_id="run-1", tool_name=first.tool_name, arguments=first.arguments,
        risk_level=first.risk_level, execution_kind=first.execution_kind,
        idempotent=first.idempotent, idempotency_key=first.idempotency_key,
        timeout_seconds=first.timeout_seconds, retry_of="call-1",
        tool_call_id="call-2",
    )
    assert retry.retry_of == "call-1"
    assert retry.attempt == 2
    with pytest.raises(InvalidTransition):
        store_with_run.transition_tool_call(
            "call-1", ToolCallStatus.RUNNING, "tool.retry_started"
        )


def test_retry_must_reference_same_run(store_with_run, tmp_path):
    create_test_call(store_with_run, "call-1")
    store_with_run.create_run(
        goal="other", workflow="repo_maintenance", workspace=tmp_path,
        model="test-model", prompt_version="v1", run_id="run-2",
    )
    with pytest.raises(ValueError, match="retry_of belongs to another run"):
        store_with_run.create_tool_call(
            run_id="run-2", tool_name="bash", arguments={"command": "pytest"},
            risk_level=RiskLevel.MUTATING,
            execution_kind=ExecutionKind.SUBPROCESS, idempotent=False,
            idempotency_key=None, timeout_seconds=120, retry_of="call-1",
            tool_call_id="call-2",
        )
```

`create_test_call()`is a test-only helper in`tests/runtime/test_store.py`; it calls`create_tool_call()`with`RiskLevel.READ_ONLY`,`ExecutionKind.SUBPROCESS`and`idempotent=True`, so the retry test exercises an explicitly retryable operation. The separate creation test above retains`idempotent=False`to prove mutating metadata is persisted without implying retry permission.

- [x] **Step 3: Run new ToolCall tests and verify RED**

Run: `python -m pytest tests/runtime/test_store.py -v`

Expected: failures report missing`create_tool_call`and`transition_tool_call`.

- [x] **Step 4: Implement ToolCall creation**

`create_tool_call()`uses`BEGIN IMMEDIATE`. If`retry_of`is absent, set`attempt = 1`. If present:

1. load the referenced call;
2. raise`KeyError("retry source not found: <id>")`if absent;
3. require the same`run_id`;
4. require the source status to be`FAILED`or`TIMED_OUT`;
5. require the source to be idempotent; otherwise raise`ValueError("non-idempotent tool call cannot be auto-retried")`;
6. set`attempt = source.attempt + 1`.

Insert`tool.created`with payload containing only`tool_name`,`attempt`and`retry_of`; do not put raw arguments in the event.

- [x] **Step 5: Implement ToolCall transitions**

`transition_tool_call(call_id, to_status, event_type, result_summary=None, payload=None)`loads the call and its`run_id`, validates with`ensure_tool_call_transition`, updates timestamps, and appends the event in one transaction.

- Set`started_at`only when first entering`RUNNING`.
- Set`ended_at`for`SUCCEEDED`,`FAILED`,`TIMED_OUT`and`CANCELLED`.
- Leave`ended_at`unset for`INTERRUPTED`, because recovery has not resolved the outcome.
- Truncate`result_summary`to2,000characters before persistence; full output storage belongs to Phase 2.

- [x] **Step 6: Run store tests and verify GREEN**

Run: `python -m pytest tests/runtime/test_store.py -v`

Expected: all runtime store tests pass.

- [x] **Step 7: Commit ToolCall persistence**

```bash
git add corecoder/runtime/store.py tests/runtime/test_store.py
git commit -m "feat(runtime): persist tool-call attempts and retries"
```

## Task 6: Expose the Runtime API and Verify Phase 1

**Files:**

- Create: `corecoder/runtime/__init__.py`
- Modify: `tests/runtime/test_store.py`

**Interfaces:**

- Consumes: all Phase 1 modules.
- Produces: importable public API from`corecoder.runtime`; no top-level`corecoder.__init__`change yet.

- [ ] **Step 1: Write a failing public API test**

```python
def test_runtime_public_api():
    from corecoder.runtime import (
        EventRecord,
        ExecutionKind,
        InvalidTransition,
        RiskLevel,
        RunRecord,
        RunStatus,
        SQLiteStore,
        ToolCallRecord,
        ToolCallStatus,
    )

    assert SQLiteStore is not None
    assert RunStatus.CREATED.value == "created"
    assert ToolCallStatus.INTERRUPTED.value == "interrupted"
    assert all(value is not None for value in (
        EventRecord, ExecutionKind, InvalidTransition, RiskLevel,
        RunRecord, ToolCallRecord,
    ))
```

- [ ] **Step 2: Run the public API test and verify RED**

Run: `python -m pytest tests/runtime/test_store.py::test_runtime_public_api -v`

Expected: import failure because`corecoder.runtime.__init__`does not export these names.

- [ ] **Step 3: Add explicit exports**

Import each symbol from`models.py`,`state.py`and`store.py`; define`__all__`with the exact nine names used by the test. Do not modify`corecoder/__init__.py`in this phase.

- [ ] **Step 4: Run Phase 1 tests**

Run: `python -m pytest tests/runtime/ -v`

Expected: all Phase 1 tests pass.

- [ ] **Step 5: Run repository regression checks**

```bash
python -m pytest tests/ -v
python -m compileall -q corecoder tests
ruff check corecoder tests
```

Expected: pytest reports zero failures, compileall exits0, and ruff reports no violations. If a pre-existing user change fails a check, record the exact failure separately and do not modify that unrelated change.

- [ ] **Step 6: Commit Phase 1 exports**

```bash
git add corecoder/runtime/__init__.py tests/runtime/test_store.py
git commit -m "feat(runtime): expose runtime foundation API"
```

## Phase 1 Learning Checkpoint

Before Phase 2, explain and demonstrate:

1. 为什么状态不能只保存在LLM消息历史里；
2. 为什么状态更新和Event必须处于同一事务；
3. 为什么Event sequence使用`BEGIN IMMEDIATE`保护；
4. 为什么重试创建新ToolCall，而不是让终态回到`running`；
5. 当前事实层能保证什么，尚不能保证什么；
6. 用一条Run和两次ToolCall attempt展示数据库中的完整记录。
