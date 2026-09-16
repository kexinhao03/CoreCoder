from corecoder.runtime import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    EventRecord,
    ExecutionKind,
    ExecutionRefused,
    FailureKind,
    InvalidTransition,
    ManagedProcessRunner,
    PendingApproval,
    ProcessResult,
    ProcessSpec,
    RecoveryCandidate,
    RecoveryKind,
    RecoveryManager,
    RecoveryResolution,
    RiskLevel,
    RunRecord,
    RunStatus,
    RuntimeExecutor,
    RuntimeResult,
    SQLiteStore,
    ToolCallRecord,
    ToolCallStatus,
    ToolPolicy,
    ToolPolicyRegistry,
)


def test_runtime_public_api():
    assert SQLiteStore is not None
    assert RunStatus.CREATED.value == "created"
    assert ToolCallStatus.INTERRUPTED.value == "interrupted"
    exported = (
        EventRecord, ExecutionKind, InvalidTransition, RiskLevel, RunRecord,
        RunStatus, SQLiteStore, ToolCallRecord, ToolCallStatus,
        ApprovalDecision, ApprovalRecord, ApprovalStatus, ExecutionRefused,
        FailureKind, ManagedProcessRunner, PendingApproval, ProcessResult,
        ProcessSpec, RecoveryCandidate, RecoveryKind, RecoveryManager,
        RecoveryResolution, RuntimeExecutor, RuntimeResult, ToolPolicy,
        ToolPolicyRegistry,
    )
    assert all(symbol is not None for symbol in exported)
