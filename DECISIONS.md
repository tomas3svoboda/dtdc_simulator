# DECISIONS.md

Log of `DECIDE` choices made while building the DTDC simulator, per
`Specifications/DTDC_Simulator_BuildSpec.md`. Newest entries at the top.

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

The quality kinetics (§7.11: TIA biexponential blend, protein denaturation)
**are** implemented from the spec's exact Arrhenius parameters, but each is
simplified to a single first-order decay per tick
(`C_TIA(t+dt) = C_TIA(t)*exp(-k_eff(T)*dt)`, `k_eff = A*k1 + (1-A)*k2`)
rather than the literal biexponential-in-elapsed-time form, since the
elapsed-time formulation assumes a batch/plug-flow frame that doesn't map
cleanly onto per-tick recurrence. Revisit if TIA validation against
literature curves requires the exact biexponential shape.

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
