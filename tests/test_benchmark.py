from __future__ import annotations

from dataclasses import replace

import pytest

from dtdc_simulator.benchmark import (
    delivered_heat_audit,
    design_gates,
    heat_gates,
    inventory_residence_audit,
    load_benchmark,
    output_gates,
    solver_gate,
)
from dtdc_simulator.config.builder import assemble_model
from dtdc_simulator.config.loader import load_scenario
from scripts.calibration_scorecard import DRY_MEAL_PER_RAW_SOY, _build_inputs, score_dt


@pytest.fixture(scope="module")
def benchmark_case():
    cfg = load_scenario("scenarios/soybean_default.yaml", properties_dir="properties")
    model, state = assemble_model(cfg)
    inputs = _build_inputs(cfg, coamo_feed=True)
    spec = load_benchmark("benchmarks/coamo_industrial.yaml")
    _, result = score_dt(model, cfg, inputs, solver_settings=spec["numerics"])
    return model, state, inputs, result


def test_inventory_residence_is_not_relaxation_tau(benchmark_case) -> None:
    model, state, inputs, _ = benchmark_case
    audit = inventory_residence_audit(model, state, inputs)
    assert audit.total_inventory_residence_min == pytest.approx(27.805, rel=1.0e-4)
    assert audit.total_relaxation_tau_min == pytest.approx(9.0)
    assert audit.total_inventory_residence_min != audit.total_relaxation_tau_min


def test_heat_audit_subtracts_top_water_carrythrough(benchmark_case) -> None:
    model, _, inputs, result = benchmark_case
    audit = delivered_heat_audit(model, inputs, result)
    assert audit.direct_delivered_MW_high < audit.direct_input_MW
    assert audit.direct_delivered_MW_low <= audit.direct_delivered_MW_high
    assert audit.direct_water_blowthrough_low_kg_s <= audit.direct_water_blowthrough_high_kg_s
    assert abs(audit.water_balance_residual_kg_s) < 0.05


def test_default_seed_passes_all_ordered_industry_gates(benchmark_case) -> None:
    model, state, inputs, result = benchmark_case
    spec = load_benchmark("benchmarks/coamo_industrial.yaml")
    residence = inventory_residence_audit(
        model, state, inputs, float(spec["design"]["hot_contact_temperature_C"])
    )
    heat = delivered_heat_audit(model, inputs, result)
    raw_soy_kg_s = inputs.feed_flow_rate / DRY_MEAL_PER_RAW_SOY
    direct_specific = sum(inputs.direct_steam.values()) / raw_soy_kg_s * 1000.0
    gates = (
        design_gates(model, residence, spec)
        + heat_gates(heat, direct_specific, spec)
        + [solver_gate(result, spec)]
        + output_gates(result, inputs.feed_oil, spec)
    )

    assert [(gate.name, gate.status) for gate in gates if gate.status != "PASS"] == []


def test_default_phz_removal_matches_literature_range(benchmark_case) -> None:
    _, _, inputs, result = benchmark_case
    reduction = (inputs.feed_hexane - result.phz.exit_state.X2) / inputs.feed_hexane
    assert 0.10 <= reduction <= 0.25


def test_zonal_domain_uses_live_loaded_depth(benchmark_case) -> None:
    model, state, _, result = benchmark_case
    expected_loaded_depth = 0.0
    for i, stage in enumerate(model.stages):
        if stage.role.value not in {"PREDESOLV", "MAIN", "SPARGE"}:
            continue
        fill = state.M[i] / model._stage_M_max(stage)
        expected_loaded_depth += stage.bed_height_m * fill
    assert result.axial_profile.z_m[-1] == pytest.approx(expected_loaded_depth)
    assert result.axial_profile.z_m[-1] == pytest.approx(3.25)


def test_low_jacket_dcz_converges_and_is_cap_independent(benchmark_case) -> None:
    model, _, inputs, _ = benchmark_case
    cfg = load_scenario("scenarios/soybean_default.yaml", properties_dir="properties")
    low_duty = replace(
        inputs,
        indirect_steam={
            "PD1": 1.2e6,
            "PD2": 1.2e6,
            "PD3": 1.2e6,
            "MN1": 0.0,
            "MN2": 0.0,
            "SP1": 0.0,
        },
    )
    base_settings = {
        "nz_phz": 20,
        "nz_ftrz": 20,
        "nz_dcz": 20,
        "outer_tol": 0.001,
        "outer_max_iter": 300,
    }
    _, result_100 = score_dt(
        model, cfg, low_duty, solver_settings={**base_settings, "dcz_inner_max_iter": 100}
    )
    _, result_500 = score_dt(
        model, cfg, low_duty, solver_settings={**base_settings, "dcz_inner_max_iter": 500}
    )

    assert result_100.converged and result_500.converged
    assert result_100.dcz.converged and result_500.dcz.converged
    assert result_100.dcz.iterations < 100
    assert result_500.dcz.vapor_water_out_kg_s == pytest.approx(
        result_100.dcz.vapor_water_out_kg_s, abs=base_settings["outer_tol"]
    )
    audit = delivered_heat_audit(model, low_duty, result_500)
    assert abs(audit.water_balance_residual_kg_s) < 0.01


@pytest.mark.parametrize("predesolv_total_kg_s", [0.0, 2.0, 4.0])
def test_extreme_predesolv_jacket_cases_keep_ftrz_on_first_countercurrent_tray(
    benchmark_case, predesolv_total_kg_s
) -> None:
    model, _, inputs, _ = benchmark_case
    cfg = load_scenario("scenarios/soybean_default.yaml", properties_dir="properties")
    # The GUI reports jacket duty as equivalent fully-condensed supply steam.
    duty_per_tray = predesolv_total_kg_s * cfg.physical.dH_vap_water / 3.0
    duties = dict(inputs.indirect_steam)
    duties.update({stage: duty_per_tray for stage in ("PD1", "PD2", "PD3")})
    case = replace(inputs, indirect_steam=duties)
    numerics = load_benchmark("benchmarks/coamo_industrial.yaml")["numerics"]

    _, result = score_dt(model, cfg, case, solver_settings=numerics)
    first_ftrz_stage = next(
        stage
        for stage, zone in zip(result.axial_profile.stage_id, result.axial_profile.zone)
        if zone == "FTRZ"
    )

    assert result.converged
    assert result.phz.boundary_tray_index == 2
    assert first_ftrz_stage == "MN1"
    assert all(cell.dz_m > 0.0 for cell in result.ftrz.cells)
    vapor_temperatures = [cell.vapor_out.T for cell in result.ftrz.cells]
    assert vapor_temperatures == sorted(vapor_temperatures)
    assert max(vapor_temperatures) <= max(
        model.constants.dt_constants.T_direct_steam,
        result.phz.exit_state.T,
    )
    assert all(cell.sensible_heat_to_solid_w >= 0.0 for cell in result.ftrz.cells)
    if predesolv_total_kg_s == 0.0:
        assert result.phz.exit_state.T == pytest.approx(case.feed_temperature)
    if predesolv_total_kg_s == 4.0:
        assert result.phz.exit_state.T > model.constants.dt_constants.phz.T_boil_hexane
        assert "FALLING_RATE" in result.axial_profile.mechanism
        assert result.ftrz.cells[0].solid.T == pytest.approx(result.phz.exit_state.T, abs=1.0)
