"""Fixed Runtime policies for the deterministic ML workflow."""

from corecoder.runtime import (
    ExecutionKind,
    FailureKind,
    RiskLevel,
    ToolPolicy,
    ToolPolicyRegistry,
)


def build_policy_registry(
    *, max_attempts: int = 2, auto_retry: bool = True
) -> ToolPolicyRegistry:
    retryable = frozenset(
        {FailureKind.NONZERO_EXIT, FailureKind.SPAWN_ERROR, FailureKind.TIMED_OUT}
    )
    read_only = ToolPolicy(
        risk_level=RiskLevel.READ_ONLY,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=30,
        max_attempts=max_attempts,
        idempotent=True,
        auto_retry=auto_retry,
        retryable_failures=retryable,
        output_limit=15_000,
    )
    experiment = ToolPolicy(
        risk_level=RiskLevel.EXTERNAL_EFFECT,
        execution_kind=ExecutionKind.SUBPROCESS,
        timeout_seconds=30,
        max_attempts=1,
        idempotent=False,
        auto_retry=False,
        retryable_failures=frozenset(),
        output_limit=15_000,
    )
    return ToolPolicyRegistry(
        {
            "ml_experiment.environment_check": read_only,
            "ml_experiment.run_experiment": experiment,
            "ml_experiment.extract_metrics": read_only,
            "ml_experiment.verify_effect": read_only,
        }
    )
