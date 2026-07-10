"""Per-MV MANUAL/AUTO arbitration and bumpless transfer (BuildSpec §6).

Pure logic (no I/O); mutation only happens through the methods below so the
bumpless-transfer invariant always holds. `RuntimeFacade` is the only caller
and serializes access with its lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


def clip(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


@dataclass
class ManipulatedVariable:
    key: str
    manual_setpoint: float
    auto_setpoint: float
    mode: Mode = Mode.MANUAL
    min: float = float("-inf")
    max: float = float("inf")
    rate_limit: float | None = None  # unit/s; None = unlimited
    effective_value: float = 0.0

    def __post_init__(self) -> None:
        raw = self.manual_setpoint if self.mode is Mode.MANUAL else self.auto_setpoint
        self.effective_value = clip(raw, self.min, self.max)

    def set_mode(self, mode: Mode) -> None:
        """Bumpless transfer (§6): seed the newly-active setpoint from effective_value."""
        if mode == self.mode:
            return
        if mode is Mode.MANUAL:
            self.manual_setpoint = self.effective_value
        else:
            self.auto_setpoint = self.effective_value
        self.mode = mode

    def set_manual_setpoint(self, value: float) -> None:
        self.manual_setpoint = value

    def set_auto_setpoint(self, value: float) -> None:
        self.auto_setpoint = value

    def tick(self, dt: float) -> float:
        """Advance `effective_value` one tick toward the active setpoint (§6)."""
        raw = self.manual_setpoint if self.mode is Mode.MANUAL else self.auto_setpoint
        clamped = clip(raw, self.min, self.max)
        if self.rate_limit is not None and dt > 0:
            max_step = self.rate_limit * dt
            delta = clip(clamped - self.effective_value, -max_step, max_step)
            self.effective_value = self.effective_value + delta
        else:
            self.effective_value = clamped
        return self.effective_value


@dataclass
class DisturbanceVariable:
    key: str
    value: float

    def set(self, value: float) -> None:
        self.value = value
