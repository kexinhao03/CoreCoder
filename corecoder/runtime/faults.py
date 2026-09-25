"""Durable runtime fault-injection vocabulary."""

from enum import Enum


class FaultPlanState(str, Enum):
    ARMED = "armed"
    TRIGGERED = "triggered"


class FaultCheckpoint(str, Enum):
    ENVIRONMENT_AFTER_START = "environment_after_start"
    EXPERIMENT_AFTER_EFFECT = "experiment_after_effect"
