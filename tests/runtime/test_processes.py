import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from corecoder.runtime import processes
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


def ready_gated_monotonic(monkeypatch, ready):
    real_monotonic = time.monotonic
    frozen_at = real_monotonic()
    ready_at = None

    def clock():
        nonlocal ready_at
        if ready_at is None:
            if not ready.exists():
                return frozen_at
            ready_at = real_monotonic()
        return frozen_at + (real_monotonic() - ready_at)

    monkeypatch.setattr(processes, "monotonic", clock)


def delay_popen(monkeypatch):
    real_popen = subprocess.Popen

    def delayed_popen(*args, **kwargs):
        time.sleep(0.4)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", delayed_popen)


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
    assert result.process_evidence is not None
    assert result.process_evidence.pid > 0
    if os.name == "posix":
        assert result.process_evidence.pgid == result.process_evidence.pid
    assert len(result.process_evidence.process_token) == 32
    expected_argv_sha256 = hashlib.sha256(
        json.dumps(
            [sys.executable, "-c", "print('hello')"],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert result.process_evidence.argv_sha256 == expected_argv_sha256
    assert result.process_evidence.started_at.endswith("+00:00")
    assert result.process_evidence.ended_at.endswith("+00:00")
    assert result.process_evidence.termination_confirmed is True


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
        make_spec(
            tmp_path,
            str(tmp_path / "missing-command"),
            output_limit=4,
        ),
        threading.Event(),
    )

    assert result.exit_code is None
    assert result.stdout == ""
    assert len(result.stderr) <= 4
    assert result.failure_kind is FailureKind.SPAWN_ERROR
    assert result.termination_confirmed is True


def test_timeout_is_bounded_and_confirmed(tmp_path):
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(3)"),
            cwd=tmp_path,
            timeout_seconds=0.1,
            output_limit=1000,
            termination_grace_seconds=0.1,
        ),
        threading.Event(),
    )

    assert result.failure_kind is FailureKind.TIMED_OUT
    assert result.termination_confirmed is True
    assert result.exit_code is not None
    assert result.duration_seconds < 2


def test_pre_cancelled_request_never_spawns(tmp_path):
    marker = tmp_path / "started"
    cancelled = threading.Event()
    cancelled.set()

    result = ManagedProcessRunner().run(
        make_spec(
            tmp_path,
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close()",
        ),
        cancelled,
    )

    assert result.failure_kind is FailureKind.CANCELLED
    assert result.termination_confirmed is True
    assert result.exit_code is None
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_timeout_kills_descendant_process(tmp_path, monkeypatch):
    survived = tmp_path / "child-survived"
    child_ready = tmp_path / "child-ready"
    parent_ready = tmp_path / "parent-ready"
    child = (
        f"import time; open({str(child_ready)!r}, 'w').close(); time.sleep(3); "
        f"open({str(survived)!r}, 'w').close()"
    )
    parent = (
        "import signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "def stop(signum, frame):\n"
        "    child.wait(timeout=1)\n"
        "    print('child-reaped', flush=True)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "deadline = time.monotonic() + 1.5\n"
        f"while not Path({str(child_ready)!r}).exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        child.kill()\n"
        "        child.wait(timeout=1)\n"
        "        raise SystemExit('child readiness timed out')\n"
        "    time.sleep(0.01)\n"
        f"Path({str(parent_ready)!r}).touch()\n"
        "time.sleep(10)\n"
    )
    ready_gated_monotonic(monkeypatch, parent_ready)
    delay_popen(monkeypatch)
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", parent),
            cwd=tmp_path,
            timeout_seconds=0.1,
            output_limit=1000,
            termination_grace_seconds=1.5,
        ),
        threading.Event(),
    )

    assert child_ready.exists()
    assert parent_ready.exists()
    time.sleep(3.2)
    assert result.failure_kind is FailureKind.TIMED_OUT
    assert result.termination_confirmed is True
    assert result.exit_code == 0
    assert result.stdout == "child-reaped\n"
    assert result.duration_seconds < 1
    assert not survived.exists()


def test_running_process_can_be_cancelled(tmp_path):
    ready = tmp_path / "ready"
    cancelled = threading.Event()

    def cancel_when_ready():
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancelled.set()

    canceller = threading.Thread(target=cancel_when_ready)
    canceller.start()
    try:
        result = ManagedProcessRunner().run(
            ProcessSpec(
                argv=(
                    sys.executable,
                    "-c",
                    (
                        "import time; print('started', flush=True); "
                        f"open({str(ready)!r}, 'w').close(); time.sleep(10)"
                    ),
                ),
                cwd=tmp_path,
                timeout_seconds=5,
                output_limit=1000,
                termination_grace_seconds=0.2,
            ),
            cancelled,
        )
    finally:
        cancelled.set()
        canceller.join(timeout=3)

    assert not canceller.is_alive(), "canceller did not finish within 3 seconds"
    assert ready.exists(), "process did not become ready within 3 seconds"

    assert result.failure_kind is FailureKind.CANCELLED
    assert result.termination_confirmed is True
    assert result.exit_code is not None
    assert result.stdout == "started\n"
    assert result.duration_seconds < 5


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal handling")
@pytest.mark.parametrize("grace", [0, 0.1])
def test_timeout_force_kills_process_ignoring_term(tmp_path, grace, monkeypatch):
    ready = tmp_path / "ready"
    ready_gated_monotonic(monkeypatch, ready)
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(
                sys.executable,
                "-c",
                (
                    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('ready', flush=True); "
                    f"open({str(ready)!r}, 'w').close(); time.sleep(10)"
                ),
            ),
            cwd=tmp_path,
            timeout_seconds=2,
            output_limit=1000,
            termination_grace_seconds=grace,
        ),
        threading.Event(),
    )

    assert ready.exists()
    assert result.failure_kind is FailureKind.TIMED_OUT
    assert result.termination_confirmed is True
    assert result.exit_code == -signal.SIGKILL
    assert result.stdout == "ready\n"
    assert result.duration_seconds < 4


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_timeout_kills_resistant_child_after_parent_exits(tmp_path, monkeypatch):
    survived = tmp_path / "child-survived"
    ready = tmp_path / "child-ready"
    parent_ready = tmp_path / "parent-ready"
    child = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(ready)!r}, 'w').close(); time.sleep(3); "
        f"open({str(survived)!r}, 'w').close()"
    )
    parent = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "deadline = time.monotonic() + 1.5\n"
        f"while not Path({str(ready)!r}).exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        child.kill()\n"
        "        child.wait(timeout=1)\n"
        "        raise SystemExit('child readiness timed out')\n"
        "    time.sleep(0.01)\n"
        f"Path({str(parent_ready)!r}).touch()\n"
        "time.sleep(10)\n"
    )
    real_termination_confirmed = processes._termination_confirmed
    confirmations = []

    def observe_termination(process):
        confirmed = real_termination_confirmed(process)
        confirmations.append(confirmed)
        return confirmed

    monkeypatch.setattr(processes, "_termination_confirmed", observe_termination)
    ready_gated_monotonic(monkeypatch, parent_ready)
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", parent),
            cwd=tmp_path,
            timeout_seconds=2,
            output_limit=1000,
            termination_grace_seconds=0.1,
        ),
        threading.Event(),
    )

    assert ready.exists()
    assert parent_ready.exists()
    time.sleep(3.2)
    assert not survived.exists()
    assert confirmations
    if confirmations[-1]:
        assert result.failure_kind is FailureKind.TIMED_OUT
        assert result.termination_confirmed is True
        assert result.exit_code is not None
    else:
        assert result.failure_kind is FailureKind.TERMINATION_UNKNOWN
        assert result.termination_confirmed is False
        assert result.exit_code is None
    assert result.duration_seconds < 4


@pytest.mark.skipif(os.name != "posix", reason="POSIX graceful signal handling")
def test_timeout_drains_output_written_during_grace(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    code = (
        "import signal, sys, time\n"
        "def stop(signum, frame):\n"
        "    print('stopped', flush=True)\n"
        "    print('error', file=sys.stderr, flush=True)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('ready', flush=True)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(10)\n"
    )
    ready_gated_monotonic(monkeypatch, ready)
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=2,
            output_limit=1000,
            termination_grace_seconds=0.2,
        ),
        threading.Event(),
    )

    assert ready.exists()
    assert result.failure_kind is FailureKind.TIMED_OUT
    assert result.termination_confirmed is True
    assert result.exit_code == 0
    assert result.stdout == "ready\nstopped\n"
    assert result.stderr == "error\n"


def test_polling_preserves_output_without_repeating_partial_bytes(tmp_path):
    result = ManagedProcessRunner().run(
        make_spec(
            tmp_path,
            sys.executable,
            "-c",
            "import sys, time; print('first', flush=True); time.sleep(0.2); "
            "sys.stdout.buffer.write(b'last\\xff'); sys.stderr.write('error')",
        ),
        threading.Event(),
    )

    assert result.failure_kind is None
    assert result.stdout == "first\nlast\ufffd"
    assert result.stderr == "error"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal failure injection")
@pytest.mark.parametrize("error_type", [PermissionError, ProcessLookupError])
def test_signal_failure_with_live_process_is_termination_unknown(tmp_path, monkeypatch, error_type):
    ready = tmp_path / "ready"
    real_popen = subprocess.Popen
    real_killpg = os.killpg
    processes = []

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail_signal(pgid, sig):
        if sig:
            raise error_type("injected signal failure")
        return real_killpg(pgid, sig)

    ready_gated_monotonic(monkeypatch, ready)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "killpg", fail_signal)
    try:
        result = ManagedProcessRunner().run(
            ProcessSpec(
                argv=(
                    sys.executable,
                    "-c",
                    (
                        "import sys, time; print('partial', flush=True); "
                        "print('error', file=sys.stderr, flush=True); "
                        f"open({str(ready)!r}, 'w').close(); time.sleep(10)"
                    ),
                ),
                cwd=tmp_path,
                timeout_seconds=2,
                output_limit=1000,
                termination_grace_seconds=0,
            ),
            threading.Event(),
        )
        assert ready.exists()
        assert processes[0].poll() is None
        assert result.failure_kind is FailureKind.TERMINATION_UNKNOWN
        assert result.termination_confirmed is False
        assert result.exit_code is None
        assert result.stdout == "partial\n"
        assert result.stderr == "error\n"
        assert result.duration_seconds < 4
        assert processes[0].stdout.closed
        assert processes[0].stderr.closed
    finally:
        for process in processes:
            process.kill()
            process.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group confirmation")
def test_unconfirmed_group_is_unknown_even_when_parent_and_pipes_exit(tmp_path, monkeypatch):
    real_killpg = os.killpg

    def lingering_group(pgid, sig):
        if sig == 0:
            return None
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", lingering_group)
    result = ManagedProcessRunner().run(
        ProcessSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(3)"),
            cwd=tmp_path,
            timeout_seconds=0.1,
            output_limit=1000,
            termination_grace_seconds=0,
        ),
        threading.Event(),
    )

    assert result.failure_kind is FailureKind.TERMINATION_UNKNOWN
    assert result.termination_confirmed is False
    assert result.exit_code is None
    assert result.duration_seconds < 2


def test_natural_exit_wins_cancellation_at_poll_boundary(tmp_path, monkeypatch):
    cancelled = threading.Event()
    real_communicate = subprocess.Popen.communicate

    def finish_before_cancellation(process, *args, **kwargs):
        stdout, stderr = real_communicate(process, *args, **kwargs)
        if not cancelled.is_set():
            cancelled.set()
            raise subprocess.TimeoutExpired(process.args, 0.05, output=stdout, stderr=stderr)
        return stdout, stderr

    monkeypatch.setattr(subprocess.Popen, "communicate", finish_before_cancellation)
    result = ManagedProcessRunner().run(
        make_spec(tmp_path, sys.executable, "-c", "print('finished')"),
        cancelled,
    )

    assert result.failure_kind is None
    assert result.termination_confirmed is True
    assert result.exit_code == 0
    assert result.stdout == "finished\n"


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
