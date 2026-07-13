"""`assemble_model(config) -> (Model, x0)` — the setup-phase handoff (BuildSpec §4, §3).

This is the only module allowed to depend on both `config/` (pydantic) and
`core/` (pure). It translates validated cold config into `core.model`'s plain
dataclasses and computes the initial condition.
"""

from __future__ import annotations

import math

from dtdc_simulator.config.schema import AntoineParams, ScenarioConfig
from dtdc_simulator.core.model import (
    Model,
    ModelConstants,
    OperatingSeed,
    StageRole,
    StageSpec,
    State,
)

_ATM_PRESSURE_BAR = 1.01325


def _antoine_boiling_point_k(antoine: AntoineParams, p_bar: float = _ATM_PRESSURE_BAR) -> float:
    """Solve `log10(P) = A - B/(C+T)` for T at the given pressure (K)."""
    return antoine.B / (antoine.A - math.log10(p_bar)) - antoine.C


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
    constants = ModelConstants(
        dH_vap_hexane=config.physical.dH_vap_hexane,
        dH_vap_water=config.physical.dH_vap_water,
        T_boil_hexane=config.physical.T_boil_hexane,
        T_boil_water=_antoine_boiling_point_k(config.physical.antoine_water),
        cp_solid=config.physical.cp_solid,
        cp_water_liquid=config.physical.cp_water_liquid,
        cp_oil=config.physical.cp_oil,
        oil_fraction=config.physical.oil_fraction,
        rho_solid=config.physical.rho_solid,
        bed_porosity=config.physical.bed_porosity,
        tia_k0_1=config.model.tia_k0_1,
        tia_Ea_1=config.model.tia_Ea_1,
        tia_k0_2=config.model.tia_k0_2,
        tia_Ea_2=config.model.tia_Ea_2,
        tia_A_fraction=config.model.tia_A_fraction,
        denat_k0=config.model.denat_k0,
        denat_Ea=config.model.denat_Ea,
        denat_moisture_cap=config.model.denat_moisture_cap,
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
    )
    # §4: "Compute steady-state x0 via initializer at the operating defaults." At M0
    # (placeholder physics) init_state seeds a uniform profile; TODO(M2): replace with
    # the real steady DT solve (core/initializer.py) once §7.8 lands.
    x0 = model.init_state(seed)
    return model, x0
