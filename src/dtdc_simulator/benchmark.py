"""Industry-benchmark diagnostics, kept separate from calibration/optimisation.

The ordering is intentional:

1. physical design and inventory residence;
2. boundary mass/enthalpy accounting and delivered-heat attribution;
3. process-output fit;
4. only then parameter estimation or model-form changes.

Nothing in this module mutates a scenario or fits a coefficient.  It exposes
ambiguities as BLOCKED gates instead of allowing an optimiser to hide them in
unrelated parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dtdc_simulator.core.model import DT_ROLES, Inputs, Model, State


@dataclass(frozen=True)
class Band:
    low: float
    high: float
    central: float | None = None

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high


@dataclass(frozen=True)
class Gate:
    phase: str
    name: str
    status: str
    value: str
    target: str
    note: str = ""


@dataclass(frozen=True)
class ResidenceRow:
    stage_id: str
    role: str
    fill_fraction: float
    inventory_kg_dry: float
    inventory_residence_min: float
    relaxation_tau_min: float
    temperature_C: float


@dataclass(frozen=True)
class ResidenceAudit:
    rows: tuple[ResidenceRow, ...]
    total_inventory_residence_min: float
    hot_inventory_residence_min: float
    total_relaxation_tau_min: float
    solver_uses_live_fill: bool


@dataclass(frozen=True)
class HeatAudit:
    indirect_input_MW: float
    direct_input_MW: float
    clean_steam_input_MW: float
    nominal_direct_fraction: float
    top_water_vapor_kg_s: float
    direct_water_blowthrough_low_kg_s: float
    direct_water_blowthrough_high_kg_s: float
    direct_delivered_MW_low: float
    direct_delivered_MW_high: float
    delivered_direct_fraction_low: float
    delivered_direct_fraction_high: float
    steam_delivered_MW: float
    delivered_steam_fraction: float
    water_balance_residual_kg_s: float
    attribution_is_bounded_not_traced: bool = True


def load_benchmark(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: benchmark root must be a mapping")
    return data


def band(raw: dict[str, Any]) -> Band:
    return Band(float(raw["low"]), float(raw["high"]), raw.get("central"))


def inventory_residence_audit(
    model: Model,
    state: State,
    inputs: Inputs,
    hot_threshold_C: float = 105.0,
) -> ResidenceAudit:
    """Inventory/throughput residence, distinct from the relaxation time.

    ``State.M / dry-solid throughput`` is the physically meaningful residence
    represented by the dynamic holdup model. ``Model._stage_tau`` is reported
    beside it because it controls response and DC drying kinetics, but it must
    not be presented as a geometric residence measurement.
    """
    throughput = max(inputs.feed_flow_rate, 1.0e-12)
    rows: list[ResidenceRow] = []
    for i, stage in enumerate(model.stages):
        if stage.role not in DT_ROLES:
            continue
        capacity = model._stage_M_max(stage)
        fill = float(state.M[i] / capacity) if capacity > 0.0 else 0.0
        residence_min = float(state.M[i] / throughput / 60.0)
        rows.append(
            ResidenceRow(
                stage_id=stage.id,
                role=stage.role.value,
                fill_fraction=fill,
                inventory_kg_dry=float(state.M[i]),
                inventory_residence_min=residence_min,
                relaxation_tau_min=model._stage_tau(stage, inputs) / 60.0,
                temperature_C=float(state.T[i] - 273.15),
            )
        )
    return ResidenceAudit(
        rows=tuple(rows),
        total_inventory_residence_min=sum(row.inventory_residence_min for row in rows),
        hot_inventory_residence_min=sum(
            row.inventory_residence_min for row in rows if row.temperature_C >= hot_threshold_C
        ),
        total_relaxation_tau_min=sum(row.relaxation_tau_min for row in rows),
        solver_uses_live_fill=True,
    )


def _water_vapor_enthalpy_j_kg(T_K: float, dH_vap_water: float, cp_water_vapor: float) -> float:
    """Vapor enthalpy relative to saturated vapor at 100 C as a common datum.

    The datum cancels in inlet-minus-outlet steam terms. This deliberately
    avoids pretending that the current model has a full pressure-resolved
    steam table.
    """
    return dH_vap_water + cp_water_vapor * (T_K - 373.15)


def delivered_heat_audit(model: Model, inputs: Inputs, result: Any) -> HeatAudit:
    """Boundary estimate of direct heat delivered after top-water carry-through.

    The model does not tag water molecules by origin. We therefore bound the
    direct-steam share of top water between two transparent extremes: all
    clean boundary water exits first, or all top water is direct steam. This
    is useful for calibration screening, but remains explicitly weaker than a
    source-tagged boundary enthalpy ledger.
    """
    c = model.constants
    direct_kg_s = float(sum(inputs.direct_steam.values()))
    indirect_w = float(sum(inputs.indirect_steam.values()))
    top_flow = float(result.axial_profile.vapor_flow_kg_s[0])
    top_water = top_flow * float(result.axial_profile.vapor_water_frac[0])
    top_T = float(result.axial_profile.vapor_T[0])
    clean_water = c.dt_vapor_feed_water_kg_s

    direct_out_low = max(0.0, top_water - clean_water)
    direct_out_high = min(direct_kg_s, top_water)
    cp_water_vapor = c.dt_constants.ftrz.vapor_enthalpy_ref.cp_water_vapor
    h_direct = _water_vapor_enthalpy_j_kg(
        c.dt_constants.T_direct_steam, c.dH_vap_water, cp_water_vapor
    )
    h_top = _water_vapor_enthalpy_j_kg(top_T, c.dH_vap_water, cp_water_vapor)
    direct_input_w = direct_kg_s * h_direct
    h_clean = _water_vapor_enthalpy_j_kg(
        c.dt_vapor_feed_T, c.dH_vap_water, cp_water_vapor
    )
    clean_input_w = clean_water * h_clean
    # More attributed blow-through gives the lower delivered-duty bound.
    delivered_low_w = max(0.0, direct_input_w - direct_out_high * h_top)
    delivered_high_w = max(0.0, direct_input_w - direct_out_low * h_top)

    denom_low = indirect_w + delivered_low_w
    denom_high = indirect_w + delivered_high_w
    fraction_low = delivered_low_w / denom_low if denom_low > 0.0 else 0.0
    fraction_high = delivered_high_w / denom_high if denom_high > 0.0 else 0.0

    # Exact aggregate boundary ledger for the heat-source benchmark: all
    # externally injected water vapor belongs to the direct-steam side, and
    # all water vapor leaving at the top is chimney carry-through. Molecular
    # source tags are unnecessary for this aggregate inlet-minus-outlet
    # enthalpy balance; the bounds above remain useful diagnostics if direct
    # sparge and clean lower-boundary vapor must later be reported separately.
    steam_delivered_w = max(0.0, direct_input_w + clean_input_w - top_water * h_top)
    delivered_total_w = indirect_w + steam_delivered_w
    delivered_steam_fraction = (
        steam_delivered_w / delivered_total_w if delivered_total_w > 0.0 else 0.0
    )

    meal = result.tray_summaries[-1]
    solid_water_gain = inputs.feed_flow_rate * (meal.X1 - inputs.feed_moisture)
    water_residual = clean_water + direct_kg_s - solid_water_gain - top_water
    nominal_total = indirect_w + direct_input_w
    return HeatAudit(
        indirect_input_MW=indirect_w / 1.0e6,
        direct_input_MW=direct_input_w / 1.0e6,
        clean_steam_input_MW=clean_input_w / 1.0e6,
        nominal_direct_fraction=direct_input_w / nominal_total if nominal_total > 0.0 else 0.0,
        top_water_vapor_kg_s=top_water,
        direct_water_blowthrough_low_kg_s=direct_out_low,
        direct_water_blowthrough_high_kg_s=direct_out_high,
        direct_delivered_MW_low=delivered_low_w / 1.0e6,
        direct_delivered_MW_high=delivered_high_w / 1.0e6,
        delivered_direct_fraction_low=fraction_low,
        delivered_direct_fraction_high=fraction_high,
        steam_delivered_MW=steam_delivered_w / 1.0e6,
        delivered_steam_fraction=delivered_steam_fraction,
        water_balance_residual_kg_s=water_residual,
    )


def design_gates(
    model: Model,
    residence: ResidenceAudit,
    spec: dict[str, Any],
) -> list[Gate]:
    design = spec["design"]
    stages = [stage for stage in model.stages if stage.role in DT_ROLES]
    gates: list[Gate] = []

    def add_band(name: str, value: float, raw: dict[str, Any], unit: str = "") -> None:
        target = band(raw)
        gates.append(
            Gate(
                "1-design",
                name,
                "PASS" if target.contains(value) else "FAIL",
                f"{value:.3g}{unit}",
                f"{target.low:g}-{target.high:g}{unit}",
            )
        )

    add_band("DT stage count", float(len(stages)), design["dt_stage_count"])
    diameter_target = band(design["dt_diameter_m"])
    diameters = [stage.diameter_m for stage in stages]
    gates.append(
        Gate(
            "1-design",
            "DT tray diameter",
            "PASS" if diameters and all(diameter_target.contains(d) for d in diameters) else "FAIL",
            ", ".join(f"{d:.2f}" for d in diameters) or "none",
            f"{diameter_target.low:g}-{diameter_target.high:g} m",
            "Coletto 4 m base scaled by sqrt(dry-solid throughput) to ~5.8 m",
        )
    )
    for role_name, raw in design["role_counts"].items():
        count = sum(stage.role.value == role_name for stage in stages)
        add_band(f"{role_name} tray count", float(count), raw)
    row_by_id = {row.stage_id: row for row in residence.rows}
    for role_name, raw in design["bed_depth_m"].items():
        target = band(raw)
        matching = [stage for stage in stages if stage.role.value == role_name]
        loaded_depths = [
            stage.bed_height_m * row_by_id[stage.id].fill_fraction for stage in matching
        ]
        ok = bool(loaded_depths) and all(target.contains(depth) for depth in loaded_depths)
        values = ", ".join(f"{depth:.2f}" for depth in loaded_depths) or "none"
        gates.append(
            Gate(
                "1-design",
                f"{role_name} loaded bed depth",
                "PASS" if ok else "FAIL",
                values,
                f"{target.low:g}-{target.high:g} m",
                "declared maximum depth multiplied by live fill fraction",
            )
        )
    add_band(
        "Inventory residence",
        residence.total_inventory_residence_min,
        design["inventory_residence_min"],
        " min",
    )
    add_band(
        "Hot-contact inventory residence",
        residence.hot_inventory_residence_min,
        design["hot_contact_residence_min"],
        " min",
    )
    gates.append(
        Gate(
            "1-design",
            "Steady solver/live holdup consistency",
            "PASS" if residence.solver_uses_live_fill else "BLOCKED",
            ", ".join(f"{100 * row.fill_fraction:.0f}%" for row in residence.rows),
            "solver uses declared depth x live fill",
            "solve_dt receives the same loaded fractions represented by State.M",
        )
    )
    return gates


def heat_gates(heat: HeatAudit, direct_steam_kg_per_t_raw: float, spec: dict[str, Any]) -> list[Gate]:
    heat_spec = spec["heat"]
    target = band(heat_spec["delivered_direct_fraction"])
    steam_target = band(heat_spec["direct_steam_kg_per_t_raw"])
    return [
        Gate(
            "3-targets",
            "Delivered steam-heat fraction",
            "PASS" if target.contains(heat.delivered_steam_fraction) else "FAIL",
            f"{100 * heat.delivered_steam_fraction:.1f}%",
            f"{100 * target.low:.0f}-{100 * target.high:.0f}%",
            "all injected water-vapor enthalpy minus top water-vapor carry-through",
        ),
        Gate(
            "2-heat",
            "Steam boundary enthalpy ledger",
            "PASS" if abs(heat.water_balance_residual_kg_s) <= 0.05 else "BLOCKED",
            f"{heat.steam_delivered_MW:.3f} MW delivered",
            "closed water boundary required",
            "direct-vs-clean attribution remains bounded but is not needed for their aggregate heat source",
        ),
        Gate(
            "2-heat",
            "Specific direct steam",
            "PASS" if steam_target.contains(direct_steam_kg_per_t_raw) else "FAIL",
            f"{direct_steam_kg_per_t_raw:.2f} kg/t_raw",
            f"{steam_target.low:g}-{steam_target.high:g} kg/t_raw",
        ),
        Gate(
            "2-heat",
            "Whole-DT water boundary residual",
            "PASS" if abs(heat.water_balance_residual_kg_s) <= 0.05 else "FAIL",
            f"{heat.water_balance_residual_kg_s:+.4f} kg/s",
            "abs <= 0.05 kg/s",
        ),
    ]


def solver_gate(result: Any, spec: dict[str, Any]) -> Gate:
    inner_cap = int(spec["numerics"]["dcz_inner_max_iter"])
    fully_converged = bool(result.converged and result.dcz.iterations < inner_cap)
    return Gate(
        "2-heat",
        "Validation solver convergence",
        "PASS" if fully_converged else "BLOCKED",
        f"outer={result.outer_iterations}, inner={result.dcz.iterations}/{inner_cap}",
        "outer and inner below caps",
        "balance and calibration targets are not authoritative at an iteration cap",
    )


def output_gates(result: Any, feed_oil: float, spec: dict[str, Any]) -> list[Gate]:
    """Industrial output targets, evaluated only after design/heat gates are visible."""
    output_spec = spec["outputs"]
    profile = result.axial_profile
    meal = result.tray_summaries[-1]
    moisture_wb = 100.0 * meal.X1 / (1.0 + meal.X1 + meal.X2 + feed_oil)
    values = {
        "dome_temperature_C": float(profile.vapor_T[0] - 273.15),
        "dome_hexane_wt_pct": 100.0 * float(profile.vapor_hexane_frac[0]),
        "meal_temperature_C": float(meal.T - 273.15),
        "meal_moisture_wb_pct": moisture_wb,
        "meal_hexane_ppm": float(meal.X2 * 1.0e6),
    }
    labels = {
        "dome_temperature_C": ("Dome temperature", " C"),
        "dome_hexane_wt_pct": ("Dome hexane", " wt%"),
        "meal_temperature_C": ("DT meal temperature", " C"),
        "meal_moisture_wb_pct": ("DT meal moisture", " wt%wb"),
        "meal_hexane_ppm": ("DT meal hexane", " ppm"),
    }
    gates: list[Gate] = []
    for key, value in values.items():
        target = band(output_spec[key])
        label, unit = labels[key]
        gates.append(
            Gate(
                "3-targets",
                label,
                "PASS" if target.contains(value) else "FAIL",
                f"{value:.3g}{unit}",
                f"{target.low:g}-{target.high:g}{unit}",
            )
        )
    return gates
