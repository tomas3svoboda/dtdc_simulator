"""Reproducible solver-performance evaluation over changing operating points."""

from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import yaml

from dtdc_simulator.core import dt_solver
from dtdc_simulator.core.model import Inputs, Model, _build_dt_trays, _dt_role_stages, _shaft_rpm
from dtdc_simulator.core.zones import ftrz


@dataclass(frozen=True)
class SolverLevel:
    name: str
    nz_phz: int
    nz_ftrz: int
    nz_dcz: int
    outer_tol: float
    outer_max_iter: int
    dcz_inner_max_iter: int
    outer_relaxation: float = 0.5


@dataclass(frozen=True)
class OperatingPoint:
    trajectory: str
    point: int
    fraction: float
    inputs: Inputs


@dataclass(frozen=True)
class PerformanceRecord:
    level: str
    trajectory: str
    point: int
    fraction: float
    start: str
    wall_s: float
    outer_iterations: int
    dcz_iterations: int
    converged: bool
    valid: bool
    error: str
    max_dcz_scaled_residual: float
    max_coupling_residual: float
    max_coupling_scaled_residual: float
    phz_ftrz_handover_valid: bool
    ftrz_dcz_handover_valid: bool
    phz_ftrz_temperature_step_K: float
    phz_ftrz_hexane_step: float
    coupling_solid_T_K: float
    coupling_vapor_T_K: float
    coupling_vapor_hexane_fraction: float
    coupling_vapor_water_flow_kg_s: float
    coupling_vapor_hexane_flow_kg_s: float
    coupling_ftrz_length_m: float
    final_temperature_relaxation: float
    final_hexane_relaxation: float
    final_water_relaxation: float
    ftrz_geometry_error_m: float
    total_geometry_error_m: float
    meal_temperature_C: float
    meal_moisture: float
    meal_hexane_ppm: float
    dome_temperature_C: float


def load_performance_spec(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as stream:
        spec = yaml.safe_load(stream)
    if not isinstance(spec, dict) or not spec.get("levels") or not spec.get("trajectories"):
        raise ValueError("performance spec requires nonempty 'levels' and 'trajectories'")
    return spec


def solver_levels(spec: dict[str, Any]) -> tuple[SolverLevel, ...]:
    return tuple(SolverLevel(name=name, **values) for name, values in spec["levels"].items())


def _scaled_mapping(values: dict[str, float], factor: float) -> dict[str, float]:
    return {key: value * factor for key, value in values.items()}


def _apply_changes(base: Inputs, changes: dict[str, float]) -> Inputs:
    direct_factor = float(changes.pop("direct_steam_factor", 1.0))
    indirect_factor = float(changes.pop("indirect_steam_factor", 1.0))
    sweep_factor = float(changes.pop("sweep_arm_factor", 1.0))
    allowed = {
        "feed_flow_rate",
        "feed_temperature",
        "feed_moisture",
        "feed_hexane",
        "feed_oil",
        "heated_air_temp",
        "heated_air_flow",
        "ambient_air_temp",
        "ambient_air_flow",
        "ambient_relative_humidity",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported operating changes: {sorted(unknown)}")
    return replace(
        base,
        **changes,
        direct_steam=_scaled_mapping(base.direct_steam, direct_factor),
        indirect_steam=_scaled_mapping(base.indirect_steam, indirect_factor),
        sweep_arm_speed=_scaled_mapping(base.sweep_arm_speed, sweep_factor),
    )


def build_trajectories(base: Inputs, spec: dict[str, Any]) -> tuple[OperatingPoint, ...]:
    """Expand piecewise-linear macro trajectories, including both end points."""
    points: list[OperatingPoint] = []
    for name, raw in spec["trajectories"].items():
        steps = int(raw.get("steps", 3))
        if steps < 2:
            raise ValueError(f"trajectory {name!r} must contain at least two points")
        start = raw.get("start", {})
        end = raw.get("end", {})
        keys = set(start) | set(end)
        for index in range(steps):
            fraction = index / (steps - 1)
            changes = {
                key: float(start.get(key, 1.0 if key.endswith("_factor") else getattr(base, key)))
                + fraction
                * (
                    float(end.get(key, 1.0 if key.endswith("_factor") else getattr(base, key)))
                    - float(start.get(key, 1.0 if key.endswith("_factor") else getattr(base, key)))
                )
                for key in keys
            }
            points.append(OperatingPoint(name, index, fraction, _apply_changes(base, changes)))
    return tuple(points)


def _coupling_max(result: dt_solver.DTResult) -> float:
    values = asdict(result.coupling_residuals).values()
    finite = [abs(float(value)) for value in values if math.isfinite(float(value))]
    return max(finite, default=math.inf)


def _handover_diagnostics(
    result: dt_solver.DTResult, trays: list[dt_solver.DTTray], level: SolverLevel
) -> dict[str, float | bool]:
    phz = result.phz.exit_state
    first_ftrz = result.ftrz.cells[0].solid
    last_predesolv = max(i for i, tray in enumerate(trays) if tray.role == "PREDESOLV")
    phz_ftrz_valid = (
        result.phz.boundary_tray_index == last_predesolv
        and result.phz.z_star_m > 0.0
        and result.ftrz.cells[0].dz_m > 0.0
        and first_ftrz.T >= min(phz.T, result.ftrz.solid_out.T) - 1.0e-6
        and first_ftrz.X2 <= phz.X2 + 1.0e-9
    )
    ftrz_geometry_error = abs(result.L_FTRZ_m - result.ftrz.L_FTRZ_m)
    total_loaded_depth = sum(tray.bed_height_m for tray in trays)
    total_geometry_error = abs(
        result.L_PHZ_m + result.L_FTRZ_m + result.L_DCZ_m - total_loaded_depth
    )
    residuals = result.coupling_residuals
    ftrz_dcz_valid = (
        result.converged
        and result.dcz.converged
        and result.dcz.residuals.maximum_scaled <= 1.0
        and ftrz_geometry_error <= 1.0e-3
        and total_geometry_error <= 1.0e-6
        and all(
            math.isfinite(value)
            for value in (
                residuals.solid_interface_T,
                residuals.vapor_interface_T,
                residuals.vapor_interface_hexane_fraction,
                residuals.vapor_interface_water_flow,
                residuals.vapor_interface_hexane_flow,
                residuals.ftrz_length,
            )
        )
        and max(
            residuals.solid_interface_T,
            residuals.vapor_interface_T,
            residuals.vapor_interface_hexane_fraction,
            residuals.vapor_interface_water_flow,
            residuals.vapor_interface_hexane_flow,
            residuals.ftrz_length,
        )
        <= level.outer_tol
    )
    return {
        "phz_ftrz_handover_valid": phz_ftrz_valid,
        "ftrz_dcz_handover_valid": ftrz_dcz_valid,
        # This is the first finite-volume cell change after the boundary, not
        # an asserted discontinuity at the mathematical interface.
        "phz_ftrz_temperature_step_K": abs(first_ftrz.T - phz.T),
        "phz_ftrz_hexane_step": abs(first_ftrz.X2 - phz.X2),
        "coupling_solid_T_K": residuals.solid_interface_T,
        "coupling_vapor_T_K": residuals.vapor_interface_T,
        "coupling_vapor_hexane_fraction": residuals.vapor_interface_hexane_fraction,
        "coupling_vapor_water_flow_kg_s": residuals.vapor_interface_water_flow,
        "coupling_vapor_hexane_flow_kg_s": residuals.vapor_interface_hexane_flow,
        "coupling_ftrz_length_m": residuals.ftrz_length,
        "ftrz_geometry_error_m": ftrz_geometry_error,
        "total_geometry_error_m": total_geometry_error,
    }


def _solve_one(
    model: Model,
    point: OperatingPoint,
    level: SolverLevel,
    warm_result: dt_solver.DTResult | None,
) -> tuple[PerformanceRecord, dt_solver.DTResult | None]:
    inputs = point.inputs
    constants = model.constants
    trays = _build_dt_trays(
        _dt_role_stages(model.stages),
        inputs.indirect_steam,
        inputs.direct_steam,
        model._steady_fill_fractions(inputs),
    )
    solid = dt_solver.SolidFeed(
        T=inputs.feed_temperature,
        X1=inputs.feed_moisture,
        X2=inputs.feed_hexane,
        X3=inputs.feed_oil,
        m_dry_kg_s=max(inputs.feed_flow_rate, 1.0e-9),
    )
    vapor = dt_solver.VaporFeed(
        m_water_kg_s=constants.dt_vapor_feed_water_kg_s,
        m_hex_kg_s=constants.dt_vapor_feed_hex_kg_s,
        T=constants.dt_vapor_feed_T,
    )
    warm_vapor = None
    warm_temperature = None
    if warm_result is not None:
        warm_vapor = ftrz.VaporState(
            m_water_kg_s=warm_result.dcz.vapor_water_out_kg_s,
            m_hex_kg_s=warm_result.dcz.vapor_hexane_out_kg_s,
            T=warm_result.dcz.vapor_out.T,
        )
        warm_temperature = warm_result.ftrz.solid_out.T

    started = time.perf_counter()
    result = None
    residual_log: list[
        tuple[int, dt_solver.DTCouplingResiduals, float, float, float]
    ] = []
    error = ""
    valid = False
    try:
        result = dt_solver.solve_dt(
            trays,
            solid,
            vapor,
            constants.dt_constants,
            nz_phz=level.nz_phz,
            nz_ftrz=level.nz_ftrz,
            nz_dcz=level.nz_dcz,
            outer_tol=level.outer_tol,
            outer_max_iter=level.outer_max_iter,
            dcz_inner_max_iter=level.dcz_inner_max_iter,
            outer_relaxation=level.outer_relaxation,
            warm_start_vapor_in=warm_vapor,
            warm_start_T_L_sup=warm_temperature,
            sweep_arm_rpm=_shaft_rpm(model.stages, inputs.sweep_arm_speed),
            residual_log=residual_log,
        )
        dt_solver.validate_dt_result(result, solid, constants.dt_constants)
        valid = True
    except Exception as exc:  # benchmark must record failures and continue the matrix
        error = f"{type(exc).__name__}: {exc}"
    wall_s = time.perf_counter() - started

    meal = result.tray_summaries[-1] if result and result.tray_summaries else None
    profile = result.axial_profile if result else None
    handovers = (
        _handover_diagnostics(result, trays, level)
        if result
        else {
            "phz_ftrz_handover_valid": False,
            "ftrz_dcz_handover_valid": False,
            "phz_ftrz_temperature_step_K": math.inf,
            "phz_ftrz_hexane_step": math.inf,
            "coupling_solid_T_K": math.inf,
            "coupling_vapor_T_K": math.inf,
            "coupling_vapor_hexane_fraction": math.inf,
            "coupling_vapor_water_flow_kg_s": math.inf,
            "coupling_vapor_hexane_flow_kg_s": math.inf,
            "coupling_ftrz_length_m": math.inf,
            "ftrz_geometry_error_m": math.inf,
            "total_geometry_error_m": math.inf,
        }
    )
    final_relaxations = (
        residual_log[-1][2:] if residual_log else (math.nan, math.nan, math.nan)
    )
    record = PerformanceRecord(
        level=level.name,
        trajectory=point.trajectory,
        point=point.point,
        fraction=point.fraction,
        start="warm" if warm_result is not None else "cold",
        wall_s=wall_s,
        outer_iterations=result.outer_iterations if result else 0,
        dcz_iterations=result.dcz.iterations if result else 0,
        converged=bool(result and result.converged and result.dcz.converged),
        valid=valid,
        error=error,
        max_dcz_scaled_residual=(
            float(result.dcz.residuals.maximum_scaled) if result else math.inf
        ),
        max_coupling_residual=_coupling_max(result) if result else math.inf,
        max_coupling_scaled_residual=(
            result.coupling_residuals.maximum_scaled(level.outer_tol)
            if result
            else math.inf
        ),
        **handovers,
        final_temperature_relaxation=final_relaxations[0],
        final_hexane_relaxation=final_relaxations[1],
        final_water_relaxation=final_relaxations[2],
        meal_temperature_C=float(meal.T - 273.15) if meal else math.nan,
        meal_moisture=float(meal.X1) if meal else math.nan,
        meal_hexane_ppm=float(meal.X2 * 1.0e6) if meal else math.nan,
        dome_temperature_C=float(profile.vapor_T[0] - 273.15) if profile else math.nan,
    )
    return record, result if valid else None


def run_performance_matrix(
    model: Model,
    points: Iterable[OperatingPoint],
    levels: Iterable[SolverLevel],
    *,
    include_cold: bool = True,
) -> list[PerformanceRecord]:
    """Run each trajectory in order; warm state never crosses trajectory boundaries."""
    records: list[PerformanceRecord] = []
    points = tuple(points)
    for level in levels:
        warm_by_trajectory: dict[str, dt_solver.DTResult | None] = {}
        for point in points:
            previous = warm_by_trajectory.get(point.trajectory)
            warm_record, warm_result = _solve_one(model, point, level, previous)
            records.append(warm_record)
            warm_by_trajectory[point.trajectory] = warm_result
            if include_cold and previous is not None:
                cold_record, _ = _solve_one(model, point, level, None)
                records.append(cold_record)
    return records


def write_records(records: Iterable[PerformanceRecord], path: str | Path) -> None:
    rows = [asdict(record) for record in records]
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".json":
        clean_rows = [
            {
                key: (value if not isinstance(value, float) or math.isfinite(value) else None)
                for key, value in row.items()
            }
            for row in rows
        ]
        destination.write_text(json.dumps(clean_rows, indent=2, allow_nan=False), encoding="utf-8")
        return
    with destination.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(PerformanceRecord.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(rows)


def summarize(records: Iterable[PerformanceRecord]) -> list[dict[str, Any]]:
    grouped: dict[str, list[PerformanceRecord]] = {}
    for record in records:
        grouped.setdefault(record.level, []).append(record)
    summary = []
    for level, rows in grouped.items():
        valid = [row for row in rows if row.valid]
        timings = sorted(row.wall_s for row in rows)
        summary.append(
            {
                "level": level,
                "solves": len(rows),
                "valid": len(valid),
                "success_rate": len(valid) / len(rows),
                "wall_total_s": sum(timings),
                "wall_median_s": timings[len(timings) // 2],
                "wall_max_s": max(timings),
                "outer_iterations_max": max((row.outer_iterations for row in rows), default=0),
                "dcz_iterations_max": max((row.dcz_iterations for row in rows), default=0),
                "phz_ftrz_handover_passes": sum(
                    row.phz_ftrz_handover_valid for row in rows
                ),
                "ftrz_dcz_handover_passes": sum(
                    row.ftrz_dcz_handover_valid for row in rows
                ),
            }
        )
    return summary
