import pytest

from corecoder.evals.faults import FaultInjected, FaultInjector
from corecoder.evals.models import FaultSchedule


def test_fault_injector_fires_once_at_its_named_boundary():
    injector = FaultInjector(FaultSchedule("after_tool_persist"))

    injector.checkpoint("before_effect")
    with pytest.raises(FaultInjected, match="after_tool_persist"):
        injector.checkpoint("after_tool_persist")
    injector.checkpoint("after_tool_persist")

    assert injector.triggered is True
