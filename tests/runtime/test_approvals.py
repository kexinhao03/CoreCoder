from dataclasses import FrozenInstanceError

import pytest

from corecoder.runtime.approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    summarize_arguments,
)


def test_approval_record_is_frozen():
    approval = ApprovalRecord(
        id="approval-1",
        tool_call_id="call-1",
        status=ApprovalStatus.PENDING,
        decision=None,
        tool_name="bash",
        arguments_summary='{"argv": ["pytest"]}',
        workspace="/tmp/work",
        risk_reason="runs a command",
        requested_at="t0",
        resolved_at=None,
    )

    with pytest.raises(FrozenInstanceError):
        approval.status = ApprovalStatus.APPROVED


def test_argument_summary_redacts_sensitive_keys_and_is_bounded():
    arguments = {
        "argv": ["client", "--verbose"],
        "api_key": "secret-value",
        "nested": {"password": "hidden", "text": "x" * 2000},
    }
    original_nested = arguments["nested"].copy()

    summary = summarize_arguments(arguments)

    assert "secret-value" not in summary
    assert "hidden" not in summary
    assert "[REDACTED]" in summary
    assert len(summary) == 1000
    assert arguments == {
        "argv": ["client", "--verbose"],
        "api_key": "secret-value",
        "nested": original_nested,
    }


def test_argument_summary_redacts_sensitive_keys_nested_in_tuples():
    arguments = {"items": ({"password": "hidden"},)}

    summary = summarize_arguments(arguments)

    assert summary == '{"items": [{"password": "[REDACTED]"}]}'
    assert arguments == {"items": ({"password": "hidden"},)}


def test_argument_summary_normalizes_mixed_basic_key_types():
    arguments = {1: "integer", "1": "string"}

    summary = summarize_arguments(arguments)

    assert summary == '{"1": "string", "[int:1]": "integer"}'
    assert arguments == {1: "integer", "1": "string"}


def test_argument_summary_suffixes_colliding_typed_key_labels():
    arguments = {1: "integer", "[int:1]": "string"}

    summary = summarize_arguments(arguments)

    assert summary == '{"[int:1]": "string", "[int:1]#2": "integer"}'
    assert arguments == {1: "integer", "[int:1]": "string"}


def test_approval_values_are_stable():
    assert ApprovalStatus.PENDING.value == "pending"
    assert ApprovalDecision.ALLOW_ONCE.value == "allow_once"
