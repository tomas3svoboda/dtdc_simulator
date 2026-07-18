"""Pure numerical core: `Model`, its state/input/output types, and `step()`.

BuildSpec §3 invariant: this module must never import `asyncua`, do file/network
I/O, or touch wall-clock/threading. It must be fully unit-testable with plain
arrays and deterministic given (x, u, t, dt).

M3a (BuildSpec §14, §7.9): DT-role stages' equilibrium targets now come from
`core/dt_solver.py::solve_dt` — the real Coletto (2022) dual-scale zonal
solve (PHZ/FTRZ/DCZ) — instead of M0's placeholder mechanistic cascade. Per
§7.9, the DT is a "quasi-steady map... recomputed each engine tick" in
principle, but `solve_dt` costs 9-60+ seconds per call even at a coarsened
mesh (measured directly), against a `dt_wall_s ~ 0.2s` tick budget — so it is
recomputed on a PERIODIC cadence (`ModelParams.dt_resolve_interval_s`, sim
-time), not every tick, holding the last-converged per-tray targets fixed in
between. The EXISTING first-order lag/holdup relaxation (`State.T/X1/X2/M`,
`_stage_tau`, unchanged since M0) still runs every tick, now relaxing toward
whichever target is current — this is the transport-lag mechanism §7.9 asks
for, reusing 100% of already-tested machinery rather than adding a second one.
See `DECISIONS.md`'s M3a entry for why a literal every-tick re-solve isn't
achievable without the (explicitly out-of-scope, deferred to M3b) persistent
-particle-state redesign of `core/zones/dcz.py`.

DC (dryer/cooler) stages now use `core/dc.py`'s real air-contacting balance
(§7.10), not a placeholder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from dtdc_simulator.core import dc, dt_solver
from dtdc_simulator.core.zones import ftrz

R_GAS = 8.314462618  # J/(mol K)


class StageRole(str, Enum):
    PREDESOLV = "PREDESOLV"
    MAIN = "MAIN"
    SPARGE = "SPARGE"
    DRYER = "DRYER"
    COOLER = "COOLER"


DT_ROLES = {StageRole.PREDESOLV, StageRole.MAIN, StageRole.SPARGE}
DC_ROLES = {StageRole.DRYER, StageRole.COOLER}


@dataclass(frozen=True)
class StageSpec:
    id: str
    role: StageRole
    diameter_m: float
    bed_height_m: float

    @property
    def volume_m3(self) -> float:
        return math.pi / 4.0 * self.diameter_m**2 * self.bed_height_m


@dataclass(frozen=True)
class ModelConstants:
    dH_vap_hexane: float
    dH_vap_water: float
    T_boil_hexane: float
    T_boil_water: float  # K, from Antoine(antoine_water) at 1 atm — see config/builder.py
    cp_solid: float
    cp_water_liquid: float
    cp_oil: float
    oil_fraction: float  # kg oil/kg dry solid (X3)
    rho_solid: float  # kg/m3, bulk solid-phase density (bed-holdup mass balance)
    bed_porosity: float  # eps_b, bulk void fraction (bed-holdup mass balance)
    # --- M3a additions ---
    dt_constants: dt_solver.DTSolverConstants
    # dt_resolve_interval_s is NOT here -- it's a HOT, live-tunable value on
    # `Inputs` now (M3a follow-up "C"), not a cold constant frozen at assembly.
    dt_nz_phz: int
    dt_nz_ftrz: int
    dt_nz_dcz: int
    dt_vapor_feed_water_kg_s: float
    dt_vapor_feed_hex_kg_s: float
    dt_vapor_feed_T: float
    dc_constants: dc.DCConstants
    # M3a follow-up ("A2"): real-time-tuned DT solve convergence settings,
    # deliberately looser than dt_solver.solve_dt()'s own validation-run
    # defaults -- see config/schema.py's own comment for the measured
    # numbers behind this choice.
    dt_outer_tol: float
    dt_outer_max_iter: int
    dt_dcz_inner_max_iter: int


@dataclass(frozen=True)
class OperatingSeed:
    """Initial-condition seed for `init_state` (from operating/disturbance
    defaults). M3a: carries enough of `OperatingDefaults` (feed flow rate,
    per-tray steam duties) to run one real `solve_dt` at assembly time
    (BuildSpec §4: "compute steady-state x0 via initializer at the operating
    defaults") — a one-time setup-phase cost, not tick-budget-constrained."""

    feed_temperature: float
    feed_moisture: float
    feed_hexane: float
    feed_flow_rate: float
    indirect_steam: dict[str, float] = field(default_factory=dict)
    direct_steam: dict[str, float] = field(default_factory=dict)
    sweep_arm_speed: dict[str, float] = field(default_factory=dict)


@dataclass
class State:
    """Persistent transient state carried between ticks (BuildSpec §7.12), one
    entry per stage, ordered as `Model.stages`."""

    T: np.ndarray  # K
    X1: np.ndarray  # moisture, kg/kg dry solid
    X2: np.ndarray  # hexane, kg/kg dry solid
    M: np.ndarray  # kg dry solid currently retained (bed holdup)
    # --- M3a: cached DT-solve targets + warm-start, one entry per DT-role stage ---
    dt_target_T: np.ndarray
    dt_target_X1: np.ndarray
    dt_target_X2: np.ndarray
    dt_warm_start_vapor_wV2: float
    dt_warm_start_vapor_T: float
    dt_warm_start_T_L_sup: float
    dt_last_solve_sim_time: float
    dt_converged: bool
    dt_outer_iterations: int

    def copy(self) -> "State":
        return State(
            T=self.T.copy(),
            X1=self.X1.copy(),
            X2=self.X2.copy(),
            M=self.M.copy(),
            dt_target_T=self.dt_target_T.copy(),
            dt_target_X1=self.dt_target_X1.copy(),
            dt_target_X2=self.dt_target_X2.copy(),
            dt_warm_start_vapor_wV2=self.dt_warm_start_vapor_wV2,
            dt_warm_start_vapor_T=self.dt_warm_start_vapor_T,
            dt_warm_start_T_L_sup=self.dt_warm_start_T_L_sup,
            dt_last_solve_sim_time=self.dt_last_solve_sim_time,
            dt_converged=self.dt_converged,
            dt_outer_iterations=self.dt_outer_iterations,
        )


@dataclass
class Inputs:
    """Hot inputs `u` for one tick — MV effective values + DV values (BuildSpec §5.2)."""

    feed_flow_rate: float  # kg/s dry solid
    feed_temperature: float  # K (static boundary at this fidelity; see DECISIONS.md)
    indirect_steam: dict[str, float] = field(default_factory=dict)  # W, per DT stage
    direct_steam: dict[str, float] = field(default_factory=dict)  # kg/s, per SPARGE stage
    sweep_arm_speed: dict[str, float] = field(default_factory=dict)  # rpm, per stage
    gate_opening: dict[str, float] = field(default_factory=dict)  # 0-100 %, per stage
    heated_air_temp: float = 380.0  # K
    heated_air_flow: float = 0.0  # kg/s
    ambient_air_temp: float = 298.0  # K
    ambient_air_flow: float = 0.0  # kg/s
    feed_moisture: float = 0.0  # kg/kg dry solid
    feed_hexane: float = 0.0  # kg/kg dry solid
    ambient_temp: float = 298.0  # K
    ambient_humidity: float = 0.01  # kg/kg
    # M3a follow-up ("C"): HOT, live-tunable -- see config/schema.py's
    # OperatingDefaults.dt_resolve_interval_s for the floor rationale.
    dt_resolve_interval_s: float = 120.0  # s, SIM time


@dataclass(frozen=True)
class MassInventory:
    """Very simple, live plant-wide mass-inventory diagnostic (mass/energy
    balance quality gate follow-up, DECISIONS.md) -- NOT a rigorous
    conservation proof (that's `core/balance.py`'s own job, test-suite-only,
    zero runtime cost); this is a cheap, always-on O(n_stages) signal for
    the real-time engine: is anything ACCUMULATING somewhere in the plant.
    `total_*_holdup_kg` are raw snapshots -- the actual "should read ~0 in
    steady state" signal is the CHANGE in these between consecutive
    `Outputs` (a consumer's own tick-to-tick diff; `Model.step` stays pure,
    no history tracked here). `feed_*`/`product_*` rates are exposed
    alongside for context (their own difference is total evaporation/duty
    -driven mass loss, genuinely nonzero in normal operation -- NOT itself
    a conservation signal, unlike the holdup totals)."""

    total_dry_solid_holdup_kg: float
    total_hexane_holdup_kg: float
    total_water_holdup_kg: float
    feed_dry_solid_kg_s: float
    feed_hexane_kg_s: float
    feed_water_kg_s: float
    product_dry_solid_kg_s: float
    product_hexane_kg_s: float
    product_water_kg_s: float


@dataclass
class Outputs:
    """PVs/KPIs for one tick (BuildSpec §9.1 PV/ node map)."""

    stage_T: dict[str, float]
    stage_X_hex_ppm: dict[str, float]
    stage_X_w_pct: dict[str, float]
    stage_vapor_temp: dict[str, float]
    stage_level_pct: dict[str, float]
    kpi_residual_hexane_ppm: float
    kpi_meal_moisture_pct: float
    kpi_steam_consumption_kg_per_t: float
    kpi_throughput_t_per_day: float
    dt_solver_converged: bool
    dt_solver_outer_iterations: int
    mass_inventory: MassInventory


def _dt_role_stages(stages: tuple[StageSpec, ...]) -> list[StageSpec]:
    return [s for s in stages if s.role in DT_ROLES]


def _build_dt_trays(
    dt_stages: list[StageSpec], indirect_steam: dict[str, float], direct_steam: dict[str, float]
) -> list[dt_solver.DTTray]:
    return [
        dt_solver.DTTray(
            id=s.id,
            role=s.role.value,
            diameter_m=s.diameter_m,
            bed_height_m=s.bed_height_m,
            Q_indirect_w=indirect_steam.get(s.id, 0.0),
            direct_steam_kg_s=direct_steam.get(s.id, 0.0),
        )
        for s in dt_stages
    ]


def _mean_sweep_arm_rpm(dt_stages: list[StageSpec], sweep_arm_speed: dict[str, float]) -> float:
    """One representative rpm for `solve_dt`'s own single (not per-tray)
    `bed_transport_coefficients` call -- mean across DT-role stages, same
    3.0 rpm fallback `_stage_tau` already uses per stage when unconfigured."""
    if not dt_stages:
        return 3.0
    return sum(sweep_arm_speed.get(s.id, 3.0) for s in dt_stages) / len(dt_stages)


def _reconstruct_warm_start_vapor_in(
    x: State, vapor_feed: dt_solver.VaporFeed, trays: list[dt_solver.DTTray]
) -> ftrz.VaporState:
    """Rebuild the FTRZ-facing `ftrz.VaporState` warm start from the cached
    `(wV2, T)` pair (`DTResult.dcz.vapor_out`'s own shape) plus the current
    tick's total vapor mass flow — mirrors `dt_solver.solve_dt`'s own
    internal derivation (`tests/test_dt_solver.py`'s warm-start test uses the
    identical construction)."""
    m_dir = trays[-1].direct_steam_kg_s if trays else 0.0
    m_vapor_total = vapor_feed.m_water_kg_s + vapor_feed.m_hex_kg_s + m_dir
    wV2 = x.dt_warm_start_vapor_wV2
    return ftrz.VaporState(
        m_water_kg_s=(1.0 - wV2) * m_vapor_total,
        m_hex_kg_s=wV2 * m_vapor_total,
        T=x.dt_warm_start_vapor_T,
    )


def _apply_dt_result(x_next: State, result: dt_solver.DTResult) -> None:
    for j, summary in enumerate(result.tray_summaries):
        x_next.dt_target_T[j] = summary.T
        x_next.dt_target_X1[j] = summary.X1
        x_next.dt_target_X2[j] = summary.X2
    x_next.dt_warm_start_vapor_wV2 = result.dcz.vapor_out.wV2
    x_next.dt_warm_start_vapor_T = result.dcz.vapor_out.T
    x_next.dt_warm_start_T_L_sup = result.ftrz.solid_out.T


@dataclass(frozen=True)
class Model:
    """Immutable assembled model (BuildSpec §4: bound at setup, never mutated)."""

    stages: tuple[StageSpec, ...]
    constants: ModelConstants
    base_residence_s: float = 90.0  # nominal per-stage lag time constant at 3 rpm sweep speed

    def stage_index(self, stage_id: str) -> int:
        for i, s in enumerate(self.stages):
            if s.id == stage_id:
                return i
        raise KeyError(f"unknown stage id: {stage_id}")

    def init_state(self, seed: OperatingSeed, start_empty: bool = False) -> State:
        """Seeds `x0` via one real `solve_dt` call at the operating defaults
        (BuildSpec §4) -- a one-time, not tick-budget-constrained cost. Left
        to raise naturally on failure (a genuinely bad config should block
        the CONFIGURED->READY transition, not be silently swallowed --
        `RuntimeFacade.assemble()` already leaves the state machine at
        CONFIGURED if this raises, per its own docstring).

        `start_empty` (M3a follow-up "B") only changes how the ACTUAL
        starting state is seeded -- `dt_target_*` is always computed from
        this same solve_dt call regardless, since that's what the existing
        lag mechanism relaxes toward either way. `start_empty=True` seeds an
        empty vessel (M=0, T=feed, X1=X2=0) instead of the converged target,
        so a run visibly fills/propagates material through the unit over
        simulated time rather than starting pre-solved at steady state.
        """
        n = len(self.stages)
        c = self.constants
        dt_stages = _dt_role_stages(self.stages)
        M0 = np.array([0.5 * self._stage_M_max(s) for s in self.stages], dtype=float)

        trays = _build_dt_trays(dt_stages, seed.indirect_steam, seed.direct_steam)
        solid_feed = dt_solver.SolidFeed(
            T=seed.feed_temperature,
            X1=seed.feed_moisture,
            X2=seed.feed_hexane,
            X3=c.oil_fraction,
            m_dry_kg_s=max(seed.feed_flow_rate, 1e-9),
        )
        vapor_feed = dt_solver.VaporFeed(
            m_water_kg_s=c.dt_vapor_feed_water_kg_s,
            m_hex_kg_s=c.dt_vapor_feed_hex_kg_s,
            T=c.dt_vapor_feed_T,
        )
        result = dt_solver.solve_dt(
            trays,
            solid_feed,
            vapor_feed,
            c.dt_constants,
            nz_phz=c.dt_nz_phz,
            nz_ftrz=c.dt_nz_ftrz,
            nz_dcz=c.dt_nz_dcz,
            outer_tol=c.dt_outer_tol,
            outer_max_iter=c.dt_outer_max_iter,
            dcz_inner_max_iter=c.dt_dcz_inner_max_iter,
            sweep_arm_rpm=_mean_sweep_arm_rpm(dt_stages, seed.sweep_arm_speed),
        )
        dt_target_T = np.array([s.T for s in result.tray_summaries], dtype=float)
        dt_target_X1 = np.array([s.X1 for s in result.tray_summaries], dtype=float)
        dt_target_X2 = np.array([s.X2 for s in result.tray_summaries], dtype=float)

        if start_empty:
            # Empty vessel: no holdup, no material -- DT-role T/X1/X2 have no
            # physical meaning without mass present, so seed them at the feed
            # state (a reasonable, non-NaN placeholder) rather than the
            # solved target; M=0 is the load-bearing part.
            T0 = np.full(n, seed.feed_temperature, dtype=float)
            X10 = np.zeros(n, dtype=float)
            X20 = np.zeros(n, dtype=float)
            M0 = np.zeros(n, dtype=float)
        else:
            # Seed DT-role stages directly at their converged target (skip
            # the cold feed-state transient the lag would otherwise start
            # from); non-DT (DC) stages still seed at the feed state,
            # matching M0's own original placeholder convention.
            T0 = np.full(n, seed.feed_temperature, dtype=float)
            X10 = np.full(n, seed.feed_moisture, dtype=float)
            X20 = np.full(n, seed.feed_hexane, dtype=float)
            j = 0
            for i, s in enumerate(self.stages):
                if s.role in DT_ROLES:
                    T0[i], X10[i], X20[i] = dt_target_T[j], dt_target_X1[j], dt_target_X2[j]
                    j += 1

        return State(
            T=T0,
            X1=X10,
            X2=X20,
            M=M0,
            dt_target_T=dt_target_T,
            dt_target_X1=dt_target_X1,
            dt_target_X2=dt_target_X2,
            dt_warm_start_vapor_wV2=result.dcz.vapor_out.wV2,
            dt_warm_start_vapor_T=result.dcz.vapor_out.T,
            dt_warm_start_T_L_sup=result.ftrz.solid_out.T,
            dt_last_solve_sim_time=0.0,
            dt_converged=result.converged,
            dt_outer_iterations=result.outer_iterations,
        )

    def _stage_M_max(self, stage: StageSpec) -> float:
        """Max dry-solid holdup (kg) implied by tray geometry and bulk density."""
        c = self.constants
        return stage.volume_m3 * c.rho_solid * (1.0 - c.bed_porosity)

    def _stage_tau(self, stage: StageSpec, u: Inputs) -> float:
        rpm = u.sweep_arm_speed.get(stage.id, 3.0)
        # gate_opening restricts discharge, so it sets residence time too (§5.2:
        # gate_opening "sets inter-stage solid flow / holdup (level)"). Normalized
        # against the scenario's default 50% so today's default config produces
        # the same tau as before this MV was wired up.
        gate_norm = min(max(u.gate_opening.get(stage.id, 50.0) / 50.0, 0.1), 3.0)
        if stage.role in DC_ROLES:
            base = self.base_residence_s * 1.5
        else:
            base = self.base_residence_s
        return base / max(rpm / 3.0, 0.1) / gate_norm

    def _dc_equilibrium(
        self, stage: StageSpec, T_in: float, X1_in: float, X2_in: float, u: Inputs
    ) -> tuple[float, float, float]:
        """§7.10: real well-mixed air-contacting balance (`core/dc.py`),
        shared by DRYER/COOLER -- only the air-stream arguments differ.
        `dc.air_contact_equilibrium` also reports the air stream's own exit
        state (`air_T_out`/`air_humidity_out`, added for `core/balance.py`'s
        two-sided DC conservation check) -- not consumed by `Model.step`
        today (no cross-stage air tracking exists yet), so only the solid
        -side targets are returned here."""
        c = self.constants
        m_dry = max(u.feed_flow_rate, 1e-9)
        if stage.role is StageRole.DRYER:
            T_eq, X1_eq, X2_eq, _, _ = dc.air_contact_equilibrium(
                T_in,
                X1_in,
                X2_in,
                u.heated_air_temp,
                u.heated_air_flow,
                u.ambient_humidity,
                m_dry,
                c.dc_constants,
            )
            return T_eq, X1_eq, X2_eq
        if stage.role is StageRole.COOLER:
            T_eq, X1_eq, X2_eq, _, _ = dc.air_contact_equilibrium(
                T_in,
                X1_in,
                X2_in,
                u.ambient_air_temp,
                u.ambient_air_flow,
                u.ambient_humidity,
                m_dry,
                c.dc_constants,
            )
            return T_eq, X1_eq, X2_eq
        raise ValueError(f"unhandled DC stage role: {stage.role}")

    def _resolve_dt(self, x: State, x_next: State, u: Inputs, dt_stages: list[StageSpec]) -> None:
        """Runs one `solve_dt` call and caches its per-tray targets onto
        `x_next` -- called from `step()` only when the periodic resolve
        interval has elapsed (see module docstring). On failure, keeps the
        previous targets and flags `dt_converged=False` (§7.9: "publish the
        best estimate and flag SolverStress", not crash the tick loop)."""
        c = self.constants
        trays = _build_dt_trays(dt_stages, u.indirect_steam, u.direct_steam)
        solid_feed = dt_solver.SolidFeed(
            T=u.feed_temperature,
            X1=u.feed_moisture,
            X2=u.feed_hexane,
            X3=c.oil_fraction,
            m_dry_kg_s=max(u.feed_flow_rate, 1e-9),
        )
        vapor_feed = dt_solver.VaporFeed(
            m_water_kg_s=c.dt_vapor_feed_water_kg_s,
            m_hex_kg_s=c.dt_vapor_feed_hex_kg_s,
            T=c.dt_vapor_feed_T,
        )
        has_warm_start = x.dt_outer_iterations > 0
        warm_vapor_in = (
            _reconstruct_warm_start_vapor_in(x, vapor_feed, trays) if has_warm_start else None
        )
        warm_T_L_sup = x.dt_warm_start_T_L_sup if has_warm_start else None
        try:
            result = dt_solver.solve_dt(
                trays,
                solid_feed,
                vapor_feed,
                c.dt_constants,
                nz_phz=c.dt_nz_phz,
                nz_ftrz=c.dt_nz_ftrz,
                nz_dcz=c.dt_nz_dcz,
                outer_tol=c.dt_outer_tol,
                outer_max_iter=c.dt_outer_max_iter,
                dcz_inner_max_iter=c.dt_dcz_inner_max_iter,
                warm_start_vapor_in=warm_vapor_in,
                warm_start_T_L_sup=warm_T_L_sup,
                sweep_arm_rpm=_mean_sweep_arm_rpm(dt_stages, u.sweep_arm_speed),
            )
            _apply_dt_result(x_next, result)
            x_next.dt_converged = result.converged
            x_next.dt_outer_iterations = result.outer_iterations
        except Exception:
            # Keep last-good targets; SolverStress (facade layer) picks up
            # dt_converged=False. Still stamp dt_last_solve_sim_time below so
            # a persistent failure doesn't retry (and re-fail) every tick.
            x_next.dt_converged = False

    def step(self, x: State, u: Inputs, t: float, dt: float) -> tuple[State, Outputs]:
        x_next = x.copy()
        dt_stages = _dt_role_stages(self.stages)

        if t - x.dt_last_solve_sim_time >= u.dt_resolve_interval_s:
            self._resolve_dt(x, x_next, u, dt_stages)
            x_next.dt_last_solve_sim_time = t

        T_in, X1_in, X2_in = u.feed_temperature, u.feed_moisture, u.feed_hexane
        m_in = u.feed_flow_rate  # kg/s dry solid, chained bed-holdup mass balance
        dt_idx = 0
        for i, stage in enumerate(self.stages):
            if stage.role in DT_ROLES:
                T_eq = x_next.dt_target_T[dt_idx]
                X1_eq = x_next.dt_target_X1[dt_idx]
                X2_eq = x_next.dt_target_X2[dt_idx]
                dt_idx += 1
            else:
                T_eq, X1_eq, X2_eq = self._dc_equilibrium(stage, T_in, X1_in, X2_in, u)

            tau = self._stage_tau(stage, u)
            decay = math.exp(-dt / tau)

            T_new = T_eq + (x.T[i] - T_eq) * decay
            X1_new = X1_eq + (x.X1[i] - X1_eq) * decay
            X2_new = X2_eq + (x.X2[i] - X2_eq) * decay

            # Bed holdup: same closed-form relaxation as T/X1/X2, toward the
            # steady-state mass implied by current inflow and residence time.
            M_eq = m_in * tau
            M_new = M_eq + (x.M[i] - M_eq) * decay
            m_out = M_new / tau

            x_next.T[i] = T_new
            x_next.X1[i] = X1_new
            x_next.X2[i] = X2_new
            x_next.M[i] = M_new

            T_in, X1_in, X2_in = T_new, X1_new, X2_new
            m_in = m_out

        return x_next, self.outputs(x_next, u)

    def outputs(self, x: State, u: Inputs) -> Outputs:
        c = self.constants
        stage_T = {s.id: float(x.T[i]) for i, s in enumerate(self.stages)}
        stage_X_hex_ppm = {s.id: float(x.X2[i] * 1.0e6) for i, s in enumerate(self.stages)}
        stage_X_w_pct = {s.id: float(x.X1[i] * 100.0) for i, s in enumerate(self.stages)}
        stage_vapor_temp = dict(stage_T)  # placeholder: no distinct vapor-phase state yet
        # Not clamped to 100: an over-restricted gate can genuinely overfill a
        # tray, and showing >100% is the useful HMI "flood" signal for that.
        stage_level_pct = {
            s.id: float(x.M[i] / self._stage_M_max(s) * 100.0) for i, s in enumerate(self.stages)
        }

        total_steam_kg_s = (
            sum(u.direct_steam.values()) + sum(u.indirect_steam.values()) / c.dH_vap_water
        )
        throughput_kg_s = max(u.feed_flow_rate, 1e-9)
        steam_kg_per_t = total_steam_kg_s / throughput_kg_s * 1000.0
        throughput_t_per_day = u.feed_flow_rate * 86400.0 / 1000.0

        last_stage = self.stages[-1]
        tau_last = max(self._stage_tau(last_stage, u), 1e-9)
        product_dry_solid_kg_s = float(x.M[-1]) / tau_last
        mass_inventory = MassInventory(
            total_dry_solid_holdup_kg=float(np.sum(x.M)),
            total_hexane_holdup_kg=float(np.sum(x.M * x.X2)),
            total_water_holdup_kg=float(np.sum(x.M * x.X1)),
            feed_dry_solid_kg_s=u.feed_flow_rate,
            feed_hexane_kg_s=u.feed_flow_rate * u.feed_hexane,
            feed_water_kg_s=u.feed_flow_rate * u.feed_moisture,
            product_dry_solid_kg_s=product_dry_solid_kg_s,
            product_hexane_kg_s=product_dry_solid_kg_s * float(x.X2[-1]),
            product_water_kg_s=product_dry_solid_kg_s * float(x.X1[-1]),
        )

        return Outputs(
            stage_T=stage_T,
            stage_X_hex_ppm=stage_X_hex_ppm,
            stage_X_w_pct=stage_X_w_pct,
            stage_vapor_temp=stage_vapor_temp,
            stage_level_pct=stage_level_pct,
            kpi_residual_hexane_ppm=float(x.X2[-1] * 1.0e6),
            kpi_meal_moisture_pct=float(x.X1[-1] * 100.0),
            kpi_steam_consumption_kg_per_t=steam_kg_per_t,
            kpi_throughput_t_per_day=throughput_t_per_day,
            dt_solver_converged=bool(x.dt_converged),
            dt_solver_outer_iterations=int(x.dt_outer_iterations),
            mass_inventory=mass_inventory,
        )
