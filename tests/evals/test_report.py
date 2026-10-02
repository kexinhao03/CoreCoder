import json
from dataclasses import replace
from pathlib import Path

from corecoder.evals.models import EvaluationResult, FaultSchedule
from corecoder.evals.report import write_markdown, write_raw_result
from corecoder.evals.runner import EvaluationRunner
from corecoder.evals.suites.phase3 import phase3_configurations, phase3_suite

ROOT = Path(__file__).resolve().parents[2]


def test_markdown_renderer_uses_raw_json_without_store(tmp_path):
    results = EvaluationRunner(tmp_path / "runs", phase3_configurations()).run(
        (phase3_suite()[0],)
    )
    raw_path = write_raw_result(results, tmp_path)
    markdown = write_markdown(raw_path, tmp_path).read_text()

    assert "E01" in markdown and "full" in markdown
    assert "Token/Cost: not available" in markdown
    assert "9 / 9" in markdown


def test_raw_report_contains_reproducible_results_and_null_zero_denominators(tmp_path):
    results = EvaluationRunner(tmp_path / "runs", phase3_configurations()).run(
        (phase3_suite()[0],)
    )

    raw_path = write_raw_result(results, tmp_path)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    assert raw["schema_version"] == "reliagent.eval.v1"
    assert raw["summary"]["total_repetitions"] == 9
    assert raw["summary"]["contract_passed_repetitions"] == 9
    assert raw["summary"]["by_configuration"]["full"] == {
        "duplicate_side_effect_rate": None,
        "error_count": 0,
        "contract_passed_repetitions": 3,
        "recovery_success_rate": None,
        "repetitions": 3,
        "task_success_rate": 1.0,
        "trace_complete_rate": 1.0,
    }
    assert len(raw["results"]) == 9
    assert raw["results"][0]["case_description"] == "normal success"
    assert raw["results"][0]["scenario"] == "normal_success"


def test_raw_report_rewrites_repository_paths_as_relative(tmp_path):
    absolute_fixture = ROOT / "corecoder" / "workflows" / "fixture.py"
    result = EvaluationResult(
        case_id="E00",
        case_description="portable evidence",
        scenario="normal_success",
        config_id="full",
        repetition=1,
        input_id="portable-v1",
        fault_schedule=FaultSchedule("none"),
        passed=True,
        task_succeeded=True,
        recovery_succeeded=None,
        error=None,
        effect_observations={"command": f"python {absolute_fixture}"},
    )

    raw_path = write_raw_result([result], tmp_path)
    raw_text = raw_path.read_text(encoding="utf-8")

    assert str(ROOT) not in raw_text
    relative_fixture = Path("corecoder") / "workflows" / "fixture.py"
    assert f"python {relative_fixture}" in raw_text


def test_raw_report_normalizes_windows_repository_paths(tmp_path):
    windows_root = str(ROOT).replace("/", "\\")
    escaped_windows_root = windows_root.replace("\\", "\\\\")
    result = EvaluationResult(
        case_id="E00",
        case_description="portable Windows evidence",
        scenario="normal_success",
        config_id="full",
        repetition=1,
        input_id="portable-windows-v1",
        fault_schedule=FaultSchedule("none"),
        passed=True,
        task_succeeded=True,
        recovery_succeeded=None,
        error=None,
        effect_observations={
            "command": f"python {windows_root}\\corecoder\\workflows\\fixture.py",
            "escaped_command": (
                f"python {escaped_windows_root}\\\\corecoder\\\\workflows\\\\fixture.py"
            ),
            "mixed": (
                f"path={windows_root}\\corecoder\\fixture.py; regex=\\d+\\w+"
            ),
        },
    )

    raw_path = write_raw_result([result], tmp_path)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    assert raw["results"][0]["effect_observations"]["command"] == (
        "python corecoder/workflows/fixture.py"
    )
    assert raw["results"][0]["effect_observations"]["escaped_command"] == (
        "python corecoder/workflows/fixture.py"
    )
    assert raw["results"][0]["effect_observations"]["mixed"] == (
        "path=corecoder/fixture.py; regex=\\d+\\w+"
    )


def test_report_counts_evaluation_errors_in_rate_denominators(tmp_path):
    results = EvaluationRunner(tmp_path / "runs", phase3_configurations()).run(
        (phase3_suite()[0],)
    )
    failed = replace(
        results[0],
        passed=None,
        task_succeeded=None,
        error="injected evaluator error",
        runtime_summary=None,
        trace_integrity=None,
        metrics_snapshot=None,
        effect_observations=None,
        assertions=None,
    )

    raw_path = write_raw_result([failed, *results[1:]], tmp_path)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    ablation = raw["summary"]["by_configuration"]["no_retry_no_recovery"]

    assert ablation["task_success_rate"] == 2 / 3
    assert ablation["trace_complete_rate"] == 2 / 3
    assert ablation["error_count"] == 1
    assert ablation["duplicate_side_effect_rate"] is None
