# Industry benchmark calibration workflow

Calibration proceeds through ordered gates. A later phase must not compensate
for a failed or ambiguous earlier phase.

## Phase 1 — physical design and retention

1. Select a named industrial benchmark and preserve its feed/throughput basis.
2. Check tray count, role, diameter, loaded bed depth and shaft arrangement.
3. Calculate residence from dry-solid inventory divided by dry-solid flow.
4. Report the dynamic relaxation constant separately; it is not a physical
   inventory residence measurement.
5. Require the steady zonal solver's bed depth to agree with the live holdup
   represented by the dynamic model.

## Phase 2 — boundary mass and delivered-heat accounting

1. Close dry-solid, water, hexane and energy boundaries independently.
2. Record jacket duty, live-steam inlet enthalpy and top-vapor enthalpy.
3. Exclude live steam that leaves as top water vapor from delivered direct heat.
4. Do not use `steam flow * latent heat` as delivered duty without subtracting
   carry-through.
5. For the aggregate direct-steam heat source, group sparge and lower-boundary
   injected water vapor, then subtract all top water-vapor enthalpy. Source
   tracing is only required if those two injected streams must be separated.

## Phase 3 — parameter calibration

Fit only parameters with an identified physical role and defensible bounds.
Use independent targets for the PHZ, FTRZ, DCZ and dryer/cooler rather than one
aggregate objective. Solver convergence and balance residuals are hard
constraints, not soft calibration penalties.

Suggested order:

1. operating duties and boundary conditions;
2. measurable geometry/retention variables;
3. literature-bounded transport properties;
4. genuine `[PLACE]` coefficients, with uncertainty intervals.

Numerical mesh sizes, tolerances and relaxation factors are convergence-study
settings and must not be fitted to plant outputs.

## Phase 4 — model-form review

Only after Phases 1–3 pass should a persistent benchmark miss trigger changes
to first-principles equations or closures. Record the failed observable, its
sensitivity/identifiability evidence, the affected balance, and the literature
support before changing model structure.

Run the current audit with:

```powershell
.\.venv\Scripts\python.exe scripts\industry_benchmark.py --strict
.\.venv\Scripts\python.exe scripts\place_parameter_sensitivity.py
```

The benchmark definition is `benchmarks/coamo_industrial.yaml`.
`PLACE_PARAMETER_AUDIT.md` records physical intervals, macro-KPI
sensitivity, and which formerly tagged values are numerical settings or
operating/disturbance seeds rather than calibratable coefficients.
