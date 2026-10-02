# ReliAgent Final-Acceptance P0 Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended for this shared worktree) or, when the user explicitly authorizes delegation, superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the formal Agent path, SQLite persistence boundary, Evaluation labels, and repository delivery surface satisfy the approved minimum portfolio-delivery acceptance design.

**Architecture:** Persist only redacted audit representations while the Executor retains raw arguments ephemerally for the active process. Build one CLI-session Runtime around the existing `RuntimeToolAdapter`, rename the Runtime ablation honestly, then regenerate all evidence from a clean repaired Runtime commit.

**Tech Stack:** Python 3.10+, dataclasses, SQLite, pytest, Ruff, POSIX shell, GitHub Actions, standard-library hashing and JSON.

**Spec:** `docs/superpowers/specs/2026-09-27-reliagent-final-acceptance-p0-design.md`

## Global Constraints

- Add no runtime or development dependency.
- Write each behavioral test first and observe the expected failure before changing production code.
- Preserve the public `corecoder` and `reliagent` entry points.
- Migrate only `read_file` and `write_file` into the formal Agent Runtime path.
- Never pass a persisted `[REDACTED]` value to a tool as an execution argument.
- Return the active Tool's functional output to the Agent, but persist only the redacted summary.
- Keep the historical `evidence/reliagent/c724f8a/` snapshot and label it superseded.
- Do not merge `main`, create a tag/release, or publish a package in this plan.

---

## File Structure

- `corecoder/runtime/redaction.py`: storage-safe argument and text redaction primitives.
- `corecoder/runtime/models.py`: replayability field on immutable ToolCall records.
- `corecoder/runtime/store.py`: schema migration, legacy scrub, safe argument/result persistence, retry propagation.
- `corecoder/runtime/executor.py`: ephemeral raw-argument ownership and replay refusal.
- `corecoder/runtime/recovery.py`: replayability eligibility in startup recovery.
- `corecoder/runtime/tool_adapter.py`: durable approval ownership marker and raw-argument approval callback.
- `corecoder/agent_runtime.py`: one CLI-session Runtime lifecycle and built-in tool replacement.
- `corecoder/agent.py`: skip duplicate Permission checks for Runtime-managed tools while retaining plan mode.
- `corecoder/cli.py`: Runtime flags, production builder, and session terminal transitions.
- `corecoder/evals/suites/*.py`: honest configuration identifiers.
- `corecoder/evals/scenarios/ml_experiment.py`: production orphan discovery instead of forced terminal writes.
- `README.md`, `README_CN.md`, `pyproject.toml`, `.github/workflows/ci.yml`: fork-first delivery surface.
- `evidence/reliagent/<runtime-commit>/`: regenerated Raw JSON, Markdown and manifest.

### Task 1: Storage-safe redaction primitives

**Files:**
- Modify: `corecoder/runtime/redaction.py`
- Modify: `tests/runtime/test_redaction.py`

**Interfaces:**
- Produces: `redact_for_storage(tool_name: str, arguments: dict) -> tuple[dict, bool]`
- Produces: `redact_text(text: str) -> str`
- The boolean is true only when the persisted arguments equal the execution arguments.

- [ ] **Step 1: Add failing tests for storage arguments and free-form credentials**

Add literal assertions covering both argv flag forms, common token text, and `write_file` content:

```python
def test_storage_redaction_marks_changed_arguments_non_replayable():
    stored, replayable = redact_for_storage(
        "probe",
        {"argv": ["probe", "--api-key", "AUDIT_ARG_SECRET"]},
    )
    assert stored == {"argv": ["probe", "--api-key", "[REDACTED]"]}
    assert replayable is False


def test_write_content_is_replaced_by_hash_descriptor():
    stored, replayable = redact_for_storage(
        "write_file", {"file_path": "note.txt", "content": "private body"}
    )
    assert stored["file_path"] == "note.txt"
    assert stored["content"] == {
        "redacted": True,
        "bytes": 12,
        "sha256": "aaecb569221e2e49869a9b3e5d61280a2098fb65b08bae1198e892e8f6f00aba",
    }
    assert replayable is False


def test_text_redaction_removes_common_credential_tokens():
    assert redact_text("failed: AUDIT_OUTPUT_SECRET sk-abcdefghijklmnopqrst") == (
        "failed: [REDACTED] [REDACTED]"
    )
```

Before committing the test, calculate the fixed SHA-256 once with `python -c` and paste the full literal; the expectation must not call production hashing code.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/pytest -q tests/runtime/test_redaction.py`

Expected: import failure for `redact_for_storage` or assertion failures showing the seeded secrets remain visible.

- [ ] **Step 3: Implement the minimal storage redactor**

Implement `redact_for_storage()` as a copying operation:

```python
def redact_for_storage(tool_name: str, arguments: dict) -> tuple[dict, bool]:
    stored = redact(arguments)
    if tool_name == "write_file" and isinstance(arguments.get("content"), str):
        raw = arguments["content"].encode("utf-8")
        stored["content"] = {
            "redacted": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return stored, stored == arguments
```

Extend `_TEXT_SECRET_PATTERNS` with bounded patterns for `sk-` tokens and non-whitespace credential-shaped values containing `secret`, `token`, `password`, `api-key`, or `api_key`. Do not redact ordinary prose merely because it says that a secret is unavailable.

- [ ] **Step 4: Run RED tests and existing redaction tests to verify GREEN**

Run: `.venv/bin/pytest -q tests/runtime/test_redaction.py`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add corecoder/runtime/redaction.py tests/runtime/test_redaction.py
git commit -m "fix(runtime): add storage-safe redaction"
```

### Task 2: Safe SQLite schema, migration, and raw-cell tests

**Files:**
- Modify: `corecoder/runtime/models.py`
- Modify: `corecoder/runtime/store.py`
- Modify: `tests/runtime/test_store.py`

**Interfaces:**
- Consumes: `redact_for_storage(tool_name, arguments)` and `redact_text(text)` from Task 1.
- Produces: `ToolCallRecord.arguments_replayable: bool`.
- `SQLiteStore.create_tool_call()` continues accepting raw `arguments`; it persists only the safe representation.

- [ ] **Step 1: Add failing raw-SQLite tests**

Create a ToolCall containing `AUDIT_PLAINTEXT_SECRET`, persist a terminal summary containing `AUDIT_OUTPUT_SECRET`, then query SQLite directly:

```python
with sqlite3.connect(store.path) as connection:
    arguments_json, result_summary, replayable = connection.execute(
        "SELECT arguments_json, result_summary, arguments_replayable "
        "FROM tool_calls WHERE id = ?",
        (call.id,),
    ).fetchone()
assert "AUDIT_PLAINTEXT_SECRET" not in arguments_json
assert "AUDIT_OUTPUT_SECRET" not in result_summary
assert replayable == 0
```

Add a migration test that first creates the old schema manually with plaintext rows, calls `store.initialize()`, and asserts the legacy raw cells are scrubbed and marked non-replayable.

- [ ] **Step 2: Run store tests and verify RED**

Run: `.venv/bin/pytest -q tests/runtime/test_store.py`

Expected: missing `arguments_replayable` column and plaintext assertions fail.

- [ ] **Step 3: Add schema and immutable model field**

Add after `arguments` in `ToolCallRecord`:

```python
arguments_replayable: bool
```

Add `arguments_replayable INTEGER NOT NULL DEFAULT 1` to new schema creation and an `ALTER TABLE` migration for existing databases. Update `_tool_call_from_row()` and every ToolCall INSERT/SELECT-copy transaction, including ordinary retry and recovery retry, to preserve the flag.

- [ ] **Step 4: Redact every Store write boundary**

In `create_tool_call()`, call `redact_for_storage()` before constructing the record and serializing JSON. In `transition_tool_call()`, call `redact_text(result_summary)` before truncating to 2000 characters. Apply `redact_text()` to other direct result-summary writes found by:

```bash
rg -n "result_summary" corecoder/runtime/store.py
```

During `initialize()`, transactionally read every legacy ToolCall row, redact argument JSON and result text, set `arguments_replayable=0` when arguments change, and update only changed rows.

- [ ] **Step 5: Run store/model tests to verify GREEN**

Run: `.venv/bin/pytest -q tests/runtime/test_store.py tests/runtime/test_models.py tests/runtime/test_redaction.py`

Expected: all tests pass; direct SQLite probes contain no seeded plaintext.

- [ ] **Step 6: Commit Task 2**

```bash
git add corecoder/runtime/models.py corecoder/runtime/store.py tests/runtime/test_store.py
git commit -m "fix(runtime): redact durable tool facts"
```

### Task 3: Ephemeral execution arguments and replay refusal

**Files:**
- Modify: `corecoder/runtime/executor.py`
- Modify: `corecoder/runtime/recovery.py`
- Modify: `tests/runtime/test_executor.py`
- Modify: `tests/runtime/test_recovery.py`

**Interfaces:**
- Produces: Executor-private `_execution_arguments: dict[str, dict]`.
- Produces: `ExecutionRefused("sensitive_arguments_not_replayable")` for cross-process execution without raw arguments.

- [ ] **Step 1: Add failing tests for active execution and new-Executor refusal**

Use a real in-process callable to capture received arguments. Assert that the active Executor receives the original secret while SQLite stores the redacted value. Resolve a pending Approval, create a second Executor over the same Store, and assert `execute_approved_in_process()` raises the stable refusal rather than invoking the operation with `[REDACTED]`.

Add a RecoveryManager test asserting an interrupted non-replayable read-only call is `HUMAN_REQUIRED`, never `RETRY_ALLOWED`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/pytest -q tests/runtime/test_executor.py tests/runtime/test_recovery.py`

Expected: the active operation receives redacted persisted arguments and recovery incorrectly allows replay.

- [ ] **Step 3: Implement ephemeral argument ownership**

Store a copied raw argument dict by ToolCall id immediately after `_submit()` creates the durable call. Change `_execute_in_process()` and `_execute_subprocess()` to resolve execution arguments as:

```python
def _arguments_for_execution(self, call: ToolCallRecord) -> dict:
    raw = self._execution_arguments.get(call.id)
    if raw is not None:
        return raw
    if call.arguments_replayable:
        return call.arguments
    raise ExecutionRefused("sensitive_arguments_not_replayable")
```

Carry the same raw arguments across a same-process retry child by copying the parent cache entry to the retry id. Remove cache entries after terminal completion/cancellation and after retry lineage finishes.

Return raw output in `RuntimeResult.output`; rely on Task 2 for redacted `call.result_summary`.

- [ ] **Step 4: Gate recovery eligibility**

Add `call.arguments_replayable` to RecoveryManager's safe predicate and the Store's atomic recovery-retry predicate. A false value yields `HUMAN_REQUIRED` with reason `sensitive_arguments_not_replayable`.

- [ ] **Step 5: Run Runtime tests to verify GREEN**

Run: `.venv/bin/pytest -q tests/runtime/test_executor.py tests/runtime/test_recovery.py tests/runtime/test_final_boundaries.py tests/runtime/test_fault_injection.py`

Expected: all tests pass and existing non-secret recovery remains unchanged.

- [ ] **Step 6: Commit Task 3**

```bash
git add corecoder/runtime/executor.py corecoder/runtime/recovery.py corecoder/runtime/store.py tests/runtime/test_executor.py tests/runtime/test_recovery.py
git commit -m "fix(runtime): refuse replay of redacted arguments"
```

### Task 4: Runtime-managed approval without duplicate prompts

**Files:**
- Modify: `corecoder/tools/base.py`
- Modify: `corecoder/agent.py`
- Modify: `corecoder/runtime/tool_adapter.py`
- Modify: `tests/test_permissions.py`
- Modify: `tests/runtime/test_tool_adapter.py`

**Interfaces:**
- Produces: `Tool.manages_approval: bool = False`.
- `RuntimeToolAdapter.manages_approval = True`.
- Changes `ApprovalHandler` to `Callable[[ApprovalRecord, dict], ApprovalDecision]`.

- [ ] **Step 1: Add failing single-prompt tests**

Construct an Agent with a Runtime-wrapped `write_file` and a `Permission` callback that records calls. The adapter handler calls that Permission. Assert one write creates one callback invocation and one durable Approval, then writes the file. Add a plan-mode assertion proving the adapter is blocked before Runtime submission.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/pytest -q tests/test_permissions.py tests/runtime/test_tool_adapter.py`

Expected: the write prompts twice or the callback lacks raw arguments.

- [ ] **Step 3: Implement approval ownership**

Add the class attribute on `Tool` and `RuntimeToolAdapter`. In `Agent._permit()`, keep the plan-mode branch first, then return `None` for a resolved tool whose `manages_approval` is true. Pass the raw `kwargs` as the second approval-handler argument.

- [ ] **Step 4: Verify permission and adapter tests GREEN**

Run: `.venv/bin/pytest -q tests/test_permissions.py tests/runtime/test_tool_adapter.py`

Expected: all tests pass; legacy unwrapped tools keep their existing Permission behavior.

- [ ] **Step 5: Commit Task 4**

```bash
git add corecoder/tools/base.py corecoder/agent.py corecoder/runtime/tool_adapter.py tests/test_permissions.py tests/runtime/test_tool_adapter.py
git commit -m "feat(agent): delegate wrapped tool approval to runtime"
```

### Task 5: Default CLI Agent Runtime session

**Files:**
- Create: `corecoder/agent_runtime.py`
- Modify: `corecoder/cli.py`
- Create: `tests/test_agent_runtime.py`
- Modify: `tests/test_permissions.py`

**Interfaces:**
- Produces: `AgentRuntimeSession.open(workspace: Path, model: str, permission: Permission) -> AgentRuntimeSession`.
- Produces: `AgentRuntimeSession.wrap_tools(tools: Sequence[Tool]) -> list[Tool]`.
- Produces: `AgentRuntimeSession.finish(status: RunStatus) -> RunRecord`.
- Produces: `_build_agent(llm, config, permission, *, runtime_workspace, runtime_enabled) -> tuple[Agent, AgentRuntimeSession | None]` in `corecoder.cli`.

- [ ] **Step 1: Add failing offline production-builder E2E**

Use `ScriptedLLM` with read, write, final-text responses. Call `_build_agent()` with the real built-in tool list and `Permission(allow_all=True)`, execute one chat, finish the session, and query `<tmp>/.reliagent/agent-runtime.sqlite`. Assert one succeeded Run, two succeeded Steps, two succeeded ToolCalls, one approved Approval, lifecycle Events, file output, and Tool messages returned to Agent.

Add parser assertions for default runtime enabled, `--runtime-workspace`, and `--no-runtime`.

- [ ] **Step 2: Run CLI/session tests and verify RED**

Run: `.venv/bin/pytest -q tests/test_agent_runtime.py tests/test_permissions.py`

Expected: missing module/builder/CLI flags.

- [ ] **Step 3: Implement `AgentRuntimeSession`**

Open the database, create and start a `corecoder_agent` Run, build `RuntimeExecutor(ToolPolicyRegistry.with_builtin_defaults())`, and replace tools by name while preserving list order:

```python
if tool.name in {"read_file", "write_file"}:
    return RuntimeToolAdapter(
        tool, self.store, self.executor, self.run_id,
        approval_handler=self._decide_approval,
    )
```

Map `Permission.check()` to `ALLOW_ONCE` when it returns `None`, otherwise `DENY`. Make `finish()` no-op if the Run is already terminal; otherwise transition with `run.completed`, `run.failed`, or cancellation through `executor.cancel_run()`.

- [ ] **Step 4: Wire production CLI lifecycle**

Add:

```python
p.add_argument("--runtime-workspace", type=Path)
p.add_argument("--no-runtime", action="store_true")
```

Extract `_build_agent()` so tests and `main()` share exactly one construction path. Default workspace is `Path.cwd()`. Wrap one-shot and interactive execution so normal return succeeds the Run, one-shot `KeyboardInterrupt` cancels it, and other one-shot exceptions fail it before retaining current CLI exit codes. `--no-runtime` uses the unchanged tool list and creates no database.

- [ ] **Step 5: Run CLI, adapter, and permission tests GREEN**

Run: `.venv/bin/pytest -q tests/test_agent_runtime.py tests/test_permissions.py tests/runtime/test_tool_adapter.py tests/test_core.py`

Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add corecoder/agent_runtime.py corecoder/cli.py tests/test_agent_runtime.py tests/test_permissions.py
git commit -m "feat(cli): route agent read and write through runtime"
```

### Task 6: Honest Evaluation ablation and real orphan discovery

**Files:**
- Modify: `corecoder/evals/suites/phase3.py`
- Modify: `corecoder/evals/suites/ml_workflow.py`
- Modify: `corecoder/evals/scenarios/ml_experiment.py`
- Modify: `tests/evals/test_models.py`
- Modify: `tests/evals/test_runner.py`
- Modify: `tests/evals/test_ml_experiment_scenario.py`
- Modify: `tests/evals/test_report.py`
- Modify: `tests/evidence/test_reliagent_portfolio.py`

**Interfaces:**
- Configuration ids become `no_retry_no_recovery`, `full`, `no_recovery`.
- No evaluator may call Store transition methods to manufacture the expected terminal outcome.

- [ ] **Step 1: Change tests first to require honest ids and recoverable observations**

Replace literal `baseline` expectations with `no_retry_no_recovery`. For ML process-loss cases, assert the renamed configuration ends `recoverable`, contains real `tool.interrupted`/recovery lifecycle evidence, and never contains the synthetic `PROCESS_EXIT_86` or `PROCESS_EXIT_87` result summary.

- [ ] **Step 2: Run Evaluation tests and verify RED**

Run: `.venv/bin/pytest -q tests/evals/test_models.py tests/evals/test_runner.py tests/evals/test_ml_experiment_scenario.py tests/evals/test_report.py`

Expected: configuration-id and expected-status failures.

- [ ] **Step 3: Rename the configurations**

Change only the ids; retain `(max_attempts=1, auto_retry=False, recovery_enabled=False)` for `no_retry_no_recovery`. Update scenario branches and report expectations.

- [ ] **Step 4: Delete forced terminal-state helper**

Remove `_fail_baseline_after_process_exit()`. For both process-loss branches, use `workflow.recovery.scan(run_id=run_id)` to classify the persisted orphan, then observe `workflow.status(run_id)` without retry/reconcile. Expected state for both non-recovery configurations is `recoverable`; only `full` executes recovery.

- [ ] **Step 5: Run complete Evaluation tests GREEN**

Run: `.venv/bin/pytest -q tests/evals tests/evidence/test_reliagent_portfolio.py`

Expected: all tests pass with the historical evidence test adjusted to treat `c724f8a` as a legacy snapshot rather than the current claim source.

- [ ] **Step 6: Commit Task 6**

```bash
git add corecoder/evals tests/evals tests/evidence/test_reliagent_portfolio.py
git commit -m "fix(evals): label runtime ablation honestly"
```

### Task 7: Fork-first metadata, README, and branch CI

**Files:**
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/reliagent/architecture.md`
- Modify: `docs/reliagent/resume-evidence.md`

**Interfaces:**
- Repository URL: `https://github.com/kexinhao03/CoreCoder`.
- Upstream attribution: `https://github.com/he-yufeng/CoreCoder`.
- Current claims use `no_retry_no_recovery`; historical evidence is labeled superseded.

- [ ] **Step 1: Rewrite the README first screen and quick start**

Lead with “ReliAgent — a reliability-runtime fork of CoreCoder”. Add an “Upstream and contribution boundary” table listing the upstream baseline, Runtime, Recovery CLI, Evaluation, ML Workflow, and evidence directories. Put exact limitations beside the evidence claims. Point clone commands, CI badge and issues to the fork; retain explicit upstream links for original essays/assets.

- [ ] **Step 2: Update project metadata and CI triggers**

Add:

```toml
maintainers = [{ name = "kexinhao03" }]
```

Change PEP 621 project URLs to the fork. Change CI push trigger to:

```yaml
on:
  push:
  pull_request:
    branches: [main]
```

- [ ] **Step 3: Update architecture and resume claim boundaries**

State that the default Agent path wraps only read/write, that the comparison is between Runtime configurations rather than an external baseline, and that P1 Metrics/Trace provenance plus a second workflow remain deferred.

- [ ] **Step 4: Run static and package checks**

Run:

```bash
.venv/bin/ruff check corecoder tests
.venv/bin/python -m compileall -q corecoder tests
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 5: Commit Task 7**

```bash
git add README.md README_CN.md pyproject.toml .github/workflows/ci.yml docs/reliagent
git commit -m "docs(reliagent): establish fork delivery surface"
```

### Task 8: Runtime acceptance, clean commit, and evidence regeneration

**Files:**
- Modify: `tests/evidence/test_reliagent_portfolio.py`
- Create: `evidence/reliagent/<runtime-prefix>/README.md`
- Create: `evidence/reliagent/<runtime-prefix>/manifest.json`
- Create: `evidence/reliagent/<runtime-prefix>/ml-workflow-results.json`
- Create: `evidence/reliagent/<runtime-prefix>/ml-workflow-report.md`
- Create: `evidence/reliagent/<runtime-prefix>/phase3-results.json`
- Create: `evidence/reliagent/<runtime-prefix>/phase3-report.md`
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `docs/reliagent/resume-evidence.md`

**Interfaces:**
- The runtime evidence commit is the clean commit after Tasks 1–7.
- The final evidence commit changes no path under `corecoder/`.

- [ ] **Step 1: Run pre-evidence full verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q corecoder tests
git diff --check
```

Expected: all commands exit 0. Record the exact pytest count.

- [ ] **Step 2: Freeze the clean repaired Runtime snapshot**

Tasks 1–7 already require focused commits. Do not create an empty or duplicate commit here. Verify the worktree contains no tracked changes, record `git rev-parse HEAD` as the Runtime evidence commit, and use its seven-character prefix for the new evidence directory.

- [ ] **Step 3: Generate both real Evaluation suites**

Use fresh temporary directories:

```bash
.venv/bin/python -m corecoder.reliagent_cli eval ml_workflow --output /tmp/reliagent-ml-p0
.venv/bin/python -m corecoder.reliagent_cli eval phase3 --output /tmp/reliagent-phase3-p0
```

Copy the generated Raw JSON and Markdown mechanically into the new evidence directory. Do not rewrite paths, UUIDs, timestamps or result fields.

- [ ] **Step 4: Build and test the new manifest**

Create a manifest containing full evaluated commit, exact generation commands, schema version, and SHA-256 for four report files. Update the evidence test to recompute configuration ids, same-input contracts, trace completeness, duplicate effects and file hashes from the new snapshot.

Run: `.venv/bin/pytest -q tests/evidence/test_reliagent_portfolio.py`

Expected: tests pass and execute the real exit-87 demo.

- [ ] **Step 5: Run explicit SQLite secret probes**

Execute focused tests plus a direct sqlite query over test databases. The literals `AUDIT_PLAINTEXT_SECRET`, `AUDIT_OUTPUT_SECRET`, and the seeded exception secret must not occur in any `arguments_json`, `result_summary`, Approval summary or Event JSON cell.

Run: `.venv/bin/pytest -q tests/runtime/test_redaction.py tests/runtime/test_store.py tests/runtime/test_executor.py tests/test_agent_runtime.py`

Expected: all tests pass.

- [ ] **Step 6: Run the final acceptance command set**

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check corecoder tests
.venv/bin/python -m compileall -q corecoder tests
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
PYTHON_BIN=.venv/bin/python sh scripts/reliagent_ml_demo.sh /tmp/reliagent-demo-p0
git diff --check
```

Expected: all commands exit 0; Demo reports exit 87, succeeded, effect count 1, no duplicate and complete Trace.

- [ ] **Step 7: Commit the evidence snapshot**

```bash
git add evidence/reliagent README.md README_CN.md docs/reliagent tests/evidence/test_reliagent_portfolio.py
git commit -m "docs(reliagent): refresh final acceptance evidence"
```

Verify:

```bash
git diff --exit-code <runtime-commit> -- corecoder
git status --short
```

Expected: no `corecoder/` difference from the evaluated Runtime commit and an empty worktree after temporary planning files are removed.

### Task 9: Push release candidate and verify GitHub Actions

**Files:** none

**Interfaces:**
- Consumes: final evidence commit from Task 8.
- Produces: remote `origin/codex/reliagent-p0` pointing at the same SHA.

- [ ] **Step 1: Show the exact push target**

Run:

```bash
git remote -v
git status --short --branch
git log -3 --oneline
```

Expected: origin is `kexinhao03/CoreCoder`, the worktree is clean, and HEAD is the evidence commit.

- [ ] **Step 2: Request external-write authorization and push**

Push the current branch to `origin/codex/reliagent-p0` using the existing verified SSH identity if HTTPS credentials remain unavailable.

- [ ] **Step 3: Verify remote SHA and CI**

Confirm the remote branch SHA equals local HEAD. Inspect the GitHub Actions run for that SHA and wait for matrix, lint and package jobs. Report any remote-only failure without changing `main`.

- [ ] **Step 4: Stop before merge or release**

Present the branch, commit, Actions result, comparison/PR URL, and remaining strict-v2 limitations. Ask separately before merging `main` or creating a release/tag.
