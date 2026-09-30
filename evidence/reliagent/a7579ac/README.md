# ReliAgent P0 release evidence

Evaluated Runtime commit: `a7579ac10aae3bf68fe8fb8ace96c6e4c2e4d6fd`.
The Runtime had no tracked changes when evaluation began. The subsequent
evidence commit changes no file under `corecoder/`.

Both suites ran through the real CLI. The four report files are byte-for-byte
copies of the generated outputs. Repository-local paths are serialized as
relative paths; UUIDs, timestamps, temporary paths, and result fields were not
edited. `manifest.json` records the exact generation commands and SHA-256
hashes.

| Suite | Scenario contracts | Configurations | Duplicate effects | Trace complete |
|---|---:|---|---:|---:|
| ML workflow | 24 / 24 | `no_retry_no_recovery`, `full`, `no_recovery` | 0 | 24 / 24 |
| phase3 | 90 / 90 | `no_retry_no_recovery`, `full`, `no_recovery` | 0 | 90 / 90 |

These are fixed local scenario contracts, including expected failure, denial,
and unresolved-recovery outcomes. They are not 100% task-success claims. The
ML matrix observes real child-process exits 86 and 87; disabled recovery leaves
interrupted work recoverable rather than inventing a failed execution.

Before freezing this Runtime commit, the default suite reported `536 passed,
1 skipped`; repository Ruff and compileall checks both exited zero. Packaging
acceptance built the sdist and wheel in an isolated environment, passed
`twine check` for both artifacts, installed the wheel and dependencies in a
new virtual environment, ran both CLI help entries, and ran the installed-wheel
ML evaluation with 24 / 24 scenario contracts.

The evidence test recomputes configuration/case/repetition coverage, input and
fault-schedule equality, event sequence and lifecycle completeness, duplicate
effect counts, summary counts, Markdown rendering, portable-path constraints,
and all four file hashes. It also runs the real exit-87 reconciliation demo
with one effect marker:

```bash
.venv/bin/python -m pytest -q tests/evidence/test_reliagent_portfolio.py
git diff --exit-code a7579ac10aae3bf68fe8fb8ace96c6e4c2e4d6fd -- corecoder
```

The comparison is internal to this Runtime. Trace completeness means the
current persisted lifecycle-event contract; P1 Metrics/Trace provenance,
Run-scoped token/cost telemetry, a second workflow, and an external baseline
remain deferred. No strict-v2 or arbitrary exactly-once guarantee is implied.

Superseded snapshots were removed from the current tree because their raw
generated output exposed workstation-specific absolute paths. They remain
recoverable from Git history and are not rewritten as current evidence.
