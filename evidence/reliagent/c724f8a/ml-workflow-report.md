# ReliAgent Evaluation

Token/Cost: not available unless run-scoped LLM telemetry is captured.

Scenario-contract assertions passed: 24 / 24

| Configuration | Contract assertions | Task success | Recovery success | Duplicate effects | Trace complete | Errors |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 8 / 8 | 37.5% | 0.0% | 0.0% | 100.0% | 0 |
| full | 8 / 8 | 62.5% | 50.0% | 0.0% | 100.0% | 0 |
| no_recovery | 8 / 8 | 37.5% | 0.0% | 0.0% | 100.0% | 0 |

| Case | Scenario | Config | Repetition | Passed | Task | Recovery | Error |
|---|---|---|---:|---|---|---|---|
| ML01 normal approval success | ml_normal_success | baseline | 1 | True | True | None |  |
| ML01 normal approval success | ml_normal_success | full | 1 | True | True | None |  |
| ML01 normal approval success | ml_normal_success | no_recovery | 1 | True | True | None |  |
| ML02 environment process loss recovery | ml_environment_recovery | baseline | 1 | True | False | False |  |
| ML02 environment process loss recovery | ml_environment_recovery | full | 1 | True | True | True |  |
| ML02 environment process loss recovery | ml_environment_recovery | no_recovery | 1 | True | False | False |  |
| ML03 experiment after-effect interruption | ml_experiment_after_effect | baseline | 1 | True | False | False |  |
| ML03 experiment after-effect interruption | ml_experiment_after_effect | full | 1 | True | False | False |  |
| ML03 experiment after-effect interruption | ml_experiment_after_effect | no_recovery | 1 | True | False | False |  |
| ML04 valid completed reconciliation | ml_valid_reconciliation | baseline | 1 | True | False | False |  |
| ML04 valid completed reconciliation | ml_valid_reconciliation | full | 1 | True | True | True |  |
| ML04 valid completed reconciliation | ml_valid_reconciliation | no_recovery | 1 | True | False | False |  |
| ML05 damaged artifact reconciliation refusal | ml_damaged_reconciliation | baseline | 1 | True | False | False |  |
| ML05 damaged artifact reconciliation refusal | ml_damaged_reconciliation | full | 1 | True | False | False |  |
| ML05 damaged artifact reconciliation refusal | ml_damaged_reconciliation | no_recovery | 1 | True | False | False |  |
| ML06 repeated successful resume | ml_repeated_resume | baseline | 1 | True | True | None |  |
| ML06 repeated successful resume | ml_repeated_resume | full | 1 | True | True | None |  |
| ML06 repeated successful resume | ml_repeated_resume | no_recovery | 1 | True | True | None |  |
| ML07 approval denial | ml_approval_denial | baseline | 1 | True | False | None |  |
| ML07 approval denial | ml_approval_denial | full | 1 | True | False | None |  |
| ML07 approval denial | ml_approval_denial | no_recovery | 1 | True | False | None |  |
| ML08 report refusal after artifact tamper | ml_report_tamper | baseline | 1 | True | True | None |  |
| ML08 report refusal after artifact tamper | ml_report_tamper | full | 1 | True | True | None |  |
| ML08 report refusal after artifact tamper | ml_report_tamper | no_recovery | 1 | True | True | None |  |
