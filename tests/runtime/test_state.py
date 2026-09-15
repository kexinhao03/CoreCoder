import pytest

from corecoder.runtime.state import (
    InvalidTransition,
    RunStatus,
    ToolCallStatus,
    ensure_run_transition,
    ensure_tool_call_transition,
)


def test_run_can_start_and_finish():
    ensure_run_transition(RunStatus.CREATED, RunStatus.RUNNING)
    ensure_run_transition(RunStatus.RUNNING, RunStatus.SUCCEEDED)


def test_terminal_run_cannot_return_to_running():
    with pytest.raises(InvalidTransition, match="succeeded -> running"):
        ensure_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)


def test_running_tool_call_can_be_interrupted():
    ensure_tool_call_transition(ToolCallStatus.RUNNING, ToolCallStatus.INTERRUPTED)


def test_terminal_tool_call_cannot_be_retried_in_place():
    with pytest.raises(InvalidTransition, match="failed -> running"):
        ensure_tool_call_transition(ToolCallStatus.FAILED, ToolCallStatus.RUNNING)
