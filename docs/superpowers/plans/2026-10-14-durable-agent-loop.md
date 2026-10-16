# Durable Agent Loop Implementation Plan

> **For agentic workers:** Use subagent-driven-development for sequential tasks with task review and final whole-branch review. Follow TDD and record evidence in the plan ledger.

**Goal:** Make CoreCoder's real reasoning loop resumable without regenerating dangerous tool calls.
**Architecture:** Retain CoreCoder's Agent/LLM. Add encrypted SQLite reasoning journal and a single Runtime dispatcher; terminal execution facts and complete tool outputs commit together.
**Tech Stack:** Python 3.10+, SQLite, cryptography Fernet, existing synchronous tools/managed subprocess runner.
**Spec:** `docs/superpowers/specs/2026-10-14-durable-agent-loop-design.md` (user approved).

## Global Constraints
- Audit plaintext remains redacted; raw context, arguments and full bounded results only in authenticated ciphertext.
- Missing/wrong key blocks resume before any LLM/tool call.
- Keep provider tool IDs stable per turn; terminal results cached, unknown calls block all further tools/LLM.
- One active attempt per Run; child Agent uses independent linked Run; no recursive child spawn.
- No general exactly-once or Windows full-process-tree guarantees.
- No production changes on local main; preserve existing user planning files.

## Task 1: Encrypted journal and atomic attempt facts

Files: create runtime/checkpoints.py and runtime/agent_store.py; modify runtime/store.py transaction boundaries; create tests/runtime/test_agent_store.py.

Interfaces (stable public contract for subsequent tasks):
- `CheckpointCipher(key: str | bytes | None = None)` reads RELIAGENT_CHECKPOINT_KEY only if key omitted. `seal(identity: dict, payload: dict) -> bytes`, `open(identity: dict, ciphertext: bytes) -> dict`; errors derive from ValueError.
- `AgentJournal(store: SQLiteStore, cipher: CheckpointCipher)` initializes new tables.
- `create(run_id: str, state: dict)`, `load(run_id: str) -> dict` returning `state`, `phase`, `revision`, `turn_sequence`.
- `save_state(run_id: str, state: dict, *, phase: str = 'ready')`.
- `begin_turn(run_id: str, request: dict) -> int` commits llm_inflight and request before inference.
- `record_response(run_id: str, turn_sequence: int, message: dict) -> list[dict]` commits assistant message and complete queued invocations before execution.
- `list_invocations(run_id: str, turn_sequence: int | None = None) -> list[dict]`; each dict includes id/run_id/turn_sequence/provider_call_id/ordinal/tool_name/arguments/state/attempt_id/result, raw fields decrypted only here.
- `complete_turn(run_id: str, state: dict)` refuses unfinished invocations, sets ready/complete based on response; `block(run_id: str, reason: str)`.
- `SQLiteStore.create_tool_call(..., agent_invocation_id: str | None = None)`: atomic Step + ToolCall + invocation binding when durable invocation supplied, not duplicate separate writes.
- `SQLiteStore.transition_tool_call(..., agent_result: bytes | None = None)`: commits encrypted result, matching model tool message, invocation completion, Step state and Event in same transaction as terminal ToolCall; no encrypted result for interrupted unknown effects.
- `AgentJournal.result_token(invocation_id: str, output: str) -> bytes` creates identity-bound full result for transaction API.
- `AgentJournal.complete_without_attempt(invocation_id: str, output: str)` handles unknown-tool/plan/validation denials atomically and durably.
- `RunLease(workspace: Path, run_id: str)` explicit acquire/release and context manager for nonblocking per-Run OS file lock; process crash releases lock; scope same-host only.

- [ ] Write and observe failing tests, including real transactions:
```python
def test_response_is_durable_before_tool_start(journal, run):
    sequence = journal.begin_turn(run.id, {"messages": []})
    journal.record_response(run.id, sequence, {"role": "assistant", "tool_calls": [
        {"id": "provider-A", "type": "function", "function": {"name": "write_file", "arguments": '{"content":"private"}'}}
    ]})
    assert journal.list_invocations(run.id)[0]["provider_call_id"] == "provider-A"
    assert run.store.list_tool_calls(run.id) == []
```
- [ ] Verify cipher missing/wrong key/tamper/cross-record identity rejection; database dump excludes supplied secret.
- [ ] Implement smallest schema/journal/transaction extension. Inject terminal rollback and assert no split success/output state.
- [ ] Run journal and existing Store suites; report APIs/commit and review.

## Task 2: Runtime unified dispatch and safe resume

Files: corecoder/agent_runtime.py, runtime/executor.py, runtime/policies.py; new runtime agent dispatcher tests.
Consumes Task 1; provides session methods `attach(agent)`, `chat(agent, user_input, on_token=None, on_tool=None)`, `resume(agent, on_token=None, on_tool=None)`.
Existing AgentRuntimeSession legacy library entry may remain compatible; production durable session is explicit and requires key. No silent degraded production mode.

- [ ] Write RED test through real Agent with counting model:
```python
def test_cached_call_is_not_reexecuted(restored_agent, counter):
    restored_agent.resume()
    assert counter.effects == 1
    assert restored_agent.messages[2]["tool_call_id"] == "provider-A"
```
- [ ] Dispatch every registered/unknown tool through journal identity; approval and plan/validation failures persisted.
- [ ] Extend executor with controlled invocation/raw-argument context and terminal result token; preserve workflow behavior when no invocation. Authenticated result state updates may carry todo/cwd in the same terminal transaction; extend Journal narrowly with tests if needed.
- [ ] On resume: completed→cache; queued→same identity; started/unknown→block; llm_inflight→explicit retry authorization; no new input bypass.
- [ ] Restore todo/cwd/plan and validate model/tool/policy signature before continuing. Guard same-host Run ownership.
- [ ] Verify response rollback zero-execution, crash windows and multi-call batch blocking.

## Task 3: CoreCoder Loop and CLI integration

Files: agent.py, cli.py, tools/agent.py, tools/todo.py, mcp.py, hooks.py; new tests/test_durable_agent_loop.py and integration tests.
Consumes session methods from Task 2.

- [ ] Add optional runtime session to Agent; durable chat delegates into existing LLM and tool mechanisms; resume has no new user prompt.
- [ ] CLI defaults to keyed durable runtime; add --resume-run plus status/reconcile actions. Existing -r JSON sessions never impersonate durable Runs.
- [ ] Route /reset,/save,/undo and compression through documented gates; no plaintext transcript export or LLM-summary bypass.
- [ ] Child factory creates/resumes independent linked Runs with fresh tools and cancellation; recover original child instead of spawning a duplicate.
- [ ] MCP transport uncertainty blocks; hooks are persisted suboperations after approval, post-hook failure does not rerun tool.
- [ ] RED/GREEN all built-ins, MCP, child, hooks, restored context. Real-provider opt-in test calls actual configured LLM through production builder and approvals.

## Task 4: Integrated crash verification and delivery

Files: tests/integration/, docs/reliagent/, README.md/README_CN.md, evidence if appropriate.

- [ ] Real os._exit at response saved / started / result committed / pre-next-LLM; assert stable provider IDs and effect markers.
- [ ] Two-process same-Run lock; wrong-key and identity probes; child/MCP uncertainty and no new prompt bypass.
- [ ] Full pytest/Ruff/compileall/build; current package counts synchronized.
- [ ] Independent final review, fix findings, scoped re-review. Real model test remains explicitly unverified until local credentials are configured; do not fabricate evidence.

## Rulings and prior work
- Existing Windows shell quoting CI failure is fixed separately in current coding-chain branch before this architecture commit.
- If integration reveals a missing cross-task method, controller defines it in this plan before changing consumers; no parallel edits to shared files.
