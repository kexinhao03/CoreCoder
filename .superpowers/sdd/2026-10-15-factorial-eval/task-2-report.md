# Task 2 implementation report

## Scope and result

Implemented runnable `corecoder.evals.live_comparison` supervisor and standalone `live_comparison_worker`, offline tests, conditional live reporting, and live evaluation documentation. No pre-existing dirty Agent, CLI, Runtime, tools, hooks or MCP implementation files were edited or staged.

CLI defaults: `--repetitions 5 --seed 1729 --model qwen3:8b --base-url http://127.0.0.1:11434/v1 --deadline-seconds 300 --provider real`; `--output` is required and must be new. `--provider fixture` is explicitly offline and cannot reach the provider. Console progress is printed after each retained trial; JSON and Markdown are updated incrementally before and after each trial.

The final matrix follows the parent's revised ruling: **35 eligible trials + 5 explicit upstream durable-boundary skips = 40 requested cells**. D_agent normal / after_effect / after_result / after_accepted_result; E normal / after_effect / after_accepted_result. The shared accepted-result case distinguishes `durable_result` from `volatile_result`. D's callback fires only while the production checkpoint is ready, before beginning the next inference; E's observer checks an accepted bash tool message immediately before the next provider call. D-only postcommit results never enter matched common-case completion contrasts. Existing metric definitions remain unchanged; the report adds a separate descriptive common-case match without pooled unequal mixtures.

D_agent explicitly records auto_tool_retry=false and retry_not_exercised=true. E is pristine Git archive `069948d180cb0ceb1a6d7aafeb0e141c166a009d`. Worker source selection and import-cache clearing occur before any CoreCoder import, and the actual Agent module file/blob are verified. Both runtime source trees and the evaluator worker are frozen under output/sources before trials. Actual schemas, system prompts, generation settings, requests/HTTP statuses, parsed responses/tool calls, approvals, tool results, trace and invocation identities are retained. Keys remain mode0600 outside evidence; private key metadata maps a key to its trial, while published evidence contains only actual permission mode.

Production D manages bash subprocesses directly, bypassing BashTool.execute. Consequently the after-effect handshake is emitted by the real external subprocess after fsync; the supervisor kills the actual worker and cleans the registered child. D postcommit uses production session.after_result. No guessed result, forced real model sequence, manufactured E restart, consent deduplication, or human reconciliation is added. Ownership is checked before and after child registration and before effect fsync. Deadlines span initial execution and resume. POSIX-only fault cells are explicitly N/A elsewhere, genuine kill/liveness tests selectively skip elsewhere, and normal/source/schema/report tests remain enabled.

## TDD and verification

RED: initial new suite produced 3 failed + 5 setup failures, all identifying the missing live implementation. Subsequent RED cycles covered missing shared accepted-result cases, interrupted model timing, and missing common-case report matching.

GREEN: final new suite **14 passed**. Tests cover pristine upstream module identity despite preloaded current modules, model/options capture, identical D/E tool schemas, genuine after-effect and postcommit SIGKILL, D unknown-outcome block, D completed-call identity reuse, upstream no restart, boundary miss invalidity, shared durable/volatile result boundaries, completed versus censored provider timing, common-case matching, deadline retention, and late child registration after owner death. Ruff checks on all scoped Python files passed.

An earlier combined old/new suite passed 32/32. A later combined expanded suite produced 35 passed and two existing mechanism delayed-registration tests failed because their 2s deadline elapsed before the child registered during concurrent formal mechanism and Qwen execution. These failures were reported to the parent; no Task1 test budgets or production behavior were changed here. The parent's isolated verification owns resolution of those load-sensitive checks.

Final offline fixture CLI evidence: `.tmp/live-task2-fixture-final/comparison-results.json` and report. Seven assigned rows were retained: D normal, durable postcommit and shared accepted-result complete with one effect; D after-effect safely blocks for reconciliation; E normal completes; E after-effect and accepted-result remain incomplete with one effect and no restart. This is measurement verification, never real-model reliability evidence.

## Actual real-model smoke and limits

Real provider smoke evidence: `.tmp/live-task2-smoke-v1/comparison-results.json`, separate report and per-trial logs. Existing local Qwen3:8b was used with actual loopback OpenAI requests; no download or fixture was used. This development smoke used the old long interpreter command and 150s deadline, before the final short-command prompt and shared-case revision. All five assigned rows were retained:

- D after_result: missed boundary, zero effects; Qwen shortened the exact interpreter path, consent denied it, and the model requested clarification.
- E normal: deadline, one independently observed effect, unfinished final artifact.
- D after_effect: deadline, zero effects; no confirmed fault.
- E after_effect: genuine worker SIGKILL after independent effect fsync, one effect, no restart, incomplete task.
- D normal: deadline, unfinished task.

The final common command is `python3 effect.py` on POSIX (`python effect.py` elsewhere), prompt version `live-effect-v2-short-command`; default deadline is now300s. Old smoke failures are not deleted, silently rerun, pooled with formal evidence, or called successful acceptance. Final short-command D recovery and both shared accepted-result cases are verified offline but **not yet accepted with the real provider**. At the parent's instruction, no redundant second seven-case real smoke was launched. The parent owns full five-repetition real execution after independent review and completion of the frozen mechanism run; its first real cases serve as rollout checks.

Provider requests in the old smoke took approximately35–50s each. Model latency and concurrent host load dominate elapsed measurements; completed llm.chat durations and residual non-model elapsed are recorded, interrupted intervals censored. Residual includes startup/tools/Runtime/fsynced instrumentation and is not pure Runtime overhead. Unknown/missed/deadline outcomes remain in primary assigned denominators. Full repeated real comparison remains required before empirical benefit claims.
