import pytest

from corecoder.evals.faults import EffectMarker, FaultInjected, FaultInjector
from corecoder.evals.models import FaultSchedule


def test_fault_injector_fires_once_at_its_named_boundary():
    injector = FaultInjector(FaultSchedule("after_tool_persist"))

    injector.checkpoint("before_effect")
    with pytest.raises(FaultInjected, match="after_tool_persist"):
        injector.checkpoint("after_tool_persist")
    injector.checkpoint("after_tool_persist")

    assert injector.triggered is True


def test_effect_marker_appends_and_counts_duplicate_effects(tmp_path):
    marker = EffectMarker(tmp_path / "effects.log")

    marker.record()
    marker.record()

    assert marker.count == 2
    assert marker.duplicate_count == 1
    assert (tmp_path / "effects.log").read_text(encoding="utf-8") == (
        "effect\neffect\n"
    )
