"""Persist raw evaluation evidence and render it without database access."""

import json
from dataclasses import asdict
from pathlib import Path


def write_raw_result(results: list[object], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "evaluation-results.json"
    path.write_text(json.dumps([asdict(result) for result in results], sort_keys=True, indent=2), encoding="utf-8")
    return path


def render_markdown(raw_results: list[dict]) -> str:
    lines = [
        "# ReliAgent Evaluation",
        "",
        "Token/Cost: not available unless run-scoped LLM telemetry is captured.",
        "",
        "| Case | Config | Repetition | Task | Error |",
        "|---|---|---:|---|---|",
    ]
    for result in raw_results:
        lines.append(f"| {result['case_id']} | {result['config_id']} | {result['repetition']} | {result['task_succeeded']} | {result['error'] or ''} |")
    return "\n".join(lines) + "\n"


def write_markdown(raw_path: Path, output_dir: Path) -> Path:
    raw_results = json.loads(raw_path.read_text(encoding="utf-8"))
    path = output_dir / "evaluation-report.md"
    path.write_text(render_markdown(raw_results), encoding="utf-8")
    return path
