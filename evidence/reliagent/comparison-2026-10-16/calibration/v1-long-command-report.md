# Reliability comparison

Schema: `reliagent.comparison.v1`. SMOKE RUN — 5 assigned trials; 0 pending; 2 invalid/missed/pending; 0 warmups excluded.

D_agent is the current production durable Agent (auto_tool_retry=false; retry is not exercised). E is pristine upstream CoreCoder. This layer compares durable recovery and combined implementation/provider cost. Provider: real; 6 requested; 5 eligible assigned; 1 skipped.

Real provider trials let the LLM select calls. Offline provider=fixture trials are deterministic tests and are not model reliability evidence. SIGKILL follows a durable boundary handshake after a real fsynced effect or production result commit. E has no durable result boundary and is never restarted.

Task completion requires a correct external final artifact and exactly one matching independently fsynced effect. The primary denominator is all assigned repetitions, including invalid/missed faults and pending observations. Hazard denominators include observed side-effect trials; unknown observations are excluded and disclosed. Fault-missed trials can complete the task but provide no evidence of recovery. Only matched eligible observations enter the paired contrasts.

| Measure | Observed rate |
|---|---|
| Task completion | 0/5 (95% [0.000, 0.434]; unknown=0) |
| Automatic completion | 0/5 (95% [0.000, 0.434]; unknown=0) |
| Human-assisted completion | 0/5 (95% [0.000, 0.434]; unknown=0) |
| Duplicate side-effect trials (>1) | 0/5 (95% [0.000, 0.434]; unknown=0) |
| Unsafe-replay trials (total starts=0) | 0/5 (95% [0.000, 0.434]; unknown=0) |
| Additional human reconciliation | 0/5 (95% [0.000, 0.434]; unknown=0) |

Routine consent is logged separately. D_agent blocks unknown effects for explicit human reconciliation; completed calls are reused by their durable invocation identity. No human resolutions are simulated. E receives no evaluator restart or replay.

## Latency (seconds)

- All end-to-end: n=5; median=150.0139; P95=150.0205; min=114.5857; max=150.0205
- Restart launch → automatic completion: n=0; unavailable
- Restart launch → explicit safe block: n=0; unavailable
- Interruption → completed outcome or safe block: n=0; unavailable
- Unfinished/censored recovery samples: 0; never substituted with zero.
- Human waiting: n=0; unavailable; no fabricated waiting duration.

## Strata

| Layer / config / workload / case / boundary / type | Completion | Duplicates | Unsafe replay | Human | Invalid |
|---|---|---|---|---|---|---|
| live_agent / D_agent / opaque / after_effect / after_effect_before_result_persist / runtime_kill | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 1 |
| live_agent / D_agent / opaque / after_result / after_result_before_next_inference / runtime_kill | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 1 |
| live_agent / D_agent / opaque / normal / none / none | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0 |
| live_agent / E / opaque / after_effect / after_effect_before_result_persist / runtime_kill | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0 |
| live_agent / E / opaque / normal / none / none | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0/1 (95% [0.000, 0.793]; unknown=0) | 0 |

### Recovery latency by stratum (seconds)

| Stratum | Automatic completion | Safe block | Censored |
|---|---|---|---|

## Matched common live cases

Descriptive D_agent−E completion differences match the same common case and repetition, excluding invalid or missed faults. No pooled comparison of D_agent's four cases against E's three is made. The durable-only postcommit boundary is excluded. Accepted-result crashes observe a durable result in D_agent and a volatile accepted result in E; E is never described as persisted or restarted.

| Common case | Matched n | Completion difference |
|---|---|---|
| normal | 1 | 0 |

## Normal-path overhead

Timing includes process startup and fsynced measurement instrumentation. No warmups are run by default. SQL statement counts come from live SQLite connection tracing; write transactions are recorded after commit. These are measured SQL operations, not event counts. Compare A–D flags within mechanism; D_agent–E live differences combine implementation and provider cost and include LLM variability.

| Layer / config | Total seconds | SQL write statements | Committed write transactions |
|---|---|---|---|
| live_agent / D_agent | n=1; median=150.0205; P95=150.0205; min=150.0205; max=150.0205 | n=1; median=81.0000; P95=81.0000; min=81.0000; max=81.0000 | n=1; median=43.0000; P95=43.0000; min=43.0000; max=43.0000 |
| live_agent / E | n=1; median=150.0145; P95=150.0145; min=150.0145; max=150.0145 | n=1; median=0.0000; P95=0.0000; min=0.0000; max=0.0000 | n=1; median=0.0000; P95=0.0000; min=0.0000; max=0.0000 |

Provider timing is measured around each completed llm.chat call. Non-model elapsed includes startup, tools, Runtime and fsynced instrumentation; it is not a pure Runtime microbenchmark. Interrupted model intervals are censored. D_agent−E timing includes provider variability.

| Config | Completed model-call seconds | Non-model elapsed seconds | Censored model timing |
|---|---|---|---|
| D_agent | n=1; median=122.7767; P95=122.7767; min=122.7767; max=122.7767 | n=0; unavailable | 1 |
| E | n=1; median=123.4297; P95=123.4297; min=123.4297; max=123.4297 | n=0; unavailable | 1 |

## Failure / intervention evidence

- `D_agent-after_result-0`: missed_fault; effects=0; error=None; artifacts: `<REPO>/.tmp/live-task2-smoke-v1/D_agent-after_result-0`
- `E-normal-0`: deadline; effects=1; error=trial deadline exceeded; artifacts: `<REPO>/.tmp/live-task2-smoke-v1/E-normal-0`
- `D_agent-after_effect-0`: deadline; effects=0; error=trial deadline exceeded; artifacts: `<REPO>/.tmp/live-task2-smoke-v1/D_agent-after_effect-0`
- `E-after_effect-0`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/live-task2-smoke-v1/E-after_effect-0`
- `D_agent-normal-0`: deadline; effects=1; error=trial deadline exceeded; artifacts: `<REPO>/.tmp/live-task2-smoke-v1/D_agent-normal-0`

All 5 such rows remain in raw JSON; examples above are capped at 24.

## Applicability and limits

1 matrix cells are explicitly excluded from eligibility:
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
      "head": "32e48c773a658175e816e34762ee2b520a5ea32f",
      "platform": "macOS-26.1-arm64-arm-64bit",
      "python": "3.12.14 (main, Aug 25 2026, 13:50:33) [Clang 22.1.3 ]",
      "python_source_sha256": "cf958ac53e79d7419fdd52afa4323e5b2faf80b9d8f9916ece5703d848630c83",
      "tracked_diff_sha256": "66ccfdf59698a96028630a592e0789d8d032e41a7aa59cddca949b0c852c1102"
    },
    "path": "<REPO>/.tmp/live-task2-smoke-v1/sources/durable"
  },
  "E": {
    "agent_sha256": "74a701db87920011d6d7afb85de60fa489a74532aaddbc237adffaf22a52d99e",
    "head": "069948d180cb0ceb1a6d7aafeb0e141c166a009d",
    "path": "<REPO>/.tmp/live-task2-smoke-v1/sources/upstream"
  }
}
```
