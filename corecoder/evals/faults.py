"""Named, one-shot deterministic fault injection boundaries."""

from .models import FaultSchedule


class FaultInjected(RuntimeError):
    """Raised only when an evaluation reaches its scheduled boundary."""


class FaultInjector:
    def __init__(self, schedule: FaultSchedule) -> None:
        self._schedule = schedule
        self.triggered = False

    def checkpoint(self, point: str) -> None:
        if not self.triggered and point == self._schedule.point:
            self.triggered = True
            raise FaultInjected(point)
