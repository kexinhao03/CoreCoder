# Reliability comparison

Schema: `reliagent.comparison.v1`. 1040 assigned trials; 0 pending; 0 invalid/missed/pending; 0 warmups excluded.

A=(retry off, recovery off), B=(on, off), C=(off, on), D=(on, on). A is the same production Runtime with flags disabled. D_agent and E, if present, are separate live-Agent comparators and is excluded from factorial estimates.

Mechanism requests are deterministic. The final boundary is before **fixed-task continuation**, with no inference in this layer. SIGKILL, child termination, and raised exceptions are distinct treatments.

Task completion requires a correct external final artifact and exactly one matching independently fsynced effect. The primary denominator is all assigned repetitions, including invalid/missed faults and pending observations. Hazard denominators include observed side-effect trials; unknown observations are excluded and disclosed. Fault-missed trials can complete the task but provide no evidence of recovery. Only matched eligible observations enter the paired contrasts.

| Measure | Observed rate |
|---|---|
| Task completion | 400/1040 (95% [0.356, 0.415]; unknown=0) |
| Automatic completion | 400/1040 (95% [0.356, 0.415]; unknown=0) |
| Human-assisted completion | 0/1040 (95% [0.000, 0.004]; unknown=0) |
| Duplicate side-effect trials (>1) | 0/1040 (95% [0.000, 0.004]; unknown=0) |
| Unsafe-replay trials (total starts=0) | 0/1040 (95% [0.000, 0.004]; unknown=0) |
| Additional human reconciliation | 80/1040 (95% [0.062, 0.095]; unknown=0) |

Routine consent is logged separately. Disabled recovery leaves incomplete work without automatically requesting human reconciliation. Queryable receipts can be reconciled through the production recovery API; opaque uncertain effects stop for human reconciliation. No human resolutions are simulated.

## Latency (seconds)

- All end-to-end: n=1040; median=1.6091; P95=4.0371; min=0.7036; max=6.4381
- Restart launch → automatic completion: n=280; median=1.1755; P95=2.5092; min=0.7156; max=3.6824
- Restart launch → explicit safe block: n=80; median=0.3940; P95=0.8059; min=0.3496; max=0.8877
- Interruption → completed outcome or safe block: n=360; median=1.1431; P95=2.5486; min=0.3576; max=3.6907
- Unfinished/censored recovery samples: 80; never substituted with zero.
- Human waiting: n=0; unavailable; no fabricated waiting duration.

## Strata

| Layer / config / workload / case / boundary / type | Completion | Duplicates | Unsafe replay | Human | Invalid |
|---|---|---|---|---|---|---|
| mechanism / A / opaque / fault / after_call_persist_before_start / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / after_call_persist_before_start / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / after_effect_before_result_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / after_effect_before_result_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / after_result_before_next_inference / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / after_result_before_next_inference / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / before_call_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / before_call_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / opaque / retry_only / none / none | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / after_call_persist_before_start / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / after_call_persist_before_start / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / after_effect_before_result_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / after_effect_before_result_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / after_result_before_next_inference / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / after_result_before_next_inference / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / before_call_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / before_call_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / A / receipt / retry_only / none / none | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / after_call_persist_before_start / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / after_call_persist_before_start / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / after_effect_before_result_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / after_effect_before_result_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / after_result_before_next_inference / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / after_result_before_next_inference / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / before_call_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / before_call_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / opaque / retry_only / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / after_call_persist_before_start / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / after_call_persist_before_start / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / after_effect_before_result_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / after_effect_before_result_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / after_result_before_next_inference / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / after_result_before_next_inference / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / before_call_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / before_call_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / B / receipt / retry_only / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / after_call_persist_before_start / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / after_call_persist_before_start / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / after_effect_before_result_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / C / opaque / fault / after_effect_before_result_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / C / opaque / fault / after_result_before_next_inference / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / after_result_before_next_inference / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / before_call_persist / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / before_call_persist / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / C / opaque / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / opaque / retry_only / none / none | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / after_call_persist_before_start / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / after_call_persist_before_start / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / after_effect_before_result_persist / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / after_effect_before_result_persist / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / after_result_before_next_inference / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / after_result_before_next_inference / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / before_call_persist / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / before_call_persist / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / C / receipt / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / C / receipt / retry_only / none / none | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / after_call_persist_before_start / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / after_call_persist_before_start / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / after_effect_before_result_persist / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / D / opaque / fault / after_effect_before_result_persist / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / D / opaque / fault / after_result_before_next_inference / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / after_result_before_next_inference / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / before_call_persist / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / before_call_persist / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / D / opaque / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / opaque / retry_only / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / after_call_persist_before_start / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / after_call_persist_before_start / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / after_effect_before_result_persist / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / after_effect_before_result_persist / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / after_result_before_next_inference / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / after_result_before_next_inference / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / before_call_persist / exception | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / before_call_persist / runtime_kill | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / during_tool_execution / child_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / during_tool_execution / exception | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / fault / during_tool_execution / runtime_kill | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 10/10 (95% [0.722, 1.000]; unknown=0) | 0 |
| mechanism / D / receipt / normal / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |
| mechanism / D / receipt / retry_only / none / none | 10/10 (95% [0.722, 1.000]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0/10 (95% [0.000, 0.278]; unknown=0) | 0 |

### Recovery latency by stratum (seconds)

| Stratum | Automatic completion | Safe block | Censored |
|---|---|---|---|
| mechanism / C / opaque / fault / after_call_persist_before_start / exception | n=10; median=1.1979; P95=2.3308; min=1.1192; max=2.3308 | n=0; unavailable | 0 |
| mechanism / C / opaque / fault / after_call_persist_before_start / runtime_kill | n=10; median=1.1689; P95=2.8326; min=1.0724; max=2.8326 | n=0; unavailable | 0 |
| mechanism / C / opaque / fault / after_effect_before_result_persist / exception | n=0; unavailable | n=10; median=0.3856; P95=0.8059; min=0.3593; max=0.8059 | 0 |
| mechanism / C / opaque / fault / after_effect_before_result_persist / runtime_kill | n=0; unavailable | n=10; median=0.3872; P95=0.8125; min=0.3496; max=0.8125 | 0 |
| mechanism / C / opaque / fault / after_result_before_next_inference / exception | n=10; median=1.1258; P95=2.3837; min=0.7175; max=2.3837 | n=0; unavailable | 0 |
| mechanism / C / opaque / fault / after_result_before_next_inference / runtime_kill | n=10; median=0.8208; P95=1.6951; min=0.7529; max=1.6951 | n=0; unavailable | 0 |
| mechanism / C / opaque / fault / before_call_persist / exception | n=10; median=1.1965; P95=2.2055; min=1.0924; max=2.2055 | n=0; unavailable | 0 |
| mechanism / C / opaque / fault / before_call_persist / runtime_kill | n=10; median=1.1833; P95=2.5100; min=1.1031; max=2.5100 | n=0; unavailable | 0 |
| mechanism / C / opaque / fault / during_tool_execution / child_kill | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / C / opaque / fault / during_tool_execution / exception | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / C / opaque / fault / during_tool_execution / runtime_kill | n=0; unavailable | n=10; median=0.4075; P95=0.7472; min=0.3759; max=0.7472 | 0 |
| mechanism / C / receipt / fault / after_call_persist_before_start / exception | n=10; median=1.2251; P95=1.7417; min=1.0813; max=1.7417 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / after_call_persist_before_start / runtime_kill | n=10; median=1.2337; P95=3.0089; min=1.1000; max=3.0089 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / after_effect_before_result_persist / exception | n=10; median=0.8006; P95=2.6137; min=0.7349; max=2.6137 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / after_effect_before_result_persist / runtime_kill | n=10; median=0.7716; P95=2.2588; min=0.7360; max=2.2588 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / after_result_before_next_inference / exception | n=10; median=0.8073; P95=1.6709; min=0.7306; max=1.6709 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / after_result_before_next_inference / runtime_kill | n=10; median=0.8825; P95=2.4181; min=0.7776; max=2.4181 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / before_call_persist / exception | n=10; median=1.2591; P95=2.4055; min=1.0922; max=2.4055 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / before_call_persist / runtime_kill | n=10; median=1.2109; P95=3.3965; min=1.1061; max=3.3965 | n=0; unavailable | 0 |
| mechanism / C / receipt / fault / during_tool_execution / child_kill | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / C / receipt / fault / during_tool_execution / exception | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / C / receipt / fault / during_tool_execution / runtime_kill | n=0; unavailable | n=10; median=0.3907; P95=0.7622; min=0.3554; max=0.7622 | 0 |
| mechanism / D / opaque / fault / after_call_persist_before_start / exception | n=10; median=1.2035; P95=2.3626; min=1.1089; max=2.3626 | n=0; unavailable | 0 |
| mechanism / D / opaque / fault / after_call_persist_before_start / runtime_kill | n=10; median=1.1554; P95=2.3648; min=1.1151; max=2.3648 | n=0; unavailable | 0 |
| mechanism / D / opaque / fault / after_effect_before_result_persist / exception | n=0; unavailable | n=10; median=0.4002; P95=0.7589; min=0.3499; max=0.7589 | 0 |
| mechanism / D / opaque / fault / after_effect_before_result_persist / runtime_kill | n=0; unavailable | n=10; median=0.3979; P95=0.8877; min=0.3609; max=0.8877 | 0 |
| mechanism / D / opaque / fault / after_result_before_next_inference / exception | n=10; median=1.1596; P95=2.4161; min=0.7156; max=2.4161 | n=0; unavailable | 0 |
| mechanism / D / opaque / fault / after_result_before_next_inference / runtime_kill | n=10; median=0.7953; P95=1.8603; min=0.7164; max=1.8603 | n=0; unavailable | 0 |
| mechanism / D / opaque / fault / before_call_persist / exception | n=10; median=1.1675; P95=2.4491; min=1.0932; max=2.4491 | n=0; unavailable | 0 |
| mechanism / D / opaque / fault / before_call_persist / runtime_kill | n=10; median=1.1933; P95=2.6525; min=1.1123; max=2.6525 | n=0; unavailable | 0 |
| mechanism / D / opaque / fault / during_tool_execution / child_kill | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / D / opaque / fault / during_tool_execution / exception | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / D / opaque / fault / during_tool_execution / runtime_kill | n=0; unavailable | n=10; median=0.3903; P95=0.8071; min=0.3565; max=0.8071 | 0 |
| mechanism / D / receipt / fault / after_call_persist_before_start / exception | n=10; median=1.2751; P95=1.8851; min=1.0934; max=1.8851 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / after_call_persist_before_start / runtime_kill | n=10; median=1.1929; P95=3.0843; min=1.1031; max=3.0843 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / after_effect_before_result_persist / exception | n=10; median=0.7887; P95=2.4584; min=0.7330; max=2.4584 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / after_effect_before_result_persist / runtime_kill | n=10; median=0.8097; P95=1.6575; min=0.7224; max=1.6575 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / after_result_before_next_inference / exception | n=10; median=0.7804; P95=1.7010; min=0.7281; max=1.7010 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / after_result_before_next_inference / runtime_kill | n=10; median=0.8422; P95=2.5481; min=0.7318; max=2.5481 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / before_call_persist / exception | n=10; median=1.2128; P95=2.9974; min=1.0965; max=2.9974 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / before_call_persist / runtime_kill | n=10; median=1.2255; P95=3.6824; min=1.1255; max=3.6824 | n=0; unavailable | 0 |
| mechanism / D / receipt / fault / during_tool_execution / child_kill | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / D / receipt / fault / during_tool_execution / exception | n=0; unavailable | n=0; unavailable | 10 |
| mechanism / D / receipt / fault / during_tool_execution / runtime_kill | n=0; unavailable | n=10; median=0.3893; P95=0.6165; min=0.3649; max=0.6165 | 0 |

## Paired factorial completion differences

Descriptive risk differences, no significance claims. Retry: B−A and D−C; recovery: C−A and D−B; interaction: D−B−C+A. E is never included. Small samples do not establish a general reliability rate.

| Contrast | Matched n | Mean difference |
|---|---|---|
| B-A | 260 | 0.07692307692307693 |
| D-C | 260 | 0.07692307692307693 |
| C-A | 260 | 0.5384615384615384 |
| D-B | 260 | 0.5384615384615384 |
| D-B-C+A | 260 | 0 |

| Stratum | B−A | D−C | C−A | D−B | Interaction |
|---|---|---|---|---|---|
| mechanism / opaque / fault / after_call_persist_before_start / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / opaque / fault / after_call_persist_before_start / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / opaque / fault / after_effect_before_result_persist / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / opaque / fault / after_effect_before_result_persist / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / opaque / fault / after_result_before_next_inference / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / opaque / fault / after_result_before_next_inference / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / opaque / fault / before_call_persist / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / opaque / fault / before_call_persist / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / opaque / fault / during_tool_execution / child_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / opaque / fault / during_tool_execution / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / opaque / fault / during_tool_execution / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / opaque / normal / none / none | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / opaque / retry_only / none / none | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / receipt / fault / after_call_persist_before_start / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / after_call_persist_before_start / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / after_effect_before_result_persist / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / after_effect_before_result_persist / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / after_result_before_next_inference / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / after_result_before_next_inference / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / before_call_persist / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / before_call_persist / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 |
| mechanism / receipt / fault / during_tool_execution / child_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / receipt / fault / during_tool_execution / exception | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / receipt / fault / during_tool_execution / runtime_kill | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / receipt / normal / none / none | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |
| mechanism / receipt / retry_only / none / none | n=10; difference=1 | n=10; difference=1 | n=10; difference=0 | n=10; difference=0 | n=10; difference=0 |

## Normal-path overhead

Timing includes process startup and fsynced measurement instrumentation. No warmups are run by default. SQL statement counts come from live SQLite connection tracing; write transactions are recorded after commit. These are measured SQL operations, not event counts. Compare A–D flags within mechanism; D_agent–E live differences combine implementation and provider cost and include LLM variability.

| Layer / config | Total seconds | SQL write statements | Committed write transactions |
|---|---|---|---|
| mechanism / A | n=20; median=1.6457; P95=3.7368; min=1.4862; max=4.5943 | n=20; median=84.0000; P95=84.0000; min=84.0000; max=84.0000 | n=20; median=49.0000; P95=49.0000; min=49.0000; max=49.0000 |
| mechanism / B | n=20; median=1.6597; P95=3.3368; min=1.5222; max=5.3130 | n=20; median=84.0000; P95=84.0000; min=84.0000; max=84.0000 | n=20; median=49.0000; P95=49.0000; min=49.0000; max=49.0000 |
| mechanism / C | n=20; median=1.6522; P95=3.0926; min=1.4792; max=5.3479 | n=20; median=84.0000; P95=84.0000; min=84.0000; max=84.0000 | n=20; median=49.0000; P95=49.0000; min=49.0000; max=49.0000 |
| mechanism / D | n=20; median=1.6285; P95=2.8947; min=1.4885; max=4.6032 | n=20; median=84.0000; P95=84.0000; min=84.0000; max=84.0000 | n=20; median=49.0000; P95=49.0000; min=49.0000; max=49.0000 |

## Failure / intervention evidence

- `C-opaque-fault-during_tool_execution-child_kill-6`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/C-opaque-fault-during_tool_execution-child_kill-6`
- `B-opaque-fault-during_tool_execution-child_kill-6`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-opaque-fault-during_tool_execution-child_kill-6`
- `D-opaque-fault-during_tool_execution-child_kill-6`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/D-opaque-fault-during_tool_execution-child_kill-6`
- `A-opaque-fault-during_tool_execution-child_kill-6`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-opaque-fault-during_tool_execution-child_kill-6`
- `B-receipt-fault-after_call_persist_before_start-runtime_kill-7`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-receipt-fault-after_call_persist_before_start-runtime_kill-7`
- `A-receipt-fault-after_call_persist_before_start-runtime_kill-7`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-receipt-fault-after_call_persist_before_start-runtime_kill-7`
- `B-receipt-fault-during_tool_execution-child_kill-9`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-receipt-fault-during_tool_execution-child_kill-9`
- `D-receipt-fault-during_tool_execution-child_kill-9`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/D-receipt-fault-during_tool_execution-child_kill-9`
- `A-receipt-fault-during_tool_execution-child_kill-9`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-receipt-fault-during_tool_execution-child_kill-9`
- `C-receipt-fault-during_tool_execution-child_kill-9`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/C-receipt-fault-during_tool_execution-child_kill-9`
- `A-receipt-fault-after_result_before_next_inference-runtime_kill-3`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-receipt-fault-after_result_before_next_inference-runtime_kill-3`
- `B-receipt-fault-after_result_before_next_inference-runtime_kill-3`: incomplete; effects=1; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-receipt-fault-after_result_before_next_inference-runtime_kill-3`
- `B-receipt-fault-before_call_persist-exception-9`: incomplete; effects=0; error=InjectedFault: injected exception at before_call_persist; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-receipt-fault-before_call_persist-exception-9`
- `A-receipt-fault-before_call_persist-exception-9`: incomplete; effects=0; error=InjectedFault: injected exception at before_call_persist; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-receipt-fault-before_call_persist-exception-9`
- `B-opaque-fault-after_result_before_next_inference-exception-7`: incomplete; effects=1; error=InjectedFault: injected exception at after_result_before_next_inference; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-opaque-fault-after_result_before_next_inference-exception-7`
- `A-opaque-fault-after_result_before_next_inference-exception-7`: incomplete; effects=1; error=InjectedFault: injected exception at after_result_before_next_inference; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-opaque-fault-after_result_before_next_inference-exception-7`
- `A-receipt-retry_only-none-none-7`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-receipt-retry_only-none-none-7`
- `C-receipt-retry_only-none-none-7`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/C-receipt-retry_only-none-none-7`
- `B-receipt-fault-after_result_before_next_inference-exception-9`: incomplete; effects=1; error=InjectedFault: injected exception at after_result_before_next_inference; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-receipt-fault-after_result_before_next_inference-exception-9`
- `A-receipt-fault-after_result_before_next_inference-exception-9`: incomplete; effects=1; error=InjectedFault: injected exception at after_result_before_next_inference; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-receipt-fault-after_result_before_next_inference-exception-9`
- `D-opaque-fault-during_tool_execution-exception-0`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/D-opaque-fault-during_tool_execution-exception-0`
- `B-opaque-fault-during_tool_execution-exception-0`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/B-opaque-fault-during_tool_execution-exception-0`
- `A-opaque-fault-during_tool_execution-exception-0`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/A-opaque-fault-during_tool_execution-exception-0`
- `C-opaque-fault-during_tool_execution-exception-0`: incomplete; effects=0; error=None; artifacts: `<REPO>/.tmp/factorial-mechanism-10/C-opaque-fault-during_tool_execution-exception-0`

All 640 such rows remain in raw JSON; examples above are capped at 24.

## Applicability and limits

32 matrix cells are explicitly excluded from eligibility:
- A / opaque / before_call_persist / child_kill: no live child at this boundary
- B / opaque / before_call_persist / child_kill: no live child at this boundary
- C / opaque / before_call_persist / child_kill: no live child at this boundary
- D / opaque / before_call_persist / child_kill: no live child at this boundary
- A / opaque / after_call_persist_before_start / child_kill: no live child at this boundary
- B / opaque / after_call_persist_before_start / child_kill: no live child at this boundary
- C / opaque / after_call_persist_before_start / child_kill: no live child at this boundary
- D / opaque / after_call_persist_before_start / child_kill: no live child at this boundary
- A / opaque / after_effect_before_result_persist / child_kill: no live child at this boundary
- B / opaque / after_effect_before_result_persist / child_kill: no live child at this boundary
- C / opaque / after_effect_before_result_persist / child_kill: no live child at this boundary
- D / opaque / after_effect_before_result_persist / child_kill: no live child at this boundary
- A / opaque / after_result_before_next_inference / child_kill: no live child at this boundary
- B / opaque / after_result_before_next_inference / child_kill: no live child at this boundary
- C / opaque / after_result_before_next_inference / child_kill: no live child at this boundary
- D / opaque / after_result_before_next_inference / child_kill: no live child at this boundary
- A / receipt / before_call_persist / child_kill: no live child at this boundary
- B / receipt / before_call_persist / child_kill: no live child at this boundary
- C / receipt / before_call_persist / child_kill: no live child at this boundary
- D / receipt / before_call_persist / child_kill: no live child at this boundary
- A / receipt / after_call_persist_before_start / child_kill: no live child at this boundary
- B / receipt / after_call_persist_before_start / child_kill: no live child at this boundary
- C / receipt / after_call_persist_before_start / child_kill: no live child at this boundary
- D / receipt / after_call_persist_before_start / child_kill: no live child at this boundary
- A / receipt / after_effect_before_result_persist / child_kill: no live child at this boundary
- B / receipt / after_effect_before_result_persist / child_kill: no live child at this boundary
- C / receipt / after_effect_before_result_persist / child_kill: no live child at this boundary
- D / receipt / after_effect_before_result_persist / child_kill: no live child at this boundary
- A / receipt / after_result_before_next_inference / child_kill: no live child at this boundary
- B / receipt / after_result_before_next_inference / child_kill: no live child at this boundary
- C / receipt / after_result_before_next_inference / child_kill: no live child at this boundary
- D / receipt / after_result_before_next_inference / child_kill: no live child at this boundary

No-recovery and safe-block outcomes are not automatic task completion. Zero observed duplicates does not prove exactly-once behavior. Unsafe replay uses the independent dispatch ledger and prior unresolved durable state, independently of the effect counter. Fixed-task continuation and receipt reconciliation do not establish real-model reliability. Failed/missed/deadline trials and external evidence are retained.

## Existing regression evidence

The historical 90/90 and 24/24 scenario assertion contracts remain separate regression checks. They are not denominators or comparative reliability rates in this report.

## Source fingerprint

```json
{
  "executable": "<REPO>/.venv/bin/python",
  "head": "32e48c773a658175e816e34762ee2b520a5ea32f",
  "platform": "macOS-26.1-arm64-arm-64bit",
  "python": "3.12.14 (main, Aug 25 2026, 13:50:33) [Clang 22.1.3 ]",
  "python_source_sha256": "d9a222feee3ba663fe87c013f3ceb2e9da284f315e808182412be93347ed6974",
  "tracked_diff_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```
