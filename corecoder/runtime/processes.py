"""Bounded subprocess execution results."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from corecoder.runtime.policies import FailureKind

_TRUNCATION_MARKER = "\n... output truncated ...\n"


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
        del cancellation_event  # Timeout and cancellation handling are introduced in Task 5.
        started_at = time.monotonic()
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
                stderr=str(error),
                duration_seconds=time.monotonic() - started_at,
                failure_kind=FailureKind.SPAWN_ERROR,
                termination_confirmed=True,
            )

        stdout_bytes, stderr_bytes = process.communicate()
        failure_kind = None if process.returncode == 0 else FailureKind.NONZERO_EXIT
        return ProcessResult(
            exit_code=process.returncode,
            stdout=_bound_output(
                stdout_bytes.decode("utf-8", errors="replace"),
                spec.output_limit,
            ),
            stderr=_bound_output(
                stderr_bytes.decode("utf-8", errors="replace"),
                spec.output_limit,
            ),
            duration_seconds=time.monotonic() - started_at,
            failure_kind=failure_kind,
            termination_confirmed=True,
        )


def _bound_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:limit]

    content_limit = limit - len(_TRUNCATION_MARKER)
    head_length = int(content_limit * 0.6)
    tail_length = content_limit - head_length
    return text[:head_length] + _TRUNCATION_MARKER + text[-tail_length:]
