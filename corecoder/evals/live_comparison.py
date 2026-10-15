"""Supervised real-Agent comparison against an unmodified, frozen upstream tree."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

from .comparison import SCHEMA, append_json, atomic_json, read_json, read_lines, source_fingerprint, write_bundle

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name('live_comparison_worker.py')
UPSTREAM = '069948d180cb0ceb1a6d7aafeb0e141c166a009d'
CONFIGS = {'D_agent': {'recovery': True, 'auto_tool_retry': False, 'retry_not_exercised': True},
           'E': {'recovery': False, 'auto_tool_retry': False, 'pristine_upstream': UPSTREAM}}
GENERATION = {'temperature': 0, 'max_tokens': 1024, 'extra_body': {'think': False}}
POINTS = {'normal': 'none', 'after_effect': 'after_effect_before_result_persist',
          'after_result': 'after_result_before_next_inference',
          'after_accepted_result': 'after_accepted_tool_result_before_next_inference'}


def make_trial(config: str, case: str, repetition: int) -> dict:
    return {'trial_id': f'{config}-{case}-{repetition}', 'layer': 'live_agent', 'config_id': config,
            'case_id': case, 'workload': 'opaque', 'repetition': repetition, 'fault_point': POINTS[case],
            'fault_type': 'none' if case == 'normal' else 'runtime_kill',
            'boundary_label': ('durable_result' if config == 'D_agent' else 'volatile_result')
                if case == 'after_accepted_result' else POINTS[case]}


def build_matrix(repetitions=5, seed=1729):
    if repetitions < 1:
        raise ValueError('repetitions must be positive')
    rows, skipped = [], []
    for repetition in range(repetitions):
        for case in POINTS:
            for config in CONFIGS:
                trial = make_trial(config, case, repetition)
                reason = ('upstream has no durable result persistence boundary' if config == 'E' and case == 'after_result'
                          else 'POSIX SIGKILL unavailable' if case != 'normal' and os.name != 'posix' else None)
                if reason:
                    skipped.append({**trial, 'reason': reason})
                else:
                    rows.append(trial)
    random.Random(seed).shuffle(rows)
    return rows, skipped


def prepare_sources(path: Path) -> dict:
    path.mkdir(parents=True, exist_ok=False)
    current = path / 'durable'
    shutil.copytree(ROOT / 'corecoder', current / 'corecoder', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    upstream = path / 'upstream'
    upstream.mkdir()
    archive = subprocess.check_output(['git', 'archive', UPSTREAM], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(upstream, filter='data')
    result = {}
    for name, source in [('D_agent', current), ('E', upstream)]:
        result[name] = {'path': str(source.resolve()),
                        'agent_sha256': hashlib.sha256((source / 'corecoder/agent.py').read_bytes()).hexdigest()}
    result['D_agent']['fingerprint'] = source_fingerprint()
    result['E']['head'] = UPSTREAM
    atomic_json(path / 'source-manifest.json', result)
    return result


def observe_trial(path: Path) -> dict:
    effects, starts = read_lines(path / 'effects.jsonl'), read_lines(path / 'dispatch.jsonl')
    task_id = read_json(path / 'manifest.json', {}).get('trial', {}).get('trial_id')
    completed = (effects == [{'task_id': task_id, 'operation': 'effect'}]
                 and read_json(path / 'work/final-artifact.json') == {'task_id': task_id, 'verified_effects': 1})
    return {'task_completed': completed, 'effect_count': None if effects is None else len(effects),
            'effect_observation_available': effects is not None,
            'unsafe_replay_count': None if starts is None else sum(item['prior_unresolved'] for item in starts),
            'unsafe_replay_observation_available': starts is not None}


def _phase(path: Path, trial: dict, phase: str, deadline: float, key: Path) -> dict:
    env = {name: value for name, value in os.environ.items() if name.lower() not in
           {'http_proxy', 'https_proxy', 'all_proxy'} and name not in {'RELIAGENT_CHECKPOINT_KEY', 'PYTHONPATH'}}
    env['RELIAGENT_EVAL_KEY_FILE'] = str(key)
    result = {'exit_code': None, 'fault_triggered': False, 'timed_out': False, 'interrupted_at': None}
    with (path / f'{phase}.stdout').open('w') as stdout, (path / f'{phase}.stderr').open('w') as stderr:
        worker_path = read_json(path / 'manifest.json')['worker_path']
        process = subprocess.Popen([sys.executable, worker_path, str(path), phase], cwd=path / 'work', env=env,
                                   stdout=stdout, stderr=stderr, start_new_session=(os.name == 'posix'))
        append_json(path / 'observer.jsonl', {'event': 'worker_started', 'phase': phase, 'pid': process.pid})
        delivered = False
        try:
            while process.poll() is None:
                handshake = read_json(path / 'handshake.json') if phase == 'initial' and not delivered else None
                if handshake:
                    if (handshake['pid'] != process.pid or handshake['boundary'] != trial['fault_point']
                            or handshake['effect_count'] < 1):
                        raise ValueError('invalid live boundary handshake')
                    result['interrupted_at'] = time.monotonic()
                    os.kill(process.pid, signal.SIGKILL)
                    delivered = True
                    append_json(path / 'observer.jsonl', {'event': 'fault_delivered', **handshake})
                if time.monotonic() >= deadline:
                    result['timed_out'] = True
                    process.kill()
                    break
                time.sleep(0.01)
            result['exit_code'] = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            # All shell descendants are in the worker's session. Late effect children
            # additionally check worker liveness and the shared deadline before writing.
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            retired = {item['pid'] for item in read_lines(path / 'child-exits.jsonl') or []}
            for child in read_lines(path / 'children.jsonl') or []:
                if os.name == 'posix' and child['worker_pid'] == process.pid and child['pid'] not in retired:
                    try:
                        if os.getpgid(child['pid']) == child['pgid']:
                            os.kill(child['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    result['fault_triggered'] = delivered and result['exit_code'] == -getattr(signal, 'SIGKILL', 9)
    append_json(path / 'observer.jsonl', {'event': 'worker_exited', 'phase': phase, **result})
    return result


def run_trial(trial, output, sources, *, model='qwen3:8b', base_url='http://127.0.0.1:11434/v1',
              provider='real', fixture_mode='task', deadline_seconds=300):
    if urlsplit(base_url).username or urlsplit(base_url).password or urlsplit(base_url).query:
        raise ValueError('endpoint must not contain credentials or query parameters')
    path = Path(output).resolve() / trial['trial_id']
    path.mkdir(parents=True, exist_ok=False)
    work = path / 'work'
    work.mkdir()
    started = time.monotonic()
    deadline = started + deadline_seconds
    # Retained private key is outside the evidence subtree and never copied into it.
    from cryptography.fernet import Fernet
    private = Path(tempfile.mkdtemp(prefix='reliagent-live-keys-'))
    key = private / 'checkpoint.key'
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(Fernet.generate_key())
    atomic_json(private / 'trial.json', {'artifact_dir': str(path), 'key_file': key.name})
    worker_path = Path(sources['D_agent']['path']) / 'corecoder/evals/live_comparison_worker.py'
    command = 'python3 effect.py' if os.name == 'posix' else 'python effect.py'
    expected = {'task_id': trial['trial_id'], 'verified_effects': 1}
    prompt = ('Read instructions.txt with read_file. Follow its task: run the exact bash command once, '
              'then write final-artifact.json with write_file. Do not claim completion before both succeed. /no_think')
    (work / 'instructions.txt').write_text(f'Execute exactly `{command}` once to perform the external effect. '
        f'After it succeeds, write final-artifact.json containing exactly this JSON: {json.dumps(expected)}\n')
    # Launch an evaluator-owned external process through the unchanged builtin bash tool.
    (work / 'effect.py').write_text('import runpy, sys\n'
        f'sys.argv = [{str(worker_path)!r}, {str(path)!r}, "effect"]\nrunpy.run_path({str(worker_path)!r}, run_name="__main__")\n')
    atomic_json(path / 'manifest.json', {'schema': SCHEMA, 'trial': trial, 'configuration': CONFIGS[trial['config_id']],
        'source': sources[trial['config_id']], 'worker_path': str(worker_path),
        'worker_sha256': hashlib.sha256(worker_path.read_bytes()).hexdigest(), 'model': model, 'base_url': base_url, 'generation': GENERATION,
        'provider': provider, 'fixture_mode': fixture_mode, 'prompt': prompt, 'command': command,
        'prompt_version': 'live-effect-v2-short-command', 'path_environment': os.environ.get('PATH', ''),
        'checkpoint_key_permissions': oct(key.stat().st_mode & 0o777), 'max_context_tokens': 4096, 'max_rounds': 12, 'deadline_monotonic': deadline, 'deadline_seconds': deadline_seconds})
    for name in ('effects', 'dispatch', 'observer', 'provider', 'tools', 'approvals', 'sql'):
        (path / f'{name}.jsonl').write_text('')
    row = {**trial, 'provider': provider, 'model': model, 'base_url': base_url, 'valid': True,
           'fault_triggered': False, 'outcome': 'incomplete', 'task_completed': False, 'human_intervention': False,
           'human_assisted_completion': False, 'recovery_seconds': None, 'recovery_censored': False,
           'interruption_to_outcome_seconds': None, 'error': None, 'warmup': False, 'artifact_dir': str(path),
           'sql_write_statements': None, 'sql_write_transactions': None, 'runtime_status': None}
    try:
        initial = _phase(path, trial, 'initial', deadline, key)
        row.update(initial_exit_code=initial['exit_code'], fault_triggered=initial['fault_triggered'])
        worker = read_json(path / 'worker-initial.json', {})
        row['error'] = worker.get('error')
        if initial['exit_code'] != 0 and not initial['fault_triggered'] and not initial['timed_out']:
            row.update(valid=False, outcome='error', error=row['error'] or f"worker exited {initial['exit_code']}; see initial.stderr")
        if trial['case_id'] != 'normal' and not row['fault_triggered']:
            row.update(valid=False, outcome='missed_fault', fault_missed_reason='target boundary/action not observed')
        if initial['timed_out']:
            row.update(outcome='deadline', error='trial deadline exceeded')
        elif row['fault_triggered'] and CONFIGS[trial['config_id']]['recovery']:
            launch = time.monotonic()
            resumed = _phase(path, trial, 'resume', deadline, key)
            worker = read_json(path / 'worker-resume.json', {})
            row.update(human_intervention=worker.get('human_intervention', False), error=worker.get('error'),
                       recovery_seconds=time.monotonic() - launch,
                       interruption_to_outcome_seconds=time.monotonic() - initial['interrupted_at'])
            row['recovery_censored'] = resumed['timed_out'] or not (
                row['human_intervention'] or observe_trial(path)['task_completed'])
            if resumed['timed_out']:
                row.update(outcome='deadline', error='trial deadline exceeded during recovery')
        row['runtime_status'] = worker.get('runtime_status')
    except Exception as error:  # noqa: BLE001 - keep every assigned failure
        row.update(valid=False, outcome='error', error=f'{type(error).__name__}: {error}')
    row.update(observe_trial(path))
    if row['outcome'] == 'incomplete':
        row['outcome'] = 'completed' if row['task_completed'] else 'human_required' if row['human_intervention'] else 'incomplete'
    sql = read_lines(path / 'sql.jsonl')
    if sql is not None:
        row['sql_write_statements'] = sum(item.get('statements', 0) for item in sql)
        row['sql_write_transactions'] = sum(item.get('transactions', 0) for item in sql)
    row['elapsed_seconds'] = time.monotonic() - started
    provider_events = read_lines(path / 'provider.jsonl')
    row['model_seconds'] = None if provider_events is None else sum(item.get('model_seconds', 0) for item in provider_events)
    row['model_timing_censored'] = provider_events is None or (
        sum(item['event'] == 'request' for item in provider_events)
        != sum(item['event'] == 'response' for item in provider_events))
    row['non_model_seconds'] = None if row['model_timing_censored'] else max(0, row['elapsed_seconds'] - row['model_seconds'])
    atomic_json(path / 'result.json', row)
    return row


def new_bundle(rows, skipped, sources, *, repetitions, seed, provider):
    return {'schema': SCHEMA, 'layer': 'live_agent', 'configurations': CONFIGS, 'repetitions': repetitions,
            'seed': seed, 'provider': provider, 'smoke': repetitions < 5, 'source': sources,
            'schedule': rows, 'assigned_trials': len(rows), 'skipped_matrix': skipped, 'trials': [],
            'requested_trials': len(rows) + len(skipped),
            'environment': {'platform': platform.platform(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version},
            'timing': 'Instrumented elapsed includes startup; model durations separated; incomplete intervals censored.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--model', default='qwen3:8b')
    parser.add_argument('--base-url', default='http://127.0.0.1:11434/v1')
    parser.add_argument('--deadline-seconds', type=float, default=300)
    parser.add_argument('--provider', choices=['real', 'fixture'], default='real')
    args = parser.parse_args()
    if urlsplit(args.base_url).username or urlsplit(args.base_url).password or urlsplit(args.base_url).query:
        raise ValueError('endpoint must not contain credentials or query parameters')
    rows, skipped = build_matrix(args.repetitions, args.seed)
    args.output.mkdir(parents=True, exist_ok=False)
    sources = prepare_sources(args.output / 'sources')
    bundle = new_bundle(rows, skipped, sources, repetitions=args.repetitions, seed=args.seed, provider=args.provider)
    bundle.update(model=args.model, base_url=args.base_url, generation=GENERATION)
    if args.provider == 'real':
        parts = urlsplit(args.base_url)
        tags_url = urlunsplit((parts.scheme, parts.netloc, '/api/tags', '', ''))
        try:
            with build_opener(ProxyHandler({})).open(tags_url, timeout=5) as response:
                tags = json.load(response)
            bundle['model_fingerprint'] = [item for item in tags.get('models', [])
                                           if item.get('name') == args.model or item.get('model') == args.model]
        except Exception as error:  # noqa: BLE001 - availability failures remain evidence
            bundle['model_fingerprint'] = {'available': False, 'error_type': type(error).__name__}

    write_bundle(args.output, bundle)
    for trial in rows:
        bundle['trials'].append(run_trial(trial, args.output, sources, model=args.model, base_url=args.base_url,
            provider=args.provider, deadline_seconds=args.deadline_seconds))
        write_bundle(args.output, bundle)
        print(f"{len(bundle['trials'])}/{len(rows)} {trial['trial_id']}: {bundle['trials'][-1]['outcome']}", flush=True)


if __name__ == '__main__':
    main()
