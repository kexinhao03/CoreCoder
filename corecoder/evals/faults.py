"""Named, one-shot deterministic fault injection boundaries."""

from pathlib import Path

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


class EffectMarker:
    """Append-only local evidence for externally visible scenario effects."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("effect\n")

    @property
    def count(self) -> int:
        if not self.path.exists():
            return 0
        return self.path.read_text(encoding="utf-8").splitlines().count("effect")

    @property
    def duplicate_count(self) -> int:
        return max(0, self.count - 1)
