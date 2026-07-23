# DTDC steady-state literature and industry reference

This document separates whole-tower industry targets from the Coletto (2022)
zone/particle equations. The authoritative active inputs are
`scenarios/soybean_default.yaml` plus `properties/soybean.yaml`; the named
release gates are in `benchmarks/coamo_industrial.yaml`.

## Evidence layers

1. Coletto, Bandoni & Blanco (2022), including supplementary material:
   PHZ/FTRZ/DCZ topology, governing equations, transport properties, and the
   six-tray laboratory/base arrangement.
2. Cardarelli & Crapiste (1996), Cardarelli et al. (2002), Faner et al.
   (2019), and Gianini/Luz papers: soybean sorption, diffusion, critical
   loading, moisture equilibrium, and dryer kinetics.
3. Kemper/AOCS/Svoboda and EPA AP-42: industrial tray depths, residence,
   steam use, dome behavior, and DT-dryer-cooler meal-hexane cascades.
4. The named COAMO case: feed and plant-scale output ranges used by the
   release benchmark.

Exact equation-by-equation disposition is maintained in
`GROUNDING_MATRIX.md`. Parameter bounds and macro-KPI sensitivity are in
`PLACE_PARAMETER_AUDIT.md`.

## Whole-tower reference envelope

| Point or design quantity | Literature/industry envelope |
|---|---|
| Feed wet flakes | roughly 45-60 C, solvent-rich and variable by extractor |
| PREDESOLV section | conductive/jacket heat; meal approaches the 68.7 C hexane boiling region |
| PHZ solvent removal | about 10-25%; Coletto base case 14.3% |
| First countercurrent tray | start of FTRZ after the physical PREDESOLV section |
| Hot-contact inventory residence | at least 20 min; total DT commonly 20-30 min |
| Dome | about 70-75 C for lean, efficient steam use |
| DT exit | about 105-115 C, 16-22%wb moisture, residual hexane below 500-800 ppm |
| Dryer exit | about 12-14%wb |
| Cooler/product | near ambient plus 5-10 C, about 11-12.5%wb, below 300-500 ppm hexane |
| Direct steam | about 100-150 kg/t raw soybean |
| Delivered direct-steam heat | approximately 70-80% when top water-vapor carry-through is excluded |

## Authoritative COAMO seed

All feed composition fields use kg per kg dry solid:

| Input | Value |
|---|---:|
| Dry-solid flow | 25.0 kg/s |
| Feed temperature | 322.15 K (49 C) |
| Feed moisture, X1 | 0.124 |
| Feed hexane, X2 | 0.388 |
| Feed oil, X3 | 0.0137 |
| Direct steam | 3.9 kg/s (110.45 kg/t raw) |
| Indirect duty | 2.50 MW total: 2.30 MW PREDESOLV, 0.20 MW TOAST |

The same values initialize the GUI and the strict benchmark. Feed cards and
sliders display water and hexane on a complete wet-meal basis,
`Xi / (1 + X1 + X2 + X3)`, while the solver stores dry-solid-basis values.

## Current validation result

Run:

```powershell
.\.venv\Scripts\python.exe scripts\industry_benchmark.py --strict
```

Current validation-grade result:

| Audit item | Result |
|---|---:|
| Loaded DT depths | 0.30 / 0.30 / 0.30 / 1.00 / 0.60 / 0.75 m |
| Total / hot inventory residence | 27.81 / 20.11 min |
| PHZ solvent removal | 13.99% |
| Delivered steam heat | 74.68% |
| Water residual | -0.0020 kg/s |
| Solver | converged, 11 outer / 66 inner iterations |
| Dome | 72.38 C / 90.25 wt% hexane |
| DT exit | 111.73 C / 19.64%wb / 284 ppm |
| Dryer exit | 59.51 C / 12.61%wb / 79 ppm |
| Cooler/product | 31.15 C / 11.37%wb / 36 ppm |

Every ordered design, residence, heat, balance, solver, dome, and meal gate
passes. Product values are reported as model predictions; the release gates
remain intentionally broader than a single calibrated point.

## Interpretation and limitations

The PHZ-to-FTRZ handoff is both literature-consistent and physically
defensible: PHZ is limited by the actual PREDESOLV hardware, and FTRZ accepts
the resulting boiling-point state even when its wet core remains above the
empirical critical loading. Countercurrent vapor then supplies the remaining
flash/temperature-raising duty.

Not every closure is a verbatim paper equation. Water transfer is a
documented extension to Coletto's hexane-only formulation; FTRZ V-SCAL
mixing and the A.18 heat-flux unit reconstruction remain explicit
model-form uncertainties. The bottom clean-vapor boundary and lumped
dryer/cooler contact closures require plant identification for predictive
use away from the benchmark. Numerical tolerances and mesh sizes are
verification settings and must not be fitted to plant outputs.
