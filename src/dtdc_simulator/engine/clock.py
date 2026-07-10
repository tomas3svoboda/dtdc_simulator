"""Injectable clock strategies (BuildSpec §8.2)."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


class Clock(ABC):
    @abstractmethod
    def advance(self, dt_wall: float) -> float:
        """Return the sim-time delta this tick should integrate."""

    @abstractmethod
    def pace(self, dt_wall: float) -> None:
        """Block (or not) to align wall-clock progress with `dt_wall`."""

    @property
    @abstractmethod
    def actual_speed(self) -> float:
        """Achieved sim/wall ratio, updated by the most recent `pace()` call."""


class RealTimeClock(Clock):
    """Sim time = wall time x speed_factor. Sleeps out any slack; reports a
    reduced `actual_speed` on overrun instead of silently falling behind (§8.4)."""

    def __init__(self, speed_factor: float = 1.0) -> None:
        self.speed_factor = speed_factor
        self._tick_started_at: float | None = None
        self._actual_speed = speed_factor

    def advance(self, dt_wall: float) -> float:
        self._tick_started_at = time.monotonic()
        return self.speed_factor * dt_wall

    def pace(self, dt_wall: float) -> None:
        if self._tick_started_at is None:
            return
        elapsed = time.monotonic() - self._tick_started_at
        remaining = dt_wall - elapsed
        if remaining > 0:
            time.sleep(remaining)
            self._actual_speed = self.speed_factor
        else:
            actual_wall = max(elapsed, 1e-9)
            self._actual_speed = self.speed_factor * dt_wall / actual_wall

    @property
    def actual_speed(self) -> float:
        return self._actual_speed


class FreeRunClock(Clock):
    """Deterministic, as-fast-as-possible clock for regression tests (§8.2)."""

    def __init__(self, dt_sim: float) -> None:
        self.dt_sim = dt_sim

    def advance(self, dt_wall: float) -> float:
        return self.dt_sim

    def pace(self, dt_wall: float) -> None:
        return

    @property
    def actual_speed(self) -> float:
        return float("inf")
