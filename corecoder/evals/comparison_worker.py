"""Evaluation adapters instrument production boundaries; all execution stays in Runtime."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from corecoder.evals.comparison import append_json, atomic_json, read_json, read_lines
from corecoder.reliagent import ReliAgentRuntime, TaskStep
from corecoder.runtime import ApprovalDecision, ApprovalStatus, RunStatus, RuntimeExecutor, SQLiteStore, ToolCallStatus
from corecoder.runtime.policies import FailureKind, ToolPolicy, ToolPolicyRegistry
from corecoder.runtime.recovery import RecoveryKind, RecoveryManager, RecoveryResolution
from corecoder.runtime.state import ExecutionKind, RiskLevel


class InjectedFault(RuntimeError):
    pass


def check_tool_owner(path: Path, manifest: dict):
    expected_parent = os.environ.get('RELIAGENT_EVAL_WORKER_PID')
    parent_lost = expected_parent is None or os.getppid() != int(expected_parent)
    deadline_expired = time.monotonic() >= manifest['deadline_monotonic']
    if parent_lost or deadline_expired:
        append_json(path / 'child-aborts.jsonl', {'pid': os.getpid(), 'parent_lost': parent_lost,
                                              'deadline_expired': deadline_expired})
        raise SystemExit(125)


def boundary(path: Path, point: str, *, role='runtime', enabled=True):
    manifest = read_json(path / 'manifest.json')
    trial = manifest['trial']
    if not enabled or trial['fault_point'] != point or trial['fault_type'] == 'none':
        return
    if role == 'child':
        check_tool_owner(path, manifest)
    atomic_json(path / 'handshake.json', {'boundary': point, 'pid': os.getpid(), 'role': role,
                                        'monotonic': time.monotonic()})
    # Supervisor either kills this process or explicitly authorizes a raised exception.
    while not (path / 'fault-response.json').exists():
        if role == 'child':
            check_tool_owner(path, manifest)
        time.sleep(0.005)
    atomic_json(path / 'exception-raised.json', {'boundary': point, 'pid': os.getpid(), 'role': role})
    raise InjectedFault(f'injected exception at {point}')


class MeasuredConnection(sqlite3.Connection):
    """Trace real writes; record transactions only after SQLite confirms commit."""
    pending_writes = 0
    autocommit_writes = 0
    ledger: Path

    def trace(self, statement):
        keyword = statement.lstrip().split(maxsplit=1)[0].upper() if statement.strip() else ''
        if keyword in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER', 'DROP'}:
            append_json(self.ledger, {'pid': os.getpid(), 'statements': 1, 'kind': keyword})
            self.pending_writes += 1
            if not self.in_transaction:
                self.autocommit_writes += 1

    def _record_autocommit(self):
        if not self.in_transaction and self.autocommit_writes:
            append_json(self.ledger, {'pid': os.getpid(), 'transactions': self.autocommit_writes})
            self.pending_writes = 0
            self.autocommit_writes = 0

    def execute(self, *args, **kwargs):
        result = super().execute(*args, **kwargs)
        self._record_autocommit()
        return result

    def executescript(self, *args, **kwargs):
        result = super().executescript(*args, **kwargs)
        self._record_autocommit()
        return result

    def commit(self):
        writes = self.pending_writes
        super().commit()
        if writes:
            append_json(self.ledger, {'pid': os.getpid(), 'transactions': 1})
        self.pending_writes = self.autocommit_writes = 0

    def rollback(self):
        super().rollback()
        self.pending_writes = self.autocommit_writes = 0


class EvaluationStore(SQLiteStore):
    def __init__(self, path: Path, enabled: bool):
        self.directory = path
        self.enabled = enabled
        super().__init__(path / 'runtime.sqlite3')

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5, factory=MeasuredConnection)
        connection.ledger = self.directory / 'sql.jsonl'
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 5000')
        connection.set_trace_callback(connection.trace)
        return connection

    def create_tool_call(self, **kwargs):
        if kwargs['tool_name'] == 'effect':
            boundary(self.directory, 'before_call_persist', enabled=self.enabled)
        return super().create_tool_call(**kwargs)

    def transition_tool_call(self, call_id, status, event_type, **kwargs):
        call = super().transition_tool_call(call_id, status, event_type, **kwargs)
        if call.tool_name == 'effect' and status is ToolCallStatus.SUCCEEDED:
            boundary(self.directory, 'after_result_before_next_inference', enabled=self.enabled)
        return call


class EvaluationExecutor(RuntimeExecutor):
    def __init__(self, store, registry, enabled):
        self.directory = store.directory
        self.enabled = enabled
        super().__init__(store, registry, fault_injector=self)

    def _start_call(self, call):
        if call.tool_name == 'effect':
            boundary(self.directory, 'after_call_persist_before_start', enabled=self.enabled)
            prior = [other.id for other in self._store.list_tool_calls(call.run_id)
                     if other.tool_name == 'effect'
                     and other.status in {ToolCallStatus.RUNNING, ToolCallStatus.INTERRUPTED}]
            append_json(self.directory / 'dispatch.jsonl', {'call_id': call.id, 'logical_operation': 'effect',
                        'prior_unresolved': prior, 'pid': os.getpid()})
        return super()._start_call(call)

    def checkpoint(self, call, checkpoint):
        if call.tool_name == 'effect' and checkpoint == 'experiment_after_effect':
            boundary(self.directory, 'after_effect_before_result_persist', enabled=self.enabled)


def _registry(config):
    safe = ToolPolicy(RiskLevel.READ_ONLY, ExecutionKind.SUBPROCESS, 10, config['max_attempts'], True,
                      config['retry'], frozenset({FailureKind.NONZERO_EXIT}), 2000)
    effect = ToolPolicy(RiskLevel.EXTERNAL_EFFECT, ExecutionKind.SUBPROCESS, 10, 1, False, False, frozenset(), 2000)
    verify = ToolPolicy(RiskLevel.MUTATING, ExecutionKind.SUBPROCESS, 10, 1, False, False, frozenset(), 2000)
    return ToolPolicyRegistry({'prepare': safe, 'effect': effect, 'verify': verify})


def _receipt_valid(path, trial):
    return read_json(path / 'receipt.json') == {'task_id': trial['trial_id'], 'operation': 'effect', 'completed': True}


def execute(path: Path, phase: str):
    os.environ['RELIAGENT_EVAL_WORKER_PID'] = str(os.getpid())
    manifest = read_json(path / 'manifest.json')
    trial = manifest['trial']
    store = EvaluationStore(path, phase == 'initial')
    store.initialize()
    registry = _registry(manifest['configuration'])
    executor = EvaluationExecutor(store, registry, phase == 'initial')
    runtime = ReliAgentRuntime(store, executor)
    # Restore the exact evaluator-owned initial task, including original argv.
    steps = tuple(TaskStep(step['tool_name'], tuple(step['argv'])) for step in manifest['steps'])
    run_id = trial['trial_id']
    if phase == 'initial':
        runtime.create_task(goal='prepare, perform one effect, verify final artifact', workspace=path, steps=steps, run_id=run_id)
    else:
        manager = RecoveryManager(store, registry)
        for candidate in manager.scan(run_id):
            if candidate.kind is RecoveryKind.RETRY_ALLOWED:
                executor.execute_recovery_subprocess(manager.resume_retry(candidate).id)
            elif candidate.kind is RecoveryKind.HUMAN_REQUIRED:
                if (candidate.call.tool_name == 'effect' and trial['workload'] == 'receipt'
                        and candidate.call.status is ToolCallStatus.INTERRUPTED and _receipt_valid(path, trial)):
                    append_json(path / 'observer.jsonl', {'event': 'receipt_reconciled', 'call_id': candidate.call.id,
                                                       'receipt': read_json(path / 'receipt.json')})
                    manager.reconcile(candidate.call.id, RecoveryResolution.CONFIRMED_SUCCEEDED)
                else:
                    return {'runtime_status': store.get_run(run_id).status.value, 'human_intervention': True,
                            'recovery_reason': candidate.reason}
    # Routine consent is identical in A-D and distinct from crash reconciliation.
    for _ in range(5):
        result = runtime.resume(run_id, steps=steps)
        if result.run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.RECOVERABLE}:
            break
        approvals = [store.get_approval_for_tool_call(call.id) for call in store.list_tool_calls(run_id)]
        pending = [approval for approval in approvals if approval and approval.status is ApprovalStatus.PENDING]
        if not pending:
            break
        for approval in pending:
            store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
            append_json(path / 'observer.jsonl', {'event': 'routine_approval', 'approval_id': approval.id})
    child_exit = None
    for call in store.list_tool_calls(run_id):
        if call.tool_name == 'effect':
            for event in store.list_events(run_id):
                if event.type == 'tool.failed' and event.payload.get('tool_call_id') == call.id:
                    child_exit = event.payload.get('exit_code')
    return {'runtime_status': store.get_run(run_id).status.value, 'human_intervention': False, 'child_exit_code': child_exit}


def tool(path: Path, name: str):
    manifest = read_json(path / 'manifest.json')
    trial = manifest['trial']
    check_tool_owner(path, manifest)
    append_json(path / 'children.jsonl', {'pid': os.getpid(), 'tool': name, 'token': os.environ.get('RELIAGENT_PROCESS_TOKEN')})
    # Registration itself may have been delayed beyond supervisor cleanup.
    check_tool_owner(path, manifest)
    if name == 'prepare':
        attempts = read_lines(path / 'preparation.jsonl') or []
        append_json(path / 'preparation.jsonl', {'attempt': len(attempts) + 1})
        if trial['case_id'] == 'retry_only' and not attempts:
            atomic_json(path / 'transient-failure.json', {'tool': 'prepare', 'attempt': 1, 'exit_code': 7})
            print('one evaluator-scheduled transient failure', file=sys.stderr)
            return 7
    elif name == 'effect':
        boundary(path, 'during_tool_execution', role='child')
        check_tool_owner(path, manifest)
        append_json(path / 'effects.jsonl', {'task_id': trial['trial_id'], 'operation': 'effect'})
        if trial['workload'] == 'receipt':
            atomic_json(path / 'receipt.json', {'task_id': trial['trial_id'], 'operation': 'effect', 'completed': True})
    elif name == 'verify':
        records = read_lines(path / 'effects.jsonl')
        if records != [{'task_id': trial['trial_id'], 'operation': 'effect'}]:
            print('independent effect validation failed', file=sys.stderr)
            return 8
        check_tool_owner(path, manifest)
        atomic_json(path / 'final-artifact.json', {'task_id': trial['trial_id'], 'verified_effects': 1})
    print(json.dumps({'tool': name, 'result': 'completed'}))
    return 0


def main():
    path, phase = Path(sys.argv[1]).resolve(), sys.argv[2]
    if phase == 'tool':
        try:
            return tool(path, sys.argv[3])
        finally:
            append_json(path / 'child-exits.jsonl', {'pid': os.getpid()})
    try:
        result = execute(path, phase)
    except Exception as error:
        atomic_json(path / f'worker-{phase}.json', {'error': f'{type(error).__name__}: {error}'})
        raise
    atomic_json(path / f'worker-{phase}.json', result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
