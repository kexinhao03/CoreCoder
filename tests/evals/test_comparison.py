"""Behavioral evidence for factorial measurement, real faults, and safe recovery."""
import importlib
import importlib.util
import json
import os
import signal
import sqlite3

import pytest

POSIX_ONLY = pytest.mark.skipif(os.name != 'posix', reason='requires POSIX SIGKILL and process-liveness semantics')


def comparison():
    assert importlib.util.find_spec('corecoder.evals.comparison'), 'comparison supervisor is not implemented'
    return importlib.import_module('corecoder.evals.comparison')


def report():
    assert importlib.util.find_spec('corecoder.evals.comparison_report'), 'comparison report is not implemented'
    return importlib.import_module('corecoder.evals.comparison_report')


def trial(config='D', point='none', fault='none', workload='opaque', case='normal'):
    return {'trial_id': f'{config}-{workload}-{case}-{point}-{fault}-0', 'layer': 'mechanism',
                'config_id': config, 'case_id': case, 'repetition': 0, 'fault_point': point,
                'fault_type': fault, 'workload': workload}


def test_matrix_is_complete_interleaved_and_excludes_inapplicable_children():
    mod = comparison()
    rows, skipped = mod.build_matrix(2, 1729)
    # POSIX includes SIGKILL cells; other platforms retain the controls and all exceptions.
    expected_rows, expected_skips = (208, 32) if mod.os.name == 'posix' else (112, 80)
    assert len(rows) == expected_rows
    assert len({row['trial_id'] for row in rows}) == expected_rows
    assert {row['config_id'] for row in rows} == {'A', 'B', 'C', 'D'}
    assert len(skipped) == expected_skips
    assert all(row['fault_point'] == 'during_tool_execution' for row in rows if row['fault_type'] == 'child_kill')
    assert mod.build_matrix(2, 1729) == (rows, skipped)
    assert mod.build_matrix(2, 1730)[0] != rows
    for group in range(0, len(rows), 4):
        assert {row['config_id'] for row in rows[group:group + 4]} == {'A', 'B', 'C', 'D'}


def test_independent_observation_does_not_infer_success_or_missing_effect_zero(tmp_path):
    mod = comparison()
    mod.atomic_json(tmp_path / 'worker-initial.json', {'runtime_status': 'succeeded'})
    observed = mod.observe_trial(tmp_path)
    assert observed['task_completed'] is False
    assert observed['effect_count'] is None
    assert observed['unsafe_replay_count'] is None
    (tmp_path / 'effects.jsonl').write_text('{"task_id":"x"}\n' * 2)
    assert mod.observe_trial(tmp_path)['effect_count'] == 2


def test_rate_denominators_intervals_latency_and_pairing():
    mod = report()
    rows = []
    for config, complete in [('A', False), ('B', True), ('C', False), ('D', True)]:
        rows.append({**trial(config), 'valid': True, 'task_completed': complete, 'human_intervention': False,
                     'effect_count': 1, 'unsafe_replay_count': 0, 'elapsed_seconds': 2,
                     'recovery_seconds': None, 'recovery_censored': False, 'outcome': 'completed' if complete else 'failed'})
    rows.append({**rows[0], 'repetition': 1, 'valid': False, 'effect_count': None,
                 'unsafe_replay_count': None, 'outcome': 'missed_fault', 'recovery_censored': True})
    summary = mod.summarize(rows)
    assert summary['completion']['numerator'] == 2
    assert summary['completion']['denominator'] == 5
    assert summary['duplicates']['denominator'] == 4
    assert summary['duplicates']['unknown'] == 1
    assert summary['invalid'] == 1
    assert mod.wilson(0, 1) == pytest.approx([0, 0.7934506856])
    assert mod.durations([1, 2, 3, 100]) == {'n': 4, 'median': 2.5, 'p95': 100, 'min': 1, 'max': 100}
    paired = mod.paired_differences(rows)
    assert paired['B-A']['n'] == 1
    assert paired['B-A']['mean'] == 1
    assert paired['D-B-C+A']['mean'] == 0


@pytest.mark.parametrize('config,completed', [('A', False), ('B', True), ('C', False), ('D', True)])
def test_transient_preparation_retry_benefit(tmp_path, config, completed):
    row = comparison().run_trial(trial(config, case='retry_only'), tmp_path)
    assert row['task_completed'] is completed, row
    assert row['effect_count'] == int(completed)
    assert row['fault_triggered'] is True
    assert row['sql_write_statements'] > row['sql_write_transactions'] > 0
    assert row['human_intervention'] is False


@pytest.mark.parametrize('fault', [pytest.param('runtime_kill', marks=POSIX_ONLY), 'exception'])
@pytest.mark.parametrize('workload,complete,human', [('opaque', False, True), ('receipt', True, False)])
def test_after_effect_uses_real_recovery_without_replay(tmp_path, workload, complete, human, fault):
    row = comparison().run_trial(trial(point='after_effect_before_result_persist', fault=fault,
                                      workload=workload, case='fault'), tmp_path)
    path = tmp_path / row['trial_id']
    assert row['fault_triggered'] is True, row
    assert row['initial_exit_code'] == (-signal.SIGKILL if fault == 'runtime_kill' else 1)
    assert row['effect_count'] == 1
    assert row['unsafe_replay_count'] == 0
    assert row['human_intervention'] is human
    assert row['task_completed'] is complete
    assert row['recovery_seconds'] is not None
    assert (path / 'manifest.json').exists()
    assert (path / 'runtime.sqlite3').exists()
    assert (path / 'initial.stderr').exists()
    with sqlite3.connect(path / 'runtime.sqlite3') as db:
        effects = db.execute("SELECT status FROM tool_calls WHERE tool_name='effect'").fetchall()
    assert effects == [('succeeded' if complete else 'interrupted',)]


@pytest.mark.parametrize('fault', [pytest.param('runtime_kill', marks=POSIX_ONLY), 'exception'])
def test_after_result_reuses_persisted_success(tmp_path, fault):
    row = comparison().run_trial(trial(point='after_result_before_next_inference', fault=fault, case='fault'), tmp_path)
    assert row['fault_triggered'] and row['task_completed'], row
    assert row['effect_count'] == 1
    assert row['unsafe_replay_count'] == 0
    assert row['boundary_label'] == 'after_result_before_fixed_task_continuation'
    assert row['human_intervention'] is False


def test_recovery_off_does_not_restart_or_count_reconciliation(tmp_path):
    row = comparison().run_trial(trial('B', 'after_effect_before_result_persist', 'exception', case='fault'), tmp_path)
    assert row['effect_count'] == 1
    assert row['task_completed'] is False
    assert row['human_intervention'] is False
    assert row['recovery_seconds'] is None
    assert not (tmp_path / row['trial_id'] / 'resume.stdout').exists()


@POSIX_ONLY
def test_child_kill_is_not_runtime_kill_and_has_confirmed_exit(tmp_path):
    row = comparison().run_trial(trial(point='during_tool_execution', fault='child_kill', case='fault'), tmp_path)
    assert row['fault_triggered'] is True, row
    assert row['initial_exit_code'] == 0
    assert row['child_exit_code'] == -signal.SIGKILL
    assert row['effect_count'] == 0
    assert row['task_completed'] is False


def test_failed_and_missed_rows_keep_evidence(tmp_path):
    row = comparison().run_trial(trial(point='never_reached', fault='exception', case='fault'), tmp_path)
    assert row['outcome'] == 'missed_fault'
    assert row['valid'] is False
    assert row['task_completed'] is True
    saved = json.loads((tmp_path / row['trial_id'] / 'result.json').read_text())
    assert saved == row
    assert (tmp_path / row['trial_id'] / 'manifest.json').exists()


def test_report_keeps_invalid_and_unknown_and_separates_regression():
    mod = report()
    bundle = {'schema': 'reliagent.comparison.v1', 'trials': [
        {**trial(), 'valid': False, 'outcome': 'missed_fault', 'task_completed': False,
         'human_intervention': False, 'effect_count': None, 'unsafe_replay_count': None,
         'elapsed_seconds': 1, 'recovery_seconds': None, 'recovery_censored': True}],
         'skipped_matrix': [], 'source': {}, 'repetitions': 1}
    text = mod.render_report(bundle)
    assert '0/1' in text
    assert 'unknown=1' in text
    assert 'missed_fault' in text
    assert 'regression' in text.lower()
    assert 'fixed-task continuation' in text


def test_sql_counts_real_commits_not_rollback_or_audit_event_counts(tmp_path):
    comparison()
    worker = importlib.import_module('corecoder.evals.comparison_worker')
    connection = sqlite3.connect(tmp_path / 'measured.sqlite3', factory=worker.MeasuredConnection)
    connection.ledger = tmp_path / 'sql.jsonl'
    connection.set_trace_callback(connection.trace)
    connection.execute('CREATE TABLE sample(value)')  # one autocommit write transaction
    connection.execute('INSERT INTO sample VALUES (1)')
    connection.rollback()  # a write statement, but no committed transaction
    connection.execute('INSERT INTO sample VALUES (2)')
    connection.execute('INSERT INTO sample VALUES (3)')
    connection.commit()  # two writes in one actual transaction
    connection.close()
    records = comparison().read_lines(tmp_path / 'sql.jsonl')
    assert sum(item.get('statements', 0) for item in records) == 4
    assert sum(item.get('transactions', 0) for item in records) == 2


def test_deadline_retains_unknowns_and_pending_assignments_in_report(tmp_path):
    mod = comparison()
    row = mod.run_trial(trial(), tmp_path, deadline_seconds=0.001)
    assert row['outcome'] == 'deadline'
    assert row['task_completed'] is False
    assert row['fault_triggered'] is False
    assert (tmp_path / row['trial_id'] / 'initial.stderr').exists()
    bundle = {'schema': 'reliagent.comparison.v1', 'trials': [], 'schedule': [trial()], 'assigned_trials': 1}
    text = report().render_report(bundle)
    assert '0/1' in text
    assert 'pending' in text.lower()


@POSIX_ONLY
def test_runtime_kill_during_child_keeps_fault_evidence_and_safely_blocks(tmp_path):
    row = comparison().run_trial(trial(point='during_tool_execution', fault='runtime_kill', case='fault'), tmp_path)
    assert row['valid'] and row['fault_triggered'], row
    assert row['initial_exit_code'] == -signal.SIGKILL
    assert row['outcome'] == 'human_required'
    assert row['effect_count'] == 0
    assert row['unsafe_replay_count'] == 0


def test_fault_deadline_is_invalid_and_excluded_from_factorial_pairs(tmp_path):
    mod = comparison()
    rows = [mod.run_trial(trial(config, 'during_tool_execution', 'exception', case='fault'),
                          tmp_path, deadline_seconds=0.001) for config in 'ABCD']
    assert all(row['outcome'] == 'deadline' and not row['fault_triggered'] for row in rows)
    assert all(row['valid'] is False for row in rows)
    summary = report().summarize(rows)
    assert summary['completion']['denominator'] == 4
    assert summary['invalid'] == 4
    assert all(value['n'] == 0 for value in report().paired_differences(rows).values())


@pytest.mark.parametrize('completed', [False, True])
@pytest.mark.parametrize('trigger', [False, None])
def test_unconfirmed_fault_is_never_paired_even_in_legacy_valid_rows(completed, trigger):
    rows = [{**trial(config, 'during_tool_execution', 'runtime_kill', case='fault'),
             'valid': True, 'fault_triggered': trigger, 'task_completed': completed} for config in 'ABCD']
    assert all(value['n'] == 0 for value in report().paired_differences(rows).values())


@POSIX_ONLY
@pytest.mark.parametrize('point', ['during_tool_execution', 'never_reached'])
def test_child_delayed_registration_exits_after_supervisor_deadline(tmp_path, monkeypatch, point):
    import time
    from types import SimpleNamespace

    mod = comparison()
    wrapper = tmp_path / 'delayed_registration.py'
    wrapper.write_text('''import json, os, sys, time
time.sleep(2.5)  # Deliberately slower than the original two-second startup allowance.
from corecoder.evals import comparison_worker as worker
original_append = worker.append_json
def delayed_append(path, value):
    if path.name == 'children.jsonl' and value.get('tool') == 'effect':
        (path.parent / 'registration-blocked.json').write_text(json.dumps({'pid': os.getpid()}))
        while not (path.parent / 'release-registration').exists():
            time.sleep(0.005)
    original_append(path, value)
worker.append_json = delayed_append
raise SystemExit(worker.main())
''')
    actual_phase = mod._phase
    startup_deadline = None

    def delayed_phase(path, assigned, phase, deadline):
        nonlocal startup_deadline
        if phase == 'initial':
            startup_deadline = deadline
            manifest = mod.read_json(path / 'manifest.json')
            manifest['steps'][1]['argv'][1] = str(wrapper)
            mod.atomic_json(path / 'manifest.json', manifest)
        return actual_phase(path, assigned, phase, deadline)

    monkeypatch.setattr(mod, '_phase', delayed_phase)
    assigned = trial('A', point, 'runtime_kill', case='fault')
    directory = tmp_path / assigned['trial_id']
    # Expire only the supervisor's test clock after a real child startup handshake.
    # The tool retains a generous real deadline; parent loss must still stop it.
    def supervisor_clock():
        if startup_deadline is not None and (directory / 'registration-blocked.json').exists():
            return max(time.monotonic(), startup_deadline + 1)
        return time.monotonic()

    monkeypatch.setattr(mod, 'time', SimpleNamespace(monotonic=supervisor_clock, sleep=time.sleep))
    child_pid = None
    try:
        row = mod.run_trial(assigned, tmp_path, deadline_seconds=30)
        assert row['outcome'] == 'deadline', row
        blocked = mod.read_json(directory / 'registration-blocked.json')
        assert blocked is not None, row  # actual child launched, registration deliberately withheld
        child_pid = blocked['pid']
        assert not any(item.get('tool') == 'effect' for item in mod.read_lines(directory / 'children.jsonl'))
        (directory / 'release-registration').touch()
        until = time.monotonic() + 5
        while time.monotonic() < until:
            exits = mod.read_lines(directory / 'child-exits.jsonl') or []
            if any(item['pid'] == child_pid for item in exits):
                break
            time.sleep(0.01)
        assert any(item['pid'] == child_pid for item in exits), 'late-registering child remained alive after supervisor returned'
        assert not (directory / 'handshake.json').exists()
        assert mod.observe_trial(directory)['effect_count'] == 0
        aborts = mod.read_lines(directory / 'child-aborts.jsonl')
        assert any(item['pid'] == child_pid and item['parent_lost'] for item in aborts)
        # Verify actual process disappearance, not only an exit-intent record.
        while time.monotonic() < until:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_pid = None
                break
            time.sleep(0.01)
        assert child_pid is None, 'late-registering child did not actually exit'
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_non_posix_selector_satisfies_matrix_contract(monkeypatch):
    from types import SimpleNamespace

    mod = comparison()
    monkeypatch.setattr(mod, 'os', SimpleNamespace(name='nt'))
    test_matrix_is_complete_interleaved_and_excludes_inapplicable_children()
    rows, skipped = mod.build_matrix(2, 1729)
    assert len(rows) == 112
    assert len(skipped) == 80
    assert {row['fault_type'] for row in rows} == {'none', 'exception'}
    assert {row['case_id'] for row in rows} == {'normal', 'retry_only', 'fault'}
    assert all(item['fault_type'] in {'runtime_kill', 'child_kill'} for item in skipped)


@pytest.mark.parametrize('fault', ['runtime_kill', 'child_kill'])
def test_direct_non_posix_kill_is_refused_before_artifacts_or_effects(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace

    mod = comparison()
    simulated_os = SimpleNamespace(**vars(mod.os))
    simulated_os.name = 'nt'
    monkeypatch.setattr(mod, 'os', simulated_os)
    with pytest.raises(ValueError, match='POSIX SIGKILL'):
        mod.run_trial(trial(point='during_tool_execution', fault=fault, case='fault'), tmp_path)
    assert list(tmp_path.iterdir()) == []
