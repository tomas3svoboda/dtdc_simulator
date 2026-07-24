"""Run the macro-scale solver performance matrix and save machine-readable results."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dtdc_simulator.config.builder import assemble_model  # noqa: E402
from dtdc_simulator.config.loader import load_scenario  # noqa: E402
from dtdc_simulator.performance import (  # noqa: E402
    build_trajectories,
    load_performance_spec,
    run_performance_matrix,
    solver_levels,
    summarize,
    write_records,
)
from scripts.calibration_scorecard import _build_inputs  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="scenarios/soybean_default.yaml")
    parser.add_argument("--spec", default="benchmarks/solver_performance.yaml")
    parser.add_argument("--output", default="benchmarks/results/solver_performance.csv")
    parser.add_argument("--level", action="append", help="level name; repeat to select several")
    parser.add_argument("--trajectory", action="append", help="trajectory name; repeat to select")
    parser.add_argument("--point", type=int, action="append", help="point index; repeat to select")
    parser.add_argument("--warm-only", action="store_true", help="skip matched cold-start solves")
    args = parser.parse_args()

    cfg = load_scenario(args.scenario, properties_dir="properties")
    model, _ = assemble_model(cfg)
    base = _build_inputs(cfg, coamo_feed=False)
    spec = load_performance_spec(args.spec)
    levels = solver_levels(spec)
    points = build_trajectories(base, spec)
    if args.level:
        levels = tuple(level for level in levels if level.name in set(args.level))
    if args.trajectory:
        points = tuple(point for point in points if point.trajectory in set(args.trajectory))
    if args.point:
        points = tuple(point for point in points if point.point in set(args.point))
    if not levels or not points:
        raise SystemExit("selection produced an empty benchmark matrix")

    records = run_performance_matrix(model, points, levels, include_cold=not args.warm_only)
    write_records(records, args.output)
    print(json.dumps(summarize(records), indent=2))
    print(f"records: {args.output}")
    if any(not record.valid for record in records):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
