"""Evaluation adapter that uses only the public ML Workflow surface."""

from __future__ import annotations

import json

from corecoder.evals.models import RuntimeExecution
from corecoder.runtime import ApprovalDecision, RunStatus
from corecoder.workflows.ml_experiment import (
    MLExperimentReportService,
    MLExperimentWorkflow,
    MLReconciliationService,
    ReconciliationDecision,
)


class _InjectedExit(BaseException):
    pass


def _exit_process(code: int) -> None:
    raise _InjectedExit(code)


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


def _crash_environment(workspace):
    workflow = MLExperimentWorkflow(workspace, exit_process=_exit_process)
    try:
        workflow.create(fault="environment_after_start")
    except _InjectedExit:
        pass
    [run] = workflow.store.list_runs()
    return workflow, run.id


def _crash_experiment(workspace):
    workflow = MLExperimentWorkflow(workspace, exit_process=_exit_process)
    run = workflow.create(fault="experiment_after_effect")
    _approve_experiment(workflow, run.id)
    try:
        workflow.resume(run.id)
    except _InjectedExit:
        pass
    return workflow, run.id


def execute_ml_scenario(case, config, workspace) -> RuntimeExecution:
    scenario = case.scenario
    report_refused = False
    reconciliation_refused = False
    recovery_succeeded = None
    fault_expected = case.fault_schedule.point in {
        "environment_after_start",
        "experiment_after_effect",
    }
    if scenario == "ml_normal_success":
        workflow = MLExperimentWorkflow(workspace)
        run_id = _complete_normal(workflow)
    elif scenario == "ml_environment_recovery":
        workflow, run_id = _crash_environment(workspace)
        workflow = MLExperimentWorkflow(workspace)
        if config.id == "full":
            workflow.resume(run_id)
            _approve_experiment(workflow, run_id)
            workflow.resume(run_id)
            recovery_succeeded = True
        elif config.id == "no_recovery":
            workflow.recovery.scan(run_id=run_id)
            recovery_succeeded = False
        else:
            recovery_succeeded = False
    elif scenario in {
        "ml_experiment_after_effect",
        "ml_valid_reconciliation",
        "ml_damaged_reconciliation",
    }:
        workflow, run_id = _crash_experiment(workspace)
        workflow = MLExperimentWorkflow(workspace)
        if config.id != "baseline":
            blocked = workflow.resume(run_id)
            call_id = blocked.pending_reconciliation_tool_call_id
            if scenario == "ml_valid_reconciliation" and config.id == "full":
                MLReconciliationService(workflow).reconcile(
                    call_id, ReconciliationDecision.COMPLETED
                )
                workflow.resume(run_id)
                recovery_succeeded = True
            elif scenario == "ml_damaged_reconciliation" and config.id == "full":
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
        workflow = MLExperimentWorkflow(workspace)
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
        workflow = MLExperimentWorkflow(workspace)
        run = workflow.create()
        run_id = run.id
        call = workflow.store.list_tool_calls(run_id)[1]
        approval = workflow.store.get_approval_for_tool_call(call.id)
        workflow.resolve_approval(approval.id, ApprovalDecision.DENY)
    elif scenario == "ml_report_tamper":
        workflow = MLExperimentWorkflow(workspace)
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

    run = workflow.store.get_run(run_id)
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
            if config.id == "no_recovery"
            else RunStatus.RUNNING
        )
    elif scenario == "ml_valid_reconciliation" and config.id == "full":
        expected_status = RunStatus.SUCCEEDED
    elif scenario.startswith(("ml_experiment", "ml_valid", "ml_damaged")):
        expected_status = RunStatus.RECOVERABLE if config.id != "baseline" else RunStatus.RUNNING
    scenario_contract = True
    if scenario == "ml_damaged_reconciliation" and config.id == "full":
        scenario_contract = reconciliation_refused
    if scenario == "ml_repeated_resume":
        scenario_contract = repeated_resume_inert
    if scenario == "ml_report_tamper":
        scenario_contract = report_refused
    assertions = {
        "effect_not_duplicated": len(effects) <= 1,
        "expected_status": run.status is expected_status,
        "fault_plan_observed": fault_triggered is fault_expected,
        "runtime_invoked": bool(workflow.store.list_tool_calls(run_id)),
        "scenario_contract": scenario_contract,
    }
    return RuntimeExecution(
        task_succeeded=run.status is RunStatus.SUCCEEDED,
        recovery_succeeded=recovery_succeeded,
        store=workflow.store,
        run_id=run_id,
        effect_observations={
            "duplicate_effects": max(0, len(effects) - 1),
            "effect_count": len(effects),
            "fault_triggered": fault_triggered,
        },
        assertions=assertions,
    )
