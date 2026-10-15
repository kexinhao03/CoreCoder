# Comparative reliability evaluation

Run the deterministic mechanism layer from the repository root:

```sh
.venv/bin/python -m corecoder.evals.comparison --output .tmp/comparison --repetitions 10 --seed 1729
```

The output directory must be new for its trial IDs; existing trial artifacts are never overwritten or deleted. A one-repetition run is explicitly labeled a smoke run. `--deadline-seconds` sets the shared deadline for the initial worker and any restart (default 15 seconds). The supervisor incrementally saves `comparison-results.json` and `comparison-report.md`. Regenerate the report without execution:

```sh
.venv/bin/python -m corecoder.evals.comparison_report .tmp/comparison/comparison-results.json --output .tmp/comparison/report-copy.md
```

## Controlled experiment

A=(retry off, recovery off), B=(on, off), C=(off, on), D=(on, on). All four use production `RuntimeExecutor`, `SQLiteStore`, `RecoveryManager`, and `ReliAgentRuntime`. A is not original upstream CoreCoder. Safe, idempotent preparation is eligible for up to three attempts when retry is enabled, and one attempt otherwise. External effects never become automatically retryable. Routine consent is automatically supplied by the same evaluator policy in all groups and recorded separately from recovery reconciliation.

The fixed task performs safe preparation, one subprocess external effect, and a final subprocess verification. Preparation fails once only in the `retry_only` control. In crash cells it succeeds normally, ensuring disabled retry does not prevent A/C from reaching the assigned crash boundary. Preparation's attempt log and injected transient-failure marker are evaluator instrumentation, not business side effects.

Effects have two variants: `receipt` publishes a queryable completion receipt; `opaque` does not provide runtime reconciliation evidence. Both append an independently fsynced record outside SQLite. Final verification checks the full effect record and writes an independently validated artifact. Completion requires the artifact to match the task ID **and** exactly one matching effect. Runtime success alone is insufficient.

On POSIX, one repetition has 104 trials: two workloads × four groups × (two controls + five boundaries × two fault types + one applicable child-kill case). The seed randomizes blocks and the four groups within each block. The default produces 1,040 trials. E, the pristine upstream live-Agent comparator, belongs to the separate live layer with D_agent and never enters factorial estimates. D_agent has production Agent retry disabled and is not factorial D.

## Faults and recovery

| Boundary | Acknowledged state | Eligible delivery |
|---|---|---|
| `before_call_persist` | Preparation committed; effect call not created | Exception, runtime SIGKILL |
| `after_call_persist_before_start` | Effect call and allow-once approval committed; execution has not started | Exception, runtime SIGKILL |
| `during_tool_execution` | Real effect subprocess is alive and waiting before its effect | Child exception, child SIGKILL, runtime SIGKILL |
| `after_effect_before_result_persist` | Effect and optional receipt fsynced; child exited; tool result not committed | Exception, runtime SIGKILL |
| `after_result_before_next_inference` | Tool success committed, before the fixed task proceeds | Exception, runtime SIGKILL |

The last schema name is shared with live-Agent evaluation. Its mechanism `boundary_label` is `after_result_before_fixed_task_continuation`: this layer has **no inference**. Child-kill combinations without a child are recorded in `skipped_matrix`. Non-POSIX systems exclude SIGKILL cells instead of relabeling another termination method as SIGKILL.

The parent waits for a durable, exact-boundary handshake. The child/worker cannot advance until an explicit response or kill. Polling delay is not fault proof. The parent records actual process exit status; child-kill proof comes from the production runtime's recorded child exit code. Exceptions preserve exception acknowledgments. A missing boundary or failed action is an invalid `missed_fault` trial even if the task finishes normally.

Recovery is a new process that reads existing production facts. The original run/step IDs and argv come from the initial evaluator manifest. The production fixed-task adapter continues unstarted work or reuses committed success. `RecoveryManager.scan()` handles orphaned running calls. Queryable exact-task receipts are passed to the production `reconcile(CONFIRMED_SUCCEEDED)` API; the adapter never fabricates an output. Opaque uncertainty remains interrupted and requests human reconciliation. Recovery-disabled groups are incomplete after a crash; they are not automatically classified as requiring reconciliation.

## Raw contract and evidence

Top-level schema: `reliagent.comparison.v1`. The bundle includes `configurations`, `source`, `environment`, `schedule`, `assigned_trials`, `skipped_matrix`, and `trials`. `source` includes HEAD, a tracked-source diff digest, and a digest over all Python implementation files, including dirty/new files. Secrets and checkpoint keys are never included. The mechanism does not use encrypted Agent checkpoints; its fixed task is persisted in production run/step/call tables and an evaluator manifest.

Each row has:

- Identity: `trial_id`, `layer`, `config_id`, `case_id`, `workload`, `repetition`, `artifact_dir`.
- Injection: `fault_point`, `boundary_label`, `fault_type`, `fault_triggered`, `valid`, `initial_exit_code`, `child_exit_code`.
- Outcome: `outcome`, `task_completed`, `human_intervention`, `human_assisted_completion`, `runtime_status`, `error`.
- Observation: nullable `effect_count` and `unsafe_replay_count`, plus explicit availability flags. Missing or malformed logs remain unknown.
- Cost: `elapsed_seconds`, nullable `recovery_seconds`, `interruption_to_outcome_seconds`, `recovery_censored`, `sql_write_statements`, `sql_write_transactions`, `warmup`.

Every trial retains its manifest, production SQLite database, redacted `trace.json`, independent effect/dispatch/observer/SQL ledgers, fault handshake and response if applicable, separate worker stdout/stderr, worker result, and supervisor `result.json`. A dispatch log records attempted starts against prior unresolved durable effect calls. Unsafe replay is derived from that evidence, never inferred from duplicates. Children self-register their PID and production process token before work and record exit in `finally`. Cleanup targets only unretired trial-owned child groups; exited or previously cleaned IDs are never signalled again on resume.

SQL write statements are counted by tracing actual connections, including schema writes, DML, and retry/recovery phases. Successful explicit commits are counted after SQLite returns; successful autocommit writes are counted separately. Rolled-back write statements remain statements but do not count as committed transactions. No event-count proxy is used. The extra fsync operations used to retain measurement evidence affect wall time; the timing is instrumented end-to-end elapsed time, including startup, and is not production microbenchmark latency. At an arbitrary deadline, counters retain only confirmed observations up to termination.

## Reading the report

Primary completion uses all assigned repetitions; invalid/missed and not-yet-observed scheduled trials remain visible. Automatic and human-assisted completion are separate. Duplicate and unsafe-replay rates use observed side-effect trials with unknown counts disclosed. An observed zero from a preinitialized intact effect ledger is valid; a missing ledger is not zero. Hazard rates do not establish exactly-once guarantees.

Rates show n/N and Wilson 95% intervals. Latency summaries show n, median, nearest-rank P95, minimum and maximum. Restart-to-completion and restart-to-safe-block are separate; unfinished samples are censored, never zero. Full interruption-to-outcome and human waiting are separate. No operator action or waiting interval is simulated.

Reports stratify by layer, group, workload, case, fault point and fault type. Matched eligible A–D observations produce B−A, D−C, C−A, D−B and D−B−C+A completion differences, both overall and by stratum. They are descriptive; small smoke samples support no significance claim. The normal controls report time and SQLite cost by configuration. D_agent–E live overhead includes implementation and provider variability and is not attributed to retry/recovery flags.

The historical 90/90 and 24/24 scenario assertion suites remain regression contracts. New measurement tests check evidence and arithmetic; they do not increase a benchmark reliability score.
