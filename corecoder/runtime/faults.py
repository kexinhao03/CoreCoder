"""Durable runtime fault-injection vocabulary."""

import os
from collections.abc import Callable
from enum import Enum
from typing import NoReturn

from .models import ToolCallRecord
from .store import SQLiteStore


class FaultPlanState(str, Enum):
    ARMED = "armed"
    TRIGGERED = "triggered"


class FaultCheckpoint(str, Enum):
    ENVIRONMENT_AFTER_START = "environment_after_start"
    EXPERIMENT_AFTER_EFFECT = "experiment_after_effect"


class RuntimeFaultInjector:
    """Trigger an explicitly persisted test/demo fault plan at a safe checkpoint."""

    def __init__(
        self,
        store: SQLiteStore,
        exit_process: Callable[[int], NoReturn] = os._exit,
    ) -> None:
        self._store = store
        self._exit_process = exit_process

    def checkpoint(self, call: ToolCallRecord, checkpoint: str) -> None:
        checkpoint_value = FaultCheckpoint(checkpoint).value
        if call.step_id is None:
            return
        step = next(
            (
                candidate
                for candidate in self._store.list_steps(call.run_id)
                if candidate.id == call.step_id
            ),
            None,
        )
        if step is None or step.step_key is None:
            return
        plan = self._store.trigger_fault_plan(
            call.run_id, step.step_key, checkpoint_value
        )
        if plan is not None:
            self._exit_process(plan.exit_code)
