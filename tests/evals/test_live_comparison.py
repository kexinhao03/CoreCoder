"""Offline fixture tests; no real provider requests or benchmark reliability claims."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def api():
    try:
        return importlib.import_module('corecoder.evals.live_comparison')
    except ModuleNotFoundError:
        pytest.fail('live comparison implementation is missing')


def test_matrix_has_35_eligible_and_five_explicit_upstream_skips():
    rows, skipped = api().build_matrix(5, 1729)
    assert len(rows) == (35 if os.name == 'posix' else 10)
    assert len(skipped) == (5 if os.name == 'posix' else 30)
    durable_skips = [row for row in skipped if row['config_id'] == 'E' and row['case_id'] == 'after_result']
    assert len(durable_skips) == 5
    assert all('persistence' in row['reason'] for row in durable_skips)
    assert all(row['config_id'] in {'D_agent', 'E'} for row in rows)


@pytest.fixture
def sources(tmp_path):
    return api().prepare_sources(tmp_path / 'sources')


def test_worker_imports_pristine_upstream_even_with_current_modules_preloaded(sources, tmp_path):
    importlib.import_module("corecoder.agent")
    module = api()
    spec = module.make_trial('E', 'normal', 0)
    row = module.run_trial(spec, tmp_path / 'run', sources, provider='fixture', deadline_seconds=20)
    path = Path(row['artifact_dir'])
    identity = json.loads((path / 'source-identity.json').read_text())
    expected = subprocess.check_output(['git', 'show', f'{module.UPSTREAM}:corecoder/agent.py'], cwd=module.ROOT)
    import hashlib
    assert identity['agent_sha256'] == hashlib.sha256(expected).hexdigest()
    assert Path(identity['agent_file']).is_relative_to(Path(sources['E']['path']))
    assert row['provider'] == 'fixture'
    assert row['task_completed']
    assert not (path / 'worker-resume.json').exists()
    manifest = json.loads((path / 'manifest.json').read_text())
    assert manifest['model'] == 'qwen3:8b'
    assert manifest['generation'] == {'temperature': 0, 'max_tokens': 1024, 'reasoning_effort': 'none'}
    assert 'api_key' not in json.dumps(manifest)


@pytest.mark.skipif(os.name != 'posix', reason='genuine POSIX SIGKILL boundary requires POSIX')
@pytest.mark.parametrize('case', ['after_effect', 'after_result'])
def test_durable_fixture_crash_has_real_effect_and_production_recovery(sources, tmp_path, case):
    module = api()
    row = module.run_trial(module.make_trial('D_agent', case, 0), tmp_path / case, sources,
                           provider='fixture', deadline_seconds=20)
    path = Path(row['artifact_dir'])
    assert row['fault_triggered']
    assert row['initial_exit_code'] == -9
    assert row['effect_count'] == 1
    assert row['unsafe_replay_count'] == 0
    assert row['valid']
    assert row['human_intervention'] is (case == 'after_effect')
    assert row['task_completed'] is (case == 'after_result')
    handshake = json.loads((path / 'handshake.json').read_text())
    assert handshake['effect_count'] == 1
    if case == 'after_result':
        initial = json.loads((path / 'invocations-initial.json').read_text())
        resumed = json.loads((path / 'invocations-resume.json').read_text())
        effect = next(item for item in initial if item['tool_name'] == 'bash')
        assert effect['state'] == 'completed'
        assert [item for item in resumed if item['tool_name'] == 'bash'] == [effect]
    else:
        assert 'unknown tool outcome' in row['error']


@pytest.mark.skipif(os.name != 'posix', reason='genuine POSIX SIGKILL boundary requires POSIX')
def test_upstream_fault_does_not_invent_restart(sources, tmp_path):
    module = api()
    row = module.run_trial(module.make_trial('E', 'after_effect', 0), tmp_path / 'e', sources,
                           provider='fixture', deadline_seconds=20)
    assert row['fault_triggered'] and row['effect_count'] == 1
    assert not row['task_completed']
    assert row['recovery_seconds'] is None
    assert not row['human_intervention']
    assert not (Path(row['artifact_dir']) / 'resume.stdout').exists()


def test_missed_fault_is_invalid_even_if_model_finishes(sources, tmp_path):
    module = api()
    row = module.run_trial(module.make_trial('D_agent', 'after_effect', 0), tmp_path / 'miss', sources,
                           provider='fixture', fixture_mode='no_tools', deadline_seconds=20)
    assert not row['valid']
    assert row['outcome'] == 'missed_fault'
    assert not row['fault_triggered']
    assert row['effect_count'] == 0


def test_live_report_discloses_real_scope_without_factorial_claims():
    module = api()
    from corecoder.evals.comparison_report import render_report
    text = render_report(module.new_bundle([], [], {}, repetitions=1, seed=1, provider='fixture'))
    assert 'auto_tool_retry=false' in text
    assert 'pristine upstream' in text
    assert 'no inference in this layer' not in text
    assert 'Queryable receipts can be reconciled' not in text
    assert '## Paired factorial completion differences' not in text


def test_late_effect_child_cannot_write_after_worker_exit(tmp_path):
    module = api()
    path = tmp_path / 'child'
    path.mkdir()
    (path / 'manifest.json').write_text(json.dumps({'trial': {'trial_id': 'late'}, 'deadline_monotonic': 0}))
    result = subprocess.run([sys.executable, str(module.WORKER), str(path), 'effect'], capture_output=True, check=False)
    assert result.returncode == 125
    assert not (path / 'effects.jsonl').exists()


def test_d_and_e_capture_identical_tool_schemas_and_generation(sources, tmp_path):
    module = api()
    configs = []
    for name in ('D_agent', 'E'):
        row = module.run_trial(module.make_trial(name, 'normal', 0), tmp_path / name, sources,
                               provider='fixture', deadline_seconds=20)
        assert row['task_completed']
        path = Path(row['artifact_dir'])
        configs.append(json.loads((path / 'agent-config-initial.json').read_text()))
        assert row['model_seconds'] >= 0
        assert row['non_model_seconds'] >= 0
        assert not row['model_timing_censored']
        assert json.loads((path / 'manifest.json').read_text())['provider'] == 'fixture'
    for field in ('tools', 'generation', 'model', 'base_url'):
        assert configs[0][field] == configs[1][field]


def test_deadline_preserves_missing_fault_and_censors_interrupted_model(sources, tmp_path):
    module = api()
    row = module.run_trial(module.make_trial('D_agent', 'after_effect', 0), tmp_path / 'deadline', sources,
                           provider='fixture', fixture_mode='wait', deadline_seconds=5)
    assert row['outcome'] == 'deadline'
    assert not row['valid']
    assert not row['fault_triggered']
    assert row['model_timing_censored']
    assert row['non_model_seconds'] is None
    assert (Path(row['artifact_dir']) / 'result.json').exists()


@pytest.mark.skipif(os.name != 'posix', reason='genuine POSIX SIGKILL boundary requires POSIX')
@pytest.mark.parametrize('config', ['D_agent', 'E'])
def test_shared_accepted_result_boundary_is_durable_only_for_d(sources, tmp_path, config):
    module = api()
    row = module.run_trial(module.make_trial(config, 'after_accepted_result', 0), tmp_path / config, sources,
                           provider='fixture', deadline_seconds=20)
    assert row['valid'] and row['fault_triggered']
    assert row['effect_count'] == 1
    assert row['task_completed'] is (config == 'D_agent')
    path = Path(row['artifact_dir'])
    handshake = json.loads((path / 'handshake.json').read_text())
    assert handshake['boundary_label'] == ('durable_result' if config == 'D_agent' else 'volatile_result')
    assert handshake['accepted_tool_call_id']
    if config == 'D_agent':
        before = json.loads((path / 'invocations-initial.json').read_text())
        after = json.loads((path / 'invocations-resume.json').read_text())
        assert [item for item in before if item['tool_name'] == 'bash'] == [item for item in after if item['tool_name'] == 'bash']
    else:
        assert not (path / 'resume.stdout').exists()


def test_live_common_case_comparison_never_pools_unmatched_durable_only_case():
    from corecoder.evals.comparison_report import live_paired_differences
    rows = [{'config_id': 'D_agent', 'case_id': 'after_result', 'repetition': 0, 'valid': True, 'task_completed': True,
             'fault_type': 'runtime_kill', 'fault_triggered': True}]
    assert live_paired_differences(rows) == {}
    rows.extend({'config_id': name, 'case_id': 'after_accepted_result', 'repetition': 0, 'valid': True,
                 'task_completed': name == 'D_agent', 'fault_type': 'runtime_kill', 'fault_triggered': True}
                for name in ('D_agent', 'E'))
    assert live_paired_differences(rows) == {'after_accepted_result': {'n': 1, 'mean': 1}}


@pytest.mark.skipif(os.name != 'posix', reason='POSIX worker PID liveness check requires POSIX')
def test_effect_child_delayed_registration_checks_owner_again_before_effect(tmp_path):
    import os
    import time
    module = api()
    path = tmp_path / 'late-child'
    path.mkdir()
    (path / 'manifest.json').write_text(json.dumps({'trial': {'trial_id': 'late', 'fault_point': 'none'},
                                                   'deadline_monotonic': time.monotonic() + 20}))
    owner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
    wrapper = path / 'wrapper.py'
    wrapper.write_text('''import runpy, sys, time
from pathlib import Path
scope = runpy.run_path(sys.argv[1])
original = scope['append']
def delayed(path, value):
    if path.name == 'children.jsonl':
        (path.parent / 'registered-waiting').touch()
        while not (path.parent / 'release').exists():
            time.sleep(0.01)
    original(path, value)
scope['effect'].__globals__['append'] = delayed
scope['effect'](Path(sys.argv[2]))
''')
    env = {**os.environ, 'RELIAGENT_EVAL_WORKER_PID': str(owner.pid)}
    child = subprocess.Popen([sys.executable, str(wrapper), str(module.WORKER), str(path)], env=env)
    try:
        deadline = time.monotonic() + 10
        while not (path / 'registered-waiting').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (path / 'registered-waiting').exists()
        owner.kill()
        owner.wait(timeout=5)
        (path / 'release').touch()
        assert child.wait(timeout=5) == 125
        assert not (path / 'effects.jsonl').exists()
    finally:
        for process in (owner, child):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_completed_fallback_attempts_do_not_censor_chat_timing(sources, tmp_path, monkeypatch):
    module = api()

    def completed_fallback(path, trial, phase, deadline, key):
        # A known HTTP400 parameter rejection and successful fallback are one completed chat.
        events = [{'event': 'chat_start', 'phase': phase},
                  {'event': 'request', 'phase': phase},
                  {'event': 'request', 'phase': phase},
                  {'event': 'response', 'phase': phase, 'model_seconds': 0.25}]
        (path / 'provider.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
        return {'exit_code': 0, 'timed_out': False, 'fault_triggered': False}

    monkeypatch.setattr(module, '_phase', completed_fallback)
    row = module.run_trial(module.make_trial('E', 'normal', 0), tmp_path / 'fallback', sources,
                           provider='fixture', deadline_seconds=20)
    assert row['model_seconds'] == 0.25
    assert row['model_timing_censored'] is False
    assert row['non_model_seconds'] is not None
