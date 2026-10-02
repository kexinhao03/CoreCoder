# ReliAgent Phase 3 v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the v2 Phase 3 evidence chain: minimal Step and CLI/adapter integration, redacted versioned Trace, derived metrics, deterministic Runtime evaluation, and raw JSON-backed Markdown reporting.

**Architecture:** SQLite remains the only source of runtime facts. Step, ToolCall linkage, and write-time redaction extend the existing Store transactions. Trace/Metrics are read-only projections. Evaluation creates isolated temporary Stores and invokes `RuntimeExecutor` and `RecoveryManager`; the report renders only its raw result model.

**Tech Stack:** Python 3.10+, standard library (`argparse`, `dataclasses`, `json`, `tempfile`, `pathlib`), SQLite, pytest, Ruff.

**Spec:** The user-provided v2 design supersedes the narrower repository history in `docs/superpowers/specs/2026-09-21-reliagent-phase3-trace-eval-design.md` where they conflict.

## Global Constraints

- Preserve Phase 2's external-effect safety: no automatic replay after an unknown high-risk effect.
- Do not add a web UI, API, distributed worker, LLM dependency, network access, or exactly-once claim.
- Keep `token` and `cost` explicitly unavailable (`null` plus reason) until Run-scoped LLM telemetry exists.
- Tests use temporary paths, SQLite databases, deterministic local tools, injected clocks/fault schedules, and no user workspace.
- Every behavioral change follows RED → GREEN → regression verification.
- Do not modify the externally dirty leading blank line in `corecoder/runtime/state.py`.

---

### Task 1: Shared recursive redaction service

**Files:**
- Create: `corecoder/runtime/redaction.py`
- Modify: `corecoder/runtime/store.py`, `corecoder/runtime/approvals.py`, `corecoder/runtime/__init__.py`
- Test: `tests/runtime/test_redaction.py`, `tests/runtime/test_store.py`

**Interfaces:**
- Produces `redact(value: object) -> object` and `redact_text(text: str) -> str`.
- `SQLiteStore._insert_event()` persists only `redact(payload)`.

- [ ] **Step 1: Write failing tests**

```python
def test_redact_recursively_hides_sensitive_mapping_values():
    value = {"token": "plain", "nested": [{"Cookie": "session"}], "safe": "ok"}
    assert redact(value) == {"token": "[REDACTED]", "nested": [{"Cookie": "[REDACTED]"}], "safe": "ok"}

def test_event_storage_never_retains_plain_secret(running_store):
    running_store.record_event("run-1", "test", {"authorization": "Bearer secret-value"})
    assert "secret-value" not in str(running_store.list_events("run-1"))
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/runtime/test_redaction.py tests/runtime/test_store.py -q`

Expected: import failure for `corecoder.runtime.redaction` and an unredacted event assertion.

- [ ] **Step 3: Implement minimal redaction**

```python
SENSITIVE_KEY_PARTS = ("api_key", "apikey", "token", "access_token", "refresh_token", "password", "secret", "authorization", "cookie")

def redact(value: object) -> object:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if is_sensitive_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
```

Apply it at Store event insertion and reuse it for approval summaries. Do not mutate caller data.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/runtime/test_redaction.py tests/runtime/test_store.py -q`

Commit: `feat(runtime): redact persisted runtime facts`

### Task 2: Minimal Step model and atomic Step persistence

**Files:**
- Modify: `corecoder/runtime/state.py`, `corecoder/runtime/models.py`, `corecoder/runtime/store.py`, `corecoder/runtime/__init__.py`
- Test: `tests/runtime/test_state.py`, `tests/runtime/test_models.py`, `tests/runtime/test_store.py`, `tests/runtime/test_steps.py`

**Interfaces:**
- Produces `StepStatus`, frozen `StepRecord`, and Store methods `create_step`, `get_step`, `list_steps`, `transition_step`.
- `create_tool_call` gains an optional `step_id: str | None` and validates the Step belongs to the Run when supplied.

- [ ] **Step 1: Write failing tests**

```python
def test_succeeded_step_cannot_return_to_running():
    ensure_step_transition(StepStatus.SUCCEEDED, StepStatus.RUNNING)

def test_step_transition_commits_state_and_event_atomically(running_store):
    step = running_store.create_step("run-1", sequence=1, title="inspect")
    updated = running_store.transition_step(step.id, StepStatus.RUNNING, "step.started")
    assert updated.status is StepStatus.RUNNING
    assert running_store.list_events("run-1")[-1].type == "step.started"
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/runtime/test_steps.py tests/runtime/test_state.py -q`

Expected: missing Step symbols and Store methods.

- [ ] **Step 3: Implement minimal schema and transitions**

Add `steps` table with run foreign key, unique `(run_id, sequence)`, status, attempt count and lifecycle timestamps. Add optional `step_id` foreign key to `tool_calls`; use a migration-safe nullable column strategy compatible with new Phase 3 databases. Persist Step transitions and events in the same transaction.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/runtime/test_steps.py tests/runtime/test_state.py tests/runtime/test_store.py -q`

Commit: `feat(runtime): persist minimal workflow steps`

### Task 3: Step-aware Runtime adapter and minimal ReliAgent CLI

**Files:**
- Create: `corecoder/reliagent.py`, `corecoder/reliagent_cli.py`
- Modify: `pyproject.toml`, `corecoder/runtime/executor.py`
- Test: `tests/test_reliagent.py`, `tests/test_reliagent_cli.py`

**Interfaces:**
- `WorkflowTask` contains ordered local tool requests; `ReliAgentRuntime.run_task(task)` creates Run/Steps and dispatches through `RuntimeExecutor`.
- CLI provides `reliagent run <task-file>`, `resume <run-id>`, `trace <run-id> --format json`, and `eval <suite-file> --output <dir>`.

- [ ] **Step 1: Write failing integration tests**

```python
def test_adapter_executes_step_through_runtime_and_marks_it_succeeded(tmp_path):
    result = ReliAgentRuntime(store, policies).run_task(read_only_task(tmp_path))
    assert result.step.status is StepStatus.SUCCEEDED
    assert result.call.status is ToolCallStatus.SUCCEEDED

def test_resume_skips_a_succeeded_step(tmp_path):
    result = ReliAgentRuntime(store, policies).resume(result.run.id)
    assert result.executed_step_ids == ()
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/test_reliagent.py tests/test_reliagent_cli.py -q`

Expected: missing adapter and CLI entry point.

- [ ] **Step 3: Implement the narrow production path**

Use JSON task files with a fixed ordered `steps` list and local subprocess argv. The adapter is not an LLM planner: it converts that file to persisted Steps, sends each request to `RuntimeExecutor`, and never advances after failed/cancelled/waiting approval work. Resume skips durable succeeded Steps.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/test_reliagent.py tests/test_reliagent_cli.py -q`

Commit: `feat(reliagent): add minimal step-aware runtime entrypoint`

### Task 4: Versioned Trace export and integrity validation

**Files:**
- Create: `corecoder/runtime/tracing.py`
- Modify: `corecoder/runtime/__init__.py`
- Test: `tests/runtime/test_tracing.py`

**Interfaces:**
- `TraceService(store).export_run(run_id) -> dict` returns schema version, run, steps, calls, approvals, events, integrity, and instrumentation availability.

- [ ] **Step 1: Write failing tests**

```python
def test_trace_export_is_sequence_ordered_and_redacted(running_store):
    running_store.record_event("run-1", "test", {"token": "plain"})
    export = TraceService(running_store).export_run("run-1")
    assert export["events"][0]["sequence"] == 1
    assert export["events"][-1]["payload"]["token"] == "[REDACTED]"

def test_trace_reports_missing_terminal_tool_event(running_store):
    # directly seed a terminal call without its terminal event only in this integrity fixture
    assert "tool.completed" in TraceService(running_store).export_run("run-1")["integrity"]["missing"]
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/runtime/test_tracing.py -q`

Expected: missing `TraceService`.

- [ ] **Step 3: Implement read-only export**

Export copied records only; validate contiguous sequence, Run/Step/Tool/Approval terminal-event coverage, conflicting terminal events, and cross-entity constraints. Mark LLM lifecycle and unavailable instrumentation explicitly; do not count unavailable categories as complete.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/runtime/test_tracing.py -q`

Commit: `feat(runtime): export versioned redacted traces`

### Task 5: Derived MetricsService with explicit unavailable values

**Files:**
- Create: `corecoder/runtime/metrics.py`
- Modify: `corecoder/runtime/__init__.py`
- Test: `tests/runtime/test_metrics.py`

**Interfaces:**
- `MetricsService(store).summarize_run(run_id, *, as_of: datetime | None = None) -> RunMetrics`.

- [ ] **Step 1: Write failing formula tests**

```python
def test_metrics_counts_attempts_not_calls_as_retries(running_store):
    metrics = MetricsService(running_store).summarize_run("run-1")
    assert metrics.retry_count == 2
    assert metrics.tool_success_rate == 0.5
    assert metrics.token_cost.available is False
    assert metrics.token_cost.value is None

def test_nonterminal_run_requires_as_of_for_latency(running_store):
    assert MetricsService(running_store).summarize_run("run-1").end_to_end_seconds is None
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/runtime/test_metrics.py -q`

Expected: missing metrics module.

- [ ] **Step 3: Implement timestamp-safe projections**

Parse only offset-bearing ISO timestamps. Calculate per spec; expose null/not-applicable separately; count manual interventions and uncertain effects from persisted interruption/recovery facts.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/runtime/test_metrics.py -q`

Commit: `feat(runtime): derive trace-backed run metrics`

### Task 6: Evaluation models, real Runtime configurations, and fault injection

**Files:**
- Create: `corecoder/evals/__init__.py`, `corecoder/evals/models.py`, `corecoder/evals/faults.py`, `corecoder/evals/runner.py`
- Test: `tests/evals/test_runner.py`, `tests/evals/test_faults.py`

**Interfaces:**
- `EvaluationCase`, `EvaluationConfig`, `FaultSchedule`, `EvaluationResult`.
- `EvaluationRunner.run(suite)` runs isolated repetitions in baseline/full/no_recovery configurations.

- [ ] **Step 1: Write failing integration tests**

```python
def test_full_and_no_recovery_share_fault_schedule_but_diverge_on_recovery(tmp_path):
    results = EvaluationRunner(configurations=phase3_configurations()).run((recovery_case(),))
    assert results.by_config("full").fault_schedule == results.by_config("no_recovery").fault_schedule
    assert results.by_config("full").recovery_succeeded is True
    assert results.by_config("no_recovery").recovery_succeeded is False
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/evals/test_runner.py tests/evals/test_faults.py -q`

Expected: missing evaluation package.

- [ ] **Step 3: Implement isolated case execution**

Implement only named injection boundaries. Baseline uses a direct local operation and external markers; Full/No-recovery use the production adapter, RuntimeExecutor, Store, approvals, and RecoveryManager. The runner records exceptions as evaluation errors, keeps remaining repetitions running, and never writes expected terminal state directly.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/evals/test_runner.py tests/evals/test_faults.py -q`

Commit: `feat(evals): run deterministic runtime fault cases`

### Task 7: Twelve scenarios and two fixed local workflows

**Files:**
- Create: `corecoder/evals/suites/phase3.py`, `tests/evals/test_phase3_suite.py`, `tests/fixtures/reliagent_workflows/`
- Test: `tests/evals/test_phase3_suite.py`

**Interfaces:**
- `phase3_suite() -> tuple[EvaluationCase, ...]` contains the twelve E01–E12 cases with repeat count three.

- [ ] **Step 1: Write failing suite contract tests**

```python
def test_phase3_suite_has_twelve_unique_cases_and_two_workflows():
    suite = phase3_suite()
    assert [case.id for case in suite] == [f"E{number:02d}" for number in range(1, 13)]
    assert sum(case.is_workflow for case in suite) >= 2
    assert all(case.repeat_count == 3 for case in suite)
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/evals/test_phase3_suite.py -q`

Expected: missing phase-3 suite.

- [ ] **Step 3: Implement deterministic cases**

Cover v2 E01–E12: success, retry success, retry exhaustion, timeout, approval allow/deny, approval restart, succeeded-Step restart, unknown high-risk effect, cancellation, completion-persist failure, and repeated resume. Add two local fixture workflows: JSON metric extraction and a configuration repair that requires approval. Each uses only temporary copies and effect markers.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/evals/test_phase3_suite.py -q`

Commit: `feat(evals): add phase 3 deterministic evidence suite`

### Task 8: Raw JSON persistence and Markdown-only renderer

**Files:**
- Create: `corecoder/evals/report.py`
- Modify: `corecoder/evals/runner.py`, `corecoder/reliagent_cli.py`
- Test: `tests/evals/test_report.py`

**Interfaces:**
- `write_raw_result(report, output_dir) -> Path`; `render_markdown(raw_report) -> str`; `write_markdown(raw_path, output_dir) -> Path`.

- [ ] **Step 1: Write failing report tests**

```python
def test_markdown_renderer_uses_raw_json_without_store(tmp_path):
    raw_path = write_raw_result(sample_report(), tmp_path)
    markdown = write_markdown(raw_path, tmp_path).read_text()
    assert "E01" in markdown and "full" in markdown
    assert "Token/Cost: not available" in markdown
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/evals/test_report.py -q`

Expected: missing report module.

- [ ] **Step 3: Implement versioned results and renderer**

Record automatically resolved Git commit/dirty status, Python/OS, ordered repetition records, assertions, metrics, trace integrity, marker observations, and error fields. JSON is sorted and Markdown reads only the decoded JSON model.

- [ ] **Step 4: Run GREEN tests and commit**

Run: `python -m pytest tests/evals/test_report.py -q`

Commit: `feat(evals): render evidence reports from raw results`

### Task 9: Public API, documentation, full verification, and generated evidence

**Files:**
- Modify: `README.md`, `README_CN.md`, `corecoder/runtime/__init__.py`
- Create: `docs/reliagent/phase3-evidence.md`
- Test: `tests/runtime/test_public_api.py`, `tests/evals/test_public_api.py`

- [ ] **Step 1: Write failing public-surface and report-consistency tests**

```python
def test_runtime_exports_trace_metrics_step_and_redaction_symbols():
    assert {"StepRecord", "StepStatus", "TraceService", "MetricsService", "redact"} <= set(runtime.__all__)

def test_generated_report_has_same_case_configuration_repetition_totals_as_raw_json(tmp_path):
    raw, markdown = generate_phase3_evidence(tmp_path)
    assert report_totals(raw) == markdown_totals(markdown)
```

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest tests/runtime/test_public_api.py tests/evals/test_public_api.py -q`

Expected: missing exports and evidence helper.

- [ ] **Step 3: Document verified scope and generate evidence**

State only observed fixed-suite results. Document unavailable Token/Cost, non-exactly-once semantics, platform limits, and the distinction between safety and recovery success. Do not present a generated report as production reliability evidence.

- [ ] **Step 4: Final verification and commit**

Run: `python -m pytest tests/ -q`

Run: `python -m compileall -q corecoder tests`

Run: `ruff check corecoder tests`

Run: `reliagent eval phase3 --output /tmp/reliagent-phase3-evidence`

Commit: `feat(reliagent): complete phase 3 trace and evaluation evidence`
