# DECISIONS.md

Log of `DECIDE` choices made while building the DTDC simulator, per
`Specifications/DTDC_Simulator_BuildSpec.md`. Newest entries at the top.

## Equipment envelope locked; DC steam-drying trays not supported (2026-07-24)

Foundation for the strict/superset OPC UA interface: a fixed *equipment
envelope* (`envelope.yaml`, rationale in
`app_specifications/DTDC_Equipment_Envelope.md`) defines the largest realistic
DTDC the address space is built against. Caps are literature-derived (Kemper
2019, Ch. 4, `literature_sources/Kemper_Solvent_Extraction.pdf`): PREDESOLV ≤ 7
(p.109), MAIN/countercurrent ≤ 4 (p.111), SPARGE = 1 (p.111), DRYER/air-drying
≤ 3, COOLER/air-cooling ≤ 2 → 17 canonical stage slots. Cross-checked against
the Paraíso/COAMO 7-stage DT and Coletto's 6-tray base case.

**This version does NOT support DC steam-drying trays.** Kemper p.114 documents
conductive/jacketed DC steam-drying trays (0–5), but the model's DC is
air-contact only (`core/dc.py`), so `DRYER` maps to Kemper's air-drying trays
and there is no `STEAM_DRYER` role/zone. Deferred rather than reserved as
always-inactive nodes: reserving nodes for unmodeled physics is speculative,
and the envelope is versioned so a later addition is explicit and traceable.

## Jacket-driven falling-rate PREDESOLV and extreme-operation envelope (2026-07-23)

**Physical distinction.** The critical loading `X2_cr` is a particle-mechanism
transition, not a tray-hardware boundary. Direct steam cannot penetrate the
sealed PREDESOLV beds; FTRZ is therefore pinned to the top of MN1, the first
countercurrent tray, for every jacket duty. All PREDESOLV trays are always
marched before that handoff.

**New falling-rate closure.** Above `X2_cr`, jacket heat produces the existing
Coletto sensible/constant-rate sequence. Below it, Faner (2019) eqs. 6--7
provide the receding wet-core diameter and dry-shell heat resistance. The
fraction `U/h` of delivered jacket heat reaches pore solvent as latent heat;
the balance heats the matrix. Faner's source was convective superheated
hexane. Driving the same particle resistance with Coletto's delivered jacket
heat density is this model's new, explicitly labelled coupling, not a claim
that either paper published a jacket model.

**Cold handoff.** A zero/weak jacket no longer raises a PHZ feasibility error.
FTRZ receives the actual cold meal at MN1 and its thickness equation includes
the sensible duty required to reach hexane boiling. Strong jacket operation
can remove free solvent, enter the pore-limited regime, and heat the matrix
above the 68.75 C plateau while a receding wet core remains.

**Extreme-case verification.** With all other COAMO benchmark inputs fixed,
PREDESOLV equivalent jacket-steam totals of 0, 2, and 4 kg/s all converge and
start FTRZ in MN1. The PHZ outlets are respectively 49.0 C / X2=0.388,
69.7 C / X2=0.0765, and 125.7 C / X2=0.0260; FTRZ thicknesses are 5.95,
1.87, and less than 0.001 mm. In the severe case, stored matrix heat supplies
the remaining pore evaporation and collapses the steam-driven front toward
zero thickness. These endpoints intentionally span cold startup through
severe over-jacketing and are regression-tested. The mechanism is emphasized
in `paper/main.tex` as an off-design digital-twin contribution relevant to
ramp-up, ramp-down, shutdown/restart, and product change.

**Operator diagnostics.** The axial profile now records a local mechanism
(`SENSIBLE`, `CONSTANT_RATE`, `FALLING_RATE`, `STEAM_FLASH`, or
`DIFFUSION_CONTROLLED`) in addition to the hardware zone. Meal-profile
moisture is displayed on an explicit wet basis. A failed periodic solve keeps
the last successful profile and reports its true age separately from the last
attempt, preventing a stale curve from being labelled freshly resolved.

**Overheated-vapor correction.** The first signed-sensible implementation
subtracted the complete `m_dot*cp*(Tb-T_PHZ,out)` term from the vapor energy
balance. For an overheated meal this became a large negative sink, so the same
stored matrix heat both funded hexane latent heat and was added again to the
vapor. When the FTRZ collapsed to nanometre-scale cells, the duplicated
4.2 MW credit drove a mesh-marched vapor spike above 1400 C. The ledgers are
now separated: cold-meal sensible heat is debited from vapor, whereas
hot-matrix energy is capped at the remaining latent requirement and acts only
as an internal wet-core credit. Excess bulk superheat is carried into DCZ.
The minimum collapsed-zone thickness is divided by `nz`, making its total
mesh-independent. Regression tests require positive thicknesses, monotone
vapor temperature, no negative vapor sensible term, and a vapor temperature
below the hottest physical inlet. The 4 kg/s case now spans 110.27--111.68 C
in FTRZ vapor while retaining about 125 C in the bulk matrix.

## Release audit: authoritative seed, oil basis, and bounded placeholders (2026-07-23)

**Authoritative initialization.** The GUI scenario had drifted away from the
COAMO benchmark used by `industry_benchmark.py`: it still initialized the
generic 56.9 C / 0.08 water / 0.5869 hexane / 0.01 oil feed on a coarse
10/10/8 mesh. The active scenario and its documentation mirror now use the
named COAMO dry-solid-basis feed (49 C, X1=0.124, X2=0.388, X3=0.0137) and
the validation-qualified 20/20/20 mesh. Assembly now starts from a converged
DT solution rather than showing `SolverStress` at time zero.

**Live oil consistency.** The model accepted `feed_oil` as a GUI/OPC UA
disturbance but FTRZ and particle constants retained assembly-time X3.
`OperatingSeed` now carries oil, and every DT solve makes local immutable
copies of the FTRZ/particle constants with the live X3. A regression test
proves the live feed value overrides a deliberately stale constant without
mutating the caller's constants.

**Sorption heat correction.** The former placeholder
`C0=3e5, C1=-0.5` was outside the published soybean caloric order of
magnitude. The replacement `C0=1.61e4, C1=-0.4` is a transparent two-point
fit to Cardarelli & Crapiste's net heat: 22.0 kJ/mol at W2=0.001 and
3.5 kJ/mol at W2=0.1. It is paper-bounded, not fitted to process outputs.

**Diffusion length and benchmark.** With the COAMO oil basis propagated
consistently, `particle_radius=0.208 mm` remains within the 0.125-0.25 mm
industrial-flake half-thickness band and gives 284 ppm at DT exit. The
strict benchmark passes all ordered gates: 27.81 min total / 20.11 min hot
inventory residence, 74.68% delivered-steam heat, 72.38 C / 90.25 wt%
hexane dome, 111.73 C / 19.64%wb / 284 ppm DT meal, -0.0020 kg/s water
residual, and 11 outer / 66 inner iterations.

**Parameter classification.** Numerical meshes/tolerances are numerical
verification settings; duties, air flows, gate positions, and weather are
operating/disturbance seeds; they are not constitutive `[PLACE]` constants.
The genuine unresolved boundary is the clean vapor admitted below SP1.
Dryer/cooler base contact time and arm factors are calibrated dynamic
closures whose extrapolative uncertainty is recorded in
`PLACE_PARAMETER_AUDIT.md`.

**GUI control model.** Per-tray jacket sliders were removed from the HMI.
The HMI now exposes only `Predesolv jacket` and `Toast jacket` zone totals;
the facade atomically preserves the internal tray split and honors actuator
limits, while per-tray MVs remain available to OPC UA. Feed moisture and
hexane controls/readouts now use one complete wet-basis denominator including
water, hexane, and oil.

## PHZ hardware-boundary refactor and industry seed (2026-07-23)

**Trigger and root cause.** Reducing indirect heat below about 3.4 MW did not
expose a DCZ instability: the integrated solver stopped before entering its
coupled loop because it required PHZ to reach Faner's `X2_cr=0.20` regardless
of the physical tray role. Residence time could not affect this algebraic
heat requirement. It therefore extended jacket-only predesolventizing into
the countercurrent section and structurally over-attributed heat to the
jacket.

**Refactor.** PHZ was first capped at the last contiguous PREDESOLV tray.
The subsequent falling-rate refinement documented above made that cap a
strict hardware boundary: `X2_cr` now changes the internal jacket-driven
mechanism and never ends PREDESOLV early. FTRZ receives the actual outlet and
starts at MN1. FTRZ jacket density is evaluated over its live free-boundary
length, and FTRZ/DCZ domain and tray reporting support boundaries crossing
any countercurrent tray.

**Ordered design correction.** The nominal SPARGE loaded depth was increased
from 0.60 to 0.75 m, still within the 0.60-1.10 m industrial range. This moves
hot-contact inventory residence from 18.8 to 20.1 min and total DT inventory
residence from 26.5 to 27.8 min, inside their respective 20-30 min gates.

**Post-refactor seed.** Total indirect duty is 2.50 MW: 2.30 MW equally
distributed over PD1-PD3 and 0.20 MW over MN1/MN2/SP1 in the retained 1:1:0.4
ratio. `particle_radius=0.208 mm` remains within the industrial-flake
half-thickness band, and lower-boundary clean water is 0.25 kg/s. At the
validation mesh the PHZ removes about 14% of feed solvent, delivered steam
supplies 74.7% of delivered heat after chimney carry-through, and the solver
converges in 11 outer / 66 inner iterations. Dome is 72.4 C / 90.3 wt%
hexane; DT meal is 111.7 C / 19.6 wt%wb / 284 ppm. Water residual is
-0.0020 kg/s. Every ordered industry benchmark gate passes.

## DCZ explicit component-flow reformulation (2026-07-23)

**Trigger.** After finite-rate FTRZ water transfer, lowering jacket duty exposed a DCZ inner-loop
failure: temperature and residual hexane stabilized quickly, but moisture/top-water flow entered a
three-cycle. Raising the cap from 100 to 2000 changed the reported steam share from 67.6% to 71.2%
while the whole-DT water residual worsened from +0.036 to +0.501 kg/s. Iteration count had become
an accidental calibration knob.

**Root cause.** DCZ marched only hexane mass fraction `wV2` at a fixed total vapor flow. Water
condensation/sorption changed the solid moisture and `wV2`, but did not change local total kg/s,
velocity or vapor heat capacity. The DT handoff then reconstructed a different total flow from the
solid moisture change. The lagged, mutually-exclusive condensation/isotherm branches added a hard
active-set switch, producing the observed three-cycle at low jacket heat.

**Reformulation.** DCZ now marches explicit `m_water` and `m_hexane` profiles bottom-to-top.
Condensation plus finite-rate sorption debit water directly; vapor hexane gain is exactly the
dry-solid-flow-scaled conserved particle `X2` loss. Local total flow sets the vapor velocity/heat
capacity. Composition is derived from the component flows, never used to reconstruct them. The
FTRZ handoff, axial profile, warm-start cache and independent balance diagnostics all consume the
solved outlet component flows.

**Water active-set stabilization.** Bulk condensation is under-relaxed as an active-set mass and
is additive with finite-rate isotherm adjustment, matching FTRZ bookkeeping. The same-pass water
transfer is marched immediately rather than one iteration later. An actively condensing saturated
cell cannot immediately evaporate its new free condensate against the clamped bound-water
isotherm. This removes the discontinuous three-cycle without tuning a constitutive coefficient.

**Verification.** Isolated DCZ water closes to machine precision and hexane to ~3.5e-5 kg/s.
At the 3.6 MW PHZ-only point the validation solve is cap-independent and converges in 44 inner /
12 outer iterations: delivered steam heat 68.0%, dome 68.5 C / 92.2 wt% hexane, meal 111.7 C /
20.0%wb / 71 ppm, whole-DT water residual -0.0012 kg/s. The preserved 7.4 MW baseline converges
in 15/11 with -0.0021 kg/s residual.

**Consequence for calibration.** The previous D8 numerical blocker is resolved, but 70-80%
delivered steam heat is still not reachable by jacket reduction alone: 3.4 MW and below cannot
finish the PHZ (`X2` never reaches `X2_cr`), while the lowest feasible tested point, 3.6 MW, gives
68.0%. The next discussion is therefore the PHZ heat/residence/critical-boundary physics, not DCZ
iteration tuning.

## Finite-rate FTRZ water transfer (2026-07-22)

**Why.** The industry-gated low-jacket sweep isolated the FTRZ water extension's
instantaneous jump to `Xe(a_w)` as a model-form hurdle. That closure had no contact time and made
the FTRZ water response insensitive to the physical flake diffusivity already used in the DCZ.

**Closure.** Each FTRZ cell now relaxes analytically toward its local Luikov equilibrium over its
own solid residence, `dt_cell = dz/u_L`, with
`k_int = 15 D_water/r_P^2`, `k_ext = hM*aV`, and
`k_overall = (1/k_int + 1/k_ext)^-1`. Thus
`X1_out = X1_in + [1-exp(-k_overall*dt_cell)](Xe-X1_in)`. This introduces no fitted multiplier:
`D_water`, `r_P`, `hM`, `aV`, density, porosity and live geometry already belong to the physical
model. Internal diffusion dominates at the current values, as expected from the prior DCZ audit.

**Conservation/bookkeeping.** V-SAT bulk condensation remains the root-solved immediate event;
finite-rate isotherm sorption is a separate signed flow. The solid receives both exactly once,
only sorption is post-debited from the already-marched vapor, and its vapor enthalpy transfer is
included in the independent FTRZ balance diagnostic. Focused tests cover rate bounds,
diffusivity response, zero-rate bulk condensation, and mass/energy closure.

**Coupling defect exposed and fixed.** The FTRZ/DCZ outer loop formerly checked temperature and
vapor composition but not component flow. A warm start could therefore be declared converged
while water kg/s was still materially wrong. Water and hexane flow residuals now participate in
the convergence gate. The hot-start disturbance seed moved from 7% to 8% dry-basis moisture,
still centered in its cited 5-10% inlet range; its dry-basis hexane seed was correspondingly
recomputed from 35% wet-basis to 0.5869. No heat duty or constitutive coefficient was retuned.

**Industry result, not a calibration.** At the preserved 7.4 MW jacket baseline, the validation
solve converges in 11 outer / 11 DCZ iterations, closes water to +0.0023 kg/s, and gives 47.5%
delivered steam heat, dome 75.9 C / about 88 wt% hexane, meal 111.7 C / 18.9%wb / 20.5 ppm.
The FTRZ moisture gain is now only about 0.0034 kg/kg dry solid rather than an equilibrium jump.
At 3.6 MW concentrated in PHZ, the directional conflict improves (68.8% steam heat, dome 66.4 C /
93.1 wt% hexane, meal 19.9%wb / 59.8 ppm), but the DCZ hits its 500-iteration cap and the water
residual is +0.175 kg/s. Those low-duty outputs are therefore blocked, not calibration evidence;
the next hurdle is DCZ low-duty water coupling/convergence, not another FTRZ coefficient fit.

## Industry benchmark gates: live loaded depth, six-tray design, and delivered steam heat (2026-07-22)

**Why this precedes another calibration.** The former scorecard called the sum of six
first-order response constants a physical residence time and reported ~6 min. That was the wrong
quantity: physical residence is dry-solid inventory / dry-solid throughput. At the former
geometry and 50% fill it was ~22 min. More importantly, `solve_dt()` always used the declared
full bed depths while the dynamic holdup state represented 50% fill, so the steady PDE and live
inventory described different equipment.

**Design correction.** Restored Coletto's six-tray DT (3 PREDESOLV, 2 MAIN, 1 SPARGE).
Coletto's 4 m diameter corresponds to ~11.9 kg/s dry solid (Fig. 1); scaling diameter with
`sqrt(flow)` to this scenario's 25 kg/s gives ~5.8 m, validating the 6 m industrial shell.
`StageSpec.bed_height_m` remains maximum holdup depth; `_build_dt_trays()` now multiplies it by
live `M/M_max`, giving Coletto's loaded 0.3/0.3/0.3/1.0/0.6/0.6 m profile at nominal 50% fill.

**Density-basis correction.** The first audit still over-reported residence 2x: `_stage_M_max`
used `(1-eps_b)*rho_ps`, while Coletto eq. (2) and DCZ velocity use
`alpha_ps*(1-eps_b)*rho_ps`. Added the missing particle solid fraction. The inventory and zonal
transport bases now agree: 26.52 min total and 21.39 min at tray exits >=105 C, within the 20-30
min industrial range. Offline scorecards derive the same fill from the gate/discharge law;
runtime re-solves use actual holdup.

**No hidden duty retune.** The old 7.4 MW jacket total was preserved during design restoration:
5.0 MW PREDESOLV, 2.0 MW MAIN, 0.4 MW SPARGE, merely distributed across the restored trays. A
validation-grade benchmark mesh/tolerance is now separate from real-time numerics. At that rigor
the water boundary closes to 0.045 kg/s and the design baseline gives dome 68.0 C / 92.4 wt%
hexane, meal 111.7 C / 20.0%wb / 20 ppm.

**Validation convergence gate.** The first benchmark used `outer_tol=1e-5` for a single norm
combining Kelvin and vapor mass fraction; it hit the outer cap even at 300 iterations while the
DCZ inner loop converged. A tolerance study at the 20/20/20 validation mesh showed `0.05` converges
in 13 outer passes, while `0.01`/`0.001` stall at 100 with only 0.27 K, 0.012 moisture percentage
point and 0.15 ppm output difference. The benchmark therefore uses 0.05 K-equivalent tolerance
and now blocks calibration explicitly if either loop reaches its cap; numerical settings are not
fitted to plant outputs.

**Delivered heat definition.** The benchmark groups all externally injected water vapor (sparge
+ lower-boundary makeup) as the direct-steam heat source and subtracts the enthalpy of all water
vapor leaving the top. This excludes chimney carry-through instead of calling every kg/s times
latent heat "delivered." The current delivered steam share is 50.0%, versus the literature
70-80% target. Separate direct-vs-clean water attribution is retained as a bound but is not needed
for this aggregate boundary balance. The corrected design baseline delivers 51.3% by steam.

**First Phase-3 sweep (not committed as calibration).** Keeping measured direct steam fixed,
3.6 MW jacket duty concentrated in the PHZ reaches 71.2% delivered-steam heat and converges,
but produces dome 64.7 C / 97.2% hexane, 21.3%wb meal and 47 ppm hexane. The 3.8 MW point reaches
70.0% but hits the DCZ inner cap. Earlier screening established that DCZ water diffusivity barely
moves the dome/moisture conflict. That isolates the next hurdle in the instantaneous FTRZ
water-equilibrium closure rather than justifying a blind coefficient fit; particle radius can be
revisited later to lift the now-over-stripped residual into its 100-500 ppm band.

## DT moisture: evaporative-pinning water model + binary-VLE floor + calibration (2026-07-22)

**Trigger.** After the Coletto-faithful DCZ rework, the DT still would not raise the meal
moisture: the FTRZ condensed ~zero water and the DCZ *dried* the meal to ~8 %wb (vs the
16-21 % industrial target). Root cause (confirmed by dumping the FTRZ cells): the water
condensation was keyed to the **bulk vapor** temperature (superheated, ~106-120 °C, above its
dew point → never condenses), while the physical mechanism is film condensation onto the
**cold meal surface** (68-108 °C, far below the vapor dew point — A&G / Kemper / Paraíso;
Gianini 2006 measured 19 %wb at a_w=0.799 sampled straight from a real DT outlet).

**Surface-sorption water model, keyed to the SOLID surface (`zones/ftrz.py`, `zones/dcz.py`).**
Both zones now equilibrate the meal toward its own sorption isotherm `Xe(a_w)` with `a_w`
evaluated at the **solid-surface** temperature, not the bulk vapor. In the FTRZ the surface is
as cold as `T_boil_hexane`, so the cool descending meal condenses steam toward `Xe(0.799)` =
19 %wb — the moisture-raising the DT exists to do. This is an ADDITION beyond Coletto
(hexane-only DT); it reuses the existing Luikov/GAB isotherms and `hM`/`aV`.

**Water-interface pinning without bulk-temperature replacement.** A wet meal interface cannot
superheat past the water saturation temperature at the local partial pressure, so the FTRZ water
closure evaluates `a_w` and finite-rate transfer at the lower of the A.17 bulk temperature and
the local water dew-point temperature. The bulk FTRZ meal nevertheless remains at
Coletto's eq. A.17 temperature. These are distinct states: a low water dew point in a hexane-rich
vapour is a phase threshold, not an energy balance capable of cooling the whole meal matrix.
Conflating them produced a sub-zero bulk FTRZ temperature at 40.4 kg/s and destabilised the
FTRZ↔DCZ iteration. The DCZ retains its separately balanced wet-meal phase-change buffer, which
caps the toasting zone near the sparge `T_sat` (~112 °C) instead of the former 123 °C runaway.

**Accepted-profile boundary (2026-07-23).** `solve_dt` may return its best iterate after exhausting
the real-time iteration cap for diagnostics, but the engine now publishes a DT result only when
it is both formally converged and passes finite/composition/temperature/geometry checks. A rejected
attempt leaves the last accepted tray targets, warm start, and axial profile atomically unchanged;
`SolverStress` and the attempt timestamp still update. The HMI consequently says “latest solve
rejected; showing last accepted profile” rather than describing a nonconverged iterate as a
successful profile.

**Calibration.** The over-wetting first seen (29 %wb, with ~2.8 kg/s water conjured from
nothing) was NOT the pinning — the scenario ran the sparge at **1.05 kg/s (≈30 kg/t_raw), a
quarter of the industrial rate**. That cool, weak sparge let the abundant vent steam
supersaturate the bottom so the DCZ condensed everything low. At the realistic **3.9 kg/s
(≈110 kg/t, the Kemper/Svoboda target)** the DT lands on target and conserves. Retuned:
`direct_steam=3.9`, `dt_vapor_feed_water=0.15` (the vent is now a small dome-setting purge, not
the moisture source), `dt_pressure_drop_barg=0.5` (sparge `T_sat` ~111 °C). Scorecard went
**4/6 FAIL → 9 PASS / 1 warn / 1 FAIL**: meal 18.99 %wb, 111.7 °C, 195 ppm; dome 88.5 wt%
hexane / 75 °C; direct steam 110 kg/t — all in band. (Remaining fail: DC over-drying, separate.)

**DCZ double-draw fix (mass conservation).** The DCZ walked the solid top→bottom but the vapor
rises bottom→top, so a top isotherm cell could adsorb against the FULL water budget before a
bottom condensed cell debited it — both branches drawing the same water, letting the meal gain
more than the steam supplied (the ~29 %wb / dome-water-→-0 pathology). Fixed by PRE-counting the
zone's whole condensation into the shared budget. **No-op at the calibrated point** (the sparge
is strong enough that the meal reaches 19 % via the isotherm alone); binds only when under-set.

**Binary-VLE water-saturation floor (`zones/ftrz.py`).** Answering the direct question "do we
integrate binary VLE?": we had the binary *dew-point* for the dome temperature (`_binary_dew_T`)
but the vapor *composition* was set purely by the condensation mass balance — nothing stopped it
stripping the water to 0 % and emitting pure hexane. Added a floor: while liquid water is on the
cold surface, the vapor stays in equilibrium with it, so its water partial pressure can't fall
below the meal's own `a_w·p_sat,water(T_surface)` (Luikov `a_w`, hexane side already had
`x2_equilibrium`). Water and hexane therefore always coexist. **Slack at realistic operation**
(the meal's `Xe` limit stops the sorption while the vapor is still well above saturation), so it
is a correct backstop, not a behaviour change; `y_w` and the floor are clamped against the
`p_sat→P` transient.

**Robustness guards.** (1) `thermo.gab_hexane_content[_and_slope]` now clamps `K·a_h` at its
0.999 divergence boundary instead of raising — a cold off-design transient could drive the pore
gas to hexane saturation where `K>1`. (2) The DCZ particle-energy march clamps `Tp` to a physical
250-480 K band. Both **only engage off-design** (never at a calibrated operating point) and turn
a crash into graceful degradation.

**STOPGAP — sparge minimum input guard (`engine/facade.py`, flag for later).**
`MV_LIMITS["direct_steam"]` lower bound raised 0 → 3.0 kg/s. Below ~3 kg/s (~85 kg/t, already
under the industrial band) the coupled water-sorption↔energy iteration does **not converge** —
the meal over-condenses, dome vapor water strips toward 0 %, and the solve returns
`converged=False`. This min keeps the LIVE sim inside the reliable envelope but is **NOT a real
fix**: the proper fix is to make the DCZ off-design iteration converge (proven this session that
decoupling the water-latent feedback alone is insufficient — other divergence sources remain),
after which the bound can drop back to 0. Flagged explicitly so it is not mistaken for physics.

**Tests.** 137 pass (was 135 + 2 xfail). The two xfails the rework anticipated now PASS and were
un-xfailed: `test_hexane_decreases_monotonically_across_the_whole_dt` and — the headline one —
`test_direct_steam_does_not_invert_sparge_moisture` (more sparge steam now correctly RAISES
moisture). `test_balance.py::test_dcz_solid_water_gain_...` relaxed from a strict one-sided
`gain ≥ condensed` to allow the isotherm's small (<2 %) conservative desorption pullback.

**DC dryer recalibration (follow-on).** With the DT now delivering the correct 19 %wb (it used
to over-dry to ~8 %), the dryer — tuned for that old drier feed — over-dried the product to
10.7 %wb (target 12-13). Diagnosed as fully **equilibrium-limited**: the meal reaches the hot-
dry-air Luz isotherm `X_e` (~11 %wb) regardless of air flow or temperature, so no air-side knob
can raise it — a real dryer is instead **rate-limited** (sized so the meal stops before `X_e`).
The residence knob in this model is the sweep-arm speed (`_stage_tau = base*1.5/(rpm/3)`, NOT bed
height — that only sets holdup), so raised `DR1` to **4.2 rpm** (tau 132→94 s) and trimmed
`heated_air_temp` 360→348 K to hold the outlet in the 55-65 °C band. Result: dryer 12.2 %wb /
60.8 °C, cooler +5.4 K — **scorecard 10 PASS / 1 warn / 0 FAIL** (the lone warn is the dome
temp, 75.1 vs 75 °C, 0.1° over). The whole DTDC is now in band.

**`init_state` now pre-solves the DC too, so `main.py` opens at the FULLY tuned steady state.**
`init_state` (start_empty=False) seeded only the DT stages at their converged targets and left
the DRYER/COOLER at the raw feed state (X2=0.58, X1=0.07) -- so x0 showed a garbage DC that only
settled after ~15 s of ticks. It now chains the same air-contact equilibrium `step()` uses, from
the DT exit through DR1→CL1, seeding the DC at its steady state (DR1 12.1 %wb / 61.5 °C, product
CL1 11.0 %wb / 32.2 °C). Added the DC air conditions to `OperatingSeed` for this.

**COOLER recalibration.** The cooler was over-drying the 12.1 % dryer output to ~10.5 %wb (below
the 11-12.5 % band) AND over-cooling it to ~31 °C. Same diagnosis as the dryer: the cooler
moisture is **equilibrium-limited** (the meal races to its ~10.5 % ambient-air isotherm regardless
of air flow), so it too must be **rate-limited** -- raised `CL1` to **6.5 rpm** (tau ~61 s) to stop
the incidental drying at ~11 %wb. Air flow left high (250 kg/s) since it only sets the temperature
(not the moisture) and cools the meal to ~32 °C, 7 K above ambient (inside the scorecard's ≤10 K
gate). Product now **11.0 %wb / 32.2 °C / 28 ppm** -- in band. Scorecard back to **10 PASS / 1 warn
/ 0 FAIL** (lone warn: dome 75.1 vs 75 °C). Whole DTDC in band; x0 opens fully tuned end-to-end.

**Env note (not code):** the local `.venv` was found broken this session -- a Python 3.8.2 venv
carrying 3.14 wheels (nothing imported). Rebuilt against Python 3.14 (`py -3.14 -m venv .venv
--clear` + `pip install -e .[dev]`), matching `requires-python >=3.14`; 137 tests green on it.

**SINGLE-SHAFT correction (physics).** The DC recalibrations above were first done by setting
DIFFERENT per-tray sweep-arm rpm (DR1 4.2, CL1 6.5 vs 3.0) -- physically impossible: the DTDC has
ONE central shaft, so every tray turns at the SAME rpm (and the HMI's one arm-speed control would
have overwritten the split uniformly anyway). Corrected: residence is now `base_residence_s *
StageSpec.arm_mixing_factor / (shaft_rpm/3)`, where `shaft_rpm` is the single shaft speed
(`model._shaft_rpm`, uniform across all trays) and `arm_mixing_factor` is a per-tray geometry
constant (blade pitch/count/rake design) capturing the different turnover per tray. The DC tuning
moved from per-tray rpm to `geometry.arm_mixing_factor` (DR1 1.07 → ~94 s, CL1 0.69 → ~61 s), which
reproduces the exact same residences at the common 3 rpm -- so the calibration (dryer 12.2 %wb,
product 11.1 %wb / 32.2 °C, 10 PASS / 1 warn / 0 FAIL) is unchanged. The old per-role 1.5×
DC-residence multiplier is folded into these explicit per-tray factors. `sweep_arm_speed` stays a
single tower-wide value; the per-stage MV dict is kept uniform (HMI group control) and the model's
residence + bed-transport now both read one `_shaft_rpm`, never a per-tray rpm.

## DC hexane coefficient anchored to real plant data (EPA AP-42) + Naiha diffusion physics (2026-07-19)

**Trigger.** `dc_hexane_mtc` was the last hand-tuned `[PLACE]` number. The user supplied two papers
that together resolve it: Naiha & Roques (1983), hexane diffusion in DRY oilseed meal, and EPA AP-42
9.11.1 Table 4-4, measured residual hexane in real US soybean-plant meal.

**Naiha (the rate physics).** Directly measures the intraparticle diffusion `D/a²` for hexane in dry
meal at 40-105 C (the DC regime), for a ~175 um radius ~ our flake `r_P`: `D/a²` ~ 3e-8..4.4e-6 /s
(tiny), activation energy **E = 80 kJ/mol** (~190x drop 100->37 C). This is the hard number behind
"diffusion nearly stops at low temperature." NB Naiha's `D` (~1e-13, dry residual regime) is ~1e5x
smaller than Cardarelli's `D_eff` (4e-10, bulk desolventizing) -- two DIFFERENT regimes, no conflict
(DCZ uses the fast one, the DC needs the slow one).

**EPA (the validation data).** Real US soybean plants: meal hexane ~**507 ppm** at the DT exit ->
~**397** (dryer) -> ~**323** (cooler/product), range 110-650; high-feed plants sit higher (plant F:
1380 -> 440). The calibration target we'd been guessing.

**Key finding from reconciling them.** Naiha's STATIC diffusion is ~1000x too slow to explain EPA's
real ~36% DC removal, and its E=80 over-predicts the T-sensitivity (EPA's dryer/cooler split implies
an effective ~20 kJ/mol). So the real DC removal is **agitation/surface-renewal-driven** (the tumbled,
conveyed meal exposes fresh surface), not static-diffusion-limited -- which is exactly why a lumped
MTC is still needed rather than a bare `D_eff`. So `dc_hexane_mtc` is now **[PAPER-anchored]**:
magnitude to EPA Table 4-4, T-law from the escaping tendency `a_h*p_sat(T)` (moderate, matching EPA),
with Naiha's 80 kJ/mol noted as the static upper bound. Recalibrated 2.0e-2 -> 1.3e-2 so this
scenario's high-feed base case (DT ~982 ppm) lands product ~293 ppm (dryer 420, air 78 ppm, safe).
EPA cascade + Naiha added to the scenario's validation-targets block.

## DC residual hexane rebuilt mechanistically (GAB + Antoine escaping tendency); DT/DC consistency (2026-07-19)

**Trigger.** Two problems with the DC hexane term: (1) it over-stripped to ~0 ppm after
cooling (unrealistic -- real meal holds ~100-300 ppm), and (2) it was an ad-hoc first-order
strip `X2*exp(-k*air/m_dry)`, inconsistent with the DT/DCZ which use the real GAB isotherm +
Antoine vapor pressure + diffusion. A literature review (Cardarelli 1996 hexane sorption, Faner
2019 kinetics, Zhang 2018 diffusion `E_a`) confirmed the DC's *hexane* side was the one outlier
(its water side already uses Luz; the DT is internally consistent).

**Key physics (user's insight, confirmed against Cardarelli 1996).** Hexane desorption at low
temperature nearly stalls not because the DIFFUSION coefficient drops (Zhang: `E_a`~6 kJ/mol,
only ~1.5x over the whole range) but because the ESCAPING TENDENCY -- the solid's own equilibrium
hexane partial pressure `p_eq = a_h(X2,T)*p_sat_hexane(T)` -- collapses: the isosteric heat of
sorption is ~22 kJ/mol at low coverage (Cardarelli), so `p_eq` falls ~5-20x from the DT (~100 C)
to the cooler (~37 C). This is the mechanistic version of "diffusion stops at low temperature."

**Rebuild (`core/dc.py::desorb_hexane`).** Replaced the strip with a steady-state well-mixed
mass-transfer balance: `m_dry*(X2_in - X2_out) = dc_hexane_mtc*air_flow*(y_surf - y_air)`, where
`y_surf = a_h(X2_out,T)*p_sat_hexane(T)/P` (GAB inverted via new
`thermo.hexane_activity_from_loading`, SAME isotherm the DCZ uses) and `y_air` is the outlet air's
hexane mole fraction. Temperature dependence is now EMERGENT (no gate): the cold cooler desorbs
~2.6x slower than the dryer, so the product holds a realistic ~195 ppm. The hexane going into the
drying air is returned and tracked against the **~1100 ppm (10% LEL) safety limit** (DR1 ~92 ppm,
CL1 ~20 ppm at base case -- comfortably safe), shown on the DC tray card (red if exceeded).
`DCConstants` swaps `dc_hexane_strip_k` for `gab`/`antoine_hexane`/`dc_hexane_mtc`;
`air_contact_equilibrium` return grows a 6th element (air hexane); `Outputs` gains
`stage_air_hexane_ppm`. Net: DT exits ~980 ppm, dryer ~320, product ~195 ppm; air well under LEL.

## Flake particle radius fixes DT residual hexane + moisture; realistic geometry & feed basis (2026-07-19)

**Trigger.** With the DC rewrite landed and the DT geometry made realistic (6 m diameter, shallow
PREDESOLV / deep TOASTING beds), two seed-tuning requests surfaced: get the DT-exit (dryer-inlet)
residual hexane down to ~500-1000 ppm (was ~12000), and set feed hexane to ~35%.

**Feed hexane was a display/basis confusion, not a value problem.** The GUI feed card showed
`feed_hexane*100` = "47%", read as a total mass fraction. But the model stores hexane DRY-basis
(kg/kg dry solid); as a fraction of WET meal (dry + moisture + oil + hexane) the 0.4743 base case is
only ~30.5% -- already within the realistic ~30-35% band. Fixed the feed card to display total
(wet) mass fraction (`interfaces/ui/tower.py`); the underlying feed_hexane is unchanged (0.4743).
NB feed BELOW ~0.42 dry-basis collapses the PHZ (it drops under X2,cr, the pore-saturation
threshold), so the realistic-looking "35% dry" would have been unphysical -- the wet-basis framing
avoids that trap.

**The residual hexane is diffusion-TAIL limited -- seed levers and extra trays can't fix it.**
Confirmed empirically: the GAB equilibrium floor is only ~40-450 ppm (so ~500 ppm IS reachable),
but feed flow (25->12 kg/s) + direct steam (1.5->4.0) only moved 11800->9600 ppm, and adding a
whole extra toasting tray (doubling DCZ residence) only 12129->10975. The core hexane diffuses out
on an `r_P^2/D_eff` timescale (~hundreds of s) that residence can't overcome. The one effective
lever is the DIFFUSION LENGTH.

**Root fix: flake particle radius.** Real desolventizer meal is thin FLAKES (~0.2-0.4 mm), not
Cardarelli's 2 mm granules (`particle_radius` was 1.0 mm = their "diameter"/2). Set to the
flake-scale **0.18 mm** (`properties/soybean.yaml`, `[DERIVED]`). Result: DT exit **~517 ppm**
hexane (in target), still converged, PHZ intact (L_PHZ ~0.28 m). This ALSO self-consistently sped
water equilibration (`kappa_w = 15*D/r_P^2`), which fixed a long-standing moisture gap: DT exit
moisture rose from ~9.5% to a realistic **~24%** (literature target 18-22%), so the DRYER now has
real moisture to remove.

**DC air flows re-tuned for the new moisture regime.** With ~24% moisture reaching the dryer,
evaporative cooling drops DR1 to ~53 C (matching the real 311 SUSIC ~50 C) while drying to ~12%.
The dryer thus does most of the cooling, so the COOLER only needs ~53->37 C -- `ambient_air_flow`
dropped **400 -> 55 kg/s** (a normal ~2:1 air:solid, vs the old 16:1 that the low-moisture regime
had forced), landing CL1 ~37 C (SCADA CHLADIC ~38.5 C).

**Final calibration (same session, after user input):** feed hexane set to **35% by total WET mass**
(industry convention; = 0.5815 dry-basis, still above X2,cr so PHZ intact) and the feed card fixed
to display wet-basis %. `particle_radius` settled at **0.20 mm** (not 0.18): a real ~0.25 mm soy
flake diffuses through its ~0.125 mm half-thickness, so a sphere-equivalent radius is ~0.125-0.25 mm,
and 0.20 mm is the SAFE end of that band -- larger radii (0.25 mm -> ~3800 ppm DT exit) would force
the dryer to exceed the **1100 ppm hexane-in-drying-air safety limit** to reach a realistic product.
At 0.20 mm the DT exits ~2000 ppm, the dryer strips to ~186 ppm (already in the target 100-300 ppm
product band) at ~780 ppm in the drying air (safe). REMAINING (next task, user-flagged): the COOLER
currently over-strips ~186 -> ~20 ppm, but hexane diffusion nearly stops at cooler temperatures, so
the product should hold ~100-300 ppm -- needs a temperature-gated DC hexane rate plus explicit
drying-air hexane tracking against the 1100 ppm limit.

## DC (dryer/cooler) rewritten first-principles on Luz/Silva falling-rate physics (2026-07-19)

**Trigger.** The user reported two DC symptoms: (1) the air-outlet readout looked "significantly
lower than the air inlet" ("where does the air go?"), and (2) ~100 C meal meeting ~100 C dryer air
read ~43 C on the tray. Both traced to one root cause: `core/dc.py`'s previous model treated the
dryer as a WET-SURFACE, CONSTANT-RATE contactor — it evaporatively cooled the meal to the air's
adiabatic-saturation temperature and relaxed solid moisture most of the way toward the
air-humidity isotherm equilibrium each pass. That is the wrong physics for soybean meal.

**Literature.** Two sources the user pointed to (`literature_sources/`): Luz et al. (2010),
*Food Bioprod. Process.* 88:90-98 (dynamic model of a direct rotary soybean-meal dryer), and Silva
et al. (2012), *Powder Technol.* 229:61-70 (fluidized-bed drying). Both establish that soybean-meal
drying is **entirely falling-rate (internal-diffusion-controlled)** — there is NO constant-rate
period, and the drying RATE `K*(X1 - X_e)` (with a small `K`, Luz eq. 4, ~8.44e-3/s), not the air's
saturation capacity, governs how fast moisture leaves. Luz's own industrial case: 90 C air ->
89 C solid out — the meal STAYS HOT because that unit runs an ~80:1 air:solid ratio, so the hot air
supplies the evaporation's latent load.

**Rewrite (`core/dc.py`).** Each DC stage is now a well-mixed CSTR at steady state with a CLOSED
two-sided mass/energy balance:
- **Moisture (solid):** falling-rate CSTR `X1_eq = (X1_in + K*tau*X_e)/(1 + K*tau)`,
  `K = thermo.luz_mass_transfer_coefficient` (Luz eq. 4), `X_e = thermo.luz_equilibrium_moisture`
  (Luz eq. 5 — the temperature-dependent DRYING isotherm, NOT the DT-internal Gianini/Luikov
  desorption isotherm the user correctly suspected was wrong for this regime). `tau` is the stage's
  own residence time (threaded through `model._dc_equilibrium`), so the meal removes only a FRACTION
  of removable moisture per pass. Capped by the air's saturation carrying-capacity (-> 0 as air
  flow -> 0) and by moisture actually present.
- **Moisture (air):** `Y_out = Y_in + m_evap/m_air_dry` — dry air conserved EXACTLY; humidity
  accounts for every kg the solid loses. This is the closed air mass balance (answers "where does
  the air go": nowhere — it conserves dry mass and only gains humidity).
- **Energy:** a COUPLED two-phase balance. Solid temperature from a Newton-cooling balance
  convectively coupled (conductance `UA`, NTU-derived) to the outlet air; outlet air temperature
  pinned by the adiabatic total-enthalpy balance `H_in == H_out`. The coupling lets the hot air
  supply the latent load, so the meal stays warm rather than crashing to wet-bulb. No more
  evaporative-cooling special case, no `adiabatic_saturation_temperature` (deleted).

`core/balance.py::dc_stage_balance` is now a genuine two-sided check (water + total enthalpy via the
shared `dc.solid_stream_enthalpy_w`/`dc.air_stream_enthalpy_w` primitives); it closes to machine
precision in every regime, and the old `ignore_evaporative_cooling_air_enthalpy` exception is gone.

**Physical finding surfaced during implementation (important, reported to the user).** At this
plant's throughput (25 kg/s dry meal), a dryer that removes meaningful moisture CANNOT hold the meal
at ~100 C while doing so — evaporating water carries latent heat away, so DR1 settles WARM (~85 C at
the scenario's own SP1 -> DR1 inlet of ~9.5% moisture), not hot, and not the old ~43 C crash. The
43 C reading was the over-evaporation bug; ~85 C is the correct answer. Keeping a dryer meal at air
temperature while drying requires Luz's ~80:1 air:solid ratio, unrealistic here.

**Calibration.** COOLER: cooling ~25 kg/s of hot meal to the SCADA CHLADIC reference (~38.5 C) with
25 C ambient air is energy-bound to a high air:solid ratio; `ambient_air_flow` retuned 60 -> 400 kg/s
(~16:1) lands CL1 at ~39 C (60 kg/s -> ~70 C; 200 -> ~49 C — real cooling thermodynamics, not tuning
slack). `MV_LIMITS` ceilings raised accordingly (`heated_air_flow` 100->200, `ambient_air_flow`
100->800). DRYER `heated_air_flow` kept at 60 kg/s (DR1 ~85 C while drying). **Out-of-DC-scope
caveat (documented in the scenario):** SP1 currently delivers only ~9.5% moisture to the dryer vs
the 18-22% whole-tower literature target, so DR1 over-dries to ~5% and the final product lands ~7-8%
rather than the ~12% spec — a DT/sparge calibration gap, not a DC one.

**Config.** Luz correlations added as `LuzDryingParams` (`thermo.py`/`schema.py`), coefficients in
`properties/soybean.yaml` (`water_luz_drying`, `[PAPER]` Luz eqs. 4/5), wired into `DCConstants`
(replacing the DC's `luikov` field — the Gianini/Luikov isotherm stays in the DT-internal DCZ only).

**UI.** The tower's "Air out:" readout now shows the (conserved) dry-air flow alongside humidity, so
it can't be misread as the air stream shrinking.

**Verified.** Full suite 131 passed; `test_dc.py` rewritten for the three physical regimes plus
parametrized closed-balance checks; full-scenario run gives DR1 ~85 C / dries, CL1 ~39 C, both
balances ~0.

## Stale MV air-flow cap silently halved the tuned DC air flows (2026-07-18)

**Trigger.** After the previous entry's fixes, the user ran the actual GUI (not the headless
verification this session had relied on) and saw the exact symptom the DC recalibration entry
above was supposed to have fixed: DR1/CL1 barely different from the Toaster, residual hexane back
up around 1250 ppm instead of the tuned ~114 ppm.

**Root cause.** `engine/facade.py`'s `MV_LIMITS` table -- the hard min/max clamp every MV is seeded
and rate-limited against -- still capped `heated_air_flow`/`ambient_air_flow` at `(0.0, 30.0)`, a
leftover from the ORIGINAL 5.0/8.0 kg/s placeholder era. When the DC recalibration entry above
retuned `operating_defaults.heated_air_flow`/`ambient_air_flow` to 60.0 kg/s, `_build_registry`'s
own `add()` helper (`seed = min(max(seed, lo), hi)`) silently clamped that seed back down to 30.0 --
exactly half -- with no warning, because config-value validation (`config/schema.py`) and the
facade's own separate MV-limits table were never cross-checked against each other. The scenario
YAML, `assemble_model`, and every headless test in this session called `Model.step` directly with
hand-built `Inputs` (bypassing the MV registry entirely), so this was invisible everywhere except
the actual running GUI/OPC UA path -- explaining why this session's own "well behaved DTDC
demonstrated in GUI" checks (headless `model.step` calls) didn't catch it.

**Fix.** Raised both caps to `(0.0, 100.0)` in `MV_LIMITS`, giving headroom above the current 60.0
kg/s tuned point for further tuning. Verified via the real `RuntimeFacade` (not just `Model.step`)
with a `FreeRunClock`: `heated_air_flow`/`ambient_air_flow` effective values now read 60.0 (not
30.0), and a 3200s run reproduces the DC recalibration entry's own target numbers (Cooler ~39.7 C,
residual hexane ~114 ppm). Take-away for future retuning: a change to `operating_defaults` in the
scenario YAML is NOT sufficient by itself when the changed field is also MV-gated -- `MV_LIMITS`
must be checked too, since it silently overrides config on that path.

## Protein-denaturation modeling removed; GUI graceful-shutdown control added (2026-07-18)

**Trigger.** Immediately after the TIA removal below, the user asked to also remove protein
solubility/denaturation modeling entirely ("clean the code from protein solubility... make it
leaner") -- the one quality kinetic explicitly kept during the TIA removal is no longer wanted
either, so nothing of the original `§7.11 Quality kinetics` section remains. Separately, the user
hit `[Errno 10048] ... only one usage of each socket address` trying to restart `main.py` -- a prior
NiceGUI dev-server process (started for this session's own GUI verification) never exited and was
still holding port 8080, with no way to stop it short of killing the process from outside the app.

**Protein denaturation removed.** Same surgical, layer-by-layer approach as the TIA removal:
`core/model.py` (`ModelConstants.denat_k0/denat_Ea/denat_moisture_cap`, `State.S_prot`,
`_denat_rate`, the `step()`/`outputs()` wiring, `Outputs.stage_Sprot`/`kpi_protein_solubility_pct`,
plus the now-unused `c = self.constants` local in `step()`), config (`config/schema.py`'s
`ModelParams` fields, `config/builder.py`'s `assemble_model` wiring, `scenarios/soybean_default.yaml`'s
`denat_*` keys and the now-orphaned Chen 2014 citation), the GUI (`interfaces/ui/app.py`'s one
`_KPI_TILES` entry -- protein solubility had no per-tray widget or profile chart, unlike TIA), the
OPC UA server (`_KPI_FIELDS` entry, per-stage `Sprot` node, both `kpi_map["protein_solubility"]`
occurrences), and tests (`test_model.py`: dropped the `S_prot` assertion from
`test_init_state_matches_seed` and deleted `test_denat_decays_over_time` outright, since nothing is
left to assert). Docs updated to match: BuildSpec.md's `§7.11` is now a stub noting both kinetics
were implemented then removed (kept the section number since other sections cross-reference it, and
the M3 milestone description below still uses it as a historical pointer); `DTDC_default_parameters.yaml`
lost the same keys/citation. Full suite: 117 tests pass (118 minus the deleted test), `ruff check`
clean.

**GUI shutdown button added.** `interfaces/ui/app.py`'s header now has a "Shutdown" button
(`app.shutdown()`, NiceGUI's own clean-exit call, which also fires `main.py`'s existing
`app.on_shutdown(facade.shutdown)` hook) so the dashboard process can be stopped from inside the
browser instead of only via an external `kill`/Task Manager -- directly addresses the port-8080
collision, which was caused by a stale process with no in-app way to end it.

## TIA modeling removed; tuned scenario confirmed as the shipped default (2026-07-18)

**Trigger.** With the DC recalibration and overshoot fix landed and the scenario producing sane
operator-perspective numbers, the user asked to (a) confirm the tuned parameters are the
application's actual default, and (b) delete TIA (trypsin inhibitor activity) modeling and its GUI
reporting entirely, to shrink the codebase, ahead of demonstrating a "well behaved" DTDC in the GUI
and committing.

**(a) Default confirmed, no change needed.** `main.py`'s `DEFAULT_SCENARIO` already points at
`scenarios/soybean_default.yaml`, the exact file recalibrated throughout this session (air flows,
`dc_hexane_strip_k`, `feed_hexane`, `sweep_arm_transfer_gain`, `direct_steam_pressure_barg`, derated
`water_diffusivity`, reduced tray count). No separate "apply tuned params as default" step was
required.

**(b) TIA removed; protein denaturation (`S_prot`) kept.** TIA (`C_TIA`, biexponential Arrhenius
decay, `kpi_urease_proxy`) and protein denaturation (`S_prot`, first-order Arrhenius decay,
`kpi_protein_solubility_pct`) are separate quality kinetics that happen to sit on adjacent lines
throughout the codebase but never share a formula -- confirmed by inspection before removal, since a
wholesale deletion of "the quality kinetics block" would have taken `S_prot` with it. TIA was
removed surgically, layer by layer: `core/model.py` (`ModelConstants` fields, `State.C_TIA`,
`_tia_rate`, the `step()`/`outputs()` wiring, `Outputs.stage_TIA`/`kpi_urease_proxy`), config
(`config/schema.py`'s `ModelParams` fields, `config/builder.py`'s `assemble_model` wiring,
`scenarios/soybean_default.yaml`'s `tia_*` keys), the GUI (`interfaces/ui/app.py`'s KPI tile, the
per-tray/product `widgets["tia"]` cards, the `tia_profile_plot` chart and its `_resize_charts`/sync
wiring), the OPC UA server (`interfaces/opcua/server.py`'s `_KPI_FIELDS` entry, per-stage `TIA` node,
both `kpi_map["urease_proxy"]` occurrences), and tests (`test_model.py`). `S_prot` was left untouched
at every one of these sites. Full suite (118 tests) and `ruff check` both pass after the removal.
Docs (`app_specifications/DTDC_Simulator_BuildSpec.md` §7.11 and its parameter table/glossary/node-map
references, `app_specifications/DTDC_default_parameters.yaml`) updated to match; the M0-era
"Placeholder physics" entry below was trimmed to drop the now-obsolete TIA biexponential formula
while keeping its `S_prot` recurrence rationale.

## Macro-level operator-perspective validation: DC air-flow recalibration + a real overshoot bug (2026-07-18)

**Trigger.** With the core physics/quality-gate work landed, the next ask was macro-level sanity: does
the simulation behave reasonably from an OPERATOR's own perspective, not just internally consistent at
the equation level. The user provided a real DTDC SCADA screen (a "311 TOASTER" HMI, Czech-labeled) as
a rough reference for typical monitored values -- explicitly NOT to be copied 1:1 (different plant
scale, a 6-tray/2-dryer-stage design vs. this scenario's own simplified 2 PD/1 MN/1 SP/1 DR/1 CL
geometry, and the screen's own 7 barg DIRECT steam flagged by the user as atypical, not a calibration
target).

**What comparing against it found.** Running the scenario to steady state showed the Dryer (DR1) and
Cooler (CL1) stages barely moving off the Toaster's own ~102 C (DR1=102.3 C, CL1=99.8 C) -- while the
SCADA reference's own SUSIC (dryer, ~49-54 C) and CHLADIC (cooler, ~38.5 C) readings show BOTH stages
doing real work. Traced to `ambient_air_flow`/`heated_air_flow` (5.0/8.0 kg/s) being far too small
relative to `feed_flow_rate` (25 kg/s dry solid) -- an air:solid mass ratio of only 0.2-0.3:1, too low
for `core/dc.py`'s own single-pass well-mixed contactor model (`air_contact_equilibrium`) to achieve
meaningful heat/mass exchange in one pass. Separately, steady-state residual hexane came out ~11,800
ppm -- the user set a hard requirement that finished meal stay under 1000 ppm, so this needed fixing
regardless of the temperature question.

**A real bug found while recalibrating, not a tuning artifact.** Sweeping air flow upward to find a
better calibration, `CL1` converged to a STABLE -12.6 C against a 25 C ambient air supply -- physically
impossible (a passive contactor can never cool a stream below its own coolant's inlet temperature).
Root cause: `Q_sensible_w = effectiveness*air_flow_kg_s*CP_AIR_J_KG_K*(air_T-T_in)` computed the energy
the AIR stream itself could give up, using ONLY the air's own heat-capacity rate, then applied that
DIRECTLY to the solid's own (often much smaller) thermal mass with no cross-check -- fine at the OLD,
tiny air flow (air was never the limiting stream there), but wrong once air flow is large enough that
`air_flow_kg_s*CP_AIR_J_KG_K` exceeds `m_dry_kg_s*C_wet`. Fixed with the standard effectiveness-NTU
convention: `C_min = min(C_air, C_solid)`, `Q_sensible_w = effectiveness*C_min*(air_T-T_in)` -- this
bounds `T_eq` to `[min(T_in,air_T), max(T_in,air_T)]` by construction, for ANY air flow. `air_T_out`
(the air side's own exit state, added in the earlier balance-quality-gate work) was also switched from
the old `air_T - effectiveness*(air_T-T_in)` shortcut to the energy-balance-consistent `air_T -
Q_sensible_w/C_air`, so the two sides stay exactly reconciled (confirmed via `core/balance.py`'s own
`dc_stage_balance` at the same high-flow condition that triggered the bug: residuals at machine
precision). New regression test, `test_high_air_flow_never_overshoots_past_air_temperature`.

**Recalibration, `scenarios/soybean_default.yaml`.** `heated_air_flow`/`ambient_air_flow`: 5.0/8.0 ->
**60.0/60.0 kg/s** (2.4:1 air:solid, a plausible ratio for a well-designed pneumatic cooler/dryer,
found by sweeping and checking against the SCADA reference -- not derived from a source). This alone
brings CL1 to ~39.8 C, closely matching the reference's own CHLADIC (~38.5 C). DR1 stays elevated
(~103.7 C) regardless of air flow -- an accepted, structural gap, not something more air flow fixes:
the real unit runs TWO dryer passes in series (this scenario's own single DR1, like the MN/SP tray
-count simplification earlier this session, trades that structural detail for a simpler geometry), and
a single well-mixed stage can't simultaneously use air HOT enough to genuinely dry (needs `air_T >
T_in`) and end up far COOLER than that same air (tested directly: lowering `heated_air_temp` stops
overshoot-cooling but also fully suppresses evaporation, since the energy cap zeroes out whenever
`air_T <= T_in` -- an inherent tension in this simplified single-pass model, not a bug). The COOLER
stage (genuinely cool ambient air) carries the real cooling duty instead, which is where the reference
match actually landed.

`dc_hexane_strip_k`: 0.3 -> **1.0** (retuned jointly with the air-flow change, since DC's own hexane
stripping formula, `X2_eq = X2_in*exp(-k*air_flow/m_dry)`, scales with the SAME air flow -- the flow
recalibration alone would have already dropped residual hexane substantially, but this rate constant
was still explicitly `[PLACE]`/uncalibrated, so it was tuned directly against the actual requirement).
Lands residual hexane at ~115-120 ppm at the base case -- confirmed ROBUST, not a coincidence of one
operating point, via a stress test (indirect steam duty at 0.6x, discharge gates narrowed to 20%): both
stayed in the same 115-120 ppm range, since DC's own stripping dominates whatever concentration
actually arrives from the DT, regardless of upstream conditions.

**Verification.** Full suite (118 tests, incl. the new overshoot regression test) green; `ruff check`
clean; `assemble_model` timing unchanged (~3.1 s). Final base-case steady state: PD1/PD2 ~63-69 C,
MN1/SP1 (toaster) ~102-103 C (matches the SCADA reference's own 101-109 C toaster range), DR1 ~103.6 C
(does not match the reference, see structural gap above), CL1 ~39.8 C (matches closely), residual
hexane ~115 ppm (well under the 1000 ppm requirement), steam consumption ~124 kg/t (order-of-magnitude
reasonable for a real DTDC, not compared against a specific reference number).

## DCZ particle hexane mass-conservation gap (2026-07-18)

**Trigger.** The previous entry's own quality-gate work found and deferred a large (~18.6x) hexane
mass-conservation gap in `core/zones/particle.py`'s `march_particle_mass` -- confirmed real, but not
understood well enough to fix safely in that session. Explicit follow-up request: compare against
Coletto's own papers before touching core model equations, and make sure the user understood the
issue before any rewrite.

**Literature check.** Coletto, Bandoni & Blanco (2022), Appendix A (rendered as images, not
`pdftotext` -- the equation-heavy pages don't extract cleanly): eq. A.22 is the rigorous particle
-scale hexane balance, `d(alpha_pg*rho_pg*wpg2 + alpha_ps*rho_ps*X2,so)/dt = div(alpha_pg*rho_pg*
D_eff*grad(wpg2))`, with Table A.3's own boundary condition at r=rP stated directly against THIS
equation's own flux: `J_MR.r = -hM*rho_V*(wV2-wpg2,R)` (a true physical mass flux, not a transformed
one). Eq. A.28 defines `Ca = alpha_pg*rho_pg/(alpha_pg*rho_pg+alpha_ps*rho_ps*dX2,so/dwpg2)`; eq.
A.29 states the simplification `dwpg2/dt = div(Ca*D_eff*grad(wpg2))` -- the form `march_particle_
mass` had been discretizing all along. **The paper never re-states the boundary condition for this
simplified form** -- it appears once, attached to eq. A.22, and eq. A.29 is presented as a "yields"
without further boundary-condition discussion. Per the user's own framing, this is exactly the
"literature not specific" case -- confirmed, not assumed.

**Two failed attempts, and the test that falsified them.** Assumed the boundary term needed SOME
`Ca`-based rescaling to stay consistent with eq. A.29's own transformed form. Attempt 1 (`coeff_surf`
scaled by the surface layer's own `Ca`) overcorrected to ~0.34x (worse, wrong direction). Attempt 2
(re-derived via gradient-matching: `Ca/(alpha_pg*rho_pg)`) looked promising at first -- reduced the
gap to ~1.10x at the code's own typical single-shot dt -- but a RIGOROUS test (hold TOTAL elapsed time
fixed, refine only how many internal sub-steps compute it) showed the residual staying PERFECTLY FLAT
regardless of sub-step count (44.5%-45.3% across a 60x sub-step range) -- proof it wasn't a temporal
truncation error, i.e. proof the formula itself was still structurally wrong. (An earlier, apparently
-clean "convergence" result, sweeping mesh resolution `Np` together with `dt` scaled down proportional
to `dr^2`, turned out to be an artifact of shrinking the TOTAL simulated time toward zero alongside
the mesh -- both `declined` and `flux` trivially shrink together for a near-instantaneous transition,
which isn't a meaningful convergence test at all. Caught by holding total time fixed instead.)

**Root cause.** Re-derived eq. A.29 from eq. A.22 by hand: `Ca` is itself a function of `wpg2`
(spatially varying), so `div(Ca*D_eff*grad(wpg2))` is NOT `Ca*D_eff*laplacian(wpg2)` -- by the product
rule it's `Ca*D_eff*laplacian(wpg2) + D_eff*grad(Ca).grad(wpg2)`, and BOTH prior attempts' own chain
-rule reasoning implicitly dropped that cross term. Eq. A.29, taken literally, is Coletto's own CHOSEN
PDE form (matching what the interior discretization already did, correctly, via face-averaged `Ca`),
not a rigorous algebraic identity carrying an equally rigorous boundary condition alongside it.

**The fix.** Discretize eq. A.22 directly instead of eq. A.29 -- sidesteps the whole question, since
eq. A.22's own boundary condition needs no transformation at all. `X2,total = alpha_pg*rho_pg*wpg2 +
alpha_ps*rho_ps*X2,so(wpg2,T)` becomes the finite-volume method's own accumulation variable (not
`wpg2` alone). This is a SIMPLER scheme than the one it replaces: the diffusive flux terms use the
literal, CONSTANT coefficient `alpha_pg*rho_pg*D_eff` (no face-averaging of a spatially-varying `Ca`
needed anywhere, since nothing in the flux term varies spatially anymore), and the boundary term uses
eq. A.22's own literal, unmodified `hM*rho_V*(wV2-wpg2)`. The only remaining approximation (same
frozen-coefficient stability rationale used throughout this codebase) is linearizing `X2,total`'s own
accumulation via its Jacobian `M_i = dX2,total/dwpg2|_local = alpha_pg*rho_pg + alpha_ps*rho_ps*
(dX2,so/dwpg2)`, evaluated from the state entering the step -- replacing `Ca`/`_ca_per_layer` entirely
with `_accumulation_jacobian_per_layer`.

**Verification, same rigorous fixed-total-time/sub-step methodology that falsified the two prior
attempts (not just re-checking a single number):**
- At a production-realistic dt (~60 s, this project's own typical per-cell DCZ residence), SINGLE
  step, NO sub-stepping (the hardest case): residual ~1.4% (was 1860% before any fix, was 44.5% after
  the failed `Ca`-scaling attempt).
- Sub-step refinement (60 s total, fixed): shrinks cleanly and monotonically, -0.99% (60 sub-steps) to
  -0.78% (1200 sub-steps) -- continuing to shrink, not plateaued.
- Mesh refinement (`Np` 12->96, dt properly time-resolved at EACH mesh so spatial and temporal error
  aren't confounded -- the mistake in the earlier "clean" result): shrinks cleanly and monotonically,
  -0.99% to -0.23%.

Both directions behave exactly as a correctly-conservative scheme should -- confirmed, not assumed.

**Scope.** Entirely contained in `march_particle_mass` (its signature and return types are unchanged,
so no caller elsewhere needed to change). `_ca_per_layer` removed (replaced by
`_accumulation_jacobian_per_layer`); `thermo.x2_so_and_slope` (the isotherm-slope primitive) is
unchanged, just called directly instead of wrapped in `Ca`.

**Downstream effects, all expected and re-verified, not silently absorbed:**
- `tests/test_particle.py` gained `test_mass_march_conserves_x2_total_between_bulk_and_surface_flux`,
  a permanent regression guard using the same rigorous methodology (production-realistic dt, single
  step, ~2% tolerance -- not the old 1860% gap).
- `tests/test_dcz.py::test_hexane_content_decreases_from_top_to_bottom`'s own threshold dropped from
  0.25 to 0.10: the OLD scheme was transferring hexane to the vapor ~18.6x FASTER than physically
  correct, artificially inflating this zone's own reduction over a fixed iteration budget -- the fix
  makes the reduction genuinely smaller (still substantial, still monotonic, just accurate now).
- `tests/test_balance.py`'s DCZ section fully reworded: the "confirmed, deferred gap" scoping note is
  gone (the gap is fixed); `dcz_zone_balance`'s own hexane/energy tolerances tightened 10x (were bounded
  by total throughput, i.e. up to 100% relative; now <10%). One test's own premise turned out to be
  flawed independent of this fix (assumed a "pure condensation, no isotherm contribution" case that,
  on closer per-cell inspection, always had isotherm-driven adsorption ALSO active in the zone's middle
  cells) -- corrected to check the always-true bound (`total_water_to_solid_kg_s >= total_condensed_
  kg_s`) instead of a coincidental equality.
- Full suite (117 tests) green; `ruff check` clean; `assemble_model` timing unchanged (~2.9-3.2 s) --
  same matrix size and structure, just different (simpler) coefficient formulas.

**Explicitly out of scope.** `core/balance.py`'s own `dcz_zone_balance` still carries its own,
separate, DELIBERATE approximation (plain `dH_vap_hexane` for hexane's sorption heat in its energy
check, not the true isosteric value -- see that module's own docstring) -- independent of this fix,
not touched here. The small (few-percent) residual DCZ zone/bed-scale balance checks still show is
attributed to that approximation plus ordinary Gauss-Seidel convergence looseness, not a remaining
particle-scale bug.

## Mass/energy balance quality gate (2026-07-18)

**Trigger.** The previous entry's own DCZ latent-heat double-count (caught indirectly, via two
unrelated sanity tests breaking, not by any dedicated conservation check -- none existed anywhere in
this codebase) prompted an explicit request: design a rigorous quality gate that would catch this
whole *class* of bug -- double-counting, or any other conservation violation -- across every zone,
not just DCZ.

**Design, `core/balance.py` (new module).** One residual-check function per zone
(`phz_zone_balance`/`ftrz_zone_balance`/`dcz_zone_balance`/`dc_stage_balance`), plus
`dt_handoff_consistency` for the whole-DT assembly step, each returning a `MassEnergyResidual`
(hexane/water mass residuals in kg/s, energy residual in W). **Core principle, followed throughout**:
every function takes ONLY a zone's *external* boundary (its own feed/exit states, duties, steam) plus
its already-computed result object -- NEVER a zone's own internal lagged/iterative state
(`water_latent_w_m3`, `q_condL`, `sorption_sink_w_m3`, etc.). A bug living in that internal state can
cancel out against the identical bug in a check that reuses it; a check built only from boundary
conditions and reported outputs cannot silently agree with a wrong internal computation. Confirmed
with the user: test-suite-only (zero runtime cost, `tests/test_balance.py`), not a runtime-assertion
mode -- plus a separate, deliberately "very simple" LIVE diagnostic (see below) for the real-time
engine, tracking accumulation rather than re-deriving physics.

**Shared primitive, `core/thermo.py`.** `mixture_cp_per_kg_dry_solid(X1, X2, X3, cp_water_liquid,
cp_hexane_liquid, cp_oil, cp_solid)` promoted from `zones/phz.py`'s own private
`_mixture_cp_per_kg_dry_solid` (that module's only prior caller, now delegates here, bit-identical) --
gives the new balance checks a solid-side energy formula that's already tested, rather than a fresh
one invented ad hoc for the checker alone.

**DC signature extension, `core/dc.py`/`core/model.py`.** `air_contact_equilibrium` previously
returned only the SOLID's own equilibrium targets (`T_eq, X1_eq, X2_eq`) -- the air stream's own exit
state was never computed at all, making a genuine two-sided ("air loses what solid gains") check
impossible. Now returns `(T_eq, X1_eq, X2_eq, air_T_out, air_humidity_out)`: `air_humidity_out` from
the SAME `m_evap_kg_s` mass balance `X1_eq` already uses; `air_T_out` from the SAME sensible-heat
exchange `Q_sensible_w` already uses, algebraically simplified to avoid a zero-flow-guarded division.
Deliberately does NOT model the evaporated moisture's own enthalpy joining the air stream (documented
simplification, same category as DC's own pre-existing missing hexane-latent-heat gap --
`dc_stage_balance`'s own `ignore_hexane_latent_heat` parameter makes that gap visible in the check's
signature, not silently absorbed into a loose tolerance). `Model.step`'s own `_dc_equilibrium` still
returns only the 3-tuple (the 2 new values aren't consumed anywhere in the real-time engine yet);
`tests/test_dc.py`'s 8 call sites updated to unpack 5 values.

**Three real, confirmed conservation bugs found and fixed while building/validating the checks --
exactly the outcome this work was commissioned to produce:**

1. **`core/zones/dcz.py` step 4, condensation never debited from `vapor_wV2`.** The isotherm branch's
   own mass-conservation gap was already fixed (previous entry's own `water_mass_rate_w_m3` addition),
   but the SAME class of gap existed for the OTHER (supersaturated/condensation) branch:
   `condensed_kg_s[j]` (computed in step 2, same outer-loop pass) was credited into the solid's own
   moisture (step 4.5) and into that cell's own energy balance (step 2), but never subtracted from the
   vapor side's own `wV2` cascade in step 4. Fixed by adding `condensed_kg_s[j]/(A_bed*dz)` to
   `source_m`, same sign/direction as the already-fixed isotherm term (water leaving vapor raises
   `wV2`) -- no lag needed here (step 2 runs before step 4 in the same pass).
2. **`core/dt_solver.py`'s FTRZ<->DCZ handoff undercounted water transfer.** `m_vapor_into_ftrz_kg_s`
   subtracted ONLY `dcz_result.total_condensed_kg_s` (the supersaturated branch's own tally) from the
   fixed whole-DT vapor total -- but the isotherm branch's own net adsorption/desorption ALSO moves
   water between phases, and in the scenario's own typical (non-supersaturated) operating regime,
   `total_condensed_kg_s` was often EXACTLY ZERO while `X1` still moved measurably (confirmed directly
   during the earlier direct_steam inversion work) -- meaning this correction was a near no-op in the
   common case, silently reintroducing isotherm-adsorbed water into the vapor stream it had just left.
   Fixed by using the solid's own net X1 change (`m_dry*(dcz_result.solid_out_X1 - X1_in_to_dcz)`,
   dry-solid-mass-scaled), which captures BOTH branches' combined effect exactly, independent of which
   branch(es) contributed.
3. **`core/zones/dcz.py` step 4, missing `[0,1]` clamp on `vapor_wV2`.** Found empirically running
   `dcz_zone_balance` against the module's own illustrative fixtures: a strongly-desorbing case (near
   -hexane-free vapor inlet diluted by a large net desorption flux) showed `wV2` drifting slightly
   negative (order 1e-4) -- the SAME class of numerical-boundary-drift `march_particle_mass`'s own
   `wpg2_clamped` already guards against, just never applied to this cascade. Fixed with the identical
   `min(1.0, max(0.0, ...))` pattern.

**A fourth, larger, CONFIRMED-BUT-DEFERRED gap: `core/zones/particle.py`'s `march_particle_mass` does
not conserve hexane mass exactly.** Found while empirically validating `dcz_zone_balance`'s own
hexane residual against test fixtures -- it was large (2.8%-16% relative), not a rounding artifact.
Isolated with a bed-scale-independent single-particle test (uniform `wpg2=1.0` initial condition,
fixed external `wV2_local`, no `dcz.py` coupling at all): the particle's own bulk adsorbed+absorbed
content (`X2,so`, eq. A.26) declines a STABLE ~18.6x faster than `march_particle_mass`'s own
`hexane_flux_to_vapor_kg_m2_s` diagnostic, integrated over time, accounts for -- confirmed stable
across a 20x sweep of `dt` (10s down to 0.5s), which RULES OUT a simple truncation/timestep error
(that would shrink toward 1x as `dt`->0). Root cause narrowed to (NOT fully confirmed): the interior
diffusion term uses `Ca` (eq. A.28's isotherm-slope-scaled effective diffusivity -- a standard
technique making a `wpg2`-based FVM conserve `X2,total` under the local-equilibrium assumption), but
the boundary convective term (`coeff_surf`) is applied WITHOUT the same `Ca` scaling, in the SAME
linear system. A direct fix attempt (scaling `coeff_surf` by the surface layer's own `Ca`)
overcorrected substantially (~0.34x, the opposite direction) rather than converging to 1x -- so this
diagnosis is plausible, not verified. **Explicitly discussed with the user and deferred**: fixing this
properly needs a careful re-derivation (ideally cross-checked against Coletto's own eq. A.29 more
carefully than available context allowed), not further trial-and-error guessing at a formula. NOT
fixed in this session. Flagged prominently at both the source (`march_particle_mass`'s own docstring)
and every consumer (`dcz.py`'s `wV2` clamp comment, `core/balance.py`'s module docstring and
`dcz_zone_balance`'s own docstring).

**Consequence for `dcz_zone_balance`'s own reliability, and how `tests/test_balance.py` is scoped
around it:** `hexane_kg_s`, and (whenever hexane transfer is non-negligible) `water_kg_s`/`energy_w`
too, inherit the gap above -- `water_kg_s` derives the vapor's own water content from `(1-wV2)`, so
it's contaminated by the same noise whenever hexane transfer is significant (confirmed: ~0.01%
relative on an isotherm-only/low-hexane-transfer case, ~24% on a heavy-condensation case where
hexane transfer is also large). This does NOT mean water's own physics is wrong -- the solid-side
`total_water_to_solid_kg_s` (computed from `X1` alone, no `wV2` involved at all) matches
`total_condensed_kg_s` to ~1e-4 relative in both cases checked, a genuinely independent, reliable
confirmation. Tests therefore: assert TIGHT mass+energy tolerances for PHZ/FTRZ (both fully reliable,
closed-form/root-solved single-pass balances) and DC (exact after the signature extension); assert a
TIGHT water-only check for DCZ on its low-hexane-transfer fixture, PLUS the hexane-independent
solid-vs-condensed cross-check on its heavy-condensation fixture; assert only a bounded/finite
regression-net check (not a tight tolerance) for DCZ's `hexane_kg_s`/`energy_w`, clearly commented as
pending the FVM fix above, rather than hidden behind a uniformly loose number.

**Live "very simple" diagnostic, `core/model.py`.** New `MassInventory` (nested on `Outputs`,
`Outputs.mass_inventory`), computed in the existing per-tick `outputs()` method from data already
available (`State.M/X1/X2`, `Inputs.feed_flow_rate/feed_moisture/feed_hexane`, the last stage's own
already-computed holdup) -- zero new solves, O(n_stages) cost. Reports raw `total_dry_solid_holdup_
kg`/`total_hexane_holdup_kg`/`total_water_holdup_kg` (summed across all stages) plus `feed_*_kg_s`/
`product_*_kg_s` rates. **Deliberately NOT itself a rigorous balance** (that's `core/balance.py`'s
job) -- `Model.step` stays pure (no persisted "previous tick" state), so the actual "should read ~0 in
steady state" signal is the CONSUMER's own tick-to-tick diff of the holdup totals (dashboard, or the
new `test_model.py::test_mass_inventory_holdup_settles_at_steady_state`, which runs 500 ticks at fixed
inputs and confirms the holdup totals stop moving). The `feed_*`/`product_*` RATES are exposed for
context only -- their own difference is normal evaporation/duty-driven mass loss, genuinely nonzero
in operation, NOT itself a conservation signal (unlike the holdup totals).

**Tests**: new `tests/test_balance.py` (9 tests, one/two per zone per the scoping above, plus the
whole-DT handoff check); `tests/test_thermo.py` gained a `mixture_cp_per_kg_dry_solid` regression
test; `tests/test_dc.py`'s 8 call sites updated for the new 5-tuple return, plus a new zero-flow
exit-state check; `tests/test_model.py` gained the steady-state holdup test above. Full suite (116
tests) green; `ruff check` clean; `assemble_model` timing re-confirmed at ~3.0-3.1s, unchanged (Phase
0's fixes are O(1)-per-cell, the balance module itself is test-only, the live diagnostic is
O(n_stages) -- none touch the hot solve path materially).

## DCZ moisture: real sorption isotherm, tray count reduction, DT temperature runaway fix (2026-07-18)

**Trigger.** Two independent requests from the same live session: (1) simplify the DT geometry to
2 PD trays / 1 MN tray / 1 sparge tray, and (2) the previous entry's own DCZ condensation mechanism,
while real and tested, never actually moved any tray's reported moisture in practice at the
scenario's own operating point -- the bed stayed superheated relative to vapor everywhere DCZ
operates, so the dew-point trigger never fired. Fixing (2) properly required admitting the model was
missing a whole regime: hygroscopic solids don't just condense-or-not, they sit in continuous
equilibrium with local vapor-phase humidity via a sorption isotherm, the same architecture hexane's
own GAB isotherm (`wpg2` -> `W2(a_h,T)`) already uses. That gap was closed this session using
`literature_sources/Gianini_Study_of_the_equilibrium_isotherms_of_soybean_meal.pdf` (Gianini, Luz,
Sousa, Jorge & Paraíso 2006) -- desorption isotherm data measured on meal sampled **directly from a
desolventizer/toaster's own outlet** (Cocamar's own DT), not a generic food-science material.

**Tray count reduction.** `scenarios/soybean_default.yaml`'s `geometry.stages` dropped PD3/MN2,
keeping PD1, PD2, MN1, SP1; `indirect_steam`/`sweep_arm_speed`/`gate_opening` operating defaults
updated to match (total duty per role conserved from the original 6-tray base case). Verifying this
surfaced two unrelated, already-latent bugs, fixed as prerequisites (`assemble_model` was silently
producing a degenerate/runaway profile even before any DCZ moisture work started):
- `disturbance_defaults.feed_hexane` was 0.26, below the porosity-derived critical loading
  `X2,cr≈0.41` (eq. 4) -- PHZ's own wet-core evaporation never engages below that threshold, so PHZ
  collapsed to zero length. Restored to **0.4743**, Coletto's own Fig. 1 base case value (already
  used by this project's `test_phz.py`/`test_dt_solver.py` fixtures) -- reproduces on the ORIGINAL
  6-tray geometry too, so this was a pre-existing bug, not something the tray change introduced.
- A sorption-heat singularity in `core/zones/particle.py`'s `sorption_heat_source_per_layer_w_m3`:
  the M2 Phase 4 floor (`W2_floored = max(W2, 1e-9)`) only avoided a literal `ZeroDivisionError`, not
  the resulting MAGNITUDE -- a real full-scale solve drives `W2` down to ~8.5e-8 near the DCZ exit,
  where the old floor let `dH_s` reach ~28,000x `dH_vap_hexane` (vs. the ~4-30x the cited Cardarelli
  & Crapiste 1996 "rises well above the heat of vaporization at low coverage" finding actually
  supports), driving a temperature runaway (~480 K). Refloored at `0.02*gab.Xm` (a physically
  motivated low-coverage scale, not an arbitrary constant), keeping `dH_s` within ~2 orders of
  magnitude instead of 4+. See `test_sorption_heat_source_bounded_at_near_zero_hexane_content`.
- Also persisted (previously only tested via ad-hoc config overrides, never written to the scenario
  file): `model.sweep_arm_transfer_gain` 0.2 -> **1.0**, the retuned value for the sweep-arm
  agitation enhancement to `bed_transport_coefficients` (see the M3a follow-up entry) once actually
  wired into a live scenario, landing DCZ's converged temperature in a validated band.

**Isotherm design, confirmed with the user before implementation (not independently assumed):**
water condenses, but unlike hexane there is no assumed free/liquid "wet core" for it -- condensed
water becomes bound moisture immediately (no re-evaporation path, matching how the existing
condensation mechanism already worked). Moisture uptake below saturation is governed by
**bidirectional** adsorption/desorption toward a solid/gas equilibrium (isotherm), not solid/liquid
contact and not a one-way accumulation. No radial (intraparticle) diffusion model for water -- lumped
per cell (no diffusivity-vs-radius data exists for water in this matrix, unlike hexane's own 12-layer
FVM). Moisture content should influence solid thermal properties (heat capacity, via the SAME
mass-weighting `core/dc.py` already used: `C_wet = cp_solid + X1*cp_water_liquid`), not thermal
conductivity (no data exists to derive that dependence). Modified LUIKOV isotherm (Gianini Table 7,
temperature-independent by the paper's own finding across its combined 15-70 °C dataset):
`Xe = A1/(1 + A2*ln(1/a_w))`, `A1=0.880, A2=12.184` (R²=0.99) -- `properties/soybean.yaml`'s
`water_luikov`. **Extrapolation caveat, stated not hidden**: DCZ's own operating temperatures (~380 K+)
sit above the paper's tested range; its finding that T barely matters is reassuring but doesn't cover
that gap.

**New primitives, `core/thermo.py`**: `LuikovParams(A1, A2)`; `water_activity(Y_V2, T, antoine_water)`
= `_y_water_mole_fraction(Y_V2)*P/antoine_pressure_pa(T,...)`, deliberately unclamped (`a_w>=1` is
mathematically the same condition as `T<=dew_point_temperature`, routed to condensation instead);
`luikov_equilibrium_moisture(a_w, params)`; `LUIKOV_MAX_VALIDATED_UR = 0.799` (Gianini's own highest
tested UR, their KCl data point) -- required because DCZ's vapor sits close to saturation almost
everywhere, exactly the isotherm's own untested tail (unclamped, it gave `Xe>0.5`, a pure
extrapolation artifact against the fitted curve's asymptote `A1`).

**`core/zones/dcz.py` mechanism**: a new per-outer-iteration pass ("step 4.5") marches solid moisture
top-to-bottom (matching solid flow) using THAT iteration's own just-converged vapor state: cells with
active condensation (`condensed_kg_s[j]>0`, from the existing dew-point checks) credit that mass
directly; other cells implicitly relax `X1` toward `Xe(a_w)` over the cell's own residence `dt`
(`X1_new = (X1_running + dt*kappa_w*Xe)/(1+dt*kappa_w)`, the same implicit-relaxation form the
particle-scale marches already use). The resulting latent heat (`water_latent_w_m3`) and mass
transfer (`water_mass_rate_w_m3`) feed back into the vapor's energy (step 2) and mass (step 4)
balances one outer iteration lagged -- the SAME lag category `q_condL`/`m_ax_net` already use, not a
new pattern.

**Three real bugs found and fixed while chasing a stubborn regression** (adding this latent-heat
coupling broke two previously-passing, unrelated sanity tests --
`test_facade.py::test_auto_mode_drives_pv_apc_style` and
`test_model.py::test_more_steam_raises_dt_target_temperature` -- by inverting the basic "more duty
implies hotter" relationship; confirmed directly by halving vs. doubling indirect steam duty and
watching the converged profile go the wrong way):
1. **Double-counted condensation latent heat.** Step 4.5 computed `water_latent_w_m3[j]`
   unconditionally from `mass_rate_kg_s = m_dry_kg_s*(X1_new-X1_running)` for BOTH the condensed and
   subsaturated branches -- but for condensed cells, that same mass's latent heat was ALREADY
   credited into the vapor's own energy balance within THE SAME iteration (step 2's
   `source_cond_actual_w_m3`, the closed-form back-solve). Recomputing it in step 4.5 and feeding it
   back as the NEXT iteration's `source` term double-counted the identical event, self-reinforcing
   each outer pass. Fixed by only computing `water_latent_w_m3[j]` in the subsaturated (isotherm)
   branch -- the docstring already stated this intent, the code just didn't enforce it.
2. **Missing water-mass-conservation term in the vapor's own balance.** The subsaturated isotherm
   branch could move `X1` (and hence latent heat) without ever debiting/crediting the vapor's own
   `wV2` -- unlike the condensation branches, which already debit `water_remaining_kg_s` for exactly
   this reason. In effect, adsorption could "manufacture" moisture from nowhere and desorption could
   dump it without diluting the local humidity. Fixed by adding a matching lagged
   `water_mass_rate_w_m3[j]` term into step 4's own `wV2` cascade (`+water_mass_rate_w_m3[j]`: water
   leaving the vapor via adsorption raises hexane's own share `wV2` of what remains). Confirmed via a
   direct sign check (an earlier version had this backwards, driving `wV2` slightly negative).
3. **Out-of-tested-range literature extrapolation dominating the energy balance.** Even after fixing
   (1) and (2), the SAME two tests still failed with nearly unchanged numbers. Direct instrumentation
   (dumping `q_Iv_profile` vs. `water_latent_w_m3` per cell across outer iterations) showed the
   isotherm's own latent-heat term reaching 2-5x the indirect-steam duty's own magnitude in every DCZ
   cell of the real scenario -- dominating the energy balance via a real (not double-counted)
   thermostatic effect: hotter meal holds less bound moisture at equilibrium, so more duty -> hotter
   -> lower `a_w` -> lower isotherm target -> desorption's own negative heat credit outran the direct
   duty benefit. Root cause: `kappa_w = 15*water_diffusivity/r_P^2` (Glueckauf LDF, using Touffet et
   al. 2026's own highest-measured diffusivity, 60 °C) implied a ~1.8 min equilibration time constant
   that, cascaded across DCZ's own multi-cell residence, drove near-full equilibration within a
   single tray -- plausible in isolation, but Touffet's own material (pelleted ANIMAL FEED, tested
   only 25-65 °C) is a different matrix than toasted SOYBEAN MEAL at DCZ's own ~100-140 °C, a genuine
   extrapolation gap. Derated `water_diffusivity` by a documented, ENGINEERING-JUDGMENT factor of 20
   (not literature-derived) -- confirmed via a sweep (10x still marginal/inverted in places, 15x
   monotonic but thin margin, 20x comfortable margin, up to 50x diminishing extra benefit) --
   `properties/soybean.yaml`'s `water_diffusivity: 3.1e-11` (was `6.2e-10`). See that file's own
   comment for the full derivation history (this term went through three forms across the session:
   reusing hexane's own `hM*aV`, Touffet's convective coefficient, then Touffet's diffusion
   coefficient via the LDF form -- each confirmed too fast before landing here).

**Sparge steam supply temperature, corrected a second time.** The previous entry's own ~3 barG
(~144 °C) guess, sourced from the user's own recollection at the time, was superseded this session by
more specific real-plant knowledge: sparge/direct steam actually runs ~0.5-1.5 bar gauge (~100-110
°C), not 3 barG. `scenarios/soybean_default.yaml`'s `direct_steam_pressure_barg`: 3.0 -> **0.3**.
`tests/test_dt_solver.py`'s own standalone fixture `T_direct_steam` recomputed to match (380.67 K,
was a stale 416.98 K reflecting the old 3 barG guess) -- this fixture is independent of the scenario
file, so needed its own explicit fix; it was the reason a newly-added acceptance test
(`test_direct_steam_does_not_invert_sparge_moisture`) initially failed even after the real scenario
itself was already fixed.

**Net result on the actual bug this whole entry exists to fix**: `direct_steam` sweeps (0 vs. 4 kg/s,
well above SP1's own 1.5 kg/s operating default) now leave SP1 wetter, not drier, with direct steam
present -- the inverted 7.40%->7.00% behavior from the previous entry is gone. This is a NET-direction
fix, not a claim of strict monotonicity everywhere: the isotherm's own thermostatic feedback is real
and produces a small (~0.15%-relative) non-monotonic dip in the deeply-subsaturated regime before the
condensation threshold, then a step up once condensation actually triggers near the operating
default -- documented in `test_dt_solver.py::test_direct_steam_does_not_invert_sparge_moisture`'s own
docstring rather than hidden.

**Moisture-dependent particle heat capacity (`core/zones/particle.py`).** `march_particle_energy`
gained an `X1: float = 0.0` parameter (default preserves every pre-existing call site bit-for-bit --
see `test_x1_zero_reproduces_default_energy_march`), extending `Cv` with
`+ c.alpha_ps*c.rho_ps*X1*c.cp_water_liquid`, mirroring `core/dc.py`'s own `C_wet` precedent exactly.
`ParticleConstants` gained `cp_water_liquid`. `dcz.py`'s step 1 call site passes
`X1=X1_profile[j]` (previous-iteration value, same lag category as `dwpg2_dt_prev`). Thermal
conductivity intentionally NOT made moisture-dependent (no data).

**Dryer/Cooler isotherm reuse (`core/dc.py`).** Replaced the old air-side-mass-balance-derived
moisture target (a constant-rate-drying assumption: the solid can always supply whatever the air's
own humidity deficit calls for) with the SAME Luikov isotherm DCZ now uses, evaluated at the
INCOMING air's own water activity (`_air_water_activity`, the algebraic inverse of the existing
`saturation_humidity_ratio` psychrometric relation -- NOT `thermo.water_activity`, whose `Y_V2`
convention is hexane-vapor-relative and meaningless for a hexane-free air stream). The existing
`effectiveness`/energy-cap relaxation wrapper is unchanged -- only the target changed, from "however
much the air's humidity deficit can evaporate" to "whatever's in equilibrium with the air's own
relative humidity." Two side effects, both confirmed correct rather than assumed: (1) above water's
own boiling point (the scenario's own `heated_air_temp=380 K`), `p_water,sat(T)` is large, so `a_w`
naturally comes out very small and the isotherm target goes to near-zero BY CONSTRUCTION -- no
special-case workaround needed anymore for the old "no physical ceiling above bp" failure mode; (2)
the target is now genuinely BIDIRECTIONAL -- a sufficiently dry solid can ADSORB moisture from normal
(not just supersaturated) air, exothermically, which `test_hot_air_on_a_nearly_dry_solid_produces_
net_warming` was updated to expect (net warming now comes from adsorption's own exothermic release in
addition to sensible heating, not from evaporation stopping). The energy cap (evaporation can't
exceed available sensible heat) only applies to the evaporation direction -- adsorption has no
equivalent cap, the effectiveness-bounded relaxation is already self-limiting there.
`DCConstants` gained `luikov: thermo.LuikovParams`.

**Shared constant deduplication**: `LUIKOV_MAX_VALIDATED_UR` moved from a private `dcz.py` module
constant to `thermo.py` (now exported), since `dc.py`'s own isotherm reuse needed the identical
bound.

**Tests**: `tests/test_dcz.py`'s condensation test rewritten (the old `weak_hM` isolation hack is
meaningless now that `kappa_w` no longer derives from `hM` at all -- placing `vapor_inf` below its
own dew point triggers the boundary flash check directly instead); a new subsaturated-regime test
checks direction (dry desorbs down, wet adsorbs up) and genuine convergence, not exact destinations
(the latent-heat coupling makes entry-point-dependent convergence legitimate now); both needed
materially higher `outer_max_iter` after the mass-conservation fix closed a genuinely tighter
mass<->energy<->mass loop. `tests/test_particle.py` gained the X1=0 regression check and a
moisture-raises-Cv direction check. `tests/test_dc.py` gained `luikov` to its fixture and had one
test's expectation corrected (adsorption, not continued drying, at extreme dryness). New permanent
acceptance test `tests/test_dt_solver.py::test_direct_steam_does_not_invert_sparge_moisture` guards
the actual bug this entry exists to fix. Full suite (105 tests) green;
`assemble_model` timing re-checked at ~3.0 s, within the established real-time budget.

**Explicitly out of scope**: FTRZ/PHZ's own water handling untouched (this was all DCZ+DC); no
thermal-conductivity moisture dependence (no data); no vapor-phase axial dispersion specifically for
water (hexane's own `D_ax` term isn't mirrored for water); no isosteric (excess) heat of sorption for
water (Gianini's isotherm is temperature-independent by construction, so a Clausius-Clapeyron-derived
isosteric heat can't even be extracted from it -- plain `dH_vap_water` used throughout, matching
`core/dc.py`'s own pre-existing precedent).

## DCZ moisture (H2O) balance + sparge steam supply temperature (2026-07-15)

**Trigger.** The previous entry (tray simplification / DT runaway) surfaced from the same live
report: changing `direct_steam` (SP1's sparge MV) moved no tray's reported moisture at all.
Root-caused (not assumed) to `core/zones/dcz.py`'s `VaporState` carrying literally no water
balance -- see that entry for the trace.

**Mechanism, added to `core/zones/dcz.py`'s existing Primary Internal Loop rather than a bolt-on
pass:** no solid-side water-sorption isotherm exists in this project's cited literature
(`core/dc.py`'s own module docstring), so moisture uptake is bed-scale/energy-balance-driven only,
the same category `zones/ftrz.py` already uses for its own V-SCAL/V-SAT switch ("moisture gain
equals whatever water the vapor condensed in that cell"). Reused `thermo.dew_point_temperature`/
`AntoineParams` (already wired for FTRZ) rather than new physics. Two distinct checks turned out to
be necessary, found by testing against the real scenario, not assumed upfront:
1. **Cell-output check**: step 2's existing per-cell implicit relaxation may compute a candidate
   vapor temperature below that cell's own dew point -- cap it and back-solve (closed-form, the
   relaxation equation is linear in its source term, no root-finding needed, unlike FTRZ's own
   `brentq`-based V-SAT branch) the condensed-water mass the SAME equation implies.
2. **Inflow (boundary) check, added after the first version showed zero effect on the real
   scenario despite a supersaturated bottom BC:** a coarse cell's own strong indirect duty can
   re-superheat an already-supersaturated INFLOW within that same cell, so checking only the
   cell's output silently missed condensation that must physically happen right at entry, before
   any convective heating -- confirmed by direct instrumentation (the bottom-most cell's own
   OUTPUT already read 380 K, well above the ~373 K dew point, despite a ~331 K inflow). Flash
   -condense against the inflow alone (simple sensible-to-latent energy balance, no kappa_e/source
   terms -- those belong to the cell's own march) before the cell's normal balance applies.

Both mechanisms clamp condensation to the water actually flowing at that point (never manufacture
mass). Condensed water accumulates top-to-bottom (solid flow order) into a new per-cell `X1_bulk`,
replacing `core/dt_solver.py`'s previous flat `X1=exit_X1` carried unchanged through every
DCZ-spanned tray. `DCZConstants` gained `dH_vap_water`/`antoine_water` (both already computed for
`FTRZConstants` upstream, no new properties-file values needed). The FTRZ handoff
(`new_vapor_in` in `dt_solver.py`) now subtracts DCZ's own `total_condensed_kg_s` from the fixed
whole-DT vapor total instead of assuming it's conserved. **Documented simplification, consistent
with an existing one**: total vapor mass flow (hence `u_V`/`hQ`/`hM`/`aV`, computed once upstream
from the bottom BC) does NOT get updated as condensation removes mass -- the same simplification
already accepted for FTRZ's own (comparatively larger) hexane evaporation.

**Sparge steam's own supply temperature -- a second, deeper bug found while chasing why the
mechanism above still showed zero visible effect on the real scenario.** `dt_solver.py`'s bottom BC
mixing formula (`T_bottom`) was mixing `direct_steam` in at `ftrz.T_boil_water` -- water's
ATMOSPHERIC (1 atm) boiling point, the DT vessel's OWN internal vapor-space reference, not the
steam SUPPLY's own temperature. Checked this project's own
`literature_sources/Svoboda_Case_for_Advanced_Process_Control_VRX-DTDC_Concept.pdf` (rendered the
actual page as an image after `pdftotext`'s column extraction scrambled the table): confirms
INDIRECT steam at 9.5 barG (~185 °C, matching `DTDC_steady_state_reference.md`'s independently
-cited "~185 °C (10 barg)"), but doesn't state direct/sparge steam's own pressure. Confirmed with
the user (the paper's own author) as ~3 barG typical plant practice (~144 °C saturated, via the
SAME `antoine_water` correlation `_antoine_boiling_point_k` already uses for `T_boil_water`, just
at a different pressure). New `ModelParams.direct_steam_pressure_barg` (default 3.0); `builder.py`
derives `T_direct_steam` from it and threads it into a new `DTSolverConstants.T_direct_steam`
field, used ONLY in the `T_bottom` mix (NOT `ftrz.T_boil_water`, which stays correct at 1 atm for
FTRZ's/DCZ's own internal dew-point/enthalpy reference -- a genuinely different quantity from what
the steam supply itself arrives at).

**Net result, and an honest account of what's still open:** at the CORRECTED ~144 °C sparge supply
temperature, injected steam is now comfortably hotter than the DT's current (still-elevated, see
previous entry's own "DT runaway" finding) internal vapor temperatures (~390-420 K / ~117-147 °C at
this scenario's current, still-uncalibrated indirect duty) -- so it now visibly and correctly
raises bed temperature (confirmed: SP1 rose from 391.5 K at 0 direct steam to 403.4 K at 4 kg/s),
but STILL doesn't trigger condensation in THIS scenario's specific tuning, because the vapor stays
superheated relative to the (still too-hot) bed everywhere in DCZ. This traces directly back to the
SAME open gap the previous entry already flagged and deliberately left unresolved (indirect duty is
still `[PLACE]`, PD1 still runs ~417-423 K vs. the scenario's own ~342-383 K validation targets) --
not a new bug. Once that separate calibration lands and DCZ's own vapor temperature drops toward
its target band, sparge steam (now correctly modeled above the dew point) should condense on
contact with the cooler bed exactly as `Svoboda_Case_for_Advanced_Process_Control_VRX-DTDC_Concept.
pdf`'s own SD-tray note describes ("Direct steam toasting; 15-16% H2O") -- the mechanism is real,
tested, and ready for that; the scenario's own duty just isn't calibrated to exercise it yet.

**Tests**: `tests/test_dcz.py` gained dedicated condensation tests (a supersaturated case with
`total_condensed_kg_s > 0`, monotonic `X1_bulk`, never exceeding available water; a no-condensation
case confirming zero effect when boundary conditions stay superheated) -- the module's own OLD
illustrative boundary temperatures (371-372 K) turned out to sit almost exactly at the water dew
point, so raised them to 389-390 K to keep the EXISTING shape tests condensation-free (as they were
designed), not by coincidence. `tests/test_dt_solver.py` checks the balance is wired and mass
-conservative on its own illustrative fixture (which, like the real scenario before duty
recalibration, doesn't naturally trigger condensation either -- confirmed by direct instrumentation,
not assumed, before writing the test this way).

## DT runaway temperature + tray simplification (2026-07-15)

**Trigger.** Three live-testing requests: simplify the DT to 2 PREDESOLV + 1 MAIN + 1 SPARGE trays;
fix the DCZ moisture balance (`direct_steam` wasn't moving any tray's moisture — see the next
DECISIONS.md entry for that fix); check whether the zonal mesh's `dz` tracks actual tray fill level
(it doesn't — a documented finding, not a code change: `dz = bed_height_m/nz` everywhere uses the
static design `bed_height_m`, decoupled from the dynamic holdup-derived level%; making the zonal
solve live-follow the dynamic level would mean re-deriving bed height as a per-tick state and
re-meshing every resolve, a separate, much larger undertaking than what was asked).

**Tray reduction.** Confirmed by code audit before touching anything: no file under `src/` hardcodes
a tray count or specific tray ID — `_phz_pass`/`_build_dcz_domain` (`dt_solver.py`) iterate whatever
`trays` list they're given, grouped by `role`, not position. Pure `scenarios/soybean_default.yaml`
edit: dropped `PD3`/`MN2`; `indirect_steam`/`sweep_arm_speed`/`gate_opening` lost their now-invalid
keys (a dict key referencing a removed stage id fails `schema.py`'s `check_physical_consistency`
validator). Each role's TOTAL duty from the original 6-tray base case was conserved across the fewer
remaining trays of that role (PD 4.0e5×3=1.2e6 → 6.0e5×2; MAIN 1.2e6+8.0e5=2.0e6 → one 2.0e6 tray)
rather than deleting PD3's/MN2's capacity outright. `tests/test_dt_solver.py`'s own 6-tray fixture is
independent of the scenario YAML and was left as-is (it validates the solver generically, not this
scenario); `tests/test_config.py::test_load_scenario_ok` updated for the new `n_stages`.

**DT runaway temperature — found while verifying the tray reduction, pre-existing, unrelated to it.**
Verifying "PHZ still reaches `X2,cr`" after the tray edit surfaced `PD1` (and the whole DT) converging
to ~460-480 K (190-210 °C) — reproduced identically with the *original* 6-tray geometry and with
`solve_dt`'s tight (non-real-time) tolerances, so not caused by the tray edit or the earlier A2
tuning. Root-caused by direct instrumentation, not assumed:

1. `feed_hexane` (0.26 kg/kg, the scenario's own "~25-35%" estimate) sits *below* `X2,cr` (0.41 kg/kg,
   computed from cited Cardarelli/Cardarelli-Crapiste-Mattea particle-porosity and density values —
   eq. 4, correctly implemented). PHZ's own definition (continues while `X2 > X2,cr`) makes this a
   mathematically legitimate zero-length PHZ, not a code bug — real extractor-exit meal may
   genuinely already sit below the "excess free liquid hexane" threshold these particular particle
   parameters imply. Left as-is; not touched, since both cited values are independently defensible
   and there's no principled way to prefer one over the other without new literature.
2. With PHZ length 0, nearly all of the feed's hexane (X2 0.26 → FTRZ's own equilibrium floor ~0.013)
   evaporates within FTRZ's thin (~4 cm) receding-front zone instead of the intended PHZ+FTRZ split,
   and the DCZ particle model (entering at `wpg2=1.0` uniformly, per its own IC) consequently spends
   much more of its own domain desorbing down toward very low coverage. Direct instrumentation of a
   real `solve_dt` call found the particle's own GAB hexane content (`W2`) reaching **~8.5e-8**
   (essentially at, not just near, the existing `W2_floored = max(W2, 1e-9)` floor in
   `sorption_heat_source_per_layer_w_m3`, `core/zones/particle.py`) — a floor added in M2 Phase 4
   *only* to avoid a literal `ZeroDivisionError` on eq. A.31's negative-exponent power law, never
   checked against the resulting MAGNITUDE. At `W2≈8.5e-8`, `dH_s = dH_lv2 + C0·W2^C1` evaluates to
   **~9.5e9 J/kg — ~28,000× `dH_vap_hexane`**, an order of magnitude of heat release with no physical
   basis (`sorption_C0`/`sorption_C1` remain uncalibrated `[PLACE]` — the underlying Cardarelli/Faner
   thesis is unrecoverable, per `properties/soybean.yaml`'s own note — so this is a genuinely
   unbounded free parameter, not a value trustworthy at any magnitude the power law happens to hit).
   Confirmed as the dominant term directly: peak sorption source ~3.78e7 W/m³ (particle volume) vs.
   this scenario's own indirect-duty density ~1.6e5 W/m³ (bed volume) at the same point — roughly
   240× larger, and duty scans (0.05×-1.0× the scenario's indirect_steam) barely moved the converged
   temperature (456→480 K), ruling out duty as the driver.

   **Fixed**: raised the floor from the bare constant `1e-9` to **2% of the GAB monolayer capacity**
   (`0.02 * c.gab.Xm`, ≈1.04e-4 at this scenario's own `Xm`) — a physically motivated low-coverage
   scale (tied to the isotherm's own parameters) rather than an arbitrary round number, bounding
   `dH_s` to ~90× `dH_vap_hexane` at this scenario's own `Xm` instead of 28,000×, while leaving both
   of `test_heat_of_sorption_exceeds_latent_heat_at_low_moisture`'s existing checkpoints (`W2=0.001`,
   `W2=0.1`) unaffected (both sit well above the new floor). Explicitly a stopgap, not a calibration
   — documented as such in `particle.py`'s own docstring, since `sorption_C0`/`sorption_C1` still
   have no source to calibrate against.

**Result**: `PD1` dropped from 480 K to 416.7 K with the scenario's retuned duty; `SP1` now lands at
386.8 K, inside the scenario's own stated validation band (~378-383 K / 105-110 °C at DT exit). Not a
complete calibration — hexane content still converges to ~9,000-13,000 ppm across the DT (target
`<500-800 ppm`), a separate, pre-existing gap (mass-transfer coefficients / DCZ iteration budget /
the PHZ-length question above) not addressed here; flagged, not chased, since it wasn't what surfaced
this session and touches yet more `[PLACE]`/`[DERIVED]` constants. New test:
`test_sorption_heat_source_bounded_at_near_zero_hexane_content` (`tests/test_particle.py`) checks the
floored `dH_s` stays finite and under 200× `dH_vap_hexane`.

## M3a follow-up — DT solve performance, empty-start IC, live-tunable resolve cadence (2026-07-14)

**Trigger.** Live use of M3a surfaced two problems: the DT appeared "stuck" for up to 30s after any
input change (expected given the periodic-resolve design, but a rough UX), and the user set a firm
target — `dt_resolve_interval_s` should never go below 120s (sim-time), and `solve_dt`'s own
wall-clock cost should stay ≤4s so a 120s/20x cadence (6s wall gap) keeps real-time headroom.

**Profiling (real `cProfile` + direct timing, not estimated) found two concrete causes, one of them
a genuine inefficiency introduced by M2 Phase 4's own energy-balance bug fix.**

1. `_sorption_heat_source_w_m3` (the expensive isotherm-slope calculation, `core/zones/particle.py`)
   was called TWICE per particle layer per march — once inside `march_particle_energy`'s own matrix
   build, again inside the `sorption_heat_sink_volumetric_mean_w_m3` wrapper added earlier to fix the
   unbounded-cooling bug. Same computation, redundant call — ~14s of ~31s in a cold `assemble_model`
   run. **Fixed**: renamed to public `sorption_heat_source_per_layer_w_m3` (drops the `q_condL` term,
   callers add it themselves); `march_particle_energy`'s signature changed from
   `(q_condL_w_m3, dwpg2_dt_prev)` to one pre-computed `sources_w_m3` tuple; `dcz.py`'s step-1 loop
   computes the sorption sources once and reuses them for both the march and the bed-scale energy
   credit. Bit-identical results (pure computation-reuse, no physics change) — confirmed by
   `tests/test_dcz.py`/`tests/test_dt_solver.py` needing zero assertion changes;
   `tests/test_particle.py`'s 4 direct call sites needed only mechanical signature updates.
2. **The bigger lever**: `dt_solver.py`'s own FTRZ↔DCZ outer-loop convergence check (`outer_tol=
   1e-5`) applies the same absolute threshold to a Kelvin-scale quantity (T, ~300-450) and a
   mass-fraction quantity (wV2, ~0.0005). Traced directly (not assumed): wV2 converges by iteration
   ~5; T is still creeping by <0.001%/iteration at iteration 50+, burning 40+ extra iterations chasing
   precision far tighter than the model's own placeholder-constant uncertainty (`hQ`/`hM`/`aV`, still
   `[DERIVED]`/`[PLACE]`) warrants. **Fixed**: new `ModelParams` fields `dt_outer_tol`/
   `dt_outer_max_iter`/`dt_dcz_inner_max_iter`, threaded into `core/model.py`'s `_resolve_dt`/
   `init_state()` calls to `solve_dt` — kept strictly separate from `solve_dt`'s own function-signature
   defaults (`1e-5`/`100`/`100`, unchanged), so `tests/test_dt_solver.py` (which calls `solve_dt`
   directly without overriding these) keeps validating against the tight, conservative settings; only
   the real-time engine's own calls get the loosened, speed-tuned values (scenario ships
   `dt_outer_tol=0.05, dt_outer_max_iter=20`; the 20-pass cap is superseded
   by the adaptive-solver decision below).

**Combined measured result** (this session, this machine — hardware-dependent, retime if it matters):
~10-14s (tight defaults) → ~3.2s (`assemble_model`, tuned settings) for the scenario's real-time
mesh, for a ~0.7 K / ~2 ppm difference in the converged answer. At the 120s floor and this scenario's
`speed_factor=20`, the wall-clock gap is 6s — ~1.9x margin over the measured cost. Full pytest suite
runtime dropped too (~88s → ~45-49s), since `test_model.py`/`test_facade.py` now pay for cheaper real
`solve_dt` calls throughout.

**One test assertion changed as a direct, expected consequence, not a workaround**: 
`test_more_steam_raises_dt_target_temperature` no longer asserts `dt_converged` — at
`outer_max_iter=20`, the formal convergence flag often reads `False` even though the profile is
already within ~1K of its asymptote (the same finding driving the tuning above); `converged` is a
diagnostic for `SolverStress`, not a precondition for the profile being directionally meaningful,
which is what that test actually checks.

**Start empty (watch material propagate through the unit).** New `SimConfig.dt_start_empty: bool`.
`Model.init_state(seed, start_empty=False)` **always** runs the one real `solve_dt` call regardless
(still needed — it's what `dt_target_T/X1/X2` relax toward either way); `start_empty=True` only
changes how the *actual* starting state is seeded: `M=0` (no holdup), `T=`feed temperature,
`X1=X2=0` (no material present) — the existing, unchanged first-order lag mechanism then fills the
plant toward `dt_target_*` over simulated time. Verified live (Playwright): with the new Setup-screen
checkbox on, every DT tray starts at feed T / 0 ppm / 0% level; after 276 simulated seconds of a run,
PD1 had genuinely climbed to 181.5 °C / 8476 ppm / 63% level.

**Live-tunable `dt_resolve_interval_s`, floor enforced at 120s.** Moved from `ModelParams` (frozen)
to `OperatingDefaults`/`Inputs` (hot, per-tick) — the exact same pattern `feed_flow_rate`/
`heated_air_temp`/etc. already use via `RuntimeFacade._read_effective_inputs_locked`.
`core/model.py::Model.step()`'s resolve-gate condition reads `u.dt_resolve_interval_s` (was
`c.dt_resolve_interval_s`). Floor enforced in three places, defense in depth: schema (`Field(ge=120)`
on `OperatingDefaults.dt_resolve_interval_s`), facade (`RuntimeFacade.set_dt_resolve_interval_s`
clamps rather than rejects, mirroring `set_speed_factor`'s own convention), and the UI control's own
`min=120`. Exposed live: a number input next to the existing Speed factor slider, plus a read-only
label computing the resulting wall-clock gap (`dt_resolve_interval_s/speed_factor`) so the tradeoff
is visible, not hidden in a YAML file — verified live to read "(~6s wall-clock between DT updates)"
at the scenario's own 120s/20x default. Also exposed as a new writable `Sim/DTResolveIntervalS` OPC
UA node (same change-detection pull/push pattern as the existing `Sim/SpeedFactor`).

**DT tray count / spatial nodes: a finding, not a change.** Profiling showed the dominant costs are
DCZ's spatial resolution (`nz_dcz`) and outer/inner iteration counts (both addressed above) — not the
number of real DT trays (tray count only affects the cheap PHZ pass and per-tray duty-mapping
granularity). Left tray count and `dt_nz_phz/ftrz/dcz` unchanged; revisit only if there's a reason
unrelated to the 4s target (e.g. matching a specific real plant's tray count).

## M3a — Wiring the integrated DT solve into the real-time engine (2026-07-14)

**Scope.** BuildSpec §14 M3, first slice: wrap `core/dt_solver.py::solve_dt` as `core/model.py::
Model.step()`'s DT-role physics (§7.9), implement the DC (dryer/cooler) air-contacting stages
(§7.10), and make `SolverStress` reflect real convergence (§9.1). Explicitly split from **M3b**
(a separate, future effort): a persistent, per-tick-advected particle-state redesign of
`core/zones/dcz.py`, needed to eventually recompute the DT every tick rather than periodically —
see the "solve cadence" decision below for why that split was made.

**Solve cadence — user-confirmed, phased decision.** `solve_dt` measured at 9-60+ seconds per call
even at a deliberately coarsened mesh, against a `dt_wall_s = 0.2s` tick budget. Warm-starting the
FTRZ↔DCZ outer coupling doesn't fix this: `solve_dcz_zone` always re-derives its particle field from
a fresh residence-time cascade every call (Coletto's own "particle pores initially saturated with
hexane at DT entry" assumption), which is the actual cost driver, not the outer coupling. Properly
fixing this means giving DCZ's particle field genuine per-tick persistence and advection — flagged
by BuildSpec §7.13 itself as "full transient bed scale," an optional post-v1 fidelity rung, not
baseline M3. Presented to the user as a three-way choice (recompute every tick anyway/periodic
resolve/persistent-state redesign); the user chose the persistent-state redesign as the ultimate
direction, but — recognizing its own scope (a genuine free-boundary/advection numerical redesign,
not a small change) — agreed to phase it: **M3a ships a working, tested, periodic-resolve baseline
now; M3b (persistent DCZ state) is separate future work built on top of it.**

**Mechanism: `Model.step()` re-runs `solve_dt` only when accumulated sim-time since the last solve
exceeds `ModelParams.dt_resolve_interval_s`** (new field). Between resolves, DT-role stages'
equilibrium targets are held at the last converged `DTResult`'s per-tray values
(`State.dt_target_T/X1/X2`, new fields, one entry per DT-role stage), while the *existing*
first-order lag/holdup relaxation (`State.T/X1/X2/M`, `_stage_tau`, unchanged since M0) keeps
relaxing toward them every tick — this satisfies §7.9's transport-lag requirement by reusing 100%
of already-tested machinery, not adding a second lag mechanism. `_stage_equilibrium` (the M0
placeholder mechanistic cascade) is deleted outright, not kept as a fallback.

**Wall-clock-vs-cadence tuning is a real operational parameter, left explicit, not hidden.**
`dt_resolve_interval_s` is *sim*-time; the *wall-clock* gap between solves is
`dt_resolve_interval_s / speed_factor`, which must stay comfortably above `solve_dt`'s own
(hardware-dependent) wall-clock cost or the tick loop stutters every resolve. Reference scenario
ships `dt_resolve_interval_s: 600.0` (10 min sim-time) against `speed_factor: 20` → a 30s wall-clock
gap, ~2.5x the ~12s measured cost at the scenario's own coarsened real-time mesh (`dt_nz_phz: 10`,
`dt_nz_ftrz: 10`, `dt_nz_dcz: 8` — the last verified in M2 Phase 4's own testing to stay
converged/monotonic, not picked blindly). Retune both together if either changes; `SolverStress`/
`actual_speed` telemetry (already existing, already tested) is the intended feedback signal, not a
one-time calculated constant.

**No `engine/`/`interfaces/` threading changes needed — confirmed by direct inspection, not
assumed.** `RuntimeFacade.tick()` already calls `model.step()` *outside* its lock, and the OPC
UA/UI adapters only ever touch the facade's locked snapshot/setter surface, never `Model.step()`
directly. A slow periodic solve stalls the tick loop's own worker thread for its duration (already
reported as a dip in `actual_speed`) but never blocks the UI or OPC UA server. This is exactly why
M3a's own scope stayed small in `engine/`/`interfaces/`: one line in `facade.py` (`solver_stress`
now also reflects `DTResult.converged`, not only a raised exception) and two additive `Outputs`
fields surfaced through the existing PV/Sim node machinery.

**`init_state()` now runs one real `solve_dt` call** (BuildSpec §4: "compute steady-state x0 via
initializer at the operating defaults") instead of seeding a uniform feed-state placeholder — a
one-time setup-phase cost, not tick-budget-constrained. `OperatingSeed` grows to carry
`feed_flow_rate`/`indirect_steam`/`direct_steam` from `OperatingDefaults` (already in config, just
not threaded through before) so `init_state` can build the same `DTTray` list `step()` does. Left
to raise naturally on failure — a genuinely bad config should block the CONFIGURED→READY transition
(the facade already does this correctly: the state-machine transition is the last statement in
`assemble()`), not be silently caught.

**DC (dryer/cooler) real air-contacting model (`core/dc.py`, new) — §7.10, `DECIDE` as the spec
explicitly allows.** One well-mixed 0-D air-solid contactor, shared by DRYER and COOLER (only the
air-stream arguments differ, per §7.10's own "use the same per-stage balance structure"). Solid-side
moisture equilibrium is derived from an air-side mass balance (constant-rate drying-period
assumption), not an independent water-sorption isotherm — this codebase's GAB isotherm is
parameterized for HEXANE sorption only (Cardarelli & Crapiste 1996); no water-sorption correlation
exists or is cited anywhere in `literature_sources/`.

**Real bug caught by testing before it shipped: unbounded-cooling-style failure, this time
overheating/undercooling, from an unphysical psychrometric ceiling.** The single-component
saturation-humidity formula (`Y_sat`, Raoult's law against a pure liquid) has no physical meaning
above water's own boiling point at 1 atm (373.15 K) — no liquid phase exists there to be in
equilibrium with. The scenario's own `heated_air_temp` (380 K) sits above it. Taken at face value,
the resulting "evaporate all available moisture instantly" result demanded far more latent heat than
the air stream could thermodynamically supply, driving `T_eq` to ~180 K — below both boundary
temperatures, with no external refrigeration in the model. Root-caused and fixed by capping
evaporation at whatever the available *sensible* heat can actually support (`m_evap_energy_cap`),
not just by humidity driving force and moisture availability. This is a physically real regime, not
an edge case: at realistic dryer air:solid mass ratios (this scenario's own ~0.2-0.3), water's latent
heat (~2.26 MJ/kg) is expensive enough relative to a typical air stream's sensible capacity
(~1 kJ/(kg·K)) that evaporation is *usually* energy-limited, landing `T_eq` at (not above) `T_in` —
the physically-expected "constant-rate drying period" plateau of an evaporatively-cooled wet
surface, not a bug, but a genuinely different (and more muted) temperature response than the old M0
placeholder implied. Both regimes (energy-limited plateau, and net warming once moisture is scarce
enough for availability to bind instead) are covered in `tests/test_dc.py`.

**`SolverStress` now reflects real DT-solve non-convergence, not only a raised exception.**
`Outputs` gained `dt_solver_converged`/`dt_solver_outer_iterations` (threaded from `DTResult`);
`RuntimeFacade.tick()`'s `solver_stress` flag is now `(exception raised) or (not
y.dt_solver_converged)`. Surfaced as a new `Sim/DTSolverOuterIterations` OPC UA node and a tooltip
on the UI's existing SOLVER STRESS badge — additive only, no structural change to either adapter.

**Test-suite performance, addressed directly (not deferred).** `solve_dt`'s real cost meant the
existing 300-500-tick-loop test convention (`test_model.py`, `test_facade.py`) could have ballooned
to many minutes. Mitigations, all confirmed empirically (not assumed): (1) `test_model.py`'s
`assemble_model` call (now expensive via `init_state`) is computed once via a module-scoped pytest
fixture and shared read-only across tests, not re-run per test; (2) tests that only need the LAG
mechanism (unrelated to the DT solve, e.g. gate-opening/bed-holdup, protein decay-over-time) hold
`t` constant across their loop, matching their pre-M3a pattern exactly — this deliberately never
crosses the resolve cadence, confirmed by inspection of the resolve-gate condition, not by tests
happening to run fast; (3) the one test that genuinely needs to compare DT-role temperature response
to *different* steam duties (`test_more_steam_raises_dt_target_temperature`) was redesigned to
trigger exactly one `solve_dt` call per trajectory directly (a near-zero `dt_resolve_interval_s`
override + `t` past it on the first call), replacing a 500-tick loop that would have crossed the
resolve boundary an uncontrolled number of times; (4) `test_facade.py`'s facade-level tests (which
verify plumbing — state transitions, MV routing — not DT physics correctness, that's
`test_dt_solver.py`'s/`test_model.py`'s job) use an even more aggressively coarsened mesh
(`dt_nz_phz=5, dt_nz_ftrz=5, dt_nz_dcz=4`) local to that test file's own config copy, an appropriate
trade given what they actually verify. Full suite: 98 tests, ~88s (was ~46s pre-M3a) — the added
time is almost entirely the now-unavoidable real `solve_dt` calls each `assemble_model`/resolve-
crossing test performs, not incidental slowdown.

## M2 Phase 4 — Integrated DT solve: connecting PHZ + FTRZ + DCZ (2026-07-14)

**Scope.** BuildSpec §14 M2's last slice / §7.8: `core/dt_solver.py` (`solve_dt`), the tray-by-tray
fixed-point sweep connecting the three standalone zone sub-models, plus `tests/test_dt_solver.py`.
Standalone and pure like every prior M2 phase — does not touch `core/model.py`, `config/builder.py`,
or the UI (that wiring, with transport lag, is explicitly M3).

**Key design insight, found by tracing the zones' own data dependencies (not assumed upfront):**
PHZ's solid solve depends only on `Q_indirect` and the top feed BC, never on vapor state (already
true of the standalone `phz.py`). So PHZ — and the PHZ/FTRZ boundary location — is solved ONCE, up
front, no outer-loop dependency. The only two coupling scalars across the remaining FTRZ/DCZ
interface are `DCZZoneResult.vapor_out` (top face) feeding FTRZ's `vapor_in`, and
`FTRZZoneResult.solid_out.T` (bottom cell) feeding DCZ's `T_L_sup`. This collapses BuildSpec Fig. 5's
"tray-by-tray fixed-point sweep" to a two-variable Gauss-Seidel loop spanning trays, not a full
multi-tray simultaneous solve.

**Free boundaries.** PHZ/FTRZ (`L_PHZ`, where `X2` crosses `X2,cr(T)`): located by marching real
trays top-down at full height, then `brentq` on sub-tray height within the one tray where the
crossing occurs — the same rigor `ftrz.py` already applies to its own `L_FTRZ`. FTRZ/DCZ (`L_FTRZ`):
already solved endogenously by `solve_ftrz_zone`'s own internal fixed point (unchanged); this module
only freezes it (from the first outer-loop FTRZ solve) to fix DCZ's own mesh geometry for the rest of
the Gauss-Seidel loop — re-meshing DCZ's `nz` cells every outer pass over a geometry that barely moves
(FTRZ is "order cm" against 0.3-1.0 m trays) is unneeded cost, documented as a simplification not an
oversight. **Superseded 2026-07-23:** extreme cases proved that the boundary
can move materially; the adaptive-solver decision below remeshes DCZ every
resolved outer pass.

**Per-tray duty apportionment (bed height as a genuine spatial domain, not one lumped quantity) —
resolves two gaps left open at M2 Phases 2-3.** Every zone occupying part of a tray's height now
draws the *same* uniform volumetric density `q_Iv = Q_indirect_w/(A_bed*tray_height)` as the rest of
that tray (eq. A.2a's own convention, applied consistently rather than inventing a second quantity):
- **`ftrz.py`: `Q_cond_w` → `q_Iv_w_m3`** (breaking, small signature change). Phase 2 left `Q_cond_w`
  as an absolute, externally-supplied wattage with "no formula, only a qualitative description" in
  the paper. Now it's literally the host tray's own uniform density — non-arbitrary, and slightly
  simplifies `solve_ftrz_zone`'s own inner loop (`q_Iv` is a true per-call constant, no more
  `Q_cond_w/(A_bed*L_FTRZ)` division).
- **`dcz.py`: `q_Iv_w_m3: float` → `float | tuple[float, ...]`** (backward-compatible: a scalar still
  broadcasts, existing tests unchanged). DCZ commonly spans several real trays with materially
  different `Q_indirect` (the reference scenario's MN1-remainder/MN2/SP1 span 4×10⁵-1.2×10⁶ W) — a
  single scalar was smearing them into one artificial average. `dt_solver.py` now builds a genuine
  per-cell profile by mapping each DCZ cell's z-position to whichever real tray contains it.

**`hQ`/`hM`/`aV` (bed transport coefficients) — confirmed literature gap, closed with a user-approved
standard-correlation placeholder.** Coletto's own Nuε-Reε correlation (eq. B.7) is cited to Faner's
unpublished 2008 PhD thesis, `aV` (eq. A.35) to Rhodes (2008), a textbook — M1's `DECISIONS.md` entry
already flagged both as open. Re-searched every PDF in `literature_sources/` (including the
supplementary material) specifically for a `Reε`/`aV` defining formula before closing this: genuinely
absent, not overlooked (confirmed via a dedicated Explore pass, not a skim). Closure, confirmed with
the user via `AskUserQuestion`: `aV = 3(1-εb)/rP` (packed spheres) and `Reε = ρV·uV,superficial·
(2rP)/(μV·εb)` (voidage-corrected superficial Reynolds number, standard packed-bed convention),
feeding the existing, unchanged `thermo.py` B.7-B.10 chain. Both `[DERIVED]`/`[PLACE]`, swappable if
Faner (2008)/Rhodes (2008) surface later. `kV` (bulk vapor thermal conductivity, needed for `PrV` and
absent from any existing config field) is reused from `ParticleConstants.k_pg` — same gas, particle
-pore scale instead of bed-interstitial scale, a documented cheap approximation.

**Follow-up same day: cross-checked, not upgraded.** The user located and added two further Faner
publications to `literature_sources/` (a 2019 *J. Food Process Eng.* journal article and a 2006
conference paper) — read both in full; neither is the actual 2008 thesis, neither contains eq. B.7's
0.6949/0.579 coefficients (the 2019 paper uses an unrelated Ranz-Marshall correlation instead), and
neither derives a Rhodes-type `aV`. Separately, an independent reconstruction (reasoning from Rhodes'
own superficial-vs-interstitial velocity convention, not a transcription of either source) converged
on the exact same two formulas already implemented (`Reε = ρ·us·dp/(μ·ε)`; `aV = 6(1-ε)/dp`,
algebraically identical to `3(1-ε)/rP`) — reassuring, but still only a second line of reasoning, not a
primary-source verification (its own stated confidence tops out at "moderate"). Left tagged
`[DERIVED]`/`[PLACE]`, not upgraded to `[PAPER]`; `sorption_C0`/`sorption_C1` remain fully
unrecoverable — neither new document mentions heat-of-sorption fitting at all.

**Sparge (direct steam) BC, scoped to the reference geometry.** SP1 is the bottommost DT tray, so
direct steam sets/augments DCZ's own bottom BC (`vapor_inf`) via a mass/energy mix against whatever
"clean" vapor arrives from below the DT — `solve_dt` raises if direct steam is configured on any tray
other than the last; a genuine point-source mid-domain is unimplemented future work, not silently
approximated.

**Real bug found and fixed during integration, not assumed away: `dcz.py`'s own Gauss-Seidel energy
balance does not actually converge — confirmed by direct instrumentation, this is more severe than
M2 Phase 3's own documented characterization.** Phase 3 dropped eq. A.34/A.37's `SVm2*Ĥ2`/`ṁ'ax,net*
Ĥ2` enthalpy-transport terms, writing that the zone "can run measurably cooler than its boundary
conditions" as an accepted, bounded trade-off. Feeding `dt_solver.py` real `Q_indirect` magnitudes
(not Phase 3's small illustrative ones) exposed that this is actually **unbounded** cooling — the
particle<->vapor system has no energy floor without those terms and drifts to arbitrarily low
temperature over enough outer iterations, eventually crashing the GAB isotherm's own validity range
(~305 K at these parameters) with a `ValueError`. Root-caused by instrumenting the coupling loop
directly (printing `vapor_T`/particle `Tp` per outer iteration), not by guessing.

**Fix: restore the missing term, but as a different (and correct) quantity than what Phase 3 tried
and reverted.** The first attempt (matching Coletto's own `SVm2*Ĥ2` literally, with `Ĥ2 ≈
dH_vap_hexane`, matching `particle.py`'s own existing convention for its oil-desorption term) was
tested here too and had **negligible effect** — confirmed by instrumentation, not assumed: a
particle's own diffusive relaxation timescale (`rP²/D_eff`, tens of minutes at typical DCZ
parameters) is far longer than one axial cell's residence time, so the *surface* mass flux `SVm2`
badly lags the true internal desorption rate happening throughout the particle's interior layers.
The working fix instead credits the vapor with the **particle-volume-integrated** sorption/desorption
sink — exactly what `march_particle_energy`'s own eq. A.30 source already subtracts from the particle
each step, credited back at the same cell with the opposite sign
(`particle.py::sorption_heat_sink_volumetric_mean_w_m3`, new). Being an EXACT transfer between two
already-computed energy balances (not an independently-derived absolute term), it cannot manufacture
the runaway-heating failure that sank Phase 3's own literal attempt. Verified by instrumentation to
converge (not diverge) across this task's tested parameter range.

**Honestly flagged, not resolved: this fix's own magnitude is a further open question.** Crediting
the FULL sink (including eq. A.31's isosteric excess term, not just the base latent heat) was chosen
over a latent-heat-only credit because the latter was tested and still diverges (under-credits). The
full credit converges everywhere tested, but at higher `hM` it settles to a noticeably elevated
absolute temperature (400+ K) rather than staying close to the zone's own boundary temperatures.
Plausibly an artifact of `hQ`/`hM`/`aV` still being `[PLACE]` (not fitted to real bed conditions)
compounding with this credit's own magnitude — but that's a plausibility argument, not a proof.
Exactly where between the two tested bounds the "correct" `Ĥ2` sits is open follow-up work.

**Consequence for `tests/test_dcz.py`:** the fix changes DCZ's own quantitative behavior even in
isolation (temperature no longer drifts artificially cold, so the GAB/oil isotherms' T-dependence
drives a smaller hexane reduction over the test's fixed iteration budget) — `test_hexane_content_
decreases_from_top_to_bottom`'s threshold lowered from 0.5 to 0.25 with the reasoning recorded
inline; this is a genuine physics change from a real fix, not a silently-loosened test.

**Validation (shape/order-of-magnitude vs. Coletto §3.2-3.4/Fig. 7-9, same standard as every prior
M2 phase — real data isn't available to us, only the plots):** with `scenarios/soybean_default.yaml`'s
own reference geometry and duties, the outer Gauss-Seidel loop converges (~90 iterations at
`outer_tol=1e-5`); hexane content falls monotonically and substantially from feed (~474,000 ppm) to
DT exit (~8,150 ppm, >98% reduction); FTRZ stays thin (`L_FTRZ` ~2.9 cm, "order cm" as the paper
reports); the PHZ/FTRZ boundary lands partway through PD2 rather than exactly at the PD3/MN1 tray
edge the paper's own worked example shows — plausible given this scenario's illustrative,
not-independently-calibrated per-tray `Q_indirect` split, not investigated further. Absolute
temperatures downstream of the boundary run measurably hotter (up to ~475 K) than real DT operation
(~380-390 K) — a direct, honestly-flagged consequence of `hQ`/`hM`/`aV` still being placeholders and
the DCZ energy-credit magnitude question above, not hidden behind a narrowed test tolerance.

## M2 Phase 3 — DCZ (Diffusion-Controlled Zone) dual-scale sub-model (2026-07-13)

**Scope.** BuildSpec §14 M2, third and last zone slice: `core/zones/particle.py` (12-layer spherical
FVM per particle) + `core/zones/dcz.py` (Rhodes-type bed integration + the real coupling algorithm),
plus `tests/test_particle.py`/`tests/test_dcz.py`, per Coletto (2022) Table A.3/A.4, §2.4, §7.6.
Standalone — does not touch `core/model.py` or the UI.

**User supplied the paper's supplementary material** (`literature_sources/
Caletto_Supplementary_Material.pdf`), which the main paper explicitly defers to for both the DCZ's
coupling algorithm and a parameters table. This resolved what would otherwise have been an
unavoidable reconstruction (an `AskUserQuestion` was in flight proposing three candidate coupling
architectures when the user provided the source instead) and corrected several M1-era `[PLACE]`
placeholders with real cited values.

**The real algorithm (Fig. 3 of the supplementary material) is a 4-step Gauss-Seidel sweep per outer
iteration** — energy-then-mass, particle-then-bed each — not the single combined particle march
originally proposed: (1) energy at particle scale (all cells, top→bottom), (2) energy at bed scale
(bottom→top), (3) mass at particle scale (all cells, top→bottom, using the just-updated
temperatures), (4) mass at bed scale (bottom→top). Convergence checked as `max` deviation in `wpg2`
and `Tp` across *all* cells and *all* particle layers.

**Real parameters recovered (supplementary Table 1), replacing prior gaps/placeholders:**
- Oil-in-hexane isotherm (eq. 7): `A0=0.9635`, `B=2.7036` (Cardarelli 1998) — the M1 entry below
  claimed these were unrecoverable and used an invented `[PLACE]` guess (`0.8`/`1.0`); they were
  real fitted values after all, just cited to a source we hadn't yet been given.
- `D_ax=1.022e-3 m²/s` (was `[PLACE] 1.0e-4`, a full order of magnitude off), `D_HW=1.33e-5 m²/s`
  (was `[PLACE] 1.0e-5`), `cp_solid=2317 J/(kg·K)` (was `[PLACE] 1800`), `cp_vapor=1926 J/(kg·K)`
  (was `[PLACE] 2000`), `mu_vapor=1.329e-5 Pa·s` (refined from `1.3e-5`).
- New thermal conductivities DCZ needs and nothing before it did: `k_ps=0.29`, `k_pg=0.02371`,
  `k_mixL=0.24 W/(m·K)` — all `[PAPER]`, not derived via a B.1-style mixing rule as first planned;
  the paper reports `k_mixL` as its own directly-estimated value.
- `sorption_C0`/`sorption_C1` remain `[PLACE]` — the supplementary table's own nomenclature lists
  them without stating values, still citing the same unavailable theses.

**Caught and fixed five real bugs during implementation, all via interactive sanity-checking before
locking in the test suite (the same "run it and look" discipline that caught PHZ's vapor-chaining
bug and FTRZ's entrance/exit convention bug):**
1. **Mass-march right-hand-side scaling bug**: `march_particle_mass`'s implicit linear system built
   `b[i] = wpg2_old[i]` instead of `V_i/dt * wpg2_old[i]` (the energy march had this right from the
   start) — caused a 10-order-of-magnitude blowup on the very first timestep, caught immediately by
   an interactive single-step check.
2. **Floating-point boundary drift**: the implicit solve can push `wpg2` a few ULPs past 1.0 (DCZ's
   own initial condition is exactly `wpg2=1.0`), which then rejected a valid isotherm evaluation
   downstream. Fixed by clamping to `[0,1]` after each mass step — a physical-domain safeguard, not
   a hidden behavior change.
3. **Heat-of-sorption singularity**: eq. A.31's power law (`W2^C1`, `C1<0`) is mathematically
   singular at exactly zero coverage, which the DCZ legitimately approaches. Floored `W2` away from
   the singularity (`max(W2, 1e-9)`) before evaluating it.
4. **Sorption-heat sign, empirically confirmed backwards**: eq. A.30 applied literally makes
   desorption (the dominant process throughout DCZ) a net heat *source* and adsorption a sink —
   backwards from basic sorption thermodynamics (adsorption is exothermic) and confirmed backwards
   by an isolated sanity check (near-equal boundary temperatures, no external heat, particle still
   ran away to hundreds of degrees). Implemented with the physically-consistent sign instead.
5. **Residence-time / outer-iteration conflation** (the most significant catch): an early draft let
   each axial cell's particle state persist across outer iterations and advance by one `dt` per
   *outer* pass — after `s` outer iterations, every cell had identical accumulated residence time
   regardless of axial position, instead of cell `j` reflecting `(j+1)*dt`. Fixed by re-marching a
   fresh cascade from the zone's own entry condition through all cells every outer iteration (the
   outer loop refines the frozen vapor profile each cell sees; it must not also stand in for
   residence time).

**Two further documented simplifications, found empirically, not assumed upfront:**
- **Bed-scale marching made implicit per cell**, not the naive explicit step eq. A.35 might suggest:
  the particle↔vapor transfer coefficient is typically stiff relative to a practical cell size (its
  own relaxation length can be far shorter than `dz`); an explicit step diverged by many orders of
  magnitude in testing. A backward-Euler-style per-cell relaxation is unconditionally stable —
  mirrors why the particle scale is already implicit.
- **Dropped eq. A.34/A.37's `SVm2·Ĥ2`/`ṁ'_ax,net·Ĥ2` enthalpy-transport terms** from the bed-scale
  energy source. Implemented literally, they produced runaway heating tens of degrees past *both*
  boundary temperatures with zero external heat input — traced to a magnitude mismatch against the
  particle-scale sorption sink eq. A.30 intends to counterbalance them with (confirmed by zeroing
  just this term: the same scenario then stays bounded, as physically expected). Kept only the
  direct convective heat-exchange term, the axial-conduction correction, and any external indirect
  heat. Consequence, stated plainly: this breaks strict energy conservation between the two scales
  (desorption's endothermic cost isn't fully credited back to the vapor), so the zone can run
  measurably cooler than both its own boundary conditions — an accepted, documented trade of
  rigor for stability given the alternative was unphysical divergence; tests assert bounded,
  directionally-correct behavior, not tight conservation.
- Also added **under-relaxation** to the vapor-profile updates (reusing `ModelParams.
  outer_relaxation`'s existing convention) — without it, the coupling was observed to drift for
  hundreds of iterations before settling rather than converging cleanly.

**Validation vs. Coletto §3.4/Fig. 9 (shape, not exact ppm — same reasoning as PHZ/FTRZ):** with a
~3-tray bed height and illustrative `hM`/`hQ`/`aV` (the same "explicit input, deferred to Phase 4"
treatment as FTRZ's `Q_cond`/`hQ`, since `Re_epsilon`'s correlation is still not available to us),
solid hexane content decreases monotonically and substantially top-to-bottom; the particle's own
radial hexane profile at zone exit is the paper's own "typical mass transfer profile" (high at the
center, near-zero at the surface); particle/vapor states stay within physical bounds throughout.

**Remaining known limitation, stated plainly:** the Gauss-Seidel coupling converges slowly with
these illustrative parameters (hundreds of outer iterations to tighten fully) even with
under-relaxation — tests use a loose tolerance and moderate iteration cap accordingly. Deriving
`hM`/`hQ`/`aV` from real bed conditions (M2 Phase 4) may well change this behavior; not chased
further here.

## M2 Phase 2 — FTRZ (Flashing and Temperature-Raising Zone) sub-model (2026-07-13)

**Scope.** BuildSpec §14 M2, second slice: `core/zones/ftrz.py` + `tests/test_ftrz.py`, pure and
unit-tested, per Coletto (2022) Table A.2 / §2.3/§2.3.1/§2.3.5 / §7.5. Standalone — does not touch
`core/model.py` or the UI; M2 Phase 4 (tray-by-tray fixed-point sweep) wires it in.

**Key simplification found while re-deriving the equations (not an assumption):** the solid-side
profile is entirely algebraic given the vapor-side solve — hexane content follows the
uniform-removal assumption (eq. A.6, same technique `phz.py` already uses), and solid temperature
follows directly from eq. A.17 (`T_L = w_h*T_boil_hexane + (1-w_h)*T_V`) using the Receding Front
wet-core fraction (eq. 3, whose cube cancels exactly against the mass-fraction relation). So only
ONE sequential vapor march is needed — mass + energy, tracking the V-SCAL (superheated) → V-SAT
(on the dew curve) transition against `core.thermo.dew_point_temperature` — rather than two coupled
solves.

**Free boundary (`L_FTRZ`), confirmed with user.** `q_Iv = Q_cond/(A_bed*L_FTRZ)` (eq. A.11) is
circular with `L_FTRZ = sum(dz_j)` (eq. A.21), which itself depends on `q_Iv`. Solved via the
fixed-point iteration the paper itself describes ("`L_FTRZ` is updated after each iteration"):
guess → march the vapor + compute cell thicknesses → recompute `L_FTRZ` → repeat. Converges in 2
iterations for the illustrative base case tried.

**Two inputs treated as explicit parameters rather than derived (confirmed with user), mirroring
the `phz.py` precedent:** `Q_cond_w` (paper gives no formula, only a qualitative axial-conduction
description) and `hQ` (needs the same missing `Re_epsilon` correlation flagged in M1). Deriving
both from bed conditions is deferred to M2 Phase 4.

**Documented gap-filling closure:** the paper gives explicit source-term closures for FTRZ's
hexane mass balance (eq. A.6-A.7) but not an explicit V-SCAL temperature-evolution formula. Each
cell's newly-evaporated hexane is mixed in at the wet core's own temperature (`T_boil_hexane`,
since that's what's actually evaporating) via `core.thermo`'s vapor-enthalpy machinery, plus the
cell's share of `q_Iv`; once the candidate state would fall on/below its own dew point, the cell
switches to V-SAT and solves for the condensed water mass from the full energy balance instead.

**Documented resolution (not a verified-exact transcription) for eq. A.18's cell-thickness
units:** `J_Q,cv = hQ*(T_V-T_L)` is unambiguously a flux (W/m²). For `J_Q,cs` (eq. A.19) to combine
with it without `dz` circularly appearing on both sides, it's taken as the cell's condensation
heat-release rate (`dH_vap_water * condensed_water_kg_s`, W, already known from the cell's energy
balance) divided by the bed cross-sectional area.

**Caught and fixed two bugs during implementation, both around the entrance-vs-exit convention for
a cell's reported solid state:**
- First draft used `X2_here = X2_inf + (k+1)*increment` for *both* the reported per-cell `X2` *and*
  the wet-core/`T_L` calculation. The zone-level test caught that this reports the *entrance* value
  as if it were the exit value (`solid_x2_values[0] == X2_sup` exactly — no reduction shown for the
  first reported cell), inconsistent with `phz.py`'s "cell holds the state after passing through
  it" convention.
- Naively changing the exponent to `k` (matching the reporting convention) broke the *physics*:
  the bottommost cell then exits exactly at `X2_inf` (the zone's asymptotic equilibrium), giving
  `w_h=0` and `T_L=T_V` exactly — zero driving force despite a finite hexane increment still being
  removed within that cell, causing a division-by-zero in `cell_thickness_m`.
- Resolved by using *two* values per cell: an entrance-basis `X2` (unchanged, `(k+1)*increment`)
  drives the physical `T_L`/`X2_cr`/`dz` calculations (avoids the zero-driving-force edge case at
  the terminal cell), while a separate exit-basis `X2` (`k*increment`) is what gets reported in
  `SolidState`, matching `phz.py`'s convention. Both discovered by the test suite, not caught by
  inspection alone — a genuine argument for the "write tests before declaring done" step in this
  project's workflow.

**Validation against Coletto §3.3/Fig. 8 (shape/order-of-magnitude, not exact curve values — only
the plot is available, same reasoning as Phase 1):** with illustrative inputs, hexane content drops
sharply top-to-bottom (0.096 → 0.023, approaching `X2_inf`), solid temperature rises monotonically
from ~93.5°C toward the vapor's ~100°C without ever exceeding it (eq. A.17's bound holds exactly),
and `L_FTRZ` converges to ~4.4 cm — same order of magnitude as the paper's own "<2 cm" at its base
case, given that `Q_cond`/`hQ` here are illustrative rather than bed-condition-derived (M2 Phase
4's job). Full existing `pytest tests/` suite (53 tests) stays green — purely additive.

## M2 Phase 1 — PHZ (Pre-Heating Zone) sub-model (2026-07-13)

**Scope.** BuildSpec §14 M2, first slice: `core/zones/phz.py` + `tests/test_phz.py`, pure and
unit-tested, per Coletto (2022) Table A.1 / eqs. A.1a-A.3a. Standalone — does not touch
`core/model.py` (still runs the M0 placeholder) or the UI; M2 Phase 4 (the tray-by-tray
fixed-point sweep) is what eventually wires this in.

**Per-tray marching solver**, `nz`-cell discretized (existing `nz_per_zone` config field):
sensible-heats the solid to `T_boil_hexane` per cell, then evaporates hexane isothermally once
there (eq. A.1a); vapor conserves water exactly and gains exactly the hexane the solid lost.
Validated with the paper's own Fig. 1 base case (70,000 kg/h wet meal, 58 C, 400 kW/tray): a
single pre-desolv tray does *not* reach `T_boil_hexane` on its own (340.7 K vs. 341.9 K needed),
matching the paper's own qualitative finding that evaporation only starts at the end of tray 2.

**Documented gap-filling closures (flagged in `core/zones/phz.py`'s module docstring, not
hidden):**
- The paper describes indirect heat as splitting between the solid and the passing vapor stream
  ("part... transferred to the ascending stream... part conducted to the solid") but never gives a
  quantitative split for PHZ specifically. This solver assumes 100% goes to the solid — the
  simpler, conservative reading, but it means the exact tray-by-tray timing of when evaporation
  starts may run faster than the paper's reported profile (its own hexane-reduction figures are
  10-25%; ad-hoc hand calculation with this scenario's duties gives ~40% over 3 trays) even though
  the *shape* (flat-then-declining X2, monotonically rising T) matches. Tests assert shape and
  conservation, not exact reduction percentage, for this reason.
- The paper gives explicit vapor energy-source closures for FTRZ (eq. A.10-11) and DCZ (eq. A.34)
  but not PHZ. Used a placeholder (`VAPOR_SOLID_CONTACT_FRACTION`, a simple fractional approach
  toward the solid's temperature) since it only affects the secondary vapor-side profile, not the
  solid-side profile BuildSpec's M2 acceptance criteria actually validate against.
- The "mixture" heat capacity §2.1 specifies (combining solid + interstitial pore vapor, eqs.
  B.1-B.6) is approximated by the solid-stream heat capacity alone (`core.thermo.cp_l`) — the
  interstitial vapor's mass contribution is tiny at typical bed voidage.

**Caught and fixed a bug in my own first draft**: `solve_phz_zone` originally chained the vapor
stream tray-to-tray in the *same* direction as the solid (top tray to bottom tray), but vapor
physically flows the opposite way (bottom to top) — a standalone PHZ solver can't resolve that
counter-current coupling on its own without also solving what's below it (FTRZ/DCZ), so each
tray's vapor inlet is now an explicit input per tray rather than incorrectly chained.

## M1 — thermo/sorption/VLLE property layer (2026-07-13)

**Scope.** BuildSpec §14 M1: `core/thermo.py` + `tests/test_thermo.py`, pure and unit-tested, no
solver. Implements Coletto (2022) Appendix A sorption/critical-hexane equations (3-7, A.31) and
Appendix B thermophysical correlations (B.1-B.12) exactly as published. Purely additive —
`core/model.py` and the UI are untouched; M2 (the zone sub-models that actually consume this layer)
is separate future work.

**Source verification.** Read the full Coletto (2022) PDF directly (rendered every page to an
image and read the actual equations/tables — text extraction garbles subscripts/Greek symbols too
badly to trust for this). Cross-checked `BuildSpec` §7/§8/§14 against it equation-by-equation:
accurate, no corrections needed. Also read both Cardarelli papers already sitting unread in
`literature_sources/` (`Hexane_Sorption_in_Oilseed_Meals` 1996, `Modeling_and_Simulation_of_
Oilseed_Meal_Desolventizing_Process` 2002) for the real numeric parameters Coletto's equations
depend on.

**Real parameters recovered, replacing `[PLACE]` guesses (`properties/soybean.yaml`):**
- GAB isotherm for soybean, exact (Cardarelli & Crapiste 1996, Table 2): `Xm=5.183e-3` kg/kg,
  `C0=3.117e-3`, `dHC_R=2262 K`, `K0=9.172e-2`, `dHK_R=729.6 K`. **This also caught a real schema
  bug**: `GabParams` had a `temp_dependence: "linear"` field, but the actual correlation is
  exponential/van't Hoff in `C`/`K` (`Xm` itself is T-independent) — restructured the field set to
  match instead of patching the wrong functional form with real numbers.
- Several other physical properties, exact (Cardarelli, Crapiste & Mattea 2002, Table 1):
  `rho_solid`/`rho_ps` 1513 kg/m³ (was 1250), bed void fraction 0.4 (was 0.45), particle porosity
  0.5 (was 0.40, with `alpha_ps`/`alpha_pg` re-derived to match), particle radius 1.0e-3 m (was
  1.5e-3), `D_eff` 4.0e-10 m²/s (was 1.0e-9, now the exact cited value in scenario YAML), and a new
  `mu_vapor` field (1.3e-5 Pa·s) we didn't carry before.
- Added `cp_water_vapor`/`cp_hexane_vapor`/`cp_hexane_liquid` (standard handbook `[STD]` values) —
  eqs. B.5/B.6 need per-component vapor/liquid heat capacities the old single `cp_vapor` placeholder
  couldn't supply.

**Genuinely unrecoverable (DECIDE, confirmed with user): oil-sorption power-law (`A0`, `B`) and
heat-of-sorption constants (`sorption_C0`, `sorption_C1`).** Neither Cardarelli paper publishes
fitted values for these — Coletto (2022) cites both to unpublished PhD theses (Cardarelli 1998;
Faner 2008) not in `literature_sources/`. Tried `WebFetch` on the paper's DOI for supplementary
material; paywalled (ScienceDirect), unreachable. Decision: implement the exact functional forms
now with clearly-tagged `[PLACE]` order-of-magnitude constants (not fit to data), swap in real
numbers later if either thesis surfaces.

**Follow-up (M2 Phase 3, 2026-07-13): the oil-sorption power-law WAS recoverable after all.** The
user supplied Coletto et al.'s supplementary material (not paywalled content — the paper's own
companion document), whose Table 1 states `A0=0.9635`, `B=2.7036` (Cardarelli 1998) directly.
Updated `properties/soybean.yaml` and this module's own reference values accordingly; `sorption_C0`/
`sorption_C1` remain genuinely unrecoverable even from that source (listed in its nomenclature
without stated values).

**VLLE dew-point curve (`Hvbw = f(YV2,TV)` → `f(YV2)`, §A.2.3) — the one piece requiring genuine
numerical design, not transcription (confirmed with user, corrected once mid-implementation):**
- Dew point solves `y_water*P = P_water,sat(T_dew)` (single-condensable-component Raoult's law,
  water condensing with hexane as an inert carrier) via `scipy.optimize.brentq` on the existing
  `antoine_water` correlation. **Not** the two-liquid azeotrope co-boiling formula
  (`P=P_hex,sat+P_water,sat`) originally proposed — corrected after re-reading §2.3.4, which
  restricts the FTRZ to "hexane content below the azeotropic value," where only water actually
  condenses. Matches the paper's own Fig. 8(b): reported FTRZ dew temperature dips below 100 °C as
  hexane dilutes the vapor and suppresses water's partial pressure — exactly what the corrected
  formula predicts and the original one wouldn't.
- Enthalpy datum (liquid water at its own bp, liquid hexane at its own bp as zero-enthalpy
  references) is a documented construction filling a gap the paper leaves implicit, not a value
  stated in the paper — flagged as such in `core/thermo.py`'s docstring for future review.

**Still open (not needed until M2):** the paper's main text never gives an explicit correlation
for `aV` (specific interfacial area) or `Re_epsilon` (Reynolds number in the Faner correlation) —
likely in the same paywalled supplementary material. `core/thermo.py`'s B.7-B.10 functions take
`Re`/`aV`-derived quantities as plain arguments, so they're usable now; computing `Re_epsilon`/`aV`
from bed conditions is deferred to M2, with a standard packed-bed correlation (e.g. `aV=3(1-eps)/rP`
for spheres) as the fallback if the supplementary material stays unreachable.

## DT equilibrium replaced with a mechanistic energy balance (2026-07-13)

**Problem.** Even the sparge tray (SP1, heaviest duty: 400 kW indirect + 1.5 kg/s direct steam)
was reading only ~68.7 °C at steady state — exactly `T_boil_hexane`. The `_stage_equilibrium`
DT-branch capped every DT stage's temperature at hexane's boiling point via
`T_eq = T_in + (T_boil_hexane - T_in)*sat_frac`, with `sat_frac` a curve-fit saturation heuristic
driven by an arbitrary `nominal_duty_w` scale — no amount of steam duty could ever push a stage
past hexane's bp, which is wrong once a tray is mostly desolventized.

**First attempt (reverted).** Added a hand-picked `T_toast_ceiling` (110 °C) that the thermal
ceiling blended toward once local hexane content dropped below a threshold. Correctly flagged by
the user as still an arbitrary constant, not physics — reverted in the same session.

**Fix: mechanistic sequential flash / sensible-heat energy balance, no fitted curve or ceiling
anywhere.** `_stage_equilibrium`'s DT-branch now computes `q_specific = Q_total/m_dry` (J per kg
dry solid processed, `Q_total` = indirect duty + direct steam's condensation latent heat) and
spends it in physical order: (1) sensibly preheat the meal to `T_boil_hexane` if not already there
(`cp_solid`/`cp_water_liquid`/`cp_oil`-weighted heat capacity), (2) evaporate residual hexane
isothermally at `T_boil_hexane` (`dH_vap_hexane`) — a pot doesn't exceed its liquid's boiling point
while that liquid is still boiling — (3) once hexane is fully stripped, sensibly heat further
toward `T_boil_water`, (4) evaporate moisture isothermally at `T_boil_water` (`dH_vap_water`), (5)
sensibly heat past that (the toasting regime) if duty still remains. `T_boil_water` itself is
solved from `antoine_water`'s Antoine coefficients at 1 atm (`config/builder.py:
_antoine_boiling_point_k`) rather than hardcoded — reuses physical properties already in
`properties/soybean.yaml` that were sitting unused. Direct steam's condensate mass is now an exact
mass balance (`q_dir_mass/m_dry`) rather than a fitted "0.03 gain" factor. Removed `nominal_duty_w`
and the `exp(-3.0*sat_frac)` hexane-decay heuristic entirely — no longer needed. `ModelConstants`
gained `T_boil_water`, `cp_water_liquid`, `cp_oil`, `oil_fraction` (all pre-existing
`PhysicalParams` fields, just not threaded through before). Verified: SP1 now settles at ~100 °C
(water's bp) once desolventized, matching the ~105-110 °C literature DT-exit figure, instead of
being stuck at 68.7 °C — see `app_specifications/DTDC_steady_state_reference.md`.

## Steady-state realism + dashboard overhaul (2026-07-12)

**Real bed-holdup/level state (`core/model.py`).** `gate_opening` — the MV §5.2 defines as setting
"inter-stage solid flow / holdup (level)" — had no effect anywhere in the M0 placeholder model; it
was read into `Inputs` and never used. Added a genuine solids-mass holdup per stage (`State.M`,
`Outputs.stage_level_pct`), using the same closed-form relaxation style already used for T/X1/X2
(`M_new = M_eq + (M_old - M_eq)*exp(-dt/tau)`, `M_eq = inflow * tau`, `outflow = M_new / tau`,
chained stage-to-stage like the existing T/X1/X2 chain). `_stage_tau` now also normalizes against
`gate_opening` (mirroring the existing `rpm/3.0` pattern, normalized so the scenario's default
50%/3rpm reproduces today's tau exactly — no silent behavior change at defaults). Level is
deliberately not clamped to 100% so an over-restricted gate can show a real flood condition. This
exposed that `DR1`/`CL1`'s placeholder geometry (3.0 m dia, 0.5 m bed) was undersized once holdup
was modeled — permanently flooded at default settings — so their geometry was bumped to 4.0 m /
0.6 m (settles ~65% at default gate/sweep, still floods if the gate is narrowed hard).

**`base_residence_s` exposed as a `ModelParams` field (`config/schema.py`, `config/builder.py`).**
Previously a hardcoded `Model` dataclass default in `core/model.py`, buried and uncited. Same
value, now visible/documented in `scenarios/soybean_default.yaml` with the literature comparison in
the new `app_specifications/DTDC_steady_state_reference.md`. (`nominal_duty_w`, exposed alongside
it at the time, was removed the next day — see the entry above: the DT equilibrium it drove was
replaced with a mechanistic energy balance that doesn't need it.)

**"Hexane stuck high, takes forever to drop" — diagnosis.** Working the equilibrium chain by hand
at this scenario's steam duties gives a converged tower-exit residual of ~15-17 ppm — already well
under the literature spec (<300-500 ppm) — so the steady state was correct. The problem was
`sim.speed_factor = 1.0` (real time) against a `base_residence_s`-driven settling time of ~20-40
simulated minutes (in line with a real DT's ~20-30 min residence): correct physics, impractical to
watch. Default `speed_factor` raised to `20.0` (checked against the undersample constraint:
`20*0.2=4.0 <= 10.0`).

**`feed_temperature` promoted to a DV.** This file's M0 entry (below) already flagged this exact
path ("if a future milestone needs it live-adjustable, promote it to a DV"). Moved from
`OperatingDefaults` to `DisturbanceDefaults` (schema + scenario YAML); `RuntimeFacade` drops its
private `_feed_temperature` attribute in favor of a `DisturbanceVariable` like `feed_moisture`/
`feed_hexane`. Falls out of the OPC UA DV folder for free (it already loops generically over
`snap.dvs`).

**Literature-grounded defaults (DECIDE).** `disturbance_defaults.feed_moisture` lowered
0.12→0.07 to match wet-flake literature (~5-10%, was reading high at 12%); `feed_hexane` (0.26)
and `feed_temperature` (330 K) were already in range, left as-is. Sources and the full
feed/PREDESOLV/MAIN/SPARGE/DT-exit/DRYER/COOLER profile are in
`app_specifications/DTDC_steady_state_reference.md`.

**UI (`interfaces/ui/app.py`): °C, feed sliders, 2-column tower, Siemens theme (DECIDE).** All
internal plumbing stays Kelvin/SI; conversion to °C happens only at UI display (stage/feed/product
cards, both profile charts, the time-history trend, the DRYER/COOLER air readouts, and — via a new
per-MV-key unit map — the generic MV table and manual-setpoint control for `heated_air_temp`/
`ambient_air_temp`). Added three feed-condition sliders (hexane 10-50%, moisture 5-25%, feed temp
40-80°C) wired directly to `facade.set_dv` (DVs have no MANUAL/AUTO mode per §5.2). Tower split
into two columns — DT (PREDESOLV/MAIN/SPARGE) and DC (DRYER/COOLER) — mirroring the physical
vessel split, with denser single-line-per-tray cards and a live bed-level bar (red + "FLOOD" badge
above 100%) per tray; the tower area is capped at a fixed height with its own scrollbar so the rest
of the dashboard (controls/KPIs/charts) never needs the page itself to scroll for it. Applied a
Siemens-petrol/graphite flat-card theme (`ui.colors`, injected CSS, a fixed dark header bar) — a
Siemens-inspired look, not literal trademarked assets; light theme only, no dark-mode toggle.

## M0 — Walking skeleton (2026-07-10)

**Milestone scope.** Implemented BuildSpec §14 M0: config models + validators,
`RuntimeFacade`, a placeholder `Model.step()`, both clocks, a threaded tick
loop, a full-address-space OPC UA server, and a NiceGUI setup/dashboard UI.
No Coletto (2022) zonal physics yet (M1/M2) — see "Placeholder physics" below.

### UI framework (§10, DECIDE)
Chose **NiceGUI** over Dash. Reasoning given to and confirmed by the user:
pure-Python reactive components, direct in-process calls into
`RuntimeFacade` (no separate callback-graph server), and `app.on_startup`
lets the OPC UA server run as an asyncio task inside NiceGUI's own event
loop — the whole process needs only one asyncio loop plus the tick-loop
worker thread (§8.3), rather than juggling two.

### Runtime port / endpoint (§9, DECIDE)
`opc.tcp://0.0.0.0:4840/dtdc/`, namespace `http://dtdc.sim/`, as suggested
in the spec text. NiceGUI dashboard defaults to `127.0.0.1:8080` (CLI flags
`--host`/`--port`/`--opcua-endpoint`/`--no-opcua` on `dtdc-sim`).

### Tick cadence (§8.1, DECIDE)
`dt_wall_s = 0.2` s (from the example scenario file, kept as-is). Idle-state
poll interval (not in spec, an M0 implementation detail) is 50 ms.

### Placeholder physics (§7 vs §14 M0)
`core/model.py` models each DT/DC stage as a first-order-lag holdup
(`x_new = x_eq + (x_old - x_eq)*exp(-dt/tau)`) chained top-to-bottom, with
`tau` set by sweep-arm speed (DT) or a fixed constant (DC), and per-stage
equilibrium targets driven by steam duty / air conditions. This is
deliberately **not** the Coletto dual-scale zonal model (PHZ/FTRZ/DCZ,
receding front, 12-layer particle FVM) — that is M1/M2 work. The exponential
form was chosen (over explicit Euler) so the placeholder stays numerically
stable at any `dt`, including the large `dt` produced by high `speed_factor`
in `RealTimeClock` — important since M0 already has to survive the full
speed-factor/undersampling machinery in §8.4.

The quality kinetics (§7.11) **were** implemented from the spec's exact
Arrhenius parameters (TIA as a biexponential blend, protein denaturation as
a single first-order decay per tick, `S_prot(t+dt) = S_prot(t)*exp(-k_den(T)*dt)`,
rather than a literal elapsed-time-integrated form, since the elapsed-time
formulation assumes a batch/plug-flow frame that doesn't map cleanly onto
per-tick recurrence) — both were later removed entirely (TIA on 2026-07-17,
protein denaturation on 2026-07-18) — see the dated entries above.

`feed_temperature` (an `OperatingDefaults` field) is treated as a fixed
boundary value captured at `assemble()` time, not a runtime-adjustable
MV/DV — the BuildSpec §5.2 MV/DV tables do not list it as either. If a
future milestone needs it live-adjustable, promote it to a DV.

### MV limits (§5.2, DECIDE — no numeric limits given)
First-cut `(min, max)` ranges in `engine/facade.py::MV_LIMITS`, sized off
the magnitudes in `Specifications/DTDC_default_parameters.yaml`
(`operating_defaults`). All `rate_limit`s are `None` (unlimited slew) for
M0. These are placeholders for real plant limits and should be revisited
once real equipment data is available.

### MV default mode at startup (§6, DECIDE)
Every MV starts in `MANUAL`, seeded from `operating_defaults` (so
`manual_setpoint == auto_setpoint` at t=0 — no bump if an APC connects and
immediately gets switched to AUTO). Rationale: the plant should not be
"driven" by an unconnected APC's stale `auto_setpoint` by default.

### OPC UA write propagation (§9.2, DECIDE)
`asyncua`'s `Node.set_writable()` only gates the *client* write permission;
there is no built-in low-latency "on write" server hook without subclassing
its `AttributeService`. M0 uses a **poll-then-push** refresh cycle
(`REFRESH_S = 0.2` s in `interfaces/opcua/server.py`): read all writable
nodes into the facade, then write the facade's snapshot back to all nodes.
Push-after-pull means a UI-side change and an OPC-UA-client-side change
both converge within one refresh cycle. Acceptable for a soft-real-time
sandbox; a future milestone could add a custom `AttributeService`/`SubHandler`
for immediate reaction if APC latency requirements tighten.

### Logging format (§1.2, DECIDE — "optional run logs")
Not implemented in M0 beyond Python's standard `logging` to stdout
(`logging.basicConfig(level=logging.INFO)` in `main.py`). No file-based run
log yet; add one if/when a milestone needs replay/audit trails.

### Python/dependency versions (§12 wheel caveat)
Verified via `pip install --dry-run` before scaffolding: `numpy` 2.5.1,
`scipy` 1.18.0, and `pydantic-core` 2.46.4 all ship `cp314-win_amd64`
wheels. No fallback to 3.13 was needed.

## Post-M0 polish (2026-07-10)

**Material properties moved to `properties/<material>.yaml`.** §11 states
the intended file layout: "one file per material property set under
properties/; one scenario file binding a property set + model params +
geometry + operating/disturbance defaults." M0 had shipped with `physical:`
embedded directly in `scenarios/soybean_default.yaml` (copied wholesale from
`Specifications/DTDC_default_parameters.yaml`) — functionally fine but not
matching that layout, and it left `properties/` sitting empty. Split the
`physical:` block out into `properties/soybean.yaml`; `config/loader.py`'s
`load_scenario()` now resolves `properties/<material>.yaml` automatically
whenever a scenario omits an inline `physical:` block (an inline block still
overrides the lookup, for one-off scenario tweaks or tests, without needing
a new properties file). `ScenarioConfig` itself is unchanged — only the
loader gained this resolution step — so `config/builder.py` and everything
downstream needed no changes.

**Formatting/lint targets bumped to cp314.** `pyproject.toml`'s
`[tool.ruff] target-version` was left at the scaffold default `"py313"`;
bumped to `"py314"` to match `requires-python = ">=3.14"`, and added an
explicit `[tool.black]` section (also `py314`) since `black` was a declared
dev dependency but had no config and was never actually run over the
codebase during M0. Running it now reformatted 8 files to its 100-column
style; behavior is unchanged (confirmed by the full test suite + `ruff
check` staying green after).

**Added `.gitignore`.** The project has no VCS yet, but `.venv/`,
`__pycache__/`, `*.egg-info/`, and the `pytest`/`ruff` cache dirs should
never be committed once one is initialized.

## Configurable transfer hardware and PLC control boundary (2026-07-23)

The former `gate_opening/<stage>` registry incorrectly implied that every
thermodynamic tray had a separate PLC-operated discharge gate. Equipment
drawings and the literature instead support stationary heated PD trays swept
by a common shaft, vapor bypass around the PD beds, controlled discharge
devices on selected lower deep beds, and rotary airlocks at vapor/air
boundaries.

The scenario now declares vapor routing on each stage and declares one
independent `topology.solid_transfers[]` boundary below every stage. PD1-PD3
use passive swept ports; MN1/MN2 use controlled gates; SP1→DR1 and the final
product outlet use controlled rotary vapor seals. The numerical discharge
closure is unchanged at the calibrated seed: every migrated active or passive
conductance is 50% with capacity factor 1.0. This is deliberately an
interface/topology refactor, not an unannounced recalibration.

The PLC-facing adapter exposes scenario-derived SISO loop tags under OPC UA as
`Control/<tag>/{SP,PV,OP,Mode,Units,Status}`. Predesolv and main/toast
indirect-steam totals use fixed seed-derived allocation weights; the one
central shaft is one common speed loop. Raw per-stage MVs remain read-only
under `Diagnostics/InternalMV`, while disturbances live under
`SimulationInputs`. Controlled transfer devices are currently position loops
(`ZIC_*`), not falsely labelled level loops: a real `LIC` requires either an
identified internal controller law or an external PLC writing OP.

## Adaptive nested DT solver and moving FTRZ/DCZ boundary (2026-07-23)

The physical and constitutive equations are unchanged. The refactor is
strictly numerical and corrects four solver-contract defects found at high
throughput/high bed level:

1. DCZ convergence now includes its top vapor temperature, top component
   flows, bottom meal state, and maximum full vapor-profile changes. A
   cap-limited inner solve is explicitly `converged=False`.
2. DCZ carries a complete warm state (particle temperature/hexane fields,
   vapor temperature/component flows, moisture, condensation/latent profiles,
   lagged rates, adaptive damping, and water active set). Resuming a solve
   therefore continues the fixed-point iteration without adding fictitious
   particle residence time.
3. Every resolved outer pass rebuilds DCZ from the current endogenous FTRZ
   length and interpolates the warm state onto the moving mesh. The former
   first-pass frozen geometry is removed.
4. Temperature, hexane, water, and the outer coupling use separate,
   residual-responsive damping. Cap-limited DCZ blocks are completed before
   the outer variables move, so one reported outer iteration is one fully
   resolved map evaluation. This is pseudo-transient continuation for large
   PLC/setpoint changes, not a change to the steady equations.

The result-publication gate now rejects a nonconverged DCZ or inconsistent
moving-boundary geometry. The configured real-time outer cap is raised from
20 to 150 because the honest high-feed case needs 117 resolved evaluations.
The former failing benchmark (40.4 kg/s dry feed, 81% DT levels, 2.07 kg/s
equivalent predesolv jacket steam) now converges with a bounded meal profile
(about 49.5--111.7 degC), about 22.7% dry-basis exit moisture, and about
837 ppm dry-basis residual hexane. These are solver-regression values, not a
new calibration target.
