# ReliAgent Lightweight ML Experiment Workflow v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the user-approved v2 deterministic ML workflow with real process-loss checkpoints, safe read-only recovery, evidence-driven experiment reconciliation, artifact-integrity reports, CLI controls, and Evaluation Harness coverage.

**Architecture:** Extend the generic Runtime only with durable workflow-definition facts, process/fault observations, and atomic reconciliation primitives. Keep fixture, policies, artifact validation, orchestration, report generation, and scenario assertions in `corecoder.workflows.ml_experiment`; Runtime must never import Workflow code. The CLI uses a workspace-local database and real subprocess invocations, while Evaluation calls the same public Workflow API.

**Tech Stack:** Python 3.10+ standard library, SQLite, argparse, subprocess, pytest, Ruff; no new dependency or network access.

**Spec:** `/Users/haokexin/Downloads/ReliAgent-Lightweight-ML-Workflow-Design-v2.md` (user-supplied authoritative v2); repository design history: `docs/superpowers/specs/2026-09-24-reliagent-ml-experiment-workflow-design.md`.

## Global Constraints

- Support macOS and Linux; real `os._exit(86/87)` integration tests are POSIX-only and skipped elsewhere.
- Use only Python standard library and the repository fixture; never install packages, download data, or read API credentials.
- Require an existing resolved workspace and keep `.reliagent/runtime.sqlite`, `artifacts/`, and `reports/` beneath it.
- Runtime must not import `corecoder.workflows`; Evaluation must call public Workflow APIs rather than copy business logic.
- Mutating experiment attempts require a fresh allow-once Approval and are never automatically retried after interruption.
- Reports treat SQLite persisted normalized ToolCall results as metric truth and current files only as integrity evidence.
- Preserve all existing public defaults and tests for generic `reliagent run`, recovery CLI, RuntimeExecutor, and Phase 3 Evaluation.
- Follow strict TDD: add one behavior test, observe the expected RED, implement minimally, and rerun focused tests before the next behavior.

---

### Task 1: Versioned Fixture and Artifact Validation

**Files:**
- Create: `corecoder/workflows/__init__.py`
- Create: `corecoder/workflows/ml_experiment/__init__.py`
- Create: `corecoder/workflows/ml_experiment/models.py`
- Create: `corecoder/workflows/ml_experiment/artifacts.py`
- Create: `corecoder/workflows/ml_experiment/fixture/check.py`
- Create: `corecoder/workflows/ml_experiment/fixture/train.py`
- Create: `corecoder/workflows/ml_experiment/fixture/extract.py`
- Create: `corecoder/workflows/ml_experiment/fixture/verify_effect.py`
- Create: `corecoder/workflows/ml_experiment/fixture/data.csv`
- Create: `tests/workflows/test_ml_experiment_fixture.py`

**Interfaces:**
- Produces `WorkflowDefinition`, `StepDefinition`, `ArtifactPaths`, `NormalizedMetrics`, and `EffectEvidence` immutable dataclasses.
- Produces `build_definition(workspace: Path, run_id: str) -> WorkflowDefinition` and `ArtifactVerifier.verify_completed_experiment(...)`.
- Fixture commands consume only explicit argv and print one sorted JSON object on success.

- [x] **Step 1: Add the fixed dataset and failing fixture tests**

Use this exact ten-row dataset so expected values are derived independently:

```csv
x,y
0,1
1,3
2,5
3,7
4,9
5,11
6,13
7,15
8,17
9,19
```

Tests execute `train.py` as a real subprocess and assert literal values: sample count `10`, intercept `1.0`, slope `2.0`, MSE `0.0`, R² `1.0`, dataset SHA-256 `33383eeafabf9ba13fc7fed3075550d72846574c56fd1ac5e0d1afe1067aed20`, one parseable marker, and no remaining `metrics.tmp.json`. A malformed CSV test must fail without publishing metrics.

- [x] **Step 2: Run the fixture tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/workflows/test_ml_experiment_fixture.py`

Expected: collection fails because `corecoder.workflows.ml_experiment` and fixture scripts do not exist.

- [x] **Step 3: Implement the minimal fixture scripts**

`train.py` must parse finite floats with `csv.DictReader`, compute OLS using the formulas in v2, append this JSON line and fsync it:

```python
{"experiment_id": experiment_id, "definition_hash": definition_hash}
```

Then write sorted JSON to `metrics.tmp.json`, flush/fsync, call `os.replace`, and fsync the parent directory when `os.open(..., os.O_DIRECTORY)` is supported. `extract.py` validates every identity/hash/schema/numeric field and prints:

```python
{
    "artifact_valid": True,
    "metrics_file_sha256": sha256_file(metrics_path),
    "normalized_metrics": metrics,
}
```

`verify_effect.py` parses every line and prints `effect_count`, `duplicate_effect`, and `effects_file_sha256`; any foreign id/hash or count other than one exits nonzero.

- [x] **Step 4: Implement workflow definitions and path validation**

Use exact constants `workflow_name="ml_experiment"`, `workflow_version="1"`, `model="deterministic"`, `config_version="ml-experiment-v1"`. `resolve_workspace()` rejects missing/non-directory paths. `ArtifactPaths.for_run()` resolves fixed workspace-local paths and checks each with `Path.is_relative_to(resolved_workspace)`. `definition_hash` is SHA-256 over stable sorted JSON covering step key, fixture/data digests, argv template, policy fields, and output templates. `experiment_id` is SHA-256 of `run_id + workflow_version + run_experiment.definition_hash`. The run_experiment persisted arguments include `experiment_id`, `definition_hash`, and `workspace`; approval summaries/events therefore bind and expose those facts together with the ToolCall attempt.

- [x] **Step 5: Verify GREEN and commit**

Run: `.venv/bin/python -m pytest -q tests/workflows/test_ml_experiment_fixture.py`

Expected: all fixture/path/schema tests pass.

Commit: `feat(workflow): add deterministic ML fixture`

---

### Task 2: Durable Step Identity, Fault Plans, and Process Evidence

**Files:**
- Modify: `corecoder/runtime/models.py`
- Modify: `corecoder/runtime/store.py`
- Modify: `corecoder/runtime/processes.py`
- Modify: `corecoder/runtime/__init__.py`
- Create: `corecoder/runtime/faults.py`
- Modify: `tests/runtime/test_store.py`
- Modify: `tests/runtime/test_processes.py`
- Create: `tests/runtime/test_faults.py`

**Interfaces:**
- `StepRecord` gains optional `step_key`, `definition_version`, and `definition_hash` fields for backward compatibility.
- Add `FaultPlanRecord`, `ProcessEvidenceRecord`, and `ReconciliationEvidenceRecord`.
- Add `SQLiteStore.create_fault_plan`, `trigger_fault_plan`, `record_process_evidence`, and read methods.
- `ProcessResult` gains optional `process_evidence: ProcessEvidence | None = None`.

- [x] **Step 1: Add failing Store migration and identity tests**

Tests initialize both a fresh database and a legacy schema, create generic legacy Steps with null identity, create ML Steps with exact identity, enforce unique `(run_id, step_key)`, arm/trigger one fault exactly once, persist `fault.injected`, and round-trip process PID/PGID/token/argv digest/start/end/termination confirmation.

- [x] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/runtime/test_store.py tests/runtime/test_processes.py tests/runtime/test_faults.py`

Expected: failures report missing models, columns, tables, and methods.

- [x] **Step 3: Add backward-compatible schema and records**

Add nullable Step columns through `PRAGMA table_info` migration. Add tables:

```sql
CREATE TABLE fault_plans (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  target_step_key TEXT NOT NULL, checkpoint TEXT NOT NULL,
  exit_code INTEGER NOT NULL, state TEXT NOT NULL,
  created_at TEXT NOT NULL, triggered_at TEXT
);
CREATE TABLE process_evidence (
  tool_call_id TEXT PRIMARY KEY REFERENCES tool_calls(id),
  pid INTEGER NOT NULL, pgid INTEGER, process_token TEXT NOT NULL,
  argv_sha256 TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
  termination_confirmed INTEGER NOT NULL
);
CREATE TABLE reconciliation_evidence (
  id TEXT PRIMARY KEY, tool_call_id TEXT NOT NULL REFERENCES tool_calls(id),
  decision TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL
);
```

Do not alter old records' semantics; null Step identity remains valid outside versioned workflows.

- [x] **Step 4: Implement atomic fault trigger and process evidence**

`trigger_fault_plan(run_id, step_key, checkpoint)` must use `BEGIN IMMEDIATE`, change only `armed -> triggered`, persist `triggered_at`, append `fault.injected`, and return the record; a second call returns `None` without an Event. `record_process_evidence` must reject a different second observation for the same ToolCall.

- [x] **Step 5: Extend ManagedProcessRunner evidence**

Generate a UUID process token before `Popen`; return PID, POSIX PGID, argv SHA-256, timezone-aware start/end timestamps, and the existing termination confirmation. Preserve the six existing positional ProcessResult fields by putting the new field last with a default.

- [x] **Step 6: Verify GREEN and commit**

Run the focused Runtime tests plus `.venv/bin/python -m pytest -q tests/runtime`.

Commit: `feat(runtime): persist workflow fault and process evidence`

---

### Task 3: RuntimeExecutor Checkpoints and Real Process Exit

**Files:**
- Modify: `corecoder/runtime/executor.py`
- Modify: `corecoder/runtime/faults.py`
- Modify: `tests/runtime/test_executor.py`
- Modify: `tests/runtime/test_faults.py`

**Interfaces:**
- `RuntimeExecutor(..., fault_injector: RuntimeFaultInjector | None = None)`.
- `RuntimeFaultInjector.checkpoint(call: ToolCallRecord, checkpoint: str) -> None`.
- Checkpoints are exactly `environment_after_start` and `experiment_after_effect`.

- [x] **Step 1: Add failing checkpoint-order tests**

Use a catchable fake exit callable only in unit tests. Assert environment injection observes persisted `tool.started` before runner invocation. Assert experiment injection occurs after successful ProcessResult and `record_process_evidence`, but before `tool.completed`. Assert a triggered plan never exits twice and a normal Run never consults an environment variable.

- [x] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/runtime/test_faults.py tests/runtime/test_executor.py`

Expected: missing injector parameter/checkpoints.

- [x] **Step 3: Implement runtime-owned checkpoints**

Call the first checkpoint immediately after `_start_call` and before cancellation registration/runner execution. After a successful or failed child return, persist ProcessEvidence, then call `experiment_after_effect` before any terminal ToolCall transition. `RuntimeFaultInjector` obtains the Step key through Store facts, atomically triggers the matching plan, flushes no user output itself, and calls injected `exit_process(exit_code)`; CLI construction uses `os._exit`, unit tests inject a raising callable.

- [x] **Step 4: Verify GREEN and regression**

Run focused tests and `.venv/bin/python -m pytest -q tests/runtime`.

Commit: `feat(runtime): add durable process-loss checkpoints`

---

### Task 4: ML Workflow Start, Approval, Resume, and Safe Recovery

**Files:**
- Modify: `corecoder/reliagent.py`
- Create: `corecoder/workflows/ml_experiment/policies.py`
- Create: `corecoder/workflows/ml_experiment/workflow.py`
- Create: `tests/workflows/test_ml_experiment_workflow.py`
- Create: `tests/workflows/test_ml_experiment_recovery.py`
- Modify: `tests/test_reliagent.py`

**Interfaces:**
- `ReliAgentRuntime.create_task(..., workflow: str = "fixed_task", model: str = "deterministic", prompt_version: str = "phase3", step_metadata: tuple[StepMetadata, ...] | None = None) -> RunRecord`.
- `MLExperimentWorkflow.create(workspace: Path, fault: str | None = None) -> RunRecord`.
- `MLExperimentWorkflow.resume(run_id: str) -> WorkflowStatus`.
- `WorkflowStatus` exposes the stable v2 CLI fields.

- [x] **Step 1: Add failing task-creation compatibility tests**

Assert old `run_task` facts are unchanged. Assert `create_task` persists four stable ML Step keys/hashes before execution and returns a created Run. Assert step count/key/hash mismatch causes resume to fail before any new ToolCall or Event.

- [x] **Step 2: Verify RED, implement create_task, verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_reliagent.py`

Implement `run_task` as `create_task` followed by existing pending execution, retaining defaults.

- [x] **Step 3: Add failing normal-path Workflow tests**

Use real SQLiteStore/RuntimeExecutor and fixture subprocesses. Assert start runs environment check then persists the exact run_experiment ToolCall/Approval and stops. Assert unapproved execution count is zero. Resolve the same approval allow-once, resume, and assert all four Steps succeed, the approved ToolCall id is unchanged, normalized extraction/effect JSON is persisted, and marker count is one. Denial through the Workflow control path must atomically fail ToolCall/Step/Run with `APPROVAL_DENIED` and create no artifacts. When `fault` is selected, `create()` arms the matching persisted FaultPlan immediately after creating the Run and before any Step executes; ordinary Runs create no plan.

- [x] **Step 4: Implement policies and orchestration**

Policies: environment/extract/verify are read-only, idempotent, subprocess, two attempts, startup recovery enabled; experiment is `external_effect`, non-idempotent, one attempt, approval required, no retry. Each argv embeds workflow/experiment/definition/dataset identities and absolute workspace-contained paths. On every resume re-resolve workspace, rebuild definitions, compare Step identities and active call arguments, then use RecoveryManager before pending execution.

- [x] **Step 5: Add failing recovery simulation tests**

With the catchable unit injector, leave environment call running, construct a fresh Workflow, resume, and assert source becomes interrupted/failed with a retry child, retry succeeds, and workflow stops at experiment approval without a marker. For an interrupted experiment, assert no retry and no later Step.

- [x] **Step 6: Implement safe recovery and repeated-resume idempotency**

Only `RecoveryKind.RETRY_ALLOWED` is automatically executed. `HUMAN_REQUIRED` returns `pending_reconciliation_tool_call_id`. A succeeded Run returns without Store writes. Compare Events/ToolCalls/artifact bytes around two successful resumes.

- [x] **Step 7: Verify and commit**

Run all `tests/workflows/test_ml_experiment_workflow.py`, recovery tests, and `tests/test_reliagent.py`.

Commit: `feat(workflow): orchestrate approved ML experiment`

---

### Task 5: Evidence-Driven Reconciliation Transactions

**Files:**
- Modify: `corecoder/runtime/store.py`
- Modify: `corecoder/runtime/state.py`
- Create: `corecoder/workflows/ml_experiment/reconcile.py`
- Create: `tests/workflows/test_ml_experiment_reconcile.py`
- Modify: `tests/runtime/test_store.py`

**Interfaces:**
- `SQLiteStore.reconcile_workflow_completed(call_id, evidence: dict) -> ToolCallRecord` atomically persists evidence, `tool.reconciled`, Step success, and Run resume.
- `SQLiteStore.approve_workflow_retry(call_id, evidence: dict, new_call_id: str, new_approval_id: str) -> Pending identifiers` atomically records evidence and creates a fresh waiting-approval attempt.
- `SQLiteStore.record_unresolved_reconciliation(call_id, evidence: dict) -> ReconciliationEvidenceRecord` appends audit facts without state change.
- `MLReconciliationService.reconcile(call_id: str, decision: ReconciliationDecision) -> ReconciliationResult`.

- [x] **Step 1: Add failing atomic Store tests**

Assert completed accepts only interrupted external-effect calls in recoverable ML Runs, performs `interrupted -> succeeded` only via the dedicated API, marks the linked running Step succeeded, returns Run to running, and writes evidence plus `tool.reconciled`, `step.completed`, `run.resumed` in one transaction. Force an event insert failure and assert every change rolls back. Generic transition APIs must still reject bypasses.

- [x] **Step 2: Verify RED and implement Store transactions**

Run targeted Store tests. Use `BEGIN IMMEDIATE`, stale-state comparisons, one active-attempt invariant, fresh approval ids, and new-attempt `retry_of` lineage.

- [x] **Step 3: Add failing ArtifactVerifier reconciliation tests**

Cover completed with valid metrics/one marker/confirmed-dead process; reject missing or malformed metrics, temp-only metrics, two markers, wrong ids/hashes, altered definition/workspace, and unconfirmed/live process evidence. `unresolved` preserves Run/Step/ToolCall states. `retry` is allowed only when no valid final metrics and no effect marker, creates a fresh attempt and Approval, and never reuses the old Approval.

- [x] **Step 4: Implement reconciliation service**

Read current artifacts only for schema/hash/process-integrity evidence. For completed, persist verified normalized metrics and hashes in reconciliation evidence, then stop; the user must call resume to run extraction. For retry, refuse automatic deletion; require the artifact directory to contain neither final metrics nor matching marker in P0. For unresolved, persist stable reason codes.

- [x] **Step 5: Verify and commit**

Run reconciliation and Runtime Store tests.

Commit: `feat(workflow): reconcile uncertain ML experiment effects`

---

### Task 6: Workspace CLI and Deterministic Reports

**Files:**
- Create: `corecoder/workflows/ml_experiment/report.py`
- Modify: `corecoder/reliagent_cli.py`
- Create: `tests/workflows/test_ml_experiment_report.py`
- Create: `tests/workflows/test_ml_experiment_cli.py`
- Modify: `tests/test_reliagent_cli.py`

**Interfaces:**
- `MLExperimentReportService.build(run_id: str) -> dict`.
- `write_report(raw: dict, report_dir: Path) -> tuple[Path, Path]` uses atomic replacement.
- CLI adds `workflow ml start|resume|report`; control commands accept either legacy `--database` or ML `--workspace`.

- [ ] **Step 1: Add failing report-source and integrity tests**

Create a real successful Workflow. Mutate current metrics values without changing the persisted extraction result and assert report generation rejects on hash mismatch rather than adopting file values. Assert Run/Steps/approval/trace integrity, persisted normalized result, persisted effect result, Runtime Metrics, reconciliation evidence, Git commit/dirty status, platform/Python, token availability false, and conclusion boundaries are present. Repeated report writes must be byte-identical and leave Runtime Events unchanged.

- [ ] **Step 2: Verify RED and implement report service**

Parse the latest succeeded `extract_metrics` and `verify_effect` ToolCall result summaries as the only metric/effect content. Recompute files solely to match persisted hashes. Render Markdown only from the already-built raw dict. Write sorted/indented JSON and Markdown through sibling temp files plus fsync/os.replace; include no timestamp.

- [ ] **Step 3: Add failing CLI normal and fault subprocess tests**

Run `python -m corecoder.reliagent_cli workflow ml start` against a real temporary workspace. Assert NDJSON first line is a flushed created Run record. For exit 86, assert the child actually returns 86, then a separate resume process creates a recovery attempt and stops at approval. For exit 87, approve then resume in a child, assert exit 87 and artifacts exist, then separate resume reports recoverable with zero retry. Exercise completed reconciliation, explicit next resume, report, denial, repeat resume, fixed paths, and path escape rejection.

- [ ] **Step 4: Implement CLI while preserving legacy commands**

Always derive ML database as `<workspace>/.reliagent/runtime.sqlite`. `workflow ml start` prints/flushes a `run_created` JSON line before execution; subsequent non-fault completion prints status JSON. Add `--inject-process-loss` with only the two enum values. `reconcile` keeps the legacy positional resolution/`--database` form and adds `--decision completed|retry|unresolved --workspace` for ML. Stable Workflow status contains exactly the v2 minimum fields plus report command when available.

- [ ] **Step 5: Verify and commit**

Run all Workflow CLI tests and existing `tests/test_reliagent_cli.py`.

Commit: `feat(cli): expose ML workflow recovery controls`

---

### Task 7: Evaluation Harness Adapter and Eight Real Scenarios

**Files:**
- Move: `corecoder/evals/scenarios.py` to `corecoder/evals/scenarios/phase3.py`
- Create: `corecoder/evals/scenarios/__init__.py`
- Create: `corecoder/evals/scenarios/ml_experiment.py`
- Modify: `corecoder/evals/runner.py`
- Modify: `corecoder/evals/suites/phase3.py`
- Create: `corecoder/evals/suites/ml_workflow.py`
- Create: `tests/evals/test_ml_experiment_scenario.py`
- Modify: `tests/evals/test_runner.py`
- Modify: `corecoder/reliagent_cli.py`

**Interfaces:**
- Preserve `from corecoder.evals.scenarios import execute_scenario`.
- Add `ml_workflow_suite()` with eight fixed cases and Baseline/Full/No-recovery configurations sharing one input/fault definition per case.
- Adapter returns normal `RuntimeExecution` evidence and calls only public Workflow APIs.

- [ ] **Step 1: Mechanically convert scenarios module to a package**

Move current implementation without behavior changes, re-export `execute_scenario`, and run all existing Eval tests before adding ML behavior.

- [ ] **Step 2: Add failing Adapter API test**

Monkeypatch only public Workflow methods to raise a sentinel and assert the Adapter reaches them; do not assert mock call counts as success evidence. A real integration test must produce Store/Trace/Metrics/artifact evidence through the Workflow.

- [ ] **Step 3: Add the eight failing scenario tests**

Cases: normal approval success; environment exit/recovery; experiment after-effect exit; valid completed reconciliation without replay; damaged artifact reconciliation refusal; repeated resume; approval denial; report refusal after artifact tampering. Every Scenario invokes RuntimeExecutor through Workflow. Assertions include marker count, retry lineage, recovery status, auto-retry zero for experiment, report integrity, and same input id across configurations.

- [ ] **Step 4: Implement Baseline/Full/No-recovery semantics**

Baseline executes the same fixture/definition/assertions with no persisted recovery after a fault. Full enables RecoveryManager and reconciliation. No-recovery persists Store/approval/Trace but does not schedule safe recovery. Do not alter fixture, workflow definitions, fault boundary, or success assertions between configs.

- [ ] **Step 5: Expose `reliagent eval ml_workflow` and verify**

Generate versioned raw JSON/Markdown using existing report functions while retaining Phase 3 output. Run all Eval tests and both CLI eval commands.

Commit: `feat(evals): benchmark ML workflow recovery`

---

### Task 8: README, Real Demo Evidence, Review, and Stable Commit

**Files:**
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `task_plan.md`
- Modify: `findings.md`
- Modify: `progress.md`
- Test: complete repository

**Interfaces:**
- Document exact committed CLI commands, safety boundaries, and measured outputs only.
- Produce three temporary evidence directories: normal, environment recovery, experiment reconciliation.

- [ ] **Step 1: Run complete verification**

Run exactly:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q corecoder tests
git diff --check
```

Record commands, exit codes, test counts, and any opt-in skips. Update README package line/test counts using the repository's existing test formula.

- [ ] **Step 2: Run three real process demos**

In explicit `/private/tmp/reliagent-ml-*` workspaces, run: normal approval completion/report; exit-86 environment recovery; exit-87 experiment effect followed by evidence-driven completed reconciliation/report. Then run resume twice and compare ToolCalls, Events, marker bytes, raw JSON, and Markdown bytes.

- [ ] **Step 3: Inspect generated evidence**

Verify metric literals from the fixed data, one marker, recovery lineage, no experiment retry, artifact hashes, Trace integrity, unavailable token/cost, Git revision, and clean conclusion boundaries. Do not copy temporary SQLite or generated evidence into Git unless the user separately requests permanent samples.

- [ ] **Step 4: Request independent code review**

Reviewer must audit every v2 safety invariant, real `os._exit` placement, transaction boundaries, artifact-source rules, Evaluation fairness, secret absence, test quality, and backward compatibility. Fix every Critical/Important finding with a new failing test before completion.

- [ ] **Step 5: Re-run verification and create the stable implementation commit**

After review, rerun the full commands and demo against the final staged snapshot. Commit only intended source/tests/docs, confirm `git status --porcelain` is empty, and report the fixed commit hash plus evidence paths.

Commit: `feat(workflow): deliver recoverable ML experiment`
