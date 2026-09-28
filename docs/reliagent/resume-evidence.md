# ReliAgent Resume Evidence

## Current, defensible P0 wording

> Added a SQLite-backed reliability Runtime to a CoreCoder fork, with persisted
> Run/Step/ToolCall/Approval/Event state, approval-gated execution, bounded
> recovery and reconciliation entry points, and narrow Agent integration for
> `read_file` and `write_file`.

Evidence is in `corecoder/runtime/`, `corecoder/reliagent.py`,
`corecoder/reliagent_cli.py`, and the focused Runtime tests. This wording does
not claim that every Agent tool is Runtime-managed, a security sandbox,
arbitrary exactly-once effects, or strict-v2 completion.

> Compared the `no_retry_no_recovery`, `full`, and `no_recovery` Runtime
> configurations under fixed local scenarios.

This is an internal Runtime-configuration comparison, not an external baseline.
P1 Metrics/Trace provenance, a second workflow, and an external baseline are
deferred, so no resume bullet should imply that they have shipped.

## Historical material is not a current claim

`evidence/reliagent/c724f8a/` and commit
`c724f8a4bc45c8ad900040f46fd306d78039279a` are legacy, superseded snapshot
evidence. The checked-in Raw JSON, Markdown reports, and their recorded
`90 / 90` and `24 / 24` scenario-contract counts may be inspected as history,
but must not be presented as validation of current source, a current external
baseline, or strict-v2 completion.

The legacy local fixture's duplicate-effect observation was limited to its
Effect Marker; it was never a guarantee for arbitrary tools or services.

## Reproduction boundary

Use the checked-in tests and the current fixed-workflow CLI to verify current
behavior. Do not use `git diff --exit-code` against the superseded `c724f8a`
commit as proof that current Runtime source is identical to the legacy snapshot.
