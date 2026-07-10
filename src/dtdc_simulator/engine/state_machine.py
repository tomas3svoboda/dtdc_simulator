"""Lifecycle state machine (BuildSpec §4.1)."""

from __future__ import annotations

from enum import Enum


class SimState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED = "CONFIGURED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


_TRANSITIONS: dict[SimState, set[SimState]] = {
    SimState.UNCONFIGURED: {SimState.CONFIGURED},
    SimState.CONFIGURED: {SimState.READY},
    SimState.READY: {SimState.RUNNING},
    SimState.RUNNING: {SimState.PAUSED, SimState.STOPPED},
    SimState.PAUSED: {SimState.RUNNING, SimState.STOPPED},
    SimState.STOPPED: {SimState.READY, SimState.CONFIGURED},
}


class InvalidTransition(RuntimeError):
    pass


class StateMachine:
    def __init__(self) -> None:
        self._state = SimState.UNCONFIGURED

    @property
    def state(self) -> SimState:
        return self._state

    def can_transition(self, target: SimState) -> bool:
        return target in _TRANSITIONS.get(self._state, set())

    def transition(self, target: SimState) -> None:
        if not self.can_transition(target):
            raise InvalidTransition(f"{self._state} -> {target} is not a legal transition")
        self._state = target
