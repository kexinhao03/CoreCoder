"""Evaluation adapter that uses only the public ML Workflow surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from corecoder.evals.models import RuntimeExecution
from corecoder.runtime import (
    ApprovalDecision,
    RunStatus,
)
from corecoder.workflows.ml_experiment import (
    MLExperimentReportService,
    MLExperimentWorkflow,
    MLReconciliationService,
    ReconciliationDecision,
)


def _workflow(workspace, config) -> MLExperimentWorkflow:
    return MLExperimentWorkflow(
        workspace,
        max_attempts=config.max_attempts,
        auto_retry=config.auto_retry,
        recovery_enabled=config.recovery_enabled,
    )


def _approve_experiment(workflow: MLExperimentWorkflow, run_id: str) -> None:
    approval_id = workflow.status(run_id).pending_approval_id
    if approval_id is None:
        raise ValueError("ML experiment is not waiting for approval")
    workflow.resolve_approval(approval_id, ApprovalDecision.ALLOW_ONCE)


def _complete_normal(workflow: MLExperimentWorkflow) -> str:
    run = workflow.create()
    _approve_experiment(workflow, run.id)
    workflow.resume(run.id)
    return run.id


def _cli_process(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "corecoder.reliagent_cli", *arguments],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )


def _config_arguments(config) -> tuple[str, ...]:
    arguments = ["--max-attempts", str(config.max_attempts)]
    if not config.auto_retry:
        arguments.append("--no-auto-retry")
    if not config.recovery_enabled:
        arguments.append("--no-recovery")
    return tuple(arguments)


def _crash_environment(workspace, config):
    process = _cli_process(
        "workflow", "ml", "start", "--workspace", str(workspace),
        "--inject-process-loss", "environment_after_start",
        *_config_arguments(config),
    )
    if process.returncode != 86:
        raise RuntimeError(f"environment fault process returned {process.returncode}")
    [created] = [json.loads(line) for line in process.stdout.splitlines()]
    return _workflow(workspace, config), created["run_id"], process.returncode


def _crash_experiment(workspace, config):
    started = _cli_process(
        "workflow", "ml", "start", "--workspace", str(workspace),
        "--inject-process-loss", "experiment_after_effect",
        *_config_arguments(config),
    )
    if started.returncode != 0:
        raise RuntimeError(f"experiment setup process returned {started.returncode}")
    created, _waiting = [json.loads(line) for line in started.stdout.splitlines()]
    workflow = _workflow(workspace, config)
    _approve_experiment(workflow, created["run_id"])
    process = _cli_process(
        "workflow", "ml", "resume", created["run_id"],
        "--workspace", str(workspace),
        *_config_arguments(config),
    )
    if process.returncode != 87:
        raise RuntimeError(f"experiment fault process returned {process.returncode}")
    return _workflow(workspace, config), created["run_id"], process.returncode


def execute_ml_scenario(case, config, workspace) -> RuntimeExecution:
    scenario = case.scenario
    report_refused = False
    reconciliation_refused = False
    recovery_succeeded = None
    process_exit_code = None
    fault_expected = case.fault_schedule.point in {
        "environment_after_start",
        "experiment_after_effect",
    }
    if scenario == "ml_normal_success":
        workflow = _workflow(workspace, config)
        run_id = _complete_normal(workflow)
    elif scenario == "ml_environment_recovery":
        workflow, run_id, process_exit_code = _crash_environment(workspace, config)
        workflow.recovery.scan(run_id=run_id)
        if config.id == "full":
            workflow.resume(run_id)
            _approve_experiment(workflow, run_id)
            workflow.resume(run_id)
            recovery_succeeded = True
        else:
            recovery_succeeded = False
    elif scenario in {
        "ml_experiment_after_effect",
        "ml_valid_reconciliation",
        "ml_damaged_reconciliation",
    }:
        workflow, run_id, process_exit_code = _crash_experiment(workspace, config)
        workflow.recovery.scan(run_id=run_id)
        if config.id == "full":
            blocked = workflow.resume(run_id)
            call_id = blocked.pending_reconciliation_tool_call_id
            if scenario == "ml_valid_reconciliation":
                MLReconciliationService(workflow).reconcile(
                    call_id, ReconciliationDecision.COMPLETED
                )
                workflow.resume(run_id)
                recovery_succeeded = True
            elif scenario == "ml_damaged_reconciliation":
                artifacts = workflow.definition(run_id).artifacts
                payload = json.loads(artifacts.metrics_file.read_text())
                payload["parameters"]["slope"] = 999.0
                artifacts.metrics_file.write_text(json.dumps(payload))
                outcome = MLReconciliationService(workflow).reconcile(
                    call_id, ReconciliationDecision.COMPLETED
                )
                reconciliation_refused = (
                    outcome.decision is ReconciliationDecision.UNRESOLVED
                )
                recovery_succeeded = False
            else:
                recovery_succeeded = False
        else:
            recovery_succeeded = False
    elif scenario == "ml_repeated_resume":
        workflow = _workflow(workspace, config)
        run_id = _complete_normal(workflow)
        before = (
            workflow.store.list_events(run_id),
            workflow.store.list_tool_calls(run_id),
            workflow.definition(run_id).artifacts.effects_file.read_bytes(),
        )
        workflow.resume(run_id)
        workflow.resume(run_id)
        repeated_resume_inert = before == (
            workflow.store.list_events(run_id),
            workflow.store.list_tool_calls(run_id),
            workflow.definition(run_id).artifacts.effects_file.read_bytes(),
        )
    elif scenario == "ml_approval_denial":
        workflow = _workflow(workspace, config)
        run = workflow.create()
        run_id = run.id
        call = workflow.store.list_tool_calls(run_id)[1]
        approval = workflow.store.get_approval_for_tool_call(call.id)
        workflow.resolve_approval(approval.id, ApprovalDecision.DENY)
    elif scenario == "ml_report_tamper":
        workflow = _workflow(workspace, config)
        run_id = _complete_normal(workflow)
        artifacts = workflow.definition(run_id).artifacts
        payload = json.loads(artifacts.metrics_file.read_text())
        payload["parameters"]["slope"] = 999.0
        artifacts.metrics_file.write_text(json.dumps(payload))
        try:
            MLExperimentReportService(workflow).build(run_id)
        except ValueError as error:
            report_refused = "Artifact Integrity Failure" in str(error)
    else:
        raise ValueError(f"unknown ML evaluation scenario: {scenario}")

    status = workflow.status(run_id)
    artifacts = workflow.definition(run_id).artifacts
    effects = (
        artifacts.effects_file.read_text().splitlines()
        if artifacts.effects_file.exists()
        else []
    )
    fault_triggered = any(plan.state == "triggered" for plan in workflow.store.list_fault_plans(run_id))
    expected_status = {
        "ml_normal_success": RunStatus.SUCCEEDED,
        "ml_repeated_resume": RunStatus.SUCCEEDED,
        "ml_approval_denial": RunStatus.FAILED,
        "ml_report_tamper": RunStatus.SUCCEEDED,
    }.get(scenario)
    if scenario == "ml_environment_recovery":
        expected_status = (
            RunStatus.SUCCEEDED
            if config.id == "full"
            else RunStatus.RECOVERABLE
        )
    elif scenario == "ml_valid_reconciliation" and config.id == "full":
        expected_status = RunStatus.SUCCEEDED
    elif scenario.startswith(("ml_experiment", "ml_valid", "ml_damaged")):
        expected_status = RunStatus.RECOVERABLE
    scenario_contract = True
    if scenario == "ml_damaged_reconciliation" and config.id == "full":
        scenario_contract = reconciliation_refused
    if scenario == "ml_repeated_resume":
        scenario_contract = repeated_resume_inert
    if scenario == "ml_report_tamper":
        scenario_contract = report_refused
    assertions = {
        "configuration_applied": (
            workflow.policies.resolve("ml_experiment.environment_check").max_attempts
            == config.max_attempts
            and workflow.policies.resolve("ml_experiment.environment_check").auto_retry
            is config.auto_retry
            and workflow.recovery_enabled is config.recovery_enabled
        ),
        "effect_not_duplicated": len(effects) <= 1,
        "expected_status": status.status == expected_status.value,
        "fault_plan_observed": fault_triggered is fault_expected,
        "runtime_invoked": bool(workflow.store.list_tool_calls(run_id)),
        "scenario_contract": scenario_contract,
    }
    if fault_expected:
        assertions["real_process_exit_observed"] = process_exit_code in {86, 87}
    return RuntimeExecution(
        task_succeeded=status.status == RunStatus.SUCCEEDED.value,
        recovery_succeeded=recovery_succeeded,
        store=workflow.store,
        run_id=run_id,
        effect_observations={
            "duplicate_effects": max(0, len(effects) - 1),
            "effect_count": len(effects),
            "fault_triggered": fault_triggered,
            "process_exit_code": process_exit_code,
        },
        assertions=assertions,
    )
