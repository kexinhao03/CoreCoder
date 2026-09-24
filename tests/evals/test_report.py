from corecoder.evals.models import EvaluationResult, FaultSchedule
from corecoder.evals.report import write_markdown, write_raw_result


def test_markdown_renderer_uses_raw_json_without_store(tmp_path):
    result = EvaluationResult("E01", "full", 1, FaultSchedule("none"), True, None, None)
    raw_path = write_raw_result([result], tmp_path)
    markdown = write_markdown(raw_path, tmp_path).read_text()

    assert "E01" in markdown and "full" in markdown
    assert "Token/Cost: not available" in markdown
