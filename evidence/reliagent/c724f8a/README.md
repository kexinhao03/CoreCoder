# ReliAgent Evidence Snapshot

This directory is a checked-in evidence snapshot for Runtime commit
`c724f8a4bc45c8ad900040f46fd306d78039279a`.

Files:

- `ml-workflow-results.json`: raw results for 8 cases × 3 configurations.
- `ml-workflow-report.md`: Markdown rendered only from that Raw JSON.
- `phase3-results.json`: raw results for 10 cases × 3 configurations × 3 repetitions.
- `phase3-report.md`: Markdown rendered only from that Raw JSON.
- `manifest.json`: evaluated commit, regeneration commands, and SHA-256 digests.

The contract totals are not task-success rates. Fault scenarios are expected to
fail or remain blocked in configurations without recovery. Effect-marker claims
apply only to the deterministic local fixture and are not a general
exactly-once guarantee.
