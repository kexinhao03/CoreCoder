"""Supervised, paired production-Runtime reliability experiment (no model calls)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 'reliagent.comparison.v1'
CONFIGS = {
    'A': {'retry': False, 'recovery': False, 'max_attempts': 1},
    'B': {'retry': True, 'recovery': False, 'max_attempts': 3},
    'C': {'retry': False, 'recovery': True, 'max_attempts': 1},
    'D': {'retry': True, 'recovery': True, 'max_attempts': 3},
}
FAULT_POINTS = ('before_call_persist', 'after_call_persist_before_start', 'during_tool_execution',
                'after_effect_before_result_persist', 'after_result_before_next_inference')
ROOT = Path(__file__).resolve().parents[2]


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name == 'posix':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def append_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(value, sort_keys=True) + '\n').encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def read_lines(path: Path):
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line]
    except (OSError, ValueError):
        return None


def source_fingerprint() -> dict:
    def git(*args):
        result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=False)
        return result.stdout
    source = hashlib.sha256()
    # Include new evaluation modules and dirty tracked implementation, not credentials.
    for path in sorted((ROOT / 'corecoder').rglob('*.py')):
        source.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes())
    return {'head': git('rev-parse', 'HEAD').decode().strip(),
            'tracked_diff_sha256': hashlib.sha256(git('diff', 'HEAD', '--', 'corecoder')).hexdigest(),
            'python_source_sha256': source.hexdigest(), 'python': sys.version,
            'platform': platform.platform(), 'executable': sys.executable}


def build_matrix(repetitions: int = 10, seed: int = 1729):
    if repetitions < 1:
        raise ValueError('repetitions must be positive')
    cells, skipped = [], []
    for workload in ('opaque', 'receipt'):
        cells.extend((workload, case, 'none', 'none') for case in ('normal', 'retry_only'))
        for point in FAULT_POINTS:
            for fault in ('exception', 'child_kill', 'runtime_kill'):
                reason = None
                if fault == 'child_kill' and point != 'during_tool_execution':
                    reason = 'no live child at this boundary'
                elif fault.endswith('kill') and os.name != 'posix':
                    reason = 'POSIX SIGKILL unavailable on this platform'
                if reason:
                    skipped.extend({'config_id': config, 'workload': workload, 'fault_point': point,
                                    'fault_type': fault, 'reason': reason} for config in CONFIGS)
                else:
                    cells.append((workload, 'fault', point, fault))
    rng = random.Random(seed)
    blocks = [(cell, repetition) for repetition in range(repetitions) for cell in cells]
    rng.shuffle(blocks)
    rows = []
    for (workload, case, point, fault), repetition in blocks:
        configs = list(CONFIGS)
        rng.shuffle(configs)
        for config in configs:
            rows.append({'trial_id': f'{config}-{workload}-{case}-{point}-{fault}-{repetition}',
                         'layer': 'mechanism', 'config_id': config, 'workload': workload,
                         'case_id': case, 'repetition': repetition, 'fault_point': point, 'fault_type': fault})
    return rows, skipped


def observe_trial(path: Path) -> dict:
    """External oracle only: absence/corruption is unknown, never zero effects."""
    effects = read_lines(path / 'effects.jsonl')
    starts = read_lines(path / 'dispatch.jsonl')
    manifest = read_json(path / 'manifest.json', {})
    artifact = read_json(path / 'final-artifact.json')
    task_id = manifest.get('trial', {}).get('trial_id')
    effect_count = None if effects is None else len(effects)
    completed = (task_id is not None and artifact == {'task_id': task_id, 'verified_effects': 1}
                 and effect_count == 1 and effects[0] == {'task_id': task_id, 'operation': 'effect'})
    return {'task_completed': completed, 'effect_count': effect_count,
            'effect_observation_available': effects is not None,
            'unsafe_replay_count': None if starts is None else sum(bool(item.get('prior_unresolved')) for item in starts),
            'unsafe_replay_observation_available': starts is not None}


def _cleanup_children(path: Path):
    # Children self-register before work and record exit in finally. Retired IDs are
    # never signalled again on resume, preventing later cleanup from using stale PIDs.
    children = read_lines(path / 'children.jsonl') or []
    retired = {item['pid'] for item in read_lines(path / 'child-exits.jsonl') or []}
    retired.update(item['pid'] for item in read_lines(path / 'child-cleanup.jsonl') or [])
    for child in children:
        if child['pid'] in retired:
            continue
        try:
            if os.name == 'posix' and os.getpgid(child['pid']) == child['pid']:
                os.killpg(child['pid'], signal.SIGKILL)
        except ProcessLookupError:
            pass
        append_json(path / 'child-cleanup.jsonl', {'pid': child['pid'], 'token': child.get('token')})


def _phase(path: Path, trial: dict, phase: str, deadline: float) -> dict:
    command = [sys.executable, '-m', 'corecoder.evals.comparison_worker', str(path), phase]
    result = {'exit_code': None, 'fault_triggered': False, 'timed_out': False, 'interrupted_at': None}
    with (path / f'{phase}.stdout').open('w') as stdout, (path / f'{phase}.stderr').open('w') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr, start_new_session=(os.name == 'posix'))
        append_json(path / 'observer.jsonl', {'event': 'worker_started', 'phase': phase, 'pid': process.pid,
                                           'monotonic': time.monotonic()})
        delivered = False
        try:
            while process.poll() is None:
                if phase == 'initial' and not delivered:
                    handshake = read_json(path / 'handshake.json')
                    if handshake:
                        if handshake['boundary'] != trial['fault_point']:
                            raise ValueError('unexpected boundary handshake')
                        append_json(path / 'observer.jsonl', {'event': 'boundary_acknowledged', **handshake})
                        result['interrupted_at'] = time.monotonic()
                        target = process.pid if trial['fault_type'] == 'runtime_kill' else handshake['pid']
                        if trial['fault_type'].endswith('kill'):
                            if trial['fault_type'] == 'child_kill' and handshake['role'] != 'child':
                                raise ValueError('child kill requested without a child')
                            os.kill(target, signal.SIGKILL)
                        else:
                            atomic_json(path / 'fault-response.json', {'action': 'raise_exception'})
                        append_json(path / 'observer.jsonl', {'event': 'fault_delivered', 'fault_type': trial['fault_type'],
                                                           'target_pid': target, 'monotonic': result['interrupted_at']})
                        delivered = True
                if time.monotonic() >= deadline:
                    result['timed_out'] = True
                    process.kill()
                    break
                time.sleep(0.005)  # Poll a durable handshake, never use elapsed sleep as fault proof.
            result['exit_code'] = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            _cleanup_children(path)
    evidence = read_json(path / 'worker-initial.json', {})
    if delivered:
        if trial['fault_type'] == 'runtime_kill':
            result['fault_triggered'] = result['exit_code'] == -signal.SIGKILL
        elif trial['fault_type'] == 'child_kill':
            result['fault_triggered'] = evidence.get('child_exit_code') == -signal.SIGKILL
        else:
            result['fault_triggered'] = bool(read_json(path / 'exception-raised.json'))
    append_json(path / 'observer.jsonl', {'event': 'worker_exited', 'phase': phase, **result})
    return result


def run_trial(trial: dict, output: Path, deadline_seconds: float = 15) -> dict:
    path = Path(output).resolve() / trial['trial_id']
    path.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + deadline_seconds
    row = {**trial, 'fault_triggered': False, 'valid': True,
           'outcome': 'error', 'task_completed': False, 'human_intervention': False,
           'human_assisted_completion': False, 'effect_count': None, 'unsafe_replay_count': None,
           'elapsed_seconds': 0.0, 'recovery_seconds': None, 'recovery_censored': False,
           'interruption_to_outcome_seconds': None, 'sql_write_statements': 0, 'sql_write_transactions': 0,
           'error': None, 'artifact_dir': str(path), 'runtime_status': None,
           'boundary_label': ('after_result_before_fixed_task_continuation'
                              if trial['fault_point'] == 'after_result_before_next_inference' else trial['fault_point']),
           'initial_exit_code': None, 'child_exit_code': None, 'warmup': False}
    atomic_json(path / 'manifest.json', {'schema': SCHEMA, 'trial': trial, 'configuration': CONFIGS[trial['config_id']],
                                      'deadline_seconds': deadline_seconds, 'deadline_monotonic': deadline, 'source': source_fingerprint(),
                                      'task': ['safe preparation', 'one external effect', 'independent final verification'],
                                      'steps': [{'tool_name': name, 'argv': [sys.executable,
                                          str(ROOT / 'corecoder/evals/comparison_worker.py'), str(path), 'tool', name]}
                                          for name in ('prepare', 'effect', 'verify')]})
    for name in ('effects.jsonl', 'dispatch.jsonl', 'observer.jsonl', 'sql.jsonl', 'children.jsonl'):
        with (path / name).open('w') as stream:
            stream.flush()
            os.fsync(stream.fileno())
    initial = None
    try:
        initial = _phase(path, trial, 'initial', deadline)
        row['initial_exit_code'] = initial['exit_code']
        row['fault_triggered'] = initial['fault_triggered'] if trial['fault_type'] != 'none' else (
            trial['case_id'] == 'retry_only' and bool(read_json(path / 'transient-failure.json')))
        worker = read_json(path / 'worker-initial.json', {})
        row['child_exit_code'] = worker.get('child_exit_code')
        row['runtime_status'] = worker.get('runtime_status')
        row['error'] = worker.get('error')
        if trial['fault_type'] != 'none' and not row['fault_triggered']:
            row.update(valid=False, fault_missed_reason='target boundary/action not observed')
        if initial['timed_out']:
            row.update(outcome='deadline', error='trial deadline exceeded', recovery_censored=True)
        elif trial['fault_type'] != 'none' and not row['fault_triggered']:
            row.update(outcome='missed_fault', valid=False, error='target boundary/action not observed')
        else:
            if CONFIGS[trial['config_id']]['recovery'] and row['fault_triggered'] and trial['fault_type'] != 'none':
                launch = time.monotonic()
                resumed = _phase(path, trial, 'resume', deadline)
                worker = read_json(path / 'worker-resume.json', {})
                row['runtime_status'] = worker.get('runtime_status', row['runtime_status'])
                row['human_intervention'] = worker.get('human_intervention', False)
                row['recovery_seconds'] = time.monotonic() - launch
                row['interruption_to_outcome_seconds'] = time.monotonic() - initial['interrupted_at']
                row['recovery_censored'] = resumed['timed_out'] or (not worker.get('human_intervention')
                    and not observe_trial(path)['task_completed'])
                if worker.get('error'):
                    row['error'] = worker['error']
                if resumed['timed_out']:
                    row['error'] = 'trial deadline exceeded during recovery'
            row['outcome'] = 'human_required' if row['human_intervention'] else 'incomplete'
    except Exception as error:  # noqa: BLE001 - every failed trial must retain evidence
        row.update(error=f'{type(error).__name__}: {error}', outcome='error', valid=False)
    row.update(observe_trial(path))
    if (path / 'runtime.sqlite3').exists():
        from corecoder.runtime.store import SQLiteStore
        from corecoder.runtime.tracing import TraceService
        try:
            store = SQLiteStore(path / 'runtime.sqlite3')
            row['runtime_status'] = store.get_run(trial['trial_id']).status.value
            atomic_json(path / 'trace.json', TraceService(store).export_run(trial['trial_id']))
        except (sqlite3.Error, KeyError) as error:
            row['trace_error'] = f'{type(error).__name__}: {error}'
    if row['task_completed'] and row['outcome'] not in {'missed_fault', 'error', 'deadline'}:
        row['outcome'] = 'completed'
    sql = read_lines(path / 'sql.jsonl')
    if sql is None:
        row['sql_write_statements'] = row['sql_write_transactions'] = None
    else:
        row['sql_write_statements'] = sum(item.get('statements', 0) for item in sql)
        row['sql_write_transactions'] = sum(item.get('transactions', 0) for item in sql)
    row['elapsed_seconds'] = time.monotonic() - started
    atomic_json(path / 'result.json', row)
    return row


def write_bundle(output: Path, bundle: dict) -> None:
    from .comparison_report import render_report
    output = Path(output)
    atomic_json(output / 'comparison-results.json', bundle)
    report_path = output / 'comparison-report.md'
    temporary = report_path.with_suffix('.tmp')
    temporary.write_text(render_report(bundle))
    os.replace(temporary, report_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=10)
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--deadline-seconds', type=float, default=15)
    args = parser.parse_args()
    trials, skipped = build_matrix(args.repetitions, args.seed)
    bundle = {'schema': SCHEMA, 'layer': 'mechanism', 'configurations': CONFIGS, 'repetitions': args.repetitions,
              'seed': args.seed, 'smoke': args.repetitions < 10, 'source': source_fingerprint(),
              'environment': {'platform': platform.platform(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version},
              'skipped_matrix': skipped, 'trials': [], 'assigned_trials': len(trials),
              'sql_measurement': 'Actual traced DML/DDL statements and successfully committed write transactions; all phases.',
              'timing': 'End-to-end including interpreter startup and fsynced measurement overhead; no warmups.',
              'schedule': trials}
    write_bundle(args.output, bundle)
    for trial in trials:
        bundle['trials'].append(run_trial(trial, args.output, args.deadline_seconds))
        write_bundle(args.output, bundle)
        print(f"{len(bundle['trials'])}/{len(trials)} {trial['trial_id']}: {bundle['trials'][-1]['outcome']}", flush=True)


if __name__ == '__main__':
    main()
