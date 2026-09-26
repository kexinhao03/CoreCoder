# ReliAgent Resume Evidence

All numbers below refer to evaluated Runtime commit
`c724f8a4bc45c8ad900040f46fd306d78039279a`. Recompute them from the checked-in
Raw JSON instead of copying numbers from this page.

## Defensible resume bullets

### Runtime engineering

> Extended a coding agent with a SQLite-backed execution Runtime covering
> persisted Run/Step/ToolCall/Approval/Event state, bounded retries, timeouts,
> cross-executor cancellation, approval gates, crash recovery, reconciliation,
> redacted Trace export, and derived metrics.

Evidence: `corecoder/runtime/`, `corecoder/reliagent.py`, and the lifecycle facts
inside both Raw JSON files. This bullet deliberately does not claim a security
sandbox or arbitrary exactly-once effects.

### Executable reliability evaluation

> Built executable Baseline, Full, and No-recovery matrices using identical
> case inputs and fault schedules; 114/114 scenario-contract repetitions passed
> across the 90-run Runtime suite and 24-run ML workflow suite.

Evidence:

- `phase3-results.json`: `90 / 90` contract assertions.
- `ml-workflow-results.json`: `24 / 24` contract assertions.
- Every ML case has one `input_id` shared by all three configurations.

Do not rewrite this as “114 tasks succeeded.” The Raw JSON records expected
failed and recoverable task outcomes for fault scenarios.

### Safe recovery and duplicate-effect evidence

> Added real process-loss checkpoints with observed exit codes 86/87 and
> evidence-driven recovery; Full recovered 50% of the fixed recovery scenarios,
> while all tested effect-bearing scenarios recorded zero duplicate effects.

Evidence:

- `effect_observations.process_exit_code` contains both 86 and 87.
- Both reports derive Full `recovery_success_rate=0.5`.
- Both reports derive `duplicate_side_effect_rate=0.0` for all configurations.
- `scripts/reliagent_ml_demo.sh` demonstrates after-effect reconciliation and
  repeated resume with one fixture Effect Marker.

The duplicate-effect claim is limited to the local fixture marker. It is not a
general exactly-once guarantee.

### Trace completeness

> Implemented redacted, read-only Trace export with semantic lifecycle checks
> and per-Run sequence validation; all 24 ML evaluation traces and all 90 Phase
> 3 traces were complete in the fixed evaluation snapshot.

Evidence: `trace_complete_rate=1.0` for every configuration in both Raw JSON
summaries, backed by per-result `sequence_contiguous=true` and empty `missing`.

## Reproduction commands

```bash
git diff --exit-code c724f8a4bc45c8ad900040f46fd306d78039279a -- corecoder
python -m corecoder.reliagent_cli eval ml_workflow --output /tmp/reliagent-ml
python -m corecoder.reliagent_cli eval phase3 --output /tmp/reliagent-phase3
```

The first command proves that the current Runtime and evaluated Runtime source
are identical while leaving this evidence directory available for comparison.
Compare aggregate fields in the generated files with
`evidence/reliagent/c724f8a/manifest.json` and its referenced snapshot.
Timestamps, temporary paths, UUIDs, and durations may differ between runs;
aggregate claims and deterministic scenario assertions must agree.
