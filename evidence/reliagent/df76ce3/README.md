# ReliAgent P0 Runtime evidence

Evaluated Runtime commit: `df76ce390796a59636300b789682d9a63312f9fd`.
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

Before freezing this Runtime commit, the default suite reported `525 passed,
1 skipped`; repository Ruff and compileall checks both exited zero. Focused
regressions additionally prove that persisted write Approval summaries contain
only storage-safe descriptors, read-only interrupts settle and permit the next
turn, and interrupted writes remain recoverable and require reconciliation.
After adding this SHA-keyed evidence snapshot and its integrity test, the full
suite reported `526 passed, 1 skipped`.

The evidence test recomputes configuration/case/repetition coverage, input and
fault-schedule equality, event sequence and lifecycle completeness, duplicate
effect counts, summary counts, Markdown rendering and all four file hashes.
It also runs the real exit-87 reconciliation demo with one effect marker:

```bash
.venv/bin/python -m pytest -q tests/evidence/test_reliagent_portfolio.py
git diff --exit-code df76ce390796a59636300b789682d9a63312f9fd -- corecoder
```

The comparison is internal to this Runtime. Trace completeness means the
current persisted lifecycle-event contract; P1 Metrics/Trace provenance,
Run-scoped token/cost telemetry, a second workflow and an external baseline
remain deferred. No strict-v2 or arbitrary exactly-once guarantee is implied.
The `980cd5d` and `c724f8a` directories remain unchanged historical,
superseded snapshots.

Packaging acceptance remains blocked in the existing project environment:
`.venv/bin/python -m build` exited 1 with `No module named build`, and
`.venv/bin/python -m twine check 'dist/*'` exited 1 with
`No module named twine`. No dependency was installed, package published, merge
performed or tag created for this snapshot.
