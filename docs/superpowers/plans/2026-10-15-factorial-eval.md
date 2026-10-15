# Factorial Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Reproducible reliability comparison, independent effect evidence, honest metrics and retained failures.
**Architecture:** Add a comparative evaluation surface alongside the frozen regression suites. A supervisor launches production Runtime workers, injects faults at acknowledged boundaries and observes external artifacts. A separate live-Agent worker compares D with pristine upstream E.
**Tech Stack:** Python >=3.10, stdlib subprocess/SQLite/statistics, existing Runtime and Ollama OpenAI-compatible LLM.
**Spec:** docs/superpowers/specs/2026-10-15-factorial-eval-design.md

## Global Constraints
- A/B/C/D retry,recovery matrix is (False,False)/(True,False)/(False,True)/(True,True).
- No new dependencies or model downloads. Preserve existing Eval raw v1 artifacts and scenario contracts.
- No credentials/keys in reports. Keep every failed trial and fault miss. Unknown outcomes never count as automatic completion.
- Existing dirty Agent integration files belong to prior work: do not edit or stage them.
- E fixed upstream commit 069948d180cb0ceb1a6d7aafeb0e141c166a009d; never equate A or --no-runtime to upstream.

### Task 1: Mechanism experiment, contracts and report
**Files:** Create corecoder/evals/comparison.py, comparison_worker.py, comparison_report.py, tests/evals/test_comparison.py (split focused modules if needed); modify suite config only if existing contract consumers remain valid. Add docs/evaluation-comparison.md.
**Interfaces:** `python -m corecoder.evals.comparison --output PATH --repetitions 10 --seed 1729` executes mechanism and writes comparison-results.json, comparison-report.md, per-trial artifacts. Raw schema `reliagent.comparison.v1`; each row: trial_id, layer, config_id, case_id, repetition, fault_point, fault_type, fault_triggered, outcome, task_completed, human_intervention, effect_count nullable, unsafe_replay_count nullable, elapsed_seconds, recovery_seconds nullable, sql_write_statements, sql_write_transactions, error, artifact_dir. Additional explicit availability/censoring fields allowed. Config metadata, source/environment and skipped matrix included.
- [ ] Write failing tests: exact matrix; independent effect vs runtime success; missing observation remains unknown; failed/missed rows retained; denominator and interval arithmetic; production worker kill+restart at after_effect with marker=1 and safe block; after_result cached no replay; transient safe prep retry benefit.
- [ ] Run `.venv/bin/python -m pytest tests/evals/test_comparison.py -q`, confirm expected RED.
- [ ] Implement supervisor+worker using actual RuntimeExecutor/RecoveryManager/ReliAgentRuntime paths, not hand-coded pretend recovery. Fault handshake lives in evaluation adapters, with precise acknowledged durable boundaries. Preserve initial task manifest, observer log, DB, output/error on every outcome. Count SQL writes with actual connection instrumentation; handle multiple worker processes and phases.
- [ ] Implement report pure from raw JSON: five measures and overhead, rates n/N with unknown/invalid counts and intervals, latency split and censoring; strata; paired factorial differences; examples of failure/human-needed rows.
- [ ] Run targeted new and existing Eval/Runtime tests plus Ruff. Run 1-repetition smoke, inspect raw artifacts. Self-review, scoped commit; report RED/GREEN commands, files, limits and source commit to designated report.

### Task 2: Real Qwen Agent / frozen upstream E comparison
**Files:** Create corecoder/evals/live_comparison.py, live_comparison_worker.py, tests/evals/test_live_comparison.py; use Task1 raw/report without rewriting its metric definitions. Extend docs/evaluation-comparison.md.
**Interfaces:** `python -m corecoder.evals.live_comparison --output PATH --repetitions 5 --model qwen3:8b --base-url http://127.0.0.1:11434/v1` outputs same schema with layer=live_agent and separate real-model report. Each trial owns source/import isolation and independent effect files. Consumers comparison_report must support D/E without factorial contrasts for E.
- [ ] Write failing tests for pristine upstream import identity, model/endpoint/config capture without credentials, faults only after actual effect/result, missed boundary not success, E no invented restart/replay, D unknown blocks and D completed-call cache resumes identity. Offline tests exercise worker with supplied provider fixture explicitly labeled fixture; opt-in real runs separate.
- [ ] Confirm RED then implement using actual Agent/LLM APIs. Extract upstream Git tree to output source directory, no upstream code edits. Equivalent read/write/bash tools and identical task/model generation options; approvals logged. Isolated configs avoid user hooks/MCP; local key stored mode0600 outside evidence subtree, no key output. Parent control/capture must not fake model sequence. Five live repetitions each D/E normal/after_effect/after_result (30 trials). Runtime boundary adapters confined to eval harness; don't alter production to weaken gating. All waits bounded and trial-owned cleanup.
- [ ] Run offline tests/Ruff, one live smoke per critical path and then full live run with existing local model; preserve failures. Document limitation if source upstream lacks comparable persisted result boundary—explicitly mark E non-applicable instead of synthesizing persistence.
- [ ] Commit scoped files, report tests/source/fault/availability and live evidence path.

### Task 3: Full experiments, interpretation and delivery
**Files:** evidence/reliagent/comparison-2026-10-15/ (portable sanitized raw/report/manifest); docs/evaluation-comparison.md; README.md and README_CN.md only evaluation claims; planning notes.
- [ ] Review Task1/2 independently and resolve findings before full runs.
- [ ] Execute mechanism10 and live5 interleaved repetitions; persist raw evidence outside tracked public data until audited for keys/path privacy.
- [ ] Rebuild report only from raw file; independently check sums, external markers, invalid/unknown/censored counts, source/model versions and fault actual termination proof.
- [ ] Write conclusions bounded by observed comparisons, retain adverse and human-required cases. Explain E vs D tooling/provider differences and what A–D can identify. Do not replace historical evidence or present contract pass rate as task completion.
- [ ] Full regression, scoped Ruff, compileall; record pre-existing failures separately. Broad review of Eval change. Scoped commit and report completed work plus remaining limitations; no merge or release.
