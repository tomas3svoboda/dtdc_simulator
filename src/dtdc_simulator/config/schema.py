"""Pydantic v2 config schema — cold configuration (BuildSpec §5.1, §11).

All fields are SI. Conversion to SI must happen once, before these models are
constructed (BuildSpec §15). These models are pure data + validation; they
must never import engine/, interfaces/, or asyncua.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class StageRole(str, Enum):
    PREDESOLV = "PREDESOLV"
    MAIN = "MAIN"
    SPARGE = "SPARGE"
    DRYER = "DRYER"
    COOLER = "COOLER"


DT_ROLES = {StageRole.PREDESOLV, StageRole.MAIN, StageRole.SPARGE}
DC_ROLES = {StageRole.DRYER, StageRole.COOLER}


class StageGeometry(BaseModel):
    id: str
    role: StageRole
    diameter_m: float = Field(gt=0)
    bed_height_m: float = Field(gt=0)


class Geometry(BaseModel):
    stages: list[StageGeometry]

    @property
    def n_stages(self) -> int:
        return len(self.stages)

    @property
    def stage_ids(self) -> list[str]:
        return [s.id for s in self.stages]

    @field_validator("stages")
    @classmethod
    def _non_empty_unique_ids(cls, v: list[StageGeometry]) -> list[StageGeometry]:
        if not v:
            raise ValueError("geometry.stages must not be empty")
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate stage ids in geometry.stages: {ids}")
        return v


class GabParams(BaseModel):
    """GAB isotherm, Cardarelli & Crapiste (1996) eq. [2]-[4]: `C = C0*exp(dHC_R/T)`,
    `K = K0*exp(dHK_R/T)`, `Xm` (their `Hm`) is temperature-independent — NOT a linear
    T-dependence (an earlier placeholder assumed a "linear" form; the real correlation
    is exponential/van't Hoff, per the cited paper's Table 2)."""

    Xm: float = Field(gt=0, description="kg/kg dry solid, monolayer capacity (Hm)")
    C0: float = Field(gt=0)
    dHC_R: float = Field(description="K, delta_H_C / R")
    K0: float = Field(gt=0)
    dHK_R: float = Field(description="K, delta_H_K / R")


class OilIsotherm(BaseModel):
    A0: float = Field(gt=0)
    B: float


class LuikovParams(BaseModel):
    """Modified LUIKOV (1978) water desorption isotherm, Gianini, Luz, Sousa,
    Jorge & Paraíso (2006) Table 7 — measured on soybean meal sampled directly
    from a desolventizer/toaster's own outlet. Temperature-independent by the
    cited paper's own finding (its tested range: 15-70 C)."""

    A1: float = Field(gt=0)
    A2: float = Field(gt=0)


class LuzDryingParams(BaseModel):
    """Soybean-meal air-drying correlations — Luz et al. (2010) eqs. (4)/(5),
    used by the DC (dryer/cooler) stage (core/dc.py). `k_*` are the
    falling-rate mass-transfer coefficient K(T_a, X_s) [1/s]; `xe_*` the
    temperature-dependent equilibrium-moisture isotherm X_e(T_s, ur). See
    core/thermo.py::LuzDryingParams for the full form/provenance. Signs vary
    (some coefficients are negative), so no positivity constraint here."""

    k_a2: float
    k_b2: float
    k_a1: float
    k_b1: float
    k_c: float = Field(gt=0, description="1/s, dominant constant term of K")
    xe_num: float = Field(gt=0, description="kg/kg dry solid, isotherm moisture ceiling")
    xe_coef: float = Field(gt=0, description="1/K, isotherm temperature/activity factor")


class AntoineParams(BaseModel):
    A: float
    B: float
    C: float


class HtcCorrelation(BaseModel):
    form: str = "faner"
    c: float = Field(gt=0)
    m: float
    n: float


class PhysicalParams(BaseModel):
    """PhysicalParams — BuildSpec §5.1 table. Frozen into the Model at assembly."""

    dH_vap_hexane: float = Field(gt=0, description="J/kg")
    dH_vap_water: float = Field(gt=0, description="J/kg")
    T_boil_hexane: float = Field(gt=0, description="K")
    rho_solid: float = Field(gt=0, description="kg/m3")
    rho_vapor_ref: float = Field(gt=0, description="kg/m3")
    cp_solid: float = Field(gt=0, description="J/(kg K)")
    cp_vapor: float = Field(gt=0, description="J/(kg K), mixed steam/hexane vapor (legacy mean)")
    cp_water_liquid: float = Field(gt=0, description="J/(kg K)")
    cp_water_vapor: float = Field(gt=0, description="J/(kg K), for B.6 CPVip per-component sum")
    cp_hexane_vapor: float = Field(gt=0, description="J/(kg K), for B.6 CPVip per-component sum")
    cp_hexane_liquid: float = Field(gt=0, description="J/(kg K), for B.5 CPL per-component sum")
    cp_oil: float = Field(gt=0, description="J/(kg K)")
    mu_vapor: float = Field(gt=0, description="Pa.s, vapor dynamic viscosity (B.10 Sc_p)")
    bed_porosity: float = Field(gt=0, lt=1, description="eps_b")
    particle_porosity: float = Field(gt=0, lt=1, description="eps_p")
    oil_fraction: float = Field(ge=0, description="kg/kg dry solid, X3")
    rho_ps: float = Field(gt=0, description="kg/m3")
    rho_hexane_liquid_ref: float = Field(gt=0, description="kg/m3")
    alpha_ps: float = Field(gt=0, lt=1)
    alpha_pg: float = Field(gt=0, lt=1)
    particle_radius: float = Field(gt=0, description="m, rP")
    k_ps: float = Field(gt=0, description="W/(m K), particle solid-phase thermal conductivity")
    k_pg: float = Field(gt=0, description="W/(m K), particle pore-gas thermal conductivity")
    k_mixL: float = Field(
        gt=0, description="W/(m K), porous solid/gas mixture thermal conductivity (bed scale, A.32)"
    )
    sorption_C0: float
    sorption_C1: float
    # Empirical critical solvent content X_c (constant->falling-rate transition). None ->
    # theoretical pore-saturation eq. 4. Faner et al. (2019) measured ~0.20 for soybean.
    x2_critical: float | None = Field(default=None, gt=0, lt=1)
    water_diffusivity: float = Field(
        gt=0, description="m2/s, water's own intraparticle diffusivity, DCZ's LDF equilibration rate"
    )
    gab_params: GabParams
    oil_isotherm: OilIsotherm
    water_luikov: LuikovParams
    water_luz_drying: LuzDryingParams
    antoine_hexane: AntoineParams
    antoine_water: AntoineParams
    material_name: str = ""

    @model_validator(mode="after")
    def _volume_fractions_consistent(self) -> "PhysicalParams":
        if self.alpha_ps + self.alpha_pg > 1.0 + 1e-9:
            raise ValueError(
                f"alpha_ps + alpha_pg must be <= 1 (got {self.alpha_ps + self.alpha_pg})"
            )
        return self


class ModelParams(BaseModel):
    """ModelParams — BuildSpec §5.1 table. Frozen into the Model at assembly."""

    D_eff: float = Field(gt=0, description="m2/s intraparticle hexane diffusivity")
    D_ax: float = Field(gt=0, description="m2/s axial dispersion")
    n_particle_layers: int = Field(gt=0, description="Np, Coletto uses 12")
    nz_per_zone: int = Field(gt=0)
    htc_correlation: HtcCorrelation
    D_HW: float = Field(gt=0, description="m2/s hexane-water diffusivity")
    outer_relaxation: float = Field(gt=0, le=1)
    outer_tol: float = Field(gt=0)
    outer_max_iter: int = Field(gt=0)
    sweep_arm_transfer_gain: float = Field(ge=0)
    base_residence_s: float = Field(
        gt=0, default=90.0, description="s, nominal per-stage residence at reference sweep/gate"
    )
    # --- M3a (BuildSpec §14): integrated DT solve wiring, §7.9 ---
    # NOTE: dt_resolve_interval_s lives in OperatingDefaults now, not here --
    # it's a HOT, live-tunable value (M3a follow-up, "C"), not a cold
    # constant frozen at assembly. See OperatingDefaults below.
    dt_nz_phz: int = Field(
        gt=0, description="PHZ axial cells for the real-time (not validation) solve"
    )
    dt_nz_ftrz: int = Field(gt=0, description="FTRZ axial cells for the real-time solve")
    dt_nz_dcz: int = Field(gt=0, description="DCZ axial cells for the real-time solve")
    dt_vapor_feed_water_kg_s: float = Field(
        gt=0,
        description="[PLACE] 'clean' water vapor arriving below the DT's bottom tray -- see "
        "dt_solver.py's own sparge-BC docstring for the same category of documented assumption",
    )
    dt_vapor_feed_hex_kg_s: float = Field(ge=0, description="[PLACE] as dt_vapor_feed_water_kg_s")
    dt_vapor_feed_T: float = Field(gt=0, description="K, [PLACE] as dt_vapor_feed_water_kg_s")
    dc_hexane_mtc: float = Field(
        gt=0,
        description="[PLACE] DC hexane desorption mass-transfer coefficient (core/dc.py::desorb_hexane); "
        "hexane removal rate = dc_hexane_mtc * air_flow * (y_surf - y_air)",
    )
    # SPARGE (direct) steam supply pressure, found this session: this project's own
    # `literature_sources/Svoboda_Case_for_Advanced_Process_Control_VRX-DTDC_Concept.pdf`
    # documents INDIRECT steam at 9.5 barG (~185 C) but does not state direct/sparge steam's
    # own supply pressure -- confirmed by user (the paper's own author) as ~3 barG plant
    # practice. Water's own saturation temperature at this pressure (~144 C, via the SAME
    # antoine_water correlation `config/builder.py::_antoine_boiling_point_k` already uses for
    # T_boil_water at 1 atm) is what direct steam actually mixes into DCZ's bottom BC at
    # (`core/dt_solver.py`'s `T_bottom`) -- NOT water's atmospheric boiling point, which this
    # scenario previously (incorrectly) assumed. See DECISIONS.md's "DCZ moisture balance" entry.
    direct_steam_pressure_barg: float = Field(
        gt=0, description="bar gauge, sparge/direct steam supply pressure -- see comment above"
    )
    # Steam SUPPLY-header conditions for the HMI readout (the "PARA" header on the
    # plant SCADA: ~6.9 barG / ~170 C saturated), shown for BOTH the jacket
    # (indirect) and sparge (direct) steam. Display-only -- the physics BCs use
    # `direct_steam_pressure_barg` (the post-expansion sparge contact temp) unchanged.
    steam_supply_pressure_barg: float = Field(
        gt=0, default=6.9, description="bar gauge, steam supply header (HMI readout)"
    )
    # DT solve convergence tuning (M3a follow-up, "A2"), kept SEPARATE from
    # dt_solver.solve_dt()'s own conservative validation-run defaults
    # (1e-5/100/100, unchanged) -- these real-time settings trade precision
    # far beyond the model's own placeholder-constant uncertainty for wall
    # -clock speed. Measured this session: outer_tol=1e-5/cap=100 -> ~10s;
    # outer_tol=0.05/cap=20 -> ~3s, for a ~0.7 K / ~2 ppm difference in the
    # converged answer. See DECISIONS.md's M3a follow-up entry.
    dt_outer_tol: float = Field(
        gt=0,
        description="real-time FTRZ<->DCZ outer-loop convergence tolerance (solve_dt's own "
        "default is 1e-5; this is deliberately looser, see comment above)",
    )
    dt_outer_max_iter: int = Field(
        gt=0, description="real-time FTRZ<->DCZ outer-loop iteration cap"
    )
    dt_dcz_inner_max_iter: int = Field(
        gt=0, description="real-time DCZ-own Gauss-Seidel inner-loop iteration cap"
    )


class OperatingDefaults(BaseModel):
    """Seed values for HOT MV state (BuildSpec §5.2). Not part of the frozen Model."""

    feed_flow_rate: float = Field(gt=0, description="kg/s dry solid")
    indirect_steam: dict[str, float] = Field(default_factory=dict, description="W per DT tray")
    direct_steam: dict[str, float] = Field(default_factory=dict, description="kg/s per SPARGE tray")
    sweep_arm_speed: dict[str, float] = Field(default_factory=dict, description="rpm per stage")
    gate_opening: dict[str, float] = Field(default_factory=dict, description="0-100% per stage")
    heated_air_temp: float = Field(gt=0, description="K, DRYER")
    heated_air_flow: float = Field(gt=0, description="kg/s, DRYER")
    ambient_air_flow: float = Field(gt=0, description="kg/s, COOLER")
    # M3a follow-up ("C"): HOT, live-tunable (moved out of ModelParams) --
    # the UI/OPC UA can adjust this while RUNNING via
    # RuntimeFacade.set_dt_resolve_interval_s. `ge=120` is a floor the user
    # asked for directly: the DT's own dynamics (~20-30 min residence) don't
    # meaningfully change faster than this, so resolving more often than
    # every 120 SIM-seconds buys nothing physically -- only wall-clock cost.
    dt_resolve_interval_s: float = Field(
        ge=120.0,
        description=(
            "s, SIM time between full solve_dt() re-solves (§7.9 quasi-steady map, periodic not "
            "every-tick -- see DECISIONS.md M3a entry for why). The WALL-CLOCK gap is "
            "dt_resolve_interval_s/speed_factor and must stay above solve_dt's own "
            "(hardware-dependent) wall-clock cost or the tick loop stutters every resolve."
        ),
    )


class DisturbanceDefaults(BaseModel):
    feed_temperature: float = Field(gt=0, description="K")
    feed_moisture: float = Field(ge=0, description="kg/kg dry solid")
    feed_hexane: float = Field(ge=0, description="kg/kg dry solid")
    # M4 (GUI redesign): feed oil (X3) is now a live disturbance, seeded here
    # from the same value `physical.oil_fraction` carries (its default keeps
    # scenarios that predate this field working). See core/model.py Inputs.feed_oil.
    feed_oil: float = Field(default=0.01, ge=0, description="kg/kg dry solid, X3")
    # Weather, not an operator setpoint -- COOLER's own inlet air temperature
    # (see engine/facade.py's MV->DV reclassification comment).
    ambient_air_temp: float = Field(gt=0, description="K, COOLER inlet air")
    # Relative humidity (0-1), not an absolute humidity ratio -- what a
    # weather report actually gives; converted to the absolute humidity the
    # physics needs at `ambient_air_temp` in `core/model.py::_dc_equilibrium`.
    ambient_relative_humidity: float = Field(ge=0, le=1, description="0-1, ambient RH")


class ClockKind(str, Enum):
    REALTIME = "realtime"
    FREERUN = "freerun"


class SimConfig(BaseModel):
    speed_factor: float = Field(ge=0, default=1.0)
    dt_wall_s: float = Field(gt=0, default=0.2)
    max_control_interval_s: float = Field(gt=0, default=10.0)
    clock: ClockKind = ClockKind.REALTIME
    # M3a follow-up ("B"): an engine/run-control choice, not a physical
    # constant -- seed the DT empty (no holdup, no material) and watch it
    # fill via the existing lag mechanism, instead of starting pre-solved at
    # steady state.
    dt_start_empty: bool = Field(
        default=False,
        description="seed init_state() empty (M=0, T=feed, X1=X2=0) instead of "
        "pre-solved at steady state, to watch material propagate through the unit",
    )


class ScenarioConfig(BaseModel):
    """Top-level scenario: material + geometry + model params + HOT seeds + sim config.

    This is the root document loaded from a scenario YAML file (BuildSpec §11).
    """

    material: str
    geometry: Geometry
    physical: PhysicalParams
    model: ModelParams
    operating_defaults: OperatingDefaults
    disturbance_defaults: DisturbanceDefaults
    sim: SimConfig = SimConfig()

    @model_validator(mode="after")
    def check_physical_consistency(self) -> "ScenarioConfig":
        """Cross-field sanity checks run before assembly (BuildSpec §11)."""
        stage_ids = set(self.geometry.stage_ids)
        dt_stage_ids = {s.id for s in self.geometry.stages if s.role in DT_ROLES}
        sparge_ids = {s.id for s in self.geometry.stages if s.role == StageRole.SPARGE}
        dryer_ids = {s.id for s in self.geometry.stages if s.role == StageRole.DRYER}
        cooler_ids = {s.id for s in self.geometry.stages if s.role == StageRole.COOLER}

        for key in self.operating_defaults.indirect_steam:
            if key not in dt_stage_ids:
                raise ValueError(f"indirect_steam references unknown/non-DT stage id: {key}")
        for key in self.operating_defaults.direct_steam:
            if key not in sparge_ids:
                raise ValueError(f"direct_steam references unknown/non-SPARGE stage id: {key}")
        for key in self.operating_defaults.sweep_arm_speed:
            if key not in stage_ids:
                raise ValueError(f"sweep_arm_speed references unknown stage id: {key}")
        for key in self.operating_defaults.gate_opening:
            if key not in stage_ids:
                raise ValueError(f"gate_opening references unknown stage id: {key}")

        if not dryer_ids and self.geometry.n_stages:
            pass  # DRYER/COOLER are optional at this fidelity; DC section is DECIDE-able
        if not cooler_ids and self.geometry.n_stages:
            pass

        if self.sim.speed_factor * self.sim.dt_wall_s > self.sim.max_control_interval_s:
            raise ValueError(
                "sim.speed_factor * sim.dt_wall_s exceeds sim.max_control_interval_s "
                "(BuildSpec §8.4 undersampling constraint) at scenario load time"
            )
        return self
