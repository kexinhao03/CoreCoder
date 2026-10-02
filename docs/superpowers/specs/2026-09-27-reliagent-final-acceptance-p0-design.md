# ReliAgent Final-Acceptance P0 Repair Design

Date: 2026-09-27

Status: proposed

Scope: minimum portfolio-delivery repair for the four P0 findings in the final acceptance audit

## 1. Objective and non-goals

This change makes the repository's formal product path and public claims match
the behavior that can be reproduced from code, SQLite, tests, and generated
evidence. It removes four delivery blockers: the disconnected Agent CLI,
plaintext durable secrets, a mislabeled Evaluation baseline, and unclear fork
ownership.

This iteration does not implement a true external no-Runtime baseline, the P1
Metrics/Trace expansion, a second workflow, general exactly-once execution, or
Run-scoped token/cost telemetry. Those remain explicit limitations. Merging the
branch into `main` and creating a release/tag are separate publication actions
that require user authorization after verification.

## 2. Formal Agent Runtime path

### 2.1 CLI lifecycle

The standard `corecoder` CLI will enable the durable Agent Runtime by default.
It will accept `--runtime-workspace PATH` to choose the workspace and database
root, and `--no-runtime` as an explicit compatibility escape hatch. With no
override, the workspace is the current working directory and the database is
`<workspace>/.reliagent/agent-runtime.sqlite`.

Startup will initialize the store, create one `corecoder_agent` Run, transition
it to `running`, and replace only `read_file` and `write_file` in the built-in
tool list with `RuntimeToolAdapter` instances. MCP and all other built-in tools
remain unchanged.

The Run covers the CLI session:

- successful one-shot completion or normal interactive exit -> `succeeded`;
- CLI error -> `failed`;
- `KeyboardInterrupt` that terminates one-shot execution -> `cancelled`;
- an interactive turn interruption leaves the session Run active because the
  REPL remains usable; final exit closes it as `succeeded`.

Terminal transitions are idempotent so cleanup cannot overwrite an already
terminal Runtime decision.

### 2.2 Approval ownership

`RuntimeToolAdapter` will declare that it owns Runtime approval. Agent plan mode
still blocks mutation before execution, but the Agent permission layer skips its
ordinary mutation prompt for Runtime-managed tools. The adapter maps the
existing session `Permission` decision to a durable `allow_once` or `deny`:

- `--yes` -> durable `allow_once`;
- one-shot without `--yes` -> durable denial;
- interactive `once` or remembered `always` -> durable `allow_once` for each
  concrete ToolCall;
- refusal -> durable denial.

This gives one user decision and one durable Approval per write, not two
independent consent prompts.

### 2.3 Offline acceptance test

An offline scripted-LLM CLI construction test will use the same production
builder as `main()`, execute one read and one approved write, then query the
generated SQLite database. It must find one Run, two Steps, two ToolCalls,
Approval and lifecycle Events in that Run, and the Agent must receive both Tool
results.

## 3. Durable redaction and replayability

### 3.1 Two representations

Runtime submission will separate:

1. **execution arguments** — the original object, kept only in the current
   Executor process long enough to execute or retry the concrete call;
2. **audit arguments** — a copied, redacted value stored in
   `tool_calls.arguments_json` and returned by Store queries.

The schema gains `arguments_replayable INTEGER NOT NULL DEFAULT 1`, exposed on
`ToolCallRecord`. It is true only when the audit representation is byte-for-byte
equivalent to the execution representation. A changed representation is safe
for audit but not sufficient for cross-process replay.

For `write_file`, `content` is always replaced by a non-secret audit descriptor
containing its byte length and SHA-256 rather than the source text. For generic
dict keys and argv flags, the existing sensitive-key and `--flag value` rules
apply. Free-form strings also redact bearer credentials, URL user-info, and
credential-shaped tokens including common `sk-...` values and values containing
`secret`, `token`, `password`, or `api-key` markers.

`SQLiteStore.initialize()` will also migrate existing rows: each legacy
`arguments_json` and `result_summary` is redacted in one transaction, and any
row whose arguments change is marked non-replayable. This prevents an upgraded
workspace from retaining plaintext that was written by an older release.

### 3.2 Execution and retry behavior

Initial execution and same-process automatic retries use the original
in-memory arguments. Approval completion in the same CLI process also uses that
ephemeral copy. The raw value is removed from the Executor cache after terminal
completion or cancellation.

Recovery and approved execution in a different process are allowed only when
`arguments_replayable` is true. Otherwise Runtime refuses automatic execution
with a stable `sensitive_arguments_not_replayable` reason and leaves the work
for explicit human reconciliation. `[REDACTED]` is never passed to a tool as if
it were the original secret.

Existing ML workflow argv contains no secrets, so its recovery behavior remains
replayable and unchanged.

### 3.3 Results and errors

The raw, output-limited Tool result still returns to the active Agent. Before
SQLite persistence, `result_summary`, exception text, stdout, and stderr pass
through text redaction. Event payloads continue to use recursive redaction.

SQLite-level tests will query raw table cells, not Trace exports, and assert
that known argv, output, and exception secrets are absent. Tests also prove
that the active Agent receives the raw non-secret functional result and that a
redacted call cannot be replayed after a new Executor is constructed.

## 4. Honest Evaluation configuration

The current `baseline` configuration is a Runtime ablation, not an external
baseline. It will be renamed everywhere to `no_retry_no_recovery`; `full` and
`no_recovery` retain their meanings.

The ML helper that directly forces ToolCall, Step, and Run into an expected
failed terminal state will be deleted. After a real exit, the scenario will use
the production orphan-discovery path to observe an unresolved/recoverable
Runtime state. Contract assertions will describe that observation instead of
manufacturing failure.

All reports, README tables, tests, evidence paths and resume bullets will stop
claiming comparison with a no-persistence baseline. The limitation will say
that a true external baseline is not implemented in this release.

The same case input and fault schedule remain shared by all three Runtime
configurations. Every scenario continues to call `RuntimeExecutor`; the renamed
configuration makes that fact explicit rather than contradictory.

## 5. Fork-first delivery surface

README and README_CN will open with ReliAgent as a reliability-runtime fork of
CoreCoder. The first screen will state:

- upstream repository and fixed fork baseline;
- ReliAgent additions and their source directories;
- evidence-backed claims and explicit limitations;
- project owner link `kexinhao03` and clear credit to original author Yufeng He.

Badges, clone commands, project links, issue links, and package URLs will point
to `kexinhao03/CoreCoder`. Upstream essays and assets may keep upstream links
when they are explicitly labeled as upstream material. `pyproject.toml` will
retain upstream authorship and add `kexinhao03` in the PEP 621 `maintainers`
field rather than replacing history.

CI will run on pushes to every branch and pull requests targeting `main`, so the
release candidate SHA receives a workflow run. The workflow will continue to
test Python 3.10–3.13 on Linux, macOS, and Windows and retain package and Ruff
jobs.

README will name the deferred P1 fields, lack of external baseline, single ML
workflow, and local-fixture Effect Marker boundary. It will not claim all six
strict-v2 priorities are complete.

## 6. Evidence migration

The existing `evidence/reliagent/c724f8a/` snapshot remains historical evidence
for that Runtime commit, but it cannot substantiate the repaired configuration
names or storage behavior. A new evidence directory keyed by the repaired
Runtime commit will be generated only after production changes are committed.

Generation order:

1. commit the repaired Runtime and tests;
2. run Phase 3 and ML evaluations from that clean commit;
3. check in Raw JSON, generated Markdown, and SHA-256 manifest under the new
   commit prefix;
4. update README and resume evidence to cite the new snapshot;
5. create a final evidence commit and verify `corecoder/` is unchanged from the
   evaluated Runtime commit.

No generated Raw JSON will be hand-edited. Machine-specific paths and UUIDs may
differ, while aggregate contracts must be recomputed from the checked-in data.

## 7. TDD and verification gates

Each behavioral repair begins with a focused failing test:

- production CLI builder does not currently create Runtime-backed tools;
- raw SQLite currently contains known argument and result secrets;
- Evaluation still exposes `baseline` and directly forces terminal state;
- CI/README metadata is handled as a reviewed configuration/document change,
  while executable links and package metadata receive existing build checks.

The final release candidate must pass:

```bash
python -m pytest tests/ -q
python -m ruff check corecoder tests
python -m compileall -q corecoder tests
python -m build
python -m twine check dist/*
python -m corecoder.reliagent_cli eval ml_workflow --output /tmp/reliagent-ml
python -m corecoder.reliagent_cli eval phase3 --output /tmp/reliagent-phase3
PYTHON_BIN=python sh scripts/reliagent_ml_demo.sh /tmp/reliagent-demo
```

Additional gates:

- direct SQLite probes find none of the seeded argument, output, or exception
  secrets;
- production Agent CLI read/write calls are queryable in one durable Run;
- regenerated reports use `no_retry_no_recovery`, `full`, and `no_recovery`;
- `git status --short` is empty;
- GitHub Actions is green for the pushed release-candidate SHA before any main
  merge or tag is proposed.
