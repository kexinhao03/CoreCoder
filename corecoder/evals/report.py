"""Persist raw evaluation evidence and render it without database access."""

import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .models import EvaluationResult

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_EFFECT_SCENARIOS = {
    "effect_persist_failure",
    "high_risk_interrupted",
    "repeated_resume",
    "ml_experiment_after_effect",
    "ml_valid_reconciliation",
    "ml_damaged_reconciliation",
    "ml_repeated_resume",
}
_RECOVERY_SCENARIOS = {
    "crash_after_step",
    "effect_persist_failure",
    "high_risk_interrupted",
    "repeated_resume",
    "ml_environment_recovery",
    "ml_experiment_after_effect",
    "ml_valid_reconciliation",
    "ml_damaged_reconciliation",
}


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _summary(results: Sequence[EvaluationResult]) -> dict:
    by_configuration = {}
    for config_id in sorted({result.config_id for result in results}):
        selected = [result for result in results if result.config_id == config_id]
        task_values = [result.task_succeeded is True for result in selected]
        recovery_values = [
            result.recovery_succeeded is True
            for result in selected
            if result.scenario in _RECOVERY_SCENARIOS
        ]
        duplicate_values = [
            bool((result.effect_observations or {}).get("duplicate_effects", 0))
            for result in selected
            if result.scenario in _EFFECT_SCENARIOS
        ]
        trace_values = [
            result.trace_integrity is not None
            and bool(result.trace_integrity["sequence_contiguous"])
            and not bool(result.trace_integrity["missing"])
            for result in selected
        ]
        by_configuration[config_id] = {
            "duplicate_side_effect_rate": _rate(duplicate_values),
            "error_count": sum(result.error is not None for result in selected),
            "contract_passed_repetitions": sum(
                result.passed is True for result in selected
            ),
            "recovery_success_rate": _rate(recovery_values),
            "repetitions": len(selected),
            "task_success_rate": _rate(task_values),
            "trace_complete_rate": _rate(trace_values),
        }
    return {
        "by_configuration": by_configuration,
        "error_count": sum(result.error is not None for result in results),
        "contract_passed_repetitions": sum(
            result.passed is True for result in results
        ),
        "total_repetitions": len(results),
    }


def _portable_paths(value):
    if isinstance(value, str):
        root = str(_REPOSITORY_ROOT)
        root_variants = {root, root.replace("\\", "/"), root.replace("/", "\\")}
        candidates = set()
        prefixes = set()
        for root_variant in root_variants:
            escaped_root = root_variant.replace("\\", "\\\\")
            candidates.update({root_variant, escaped_root})
            for candidate in {root_variant, escaped_root}:
                separators = {os.sep, "/", "\\", "\\\\"}
                for separator in separators:
                    prefixes.add(f"{candidate}{separator}")
        for prefix in sorted(prefixes, key=len, reverse=True):
            search_start = 0
            while (index := value.find(prefix, search_start)) >= 0:
                relative_start = index + len(prefix)
                quote = value[index - 1] if index and value[index - 1] in "\"'" else None
                relative_end = relative_start
                terminators = " \t\r\n;,'\"()[]{}<>"
                while relative_end < len(value):
                    character = value[relative_end]
                    if character == quote or (quote is None and character in terminators):
                        break
                    relative_end += 1
                relative = value[relative_start:relative_end]
                relative = relative.replace("\\\\", "/").replace("\\", "/")
                value = value[:index] + relative + value[relative_end:]
                search_start = index + len(relative)
        for candidate in sorted(candidates, key=len, reverse=True):
            value = value.replace(candidate, ".")
        return value
    if isinstance(value, dict):
        return {key: _portable_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable_paths(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_portable_paths(item) for item in value)
    return value


def write_raw_result(results: Sequence[EvaluationResult], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "evaluation-results.json"
    report = {
        "schema_version": "reliagent.eval.v1",
        "summary": _summary(results),
        "results": [_portable_paths(asdict(result)) for result in results],
    }
    path.write_text(
        json.dumps(report, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return path


def render_markdown(raw_report: dict) -> str:
    summary = raw_report["summary"]
    lines = [
        "# ReliAgent Evaluation",
        "",
        "Token/Cost: not available unless run-scoped LLM telemetry is captured.",
        "",
        (
            "Scenario-contract assertions passed: "
            f"{summary['contract_passed_repetitions']} / {summary['total_repetitions']}"
        ),
        "",
        "| Configuration | Contract assertions | Task success | Recovery success | Duplicate effects | Trace complete | Errors |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for config_id, values in summary["by_configuration"].items():
        lines.append(
            f"| {config_id} | {values['contract_passed_repetitions']} / {values['repetitions']} | "
            f"{_format_rate(values['task_success_rate'])} | "
            f"{_format_rate(values['recovery_success_rate'])} | "
            f"{_format_rate(values['duplicate_side_effect_rate'])} | "
            f"{_format_rate(values['trace_complete_rate'])} | {values['error_count']} |"
        )
    lines.extend([
        "",
        "| Case | Scenario | Config | Repetition | Passed | Task | Recovery | Error |",
        "|---|---|---|---:|---|---|---|---|",
    ])
    for result in raw_report["results"]:
        lines.append(
            f"| {result['case_id']} {result['case_description']} | {result['scenario']} | "
            f"{result['config_id']} | {result['repetition']} | "
            f"{result['passed']} | {result['task_succeeded']} | "
            f"{result['recovery_succeeded']} | {result['error'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def _format_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def write_markdown(raw_path: Path, output_dir: Path) -> Path:
    raw_report = json.loads(raw_path.read_text(encoding="utf-8"))
    path = output_dir / "evaluation-report.md"
    path.write_text(render_markdown(raw_report), encoding="utf-8")
    return path
