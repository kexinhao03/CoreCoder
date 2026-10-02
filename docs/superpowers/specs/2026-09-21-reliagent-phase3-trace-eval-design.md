# ReliAgent Phase 3 Trace and Evaluation Design

## Goal

Turn the Phase 2 SQLite runtime facts into stable, redacted Trace exports,
derived per-Run metrics, and repeatable deterministic evaluation results.
The phase must produce evidence, not a second source of runtime truth.

## Scope

Phase 3 adds:

- stable JSON Trace export ordered by persisted Event sequence;
- recursive payload redaction at the export boundary;
- derived Run metrics for execution and recovery evidence;
- deterministic Baseline, Full, and Ablation evaluation configurations;
- machine-readable raw results and a readable Markdown summary;
- tests that prove each result is derived from persisted facts.

Phase 3 does not add a Step model, change runtime state transitions, wire the
existing Agent loop or CLI into `RuntimeExecutor`, create a web/API UI, or
claim exactly-once external effects. Runtime token and cost values are reported
as unavailable because Phase 2 does not persist LLM usage per Run.

## Existing Facts and Authority

`SQLiteStore` owns `runs`, `tool_calls`, `approvals`, and `events`. State
changes and their lifecycle events are already written in SQLite transactions.
`list_events(run_id)` returns `EventRecord` values ordered by increasing
`sequence`; it is the only Trace input. `get_run`, `list_tool_calls(run_id)`,
and approval records provide the metric inputs.

Trace, metrics, evaluation results, and reports are derived artifacts. They
must never write state, events, approvals, or a metric table. Recomputing a
Trace or metric result from unchanged storage must return the same value.

## Architecture

```
SQLiteStore facts
  ├── TraceService ──> TraceExport (redacted JSON-ready data)
  ├── MetricsService ─> RunMetrics (derived, no writes)
  └── EvaluationRunner
          ├── deterministic scenario execution
          ├── assertion results + raw JSON result
          └── Markdown report comparing configurations
```

### Trace module

`corecoder/runtime/tracing.py` exposes immutable Trace records and a
`TraceService(store)`.

`TraceService.export_run(run_id)`:

1. loads the Run and ordered events from the Store;
2. verifies event sequences start at 1 and increment by exactly one;
3. redacts every payload recursively before returning it;
4. returns a JSON-serializable export containing schema version, Run identity,
   ordered events, and a completeness result.

The export must retain event type, sequence, timestamp, and non-sensitive
payload fields. Values whose mapping keys contain `api_key`, `apikey`,
`token`, `password`, or `secret` (case-insensitive) become `"[REDACTED]"`.
Lists and tuples are traversed; dictionary keys are converted to stable strings
only for exported JSON. The Store's original Event payload is not mutated.

Trace completeness is a fact-integrity metric, not a claim that every possible
Agent event exists. A trace is complete only if its event sequence is
contiguous and every persisted ToolCall has the lifecycle event required by its
current status:

- `succeeded` requires `tool.completed`;
- `failed` requires `tool.failed`;
- `timed_out` requires `tool.timed_out`;
- `cancelled` requires `tool.cancelled`;
- `interrupted` requires `tool.interrupted`.

`created`, `waiting_approval`, and `running` calls are nonterminal and do not
contribute a required terminal event. The export exposes missing requirements
explicitly; it never silently reports 100% completeness.

### Metrics module

`corecoder/runtime/metrics.py` exposes immutable `RunMetrics` and
`MetricsService(store)`.

The service derives:

- final Run status;
- elapsed duration from Run `created_at` to the latest event timestamp (or
  zero when no lifecycle event follows creation);
- total and terminal ToolCall counts;
- Tool Success Rate = succeeded ToolCalls / terminal ToolCalls; undefined is
  represented as `None`, not zero;
- timeout count and retry count (`attempt > 1` ToolCalls);
- approval wait duration: sum of resolved approval `resolved_at - requested_at`
  values; unresolved approvals are excluded and counted separately;
- Trace completeness and missing requirement list from `TraceService`;
- token and estimated cost as `None` with an explicit availability flag.

All timestamps are ISO-8601 UTC values written by the Store. The service uses
`datetime.fromisoformat`; malformed persisted timestamps raise an error rather
than producing invented latency numbers.

### Evaluation module

`corecoder/evals/` is independent from production Runtime execution. Its
scenarios create temporary SQLite databases and use deterministic local Python
operations; they never invoke an LLM, network, package manager, or user
workspace.

Each `EvaluationCase` declares an id, description, scenario callable,
configuration, expected assertions, and repeat count. A scenario returns its
actual Run id plus observable effect data. Assertions inspect Store/Trace/
Metrics outputs rather than trusting scenario text.

Configurations have the following exact semantics:

- `baseline`: run a deterministic operation with no recovery retry;
- `full`: execute the same operation with the Phase 2 recovery policy enabled;
- `no_recovery`: use the same Runtime operation but omit explicit recovery
  retry after a simulated process-loss boundary.

Only a fault scenario whose controls differ can compare configurations. Normal
success cases report per-configuration results but do not claim recovery gain.
The initial suite covers normal success, retryable timeout, permanent failure,
approval denial, cancellation, high-risk orphan recovery, and an effect whose
completion persistence fails. Each case is repeated three times. Reports must
show each repetition, not only aggregate values.

The runner reports:

- Task Success Rate;
- Recovery Success Rate for cases designated recovery-capable;
- Duplicate Side-effect Rate, based on scenario effect markers;
- Trace Completeness Rate;
- Tool Success Rate;
- measured elapsed duration;
- unavailable Token/Cost fields;
- Approval Violation Rate, based on attempted unapproved high-risk execution.

Rates with no applicable denominator are `null` and named as not applicable in
the Markdown report. The report must not convert them to 0% or 100%.

## Error Handling and Reproducibility

An assertion failure yields a failed case result with its actual evidence; it
does not abort remaining cases. An unexpected scenario exception is recorded
as an evaluation error and is distinct from a Runtime failure result. Each run
uses a newly created temporary directory and SQLite database. Output ordering
is deterministic: configuration name, case id, then repetition index.

Raw JSON includes report schema version, runtime Git revision supplied by the
caller, configuration, case id, repetition, assertions, metric snapshot, Trace
integrity result, and effect observations. Markdown is rendered only from this
raw result model.

## Verification

Tests will prove:

- Trace ordering, redaction, non-mutation, and missing-event detection;
- metric formulas, unavailable token/cost values, and malformed timestamp
  failure;
- case assertion failure isolation and deterministic report ordering;
- Full versus no-recovery fault outcomes using the same scenario inputs;
- high-risk and uncertain side effects retain a duplicate-side-effect count of
  zero through recovery;
- raw JSON and Markdown contain the same configuration/case totals.

Stage completion requires targeted Trace/Eval tests, Runtime regression tests,
the full test suite, compileall, Ruff, and a generated local report checked
against its raw JSON source.
