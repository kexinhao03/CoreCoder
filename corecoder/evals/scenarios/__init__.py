"""Evaluation scenario dispatch preserving the Phase 3 public import."""

from .ml_experiment import execute_ml_scenario
from .phase3 import execute_scenario as execute_phase3_scenario


def execute_scenario(case, config, workspace, injector):
    if case.scenario.startswith("ml_"):
        return execute_ml_scenario(case, config, workspace)
    return execute_phase3_scenario(case, config, workspace, injector)


__all__ = ["execute_scenario"]
