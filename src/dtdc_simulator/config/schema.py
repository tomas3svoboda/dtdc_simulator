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
    Xm: float = Field(gt=0)
    C: float = Field(gt=0)
    K: float = Field(gt=0)
    temp_dependence: str = "linear"


class OilIsotherm(BaseModel):
    A0: float = Field(gt=0)
    B: float


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
    cp_vapor: float = Field(gt=0, description="J/(kg K)")
    cp_water_liquid: float = Field(gt=0, description="J/(kg K)")
    cp_oil: float = Field(gt=0, description="J/(kg K)")
    bed_porosity: float = Field(gt=0, lt=1, description="eps_b")
    particle_porosity: float = Field(gt=0, lt=1, description="eps_p")
    oil_fraction: float = Field(ge=0, description="kg/kg dry solid, X3")
    rho_ps: float = Field(gt=0, description="kg/m3")
    rho_hexane_liquid_ref: float = Field(gt=0, description="kg/m3")
    alpha_ps: float = Field(gt=0, lt=1)
    alpha_pg: float = Field(gt=0, lt=1)
    particle_radius: float = Field(gt=0, description="m, rP")
    sorption_C0: float
    sorption_C1: float
    gab_params: GabParams
    oil_isotherm: OilIsotherm
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
    tia_k0_1: float = Field(gt=0)
    tia_Ea_1: float = Field(gt=0)
    tia_k0_2: float = Field(gt=0)
    tia_Ea_2: float = Field(gt=0)
    tia_A_fraction: float = Field(ge=0, le=1)
    denat_k0: float = Field(gt=0)
    denat_Ea: float = Field(gt=0)
    denat_moisture_cap: float = Field(gt=0)
    sweep_arm_transfer_gain: float = Field(ge=0)


class OperatingDefaults(BaseModel):
    """Seed values for HOT MV state (BuildSpec §5.2). Not part of the frozen Model."""

    feed_flow_rate: float = Field(gt=0, description="kg/s dry solid")
    feed_temperature: float = Field(gt=0, description="K")
    indirect_steam: dict[str, float] = Field(default_factory=dict, description="W per DT tray")
    direct_steam: dict[str, float] = Field(default_factory=dict, description="kg/s per SPARGE tray")
    sweep_arm_speed: dict[str, float] = Field(default_factory=dict, description="rpm per stage")
    gate_opening: dict[str, float] = Field(default_factory=dict, description="0-100% per stage")
    heated_air_temp: float = Field(gt=0, description="K, DRYER")
    heated_air_flow: float = Field(gt=0, description="kg/s, DRYER")
    ambient_air_temp: float = Field(gt=0, description="K, COOLER")
    ambient_air_flow: float = Field(gt=0, description="kg/s, COOLER")


class DisturbanceDefaults(BaseModel):
    feed_moisture: float = Field(ge=0, description="kg/kg dry solid")
    feed_hexane: float = Field(ge=0, description="kg/kg dry solid")
    ambient_temp: float = Field(gt=0, description="K")
    ambient_humidity: float = Field(ge=0, description="kg/kg")


class ClockKind(str, Enum):
    REALTIME = "realtime"
    FREERUN = "freerun"


class SimConfig(BaseModel):
    speed_factor: float = Field(ge=0, default=1.0)
    dt_wall_s: float = Field(gt=0, default=0.2)
    max_control_interval_s: float = Field(gt=0, default=10.0)
    clock: ClockKind = ClockKind.REALTIME


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
