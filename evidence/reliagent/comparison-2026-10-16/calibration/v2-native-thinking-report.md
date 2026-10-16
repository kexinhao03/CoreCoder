# Reliability comparison

Schema: `reliagent.comparison.v1`. 35 assigned trials; 0 pending; 33 invalid/missed/pending; 0 warmups excluded.

D_agent is the current production durable Agent (auto_tool_retry=false; retry is not exercised). E is pristine upstream CoreCoder. This layer compares durable recovery and combined implementation/provider cost. Provider: real; 40 requested; 35 eligible assigned; 5 skipped.

Real provider trials let the LLM select calls. Offline provider=fixture trials are deterministic tests and are not model reliability evidence. SIGKILL follows a durable boundary handshake after a real fsynced effect or production result commit. E has no durable result boundary and is never restarted.

Task completion requires a correct external final artifact and exactly one matching independently fsynced effect. The primary denominator is all assigned repetitions, including invalid/missed faults and pending observations. Hazard denominators include observed side-effect trials; unknown observations are excluded and disclosed. Fault-missed trials can complete the task but provide no evidence of recovery. Only matched eligible observations enter the paired contrasts.

| Measure | Observed rate |
|---|---|
| Task completion | 1/35 (95% [0.005, 0.145]; unknown=0) |
| Automatic completion | 1/35 (95% [0.005, 0.145]; unknown=0) |
| Human-assisted completion | 0/35 (95% [0.000, 0.099]; unknown=0) |
| Duplicate side-effect trials (>1) | 0/3 (95% [0.000, 0.561]; unknown=32) |
| Unsafe-replay trials (total starts=0) | 0/3 (95% [0.000, 0.561]; unknown=32) |
| Additional human reconciliation | 0/35 (95% [0.000, 0.099]; unknown=33) |

Routine consent is logged separately. D_agent blocks unknown effects for explicit human reconciliation; completed calls are reused by their durable invocation identity. No human resolutions are simulated. E receives no evaluator restart or replay.

## Latency (seconds)

- All end-to-end: n=2; median=89.9813; P95=114.3434; min=65.6191; max=114.3434
- Restart launch → automatic completion: n=1; median=32.9336; P95=32.9336; min=32.9336; max=32.9336
- Restart launch → explicit safe block: n=0; unavailable
- Interruption → completed outcome or safe block: n=1; median=32.9470; P95=32.9470; min=32.9470; max=32.9470
- Unfinished/censored recovery samples: 0; never substituted with zero.
- Human waiting: n=0; unavailable; no fabricated waiting duration.

## Strata

| Layer / config / workload / case / boundary / type | Completion | Duplicates | Unsafe replay | Human | Invalid |
|---|---|---|---|---|---|---|
| live_agent / D_agent / opaque / after_accepted_result / after_accepted_tool_result_before_next_inference / runtime_kill | 1/5 (95% [0.036, 0.624]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=4) | 0/1 (95% [0.000, 0.793]; unknown=4) | 0/5 (95% [0.000, 0.434]; unknown=4) | 4 |
| live_agent / D_agent / opaque / after_effect / after_effect_before_result_persist / runtime_kill | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/0 (95% unavailable; unknown=5) | 0/0 (95% unavailable; unknown=5) | 0/5 (95% [0.000, 0.434]; unknown=5) | 5 |
| live_agent / D_agent / opaque / after_result / after_result_before_next_inference / runtime_kill | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/0 (95% unavailable; unknown=5) | 0/0 (95% unavailable; unknown=5) | 0/5 (95% [0.000, 0.434]; unknown=5) | 5 |
| live_agent / D_agent / opaque / normal / none / none | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=4) | 0/1 (95% [0.000, 0.793]; unknown=4) | 0/5 (95% [0.000, 0.434]; unknown=4) | 4 |
| live_agent / E / opaque / after_accepted_result / after_accepted_tool_result_before_next_inference / runtime_kill | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=4) | 0/1 (95% [0.000, 0.793]; unknown=4) | 0/5 (95% [0.000, 0.434]; unknown=5) | 5 |
| live_agent / E / opaque / after_effect / after_effect_before_result_persist / runtime_kill | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/0 (95% unavailable; unknown=5) | 0/0 (95% unavailable; unknown=5) | 0/5 (95% [0.000, 0.434]; unknown=5) | 5 |
| live_agent / E / opaque / normal / none / none | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/0 (95% unavailable; unknown=5) | 0/0 (95% unavailable; unknown=5) | 0/5 (95% [0.000, 0.434]; unknown=5) | 5 |

### Recovery latency by stratum (seconds)

| Stratum | Automatic completion | Safe block | Censored |
|---|---|---|---|
| live_agent / D_agent / opaque / after_accepted_result / after_accepted_tool_result_before_next_inference / runtime_kill | n=1; median=32.9336; P95=32.9336; min=32.9336; max=32.9336 | n=0; unavailable | 0 |

## Matched common live cases

Descriptive D_agent−E completion differences match the same common case and repetition, excluding invalid or missed faults. No pooled comparison of D_agent's four cases against E's three is made. The durable-only postcommit boundary is excluded. Accepted-result crashes observe a durable result in D_agent and a volatile accepted result in E; E is never described as persisted or restarted.

| Common case | Matched n | Completion difference |
|---|---|---|

## Normal-path overhead

Timing includes process startup and fsynced measurement instrumentation. No warmups are run by default. SQL statement counts come from live SQLite connection tracing; write transactions are recorded after commit. These are measured SQL operations, not event counts. Compare A–D flags within mechanism; D_agent–E live differences combine implementation and provider cost and include LLM variability.

| Layer / config | Total seconds | SQL write statements | Committed write transactions |
|---|---|---|---|
| live_agent / D_agent | n=1; median=65.6191; P95=65.6191; min=65.6191; max=65.6191 | n=1; median=34.0000; P95=34.0000; min=34.0000; max=34.0000 | n=1; median=28.0000; P95=28.0000; min=28.0000; max=28.0000 |
| live_agent / E | n=0; unavailable | n=0; unavailable | n=0; unavailable |

Provider timing is measured around each completed llm.chat call. Non-model elapsed includes startup, tools, Runtime and fsynced instrumentation; it is not a pure Runtime microbenchmark. Interrupted model intervals are censored. D_agent−E timing includes provider variability.

| Config | Completed model-call seconds | Non-model elapsed seconds | Censored model timing |
|---|---|---|---|
| D_agent | n=1; median=65.0267; P95=65.0267; min=65.0267; max=65.0267 | n=1; median=0.5924; P95=0.5924; min=0.5924; max=0.5924 | 4 |
| E | n=0; unavailable | n=0; unavailable | 5 |

## Failure / intervention evidence

- `D_agent-normal-4`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-normal-4`
- `E-after_accepted_result-4`: protocol_aborted; effects=0; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_accepted_result-4`
- `E-normal-1`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-normal-1`
- `E-after_effect-4`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_effect-4`
- `D_agent-after_result-2`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-after_result-2`
- `D_agent-normal-1`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-normal-1`
- `E-after_effect-0`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_effect-0`
- `E-after_accepted_result-2`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_accepted_result-2`
- `D_agent-after_effect-3`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-after_effect-3`
- `E-normal-3`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-normal-3`
- `E-after_accepted_result-1`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_accepted_result-1`
- `E-after_effect-1`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_effect-1`
- `D_agent-after_result-3`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-after_result-3`
- `E-after_accepted_result-3`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_accepted_result-3`
- `D_agent-after_accepted_result-1`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-after_accepted_result-1`
- `E-after_effect-3`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_effect-3`
- `D_agent-normal-2`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-normal-2`
- `D_agent-normal-0`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-normal-0`
- `E-normal-2`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-normal-2`
- `E-after_effect-2`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_effect-2`
- `D_agent-after_result-0`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-after_result-0`
- `E-after_accepted_result-0`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/E-after_accepted_result-0`
- `D_agent-after_accepted_result-4`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-after_accepted_result-4`
- `D_agent-after_result-1`: not_run_protocol_aborted; effects=None; error=Calibration stopped: native think:false ignored by v1/chat/completions; identical reasoning_effort:none correction applied to both groups in new protocol; artifacts: `<REPO>/.tmp/factorial-live-5/D_agent-after_result-1`

All 34 such rows remain in raw JSON; examples above are capped at 24.

## Applicability and limits

5 matrix cells are explicitly excluded from eligibility:
- E / opaque / after_result_before_next_inference / runtime_kill: upstream has no durable result persistence boundary
- E / opaque / after_result_before_next_inference / runtime_kill: upstream has no durable result persistence boundary
- E / opaque / after_result_before_next_inference / runtime_kill: upstream has no durable result persistence boundary
- E / opaque / after_result_before_next_inference / runtime_kill: upstream has no durable result persistence boundary
- E / opaque / after_result_before_next_inference / runtime_kill: upstream has no durable result persistence boundary

No-recovery and safe-block outcomes are not automatic task completion. Zero observed duplicates does not prove exactly-once behavior. Unsafe replay uses the independent dispatch ledger and prior unresolved durable state, independently of the effect counter. Fixed-task continuation and receipt reconciliation do not establish real-model reliability. Failed/missed/deadline trials and external evidence are retained.

## Existing regression evidence

The historical 90/90 and 24/24 scenario assertion contracts remain separate regression checks. They are not denominators or comparative reliability rates in this report.

## Source fingerprint

```json
{
  "D_agent": {
    "agent_sha256": "4799bf332306ada6b836b667154064334e6b32490e523e58fc46bc6e87c4a3ab",
    "fingerprint": {
      "executable": "<REPO>/.venv/bin/python",
      "head": "ac80c79bda6a5aec159d234dda5fa08bc762587f",
      "platform": "macOS-26.1-arm64-arm-64bit",
      "python": "3.12.14 (main, Aug 25 2026, 13:50:33) [Clang 22.1.3 ]",
      "python_source_sha256": "863ad66dbdf64bc326f17cc327e7732475707aa156c388de0e168ee9d0b4c65f",
      "tracked_diff_sha256": "068e398cdcabfc823607ab1578c9fe3d275bed5e9cdbf30eecff96ba9f1ab511"
    },
    "path": "<REPO>/.tmp/factorial-live-5/sources/durable"
  },
  "E": {
    "agent_sha256": "74a701db87920011d6d7afb85de60fa489a74532aaddbc237adffaf22a52d99e",
    "head": "069948d180cb0ceb1a6d7aafeb0e141c166a009d",
    "path": "<REPO>/.tmp/factorial-live-5/sources/upstream"
  }
}
```
