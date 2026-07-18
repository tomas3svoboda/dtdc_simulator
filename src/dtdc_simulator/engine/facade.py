"""`RuntimeFacade` — the single thread-safe entry point for both adapters
(BuildSpec §3, §8.3). UI and OPC UA talk only to this class; neither imports
`core/` directly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from dtdc_simulator.config.builder import assemble_model
from dtdc_simulator.config.schema import ScenarioConfig
from dtdc_simulator.config.schema import StageRole as SchemaStageRole
from dtdc_simulator.core.model import Inputs, Model, Outputs, State
from dtdc_simulator.engine.clock import Clock, FreeRunClock, RealTimeClock
from dtdc_simulator.engine.mv import DisturbanceVariable, ManipulatedVariable, Mode
from dtdc_simulator.engine.state_machine import SimState, StateMachine

# (min, max, rate_limit) defaults per MV key — BuildSpec §5.2 leaves numeric
# limits a DECIDE; see DECISIONS.md. rate_limit=None means unlimited (no slew cap).
DT_SCHEMA_ROLES = {SchemaStageRole.PREDESOLV, SchemaStageRole.MAIN, SchemaStageRole.SPARGE}

MV_LIMITS: dict[str, tuple[float, float, float | None]] = {
    "feed_flow_rate": (0.0, 100.0, None),
    "indirect_steam": (0.0, 3.0e6, None),
    "direct_steam": (0.0, 5.0, None),
    "sweep_arm_speed": (0.0, 10.0, None),
    "gate_opening": (0.0, 100.0, None),
    "heated_air_temp": (280.0, 450.0, None),
    "heated_air_flow": (0.0, 30.0, None),
    "ambient_air_temp": (250.0, 320.0, None),
    "ambient_air_flow": (0.0, 30.0, None),
}


@dataclass
class MVSnapshot:
    key: str
    mode: Mode
    manual_setpoint: float
    auto_setpoint: float
    effective_value: float
    min: float
    max: float


@dataclass
class Snapshot:
    state: SimState
    sim_time: float
    actual_speed: float
    speed_factor: float
    undersample_warning: bool
    solver_stress: bool
    mvs: dict[str, MVSnapshot]
    dvs: dict[str, float]
    outputs: Outputs | None
    stage_roles: dict[str, str]
    stage_order: list[str]
    dt_resolve_interval_s: float  # M3a follow-up ("C")


class RuntimeFacade:
    """Owns the state machine, the immutable Model + hot state, and the
    MV/DV registry. Serializes all access with a single lock (§8.3)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sm = StateMachine()
        self._config: ScenarioConfig | None = None
        self._model: Model | None = None
        self._x0: State | None = None
        self._x: State | None = None
        self._mvs: dict[str, ManipulatedVariable] = {}
        self._dvs: dict[str, DisturbanceVariable] = {}
        self._stage_roles: dict[str, str] = {}
        self._stage_order: list[str] = []
        self._sim_time = 0.0
        self._actual_speed = 0.0
        self._speed_factor = 1.0
        self._dt_wall_s = 0.2
        self._max_control_interval_s = 10.0
        self._clock: Clock | None = None
        self._latest_outputs: Outputs | None = None
        self._undersample_warning = False
        self._solver_stress = False
        self._shutdown = False
        # M3a follow-up ("C"): HOT, live-tunable -- see config/schema.py's
        # OperatingDefaults.dt_resolve_interval_s for the 120s-floor rationale.
        self._dt_resolve_interval_s = 120.0

    # ------------------------------------------------------------------ lifecycle
    @property
    def state(self) -> SimState:
        with self._lock:
            return self._sm.state

    def configure(self, config: ScenarioConfig) -> None:
        """Load/enter config (§4.1: UNCONFIGURED/CONFIGURED -> CONFIGURED)."""
        with self._lock:
            if self._sm.state is SimState.UNCONFIGURED:
                self._sm.transition(SimState.CONFIGURED)
            elif self._sm.state is not SimState.CONFIGURED:
                raise RuntimeError(f"cannot configure from state {self._sm.state}")
            self._config = config

    def assemble(self) -> None:
        """Validate + assemble the immutable Model and compute x0 (§4.1: CONFIGURED -> READY)."""
        with self._lock:
            if self._sm.state is not SimState.CONFIGURED:
                raise RuntimeError(f"cannot assemble from state {self._sm.state}")
            if self._config is None:
                raise RuntimeError("no config loaded")
            config = self._config
            model, x0 = assemble_model(config)
            self._model = model
            self._x0 = x0
            self._x = x0.copy()
            self._sim_time = 0.0
            self._dt_wall_s = config.sim.dt_wall_s
            self._max_control_interval_s = config.sim.max_control_interval_s
            self._clock = self._build_clock(
                config.sim.clock.value, config.sim.speed_factor, config.sim.dt_wall_s
            )
            self._speed_factor = config.sim.speed_factor
            self._undersample_warning = False
            self._solver_stress = False
            self._dt_resolve_interval_s = config.operating_defaults.dt_resolve_interval_s
            self._build_registry(config)
            u0 = self._read_effective_inputs_locked(0.0)
            self._latest_outputs = model.outputs(self._x, u0)
            self._sm.transition(SimState.READY)

    def run(self) -> None:
        with self._lock:
            self._sm.transition(SimState.RUNNING)

    def pause(self) -> None:
        with self._lock:
            self._sm.transition(SimState.PAUSED)

    def stop(self) -> None:
        with self._lock:
            self._sm.transition(SimState.STOPPED)

    def reset(self) -> None:
        """Restore x0, keep Model (§4.1: STOPPED -> READY)."""
        with self._lock:
            self._sm.transition(SimState.READY)
            assert self._x0 is not None
            self._x = self._x0.copy()
            self._sim_time = 0.0
            self._solver_stress = False

    def reconfigure(self) -> None:
        """The only legal way to change a physical constant mid-session
        (§4.1: STOPPED -> CONFIGURED). Caller must then `configure()` + `assemble()`."""
        with self._lock:
            self._sm.transition(SimState.CONFIGURED)
            self._model = None
            self._x = None
            self._x0 = None

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True

    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    @staticmethod
    def _build_clock(kind: str, speed_factor: float, dt_wall_s: float) -> Clock:
        if kind == "freerun":
            return FreeRunClock(dt_sim=dt_wall_s * max(speed_factor, 1.0))
        return RealTimeClock(speed_factor=speed_factor)

    # ------------------------------------------------------------------ registry
    def _build_registry(self, config: ScenarioConfig) -> None:
        od = config.operating_defaults
        mvs: dict[str, ManipulatedVariable] = {}

        def add(key: str, seed: float, limits_key: str) -> None:
            lo, hi, rate = MV_LIMITS[limits_key]
            seed = min(max(seed, lo), hi)
            mvs[key] = ManipulatedVariable(
                key=key,
                manual_setpoint=seed,
                auto_setpoint=seed,
                mode=Mode.MANUAL,
                min=lo,
                max=hi,
                rate_limit=rate,
            )

        add("feed_flow_rate", od.feed_flow_rate, "feed_flow_rate")
        add("heated_air_temp", od.heated_air_temp, "heated_air_temp")
        add("heated_air_flow", od.heated_air_flow, "heated_air_flow")
        add("ambient_air_temp", od.ambient_air_temp, "ambient_air_temp")
        add("ambient_air_flow", od.ambient_air_flow, "ambient_air_flow")

        dt_stage_ids = [s.id for s in config.geometry.stages if s.role in DT_SCHEMA_ROLES]
        sparge_ids = [s.id for s in config.geometry.stages if s.role == SchemaStageRole.SPARGE]
        all_ids = [s.id for s in config.geometry.stages]

        for sid in dt_stage_ids:
            add(f"indirect_steam/{sid}", od.indirect_steam.get(sid, 0.0), "indirect_steam")
        for sid in sparge_ids:
            add(f"direct_steam/{sid}", od.direct_steam.get(sid, 0.0), "direct_steam")
        for sid in all_ids:
            add(f"sweep_arm_speed/{sid}", od.sweep_arm_speed.get(sid, 3.0), "sweep_arm_speed")
        for sid in all_ids:
            add(f"gate_opening/{sid}", od.gate_opening.get(sid, 50.0), "gate_opening")

        self._mvs = mvs
        self._stage_roles = {s.id: s.role.value for s in config.geometry.stages}
        self._stage_order = all_ids

        dd = config.disturbance_defaults
        self._dvs = {
            "feed_temperature": DisturbanceVariable("feed_temperature", dd.feed_temperature),
            "feed_moisture": DisturbanceVariable("feed_moisture", dd.feed_moisture),
            "feed_hexane": DisturbanceVariable("feed_hexane", dd.feed_hexane),
            "ambient_temp": DisturbanceVariable("ambient_temp", dd.ambient_temp),
            "ambient_humidity": DisturbanceVariable("ambient_humidity", dd.ambient_humidity),
        }

    # ------------------------------------------------------------------ hot writes
    def set_mv_mode(self, key: str, mode: Mode) -> None:
        with self._lock:
            self._mvs[key].set_mode(mode)

    def set_mv_manual_setpoint(self, key: str, value: float) -> None:
        with self._lock:
            self._mvs[key].set_manual_setpoint(value)

    def set_mv_auto_setpoint(self, key: str, value: float) -> None:
        with self._lock:
            self._mvs[key].set_auto_setpoint(value)

    def set_global_mode(self, mode: Mode) -> None:
        with self._lock:
            for mv in self._mvs.values():
                mv.set_mode(mode)

    def set_dv(self, key: str, value: float) -> None:
        with self._lock:
            self._dvs[key].set(value)

    def set_speed_factor(self, value: float) -> None:
        with self._lock:
            max_speed = self._max_control_interval_s / self._dt_wall_s if self._dt_wall_s else value
            if value > max_speed:
                value = max_speed
                self._undersample_warning = True
            else:
                self._undersample_warning = False
            self._speed_factor = value
            if isinstance(self._clock, RealTimeClock):
                self._clock.speed_factor = value

    def set_dt_resolve_interval_s(self, value: float) -> None:
        """M3a follow-up ("C"): live-tunable from the UI/OPC UA while
        RUNNING. Clamped to the 120s floor (see config/schema.py's own
        rationale) rather than rejected -- mirrors `set_speed_factor`'s own
        clamp-not-reject convention for an out-of-range live setpoint."""
        with self._lock:
            self._dt_resolve_interval_s = max(value, 120.0)

    def mv_keys(self) -> list[str]:
        with self._lock:
            return list(self._mvs.keys())

    def dv_keys(self) -> list[str]:
        with self._lock:
            return list(self._dvs.keys())

    # ------------------------------------------------------------------ tick loop
    def _read_effective_inputs_locked(self, dt: float) -> Inputs:
        """Caller must hold `self._lock`."""
        indirect_steam: dict[str, float] = {}
        direct_steam: dict[str, float] = {}
        sweep_arm_speed: dict[str, float] = {}
        gate_opening: dict[str, float] = {}

        for key, mv in self._mvs.items():
            if "/" in key:
                prefix, stage_id = key.split("/", 1)
                value = mv.tick(dt)
                if prefix == "indirect_steam":
                    indirect_steam[stage_id] = value
                elif prefix == "direct_steam":
                    direct_steam[stage_id] = value
                elif prefix == "sweep_arm_speed":
                    sweep_arm_speed[stage_id] = value
                elif prefix == "gate_opening":
                    gate_opening[stage_id] = value

        return Inputs(
            feed_flow_rate=self._mvs["feed_flow_rate"].tick(dt),
            feed_temperature=self._dvs["feed_temperature"].value,
            indirect_steam=indirect_steam,
            direct_steam=direct_steam,
            sweep_arm_speed=sweep_arm_speed,
            gate_opening=gate_opening,
            heated_air_temp=self._mvs["heated_air_temp"].tick(dt),
            heated_air_flow=self._mvs["heated_air_flow"].tick(dt),
            ambient_air_temp=self._mvs["ambient_air_temp"].tick(dt),
            ambient_air_flow=self._mvs["ambient_air_flow"].tick(dt),
            feed_moisture=self._dvs["feed_moisture"].value,
            feed_hexane=self._dvs["feed_hexane"].value,
            ambient_temp=self._dvs["ambient_temp"].value,
            ambient_humidity=self._dvs["ambient_humidity"].value,
            dt_resolve_interval_s=self._dt_resolve_interval_s,
        )

    def tick(self) -> None:
        """One iteration of the §8.1 tick loop. Called from the worker thread only."""
        with self._lock:
            if self._sm.state is not SimState.RUNNING:
                return
            clock, model, x, dt_wall, sim_time = (
                self._clock,
                self._model,
                self._x,
                self._dt_wall_s,
                self._sim_time,
            )
        if clock is None or model is None or x is None:
            return

        dt_target = clock.advance(dt_wall)
        with self._lock:
            u = self._read_effective_inputs_locked(dt_target)

        try:
            x_next, y = model.step(x, u, sim_time, dt_target)
            # M3a: SolverStress now reflects real DT-solve non-convergence
            # (DTResult.converged), not only a raised exception (§7.9/§9.1).
            solver_stress = not y.dt_solver_converged
        except Exception:
            x_next, y = x, model.outputs(x, u)
            solver_stress = True

        with self._lock:
            self._x = x_next
            self._sim_time += dt_target
            self._latest_outputs = y
            self._solver_stress = solver_stress

        clock.pace(dt_wall)

        with self._lock:
            self._actual_speed = clock.actual_speed

    # ------------------------------------------------------------------ reads
    def get_snapshot(self) -> Snapshot:
        with self._lock:
            mvs = {
                k: MVSnapshot(
                    key=k,
                    mode=mv.mode,
                    manual_setpoint=mv.manual_setpoint,
                    auto_setpoint=mv.auto_setpoint,
                    effective_value=mv.effective_value,
                    min=mv.min,
                    max=mv.max,
                )
                for k, mv in self._mvs.items()
            }
            dvs = {k: dv.value for k, dv in self._dvs.items()}
            return Snapshot(
                state=self._sm.state,
                sim_time=self._sim_time,
                actual_speed=self._actual_speed,
                speed_factor=self._speed_factor,
                undersample_warning=self._undersample_warning,
                solver_stress=self._solver_stress,
                mvs=mvs,
                dvs=dvs,
                outputs=self._latest_outputs,
                stage_roles=dict(self._stage_roles),
                stage_order=list(self._stage_order),
                dt_resolve_interval_s=self._dt_resolve_interval_s,
            )
