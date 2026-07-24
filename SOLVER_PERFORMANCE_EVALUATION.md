# Solver performance evaluation framework

## Purpose

This benchmark evaluates speed, convergence, physical validity, and zone-interface
robustness before solver optimization. It drives the steady DT solver through
piecewise-linear macro operating trajectories rather than timing one favorable
steady point.

The three fidelity levels are declared in
`benchmarks/solver_performance.yaml`:

| Level | Mesh (PHZ/FTRZ/DCZ) | Outer tolerance | Intended use |
|---|---:|---:|---|
| screening | 8/8/8 | 0.10 | broad envelope exploration |
| realtime | 20/20/20 | 0.05 | current runtime configuration |
| reference | 32/32/32 | 0.01 | accuracy and mesh-dependence check |

The supplied trajectories independently vary throughput, feed severity, steam
duty, and a combined adverse case. Each trajectory is sequential so the warm
start represents dynamically changing operation. Optional matched cold solves
distinguish a bad warm start from an infeasible or numerically broken point.

## Critical interface gates

PHZ/FTRZ and FTRZ/DCZ handovers are acceptance criteria, not only diagnostics.
Every output row contains:

- PHZ hardware-boundary placement at the final PREDESOLV tray;
- the first FTRZ finite-volume temperature and hexane changes after that boundary;
- positive FTRZ cell geometry and monotonic solid hexane transfer;
- FTRZ/DCZ solid-temperature, vapor-temperature, vapor-composition, water-flow,
  hexane-flow, and moving-boundary-length residuals;
- FTRZ result-versus-integrated-result geometry error;
- PHZ + FTRZ + DCZ total loaded-depth closure;
- DCZ scaled fixed-point residual and independent physical-result validation.

The first-cell PHZ/FTRZ changes are mesh increments, not mathematical interface
discontinuities. They should shrink in a mesh-convergence study; the actual
entry boundary is passed directly into the FTRZ solve.

## Initial measured baseline (2026-07-24)

Measurements are wall-clock results from this workstation and should be treated
as a baseline, not a cross-machine performance target.

| Matrix | Valid | Total | Median solve | Maximum solve | Maximum outer iterations |
|---|---:|---:|---:|---:|---:|
| screening, all macro trajectories, sequential warm | 18 | 20 | 1.65 s | 5.45 s | 25 |
| screening, throughput ramp | 5 | 5 | 1.21 s | 1.72 s | 17 |
| realtime, throughput ramp | 5 | 5 | 4.95 s | 10.10 s | 32 |
| reference, throughput ramp | 4 | 5 | 11.31 s | 18.56 s | 27 |

All valid results passed both handover gates. The initial sweep exposed two
important failure boundaries:

1. Low combined steam (55% jacket and 65% direct-steam factors) fails with
   `FTRZ heat-transfer driving force must be positive`. This occurs both as a
   cold steam-swing endpoint and at the adverse combined endpoint, so it is not
   merely stale warm-start state.
2. At 32 kg/s, the reference mesh fails from both warm and cold starts with the
   same FTRZ driving-force error, while screening and realtime meshes converge.
   This was a critical mesh-dependent PHZ/FTRZ feasibility discrepancy.

### Resolution of the 32 kg/s discrepancy

The failure was traced to jacket-duty allocation on the variable A.18 cells.
Each cell has its own energy-solved thickness, but previously received
`q_Iv*A*(L_previous/nz)`, as though all cells had uniform thickness. The fine
mesh therefore evaluated its transition cell with only 40.7 W of jacket duty
against 123 kW of latent/sensible demand.

Each cell now solves `dz = A.18(q_Iv*A*dz)` as a bracketed local scalar root.
The corrected cold-start cross-mesh result is:

| Level | Valid | Handovers | Wall time | Outer iterations | Meal hexane |
|---|---:|---:|---:|---:|---:|
| screening | yes | 2/2 | 2.93 s | 22 | 836 ppm |
| realtime | yes | 2/2 | 13.09 s | 57 | 491 ppm |
| reference | yes | 2/2 | 29.57 s | 263 | 716 ppm |

The feasibility discrepancy is resolved. The high reference iteration count
and non-monotonic mesh dependence of residual meal hexane remain explicit
optimization and accuracy targets.

## Performance profile after the handover fix

A deterministic `cProfile` run of the 32 kg/s reference point recorded about
120 million Python calls. Profiling overhead raised the measured solve from
roughly 30-43 s to 105 s, so cumulative percentages are more meaningful than
the instrumented wall time.

| Component | Profiled cumulative time | Calls | Interpretation |
|---|---:|---:|---|
| DCZ zone solve | 108.4 s | 307 | dominant cost, repeated by outer coupling |
| particle mass march | 34.5 s | 237,688 | largest DCZ kernel |
| particle sorption heat source | 23.6 s | 237,688 | repeated scalar thermo work |
| particle accumulation Jacobian | 20.1 s | 237,688 | repeated layer assembly |
| FTRZ zone solve | 17.8 s | 285 | secondary cost |
| FTRZ thickness evaluation | 14.7 s | 658,716 | local bracket/root evaluations |
| GAB content and slope | 17.5 s | 5,704,512 | hottest constitutive function |

The extra DT solve and 44 outer passes in these call counts come from model
initialization. The benchmarked stress solve itself requires 263 outer passes.
Its final relaxation factors were temperature `0.50`, hexane `0.34`, and water
`0.0495`; temperature residual (`0.00973 K`) was the final limiting gate at the
reference tolerance of `0.01`.

### Optimization disposition

Outer-map acceleration and partial/inexact DCZ maps were evaluated and rejected.
Both changed the convergence trajectory under difficult operating points, so
their implementations, command-line controls, tests, and generated experiment
artifacts have been removed. The production solver exposes only the exact outer
and nested maps.

The remaining path-preserving candidates, in priority order, are:

1. Compile the DCZ bed-scale vapour/water balance loop using strict float64
   semantics without fast-math.
2. Re-profile before selecting another kernel; the current dominant cost is
   the coupled DCZ solve, but its phase-active bed loop rejected a safe fast
   path.
3. Investigate equation-level causes of the 263-pass reference coupling mode
   without adding optional extrapolation paths.

Acceptance for each optimization should require identical handover validity,
no new cold/warm failures, and output differences below the declared reference
tolerances. Timing comparisons should exclude model assembly and use repeated
unprofiled runs.

### Exact constitutive-kernel deduplication

The exact DCZ map now uses derivative-only GAB/oil kernels for the particle
mass Jacobian, stores the invariant particle-shell total volume, assembles the
Jacobian directly into the tridiagonal matrix, and reuses final particle
loadings already computed by the bed mass balance.

| 32 kg/s level | Before | After | Iterations | Output change |
|---|---:|---:|---:|---:|
| realtime | 13.09 s | 11.92 s | 57 → 57 | <3e-9 ppm hexane |
| reference | 29.57 s | 27.57 / 28.44 s | 263 → 263 | <3e-8 ppm hexane |

The derivative kernel itself is about 38% faster over 500k calls. End-to-end
improvement is approximately 5-9%, with identical convergence and handover
outcomes. This optimization is always active because it preserves the exact
solver path.

### Optional compiled particle backend

With the `performance` optional dependency installed, Numba compiles the
complete particle mass and energy cascades across all DCZ cells into LLVM
machine-code kernels. They include constitutive slopes, sorption heat, matrix
assembly, Thomas solutions, rates, and bulk loading. Fast-math is disabled.

| 32 kg/s level | Optimized Python | JIT | Iterations | Output change |
|---|---:|---:|---:|---:|
| realtime | 11.92 s | 7.51 s | 57 → 57 | <4e-9 ppm hexane |
| reference | 27.57 / 28.44 s | 19.42 s | 263 → 263 | numerical noise |

Numba compilation occurs during the initialization solve and is disk-cached.
The simulator automatically falls back to the exact Python kernel when Numba
is absent. Set `DTDC_DISABLE_JIT=1` to force that fallback for diagnostics or
equivalence testing.

Adding the full energy cascade retained 57/263 outer iterations and both
handover checks. Warm-cache measurements improved further to 3.74 s realtime
and 11.23 s reference; a complete realtime JIT/Python comparison differed by
less than `1e-9 ppm` meal hexane.

### Solve-local FTRZ root warm brackets

Each cell now first brackets its implicit thickness root around the preceding
free-boundary iteration's thickness. Failure to obtain a valid local bracket
immediately invokes the original global logarithmic scan. State is never
carried between solver calls, so GUI step changes always begin globally.

The 32 kg/s realtime/reference cases retained 57/263 outer iterations, both
handover checks, and numerical-noise output differences. Warm-cache timing
improved to 2.76/7.59 s. The full screening matrix retained its established
32-valid/3-invalid outcome.

### Invariant arrays and normalized coupling residual

Read-only particle initial-state and geometry arrays are now built once per DCZ
solve and reused by both compiled cascades. Reference timing improved modestly
from 7.59 to 7.26 s without changing outputs or iterations.

The DT coupling gate is now expressed as a dimensionless maximum using the
same established physical tolerances. Shadow evaluation classified all 35
screening records identically before adoption; this is normalization, not
tolerance retuning.

The three invalid records are physical endpoint failures: minimum steam can
place the FTRZ vapor below the hexane front temperature, while the combined
32 kg/s cold/wet/high-hexane endpoint has insufficient duty for any positive
FTRZ thickness root. Both cold and warm starts agree where both are available.

Raw evidence is under `benchmarks/results/`. Repeated timing varies, so compare
distributions from multiple repetitions before optimizing.

## Running the framework

```powershell
.\.venv\Scripts\python.exe scripts\solver_performance.py
```

Useful focused runs:

```powershell
.\.venv\Scripts\python.exe scripts\solver_performance.py --level realtime --warm-only
.\.venv\Scripts\python.exe scripts\solver_performance.py --trajectory combined_extreme
```

The default writes CSV and returns nonzero when any solve is invalid, making it
suitable for a nightly or pre-release robustness job. Use `--output result.json`
for JSON.

## Optimization entry criteria

Optimization should start only after:

1. The declared operating trajectories are confirmed to match the intended
   physically feasible envelope.
2. The corrected local FTRZ duty/thickness closure remains valid across the
   full declared envelope.
3. Interface gates pass at every accepted point from both sequential warm start
   and cold fallback.
4. Realtime output error is compared point-by-point with the corrected reference
   solution.
5. Timing is repeated enough to report median and high-percentile latency rather
   than a single run.
