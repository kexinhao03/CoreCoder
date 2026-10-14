# Runtime coding-chain verification

The production `corecoder` CLI now wraps `read_file`, `write_file`, `edit_file`,
and `bash` with `RuntimeToolAdapter`. This extends the earlier read/write-only
integration; the frozen `a7579ac` evaluation evidence predates this change.

## Execute the offline checks

From this checkout with development dependencies installed:

```sh
python -m pytest tests/test_agent_coding_chain.py tests/test_agent_runtime.py -q
```

The tests use `cli._build_agent`, the same builder used by the production CLI.
The model is scripted and external MCP/hooks are disabled for reproducibility.
File reads, edits, SQLite transactions, shell commands, test subprocesses,
process exits, and POSIX SIGINT are real. No network or model API key is needed.

The primary fixture starts with `add(a, b)` incorrectly returning `a - b`.
It reads the source, runs a real unittest subprocess that fails with exit code
1, approves and performs an exact-match edit to `a + b`, and runs the tests again
with exit code 0. It verifies the actual file, model-visible results, approvals,
process evidence, Step outcomes, and ordered ToolCall/Event records. `-B` avoids
Python's same-second/same-size bytecode cache reuse in this rapid fixture.
A Run can succeed after an intermediate failed test is corrected; individual
ToolCalls retain their actual failure/success status.

## Verified contracts

| Scenario | Required observation |
| --- | --- |
| Read → failing test → edit → passing test | Four persisted ToolCalls; statuses succeeded/failed/succeeded/succeeded; exit codes 1 then 0 |
| Denied edit or bash | Denied Approval, cancelled ToolCall, unchanged file, no command process evidence |
| Edit search mismatch | Failed ToolCall and Step, unchanged file |
| Plan mode | Edit/bash blocked before Runtime submission, including with auto-approval |
| Timeout | Persisted requested timeout; confirmed process termination; no delayed effect; no retry |
| Run cancellation | Active command stops; subsequent edit refused; no delayed effect |
| Caller interruption | Process cleanup, interrupted ToolCall, recoverable Run, settled Step |
| Real SIGINT during a multi-tool batch | Command stops; queued edit never starts; pending model tool replies backfilled |
| Real exit-87 after an edit or command effect | New Store sees running orphan; scan classifies human_required; no replay or second effect |
| Managed batches | Sequential execution in model order; no single-active-call contention |
| Shell directory change | Next command uses session-scoped tracked cwd, persisted with its arguments |
| Invalid timeout / dangerous command | Failed validation Step, no ToolCall launched or effect |
| Opaque shell credential | Raw command reaches approval and execution; SQLite dump contains no supplied credential |
| Verbose failed test | Combined output and persisted summary preserve stderr tail within size limits |

## Execution and recovery boundaries

- Batches containing a Runtime adapter execute sequentially on the caller thread.
  Entirely unmanaged batches retain the original parallel path. A shared session
  gate also protects direct calls from other threads.
- Bash keeps its public `command`/`timeout` schema and dangerous-command check.
  The managed runner launches `/bin/sh -c` on POSIX or `COMSPEC /c` on Windows.
  Its default and maximum timeout are 120 seconds; values outside 1–120 integer
  seconds are rejected before launch. Commands never auto-retry.
- Initial bash cwd is the caller's current directory. `--runtime-workspace`
  selects audit storage; it does not silently change command execution cwd.
  Subsequent bash directory tracking retains the existing simple `cd` heuristic,
  rather than parsing arbitrary shell programs.
- File tools are synchronous in-process operations. Cancellation cannot forcibly
  interrupt or roll back an already-running file write. Interrupted mutations
  conservatively require reconciliation; they are not automatically replayed.
- Edit old/new strings and shell command source are stored as byte counts and
  SHA-256 summaries. Raw arguments are held in the current executor only, so
  restarting does not permit replaying a previously approved command or edit.
  Existing output-pattern redaction remains bounded, not a universal secret detector.
- Process-group termination checks are POSIX-only. Windows remains best-effort;
  the delayed-effect/SIGINT group assertions are explicitly skipped there.
- Agent recovery is inspected through `RecoveryManager`; the interactive CLI does
  not provide automatic session continuation or human reconciliation UI.
- This demonstrates execution contracts with a scripted model, not live-model
  task success, a security sandbox, or general exactly-once external effects.

## Local verification result — 2026-10-14

Verified on macOS / Python 3.12 in the ReliAgent development worktree:

- `python -m pytest tests -q`: **557 passed, 1 skipped** (71.81 seconds).
  The skip is the opt-in live-model integration test requiring credentials.
- The new coding-chain file contains **23** collected cases, all included in
  the successful full run.
- `python -m ruff check corecoder tests`: passed.
- `python -m compileall -q corecoder tests`: passed.
- `git diff --check`: passed.
- Independent review identified the SIGINT batch and stderr-tail issues;
  both have regression tests and passed follow-up review.

These results describe the current local changes. They do not replace the
frozen historical evaluation manifests or claim a remote cross-platform CI run.
