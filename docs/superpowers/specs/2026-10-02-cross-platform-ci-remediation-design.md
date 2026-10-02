# Cross-Platform CI Remediation Design

## Goal

Make the existing ReliAgent test, lint, compile, and package workflow complete reliably on Ubuntu, macOS, and Windows with Python 3.10 through 3.13, without weakening core runtime coverage.

## Confirmed failures and boundaries

- Python 3.13 on macOS can deadlock when a weak-reference callback re-enters the terminal-listener lock while an executor is being collected.
- Runtime cancellation monitor threads can outlive a completed submission and keep reading a temporary SQLite database during cleanup, which Windows rejects with `WinError 32`.
- Windows subprocess pipes use CRLF while the persisted audit output contract is platform-independent LF.
- Portable evaluation evidence removes the repository root but can retain Windows separators.
- Two integration tests explicitly depend on a POSIX shell and POSIX process-loss semantics; those tests are not claims about Windows runtime behavior.
- Evidence hashes must not change with checkout newline conversion.
- CI needs a finite job deadline so a deadlock cannot consume the full hosted-runner limit.

## Design

1. Change the class-level terminal-listener lock to a reentrant lock. Keep the existing listener registry and weak-reference lifecycle unchanged.
2. Track each cancellation monitor thread alongside its stop event. Unregistration sets the stop event and joins the monitor before the submission returns. It never joins the current thread.
3. Normalize decoded subprocess stdout and stderr from CRLF/CR to LF before bounding and persisting them.
4. Normalize repository-relative evidence paths to forward slashes after removing either native or escaped repository prefixes.
5. Mark only the POSIX-only demos as skipped on Windows, with explicit reasons. All core runtime, evaluation, and packaging tests continue to run on Windows.
6. Enforce LF checkout for generated evidence and shell scripts with `.gitattributes`.
7. Give each matrix test job a 20-minute timeout.

## Verification

- Every production behavior change starts with a regression test that fails for the observed reason.
- Focused runtime/process/report tests pass after each minimal fix.
- Full pytest, Ruff, compileall, build, twine check, and fresh-wheel smoke checks pass locally.
- GitHub Actions completes all 12 OS/Python test combinations plus lint and package jobs.

## Non-goals

- No claim of strong Windows process-tree termination; it remains best-effort.
- No blanket Windows exclusions and no reduction of the test matrix.
- No local merge into the dirty `main` checkout.
