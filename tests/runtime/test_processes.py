import sys
import threading
from dataclasses import FrozenInstanceError

import pytest

from corecoder.runtime.policies import FailureKind
from corecoder.runtime.processes import ManagedProcessRunner, ProcessResult, ProcessSpec


def make_spec(tmp_path, *argv: str, output_limit: int = 1000) -> ProcessSpec:
    return ProcessSpec(
        argv=argv,
        cwd=tmp_path,
        timeout_seconds=2,
        output_limit=output_limit,
        termination_grace_seconds=0.2,
    )


def test_process_runner_captures_success(tmp_path):
    result = ManagedProcessRunner().run(
        make_spec(tmp_path, sys.executable, "-c", "print('hello')"),
        threading.Event(),
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.duration_seconds >= 0
    assert result.failure_kind is None
    assert result.termination_confirmed is True


def test_process_runner_classifies_nonzero_exit(tmp_path):
    result = ManagedProcessRunner().run(
        make_spec(tmp_path, sys.executable, "-c", "raise SystemExit(7)"),
        threading.Event(),
    )

    assert result.exit_code == 7
    assert result.failure_kind is FailureKind.NONZERO_EXIT
    assert result.termination_confirmed is True


def test_process_runner_bounds_stdout_and_stderr_independently(tmp_path):
    code = (
        "import sys; "
        "sys.stdout.write('A' * 60 + 'x' * 100 + 'Z' * 40); "
        "sys.stderr.write('B' * 60 + 'y' * 100 + 'Y' * 40)"
    )
    result = ManagedProcessRunner().run(
        make_spec(tmp_path, sys.executable, "-c", code, output_limit=50),
        threading.Event(),
    )

    assert result.stdout == "A" * 14 + "\n... output truncated ...\n" + "Z" * 10
    assert result.stderr == "B" * 14 + "\n... output truncated ...\n" + "Y" * 10
    assert len(result.stdout) == 50
    assert len(result.stderr) == 50


def test_process_runner_bounds_output_when_limit_is_smaller_than_marker(tmp_path):
    result = ManagedProcessRunner().run(
        make_spec(
            tmp_path,
            sys.executable,
            "-c",
            "print('content longer than limit')",
            output_limit=4,
        ),
        threading.Event(),
    )

    assert result.stdout == "\n..."


def test_process_runner_decodes_invalid_utf8_with_replacement(tmp_path):
    result = ManagedProcessRunner().run(
        make_spec(
            tmp_path,
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'good\\xffbad')",
        ),
        threading.Event(),
    )

    assert result.stdout == "good\ufffdbad"


def test_process_runner_passes_explicit_environment(tmp_path):
    spec = make_spec(
        tmp_path,
        sys.executable,
        "-c",
        "import os; print(os.environ['RELIAGENT_PROCESS_TEST'])",
    )
    spec = ProcessSpec(
        argv=spec.argv,
        cwd=spec.cwd,
        timeout_seconds=spec.timeout_seconds,
        output_limit=spec.output_limit,
        termination_grace_seconds=spec.termination_grace_seconds,
        environment={"RELIAGENT_PROCESS_TEST": "explicit"},
    )

    result = ManagedProcessRunner().run(spec, threading.Event())

    assert result.stdout.strip() == "explicit"


def test_process_runner_classifies_spawn_error(tmp_path):
    result = ManagedProcessRunner().run(
        make_spec(tmp_path, str(tmp_path / "missing-command")),
        threading.Event(),
    )

    assert result.exit_code is None
    assert result.stdout == ""
    assert result.failure_kind is FailureKind.SPAWN_ERROR
    assert result.termination_confirmed is True


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"argv": ()}, "argv must not be empty"),
        ({"timeout_seconds": 0}, "timeout_seconds must be positive"),
        ({"output_limit": 0}, "output_limit must be positive"),
        ({"termination_grace_seconds": -0.1}, "termination_grace_seconds must be non-negative"),
    ],
)
def test_process_spec_rejects_invalid_values(tmp_path, changes, message):
    values = {
        "argv": (sys.executable,),
        "cwd": tmp_path,
        "timeout_seconds": 2,
        "output_limit": 1000,
        "termination_grace_seconds": 0.2,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ProcessSpec(**values)


def test_process_models_are_immutable(tmp_path):
    spec = make_spec(tmp_path, sys.executable)
    result = ProcessResult(
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.1,
        failure_kind=None,
        termination_confirmed=True,
    )

    with pytest.raises(FrozenInstanceError):
        spec.output_limit = 1
    with pytest.raises(FrozenInstanceError):
        result.stdout = "changed"
