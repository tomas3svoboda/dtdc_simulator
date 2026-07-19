"""`assemble_model(config) -> (Model, x0)` — the setup-phase handoff (BuildSpec §4, §3).

This is the only module allowed to depend on both `config/` (pydantic) and
`core/` (pure). It translates validated cold config into `core.model`'s plain
dataclasses and computes the initial condition.
"""

from __future__ import annotations

import math

from dtdc_simulator.config import schema
from dtdc_simulator.config.schema import AntoineParams, ScenarioConfig
from dtdc_simulator.core import dc, dt_solver, thermo
from dtdc_simulator.core.model import (
    Model,
    ModelConstants,
    OperatingSeed,
    StageRole,
    StageSpec,
    State,
)
from dtdc_simulator.core.zones import ftrz, phz
from dtdc_simulator.core.zones import particle as pt

_ATM_PRESSURE_BAR = 1.01325


def _antoine_boiling_point_k(antoine: AntoineParams, p_bar: float = _ATM_PRESSURE_BAR) -> float:
    """Solve `log10(P) = A - B/(C+T)` for T at the given pressure (K)."""
    return antoine.B / (antoine.A - math.log10(p_bar)) - antoine.C


def _thermo_gab(p: schema.GabParams) -> thermo.GabParams:
    return thermo.GabParams(Xm=p.Xm, C0=p.C0, dHC_R=p.dHC_R, K0=p.K0, dHK_R=p.dHK_R)


def _thermo_oil(p: schema.OilIsotherm) -> thermo.OilIsotherm:
    return thermo.OilIsotherm(A0=p.A0, B=p.B)


def _thermo_antoine(p: schema.AntoineParams) -> thermo.AntoineParams:
    return thermo.AntoineParams(A=p.A, B=p.B, C=p.C)


def _thermo_luikov(p: schema.LuikovParams) -> thermo.LuikovParams:
    return thermo.LuikovParams(A1=p.A1, A2=p.A2)


def _thermo_luz_drying(p: schema.LuzDryingParams) -> thermo.LuzDryingParams:
    return thermo.LuzDryingParams(
        k_a2=p.k_a2,
        k_b2=p.k_b2,
        k_a1=p.k_a1,
        k_b1=p.k_b1,
        k_c=p.k_c,
        xe_num=p.xe_num,
        xe_coef=p.xe_coef,
    )


def _build_dt_solver_constants(
    physical: schema.PhysicalParams, model: schema.ModelParams, T_boil_water: float
) -> dt_solver.DTSolverConstants:
    """Bridges validated pydantic config into `dt_solver.DTSolverConstants`
    (PHZ/FTRZ/particle sub-constants), via small shims translating pydantic
    `schema.GabParams/OilIsotherm/AntoineParams` into `thermo`'s own
    plain-dataclass equivalents (`core/` must not import pydantic, BuildSpec
    §15). Field mappings not already 1:1 (documented judgment calls, not
    bugs): `cp_ps<-cp_solid`, `cp_pg<-cp_hexane_vapor` (pore gas is
    hexane-dominated), `rho_pg<-rho_vapor_ref`."""
    gab = _thermo_gab(physical.gab_params)
    oil = _thermo_oil(physical.oil_isotherm)

    phz_c = phz.PHZConstants(
        T_boil_hexane=physical.T_boil_hexane,
        dH_vap_hexane=physical.dH_vap_hexane,
        cp_solid=physical.cp_solid,
        cp_water_liquid=physical.cp_water_liquid,
        cp_hexane_liquid=physical.cp_hexane_liquid,
        cp_oil=physical.cp_oil,
        cp_water_vapor=physical.cp_water_vapor,
        cp_hexane_vapor=physical.cp_hexane_vapor,
    )
    ftrz_c = ftrz.FTRZConstants(
        T_boil_hexane=physical.T_boil_hexane,
        T_boil_water=T_boil_water,
        dH_vap_hexane=physical.dH_vap_hexane,
        cp_water_liquid=physical.cp_water_liquid,
        gab=gab,
        oil=oil,
        antoine_water=_thermo_antoine(physical.antoine_water),
        vapor_enthalpy_ref=thermo.VaporEnthalpyRef(
            dH_vap_water=physical.dH_vap_water,
            cp_water_vapor=physical.cp_water_vapor,
            T_boil_water=T_boil_water,
            dH_vap_hexane=physical.dH_vap_hexane,
            cp_hexane_vapor=physical.cp_hexane_vapor,
            T_boil_hexane=physical.T_boil_hexane,
        ),
        alpha_pg=physical.alpha_pg,
        alpha_ps=physical.alpha_ps,
        rho_ps=physical.rho_ps,
        X3=physical.oil_fraction,
        bed_porosity=physical.bed_porosity,
        x2_critical_empirical=physical.x2_critical,
    )
    particle_c = pt.ParticleConstants(
        D_eff=model.D_eff,
        r_P=physical.particle_radius,
        Np=model.n_particle_layers,
        alpha_ps=physical.alpha_ps,
        alpha_pg=physical.alpha_pg,
        rho_ps=physical.rho_ps,
        rho_pg=physical.rho_vapor_ref,
        cp_ps=physical.cp_solid,
        cp_pg=physical.cp_hexane_vapor,
        k_ps=physical.k_ps,
        k_pg=physical.k_pg,
        X3=physical.oil_fraction,
        gab=gab,
        oil=oil,
        dH_vap_hexane=physical.dH_vap_hexane,
        sorption_C0=physical.sorption_C0,
        sorption_C1=physical.sorption_C1,
        cp_water_liquid=physical.cp_water_liquid,
        x2_critical_empirical=physical.x2_critical,
    )
    T_direct_steam = _antoine_boiling_point_k(
        physical.antoine_water, p_bar=_ATM_PRESSURE_BAR + model.direct_steam_pressure_barg
    )
    return dt_solver.DTSolverConstants(
        phz=phz_c,
        ftrz=ftrz_c,
        particle=particle_c,
        D_ax=model.D_ax,
        k_mixL=physical.k_mixL,
        rho_V=physical.rho_vapor_ref,
        cp_V=physical.cp_vapor,
        mu_V=physical.mu_vapor,
        D_HW=model.D_HW,
        T_direct_steam=T_direct_steam,
        sweep_arm_transfer_gain=model.sweep_arm_transfer_gain,
        luikov=_thermo_luikov(physical.water_luikov),
        water_diffusivity=physical.water_diffusivity,
    )


def assemble_model(config: ScenarioConfig) -> tuple[Model, State]:
    stages = tuple(
        StageSpec(
            id=s.id,
            role=StageRole(s.role.value),
            diameter_m=s.diameter_m,
            bed_height_m=s.bed_height_m,
        )
        for s in config.geometry.stages
    )
    T_boil_water = _antoine_boiling_point_k(config.physical.antoine_water)
    dc_constants = dc.DCConstants(
        cp_solid=config.physical.cp_solid,
        cp_water_liquid=config.physical.cp_water_liquid,
        dH_vap_water=config.physical.dH_vap_water,
        antoine_water=_thermo_antoine(config.physical.antoine_water),
        dc_hexane_strip_k=config.model.dc_hexane_strip_k,
        luz=_thermo_luz_drying(config.physical.water_luz_drying),
        cp_water_vapor=config.physical.cp_water_vapor,
    )
    constants = ModelConstants(
        dH_vap_hexane=config.physical.dH_vap_hexane,
        dH_vap_water=config.physical.dH_vap_water,
        T_boil_hexane=config.physical.T_boil_hexane,
        T_boil_water=T_boil_water,
        cp_solid=config.physical.cp_solid,
        cp_water_liquid=config.physical.cp_water_liquid,
        cp_oil=config.physical.cp_oil,
        oil_fraction=config.physical.oil_fraction,
        rho_solid=config.physical.rho_solid,
        bed_porosity=config.physical.bed_porosity,
        dt_constants=_build_dt_solver_constants(config.physical, config.model, T_boil_water),
        dt_nz_phz=config.model.dt_nz_phz,
        dt_nz_ftrz=config.model.dt_nz_ftrz,
        dt_nz_dcz=config.model.dt_nz_dcz,
        dt_vapor_feed_water_kg_s=config.model.dt_vapor_feed_water_kg_s,
        dt_vapor_feed_hex_kg_s=config.model.dt_vapor_feed_hex_kg_s,
        dt_vapor_feed_T=config.model.dt_vapor_feed_T,
        dc_constants=dc_constants,
        dt_outer_tol=config.model.dt_outer_tol,
        dt_outer_max_iter=config.model.dt_outer_max_iter,
        dt_dcz_inner_max_iter=config.model.dt_dcz_inner_max_iter,
    )
    model = Model(
        stages=stages,
        constants=constants,
        base_residence_s=config.model.base_residence_s,
    )

    seed = OperatingSeed(
        feed_temperature=config.disturbance_defaults.feed_temperature,
        feed_moisture=config.disturbance_defaults.feed_moisture,
        feed_hexane=config.disturbance_defaults.feed_hexane,
        feed_flow_rate=config.operating_defaults.feed_flow_rate,
        indirect_steam=dict(config.operating_defaults.indirect_steam),
        direct_steam=dict(config.operating_defaults.direct_steam),
        sweep_arm_speed=dict(config.operating_defaults.sweep_arm_speed),
    )
    # §4: "Compute steady-state x0 via initializer at the operating defaults" --
    # init_state() now runs one real solve_dt() call (M3a, BuildSpec §7.8/§7.9).
    x0 = model.init_state(seed, start_empty=config.sim.dt_start_empty)
    return model, x0
