# ReliAgent P0 Runtime evidence

Evaluated Runtime commit: `8a269a3d5da9eaef54a696854f784d1c68f80e1c`.
The Runtime had no tracked changes when evaluation began. The subsequent
evidence commit changes no file under `corecoder/`.

Both suites ran through the real CLI. The four report files are byte-for-byte
copies of the generated outputs; paths, UUIDs, timestamps and result fields
were not edited. `manifest.json` records the exact generation commands and
SHA-256 hashes. Temporary per-repetition workspaces are removed by the runner;
their paths remain historical identifiers in the Raw JSON, not retained files.

| Suite | Scenario contracts | Configurations | Duplicate effects | Trace complete |
|---|---:|---|---:|---:|
| ML workflow | 24 / 24 | `no_retry_no_recovery`, `full`, `no_recovery` | 0 | 24 / 24 |
| phase3 | 90 / 90 | `no_retry_no_recovery`, `full`, `no_recovery` | 0 | 90 / 90 |

These are fixed local scenario contracts, including expected failure, denial
and unresolved-recovery outcomes. They are not 100% task-success claims. The
ML matrix observes real child-process exits 86 and 87; disabled recovery leaves
interrupted work recoverable rather than inventing a failed execution.

Before freezing this Runtime commit, the default suite reported `534 passed,
1 skipped`; repository Ruff and compileall checks both exited zero. Focused
regressions additionally prove that the Store derives every Approval summary
from the associated storage-safe ToolCall, legacy summaries are rebuilt,
mutating `SystemExit` stays recoverable through the real one-shot CLI, and a
parallel-read admission refusal leaves no running Step.

The evidence test recomputes configuration/case/repetition coverage, input and
fault-schedule equality, event sequence and lifecycle completeness, duplicate
effect counts, summary counts, Markdown rendering and all four file hashes.
It also runs the real exit-87 reconciliation demo with one effect marker:

```bash
.venv/bin/python -m pytest -q tests/evidence/test_reliagent_portfolio.py
git diff --exit-code 8a269a3d5da9eaef54a696854f784d1c68f80e1c -- corecoder
```

The comparison is internal to this Runtime. Trace completeness means the
current persisted lifecycle-event contract; P1 Metrics/Trace provenance,
Run-scoped token/cost telemetry, a second workflow and an external baseline
remain deferred. No strict-v2 or arbitrary exactly-once guarantee is implied.
The `df76ce3`, `980cd5d` and `c724f8a` directories remain historical,
superseded snapshots.

Packaging acceptance remains blocked in the existing project environment:
`.venv/bin/python -m build` exits 1 with `No module named build`, and
`.venv/bin/python -m twine check 'dist/*'` exits 1 with
`No module named twine`. No dependency was installed, package published, merge
performed or tag created for this snapshot.
