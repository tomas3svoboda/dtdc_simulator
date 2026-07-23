"""Run the ordered industry-benchmark gates without changing calibration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dtdc_simulator.benchmark import (  # noqa: E402
    delivered_heat_audit,
    design_gates,
    heat_gates,
    inventory_residence_audit,
    load_benchmark,
    output_gates,
    solver_gate,
)
from dtdc_simulator.config.builder import assemble_model  # noqa: E402
from dtdc_simulator.config.loader import load_scenario  # noqa: E402
from scripts.calibration_scorecard import (  # noqa: E402
    DRY_MEAL_PER_RAW_SOY,
    _build_inputs,
    score_dt,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", nargs="?", default="scenarios/soybean_default.yaml")
    parser.add_argument("--benchmark", default="benchmarks/coamo_industrial.yaml")
    parser.add_argument("--strict", action="store_true", help="exit non-zero on FAIL/BLOCKED gates")
    args = parser.parse_args()

    spec = load_benchmark(args.benchmark)
    cfg = load_scenario(args.scenario, properties_dir="properties")
    model, state = assemble_model(cfg)
    inputs = _build_inputs(cfg, coamo_feed=True)
    _, result = score_dt(model, cfg, inputs, solver_settings=spec["numerics"])

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

    print(f"INDUSTRY BENCHMARK: {spec['title']}")
    print(f"scenario: {args.scenario}\n")
    print("Residence definitions (do not interchange):")
    for row in residence.rows:
        print(
            f"  {row.stage_id:<5} {row.role:<10} fill={100*row.fill_fraction:5.1f}%  "
            f"inventory/flow={row.inventory_residence_min:5.2f} min  "
            f"relaxation tau={row.relaxation_tau_min:4.2f} min  T={row.temperature_C:6.1f} C"
        )
    print(
        f"  totals: inventory/flow={residence.total_inventory_residence_min:.2f} min, "
        f"hot inventory/flow={residence.hot_inventory_residence_min:.2f} min, "
        f"relaxation tau={residence.total_relaxation_tau_min:.2f} min\n"
    )

    print("Heat boundary diagnostic:")
    print(
        f"  indirect input={heat.indirect_input_MW:.3f} MW; direct input={heat.direct_input_MW:.3f} MW; "
        f"clean steam input={heat.clean_steam_input_MW:.3f} MW; "
        f"nominal direct share={100*heat.nominal_direct_fraction:.1f}%"
    )
    print(
        f"  top water vapor={heat.top_water_vapor_kg_s:.3f} kg/s; direct-water blow-through bound="
        f"{heat.direct_water_blowthrough_low_kg_s:.3f}-{heat.direct_water_blowthrough_high_kg_s:.3f} kg/s"
    )
    print(
        f"  delivered direct heat bound={heat.direct_delivered_MW_low:.3f}-"
        f"{heat.direct_delivered_MW_high:.3f} MW; delivered direct share bound="
        f"{100*heat.delivered_direct_fraction_low:.1f}-"
        f"{100*heat.delivered_direct_fraction_high:.1f}%\n"
    )
    print(
        f"  aggregate steam delivered={heat.steam_delivered_MW:.3f} MW after all top-water "
        f"carry-through; delivered steam share={100*heat.delivered_steam_fraction:.1f}%\n"
    )

    current_phase = None
    for gate in sorted(gates, key=lambda item: item.phase):
        if gate.phase != current_phase:
            current_phase = gate.phase
            print(current_phase.upper())
        note = f" -- {gate.note}" if gate.note else ""
        print(f"  [{gate.status:<7}] {gate.name}: {gate.value} (target {gate.target}){note}")

    blocked = [
        gate
        for gate in gates
        if gate.phase.startswith(("1-", "2-")) and gate.status in {"FAIL", "BLOCKED"}
    ]
    print("\nCalibration permission:", "HOLD" if blocked else "READY FOR PARAMETER FITTING")
    if blocked:
        print("Resolve Phase 1/2 gates before fitting constitutive parameters.")
    else:
        print("Phase 1/2 are credible; Phase 3 misses are now valid calibration targets.")
    if args.strict and blocked:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
