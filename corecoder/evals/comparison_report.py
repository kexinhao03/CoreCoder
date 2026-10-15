"""Pure descriptive reliability statistics and Markdown rendered from retained JSON."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


def wilson(numerator: int, denominator: int) -> list[float] | None:
    if denominator == 0:
        return None
    z = 1.959963984540054
    proportion = numerator / denominator
    scale = 1 + z * z / denominator
    center = (proportion + z * z / (2 * denominator)) / scale
    radius = z * math.sqrt(proportion * (1 - proportion) / denominator + z * z / (4 * denominator**2)) / scale
    return [max(0.0, center - radius), min(1.0, center + radius)]


def durations(values) -> dict:
    values = sorted(value for value in values if value is not None)
    if not values:
        return {'n': 0, 'median': None, 'p95': None, 'min': None, 'max': None}
    return {'n': len(values), 'median': statistics.median(values), 'p95': values[math.ceil(0.95 * len(values)) - 1],
            'min': values[0], 'max': values[-1]}


def rate(values, *, all_assigned=False):
    values = list(values)
    known = [value for value in values if value is not None]
    n = sum(bool(value) for value in known)
    total = len(values) if all_assigned else len(known)
    return {'numerator': n, 'denominator': total, 'unknown': len(values) - len(known), 'wilson95': wilson(n, total)}


def summarize(rows: list[dict]) -> dict:
    side_effects = [row for row in rows if row.get('side_effect_eligible', True)]
    recovery = [row for row in rows if row.get('recovery_seconds') is not None]
    return {
        'assigned': len(rows), 'invalid': sum(not row.get('valid', True) for row in rows),
        'completion': rate((row.get('task_completed') for row in rows), all_assigned=True),
        'automatic_completion': rate((row.get('task_completed') and not row.get('human_assisted_completion', False)
                                     for row in rows), all_assigned=True),
        'human_assisted_completion': rate((row.get('human_assisted_completion', False) for row in rows), all_assigned=True),
        'duplicates': rate(None if row.get('effect_count') is None else row['effect_count'] > 1 for row in side_effects),
        'unsafe_replay': rate(None if row.get('unsafe_replay_count') is None else row['unsafe_replay_count'] > 0
                              for row in side_effects),
        'unsafe_replay_total': sum(row.get('unsafe_replay_count') or 0 for row in side_effects),
        'human_intervention': rate((row.get('human_intervention') for row in rows), all_assigned=True),
        'elapsed': durations(row.get('elapsed_seconds') for row in rows),
        'recovery_completion': durations(row['recovery_seconds'] for row in recovery
                                         if row.get('task_completed') and not row.get('recovery_censored')),
        'recovery_safe_block': durations(row['recovery_seconds'] for row in recovery
                                         if row.get('human_intervention') and not row.get('recovery_censored')),
        'interruption_to_outcome': durations(row.get('interruption_to_outcome_seconds') for row in recovery
                                             if not row.get('recovery_censored')),
        'recovery_censored': sum(bool(row.get('recovery_censored')) for row in rows),
        'human_wait_seconds': durations(row.get('human_wait_seconds') for row in rows),
        'sql_write_statements': durations(row.get('sql_write_statements') for row in rows),
        'sql_write_transactions': durations(row.get('sql_write_transactions') for row in rows),
    }


def paired_differences(rows: list[dict]) -> dict:
    matches = defaultdict(dict)
    for row in rows:
        if row.get('config_id') not in {'A', 'B', 'C', 'D'} or not row.get('valid', True) or row.get('task_completed') is None:
            continue
        if row.get('fault_type', 'none') != 'none' and row.get('fault_triggered') is not True:
            continue
        key = tuple(row.get(field) for field in ('layer', 'workload', 'case_id', 'fault_point', 'fault_type', 'repetition'))
        matches[key][row['config_id']] = int(row['task_completed'])
    weights = {'B-A': {'B': 1, 'A': -1}, 'D-C': {'D': 1, 'C': -1}, 'C-A': {'C': 1, 'A': -1},
               'D-B': {'D': 1, 'B': -1}, 'D-B-C+A': {'D': 1, 'B': -1, 'C': -1, 'A': 1}}
    output = {}
    for label, coefficients in weights.items():
        values = [sum(cell[config] * weight for config, weight in coefficients.items()) for cell in matches.values()
                  if coefficients.keys() <= cell.keys()]
        output[label] = {'n': len(values), 'mean': statistics.mean(values) if values else None}
    return output


def _rate(value):
    interval = value['wilson95']
    bounds = 'unavailable' if interval is None else f"[{interval[0]:.3f}, {interval[1]:.3f}]"
    return f"{value['numerator']}/{value['denominator']} (95% {bounds}; unknown={value['unknown']})"


def _duration(value):
    if not value['n']:
        return 'n=0; unavailable'
    return (f"n={value['n']}; median={value['median']:.4f}; P95={value['p95']:.4f}; "
            f"min={value['min']:.4f}; max={value['max']:.4f}")


def render_report(bundle: dict) -> str:
    rows = [row for row in bundle['trials'] if not row.get('warmup', False)]
    retained_ids = {row['trial_id'] for row in bundle['trials']}
    pending = [{**row, 'valid': False, 'outcome': 'pending', 'task_completed': None,
                'human_intervention': None, 'effect_count': None, 'unsafe_replay_count': None}
               for row in bundle.get('schedule', []) if row['trial_id'] not in retained_ids]
    rows = [*rows, *pending]
    summary = summarize(rows)
    lines = ['# Reliability comparison', '',
             (f"Schema: `{bundle['schema']}`. {'SMOKE RUN — ' if bundle.get('smoke', bundle.get('repetitions', 10) < 10) else ''}"
             f"{len(rows)} assigned trials; {len(pending)} pending; {summary['invalid']} invalid/missed/pending; "
             f"{sum(bool(row.get('warmup')) for row in bundle['trials'])} warmups excluded."), '',
             ('A=(retry off, recovery off), B=(on, off), C=(off, on), D=(on, on). '
             'A is the same production Runtime with flags disabled. D_agent and E, if present, are separate live-Agent comparators '
             'and is excluded from factorial estimates.'), '',
             ('Mechanism requests are deterministic. The final boundary is before **fixed-task continuation**, '
             'with no inference in this layer. SIGKILL, child termination, and raised exceptions are distinct treatments.'), '',
             ('Task completion requires a correct external final artifact and exactly one matching independently fsynced effect. '
             'The primary denominator is all assigned repetitions, including invalid/missed faults and pending observations. '
             'Hazard denominators include observed side-effect trials; unknown observations are excluded and disclosed. '
             'Fault-missed trials can complete the task but provide no evidence of recovery. '
             'Only matched eligible observations enter the paired contrasts.'), '',
             '| Measure | Observed rate |', '|---|---|',
             f"| Task completion | {_rate(summary['completion'])} |",
             f"| Automatic completion | {_rate(summary['automatic_completion'])} |",
             f"| Human-assisted completion | {_rate(summary['human_assisted_completion'])} |",
             f"| Duplicate side-effect trials (>1) | {_rate(summary['duplicates'])} |",
             f"| Unsafe-replay trials (total starts={summary['unsafe_replay_total']}) | {_rate(summary['unsafe_replay'])} |",
             f"| Additional human reconciliation | {_rate(summary['human_intervention'])} |", '',
             ('Routine consent is logged separately. Disabled recovery leaves incomplete work without automatically '
             'requesting human reconciliation. Queryable receipts can be reconciled through the production recovery API; '
             'opaque uncertain effects stop for human reconciliation. No human resolutions are simulated.'), '',
             '## Latency (seconds)', '',
             f"- All end-to-end: {_duration(summary['elapsed'])}",
             f"- Restart launch → automatic completion: {_duration(summary['recovery_completion'])}",
             f"- Restart launch → explicit safe block: {_duration(summary['recovery_safe_block'])}",
             f"- Interruption → completed outcome or safe block: {_duration(summary['interruption_to_outcome'])}",
             f"- Unfinished/censored recovery samples: {summary['recovery_censored']}; never substituted with zero.",
             f"- Human waiting: {_duration(summary['human_wait_seconds'])}; no fabricated waiting duration.", '',
             '## Strata', '',
             '| Layer / config / workload / case / boundary / type | Completion | Duplicates | Unsafe replay | Human | Invalid |',
             '|---|---|---|---|---|---|---|']
    strata = defaultdict(list)
    for row in rows:
        key = tuple(str(row.get(field, 'unknown')) for field in
                    ('layer', 'config_id', 'workload', 'case_id', 'fault_point', 'fault_type'))
        strata[key].append(row)
    for key, items in sorted(strata.items()):
        metrics = summarize(items)
        lines.append('| ' + ' / '.join(key) + ' | ' + ' | '.join(_rate(metrics[name]) for name in
                     ('completion', 'duplicates', 'unsafe_replay', 'human_intervention')) + f" | {metrics['invalid']} |")
    lines.extend(['', '### Recovery latency by stratum (seconds)', '',
                  '| Stratum | Automatic completion | Safe block | Censored |', '|---|---|---|---|'])
    for key, items in sorted(strata.items()):
        metrics = summarize(items)
        if any(item.get('recovery_seconds') is not None for item in items):
            lines.append(f"| {' / '.join(key)} | {_duration(metrics['recovery_completion'])} | "
                         f"{_duration(metrics['recovery_safe_block'])} | {metrics['recovery_censored']} |")
    lines.extend(['', '## Paired factorial completion differences', '',
                  ('Descriptive risk differences, no significance claims. Retry: B−A and D−C; recovery: C−A and D−B; '
                  'interaction: D−B−C+A. E is never included. Small samples do not establish a general reliability rate.'), '',
                  '| Contrast | Matched n | Mean difference |', '|---|---|---|'])
    for label, value in paired_differences(rows).items():
        lines.append(f"| {label} | {value['n']} | {value['mean']} |")
    factorial_strata = defaultdict(list)
    for row in rows:
        if row['config_id'] in {'A', 'B', 'C', 'D'}:
            key = tuple(str(row.get(field, 'unknown')) for field in
                        ('layer', 'workload', 'case_id', 'fault_point', 'fault_type'))
            factorial_strata[key].append(row)
    lines.extend(['', '| Stratum | B−A | D−C | C−A | D−B | Interaction |', '|---|---|---|---|---|---|'])
    for key, items in sorted(factorial_strata.items()):
        values = paired_differences(items)
        lines.append('| ' + ' / '.join(key) + ' | ' + ' | '.join(
            f"n={value['n']}; difference={value['mean']}" for value in values.values()) + ' |')
    lines.extend(['', '## Normal-path overhead', '',
                  ('Timing includes process startup and fsynced measurement instrumentation. No warmups are run by default. '
                  'SQL statement counts come from live SQLite connection tracing; write transactions are recorded after commit. '
                  'These are measured SQL operations, not event counts. Compare A–D flags within mechanism; D_agent–E '
                  'live differences combine implementation and provider cost and include LLM variability.'), '',
                  '| Layer / config | Total seconds | SQL write statements | Committed write transactions |', '|---|---|---|---|'])
    overhead = defaultdict(list)
    for row in rows:
        if row['case_id'] == 'normal':
            overhead[(row['layer'], row['config_id'])].append(row)
    for key, items in sorted(overhead.items()):
        metrics = summarize(items)
        lines.append(f"| {' / '.join(key)} | {_duration(metrics['elapsed'])} | "
                     f"{_duration(metrics['sql_write_statements'])} | {_duration(metrics['sql_write_transactions'])} |")
    lines.extend(['', '## Failure / intervention evidence', ''])
    examples = [row for row in rows if not row.get('task_completed') or not row.get('valid', True) or row.get('human_intervention')]
    for row in examples[:24]:
        lines.append(f"- `{row['trial_id']}`: {row['outcome']}; effects={row.get('effect_count')}; "
                     f"error={row.get('error')}; artifacts: `{row.get('artifact_dir', 'unavailable')}`")
    if not examples:
        lines.append('No failure or intervention rows observed in this run.')
    lines.extend(['', f"All {len(examples)} such rows remain in raw JSON; examples above are capped at 24.", '',
                  '## Applicability and limits', '',
                  f"{len(bundle.get('skipped_matrix', []))} matrix cells are explicitly excluded from eligibility:"])
    for item in bundle.get('skipped_matrix', []):
        lines.append(f"- {item.get('config_id')} / {item.get('workload')} / {item.get('fault_point')} / "
                     f"{item.get('fault_type')}: {item.get('reason')}")
    lines.extend(['', ('No-recovery and safe-block outcomes are not automatic task completion. Zero observed duplicates '
                  'does not prove exactly-once behavior. Unsafe replay uses the independent dispatch ledger and prior unresolved '
                  'durable state, independently of the effect counter. Fixed-task continuation and receipt reconciliation do not '
                  'establish real-model reliability. Failed/missed/deadline trials and external evidence are retained.'), '',
                  '## Existing regression evidence', '',
                  ('The historical 90/90 and 24/24 scenario assertion contracts remain separate regression checks. '
                  'They are not denominators or comparative reliability rates in this report.'), '',
                  '## Source fingerprint', '', '```json', json.dumps(bundle.get('source', {}), indent=2, sort_keys=True), '```', ''])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    text = render_report(json.loads(args.raw.read_text()))
    if args.output:
        args.output.write_text(text)
    else:
        print(text)


if __name__ == '__main__':
    main()
