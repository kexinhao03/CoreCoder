# Reliability comparison

Schema: `reliagent.comparison.v1`. 35 assigned trials; 0 pending; 0 invalid/missed/pending; 0 warmups excluded.

D_agent is the current production durable Agent (auto_tool_retry=false; retry is not exercised). E is pristine upstream CoreCoder. This layer compares durable recovery and combined implementation/provider cost. Provider: real; 40 requested; 35 eligible assigned; 5 skipped.

Real provider trials let the LLM select calls. Offline provider=fixture trials are deterministic tests and are not model reliability evidence. SIGKILL follows a durable boundary handshake after a real fsynced effect or production result commit. E has no durable result boundary and is never restarted.

Task completion requires a correct external final artifact and exactly one matching independently fsynced effect. The primary denominator is all assigned repetitions, including invalid/missed faults and pending observations. Hazard denominators include observed side-effect trials; unknown observations are excluded and disclosed. Fault-missed trials can complete the task but provide no evidence of recovery. Only matched eligible observations enter the paired contrasts.

| Measure | Observed rate |
|---|---|
| Task completion | 20/35 (95% [0.409, 0.720]; unknown=0) |
| Automatic completion | 20/35 (95% [0.409, 0.720]; unknown=0) |
| Human-assisted completion | 0/35 (95% [0.000, 0.099]; unknown=0) |
| Duplicate side-effect trials (>1) | 0/35 (95% [0.000, 0.099]; unknown=0) |
| Unsafe-replay trials (total starts=0) | 0/35 (95% [0.000, 0.099]; unknown=0) |
| Additional human reconciliation | 5/35 (95% [0.063, 0.294]; unknown=0) |

Routine consent is logged separately. D_agent blocks unknown effects for explicit human reconciliation; completed calls are reused by their durable invocation identity. No human resolutions are simulated. E receives no evaluator restart or replay.

## Latency (seconds)

- All end-to-end: n=35; median=14.3715; P95=20.8820; min=8.2914; max=21.3906
- Restart launch → automatic completion: n=10; median=7.6064; P95=8.2864; min=5.3568; max=8.2864
- Restart launch → explicit safe block: n=5; median=0.5250; P95=1.0379; min=0.4501; max=1.0379
- Interruption → completed outcome or safe block: n=15; median=7.4355; P95=8.2995; min=0.4629; max=8.2995
- Unfinished/censored recovery samples: 0; never substituted with zero.
- Human waiting: n=0; unavailable; no fabricated waiting duration.

## Strata

| Layer / config / workload / case / boundary / type | Completion | Duplicates | Unsafe replay | Human | Invalid |
|---|---|---|---|---|---|---|
| live_agent / D_agent / opaque / after_accepted_result / after_accepted_tool_result_before_next_inference / runtime_kill | 5/5 (95% [0.566, 1.000]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0 |
| live_agent / D_agent / opaque / after_effect / after_effect_before_result_persist / runtime_kill | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 5/5 (95% [0.566, 1.000]; unknown=0) | 0 |
| live_agent / D_agent / opaque / after_result / after_result_before_next_inference / runtime_kill | 5/5 (95% [0.566, 1.000]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0 |
| live_agent / D_agent / opaque / normal / none / none | 5/5 (95% [0.566, 1.000]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0 |
| live_agent / E / opaque / after_accepted_result / after_accepted_tool_result_before_next_inference / runtime_kill | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0 |
| live_agent / E / opaque / after_effect / after_effect_before_result_persist / runtime_kill | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0 |
| live_agent / E / opaque / normal / none / none | 5/5 (95% [0.566, 1.000]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0/5 (95% [0.000, 0.434]; unknown=0) | 0 |

### Recovery latency by stratum (seconds)

| Stratum | Automatic completion | Safe block | Censored |
|---|---|---|---|
| live_agent / D_agent / opaque / after_accepted_result / after_accepted_tool_result_before_next_inference / runtime_kill | n=5; median=7.6448; P95=8.2864; min=5.3568; max=8.2864 | n=0; unavailable | 0 |
| live_agent / D_agent / opaque / after_effect / after_effect_before_result_persist / runtime_kill | n=0; unavailable | n=5; median=0.5250; P95=1.0379; min=0.4501; max=1.0379 | 0 |
| live_agent / D_agent / opaque / after_result / after_result_before_next_inference / runtime_kill | n=5; median=7.5681; P95=8.2243; min=5.5398; max=8.2243 | n=0; unavailable | 0 |

## Matched common live cases

Descriptive D_agent−E completion differences match the same common case and repetition, excluding invalid or missed faults. No pooled comparison of D_agent's four cases against E's three is made. The durable-only postcommit boundary is excluded. Accepted-result crashes observe a durable result in D_agent and a volatile accepted result in E; E is never described as persisted or restarted.

| Common case | Matched n | Completion difference |
|---|---|---|
| after_accepted_result | 5 | 1 |
| after_effect | 5 | 0 |
| normal | 5 | 0 |

## Normal-path overhead

Timing includes process startup and fsynced measurement instrumentation. No warmups are run by default. SQL statement counts come from live SQLite connection tracing; write transactions are recorded after commit. These are measured SQL operations, not event counts. Compare A–D flags within mechanism; D_agent–E live differences combine implementation and provider cost and include LLM variability.

| Layer / config | Total seconds | SQL write statements | Committed write transactions |
|---|---|---|---|
| live_agent / D_agent | n=5; median=18.8558; P95=19.9858; min=13.2834; max=19.9858 | n=5; median=115.0000; P95=115.0000; min=115.0000; max=115.0000 | n=5; median=55.0000; P95=55.0000; min=55.0000; max=55.0000 |
| live_agent / E | n=5; median=18.1991; P95=19.6493; min=13.0393; max=19.6493 | n=5; median=0.0000; P95=0.0000; min=0.0000; max=0.0000 | n=5; median=0.0000; P95=0.0000; min=0.0000; max=0.0000 |

Provider timing is measured around each completed llm.chat call. Non-model elapsed includes startup, tools, Runtime and fsynced instrumentation; it is not a pure Runtime microbenchmark. Interrupted model intervals are censored. D_agent−E timing includes provider variability.

| Config | Completed model-call seconds | Non-model elapsed seconds | Censored model timing |
|---|---|---|---|
| D_agent | n=5; median=18.0701; P95=19.0255; min=12.4403; max=19.0255 | n=5; median=0.8431; P95=0.9602; min=0.7857; max=0.9602 | 0 |
| E | n=5; median=17.5929; P95=19.0530; min=12.5117; max=19.0530 | n=5; median=0.5859; P95=0.6062; min=0.4956; max=0.6062 | 0 |

## Failure / intervention evidence

- `E-after_accepted_result-4`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_accepted_result-4`
- `E-after_effect-4`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_effect-4`
- `E-after_effect-0`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_effect-0`
- `E-after_accepted_result-2`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_accepted_result-2`
- `D_agent-after_effect-3`: human_required; effects=1; error=ValueError: unknown tool outcome blocks execution; artifacts: `<REPO>/.tmp/factorial-live-5-v3/D_agent-after_effect-3`
- `E-after_accepted_result-1`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_accepted_result-1`
- `E-after_effect-1`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_effect-1`
- `E-after_accepted_result-3`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_accepted_result-3`
- `E-after_effect-3`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_effect-3`
- `E-after_effect-2`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_effect-2`
- `E-after_accepted_result-0`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-live-5-v3/E-after_accepted_result-0`
- `D_agent-after_effect-2`: human_required; effects=1; error=ValueError: unknown tool outcome blocks execution; artifacts: `<REPO>/.tmp/factorial-live-5-v3/D_agent-after_effect-2`
- `D_agent-after_effect-1`: human_required; effects=1; error=ValueError: unknown tool outcome blocks execution; artifacts: `<REPO>/.tmp/factorial-live-5-v3/D_agent-after_effect-1`
- `D_agent-after_effect-4`: human_required; effects=1; error=ValueError: unknown tool outcome blocks execution; artifacts: `<REPO>/.tmp/factorial-live-5-v3/D_agent-after_effect-4`
- `D_agent-after_effect-0`: human_required; effects=1; error=ValueError: unknown tool outcome blocks execution; artifacts: `<REPO>/.tmp/factorial-live-5-v3/D_agent-after_effect-0`

All 15 such rows remain in raw JSON; examples above are capped at 24.

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
      "head": "af866327c205e43b98438e8e8db1479596dda8c3",
      "platform": "macOS-26.1-arm64-arm-64bit",
      "python": "3.12.14 (main, Aug 25 2026, 13:50:33) [Clang 22.1.3 ]",
      "python_source_sha256": "724603b70eb16ab5d56dfa7e98417be0dda28bf362ee919f99698f8c783c032e",
      "tracked_diff_sha256": "068e398cdcabfc823607ab1578c9fe3d275bed5e9cdbf30eecff96ba9f1ab511"
    },
    "path": "<REPO>/.tmp/factorial-live-5-v3/sources/durable"
  },
  "E": {
    "agent_sha256": "74a701db87920011d6d7afb85de60fa489a74532aaddbc237adffaf22a52d99e",
    "head": "069948d180cb0ceb1a6d7aafeb0e141c166a009d",
    "path": "<REPO>/.tmp/factorial-live-5-v3/sources/upstream"
  }
}
```
