"""Bounded subprocess execution results."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from corecoder.runtime.policies import FailureKind

_TRUNCATION_MARKER = "\n... output truncated ...\n"
monotonic = time.monotonic


@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    output_limit: int
    termination_grace_seconds: float
    environment: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.output_limit <= 0:
            raise ValueError("output_limit must be positive")
        if self.termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds must be non-negative")


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    failure_kind: FailureKind | None
    termination_confirmed: bool


class ManagedProcessRunner:
    def run(
        self,
        spec: ProcessSpec,
        cancellation_event: threading.Event,
    ) -> ProcessResult:
        started_at = monotonic()
        if cancellation_event.is_set():
            return ProcessResult(
                exit_code=None,
                stdout="",
                stderr="",
                duration_seconds=monotonic() - started_at,
                failure_kind=FailureKind.CANCELLED,
                termination_confirmed=True,
            )

        platform_options: dict[str, object]
        if os.name == "nt":
            platform_options = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            }
        else:
            platform_options = {"start_new_session": True}

        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                env=spec.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                **platform_options,
            )
        except OSError as error:
            return ProcessResult(
                exit_code=None,
                stdout="",
                stderr=_bound_output(str(error), spec.output_limit),
                duration_seconds=monotonic() - started_at,
                failure_kind=FailureKind.SPAWN_ERROR,
                termination_confirmed=True,
            )

        failure_kind = None
        termination_confirmed = True
        while True:
            remaining = spec.timeout_seconds - (monotonic() - started_at)
            cancelled = cancellation_event.is_set()
            if cancelled or remaining <= 0:
                # A completed process wins a race with cancellation or the deadline.
                if process.poll() is not None:
                    try:
                        stdout_bytes, stderr_bytes = process.communicate(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        pass

                failure_kind = FailureKind.CANCELLED if cancelled else FailureKind.TIMED_OUT
                termination_confirmed = _terminate(process, spec.termination_grace_seconds)
                try:
                    stdout_bytes, stderr_bytes = process.communicate(
                        timeout=max(spec.termination_grace_seconds, 0.1),
                    )
                except subprocess.TimeoutExpired as error:
                    stdout_bytes = error.output or b""
                    stderr_bytes = error.stderr or b""
                    termination_confirmed = False
                    if os.name == "posix":
                        process.stdout.close()
                        process.stderr.close()
                    # Windows communicate readers own the pipes until EOF; close may block.
                if not termination_confirmed:
                    failure_kind = FailureKind.TERMINATION_UNKNOWN
                break

            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                # communicate retains cumulative bytes; retrying must not append them.
                pass

        if failure_kind is None and process.returncode != 0:
            failure_kind = FailureKind.NONZERO_EXIT
        return ProcessResult(
            exit_code=process.returncode if termination_confirmed else None,
            stdout=_bound_output(
                stdout_bytes.decode("utf-8", errors="replace"),
                spec.output_limit,
            ),
            stderr=_bound_output(
                stderr_bytes.decode("utf-8", errors="replace"),
                spec.output_limit,
            ),
            duration_seconds=monotonic() - started_at,
            failure_kind=failure_kind,
            termination_confirmed=termination_confirmed,
        )


def _termination_confirmed(process: subprocess.Popen) -> bool:
    if process.poll() is None:
        return False
    if os.name == "posix":
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False
    return True


def _wait_for_termination(process: subprocess.Popen, timeout: float) -> bool:
    deadline = monotonic() + timeout
    while True:
        if _termination_confirmed(process):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _terminate(process: subprocess.Popen, grace: float) -> bool:
    if _termination_confirmed(process):
        return True
    for force, timeout in ((False, grace), (True, max(grace, 0.1))):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                process.kill()
            else:
                process.terminate()
        except OSError:
            # Even ESRCH is not proof: confirm the parent and original group are gone.
            pass
        if _wait_for_termination(process, timeout):
            return True
    return False


def _bound_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:limit]

    content_limit = limit - len(_TRUNCATION_MARKER)
    head_length = int(content_limit * 0.6)
    tail_length = content_limit - head_length
    return text[:head_length] + _TRUNCATION_MARKER + text[-tail_length:]
