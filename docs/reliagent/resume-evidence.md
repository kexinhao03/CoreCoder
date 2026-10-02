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

## Current P0 snapshot

[Runtime `a7579ac` evidence](../../evidence/reliagent/a7579ac/) evaluates full
commit `a7579ac10aae3bf68fe8fb8ace96c6e4c2e4d6fd`. Real CLI runs produced
24/24 ML and 90/90 phase3 scenario-contract passes across the three named
configurations. Each suite uses the same case input and fault schedule across
configurations; the generated Raw JSON and Markdown are unchanged, with exact
commands and SHA-256 hashes recorded in the manifest.

The checked-in evidence test recomputes case/configuration coverage, lifecycle
Trace completeness and duplicate-effect counts, and runs the exit-87 demo with
one effect marker after reconciliation. These counts include expected failures
and refusals; they are not a claim of 100% task success. The evidence commit
changes no `corecoder/` source relative to the evaluated Runtime commit.

Packaging acceptance built the sdist and wheel in an isolated environment,
passed `twine check`, installed the wheel in a new virtual environment, ran
both CLI help entries, and ran the installed-wheel ML evaluation with 24/24
scenario contracts. This snapshot does not establish strict-v2 completion or
an arbitrary exactly-once guarantee.

## Historical material is not a current claim

Superseded snapshots remain recoverable from Git history, but were removed
from the current tree because their Raw JSON exposed workstation-specific
absolute paths. They must not be presented as validation of current source, a
current external baseline, or strict-v2 completion.

The legacy local fixture's duplicate-effect observation was limited to its
Effect Marker; it was never a guarantee for arbitrary tools or services.

## Reproduction boundary

Use the checked-in tests and the current fixed-workflow CLI to verify current
behavior. Do not use `git diff --exit-code` against either superseded commit as
proof that current Runtime source is identical to a historical snapshot. For
this P0 snapshot, the appropriate source-identity check is
`git diff --exit-code a7579ac10aae3bf68fe8fb8ace96c6e4c2e4d6fd -- corecoder`.
