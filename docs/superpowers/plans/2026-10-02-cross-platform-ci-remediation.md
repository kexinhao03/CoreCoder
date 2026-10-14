# Cross-Platform CI Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ReliAgent CI matrix finish reliably and preserve deterministic evidence across Ubuntu, macOS, and Windows on Python 3.10-3.13.

**Architecture:** Apply four narrow portability/lifecycle fixes at their owning boundaries: listener locking in `SQLiteStore`, monitor ownership in `RuntimeExecutor`, stream normalization in `ManagedProcessRunner`, and path normalization in the evaluation report. Keep POSIX-only demos explicit and bound CI duration.

**Tech Stack:** Python 3.10-3.13, pytest, SQLite, threading, GitHub Actions, Hatchling, build, twine.

**Spec:** `docs/superpowers/specs/2026-10-02-cross-platform-ci-remediation-design.md`

## Global Constraints

- Preserve all core runtime tests on Windows.
- Skip only tests whose command or failure model is explicitly POSIX-only.
- Do not touch or merge into the dirty local `main` checkout.
- Write a failing regression test before each production change.

---

### Task 1: Listener lifecycle and cancellation monitor cleanup

**Files:**
- Modify: `tests/runtime/test_executor.py`
- Modify: `corecoder/runtime/store.py`
- Modify: `corecoder/runtime/executor.py`

**Interfaces:**
- Consumes: `SQLiteStore.register_terminal_tool_call_listener()` and `RuntimeExecutor._register_cancellation()`.
- Produces: reentrant listener cleanup and synchronous monitor shutdown before submission cleanup.

- [ ] Add a subprocess-isolated regression proving listener garbage collection cannot deadlock while the listener lock is held.
- [ ] Run the test and verify it times out/fails against `threading.Lock`.
- [ ] Change the listener lock to `threading.RLock` and verify the test passes.
- [ ] Add a regression whose monitor signals after its stop event and assert submission does not return before that signal.
- [ ] Run the test and verify the current non-joining cleanup fails.
- [ ] Store monitor threads, set their stop events, and join them outside the cancellation lock.
- [ ] Run focused executor tests and verify monitor registries are empty after execution.

### Task 2: Deterministic subprocess text

**Files:**
- Modify: `tests/runtime/test_processes.py`
- Modify: `corecoder/runtime/processes.py`

**Interfaces:**
- Consumes: decoded `stdout_bytes` and `stderr_bytes`.
- Produces: `ProcessResult.stdout` and `.stderr` with LF newlines on every platform.

- [ ] Add a real subprocess regression that writes literal CRLF and CR bytes and expects LF text.
- [ ] Run it and verify the current decoder preserves CR characters.
- [ ] Normalize newlines immediately after decoding, before `_bound_output`.
- [ ] Run all process tests.

### Task 3: Portable evidence paths and line endings

**Files:**
- Modify: `tests/evals/test_report.py`
- Modify: `corecoder/evals/report.py`
- Create: `.gitattributes`

**Interfaces:**
- Consumes: absolute repository paths embedded in evaluation strings.
- Produces: repository-relative paths using `/` and LF-normalized evidence files.

- [ ] Add a synthetic Windows-path regression with a literal forward-slash expected value.
- [ ] Run it and verify backslashes remain in current output.
- [ ] Normalize separators only after repository-root removal.
- [ ] Add LF attributes for `evidence/reliagent/**` and `*.sh`.
- [ ] Run report and evidence-manifest tests.

### Task 4: Explicit platform boundaries and bounded CI

**Files:**
- Modify: `tests/test_demo.py`
- Modify: `tests/evidence/test_reliagent_portfolio.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: documented Windows skips for two POSIX-only demos and a 20-minute test-job deadline.

- [ ] Add `os.name == "nt"` skip markers with concrete POSIX reasons to the two demo tests.
- [ ] Add `timeout-minutes: 20` to the matrix test job.
- [ ] Run the two demo tests on POSIX to ensure they still execute.

### Task 5: Repository-wide acceptance and publication

**Files:**
- Modify: `README.md` only if package line-count assertions require it.
- Modify: `README_CN.md` only if it contains the corresponding count.

**Interfaces:**
- Produces: a pushed review branch and a fresh GitHub Actions run.

- [ ] Run full pytest using an absolute interpreter path.
- [ ] Run Ruff and compileall.
- [ ] Build sdist/wheel and run twine check.
- [ ] Install the wheel in a fresh virtual environment and run both CLI help commands plus the minimum demo.
- [ ] Update README package counts if the source-of-truth test reports new values, then rerun it.
- [ ] Review `git diff --check` and the complete diff.
- [ ] Commit and push `codex/enable-ci-dispatch`.
- [ ] Wait for all GitHub Actions matrix, lint, and package jobs to finish.
