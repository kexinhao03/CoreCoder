"""Tool risk, execution, and failure classification policies."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from corecoder.runtime.state import ExecutionKind, RiskLevel


class FailureKind(str, Enum):
    NONZERO_EXIT = "nonzero_exit"
    TIMED_OUT = "timed_out"
    SPAWN_ERROR = "spawn_error"
    EXECUTION_ERROR = "execution_error"
    CANCELLED = "cancelled"
    TERMINATION_UNKNOWN = "termination_unknown"


@dataclass(frozen=True)
class ToolPolicy:
    risk_level: RiskLevel
    execution_kind: ExecutionKind
    timeout_seconds: int
    max_attempts: int
    idempotent: bool
    auto_retry: bool
    retryable_failures: frozenset[FailureKind]
    output_limit: int

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.output_limit <= 0:
            raise ValueError("output_limit must be positive")
        if self.auto_retry and (
            not self.idempotent or self.risk_level is not RiskLevel.READ_ONLY
        ):
            raise ValueError("auto_retry requires read-only idempotent policy")

    @property
    def requires_approval(self) -> bool:
        return self.risk_level is not RiskLevel.READ_ONLY


def requires_approval(policy: ToolPolicy) -> bool:
    """Return whether executing a policy requires explicit approval."""
    return policy.requires_approval


class ToolPolicyRegistry:
    _DEFAULT_POLICY = ToolPolicy(
        risk_level=RiskLevel.EXTERNAL_EFFECT,
        execution_kind=ExecutionKind.IN_PROCESS,
        timeout_seconds=30,
        max_attempts=1,
        idempotent=False,
        auto_retry=False,
        retryable_failures=frozenset(),
        output_limit=15_000,
    )

    def __init__(self, policies: Mapping[str, ToolPolicy] | None = None) -> None:
        self._policies = dict(policies or {})

    def resolve(self, tool_name: str) -> ToolPolicy:
        return self._policies.get(tool_name, self._DEFAULT_POLICY)

    @classmethod
    def with_builtin_defaults(cls) -> "ToolPolicyRegistry":
        read_only = ToolPolicy(
            risk_level=RiskLevel.READ_ONLY,
            execution_kind=ExecutionKind.IN_PROCESS,
            timeout_seconds=30,
            max_attempts=1,
            idempotent=True,
            auto_retry=False,
            retryable_failures=frozenset(),
            output_limit=15_000,
        )
        mutating = ToolPolicy(
            risk_level=RiskLevel.MUTATING,
            execution_kind=ExecutionKind.IN_PROCESS,
            timeout_seconds=30,
            max_attempts=1,
            idempotent=False,
            auto_retry=False,
            retryable_failures=frozenset(),
            output_limit=15_000,
        )
        bash = ToolPolicy(
            risk_level=RiskLevel.MUTATING,
            execution_kind=ExecutionKind.SUBPROCESS,
            timeout_seconds=30,
            max_attempts=1,
            idempotent=False,
            auto_retry=False,
            retryable_failures=frozenset(),
            output_limit=15_000,
        )
        return cls(
            {
                "read_file": read_only,
                "glob": read_only,
                "grep": read_only,
                "write_file": mutating,
                "edit_file": mutating,
                "bash": bash,
            }
        )
