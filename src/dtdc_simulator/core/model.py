"""Pure numerical core: `Model`, its state/input/output types, and `step()`.

BuildSpec §3 invariant: this module must never import `asyncua`, do file/network
I/O, or touch wall-clock/threading. It must be fully unit-testable with plain
arrays and deterministic given (x, u, t, dt).

PLACEHOLDER PHYSICS (M0, BuildSpec §14): each DT/DC stage is modeled as a
first-order-lag holdup relaxing toward an equilibrium, chained top-to-bottom.
This is deliberately NOT the Coletto (2022) dual-scale zonal model (PHZ/FTRZ/
DCZ, receding front, 12-layer particle FVM) — that lands in M1/M2 (BuildSpec
§7, §14) behind this same `Model.init_state/step/outputs` interface, so
callers (engine/, interfaces/) do not change. The DT equilibrium *is*,
however, a mechanistic lumped energy balance (see `_stage_equilibrium`): a
sequential flash/sensible-heat cascade driven only by real dH_vap/cp/T_boil
properties (no fitted "duty saturation curve" or hand-picked ceiling) — a
lumped (0-D per tray) simplification of the same physics, not a curve fit.

The quality kinetics (§7.11: TIA biexponential-rate blend, protein
denaturation) ARE implemented per the spec's Arrhenius formulas, simplified to
single first-order decays per tick (see `_tia_rate`/`_denat_rate`) — documented
as an engineering approximation in DECISIONS.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

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
    tia_k0_1: float
    tia_Ea_1: float
    tia_k0_2: float
    tia_Ea_2: float
    tia_A_fraction: float
    denat_k0: float
    denat_Ea: float
    denat_moisture_cap: float


@dataclass(frozen=True)
class OperatingSeed:
    """Initial-condition seed for `init_state` (from operating/disturbance defaults)."""

    feed_temperature: float
    feed_moisture: float
    feed_hexane: float


@dataclass
class State:
    """Persistent transient state carried between ticks (BuildSpec §7.12), one
    entry per stage, ordered as `Model.stages`."""

    T: np.ndarray  # K
    X1: np.ndarray  # moisture, kg/kg dry solid
    X2: np.ndarray  # hexane, kg/kg dry solid
    C_TIA: np.ndarray  # trypsin-inhibitor activity, fraction of initial
    S_prot: np.ndarray  # protein solubility, fraction of initial
    M: np.ndarray  # kg dry solid currently retained (bed holdup)

    def copy(self) -> "State":
        return State(
            T=self.T.copy(),
            X1=self.X1.copy(),
            X2=self.X2.copy(),
            C_TIA=self.C_TIA.copy(),
            S_prot=self.S_prot.copy(),
            M=self.M.copy(),
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


@dataclass
class Outputs:
    """PVs/KPIs for one tick (BuildSpec §9.1 PV/ node map)."""

    stage_T: dict[str, float]
    stage_X_hex_ppm: dict[str, float]
    stage_X_w_pct: dict[str, float]
    stage_TIA: dict[str, float]
    stage_Sprot: dict[str, float]
    stage_vapor_temp: dict[str, float]
    stage_level_pct: dict[str, float]
    kpi_residual_hexane_ppm: float
    kpi_meal_moisture_pct: float
    kpi_urease_proxy: float
    kpi_protein_solubility_pct: float
    kpi_steam_consumption_kg_per_t: float
    kpi_throughput_t_per_day: float


def _tia_rate(T: float, c: ModelConstants) -> float:
    k1 = c.tia_k0_1 * math.exp(-c.tia_Ea_1 / (R_GAS * T))
    k2 = c.tia_k0_2 * math.exp(-c.tia_Ea_2 / (R_GAS * T))
    return c.tia_A_fraction * k1 + (1.0 - c.tia_A_fraction) * k2


def _denat_rate(T: float, X_water: float, c: ModelConstants) -> float:
    moisture_factor = min(max(X_water / c.denat_moisture_cap, 0.0), 1.0)
    return c.denat_k0 * math.exp(-c.denat_Ea / (R_GAS * T)) * moisture_factor


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

    def init_state(self, seed: OperatingSeed) -> State:
        n = len(self.stages)
        # Bed holdup seeds at half its max capacity, matching the scenario's
        # default 50% gate_opening — starts near its own implied steady state
        # (see _stage_tau's gate normalization) so it settles quickly.
        M0 = np.array([0.5 * self._stage_M_max(s) for s in self.stages], dtype=float)
        return State(
            T=np.full(n, seed.feed_temperature, dtype=float),
            X1=np.full(n, seed.feed_moisture, dtype=float),
            X2=np.full(n, seed.feed_hexane, dtype=float),
            C_TIA=np.ones(n, dtype=float),
            S_prot=np.ones(n, dtype=float),
            M=M0,
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

    def _stage_equilibrium(
        self, stage: StageSpec, T_in: float, X1_in: float, X2_in: float, u: Inputs
    ) -> tuple[float, float, float]:
        c = self.constants
        if stage.role in DT_ROLES:
            q_ind = u.indirect_steam.get(stage.id, 0.0)
            q_dir_mass = u.direct_steam.get(stage.id, 0.0)
            q_dir = q_dir_mass * c.dH_vap_water  # W, direct steam's condensation latent heat
            Q_total = q_ind + q_dir  # W, total heat duty delivered to this tray
            m_dry = max(u.feed_flow_rate, 1e-9)  # kg/s dry solid throughput
            q_specific = Q_total / m_dry  # J/kg dry solid processed

            # Direct steam that condenses adds its mass to the moisture stream —
            # an exact mass balance, independent of what its released heat (q_dir,
            # already inside Q_total) subsequently does below.
            moisture_gain = q_dir_mass / m_dry

            # Mechanistic sequential flash / sensible-heat cascade: no fitted
            # "duty saturation curve" or hand-picked ceiling anywhere. Heat first
            # sensibly warms the meal to hexane's boiling point (if not already
            # there), then evaporates residual hexane isothermally (a pot doesn't
            # exceed its liquid's boiling point while that liquid is still
            # boiling), then sensibly warms further toward water's boiling point
            # (from the Antoine correlation — see config/builder.py, not
            # hardcoded), then evaporates moisture isothermally, then finally
            # heats further (the toasting regime) if energy still remains. Every
            # transition is bounded purely by dH_vap/cp/T_boil physical
            # properties, so e.g. a heavily-dutied sparge tray naturally reaches
            # ~100 C+ once it has fully desolventized, instead of being capped.
            C_dry = c.cp_solid + c.oil_fraction * c.cp_oil  # J/(kg dry solid.K), hexane/water-free
            C_wet_in = C_dry + X1_in * c.cp_water_liquid

            E_preheat = C_wet_in * max(c.T_boil_hexane - T_in, 0.0)
            if q_specific < E_preheat:
                T_eq = T_in + q_specific / max(C_wet_in, 1e-9)
                X1_eq = X1_in + moisture_gain
                return T_eq, min(max(X1_eq, 0.0), 1.0), X2_in
            q_r = q_specific - E_preheat

            L_hex = X2_in * c.dH_vap_hexane
            if q_r < L_hex:
                X2_eq = X2_in - q_r / c.dH_vap_hexane
                X1_eq = X1_in + moisture_gain
                return c.T_boil_hexane, min(max(X1_eq, 0.0), 1.0), max(X2_eq, 0.0)
            q_r -= L_hex

            X1_after_gain = X1_in + moisture_gain
            C_wet = C_dry + X1_after_gain * c.cp_water_liquid
            E_to_water_bp = C_wet * max(c.T_boil_water - c.T_boil_hexane, 0.0)
            if q_r < E_to_water_bp:
                T_eq = c.T_boil_hexane + q_r / max(C_wet, 1e-9)
                return T_eq, min(max(X1_after_gain, 0.0), 1.0), 0.0
            q_r -= E_to_water_bp

            L_water = X1_after_gain * c.dH_vap_water
            if q_r < L_water:
                X1_eq = X1_after_gain - q_r / c.dH_vap_water
                return c.T_boil_water, min(max(X1_eq, 0.0), 1.0), 0.0
            q_r -= L_water

            T_eq = c.T_boil_water + q_r / max(C_dry, 1e-9)
            return T_eq, 0.0, 0.0

        if stage.role is StageRole.DRYER:
            air_ratio = u.heated_air_flow / max(u.feed_flow_rate, 1e-9)
            blend = 1.0 - math.exp(-air_ratio)
            T_eq = T_in + (u.heated_air_temp - T_in) * blend
            dry_rate = 0.01 * air_ratio * max(0.0, u.heated_air_temp - T_in) / 50.0
            X1_eq = min(max(X1_in - dry_rate, 0.0), 1.0)
            X2_eq = X2_in * 0.9
            return T_eq, X1_eq, X2_eq

        if stage.role is StageRole.COOLER:
            air_ratio = u.ambient_air_flow / max(u.feed_flow_rate, 1e-9)
            blend = 1.0 - math.exp(-air_ratio)
            T_eq = T_in + (u.ambient_air_temp - T_in) * blend
            X1_eq = X1_in
            X2_eq = X2_in * 0.98
            return T_eq, X1_eq, X2_eq

        raise ValueError(f"unhandled stage role: {stage.role}")

    def step(self, x: State, u: Inputs, t: float, dt: float) -> tuple[State, Outputs]:
        x_next = x.copy()
        c = self.constants

        T_in, X1_in, X2_in = u.feed_temperature, u.feed_moisture, u.feed_hexane
        m_in = u.feed_flow_rate  # kg/s dry solid, chained bed-holdup mass balance
        for i, stage in enumerate(self.stages):
            T_eq, X1_eq, X2_eq = self._stage_equilibrium(stage, T_in, X1_in, X2_in, u)
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

            k_tia = _tia_rate(T_new, c)
            k_den = _denat_rate(T_new, X1_new, c)
            C_TIA_new = x.C_TIA[i] * math.exp(-k_tia * dt)
            S_prot_new = x.S_prot[i] * math.exp(-k_den * dt)

            x_next.T[i] = T_new
            x_next.X1[i] = X1_new
            x_next.X2[i] = X2_new
            x_next.C_TIA[i] = C_TIA_new
            x_next.S_prot[i] = S_prot_new
            x_next.M[i] = M_new

            T_in, X1_in, X2_in = T_new, X1_new, X2_new
            m_in = m_out

        return x_next, self.outputs(x_next, u)

    def outputs(self, x: State, u: Inputs) -> Outputs:
        c = self.constants
        stage_T = {s.id: float(x.T[i]) for i, s in enumerate(self.stages)}
        stage_X_hex_ppm = {s.id: float(x.X2[i] * 1.0e6) for i, s in enumerate(self.stages)}
        stage_X_w_pct = {s.id: float(x.X1[i] * 100.0) for i, s in enumerate(self.stages)}
        stage_TIA = {s.id: float(x.C_TIA[i] * 100.0) for i, s in enumerate(self.stages)}
        stage_Sprot = {s.id: float(x.S_prot[i] * 85.0) for i, s in enumerate(self.stages)}
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

        return Outputs(
            stage_T=stage_T,
            stage_X_hex_ppm=stage_X_hex_ppm,
            stage_X_w_pct=stage_X_w_pct,
            stage_TIA=stage_TIA,
            stage_Sprot=stage_Sprot,
            stage_vapor_temp=stage_vapor_temp,
            stage_level_pct=stage_level_pct,
            kpi_residual_hexane_ppm=float(x.X2[-1] * 1.0e6),
            kpi_meal_moisture_pct=float(x.X1[-1] * 100.0),
            kpi_urease_proxy=float(x.C_TIA[-1] * 100.0),
            kpi_protein_solubility_pct=float(x.S_prot[-1] * 85.0),
            kpi_steam_consumption_kg_per_t=steam_kg_per_t,
            kpi_throughput_t_per_day=throughput_t_per_day,
        )
