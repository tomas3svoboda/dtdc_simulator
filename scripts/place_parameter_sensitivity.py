"""Bounded one-at-a-time sensitivity audit for uncertain DTDC inputs.

This is a release-audit tool, not a parameter fitter.  Bounds are physical or
operational uncertainty intervals documented in ``PLACE_PARAMETER_AUDIT.md``.
Numerical mesh, tolerance and relaxation settings are deliberately excluded:
they are verified by convergence studies and must never be fitted to outputs.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dtdc_simulator.benchmark import delivered_heat_audit, load_benchmark  # noqa: E402
from dtdc_simulator.config.builder import assemble_model  # noqa: E402
from dtdc_simulator.config.loader import load_scenario  # noqa: E402
from dtdc_simulator.core.model import DC_ROLES  # noqa: E402
from scripts.calibration_scorecard import _build_inputs, score_dt  # noqa: E402


@dataclass(frozen=True)
class KPIs:
    steam_pct: float
    dome_C: float
    dome_hex_pct: float
    phz_removed_pct: float
    dt_C: float
    dt_moisture_pct: float
    dt_hex_ppm: float
    dryer_C: float
    dryer_moisture_pct: float
    dryer_hex_ppm: float
    product_C: float
    product_moisture_pct: float
    product_hex_ppm: float
    water_residual_kg_s: float


Mutator = Callable[[object], None]


@dataclass(frozen=True)
class AuditCase:
    parameter: str
    bound: str
    value: str
    mutate: Mutator


def _wb_pct(X1: float, X2: float, X3: float) -> float:
    return 100.0 * X1 / (1.0 + X1 + X2 + X3)


def _stage(cfg: object, stage_id: str) -> object:
    return next(stage for stage in cfg.geometry.stages if stage.id == stage_id)


def _run(cfg: object, numerics: dict[str, object]) -> KPIs:
    model, _ = assemble_model(cfg)
    inputs = _build_inputs(cfg, coamo_feed=True)
    _, result = score_dt(model, cfg, inputs, solver_settings=numerics)
    heat = delivered_heat_audit(model, inputs, result)
    meal = result.tray_summaries[-1]

    T, X1, X2 = meal.T, meal.X1, meal.X2
    dryer = (T, X1, X2)
    product = (T, X1, X2)
    for stage in model.stages:
        if stage.role not in DC_ROLES:
            continue
        tau = model._stage_tau(stage, inputs)
        equilibrium = model._dc_equilibrium(stage, T, X1, X2, inputs, tau)
        T, X1, X2 = equilibrium[0], equilibrium[1], equilibrium[2]
        if stage.role.value == "DRYER":
            dryer = (T, X1, X2)
        elif stage.role.value == "COOLER":
            product = (T, X1, X2)

    return KPIs(
        steam_pct=100.0 * heat.delivered_steam_fraction,
        dome_C=result.axial_profile.vapor_T[0] - 273.15,
        dome_hex_pct=100.0 * result.axial_profile.vapor_hexane_frac[0],
        phz_removed_pct=(
            100.0
            * (inputs.feed_hexane - result.phz.exit_state.X2)
            / max(inputs.feed_hexane, 1.0e-12)
        ),
        dt_C=meal.T - 273.15,
        dt_moisture_pct=_wb_pct(meal.X1, meal.X2, inputs.feed_oil),
        dt_hex_ppm=1.0e6 * meal.X2,
        dryer_C=dryer[0] - 273.15,
        dryer_moisture_pct=_wb_pct(dryer[1], dryer[2], inputs.feed_oil),
        dryer_hex_ppm=1.0e6 * dryer[2],
        product_C=product[0] - 273.15,
        product_moisture_pct=_wb_pct(product[1], product[2], inputs.feed_oil),
        product_hex_ppm=1.0e6 * product[2],
        water_residual_kg_s=heat.water_balance_residual_kg_s,
    )


def _set_sorption_curve(cfg: object, exponent: float) -> None:
    # Preserve Cardarelli's 22 kJ/mol soybean net heat at W2=0.001 while
    # varying the log-log slope. Hexane molar mass is 0.08618 kg/mol.
    net_j_kg = 22_000.0 / 0.08618
    cfg.physical.sorption_C1 = exponent
    cfg.physical.sorption_C0 = net_j_kg / 0.001**exponent


def _cases() -> list[AuditCase]:
    return [
        AuditCase("cp_oil", "low", "1.8 kJ/kg/K", lambda c: setattr(c.physical, "cp_oil", 1800.0)),
        AuditCase("cp_oil", "high", "2.3 kJ/kg/K", lambda c: setattr(c.physical, "cp_oil", 2300.0)),
        AuditCase("sorption curve", "shallow", "C1=-0.35", lambda c: _set_sorption_curve(c, -0.35)),
        AuditCase("sorption curve", "steep", "C1=-0.45", lambda c: _set_sorption_curve(c, -0.45)),
        AuditCase(
            "clean bottom water",
            "low",
            "0.10 kg/s",
            lambda c: setattr(c.model, "dt_vapor_feed_water_kg_s", 0.10),
        ),
        AuditCase(
            "clean bottom water",
            "high",
            "0.40 kg/s",
            lambda c: setattr(c.model, "dt_vapor_feed_water_kg_s", 0.40),
        ),
        AuditCase(
            "clean bottom hexane",
            "low",
            "0 kg/s",
            lambda c: setattr(c.model, "dt_vapor_feed_hex_kg_s", 0.0),
        ),
        AuditCase(
            "clean bottom hexane",
            "high",
            "0.002 kg/s",
            lambda c: setattr(c.model, "dt_vapor_feed_hex_kg_s", 0.002),
        ),
        AuditCase(
            "clean bottom vapor T",
            "low",
            "363 K",
            lambda c: setattr(c.model, "dt_vapor_feed_T", 363.0),
        ),
        AuditCase(
            "clean bottom vapor T",
            "high",
            "383 K",
            lambda c: setattr(c.model, "dt_vapor_feed_T", 383.0),
        ),
        AuditCase(
            "DC base contact time",
            "low",
            "60 s",
            lambda c: setattr(c.model, "base_residence_s", 60.0),
        ),
        AuditCase(
            "DC base contact time",
            "high",
            "120 s",
            lambda c: setattr(c.model, "base_residence_s", 120.0),
        ),
        AuditCase(
            "dryer arm factor",
            "low",
            "0.80",
            lambda c: setattr(_stage(c, "DR1"), "arm_mixing_factor", 0.80),
        ),
        AuditCase(
            "dryer arm factor",
            "high",
            "1.34",
            lambda c: setattr(_stage(c, "DR1"), "arm_mixing_factor", 1.34),
        ),
        AuditCase(
            "cooler arm factor",
            "low",
            "0.50",
            lambda c: setattr(_stage(c, "CL1"), "arm_mixing_factor", 0.50),
        ),
        AuditCase(
            "cooler arm factor",
            "high",
            "0.90",
            lambda c: setattr(_stage(c, "CL1"), "arm_mixing_factor", 0.90),
        ),
        AuditCase(
            "DC hexane MTC",
            "low",
            "0.0065",
            lambda c: setattr(c.model, "dc_hexane_mtc", 0.0065),
        ),
        AuditCase(
            "DC hexane MTC",
            "high",
            "0.026",
            lambda c: setattr(c.model, "dc_hexane_mtc", 0.026),
        ),
        AuditCase(
            "dryer air temperature",
            "low",
            "343 K",
            lambda c: setattr(c.operating_defaults, "heated_air_temp", 343.0),
        ),
        AuditCase(
            "dryer air temperature",
            "high",
            "353 K",
            lambda c: setattr(c.operating_defaults, "heated_air_temp", 353.0),
        ),
        AuditCase(
            "dryer air flow",
            "low",
            "60 kg/s",
            lambda c: setattr(c.operating_defaults, "heated_air_flow", 60.0),
        ),
        AuditCase(
            "dryer air flow",
            "high",
            "100 kg/s",
            lambda c: setattr(c.operating_defaults, "heated_air_flow", 100.0),
        ),
        AuditCase(
            "cooler air flow",
            "low",
            "200 kg/s",
            lambda c: setattr(c.operating_defaults, "ambient_air_flow", 200.0),
        ),
        AuditCase(
            "cooler air flow",
            "high",
            "350 kg/s",
            lambda c: setattr(c.operating_defaults, "ambient_air_flow", 350.0),
        ),
        AuditCase(
            "ambient RH",
            "low",
            "30%",
            lambda c: setattr(c.disturbance_defaults, "ambient_relative_humidity", 0.30),
        ),
        AuditCase(
            "ambient RH",
            "high",
            "70%",
            lambda c: setattr(c.disturbance_defaults, "ambient_relative_humidity", 0.70),
        ),
    ]


def main() -> None:
    baseline_cfg = load_scenario("scenarios/soybean_default.yaml", properties_dir="properties")
    numerics = load_benchmark("benchmarks/coamo_industrial.yaml")["numerics"]
    baseline = _run(copy.deepcopy(baseline_cfg), numerics)
    print(
        "parameter,bound,value,steam_pct,dome_C,dome_hex_pct,phz_removed_pct,"
        "dt_C,dt_moisture_pct,dt_hex_ppm,dryer_C,dryer_moisture_pct,"
        "dryer_hex_ppm,product_C,product_moisture_pct,product_hex_ppm,"
        "water_residual_kg_s"
    )

    def emit(parameter: str, bound: str, value: str, k: KPIs) -> None:
        values = ",".join(f"{getattr(k, field):.6g}" for field in KPIs.__dataclass_fields__)
        print(f"{parameter},{bound},{value},{values}")

    emit("BASE", "central", "configured", baseline)
    for case in _cases():
        cfg = copy.deepcopy(baseline_cfg)
        case.mutate(cfg)
        emit(case.parameter, case.bound, case.value, _run(cfg, numerics))


if __name__ == "__main__":
    main()
