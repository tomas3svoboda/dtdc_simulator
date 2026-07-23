# Placeholder and calibration-parameter audit

Date: 2026-07-23

## Scope and method

This is a bounded, one-at-a-time release audit at the named COAMO seed, not
an optimizer. Every case uses the 20/20/20 validation mesh and the same
physical design, feed, and operating point as
`benchmarks/coamo_industrial.yaml`. Mass-balance and solver-convergence gates
remain hard constraints.

Reproduce the table with:

```powershell
.\.venv\Scripts\python.exe scripts\place_parameter_sensitivity.py
```

The configured baseline is:

| KPI | Value |
|---|---:|
| Delivered steam share | 74.681% |
| Dome | 72.384 C / 90.254 wt% hexane |
| PHZ removal | 13.989% |
| DT exit | 111.729 C / 19.644%wb / 283.998 ppm |
| Dryer exit | 59.512 C / 12.610%wb / 79.175 ppm |
| Product | 31.149 C / 11.369%wb / 35.704 ppm |
| Water residual | -0.00204 kg/s |

## Constitutive and boundary uncertainty

| Quantity | Basis and audited interval | Largest observed macro-KPI response | Classification |
|---|---|---|---|
| Soybean-oil heat capacity, 2.0 kJ/kg/K | Handbook-scale estimate; 1.8-2.3 kJ/kg/K | <0.001 C and <0.001 ppm at about 1% oil | Standard estimate; negligible |
| Sorption heat, `C0=1.61e4`, `C1=-0.4` | Coletto A.31 form; exponent -0.35 to -0.45, each refit to Cardarelli's 22 kJ/mol at W2=0.001 | Dome -0.05 C; DT hexane +0.10 ppm; moisture +0.008 point | Paper-bounded derived fit; low sensitivity |
| Bottom clean-vapor water, 0.25 kg/s | Unmeasured boundary; 0.10-0.40 kg/s | Dome 69.82-74.52 C; dome hexane 91.59-88.95%; DT hexane 291-278 ppm | Genuine `[PLACE]`; high dome sensitivity and identifiable from vent data |
| Bottom clean-vapor hexane, 0.0005 kg/s | Unmeasured boundary; 0-0.002 kg/s | Dome span 0.05 C; DT hexane span 2.9 ppm | Genuine `[PLACE]`; low sensitivity |
| Bottom clean-vapor temperature, 371 K | Plausible 363-383 K | <0.05 C dome; <0.1 ppm DT hexane | Genuine `[PLACE]`; negligible at this flow |
| PHZ vapor-solid temperature handoff factor, 0.3 | No paper closure; audited at 0, 0.3, and 1.0 | No change in the solid profile or macro KPIs | Structural reporting closure; non-load-bearing |
| Particle diffusion length, 0.208 mm | Industrial flake half-thickness 0.125-0.25 mm; calibrated with paper `D_eff` fixed | Directly controls residual hexane and is therefore not independently identifiable from `D_eff` without flake-size data | Bounded calibrated geometry, not a free coefficient |
| DC hexane MTC, 0.013 | EPA AP-42 cascade anchor; 0.0065-0.026 | Product hexane 76.6-13.5 ppm; DT exit unchanged | Paper/data-anchored calibration; high product-hexane sensitivity |

The former sorption pair `C0=3e5`, `C1=-0.5` failed the order-of-magnitude
check and was replaced before this audit. No remaining constitutive value is
grossly outside its documented physical interval.

## Dryer/cooler dynamic closures

These values do not change the steady zonal DT solution. They control the
lumped dryer/cooler contact calculation and the transient relaxation, so
they require plant residence or outlet-profile data before the simulator is
used predictively away from its calibration point.

| Quantity | Audited interval | Product response | Assessment |
|---|---:|---:|---|
| Base contact time, 90 s | 60-120 s | 33.31-30.04 C; 12.89-10.39%wb; 22.6-48.2 ppm | High sensitivity; measure or identify |
| Dryer arm factor, 1.07 | 0.80-1.34 | 31.85-30.61 C; 12.16-10.73%wb; 26.6-44.6 ppm | High moisture sensitivity; calibrated closure |
| Cooler arm factor, 0.69 | 0.50-0.90 | 31.81-30.55 C; 11.62-11.15%wb | Moderate; calibrated closure |

`base_residence_s` is not the physical DT inventory residence. The benchmark
reports that separately from loaded geometry and dry-solid throughput:
27.81 min total and 20.11 min hot contact.

## Operating variables and disturbances

The following values were formerly over-tagged as `[PLACE]`, but they are
operator settings or measured disturbances, not model coefficients:

| Input interval | Product response |
|---|---|
| Dryer air temperature 343-353 K | 30.57-31.73 C; 11.41-11.33%wb; 39.7-32.0 ppm |
| Dryer air flow 60-100 kg/s | 30.52-31.58 C; 11.38-11.36%wb; 48.8-27.7 ppm |
| Cooler air flow 200-350 kg/s | 32.19-29.76 C; 11.36-11.38%wb |
| Ambient RH 30-70% | 28.32-36.28 C; 10.26-13.31%wb; 40.8-28.1 ppm |

Ambient humidity is therefore a major product-moisture disturbance and
should come from a measurement in any plant comparison.

## Release interpretation

The default case is defensible as an industry-benchmark simulator: design,
inventory residence, heat accounting, balances, solver convergence, dome,
and meal gates all pass with literature/data-bounded parameters.

It is not yet a plant-certified predictive digital twin. The clean-vapor
lower boundary and dryer/cooler contact closures remain the dominant
identification tasks. Their uncertainty is localized and observable; it
does not justify altering first-principles DT equations or hiding the
uncertainty in numerical tolerances.
