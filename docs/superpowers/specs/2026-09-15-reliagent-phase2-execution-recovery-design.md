# ReliAgent Phase 2: Approval, Execution, and Recovery Design

**Date:** 2026-09-15  
**Status:** approved
**Depends on:** Phase 1 runtime state and SQLite store  
**Source of requirements:** `ReliAgent-需求文档-v1.1.md`

## 1. Objective

Phase 2 turns a persisted ToolCall into a controlled operation. It must prove that
tool risk is classified before execution, mutating or external-effect operations
cannot start without durable approval, subprocesses have bounded lifetimes, retry
creates a new attempt, and restart recovery never silently replays an unknown
side effect.

This phase builds a standalone runtime execution boundary. It does not yet replace
the current `Agent._exec_tool` path. Phase 4 will connect the runtime to the
repo-maintenance workflow through an adapter, preserving a fair legacy-versus-
runtime comparison.

## 2. Chosen Architecture

Use a sidecar `RuntimeExecutor` inside `corecoder.runtime`.

Rejected alternatives:

1. Directly rewrite `Agent._exec_tool`: faster end-to-end integration, but mixes
   persistence, interactive consent, parallel execution, and legacy behavior in
   one change.
2. Extend only `Permission` and `BashTool`: smaller, but approvals remain
   session-local and `subprocess.run()` cannot provide the required recovery or
   process-group lifecycle.

The sidecar keeps each concern independently testable and leaves the 1,171-line
teaching engine unchanged during this phase.

## 3. Scope and Non-goals

### In scope

- deterministic tool policy lookup with deny-safe defaults;
- durable allow-once or deny approval;
- subprocess start, output capture, timeout, and cancellation;
- best-effort cooperative execution for in-process tools;
- bounded retry of explicitly retryable, idempotent failures;
- restart scanning and conservative reconciliation of interrupted calls;
- fault-injection tests at persistence/process boundaries;
- event identity and transaction consistency.

### Not in scope

- permanent approval, RBAC, multiple users, or remote approval;
- arbitrary Python thread termination;
- distributed workers or message queues;
- generic exactly-once delivery;
- automatically pushing Git branches, creating pull requests, or installing
  dependencies;
- modifying the existing Agent loop;
- Trace export, metrics reports, and eval aggregation, which belong to Phase 3.

## 4. P0 Concurrency Rule

One Run may have at most one active ToolCall in Phase 2. Active means `created`,
`waiting_approval`, or `running`; `created` reserves the slot during the short
submission transaction before policy handling. The executor uses an in-process
per-Run lock for coordination and the Store enforces the persisted precondition
inside a `BEGIN IMMEDIATE` transaction.

This intentionally serializes multiple tool requests. It prevents ambiguous Run
states such as one call waiting for approval while another is running. Parallel
runtime execution may be introduced later with a different Run aggregation model.

Cancellation and call creation use serialized Store transactions:

- if creation commits first, Run cancellation must cancel that non-terminal call;
- if cancellation commits first, later creation is rejected;
- a terminal Run never accepts a new ToolCall.

## 5. Components

### 5.1 `policies.py`

`ToolPolicy` is a frozen record with:

- `risk_level: RiskLevel`;
- `execution_kind: ExecutionKind`;
- `timeout_seconds: int`;
- `max_attempts: int`;
- `idempotent: bool`;
- `auto_retry: bool`;
- `retryable_failures: frozenset[FailureKind]`;
- `output_limit: int`.

`FailureKind` contains deterministic runtime classifications:

- `nonzero_exit`;
- `timed_out`;
- `spawn_error`;
- `execution_error`;
- `cancelled`;
- `termination_unknown`.

`ToolPolicyRegistry.resolve(tool_name)` returns an explicitly registered policy.
Unknown tools receive a non-idempotent `external_effect` policy with automatic
retry disabled. Invalid policies such as `max_attempts < 1`, non-positive timeout,
or `auto_retry=True` with `idempotent=False` are rejected during construction.

Initial built-in defaults classify read/search operations as read-only, while
file writes, shell execution, package installation, Git mutation, MCP calls, and
sub-agent spawning require approval. A policy classification is not a sandbox;
approval does not make an unsafe command safe.

### 5.2 Approval model and `approvals.py`

Add these values:

- `ApprovalStatus`: `pending`, `approved`, `denied`;
- `ApprovalDecision`: `allow_once`, `deny`.

`ApprovalRecord` contains:

- `id`, `tool_call_id`, `status`, and optional `decision`;
- `tool_name`, bounded `arguments_summary`, `workspace`, and `risk_reason`;
- `requested_at` and optional `resolved_at`.

There is one approval per ToolCall attempt. A retry is a new ToolCall and requires
a new approval when its policy requires one.

Store operations:

- `request_approval(tool_call_id, summary, workspace, reason)` atomically creates
  the Approval, moves the ToolCall to `waiting_approval`, moves the Run to
  `waiting_approval`, and appends `approval.requested`;
- `resolve_approval(approval_id, decision)` atomically resolves the Approval,
  returns the Run to `running`, and appends `approval.resolved`;
- denial also moves the ToolCall to `cancelled` in the same transaction;
- approval leaves the ToolCall in `waiting_approval` until the executor performs
  the atomic transition to `running` immediately before process start.

Both approval events include Store-owned `approval_id` and `tool_call_id`. Raw
arguments, environment variables, tokens, and secrets are not written to events.
The human-facing summary is bounded to 1,000 characters.

Read-only policy bypasses Approval creation. Mutating, external-effect, and unknown
tools require approval. Phase 2 supports no session-wide or permanent grant.

### 5.3 `processes.py`

`ProcessSpec` contains an argument vector, working directory, optional explicit
environment allowlist, timeout, output limit, and termination grace period. The
runner does not implicitly invoke a shell. A future Bash adapter must explicitly
construct `/bin/sh -lc <command>` on POSIX or `cmd /d /s /c <command>` on Windows,
after approval.

`ManagedProcessRunner.run(spec, cancel_event)` returns `ProcessResult` with:

- exit code or `None` when unknown;
- bounded stdout and stderr;
- duration;
- `FailureKind | None`;
- whether termination was confirmed.

Lifecycle:

1. Start with `subprocess.Popen` only after the ToolCall is persisted as running.
2. POSIX uses `start_new_session=True`; timeout/cancellation sends `SIGTERM` to
   the process group, waits the grace period, then sends `SIGKILL` if required.
3. Windows uses `CREATE_NEW_PROCESS_GROUP`, followed by direct terminate/kill as
   a standard-library best effort. If child termination cannot be confirmed, the
   result is `termination_unknown`.
4. Timeout with confirmed termination maps to `timed_out`; user cancellation with
   confirmed termination maps to `cancelled`; unconfirmed termination maps to
   `interrupted`.
5. Output returned to the caller is bounded by the policy. Phase 1 continues to
   persist at most a 2,000-character result summary.

No API claims that an arbitrary Python callable or worker thread can be forcibly
stopped. In-process execution checks cancellation before and after invocation;
the callable may optionally cooperate with a cancellation event.

### 5.4 `executor.py`

`RuntimeExecutor` owns policy resolution and coordinates Store, approval, process,
and retry behavior. It does not own user interaction.

Submission flow:

1. Resolve policy and reject a Run that cannot accept work.
2. Persist a ToolCall before any operation starts.
3. If approval is required, persist the request and return `PendingApproval`.
4. Otherwise execute immediately.
5. An approved call is executed only when the Approval belongs to that exact
   ToolCall and is `approved`.
6. Transition the call to `running` before invoking the operating system.
7. Persist the terminal or interrupted state and its event after execution.

The unavoidable OS boundary is explicit: SQLite cannot atomically commit together
with `Popen` or an external side effect. A crash after `running` is persisted but
before spawn produces a conservative false-positive `interrupted`; a crash after
an effect but before terminal persistence produces an unknown outcome. Both are
safer than guessing success or automatically replaying a high-risk action.

`cancel_run(run_id)` first persists Run cancellation so new calls are rejected,
then signals any registered active process. All non-terminal calls are settled as
`cancelled` when termination is confirmed or `interrupted` when it is not.

### 5.5 Retry behavior

Automatic retry requires every condition below:

- source status is `failed` or `timed_out`;
- source ToolCall is idempotent;
- `ToolPolicy.auto_retry` is true;
- the observed `FailureKind` is listed in `retryable_failures`;
- `source.attempt < max_attempts`;
- the Run is still `running`.

The retry creates a new ToolCall through the existing Store lineage contract.
Mutating file operations, dependency installation, Git mutation, ordinary test
commands, external effects, cancellation, and unknown termination are not retried
by default.

### 5.6 `recovery.py`

`RecoveryManager.scan()` is deterministic and never starts work. It reads
non-terminal Runs and ToolCalls, then atomically:

- changes every legacy `running` ToolCall to `interrupted`;
- changes its Run from `running` or `waiting_approval` to `recoverable` when an
  unresolved interrupted call exists;
- preserves a pending Approval when no process had started;
- skips terminal ToolCalls;
- emits recovery events with ToolCall identity.

It returns `RecoveryCandidate` records with one of:

- `retry_allowed`: read-only, idempotent, policy-approved, within retry budget;
- `human_required`: mutating, external-effect, unknown, or non-idempotent;
- `approval_pending`: the call never entered running and still awaits a decision;
- `ready_to_start`: approval was committed but the call never entered `running`;
- `skip_terminal`: returned only by diagnostic APIs, never scheduled.

`ready_to_start` is safe from duplicate process execution because the ToolCall is
still `waiting_approval`; it starts only after an explicit resume request. The
existing allow-once Approval remains bound to that exact attempt and need not be
requested again.

An interrupted record never returns directly to `running`. Resolution is explicit:

- confirmed success: `interrupted -> succeeded`;
- confirmed no effect/failure: `interrupted -> failed`;
- abandon: `interrupted -> cancelled`.

Final-review clarification: startup also discovers a `created` reservation as
`human_required` with reason `created_not_started`. It remains CREATED until an
explicit `ABANDON` atomically cancels it and records `recovery.resolved`. This
never-started state does not justify success/failure confirmation or automatic
execution. Repeated scans are stable; a disposition rechecks status and refuses
to cancel work that has since started. The caller may submit a fresh request
after abandonment, including its normal policy/approval checks. Reserved ordinary
and recovery retries follow the same rule.

For `retry_allowed`, resume first resolves the interrupted source as failed with a
`process_lost` reason, then creates a new attempt. For `human_required`, the user
must reconcile external state. If they confirm no effect and request another try,
the new attempt goes through a fresh approval.

## 6. State and Event Mapping

| Operation | State change | Event |
|---|---|---|
| Request approval | ToolCall `created -> waiting_approval`; Run `running -> waiting_approval` | `approval.requested` |
| Approve | Approval `pending -> approved`; Run `waiting_approval -> running` | `approval.resolved` |
| Deny | Approval `pending -> denied`; ToolCall `waiting_approval -> cancelled`; Run returns to `running` | `approval.resolved` |
| Start process | ToolCall `created/waiting_approval -> running` | `tool.started` |
| Exit zero | ToolCall `running -> succeeded` | `tool.completed` |
| Exit nonzero | ToolCall `running -> failed` | `tool.failed` |
| Confirmed timeout | ToolCall `running -> timed_out` | `tool.timed_out` |
| Confirmed cancellation | ToolCall `running -> cancelled` | `tool.cancelled` |
| Lost/unconfirmed process | ToolCall `running -> interrupted`; Run `running -> recoverable` | `tool.interrupted` |
| Schedule retry | New ToolCall with `retry_of` and incremented `attempt` | `tool.retry_scheduled`, then `tool.created` |
| Cancel Run | Run non-terminal -> `cancelled`; settle active call | `run.cancelled` plus tool event |

Where one user operation changes Approval, ToolCall, Run, and Event together, the
Store exposes one purpose-specific transaction rather than composing separate
public state-transition calls.

## 7. Error Handling

- Missing or mismatched approval: return a typed execution refusal; do not spawn.
- Duplicate approval resolution: reject without changing state or adding an event.
- Invalid policy: fail at policy construction, before persistence or execution.
- Spawn failure: persist `failed`; retry only if policy explicitly classifies it.
- In-process callable exception: classify as `execution_error` and persist
  `failed`; it is not mislabeled as a subprocess spawn failure.
- Nonzero exit: persist `failed` with bounded output and exit code.
- Timeout/cancel with confirmed termination: use the corresponding terminal state.
- Termination cannot be confirmed: persist `interrupted`, never claim completion.
- Store failure before spawn: no process starts.
- Store failure after the process exits: the next scan observes `running` and uses
  conservative recovery; high-risk work is not replayed.
- Cancellation races use Store state as the final authority.

## 8. Planned File Boundaries

| File | Responsibility |
|---|---|
| `corecoder/runtime/policies.py` | Tool policy and failure classification |
| `corecoder/runtime/approvals.py` | Approval enums, records, and presentation-safe request data |
| `corecoder/runtime/processes.py` | Cross-platform subprocess lifecycle |
| `corecoder/runtime/executor.py` | Submission, approval gate, execution, cancellation, retry |
| `corecoder/runtime/recovery.py` | Startup scan and explicit reconciliation decisions |
| `corecoder/runtime/models.py` | Add shared persisted records where appropriate |
| `corecoder/runtime/store.py` | Approval schema and purpose-specific atomic operations |
| `tests/runtime/test_policies.py` | Default risk and invalid policy tests |
| `tests/runtime/test_approvals.py` | Durable approval and transaction tests |
| `tests/runtime/test_processes.py` | Exit, output, timeout, process-group, cancellation tests |
| `tests/runtime/test_executor.py` | No-approval-no-spawn, Run cancellation, retry tests |
| `tests/runtime/test_recovery.py` | Crash states and replay-safety tests |

The Store may gain private row/event helpers to control duplication, but Phase 2
will not split it into repositories or introduce an ORM.

## 9. Verification Strategy

All production behavior follows RED -> GREEN -> REFACTOR. Tests use temporary
SQLite databases and `sys.executable -c ...` subprocess fixtures; they do not
depend on network access or user shell configuration.

Required deterministic proofs:

1. Registered read-only tools bypass Approval; unknown tools require it.
2. A command that would create a marker file creates nothing without Approval.
3. Denial creates no process and settles the exact ToolCall as cancelled.
4. Approval for call A cannot authorize call B.
5. Timeout kills a POSIX child process group; Windows runs a best-effort direct
   termination test and records uncertainty conservatively.
6. Run cancellation prevents later ToolCall creation and terminates active work.
7. Only an allowed idempotent transient failure creates a new attempt.
8. Retry lineage and event identity identify both attempts.
9. Recovery changes an orphaned running call to interrupted.
10. Recovery starts zero unknown high-risk operations.
11. A safe recovery retry first resolves the source, then creates a new attempt.
12. Approval, state, and event changes roll back together on injected DB failure.
13. Existing Phase 1 and repository tests remain green.
14. New files pass Ruff and compileall; pre-existing repository lint debt remains
    separately reported unless the user asks to fix it.

## 10. Completion Criteria

Phase 2 is complete only when:

- every required proof above has an automated test;
- the public runtime API exposes only intentional stable symbols;
- one demonstration shows read-only execution, denied mutation, approved process,
  timeout, retry lineage, and crash recovery;
- no claim of generic exactly-once or arbitrary thread termination appears in code,
  tests, documentation, or resume wording;
- the stage is reviewed and the full repository regression result is recorded.
