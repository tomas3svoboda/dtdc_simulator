# DTDC Real-Time Simulator — Build Specification

> **Audience:** an autonomous coding agent building this application from scratch.
> **Nature of document:** authoritative, machine-readable specification. Where a value or
> decision is marked `DECIDE`, the agent may choose a reasonable option and record it in
> `DECISIONS.md`. Everything else is normative.
> **Language / runtime:** Python 3.14 (target 3.14.5). See §12 for wheel/compat notes.

---

## 0. One-paragraph summary

Build a **soft real-time dynamic simulator of a Desolventizer–Toaster–Dryer–Cooler (DTDC)**
that behaves like a live plant surrogate. It runs a continuous simulation loop, paced to
wall-clock time with an adjustable speed factor, and exposes the process to an external
Advanced Process Controller (APC) over an **OPC UA server (security disabled)**. Controllable
operating parameters can be driven either from the application UI (**MANUAL** mode) or by the
APC over OPC UA (**AUTO** mode), switchable per parameter with bumpless transfer. Physical
properties and model constants are configured and frozen in a **setup phase** before the
runtime loop begins.

---

## 1. Goals and non-goals

### 1.1 Goals
- A **high-fidelity DTDC model** — the Coletto (2022) dual-scale zonal DT (PHZ/FTRZ/DCZ with
  Receding Front + 12-layer particle diffusion) wrapped for real time (§7), plus air-contacting
  DC stages — presented to the controller as a quasi-steady map with explicit transport lag.
- A **real-time engine** advancing simulation time = wall time × `speed_factor`, with a
  deterministic free-running mode for reproducible regression tests.
- An **OPC UA server** mirroring a plant DCS: writable manipulated variables (MV), writable
  disturbances (DV), read-only process values (PV), and simulation-control nodes.
- **MANUAL/AUTO arbitration** per controllable parameter.
- A **setup phase** that validates constants, normalizes units, assembles an immutable model,
  and computes a steady-state initial condition.
- A **UI** for (a) entering/loading cold configuration before a run and (b) monitoring PVs and
  tuning MVs during a run.
- Clean **ports-and-adapters** separation: a pure numerical core with no I/O; OPC UA and UI are
  symmetric adapters over one runtime facade.

### 1.2 Non-goals
- No CFD, no 3-D geometry. Stages are lumped/1-D.
- No authentication/encryption on OPC UA (explicitly disabled; sandbox/edge use only).
- No persistence layer beyond config files and optional run logs (`DECIDE` on logging format).
- The APC itself is **out of scope** — this app is the plant it controls.

---

## 2. Domain background (enough to name variables correctly)

The DTDC removes hexane solvent from oilseed meal after extraction, deactivates anti-nutritional
factors by toasting, then dries and cools the meal. Meal (solid) descends through a stack of
**trays/stages**; vapor (steam + hexane) ascends counter-current in the DT section; heated/ambient
**air** contacts the meal in the DC section.

**Stage roles** (a representative conventional layout — geometry is configurable):
- Pre-desolventizing trays, main trays, sparge tray (direct steam injection) — the **DT**.
- A rotary lock separates DT from **DC**.
- Drying stage(s) (heated air), cooling stage(s) (ambient air) — the **DC**.

**Streams per stage:** descending solid (meal) carrying liquid hexane, water, oil; ascending
vapor/air carrying hexane and water vapor. Indirect (jacket) steam heats trays; direct (sparge)
steam is injected into the meal.

**KPIs to expose as PVs:**
- Residual hexane in final meal `[mg/kg]` (target: ≤ ~10 human / ~1000 animal).
- Meal moisture `[% w/w]` (target: < ~12 %).
- Steam consumption `[kg/t]`, throughput `[t/day]`.

**Reference models** (for validation targets and the fidelity ladder — do not copy text, use as
mathematical/behavioral references):
- Cardarelli, D. A., & Crapiste, G. H. (1996). *Hexane sorption in oilseed meals.* JAOCS 73(12).
- Cardarelli, Crapiste, Mattea (2002). *Modeling and simulation of an oilseed meal desolventizing process.* J. Food Eng. 52.
- Paraíso et al. (2008); Andrade et al. (2008). *Soybean oil meal desolventizing–toasting.* J. Food Eng. 86/87.
- Cauneto et al. (2017). *Modeling, simulation, and analysis of a soybean meal desolventizing equipment.* JFPE 42(2).
- Coletto, Bandoni, Blanco (2022). *A comprehensive mathematical model of an industrial Desolventizer-Toaster.* J. Food Eng. 318, 110870. **(state-of-the-art zonal model — the top of the fidelity ladder)**

---

## 3. Architecture (ports and adapters)

```
+------------------------------------------------------------------+
|  interfaces / adapters                                           |
|    - ui/            (parameter entry + runtime dashboard)        |
|    - opcua/         (asyncua server; MV/DV/PV/sim-control nodes) |
|  Both talk ONLY to the RuntimeFacade. Neither imports core/.     |
+------------------------------------------------------------------+
                          |  RuntimeFacade (thread-safe)
                          v
+------------------------------------------------------------------+
|  engine/                                                         |
|    - facade.py      (snapshot, set_setpoint, set_mode, commands) |
|    - loop.py        (tick loop: read u -> integrate -> publish)  |
|    - clock.py       (RealTimeClock | FreeRunClock — injectable)  |
|    - state_machine  (CONFIGURED/READY/RUNNING/PAUSED/STOPPED)    |
+------------------------------------------------------------------+
                          |  consumes an immutable Model + x0
                          v
+------------------------------------------------------------------+
|  core/  (PURE: numbers in, numbers out; no I/O, no asyncua)      |
|    - model.py       (Model: init_state, step, outputs)          |
|    - dt_solver.py   (integrated DT fixed-point sweep, §7.8)     |
|    - zones/         (phz.py, ftrz.py, dcz.py — §7.3/7.5/7.6)    |
|    - particle.py    (12-layer spherical FVM, §7.6)              |
|    - bed.py         (Rhodes-type axial bed integration, §7.6)   |
|    - receding_front (rfr, X2_cr, X2_eq — §7.4/7.5)              |
|    - thermo/        (VLLE, dew point, Hvbw curve, Antoine)      |
|    - sorption/      (GAB isotherm, oil power-law, heat of sorpt)|
|    - transport/     (Faner Nu, Chilton-Colburn hM, D_eff, D_ax) |
|    - properties/    (B.1-B.12 correlations + material sets)     |
|    - dc.py          (dryer/cooler air-contacting stages, §7.10) |
|    - initializer.py (steady DT solve at defaults -> x0, §7.8)   |
+------------------------------------------------------------------+
                          ^
                          |  built by
+------------------------------------------------------------------+
|  config/   (Pydantic v2)                                         |
|    - PhysicalParams, ModelParams, Geometry  (COLD, frozen)      |
|    - OperatingDefaults, DisturbanceDefaults (seed HOT state)    |
|    - builder.assemble_model(config) -> (Model, x0)             |
+------------------------------------------------------------------+
```

**Invariant:** `core/` must never `import asyncua`, never do file/network I/O, and must be fully
unit-testable with plain arrays. All wall-clock/threading/OPC concerns live in `engine/` and
`interfaces/`.

---

## 4. Lifecycle and state machine

Two phases separated by an immutable handoff.

**Setup phase (cold, runs once before the loop):**
1. Select/load constants: `PhysicalParams`, `ModelParams`, `Geometry`, plus a named material
   property set (soybean / sunflower / rapeseed).
2. Validate (fail fast) and normalize all units to SI.
3. `assemble_model(config)` → binds constants into closures, returns an **immutable** `Model`.
4. Compute steady-state `x0` via `initializer` at the operating defaults.

**Runtime phase (hot, the loop; only `x` and inputs `u` change).**

### 4.1 States and transitions
```
UNCONFIGURED --load/enter config--> CONFIGURED
CONFIGURED   --assemble+validate--> READY        (holds immutable Model + x0)
READY        --run-------------->   RUNNING
RUNNING      --pause------------>   PAUSED
PAUSED       --run-------------->   RUNNING
RUNNING/PAUSED --stop---------->    STOPPED
STOPPED      --reset----------->    READY         (restore x0, keep Model)
STOPPED      --reconfigure----->    CONFIGURED     (rebuild allowed; constants editable)
```
- Cold constants are **writable only in** `UNCONFIGURED`/`CONFIGURED`. In all other states they
  are exposed **read-only** (for provenance).
- `reconfigure` is the ONLY legal way to change a physical constant during a session; it stops the
  loop, rebuilds the model, and re-initializes. It must be logged.

---

## 5. Parameter model

### 5.1 Cold configuration (frozen into the Model)

`PhysicalParams` (SI):
| field | unit | notes |
|---|---|---|
| `dH_vap_hexane` | J/kg | latent heat of hexane |
| `dH_vap_water` | J/kg | latent heat of water |
| `T_boil_hexane` | K | at operating pressure |
| `rho_solid` | kg/m³ | dry solid density |
| `rho_vapor_ref` | kg/m³ | reference vapor density |
| `cp_solid` | J/(kg·K) | dry meal heat capacity |
| `cp_vapor` | J/(kg·K) | |
| `cp_water_liquid` | J/(kg·K) | |
| `cp_oil` | J/(kg·K) | |
| `bed_porosity` (ε_b) | – | 0<ε_b<1 |
| `particle_porosity` (ε_p) | – | 0<ε_p<1 |
| `oil_fraction` | kg/kg | oil per dry solid (X3) |
| `rho_ps` | kg/m³ | particle solid-phase density |
| `rho_hexane_liquid_ref` | kg/m³ | or via Daubert–Danner B.11–B.12 |
| `alpha_ps`, `alpha_pg` | – | particle solid / pore-gas volume fractions |
| `particle_radius` (rP) | m | sphere radius for particle FVM |
| `sorption_C0`, `sorption_C1` | – | heat-of-sorption law ΔĤ_s = ΔĤ_lv2 + C0·W2^C1 (A.31, material-specific) |
| `gab_params` | – | GAB isotherm coeffs + T-dependence (Cardarelli & Crapiste 1996), per material |
| `oil_isotherm` {A0, B} | – | hexane-in-oil power law qo = A0·ah^B (7), oil basis |
| `antoine_hexane` {A,B,C} | – | P_sat / VLLE support |
| `antoine_water` {A,B,C} | – | P_sat / VLLE support |
| `material_name` | – | soybean/sunflower/rapeseed (selects the set) |

`ModelParams`:
| field | unit | notes |
|---|---|---|
| `D_eff` | m²/s | effective intraparticle hexane diffusivity (DCZ particle scale) |
| `D_ax` | m²/s | axial dispersion coefficient (bed scale) |
| `n_particle_layers` (Np) | – | spherical FVM layers; Coletto uses 12 |
| `nz_per_zone` | – | axial cells per zone per tray |
| `htc_correlation` | – | Faner Nu = 0.6949·Reε^0.579·PrV^(1/3) (B.7); coeffs overridable |
| `D_HW` | m²/s | hexane–water diffusivity for Schmidt number (B.10) |
| `X2_critical_mode` | – | compute via (4) from densities, or override |
| `outer_relaxation` | – | under-relaxation factor for the DT fixed-point sweep |
| `outer_tol`, `outer_max_iter` | – | convergence of the integrated DT solve (§7.8) |
| `sweep_arm_transfer_gain` | – | central-shaft sweep/rake arms: sets bed turnover/residence time; secondary effect on hQ/hM (§7.9) |

`Geometry`:
| field | unit | notes |
|---|---|---|
| `n_stages` | – | total number of modeled stages |
| `stages[]` | – | ordered list; each: `{id, role, vapor_path, diameter_m, bed_height_m}` |
| `role` ∈ | – | `PREDESOLV`, `MAIN`, `SPARGE`, `DRYER`, `COOLER` |
| `vapor_path` ∈ | – | `BYPASS`, `THROUGH_BED`, `STEAM_SOURCE`; independent of meal transfer |

`Topology.solid_transfers[]` defines one meal-transfer boundary below each
stage: `{id, from_stage, to_stage, device_type, controlled, vapor_seal,
fixed_position_pct, capacity_factor}`. Device type is one of
`PASSIVE_SWEPT_PORT`, `CONTROLLED_GATE`, or `ROTARY_AIRLOCK`. This separates
the thermodynamic tray, vapor path, and physical PLC actuator.

### 5.2 Hot inputs (owned by the runtime; NOT part of the frozen Model)

**Manipulated variables (MV)** — controllable and arbitrated (§6):
| mv key | scope | unit | physical effect |
|---|---|---|---|
| `feed_flow_rate` | global | kg/s (dry solid) | sets top solid inflow ṁ_s |
| `direct_steam[stage]` | per SPARGE/MAIN stage | kg/s or 0–100 % valve | sparge steam → heat + water into meal |
| `indirect_steam[stage]` | per DT stage | kW or 0–100 % valve | jacket duty Q̇_indirect |
| `sweep_arm_speed[stage]` | per stage | rpm | central rotating shaft with sweep/rake arms; conveys meal across the tray toward discharge → sets bed turnover & residence time (secondary: surface renewal → hQ/hM) |
| `transfer_device_position[boundary]` | controlled solid-transfer boundary only | 0–100 % | active gate/airlock position; passive swept ports have no MV |
| `heated_air_temp` | DRYER | K | drying air inlet temperature |
| `heated_air_flow` | DRYER | kg/s | drying air rate |
| `ambient_air_temp` | COOLER | K | cooling air inlet temperature |
| `ambient_air_flow` | COOLER | kg/s | cooling air rate |

Every MV has: `limits {min,max}`, optional `rate_limit [unit/s]`, `mode`, `manual_setpoint`,
`auto_setpoint`, `effective_value` (computed, read-only). See §6.

**Disturbance variables (DV)** — writable (by UI or a test harness over OPC UA), no A/M mode:
| dv key | unit | notes |
|---|---|---|
| `feed_moisture` | kg/kg | water in incoming meal |
| `feed_hexane` | kg/kg | hexane in incoming meal |
| `ambient_temp` | K | environment |
| `ambient_humidity` | kg/kg | environment |

**Simulation control:**
| key | type | notes |
|---|---|---|
| `speed_factor` | float ≥ 0 | sim seconds per wall second; 0 ⇒ paused |
| `command` | enum | `RUN`, `PAUSE`, `STOP`, `RESET`, `RECONFIGURE` (as OPC UA methods) |
| `global_mode` | enum | convenience: set all MV modes to MANUAL or AUTO at once |
| `sim_time` | float (read-only) | current simulation clock [s] |
| `actual_speed` | float (read-only) | achieved sim/wall ratio (overrun detector) |
| `state` | enum (read-only) | lifecycle state |

---

## 6. MANUAL / AUTO arbitration (per MV)

Each MV holds **two independent setpoints** and a **mode**:
- `manual_setpoint` — written by the **UI** only.
- `auto_setpoint` — written by **OPC UA (the APC)** only.
- `mode ∈ {MANUAL, AUTO}` — writable by **both** UI and OPC UA.

**Effective value each tick:**
```
raw = manual_setpoint if mode == MANUAL else auto_setpoint
clamped = clip(raw, limits.min, limits.max)
effective_value = apply_rate_limit(clamped, previous_effective, rate_limit, dt)
```
The engine feeds `effective_value` into `u`. Writes to the non-active setpoint are accepted and
stored but do not affect `effective_value` until the mode switches — so UI and APC never conflict.

**Bumpless transfer on mode change:**
- switching to `MANUAL`: set `manual_setpoint := effective_value` at the instant of switch.
- switching to `AUTO`: set `auto_setpoint := effective_value` at the instant of switch.
This guarantees no step in the actuator when handing control over.

`global_mode` simply applies the same mode + bumpless transfer to every MV.

---

## 7. Numerical core specification — Coletto (2022) dual-scale zonal model

This section is **normative and precise**. The DT is modeled after Coletto, Bandoni & Blanco
(2022), *J. Food Eng.* 318, 110870, which itself extends Cardarelli, Crapiste & Mattea (2002).
Equation tags in parentheses (e.g. `A.22`) refer to that paper's appendix for traceability; the
agent implements the equations as written here. Notation follows the paper's nomenclature
(reproduced in §7.11). **The DC (dryer/cooler) section is not covered by Coletto and uses the
simpler air-contacting stage model of §7.10.**

### 7.0 Model philosophy
The DT is divided **axially into three zones** by the dominant phenomenon, not by tray:
- **PHZ** — Pre-Heating Zone: solid heated to hexane boiling point; little/no evaporation.
- **FTRZ** — Flashing & Temperature-Raising Zone: simultaneous water condensation + violent
  hexane flash; governed by the **Receding Front Model**. Very thin (order cm).
- **DCZ** — Diffusion-Controlled Zone: residual hexane removal limited by **intraparticle
  diffusion**; solved **dual-scale** (particle spherical-shell FVM ↔ axial bed).

A zone may span several trays or part of one. In the reference geometry (6 trays: 3 pre-desolv +
3 counter-current) the PHZ covers the pre-desolv trays, the FTRZ starts at the 4th tray, and the
DCZ covers the remainder of the 4th plus the 5th and 6th. Zone boundaries are **not fixed** —
FTRZ length `LFTRZ` and the PHZ/FTRZ and FTRZ/DCZ interfaces are solved for.

**Three time-scales — the system is DYNAMIC; only the DT spatial solve is steady per tick.** Be
precise, because "steady-state" here does NOT mean "not time-varying":
1. *Particle scale (within Coletto's DT solve):* time-dependent radial diffusion `∂/∂t`, used as a
   residence-time surrogate — a particle's elapsed time maps to its axial bed position.
2. *Bed scale (within Coletto's DT solve):* steady in space, `d/dz`, no `∂/∂t`. So one DT solve
   produces the spatial profile that corresponds to the **current** inputs.
3. *Application scale (our real-time wrapper, §7.9):* fully time-dependent. Each engine tick feeds
   the current (possibly just-changed) inputs into a fresh DT solve, AND advances genuine transient
   states carried between ticks — the DC holdups and the
   transport-lag states. Across ticks the plant state evolves in time; the DT interior simply tracks
   a *moving* equilibrium because its residence time (~25–30 min) dwarfs a sub-second tick.
In one line: **a dynamic simulator whose DT interior is solved quasi-steady each tick.** The
published Coletto model supplies scales 1–2; scale 3 is our wrapper (§7.9), not the paper.

Common assumptions (enforce/consume): atmospheric pressure, negligible pressure drop; constant
solid and vapor densities; constant `D_eff`, `D_ax`, gas `CP`, gas `k`; particle pores initially
saturated with hexane (`wpg2(r)=1 ∀r`) at DT entry; constant bed/particle gas volume fractions.

### 7.1 Axial discretization (bed scale)
Each zone within each tray is discretized into `nz` axial cells of volume `Vj = Δz_j · Abed`
(`Fig. 3a`). Suffix `j` labels variables **exiting** cell `j`. Macroscopic balances are the
integrals of the microscopic balances over `Vj`. Superficial reference velocities:
```
ṁ_V  = uV,sp · ρV · Abed                    (1)   direct steam at DT bottom
ṁ_ds = αps · αL · ρps · uL · Abed           (2)   incoming meal at DT top (dry solid, constant)
```

### 7.2 Component / phase notation (fix these indices in code)
- Component subscripts: **1 = water, 2 = hexane, 3 = oil**.
- Streams: **L** = descending solid/liquid (meal), **V** = ascending vapor.
- Particle phases: **ps** = solid, **pg** = pore gas, **o/3** = oil.
- `X_i` = solid-stream content of component `i` `[kg_i / kg dry solid]`.
- `wV_i` = mass fraction of `i` in vapor; `YV2 = wV2/wV1` = hexane in **water basis** `(A.2b)`.
- `W2` = hexane adsorbed on particle solid phase `[kg/kg ds]`; `qo` = hexane in oil `[kg/kg oil]`;
  `wpg2(r)` = hexane mass fraction in particle pore gas (radial field).
- `α` = volume fraction; `ρ` = density; `Ĥ` = specific enthalpy; `av` = specific area `[m²/m³]`.

---

### 7.3 Pre-Heating Zone (PHZ) — `A.1`
Only energy source = latent heat of indirect steam condensing in the tray, applied as a uniform
**volumetric** heat. Microscopic balances integrated over each cell (Table A.1). Solid heats from
inlet to `Thex,bp`; hexane evaporation is switched **on only at the boiling point**:
```
q̇_Iv        = Q̇_I / (Abed · LPHZ)                                   (A.2a)   indirect volumetric heat
SLm2        = 0                              if TLmix < Thex,bp      (A.1a)
            = − q̇_Iv / ΔĤlv,hex             if TLmix = Thex,bp
SLQmix      = q̇_Iv / ΔĤlv,hex                                        (A.3a)   solid energy source
```
Vapor-side in PHZ: no rising vapor from below in the interparticle space (only vapor from boiling
hexane); use the mixture mean thermophysical properties (`B.1`–`B.6`). Integrate cell balances
over the zone. Solid exits with pores partially/fully saturated (`X2 ≤ X2,cr`).

### 7.4 Sorption, VLLE, critical/equilibrium hexane — `(3)–(7)`, `A.31`
**Sorption isotherm (solid phase):** hexane adsorbed on the solid is a function of hexane activity
`ah` via the **Guggenheim-Anderson-DeBoer (GAB)** model; GAB parameters and their temperature
dependence come from **Cardarelli & Crapiste (1996)** (material-specific: soybean/sunflower/…).
Implement `W2_eq = GAB(ah, T; gab_params)`.

**Hexane absorbed in oil (oil basis), power law `(7)`:**
```
qo = A0 · ah^B                              (A0, B from Cardarelli & Crapiste 1996, oil basis)
```

**Heat of sorption `(A.31)`:** net isosteric contribution added to latent heat:
```
ΔĤs = ΔĤlv2 + C0 · W2^C1        (C0, C1 material-specific: Cardarelli 1998 / Faner 2008)
```

**Critical hexane content `(4)`** (pores just saturated with liquid hexane once surface hexane
is gone):
```
X2,cr = (αpg · ρhexL) / (αps · ρps)          ρhexL = liquid hexane density at Thex,sat (B.11–B.12)
```

**Equilibrium hexane content `(5)–(6)`** (pores hold only hexane *vapor*, in equilibrium with
adsorbed/absorbed hexane):
```
X2,eq   = W2,eq + X3·qo,eq + Ypg2,eq          (5)
Ypg2,eq = ρhexV·(1 − αps) / (ρps·αps)         (6)   ρhexV = hexane vapor density at atm P, Tbp
```

**VLLE of hexane–water:** the mixture shows vapor–liquid–liquid equilibrium (two liquid phases at
the azeotrope). For hexane below the azeotrope and T between azeotrope and mixture boiling point,
the liquid is water-only while the vapor is a water–hexane mix. In the FTRZ the ascending vapor
path is split into **V-SCAL** (superheated: `Ĥvbw = f(YV2,TV)`, no water condensation,
`ṁ_w,con = 0`) and **V-SAT** (saturated: on the boiling curve, water condenses). Provide the
`Ĥvbw = f(wV2, TV)` curve and its inverse (temperature from enthalpy+composition).

### 7.5 Flashing & Temperature-Raising Zone (FTRZ) — `A.2`, Receding Front `(3)`
Water condenses from the vapor while hexane flashes out of the solid; the vapor dew point drops
rapidly. Modeled with the **Receding Front Model**: below `X2,cr` a sharp front recedes into the
particle leaving a dry shell; the wet core sits at `Thex,bp`; dry-layer **mass**-transfer
resistance is taken negligible vs the flash driving force (Cardarelli 2002). Wet-front radius:
```
                 ⎧ rP                                             , X2 > X2,cr
rfr =            ⎨                                                                 (3)
                 ⎩ rP · ((X2 − X2,eq)/(X2,cr − X2,eq))^(1/3)      , X2 ≤ X2,cr
```
Wet-core mass fraction `wh` follows from `rfr`; solid temperature is interpolated `(A.17)`:
```
TL = wh·Thex,bp + (1 − wh)·TV                (A.17)
```

**Ascending-vapor macroscopic balances (solved cell bottom→top)** `(A.1b–A.5, A.6–A.11)`:
```
hexane:  ṁ_V1,j·YV2,j − ṁ_V1,j+1·YV2,j+1 = ⟨SVm2⟩_j·Vj           (A.1b)
water:   ṁ_V1,j       − ṁ_V1,j+1        = ⟨SVm1⟩_j·Vj           (A.3b)
energy:  ṁ_V1,j·Ĥvbw,j − ṁ_V1,j+1·Ĥvbw,j+1 = ⟨SVQ⟩_j·Vj          (A.4),  Ĥvbw = ĤV/wV1  (A.5)
```
Source-term closures (assumption: equal hexane removed per cell):
```
ṁ_hex,ev   = ṁ_ds·(X2,sup − X2,inf)/nz                            (A.6)
⟨SVm2⟩_j·Vj = ṁ_hex,ev                                            (A.7)
⟨SVm1⟩_j·Vj = ṁ_w,con,j   { 0 in V-SCAL ; from balances in V-SAT } (A.8–A.9)
⟨SVQ⟩_j     = q̇_Iv ,   q̇_Iv = Q̇_cond/(Abed·LFTRZ)                (A.10–A.11)
```
**Descending-solid macroscopic balances** `(A.12–A.16)`:
```
ṁ_ds·(X2,j − X2,j−1) = ⟨SLm2⟩_j·Vj ,  ⟨SLm2⟩_j·Vj = −ṁ_hex,ev     (A.12, A.15)
ṁ_ds·(X1,j − X1,j−1) = ⟨SLm1⟩_j·Vj ,  ⟨SLm1⟩_j·Vj = −ṁ_w,con,j     (A.13, A.16)
ṁ_ds·(ĤLds,j − ĤLds,j−1) = ⟨SLQ⟩_j·Vj                             (A.14)
```
**Variable cell thickness** (energy-driven, not uniform) `(A.18–A.21)`:
```
Δz_j   = ṁ_hex,ev·ΔĤlv,hex / [ Abed·αL·aV·(JQ,cs + JQ,cv)_j ]      (A.18)
JQ,cs,j = ΔĤlv,w · ṁ_w,con,j                                       (A.19)
JQ,cv,j = hQ·(TV − TL)_j                                           (A.20)
LFTRZ  = Σ_j Δz_j    (updated each outer iteration)                (A.21)
```
FTRZ terminates when `TL → TV,inf`; at that point `X2 = X2,eq(TV,inf)` and the DCZ begins.

### 7.6 Diffusion-Controlled Zone (DCZ) — dual scale — `A.3`
Residual hexane removal is diffusion-limited. Three particle phases (solid, oil, gas); radial
profiles `wpg2(r)`, `Tp(r)` (single T for all three phases). Hexane is adsorbed on solid + absorbed
in oil, both in **local equilibrium** with pore-gas hexane (via §7.4 isotherms).

**Particle scale (time-dependent, spherical), hexane `(A.22, A.26–A.29)`:**
```
∂(αpg·ρpg·wpg2 + αps·ρps·X2,so)/∂t = ∇·(αpg·ρpg·D_eff·∇wpg2)      (A.22)
X2,so = W2 + X3·qo                                                 (A.26)
Ca    = αpg·ρpg / (αpg·ρpg + αps·ρps · dX2/dwpg2)                  (A.28)
⇒  ∂wpg2/∂t = ∇·(Ca·D_eff·∇wpg2)                                  (A.29)
BCs: ∂wpg2/∂r = 0 at r=0 (symmetry);  at r=rP: JM·ř = −hM·ρV·(wV2 − wpg2R)  (convective);  wpg2(r,0)=wpg2,sup
```
**Particle-scale energy `(A.23, A.30–A.32)`:**
```
∂((αpg·ρpg·CPpg + αps·ρps·CPps)·Tp)/∂t = ∇·((αpg·kpg + αps·kps)·∇Tp) + SQ   (A.23)
SQ = −αps·ρps·(∂W2/∂t)·ΔĤs − αps·ρps·(∂qo/∂t)·ΔĤlv2 + q̇_condL              (A.30)
q̇_condL = ∇·(kLmix·∇TL)                                                     (A.32)
BCs: ∂Tp/∂r = 0 at r=0;  at r=rP: JQ·ř = −hQ·(TV − TpR);  Tp(r,0)=Tp,sup
```
Solve the particle by **Finite Volume Method, 12 spherical layers** (`Np = 12`). Volumetric mean
maps particle field → bed solid property `(8)`:  `⟨φ⟩ ≈ Σ_i φ(r_i)·V_i / Σ_i V_i`.

**Bed scale (steady in z), hexane `(A.24, A.33)` and energy `(A.25, A.34)`:**
```
∇·(αV·ρV·wV2·uV) = ∇·(αV·D_ax·ρV·∇wV2) + SVm2                     (A.24)
∇·(αV·ρV·CPV·TV·uV) = SVQ                                          (A.25)
SVm2 = −aV·αL·JM2R·ř                                              (A.33)
SVQ  = −aV·αL·JQR·ř + SVm2·Ĥ2 + q̇_Iv + ṁ′ax,net·Ĥ2              (A.34)
   with ṁ′ax,net·Ĥ2 = ∇·(αV·D_ax·ρV·∇wV2)·Ĥ2
BCs: at z=LDCZ: wV2=wV2,inf, TV=TV,inf ;  at z=0: uV·wV2 − D_ax·∂wV2/∂z = uV·wV2,sup
```
**Bed integration (Rhodes-type packed-bed method) `(A.35–A.37)`:** for a generic property φ per
cell j,
```
αV·‖uV‖·ϱ · dφ/dz = −κφ·aV·αL·(φV − φL) + S*φV                    (A.35)
   mass:   ϱ=ρV      , φV=wV   , φL=wpg2,12   , S*φV = ṁ′ax,net              (A.36)
   energy: ϱ=ρV·CPV  , φV=TV   , φL=Tp,12     , S*φV = SVm2·Ĥ2 + q̇_Iv + ṁ′ax,net·Ĥ2  (A.37)
```
**DISCRETIZATION NOTE (rigor — do not implement the continuous forms directly).** Equations
`A.22/A.23` (particle) and `A.24/A.25` (bed) are stated by Coletto in **continuous** ∇· form. They
are the *governing* PDEs, not the discrete update. Implement them discretized:
- `A.22`/`A.23` → **radial finite-volume** discretization over `Np=12` spherical shells (particle).
  The particle `∂/∂t` is the residence-time march (time ↔ bed position), NOT the outer real-time
  tick. Advance it per Coletto's algorithm; the outer real-time wrapper is §7.9.
- `A.24`/`A.25` → recast into the **axial per-cell integral form `A.35`** and integrate cell-by-cell
  from zone bottom to top (below). Do not discretize the divergence form of `A.24` directly.

"12" = the 12th (outermost) particle layer, the particle↔vapor coupling point. Integrate each
cell bottom→top.

### 7.7 Thermophysical properties — `B` (implement exactly)
```
ρLmix  = αL·ρL + αV·ρVip                                          (B.1)
ρL     = αps·ρps·(1 + X1 + X2 + X3)                               (B.2)
ρVip   = (yV1·Mw + yV2·Mhex)·P / (R·TL)                           (B.3)
CPLmix = (αL·ρL·CPL + αV·ρV·CPVip)/ρLmix                          (B.4)
CPL    = Σ_{i=1..4} wLi·CPi                                       (B.5)
CPVip  = Σ_{i=1..2} wVi·CPi                                       (B.6)
Nuε    = 0.6949·Reε^0.579·PrV^(1/3)   (Faner heat-transfer corr.) (B.7)
Nuε    = 2·hQ·rP/kV · (αV/αL)                                     (B.8)   ⇒ solve hQ
hM     = hQ/(ρV·CPV) · (PrV/Scp)^(2/3)   (Chilton–Colburn)        (B.9)
Scp    = μV/(ρV·D_HW)                                             (B.10)
ρhexL  = 61.034 / (0.26411^f1)          (Daubert–Danner)          (B.11)
f1     = 1 + (1 − Tp/507.6)^0.27537                               (B.12)
```

### 7.8 Integrated DT solving order (steady spatial solve, per outer iteration) — `Fig. 5`
Iterate tray-by-tray, **first tray → last tray**:
1. Initialize boundary conditions for every tray from macroscopic zone balances + literature,
   then interpolate solid/vapor T and composition vs height position.
2. For the FTRZ-containing tray, solve **FTRZ before DCZ**.
3. Solve each tray's active zone sub-model (PHZ / FTRZ / DCZ) with its BCs from neighbor trays.
4. Update zone lengths (`LFTRZ`, PHZ/FTRZ/DCZ interfaces) and repeat until stream profiles
   converge (outer loop index `h`).
Counter-current coupling ⇒ a tray's vapor inlet is the vapor outlet of the tray below; the whole
sweep is a fixed-point problem. Reference implementation was in R; here it is Python (§12).

### 7.9 Embedding the steady DT model in the real-time surrogate (design decision — normative)
Coletto's DT model is **steady-state** (bed steady in z; particle transient only as a
residence-time surrogate). To act as a real-time plant surrogate driven by an APC, wrap it as
follows:

- **DT block = quasi-steady map.** Treat the converged Coletto solve as a function
  `dt_solve(u_dt, boundary) → (solid_out, vapor_out, profiles, KPIs)` where `u_dt` = the DT-relevant
  hot inputs (feed, direct/indirect steam, sweep-arm speed, level). It is recomputed each engine tick with
  the **current** effective inputs (zero-order hold). Because residence time (~25–30 min) >> tick,
  quasi-steady tracking is physically reasonable for the DT interior.
- **Transport lag is added explicitly.** To preserve realistic dead time / first-order lag for the
  APC, pass DT outputs through per-tray first-order holdup lags (time constants from tray holdup /
  ṁ_ds), OR advance the particle-scale `∂/∂t` states across the tick using the residence-time map.
  `DECIDE` which; document the choice. Do **not** present a pure algebraic step response to the APC.
- **Warm start.** Seed each tick's fixed-point iteration from the previous tick's converged
  profiles → few iterations, fast enough for real-time. Cap iterations; if an attempt does not
  converge within the tick budget or fails physical-admissibility checks, retain the last accepted
  targets/profile atomically and flag `Sim/SolverStress`. The rejected best iterate remains a
  solver diagnostic, not a process PV.
- **Convergence/robustness guards.** Damp the outer fixed-point (under-relaxation), bound the VLLE
  dew-point solve, clamp `rfr∈[0,rP]`, and detect non-convergence without crashing the loop.
- **Free-run determinism** still holds: same inputs + same warm-start policy ⇒ same result.

> Risk note (normative to surface, not hide): the full dual-scale solve is stiff and iterative;
> hitting real-time at high `speed_factor` is the main performance risk. Mitigations above
> (warm-start, iteration cap, `SolverStress` node, optional coarser `nz`/`Np` at high speed) MUST
> be implemented. If real-time cannot be met, the engine reports reduced `actual_speed` rather than
> silently falling behind (§8).

### 7.10 DC section (Dryer/Cooler) — not in Coletto
Coletto covers the DT only. Model DRYER/COOLER stages as air-contacting well-mixed stages:
solid-side water evaporation driven by air humidity deficit and heated-air sensible heat (DRYER);
sensible cooling by ambient air (COOLER); residual-hexane stripping to air. Use the same
per-stage balance structure with air as the gas phase. `DECIDE` correlation details; keep it
behind the same stage interface so it can be upgraded later.

### 7.11 Quality kinetics — removed
Two Arrhenius quality kinetics (TIA biexponential decay, protein-denaturation first-order decay)
were implemented here per `DTDC and VRXDTDC.pdf` (Chen et al. 2014 basis), advected with the
descending solid and coupled to the local solid temperature/moisture. Both were removed from the
shipped application to keep the codebase lean — see DECISIONS.md's dated entries for the removal
rationale. Neither fed back into the hexane/moisture balances at any point, so removing them has
no effect on the DT/DC thermal-hydraulic model.

### 7.12 State vector and the `Model` interface
Because the DT interior is solved quasi-steady per tick (§7.9), the persistent transient **state
`x`** carried between ticks is the minimal set needed for lag + quality history:
- per DT tray: converged zone profiles used as **warm start** (not integrated as ODEs, but stored),
  plus optional particle-layer fields `wpg2,layer[tray][1..12]` if the ∂/∂t advance option is chosen;
- per stage: first-order lag states for published outputs (if the lag option is chosen);
- DC stages: `X_w`, `T` (and residual hexane) as ODE holdup states (§7.10).

`core.Model` exposes:
```
init_state(config) -> x0                         # via §7.8 steady solve at operating defaults
step(x, u, t, dt) -> (x_next, y)                 # advances one tick: dt_solve warm-started + lags + quality + DC
outputs(x, u) -> y                               # KPIs/PVs from current x,u  (pure)
```
Note: unlike a pure ODE core, the DT uses `step()` (contains the inner fixed-point solve), not a
bare `derivatives()`. The DC section and quality/lag states MAY still expose `derivatives()` and be
integrated by `solve_ivp`; the engine composes both. Keep everything pure (no I/O).

### 7.13 Fidelity ladder (all behind the same `Stage`/zone-strategy interface)
The Coletto dual-scale model is the **v1 target** (this section). Optional refinements, each a
strategy swap that touches neither config nor interfaces:
1. Richer FTRZ solid-side heat/mass transfer (paper notes outlet solid T under-predicted ~5–10 °C).
2. Full transient bed scale (drop the steady-bed assumption; integrate `∂/∂t` at bed scale too) —
   heavier, higher-fidelity dynamics for fast transients.
3. Coupled quality feedback and VRX-specific trays (superheated pre-desolventizing).
4. Higher particle resolution (`Np > 12`) / finer `nz` for validation runs.

---

## 8. Real-time engine specification

### 8.1 Tick loop (per publish interval `Δt_wall`, `DECIDE` default 100–250 ms)
```
u          = facade.read_effective_inputs()      # MV effective_value + DV + boundary
dt_target  = clock.advance(Δt_wall)              # RealTime: speed·Δt_wall ; FreeRun: Δt_sim
x, y       = model.step(x, u, sim_time, dt_target)  # DT quasi-steady solve (warm-started) + DC/quality/lag ODEs; ZOH on u
sim_time  += dt_target
facade.publish(snapshot(x, y, sim_time))
clock.pace(Δt_wall)                              # RealTime: sleep remaining; FreeRun: no sleep
facade.record_actual_speed()                     # overrun / SolverStress detector
```
`model.step()` internally warm-starts the DT fixed-point sweep (§7.8/7.9) from `x`, integrates the
DC/quality/lag ODE states with `scipy.integrate.solve_ivp` (stiff `BDF`/`LSODA`), and returns the
updated persistent state plus outputs. Inputs `u` are held constant across the tick (**ZOH**).

### 8.2 Clocks (injectable strategy)
- `RealTimeClock(speed_factor)`: `advance = speed_factor·Δt_wall`; `pace` sleeps the remaining
  wall budget; if integration overran, do not sleep and report `actual_speed < speed_factor`.
- `FreeRunClock(dt_sim)`: `advance = dt_sim`; `pace` is a no-op. Deterministic, as-fast-as-possible.

### 8.3 Threading (critical)
`asyncua` runs on an asyncio event loop. Integration is CPU-bound NumPy and MUST NOT block it.
Run the tick loop in a **worker thread** (or `loop.run_in_executor`). The `RuntimeFacade`
serializes access to shared state with a lock; the OPC UA server and UI read snapshots and write
setpoints/commands through the facade. Publishing to OPC UA nodes happens from a facade snapshot.

### 8.4 Speed/undersampling constraint (must be enforced/warned)
Effective control sampling in sim-time = `speed_factor · Δt_wall`. This must stay ≤ the APC sample
time or the loop is undersampled. Keep `Δt_wall` small and, if `speed_factor·Δt_wall` exceeds a
configured `max_control_interval`, clamp `speed_factor` and surface a warning node.

---

## 9. OPC UA server specification

- Library: `asyncua`. Endpoint `opc.tcp://0.0.0.0:4840/dtdc/` (`DECIDE` port).
- Security: `SecurityPolicy#None`, allow anonymous. **No encryption/auth.** Document the risk.
  (Runtime security/certificate control is a later milestone.)
- Namespace: one custom namespace URI, e.g. `http://dtdc.sim/`.

### 9.1 Strict superset address space (Phase 1, 2026-07-24)

The address space is rendered from the fixed **equipment envelope**
(`envelope.yaml`, `config/envelope.py`; rationale in
`app_specifications/DTDC_Equipment_Envelope.md`), **not** from the loaded
scenario. Every canonical stage, actuator, KPI and control loop of the maximal
realistic DTDC (PREDESOLV ≤ 7, MAIN ≤ 4, SPARGE 1, DRYER ≤ 3, COOLER ≤ 2 → 17
canonical stage slots) is created **once** at a fixed path. A given build only
marks each node:

- **active** — bound to a live model quantity, `StatusCode = Good`; or
- **placeholder** — present, value nulled, `StatusCode = Bad_NotConnected`, with a
  sibling `Present = false` boolean.

Reconfiguring therefore never adds or removes a node — it only flips the active
mask — so an APC/DCS tag map written once against the canonical names never
needs remapping. Canonical stage slots bind to build stages by **(role, order)**
(`interfaces/opcua/address_space.py::compute_active_mask`), so the interface is
stable even if a scenario uses non-canonical stage ids.

```
Objects/DTDC/
  Config/                        (RO; how to read this build against the superset)
    EnvelopeVersion              (RO int)
    ActiveStageCount             (RO int)
    BuildManifest                (RO String; JSON: active_stages, stage_binding,
                                  active_control_loops, role_counts)
  Constants/                     (RO provenance; structure schema-fixed, values per build)
    Physical/...                 (all PhysicalParams; nested groups as sub-objects)
    Model/...                    (all ModelParams)
    Geometry/Stage/<CANON>/      {Role, Diameter, BedHeight, VaporPath, ArmMixing, Present}
  Control/<PLC_loop_tag>/        (canonical loop superset; ZIC_<CANON> per stage outlet)
    Mode (RW enum MANUAL|AUTO), SP (RW), PV (RO), OP (RW),
    Units, Status, Description, Min, Max (RO), Present (RO bool)
  SimulationInputs/<dv_key>      (RW; the 6 disturbances, always active)
  Measurements/
    Stage/<CANON>/               {T, X_hex, X_w, VaporTemp, Level, Role, Present} (RO)
    KPI/<name>                   (RO; the 13 fixed KPIs)
  Diagnostics/
    InternalMV/<CANON_key>/      {Mode, ManualSetpoint, AutoSetpoint, EffectiveValue, Present} (RO)
  Simulation/
    SpeedFactor (RW), DTResolveIntervalS (RW), SimTime (RO), ActualSpeed (RO),
    State (RO enum), GlobalMode (RW enum), UndersampleWarning (RO bool),
    SolverStress (RO bool), DTSolverOuterIterations (RO int)
    Methods: Run(), Pause(), Stop(), Reset(), Reconfigure()
```

`<CANON>` = a canonical id (`PD1..PD7`, `MN1..MN4`, `SP1`, `DR1..DR3`, `CL1..CL2`).
DC steam-drying trays are **not modeled** in this version (see
`DECISIONS.md` 2026-07-24), so there is no such zone/node.

### 9.2 Behaviors
- Writes to `SP` feed the bound actuator target when the loop is in `AUTO`.
- Writes to `OP` feed the bound manual actuator target when the loop is in `MANUAL`.
- Only **active** loops route client writes to the facade; writes to placeholder
  (inactive) loops are ignored.
- Zone-total and common-shaft loops map atomically to internal per-stage values
  using fixed scenario allocation weights.
- Mode changes trigger bumpless transfer (§6).
- Method calls drive the state machine (§4.1) and return success/failure.
- Every refresh cycle **pulls** changed writable nodes into the facade, then
  **pushes** a fresh facade snapshot back out (push-follows-pull, so a
  UI-originated change and a client write each converge within one cycle). The
  active mask and `BuildManifest` are recomputed each push, so a reconfigure is
  reflected without rebuilding the tree. `Constants/*` are read-only here
  (writing constants in CONFIGURED is a later milestone).

---

## 10. UI specification

Framework: `DECIDE` — recommended a Python-native reactive stack (e.g. NiceGUI or Dash) so the UI
can talk to the `RuntimeFacade` directly in-process; keep it an adapter so it can be replaced.
The UI must **not** import `core/`.

### 10.1 Setup screen (state CONFIGURED)
- Select material property set; load/edit `PhysicalParams`, `ModelParams`, `Geometry`.
- Edit `OperatingDefaults` / `DisturbanceDefaults` (seed values).
- Load from / save to YAML config files.
- "Validate & Assemble" button → runs setup phase; shows validation errors; on success → READY.

### 10.2 Runtime dashboard (state READY/RUNNING/PAUSED)
- Controls: Run/Pause/Stop/Reset/Reconfigure; `speed_factor` slider; global MANUAL/AUTO toggle;
  live `sim_time`, `actual_speed`, undersample warning.
- Per MV: mode selector (MANUAL/AUTO), a manual setpoint control (enabled only in MANUAL),
  a read-only display of `auto_setpoint` and `effective_value`, and limit display.
- Live plots: per-stage `T`, `X_hex`, `X_w`; KPI trends. Update from facade snapshots.
- Constants shown read-only while running (with a "Reconfigure" affordance that stops the loop).

---

## 11. Config schema and files

- Pydantic v2 models with field validators (ranges, positivity, porosity∈(0,1), Arrhenius sanity,
  isotherm monotonicity check as a model-level validator).
- All fields carry explicit SI units in their names/docstrings; conversion to SI happens once at
  load. A cross-field `check_physical_consistency()` runs before assembly.
- File format: YAML. One file per material property set under `properties/`; one scenario file
  binding a property set + model params + geometry + operating/disturbance defaults.

Example scenario file (illustrative shape):
```yaml
material: soybean
geometry:
  n_stages: 9
  stages:
    - {id: PD1, role: PREDESOLV, diameter_m: 4.0, bed_height_m: 0.30}
    - {id: MN1, role: MAIN,      diameter_m: 4.0, bed_height_m: 1.00}
    - {id: SP1, role: SPARGE,    diameter_m: 4.0, bed_height_m: 0.60}
    - {id: DR1, role: DRYER,     diameter_m: 3.0, bed_height_m: 0.50}
    - {id: CL1, role: COOLER,    diameter_m: 3.0, bed_height_m: 0.50}
model:
  k_mass_transfer: 0.02
  h_heat_transfer: 5000.0
  # ...
operating_defaults:
  feed_flow_rate: 25.0
  indirect_steam: {PD1: 800e3, MN1: 1200e3}
  # ...
disturbance_defaults:
  feed_moisture: 0.12
  feed_hexane: 0.30
sim:
  speed_factor: 1.0
  dt_wall_s: 0.2
  max_control_interval_s: 10.0
```

---

## 12. Technology stack
- **Python 3.14 (3.14.5).**
- `numpy`, `scipy` (integrate: `solve_ivp` BDF/LSODA; optimize: `root`)
- `pydantic` v2 (config + validation)
- `asyncua` (OPC UA server; pure Python — fine on 3.14)
- UI: `DECIDE` (NiceGUI / Dash recommended)
- Testing: `pytest`, `hypothesis` (optional for property tests)
- Packaging: `pyproject.toml`, ruff + black for lint/format.

**Python 3.14 wheel caveat (normative check before install):** verify current binary wheels for
the compiled deps on cp314 — `numpy`, `scipy`, and `pydantic-core`. If a wheel is missing for
3.14.5 at build time, pin to the latest version that ships cp314 wheels, or (last resort) use a
3.13 interpreter for that dependency set and record it in `DECISIONS.md`. `asyncua` is pure Python
and imposes no constraint. Do not build scipy from source unless unavoidable.

---

## 13. Testing and validation
- **Core unit tests (no I/O):**
  - Conservation: hexane + water + energy close across the integrated DT sweep (in = out + accumulation) to tolerance.
  - Sorption isotherm (GAB) monotonic in `ah`; heat of sorption `ΔĤs → ΔĤlv2` as `W2 → 0`.
  - Receding front: `rfr = rP` for `X2 > X2,cr`; `rfr → 0` as `X2 → X2,eq`; `rfr` clamped to `[0,rP]`.
  - Particle FVM (12 layers): radial symmetry BC at `r=0`; converges under refinement; conserves hexane.
  - Property correlations (`B.7`–`B.12`) reproduce paper values; `hM` from `hQ` via Chilton–Colburn.
  - DT fixed-point sweep converges from a warm start; residual below `outer_tol`.
  - **Validation vs Coletto (2022):** DCZ hexane ~4000 ppm → ~100 ppm; thin FTRZ (order cm);
    solid/vapor temperature and water/hexane profiles match the paper's figures within tolerance.
- **Determinism test:** FreeRunClock with fixed seed/config + fixed input trajectory reproduces
  identical output bit-for-bit across runs.
- **Real-time pacing test:** with a light model, `actual_speed ≈ speed_factor` within tolerance;
  overrun is detected and reported when the model is made heavy.
- **Arbitration tests:** MANUAL ignores AUTO writes and vice versa; bumpless transfer produces no
  step in `effective_value`; rate/limit clamping works.
- **OPC UA integration test:** a test client connects, reads PVs, writes an MV `AutoSetpoint` in
  AUTO mode, and observes the corresponding PV respond.

---

## 14. Build order (milestones for the agent)

The v1 physics **is** the Coletto (2022) dual-scale model (§7). Build it in verifiable slices; do
not attempt the whole dual-scale solve before the plumbing works end to end.

**M0 — Walking skeleton (end-to-end, placeholder physics).**
`config` models + validators; `RuntimeFacade`; a **placeholder** `Model.step()` (a few well-mixed
DT trays + trivial DC, just enough to move numbers); both clocks; threaded tick loop; minimal
`asyncua` server exposing one MV (with A/M), a few PVs, and Sim controls; minimal UI (Run/Pause +
speed slider + one plot). Acceptance: connect a client, write a steam setpoint in AUTO, watch a PV
move; toggle to MANUAL and drive from the UI. **No Coletto equations yet.**

**M1 — Thermo, sorption, properties (pure, unit-tested).**
Implement §7.4 (GAB isotherm, oil power-law, heat of sorption, VLLE `Ĥvbw` curve, `X2_cr`/`X2_eq`)
and §7.7 (`B.1`–`B.12` properties, Faner `hQ`, Chilton–Colburn `hM`). Acceptance: isotherm
monotonic and matches Cardarelli & Crapiste (1996) shape; property correlations reproduce paper
values; `X2_cr`, `X2_eq` finite and ordered.

**M2 — Zone sub-models + integrated steady DT solve.**
Implement PHZ (§7.3), FTRZ with Receding Front + variable cell thickness (§7.5), DCZ dual-scale
(§7.6: 12-layer particle FVM ↔ Rhodes bed integration), and the tray-by-tray fixed-point sweep
(§7.8) with under-relaxation and warm-start. Acceptance: reproduce Coletto (2022) qualitative
profiles — hexane falls ~4000 ppm → ~100 ppm across the DCZ; thin FTRZ (order cm); solid/vapor
temperature and water/hexane profiles match the paper's figures within tolerance.
The nested solver must report inner convergence explicitly, retain complete
profile warm state, remesh DCZ when the endogenous FTRZ boundary moves, and
use safeguarded adaptive damping/continuation for large but equipment-feasible
input changes. These are numerical requirements and must not alter the M2
balance or constitutive equations.

**M3 — Real-time wrap + DC + quality + full I/O.**
Wrap the steady DT solve as the quasi-steady `step()` with transport lag (§7.9); implement the DC
air-contacting stages (§7.10) and the quality kinetics (§7.11, later removed — see DECISIONS.md)
feeding the KPIs; full MV/DV/PV node map; manual/auto per MV with bumpless transfer; setup/runtime lifecycle +
reconfigure; complete UI dashboard; `SolverStress`/undersample handling. Acceptance: real-time
pacing holds at 1× and moderate speed-ups; determinism (free-run) reproduces bit-for-bit;
over/under-processing KPI trends respond to temperature/moisture as literature describes; APC can
close a loop over OPC UA.

**M4 — Optional fidelity rungs (§7.13).**
Richer FTRZ solid-side transfer; fully transient bed scale; VRX-specific trays; higher particle
resolution for validation. Each a strategy swap; no config/interface changes.

Each milestone ships with its tests green and a short `DECISIONS.md` update.

---

## 15. Coding constraints (normative)
- `core/` is pure: no I/O, no threading, no `asyncua`, no wall-clock. Deterministic given inputs.
- SI units everywhere inside the boundary; convert once at config load.
- The assembled `Model` is immutable; constants are bound at setup, never mutated at runtime.
- UI and OPC UA are symmetric adapters over `RuntimeFacade`; neither imports `core/`.
- Zero-order hold on inputs across an integration chunk.
- All state transitions and reconfigurations are logged.
- Fail fast at setup: invalid/unphysical config must prevent assembly with a clear error.

---

## 16. Glossary
- **MV** manipulated variable (controllable). **DV** disturbance variable. **PV** process value
  (measured/output). **CV** controlled variable (a PV the APC regulates).
- **DT / DC** Desolventizer-Toaster / Dryer-Cooler.
- **ZOH** zero-order hold. **Bumpless transfer** mode switch with no actuator step.
- **Cold config** constants frozen at setup. **Hot inputs** MV/DV changed at runtime.
