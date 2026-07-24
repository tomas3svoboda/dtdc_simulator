from __future__ import annotations

from dtdc_simulator.performance import build_trajectories, solver_levels, summarize
from scripts.calibration_scorecard import _build_inputs


def test_performance_spec_expands_levels_and_trajectories() -> None:
    from dtdc_simulator.config.loader import load_scenario
    from dtdc_simulator.performance import load_performance_spec

    cfg = load_scenario("scenarios/soybean_default.yaml", properties_dir="properties")
    base = _build_inputs(cfg, coamo_feed=False)
    spec = load_performance_spec("benchmarks/solver_performance.yaml")
    levels = solver_levels(spec)
    points = build_trajectories(base, spec)

    assert [level.name for level in levels] == ["screening", "realtime", "reference"]
    throughput = [point for point in points if point.trajectory == "throughput_ramp"]
    assert [point.inputs.feed_flow_rate for point in throughput] == [18.0, 21.5, 25.0, 28.5, 32.0]
    assert throughput[0].inputs.indirect_steam == base.indirect_steam


def test_summary_handles_failed_records() -> None:
    from dtdc_simulator.performance import PerformanceRecord

    common = dict(
        level="screening", trajectory="x", fraction=0.0, start="cold",
        outer_iterations=2, dcz_iterations=3, max_dcz_scaled_residual=0.1,
        max_coupling_residual=0.1, max_coupling_scaled_residual=0.5,
        meal_temperature_C=100.0, meal_moisture=0.1,
        meal_hexane_ppm=100.0, dome_temperature_C=70.0,
        phz_ftrz_handover_valid=True, ftrz_dcz_handover_valid=True,
        phz_ftrz_temperature_step_K=0.1, phz_ftrz_hexane_step=0.001,
        coupling_solid_T_K=0.01, coupling_vapor_T_K=0.01,
        coupling_vapor_hexane_fraction=0.001, coupling_vapor_water_flow_kg_s=0.001,
        coupling_vapor_hexane_flow_kg_s=0.001, coupling_ftrz_length_m=0.001,
        final_temperature_relaxation=0.5, final_hexane_relaxation=0.5,
        final_water_relaxation=0.15,
        ftrz_geometry_error_m=0.0, total_geometry_error_m=0.0,
    )
    rows = [
        PerformanceRecord(point=0, wall_s=1.0, converged=True, valid=True, error="", **common),
        PerformanceRecord(point=1, wall_s=3.0, converged=False, valid=False, error="cap", **common),
    ]
    result = summarize(rows)[0]
    assert result["success_rate"] == 0.5
    assert result["wall_max_s"] == 3.0
