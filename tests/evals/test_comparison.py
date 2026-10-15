"""Behavioral evidence for factorial measurement, real faults, and safe recovery."""
import importlib
import importlib.util
import json
import signal
import sqlite3

import pytest


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
    # 2 workloads * 4 groups * 2 reps * (2 controls + 5*2 faults + 1 child fault)
    assert len(rows) == 208
    assert len({row['trial_id'] for row in rows}) == 208
    assert {row['config_id'] for row in rows} == {'A', 'B', 'C', 'D'}
    assert len(skipped) == 32  # 4 non-child boundaries * 4 configs * 2 workloads
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


@pytest.mark.parametrize('workload,complete,human', [('opaque', False, True), ('receipt', True, False)])
def test_sigkill_after_effect_uses_real_recovery_without_replay(tmp_path, workload, complete, human):
    row = comparison().run_trial(trial(point='after_effect_before_result_persist', fault='runtime_kill',
                                      workload=workload, case='fault'), tmp_path)
    path = tmp_path / row['trial_id']
    assert row['fault_triggered'] is True, row
    assert row['initial_exit_code'] == -signal.SIGKILL
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


def test_sigkill_after_result_reuses_persisted_success(tmp_path):
    row = comparison().run_trial(trial(point='after_result_before_next_inference', fault='runtime_kill', case='fault'), tmp_path)
    assert row['fault_triggered'] and row['task_completed'], row
    assert row['effect_count'] == 1
    assert row['unsafe_replay_count'] == 0
    assert row['boundary_label'] == 'after_result_before_fixed_task_continuation'
    assert row['human_intervention'] is False


def test_recovery_off_does_not_restart_or_count_reconciliation(tmp_path):
    row = comparison().run_trial(trial('B', 'after_effect_before_result_persist', 'runtime_kill', case='fault'), tmp_path)
    assert row['effect_count'] == 1
    assert row['task_completed'] is False
    assert row['human_intervention'] is False
    assert row['recovery_seconds'] is None
    assert not (tmp_path / row['trial_id'] / 'resume.stdout').exists()


def test_child_kill_is_not_runtime_kill_and_has_confirmed_exit(tmp_path):
    row = comparison().run_trial(trial(point='during_tool_execution', fault='child_kill', case='fault'), tmp_path)
    assert row['fault_triggered'] is True, row
    assert row['initial_exit_code'] == 0
    assert row['child_exit_code'] == -signal.SIGKILL
    assert row['effect_count'] == 0
    assert row['task_completed'] is False


def test_failed_and_missed_rows_keep_evidence(tmp_path):
    row = comparison().run_trial(trial(point='never_reached', fault='runtime_kill', case='fault'), tmp_path)
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


def test_runtime_kill_during_child_keeps_fault_evidence_and_safely_blocks(tmp_path):
    row = comparison().run_trial(trial(point='during_tool_execution', fault='runtime_kill', case='fault'), tmp_path)
    assert row['valid'] and row['fault_triggered'], row
    assert row['initial_exit_code'] == -signal.SIGKILL
    assert row['outcome'] == 'human_required'
    assert row['effect_count'] == 0
    assert row['unsafe_replay_count'] == 0
