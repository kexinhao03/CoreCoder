# ReliAgent Lightweight ML Experiment Workflow Design

## Goal

Add one deterministic non-coding workflow that proves the existing ReliAgent
runtime can execute and recover a small machine-learning experiment. The
workflow must perform a local environment check, stop for approval before the
experiment writes files, survive an injected process-loss boundary, extract
JSON metrics, prove that repeated resume does not duplicate the experiment
effect, and generate a final evidence report.

This is an extensibility proof, not a benchmark, AutoML system, notebook
runner, dependency manager, or reproduction of a large research project.

## Scope and Safety Boundaries

The workflow uses only the Python standard library and packaged local fixture
data. It does not read API credentials, use an LLM, install packages, access
the network, create a virtual environment, or modify the user's source tree.
All generated files stay under a workspace explicitly supplied to the CLI.

The injected process loss applies only to the read-only, idempotent environment
check. RecoveryManager may classify that orphan as `retry_allowed` and create a
new attempt. The experiment writes files and therefore uses a mutating,
non-idempotent policy with one-shot approval and no automatic retry. If its
outcome becomes unknown, the Run remains `recoverable` until a human explicitly
reconciles it. The workflow never treats approval as a sandbox or claims
exactly-once external effects.

## Considered Approaches

1. A dedicated `corecoder.workflows.ml_experiment` orchestrator over the
   existing Runtime. This keeps workflow rules out of Runtime and provides a
   coherent CLI/demo surface. This is the selected approach.
2. A static generic task JSON plus loose scripts. This is smaller but leaves
   fixture discovery, metric validation, and report generation scattered and
   looks like a shell wrapper rather than a reusable Workflow.
3. An Evaluation Harness scenario. This reuses reporting but conflates a
   product Workflow with a fault-test case and cannot serve as an independent
   non-coding demo.

## Package Structure

```text
corecoder/workflows/
  __init__.py
  ml_experiment/
    __init__.py       public workflow API
    workflow.py       fixed steps, policies, start/resume orchestration
    report.py         derived JSON and Markdown evidence
    fixture/
      check.py        environment and fixture integrity check
      train.py        deterministic ordinary-least-squares experiment
      extract.py      metric schema and threshold validation
      data.csv        fixed local regression dataset
tests/workflows/
  test_ml_experiment.py
  test_ml_experiment_cli.py
```

The fixture is package data located relative to the workflow module. The
workflow passes explicit absolute paths to every script and output. It never
constructs shell command strings and never enables `shell=True`.

## Experiment Contract

The fixture contains a small two-column `x,y` regression dataset. `train.py`
computes ordinary least squares deterministically and writes `metrics.json`
atomically through a temporary file followed by `Path.replace`. It appends one
line containing the stable experiment id to `effects.log` before publishing
the metrics file. Re-execution would append another line, so duplicate effects
remain observable rather than being overwritten.

`metrics.json` has a versioned schema with:

- `schema_version`;
- `experiment_id`;
- `algorithm` (`ordinary_least_squares`);
- `dataset_sha256`;
- `sample_count`;
- `parameters.intercept` and `parameters.slope`;
- `metrics.mse` and `metrics.r2`.

`extract.py` validates the schema, experiment id, dataset digest, finite numeric
values, sample count, and fixed acceptance thresholds. It prints one normalized
JSON object to stdout. The Runtime persists that output as the successful
ToolCall result; the report parses this persisted output instead of trusting
unrecorded in-memory values.

## Persisted Workflow and Runtime Integration

The Run metadata is fixed to `workflow="ml_experiment"`,
`model="deterministic"`, and a versioned workflow prompt/config identifier.
The ordered persisted Steps are:

1. `ml_environment_check`: read-only, idempotent subprocess. It verifies the
   Python version, fixture files, dataset digest, workspace existence, and that
   requested outputs remain inside the workspace.
2. `ml_experiment`: mutating, non-idempotent subprocess. It requires explicit
   allow-once approval and writes `metrics.json` plus `effects.log`.
3. `ml_extract_metrics`: read-only, idempotent subprocess. It validates the
   result and emits normalized JSON.
4. `ml_verify_effect`: read-only, idempotent subprocess. It verifies exactly
   one marker and emits the observed count.

`ReliAgentRuntime` gains only the minimal public capability needed by a
specialized Workflow: creating a fixed persisted task separately from executing
pending Steps, plus optional Run metadata. Existing `run_task` behavior and
defaults remain compatible. The Workflow owns its fixed step definitions and
policy registry; Runtime never imports from `corecoder.workflows`.

The Workflow validates on every resume that the stored Run has the expected
workflow name and workspace, that all four Step titles match in sequence, and
that persisted ToolCall arguments match the deterministic step definitions.
Mismatch aborts without executing a subprocess.

## Fault Injection and Recovery

For tests and the demo, `workflow ml start --inject-process-loss` supplies a
one-shot ProcessRunner wrapper for the environment-check ToolCall. The wrapper
raises a dedicated injected-loss exception only after RuntimeExecutor has
persisted `tool.started`. The CLI catches that exception, prints the Run id and
an explicit injected-fault result, and exits nonzero. The ToolCall remains
`running`, matching a process that disappeared before recording completion.

A later `workflow ml resume` uses a normal executor. RecoveryManager scans the
orphan, persists the interrupted/process-lost facts, checks the read-only and
idempotent policy, creates a retry attempt, and executes it through
`RuntimeExecutor.execute_recovery_subprocess`. The Workflow then continues only
until the experiment approval boundary.

After approval, another resume executes the experiment and validation Steps.
A resume of an already successful Run is an inert operation: no new ToolCall,
Event, marker, metric, or report content is created. Tests compare persisted
facts and the append-only marker before and after the repeated resume.

The fault flag is unavailable for the mutating experiment Step. Unknown
experiment outcomes follow the existing human-reconciliation path and are not
automatically replayed.

## CLI Contract

The existing `reliagent` executable adds:

```text
reliagent workflow ml start \
  --workspace <isolated-directory> --database <runtime.sqlite> \
  [--inject-process-loss]

reliagent workflow ml resume <run-id> \
  --workspace <same-directory> --database <runtime.sqlite>

reliagent workflow ml report <run-id> \
  --workspace <same-directory> --database <runtime.sqlite> \
  --output <report-directory>
```

The existing `list`, `approve`, `deny`, `reconcile`, `cancel`, and `trace`
commands remain the control plane. Start and resume return JSON containing the
Run id, status, current approval/recovery requirements, and report paths only
when the Run has succeeded. `report` refuses non-succeeded Runs or invalid
metrics rather than producing partial success evidence.

## Report Contract

The report generator is a read-only derivation over SQLite facts plus the two
workflow output files. Writing the requested report is an explicit CLI output
operation, analogous to Evaluation Harness report generation, not a hidden
Runtime ToolCall.

It writes:

- `ml-experiment-result.json`: schema version, Run id/status, experiment
  metrics, effect count, Trace integrity, derived Runtime metrics, and explicit
  unavailable Token/Cost information;
- `ml-experiment-report.md`: human-readable experiment parameters, MSE/R2,
  approval evidence, recovery/retry counts, duplicate-effect result, Trace
  integrity, and reproduction commands.

Markdown is rendered exclusively from the raw JSON object. Regeneration from
unchanged facts is deterministic except for no generated timestamp being
included, so byte-for-byte equality is testable.

## Error Handling

- Missing or altered fixture files: environment check fails before approval.
- Workspace outside the user-supplied directory or output path escape: reject
  before execution.
- Approval denied: experiment does not start; no metrics or marker exists.
- Invalid/missing metrics: extraction fails and the Run fails without a report.
- Process loss during environment check: safe recovery retry is permitted.
- Unknown mutating experiment result: no auto-retry; human reconciliation is
  required.
- Duplicate marker: verification fails and the report cannot claim success.
- Repeated successful resume: returns the terminal Run without writes.

## Tests and Acceptance Criteria

TDD tests use temporary workspaces and the real SQLiteStore and
RuntimeExecutor. No test directly fabricates successful Run or ToolCall rows.

Required tests prove:

1. fixture training produces the exact deterministic schema and accepted
   metrics using only the standard library;
2. start executes the environment check and stops at experiment approval;
3. denial creates no metrics or effect marker;
4. approval and resume execute all Steps and persist Run, Step, ToolCall,
   Approval, and Event evidence;
5. injected environment-check loss leaves a running ToolCall, and a fresh
   Workflow instance recovers it through RecoveryManager/RuntimeExecutor;
6. repeated successful resume leaves ToolCalls, Events, metrics, marker, and
   report bytes unchanged;
7. effect verification detects a second appended marker;
8. raw JSON and Markdown reports contain the same experiment and Runtime facts;
9. CLI subprocess tests demonstrate the approval/recovery sequence across
   separate process invocations;
10. full pytest, `ruff check .`, compileall, and README line-count consistency
    pass without installing dependencies.

Completion includes one stable commit and a real temporary-directory demo with
the generated metrics, raw report, Markdown report, and fixed commit hash.
