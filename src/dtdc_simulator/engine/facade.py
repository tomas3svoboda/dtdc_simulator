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
from dtdc_simulator.engine.control_interface import (
    Aggregation,
    ControlBinding,
    ControlLoopSnapshot,
    build_control_catalog,
)
from dtdc_simulator.engine.mv import DisturbanceVariable, ManipulatedVariable, Mode
from dtdc_simulator.engine.state_machine import SimState, StateMachine

# (min, max, rate_limit) defaults per MV key — BuildSpec §5.2 leaves numeric
# limits a DECIDE; see DECISIONS.md. rate_limit=None means unlimited (no slew cap).
DT_SCHEMA_ROLES = {SchemaStageRole.PREDESOLV, SchemaStageRole.MAIN, SchemaStageRole.SPARGE}

MV_LIMITS: dict[str, tuple[float, float, float | None]] = {
    "feed_flow_rate": (0.0, 100.0, None),
    "indirect_steam": (0.0, 3.0e6, None),
    # Validated model-domain floor, not the nominal operating target: below
    # ~3 kg/s the FTRZ free-boundary length exceeds the installed countercurrent
    # bed (and at intermediate points the no-driving-force limit is singular).
    # The industrial seed is 3.9 kg/s / 110.45 kg/t raw. Keep the GUI inside
    # the represented hardware domain; a future explicit shutdown/startup
    # regime would be needed before this can safely reach zero.
    "direct_steam": (3.0, 5.0, None),
    "sweep_arm_speed": (0.0, 10.0, None),
    "transfer_device_position": (0.0, 100.0, None),
    "heated_air_temp": (280.0, 450.0, None),
    # Cooling ~25 kg/s of hot meal to ~38 C with ambient air is energy-bound to a high
    # air:solid ratio (~16:1), so the COOLER air-flow ceiling is well above the DRYER's --
    # see core/dc.py's coupled two-sided balance and scenarios/soybean_default.yaml.
    "heated_air_flow": (0.0, 200.0, None),
    "ambient_air_flow": (0.0, 800.0, None),
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
class SteamInfo:
    """Steam conditions for the HMI utility boxes.

    Indirect steam condenses at the supply-header conditions. Direct steam is
    throttled to the sparge/meal contact pressure, so its displayed pressure
    and saturation temperature are deliberately separate from the header.
    `dH_vap_water` converts indirect heat duty (W) to equivalent condensate
    flow (kg/s).
    """

    supply_barg: float
    supply_T_K: float
    direct_contact_barg: float
    direct_contact_T_K: float
    dH_vap_water: float  # J/kg


@dataclass(frozen=True)
class TransferBoundaryInfo:
    id: str
    from_stage: str
    to_stage: str | None
    device_type: str
    controlled: bool
    vapor_seal: bool


@dataclass(frozen=True)
class StageGeometryInfo:
    """Cold geometry for one build stage — the provenance the OPC UA
    ``Constants/Geometry`` folder exposes read-only (BuildSpec §9.1)."""

    id: str
    role: str
    diameter_m: float
    bed_height_m: float
    vapor_path: str
    arm_mixing_factor: float


@dataclass
class ColdConfigSnapshot:
    """Read-only view of the assembled cold config for the OPC UA
    ``Constants/`` folder. ``physical``/``model`` are the pydantic
    ``model_dump()`` dicts (nested param groups stay nested); ``geometry`` is
    keyed by build stage id."""

    physical: dict
    model: dict
    geometry: dict[str, StageGeometryInfo]


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
    stage_vapor_paths: dict[str, str]
    stage_order: list[str]
    transfer_boundaries: tuple[TransferBoundaryInfo, ...]
    control_loops: dict[str, ControlLoopSnapshot]
    dt_resolve_interval_s: float  # M3a follow-up ("C")
    steam: SteamInfo | None = None


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
        self._stage_vapor_paths: dict[str, str] = {}
        self._stage_order: list[str] = []
        self._transfer_boundaries: tuple[TransferBoundaryInfo, ...] = ()
        self._control_bindings: dict[str, ControlBinding] = {}
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
        for boundary in config.topology.solid_transfers:
            if boundary.controlled:
                add(
                    f"transfer_device_position/{boundary.id}",
                    od.transfer_device_position[boundary.id],
                    "transfer_device_position",
                )

        self._mvs = mvs
        self._stage_roles = {s.id: s.role.value for s in config.geometry.stages}
        self._stage_vapor_paths = {s.id: s.vapor_path.value for s in config.geometry.stages}
        self._stage_order = all_ids
        self._transfer_boundaries = tuple(
            TransferBoundaryInfo(
                id=boundary.id,
                from_stage=boundary.from_stage,
                to_stage=boundary.to_stage,
                device_type=boundary.device_type.value,
                controlled=boundary.controlled,
                vapor_seal=boundary.vapor_seal,
            )
            for boundary in config.topology.solid_transfers
        )
        self._control_bindings = {binding.tag: binding for binding in build_control_catalog(config)}

        dd = config.disturbance_defaults
        self._dvs = {
            "feed_temperature": DisturbanceVariable("feed_temperature", dd.feed_temperature),
            "feed_moisture": DisturbanceVariable("feed_moisture", dd.feed_moisture),
            "feed_hexane": DisturbanceVariable("feed_hexane", dd.feed_hexane),
            # M4 (GUI redesign): feed oil (X3) is a live disturbance now -- see
            # core/model.py Inputs.feed_oil / _resolve_dt.
            "feed_oil": DisturbanceVariable("feed_oil", dd.feed_oil),
            # Weather, not an operator setpoint -- reclassified from an MV (see
            # MV_LIMITS' own history / DECISIONS.md): nothing sets the COOLER's
            # inlet air temperature, it's a disturbance like ambient_relative_humidity.
            "ambient_air_temp": DisturbanceVariable("ambient_air_temp", dd.ambient_air_temp),
            "ambient_relative_humidity": DisturbanceVariable(
                "ambient_relative_humidity", dd.ambient_relative_humidity
            ),
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

    def control_tags(self) -> list[str]:
        with self._lock:
            return list(self._control_bindings)

    def set_control_mode(self, tag: str, mode: Mode) -> None:
        """Set one PLC loop mode atomically across all bound internal MVs."""
        with self._lock:
            binding = self._control_bindings[tag]
            for key in binding.mv_keys:
                self._mvs[key].set_mode(mode)

    def _set_control_values_locked(
        self, binding: ControlBinding, display_value: float, *, auto: bool
    ) -> None:
        raw_value = binding.from_display(float(display_value))

        def write(key: str, value: float) -> None:
            mv = self._mvs[key]
            if auto:
                mv.set_auto_setpoint(value)
            else:
                mv.set_manual_setpoint(value)

        if binding.aggregation is Aggregation.COMMON:
            for key in binding.mv_keys:
                write(key, raw_value)
            return
        if binding.aggregation is Aggregation.SINGLE:
            write(binding.mv_keys[0], raw_value)
            return

        # TOTAL: fixed scenario allocation, with capacity-aware redistribution.
        remaining = max(raw_value, 0.0)
        active = list(range(len(binding.mv_keys)))
        allocated = [0.0] * len(binding.mv_keys)
        weights = list(binding.allocation_weights)
        while active:
            weight_sum = sum(weights[index] for index in active)
            if weight_sum <= 0.0:
                weight_sum = float(len(active))
                for index in active:
                    weights[index] = 1.0
            saturated: list[int] = []
            for index in active:
                mv = self._mvs[binding.mv_keys[index]]
                share = remaining * weights[index] / weight_sum
                if share > mv.max:
                    allocated[index] = mv.max
                    remaining -= mv.max
                    saturated.append(index)
            if not saturated:
                for index in active:
                    allocated[index] = remaining * weights[index] / weight_sum
                break
            active = [index for index in active if index not in saturated]
        for key, value in zip(binding.mv_keys, allocated):
            write(key, value)

    def set_control_setpoint(self, tag: str, value: float) -> None:
        """Write a PLC loop SP (the AUTO-side target)."""
        with self._lock:
            self._set_control_values_locked(self._control_bindings[tag], value, auto=True)

    def set_control_output(self, tag: str, value: float) -> None:
        """Write a PLC loop manual OP."""
        with self._lock:
            self._set_control_values_locked(self._control_bindings[tag], value, auto=False)

    def set_mv_group_manual_setpoint(self, prefix: str, value: float) -> None:
        """Broadcast one manual setpoint to every MV whose key shares `prefix`
        (the part before the '/', or the whole key for non-per-stage MVs) --
        e.g. a single 'arm rotation speed' HMI slider driving every
        `sweep_arm_speed/<stage>` at once. Mirrors `set_global_mode`'s
        fan-out; per-stage MVs stay individually addressable (OPC UA)."""
        with self._lock:
            for key, mv in self._mvs.items():
                if key.split("/", 1)[0] == prefix:
                    mv.set_manual_setpoint(value)

    def set_mv_weighted_group_manual_total(self, keys: list[str], total: float) -> None:
        """Set one physical zone total using its configured hydraulic split.

        The HMI exposes one PREDESOLV-jacket and one TOAST-jacket control,
        while internal per-tray duties remain available for diagnostics.
        Allocation is atomic, uses the same fixed scenario weights as the
        PLC-facing loop, and respects every per-tray limit; if a tray saturates,
        the remainder is redistributed over unsaturated trays.
        """
        with self._lock:
            if not keys:
                raise ValueError("weighted MV group requires at least one key")
            unknown = [key for key in keys if key not in self._mvs]
            if unknown:
                raise KeyError(f"unknown MV keys in weighted group: {unknown}")

            total = max(float(total), 0.0)
            capacity = sum(self._mvs[key].max for key in keys)
            remaining = min(total, capacity)
            active = list(keys)
            allocation: dict[str, float] = {}
            binding = next(
                (
                    candidate
                    for candidate in self._control_bindings.values()
                    if candidate.aggregation is Aggregation.TOTAL
                    and candidate.mv_keys == tuple(keys)
                ),
                None,
            )
            weights = (
                dict(zip(keys, binding.allocation_weights))
                if binding is not None
                else {key: max(self._mvs[key].manual_setpoint, 0.0) for key in keys}
            )
            if sum(weights.values()) <= 0.0:
                weights = {key: 1.0 for key in keys}

            while active:
                weight_sum = sum(weights[key] for key in active)
                if weight_sum <= 0.0:
                    for key in active:
                        weights[key] = 1.0
                    weight_sum = float(len(active))
                saturated: list[str] = []
                for key in active:
                    share = remaining * weights[key] / weight_sum
                    if share > self._mvs[key].max:
                        allocation[key] = self._mvs[key].max
                        remaining -= allocation[key]
                        saturated.append(key)
                if not saturated:
                    for key in active:
                        allocation[key] = remaining * weights[key] / weight_sum
                    break
                active = [key for key in active if key not in saturated]

            for key, value in allocation.items():
                self._mvs[key].set_manual_setpoint(value)

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

    # ------------------------------------------------------------------ tick loop
    def _read_effective_inputs_locked(self, dt: float) -> Inputs:
        """Caller must hold `self._lock`."""
        indirect_steam: dict[str, float] = {}
        direct_steam: dict[str, float] = {}
        sweep_arm_speed: dict[str, float] = {}
        transfer_device_position: dict[str, float] = {}

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
                elif prefix == "transfer_device_position":
                    transfer_device_position[stage_id] = value

        return Inputs(
            feed_flow_rate=self._mvs["feed_flow_rate"].tick(dt),
            feed_temperature=self._dvs["feed_temperature"].value,
            indirect_steam=indirect_steam,
            direct_steam=direct_steam,
            sweep_arm_speed=sweep_arm_speed,
            transfer_device_position=transfer_device_position,
            heated_air_temp=self._mvs["heated_air_temp"].tick(dt),
            heated_air_flow=self._mvs["heated_air_flow"].tick(dt),
            ambient_air_temp=self._dvs["ambient_air_temp"].value,
            ambient_air_flow=self._mvs["ambient_air_flow"].tick(dt),
            feed_moisture=self._dvs["feed_moisture"].value,
            feed_hexane=self._dvs["feed_hexane"].value,
            feed_oil=self._dvs["feed_oil"].value,
            ambient_relative_humidity=self._dvs["ambient_relative_humidity"].value,
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
    def _control_snapshots_locked(self) -> dict[str, ControlLoopSnapshot]:
        loops: dict[str, ControlLoopSnapshot] = {}
        for tag, binding in self._control_bindings.items():
            bound = [self._mvs[key] for key in binding.mv_keys]

            def aggregate(attribute: str) -> float:
                values = [float(getattr(mv, attribute)) for mv in bound]
                if binding.aggregation is Aggregation.TOTAL:
                    return sum(values)
                if binding.aggregation is Aggregation.COMMON:
                    return sum(values) / len(values)
                return values[0]

            modes = {mv.mode.value for mv in bound}
            mode = next(iter(modes)) if len(modes) == 1 else "MIXED"
            if binding.aggregation is Aggregation.TOTAL:
                raw_min = sum(mv.min for mv in bound)
                raw_max = sum(mv.max for mv in bound)
            elif binding.aggregation is Aggregation.COMMON:
                raw_min = max(mv.min for mv in bound)
                raw_max = min(mv.max for mv in bound)
            else:
                raw_min, raw_max = bound[0].min, bound[0].max

            loops[tag] = ControlLoopSnapshot(
                tag=tag,
                description=binding.description,
                engineering_units=binding.engineering_units,
                mode=mode,
                sp=binding.to_display(aggregate("auto_setpoint")),
                pv=binding.to_display(aggregate("effective_value")),
                op=binding.to_display(aggregate("effective_value")),
                minimum=binding.to_display(raw_min),
                maximum=binding.to_display(raw_max),
                status="Good" if len(modes) == 1 else "ConfigurationError",
                actuator_keys=binding.mv_keys,
            )
        return loops

    def get_cold_config(self) -> ColdConfigSnapshot | None:
        """The assembled cold config for the OPC UA ``Constants/`` folder, or
        ``None`` before a config is loaded (e.g. transiently during reconfigure).
        Read-only provenance — the physics uses the frozen Model, not this."""
        with self._lock:
            if self._config is None:
                return None
            cfg = self._config
            geometry = {
                s.id: StageGeometryInfo(
                    id=s.id,
                    role=s.role.value,
                    diameter_m=s.diameter_m,
                    bed_height_m=s.bed_height_m,
                    vapor_path=s.vapor_path.value if s.vapor_path is not None else "",
                    arm_mixing_factor=s.arm_mixing_factor,
                )
                for s in cfg.geometry.stages
            }
            return ColdConfigSnapshot(
                physical=cfg.physical.model_dump(),
                model=cfg.model.model_dump(),
                geometry=geometry,
            )

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
            steam = None
            if self._model is not None:
                c = self._model.constants
                assert self._config is not None
                steam = SteamInfo(
                    supply_barg=c.steam_supply_barg,
                    supply_T_K=c.steam_supply_T,
                    direct_contact_barg=self._config.model.direct_steam_pressure_barg,
                    direct_contact_T_K=c.dt_constants.T_direct_steam,
                    dH_vap_water=c.dH_vap_water,
                )
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
                stage_vapor_paths=dict(self._stage_vapor_paths),
                stage_order=list(self._stage_order),
                transfer_boundaries=self._transfer_boundaries,
                control_loops=self._control_snapshots_locked(),
                dt_resolve_interval_s=self._dt_resolve_interval_s,
                steam=steam,
            )
