# ReliAgent Evaluation

Token/Cost: not available unless run-scoped LLM telemetry is captured.

Scenario-contract assertions passed: 90 / 90

| Configuration | Contract assertions | Task success | Recovery success | Duplicate effects | Trace complete | Errors |
|---|---:|---:|---:|---:|---:|---:|
| full | 30 / 30 | 40.0% | 50.0% | 0.0% | 100.0% | 0 |
| no_recovery | 30 / 30 | 20.0% | 0.0% | 0.0% | 100.0% | 0 |
| no_retry_no_recovery | 30 / 30 | 10.0% | 0.0% | 0.0% | 100.0% | 0 |

| Case | Scenario | Config | Repetition | Passed | Task | Recovery | Error |
|---|---|---|---:|---|---|---|---|
| E01 normal success | normal_success | no_retry_no_recovery | 1 | True | True | None |  |
| E01 normal success | normal_success | no_retry_no_recovery | 2 | True | True | None |  |
| E01 normal success | normal_success | no_retry_no_recovery | 3 | True | True | None |  |
| E01 normal success | normal_success | full | 1 | True | True | None |  |
| E01 normal success | normal_success | full | 2 | True | True | None |  |
| E01 normal success | normal_success | full | 3 | True | True | None |  |
| E01 normal success | normal_success | no_recovery | 1 | True | True | None |  |
| E01 normal success | normal_success | no_recovery | 2 | True | True | None |  |
| E01 normal success | normal_success | no_recovery | 3 | True | True | None |  |
| E02 temporary error then success | transient_then_success | no_retry_no_recovery | 1 | True | False | None |  |
| E02 temporary error then success | transient_then_success | no_retry_no_recovery | 2 | True | False | None |  |
| E02 temporary error then success | transient_then_success | no_retry_no_recovery | 3 | True | False | None |  |
| E02 temporary error then success | transient_then_success | full | 1 | True | True | None |  |
| E02 temporary error then success | transient_then_success | full | 2 | True | True | None |  |
| E02 temporary error then success | transient_then_success | full | 3 | True | True | None |  |
| E02 temporary error then success | transient_then_success | no_recovery | 1 | True | True | None |  |
| E02 temporary error then success | transient_then_success | no_recovery | 2 | True | True | None |  |
| E02 temporary error then success | transient_then_success | no_recovery | 3 | True | True | None |  |
| E03 retry exhaustion | retry_exhausted | no_retry_no_recovery | 1 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | no_retry_no_recovery | 2 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | no_retry_no_recovery | 3 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | full | 1 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | full | 2 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | full | 3 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | no_recovery | 1 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | no_recovery | 2 | True | False | None |  |
| E03 retry exhaustion | retry_exhausted | no_recovery | 3 | True | False | None |  |
| E04 timeout | timeout | no_retry_no_recovery | 1 | True | False | None |  |
| E04 timeout | timeout | no_retry_no_recovery | 2 | True | False | None |  |
| E04 timeout | timeout | no_retry_no_recovery | 3 | True | False | None |  |
| E04 timeout | timeout | full | 1 | True | False | None |  |
| E04 timeout | timeout | full | 2 | True | False | None |  |
| E04 timeout | timeout | full | 3 | True | False | None |  |
| E04 timeout | timeout | no_recovery | 1 | True | False | None |  |
| E04 timeout | timeout | no_recovery | 2 | True | False | None |  |
| E04 timeout | timeout | no_recovery | 3 | True | False | None |  |
| E05 approval denial | approval_denied | no_retry_no_recovery | 1 | True | False | None |  |
| E05 approval denial | approval_denied | no_retry_no_recovery | 2 | True | False | None |  |
| E05 approval denial | approval_denied | no_retry_no_recovery | 3 | True | False | None |  |
| E05 approval denial | approval_denied | full | 1 | True | False | None |  |
| E05 approval denial | approval_denied | full | 2 | True | False | None |  |
| E05 approval denial | approval_denied | full | 3 | True | False | None |  |
| E05 approval denial | approval_denied | no_recovery | 1 | True | False | None |  |
| E05 approval denial | approval_denied | no_recovery | 2 | True | False | None |  |
| E05 approval denial | approval_denied | no_recovery | 3 | True | False | None |  |
| E06 crash after Step completion | crash_after_step | no_retry_no_recovery | 1 | True | False | False |  |
| E06 crash after Step completion | crash_after_step | no_retry_no_recovery | 2 | True | False | False |  |
| E06 crash after Step completion | crash_after_step | no_retry_no_recovery | 3 | True | False | False |  |
| E06 crash after Step completion | crash_after_step | full | 1 | True | True | True |  |
| E06 crash after Step completion | crash_after_step | full | 2 | True | True | True |  |
| E06 crash after Step completion | crash_after_step | full | 3 | True | True | True |  |
| E06 crash after Step completion | crash_after_step | no_recovery | 1 | True | False | False |  |
| E06 crash after Step completion | crash_after_step | no_recovery | 2 | True | False | False |  |
| E06 crash after Step completion | crash_after_step | no_recovery | 3 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | no_retry_no_recovery | 1 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | no_retry_no_recovery | 2 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | no_retry_no_recovery | 3 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | full | 1 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | full | 2 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | full | 3 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | no_recovery | 1 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | no_recovery | 2 | True | False | False |  |
| E07 high-risk operation interrupted | high_risk_interrupted | no_recovery | 3 | True | False | False |  |
| E08 repeated resume | repeated_resume | no_retry_no_recovery | 1 | True | False | False |  |
| E08 repeated resume | repeated_resume | no_retry_no_recovery | 2 | True | False | False |  |
| E08 repeated resume | repeated_resume | no_retry_no_recovery | 3 | True | False | False |  |
| E08 repeated resume | repeated_resume | full | 1 | True | True | True |  |
| E08 repeated resume | repeated_resume | full | 2 | True | True | True |  |
| E08 repeated resume | repeated_resume | full | 3 | True | True | True |  |
| E08 repeated resume | repeated_resume | no_recovery | 1 | True | False | False |  |
| E08 repeated resume | repeated_resume | no_recovery | 2 | True | False | False |  |
| E08 repeated resume | repeated_resume | no_recovery | 3 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | no_retry_no_recovery | 1 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | no_retry_no_recovery | 2 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | no_retry_no_recovery | 3 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | full | 1 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | full | 2 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | full | 3 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | no_recovery | 1 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | no_recovery | 2 | True | False | False |  |
| E09 effect completed before persistence failure | effect_persist_failure | no_recovery | 3 | True | False | False |  |
| E10 cancel Run | cancel_run | no_retry_no_recovery | 1 | True | False | None |  |
| E10 cancel Run | cancel_run | no_retry_no_recovery | 2 | True | False | None |  |
| E10 cancel Run | cancel_run | no_retry_no_recovery | 3 | True | False | None |  |
| E10 cancel Run | cancel_run | full | 1 | True | False | None |  |
| E10 cancel Run | cancel_run | full | 2 | True | False | None |  |
| E10 cancel Run | cancel_run | full | 3 | True | False | None |  |
| E10 cancel Run | cancel_run | no_recovery | 1 | True | False | None |  |
| E10 cancel Run | cancel_run | no_recovery | 2 | True | False | None |  |
| E10 cancel Run | cancel_run | no_recovery | 3 | True | False | None |  |
