import pytest

from corecoder.runtime.policies import FailureKind, ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.state import ExecutionKind, RiskLevel


def test_unknown_tool_uses_deny_safe_policy():
    policy = ToolPolicyRegistry().resolve("unregistered")
    assert policy.risk_level is RiskLevel.EXTERNAL_EFFECT
    assert policy.execution_kind is ExecutionKind.IN_PROCESS
    assert policy.idempotent is False
    assert policy.auto_retry is False
    assert policy.requires_approval is True


def test_builtin_reads_are_automatic_but_bash_requires_approval():
    registry = ToolPolicyRegistry.with_builtin_defaults()
    assert registry.resolve("read_file").requires_approval is False
    assert registry.resolve("grep").requires_approval is False
    assert registry.resolve("bash").requires_approval is True
    assert registry.resolve("bash").execution_kind is ExecutionKind.SUBPROCESS


def test_auto_retry_requires_read_only_idempotent_policy():
    with pytest.raises(ValueError, match="auto_retry requires read-only idempotent policy"):
        ToolPolicy(
            risk_level=RiskLevel.MUTATING,
            execution_kind=ExecutionKind.SUBPROCESS,
            timeout_seconds=10,
            max_attempts=2,
            idempotent=True,
            auto_retry=True,
            retryable_failures=frozenset({FailureKind.TIMED_OUT}),
            output_limit=1000,
        )
