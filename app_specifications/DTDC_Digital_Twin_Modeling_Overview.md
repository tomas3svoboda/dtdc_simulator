# DTDC Digital Twin: Overview on Modeling Approach

*A human-readable map of what the simulator models, what it deliberately
abstracts away, and how the pieces are coupled. For the equation-level detail,
each section points at the module that implements it.*

---

## 1. What physical unit are we modeling?

A **DTDC** train — **D**esolventizer–**T**oaster–**D**ryer–**C**ooler — the
finishing end of a solvent (hexane) oilseed-extraction plant. Wet
hexane-laden soybean flakes enter the top; finished meal (low hexane, low
moisture, cooled) leaves the bottom. It is a vertical stack of **trays**, each
a shallow agitated bed the meal cascades down through, one tray at a time.

```
            wet flakes (solid + hexane + water + oil)
                         │  ~57 °C
        ┌────────────────▼────────────────┐
        │  PD1  PREDESOLV  ── indirect steam (jacket)   ▲            │
        │  PD2  PREDESOLV  ── indirect steam            │  vapor     │  DT
   DT   │  MN1  MAIN       ── indirect steam            │  rises     │  (hexane
        │  SP1  SPARGE     ── direct (sparge) steam ────┘  through   │  world)
        └────────────────┬────────────────┘     the DT trays
                         │   ▓▓▓ rotary valve ▓▓▓  ← mechanical seal: solid passes,
        ┌────────────────▼────────────────┐        vapor does NOT cross
   DC   │  DR1  DRYER   ←→ heated air                                │  DC
        │  CL1  COOLER  ←→ ambient air                               │  (air world)
        └────────────────┬────────────────┘
                         ▼
                 product meal (~dry, ~cool)
```

The two halves live in **different gas environments**:

- **DT side** — a closed, hexane-rich **vapor** atmosphere. Its job is to boil
  the solvent out of the flakes (desolventize) and finish-cook (toast) them.
- **DC side** — an open **air** atmosphere. Its job is to dry residual
  moisture and cool the meal to a storable temperature.

They are separated by a **mechanical rotary valve** (an airlock). This matters
physically and in the model: **vapor circulates freely between DT trays, but
no gas crosses the valve** — the DC's air and the DT's hexane vapor never mix.

---

## 2. What domains (scales & phases) are we abstracting?

The model works at **three nested spatial scales**, and only resolves each
where it actually matters:

| Scale | Where it's resolved | Why |
|-------|--------------------|-----|
| **Plant / tower** | Always — the tray-to-tray cascade | The digital-twin output (per-tray T, moisture, hexane, level). |
| **Bed / tray (axial)** | Inside the DT quasi-steady solve (`nz` vertical cells per tray) | Vapor percolates up through the bed depth; the desolventizing zones stack along the tower height. |
| **Particle (intraparticle)** | DCZ only (12 spherical shells) | Near the dry end, diffusion *inside* the flake is the rate-limiting step, so a single lumped particle is not enough. |

And **two fluid phases** per side:

- **Solid phase** (both sides) — the meal: an inert *dry-solid carrier* plus
  the species it carries (below).
- **Gas phase** — **vapor** (water + hexane) in the DT, **humid air** (dry air
  + water vapor) in the DC.

Everything the operator sees per tray is a property of the **solid** phase; the
gas phase is tracked to close the balances and drive the transfer.

---

## 3. Which species do we track?

On the **solid**, everything is a **dry-basis loading** on the inert dry-solid
carrier (kg of species per kg of bone-dry solid):

| Symbol | Species | Role |
|--------|---------|------|
| `X1` | **water** (moisture) | Removed by toasting (DT) and drying (DC); the meal-moisture spec. |
| `X2` | **hexane** (solvent) | Removed in the DT (desolventizing) down to ppm; the residual-solvent spec. |
| `X3` | **oil** | Treated as **fixed** — it stays with the meal, only matters as a heat-capacity / isotherm term. |
| `T` | temperature | Solid temperature (the "tray reads … °C" value). |
| `M` | bed holdup | kg of dry solid currently retained on the tray (drives the level %). |

In the **gas** phase:

- **DT vapor** — characterized by its total mass flow, temperature, and
  `wV2` = the **hexane mass fraction** of the vapor (so `1 − wV2` is water
  vapor).
- **DC air** — characterized by its flow, temperature, and `Y` = the
  **humidity ratio** (kg water vapor per kg dry air). Dry air is inert and
  conserved.

*Not modeled as species:* air's own trace CO₂/N₂ chemistry, protein
denaturation / quality kinetics (deliberately removed — see `DECISIONS.md`),
and any downstream solvent-recovery train.

---

## 4. Main inlet and outlet flows

**Inlets**
- **Feed** (top of DT): wet desolventized flakes — dry solid + `X1` water +
  `X2` hexane + `X3` oil, ~57 °C. A *disturbance* (set by upstream extraction).
- **Indirect steam** (per DT tray): jacket/heating duty in **watts** — heats
  the bed through the tray wall without contacting it. A manipulated variable.
- **Direct (sparge) steam** (bottom SPARGE tray): live steam in **kg/s**
  injected *into* the bed — strips the last hexane and adds moisture/heat.
- **Heated air** (DRYER) and **ambient air** (COOLER): the DC contacting gas
  streams (flow + temperature).

**Outlets**
- **Product meal** (bottom of COOLER): the finished solid — the KPIs (residual
  hexane ppm, meal moisture %, temperature).
- **DT vapor** (top of DT): the boiled-off hexane + water vapor stream, headed
  to solvent recovery (modeled as leaving; its downstream fate is out of scope).
- **DC exhaust air** (per DC tray): the now-warmer/more-humid air.

Dry-solid mass is **conserved** through the whole train (feed dry-solid rate =
product dry-solid rate); only the volatile loadings `X1`/`X2` change.

---

## 5. How is one tray modeled? (the key idea)

Each tray is a **well-mixed 0-D control volume** whose state `(T, X1, X2, M)`
evolves on **two separated timescales**:

**(a) A quasi-steady "target" — where the tray *would* settle.**
Given the current inlets and duties, we compute the tray's steady-state exit
condition:
- **DT trays** — via the **Coletto zonal solve** (`core/dt_solver.py`): the DT
  column is split into three zones by *what physics dominates*, not by tray
  boundaries:
  - **PHZ** (Pre-Heating Zone) — sensible heating + surface hexane flash.
  - **FTRZ** (Flashing & Temperature-Raising Zone) — a receding evaporation
    front; hexane boils, water condenses; free boundary located where the
    solid crosses the critical hexane content.
  - **DCZ** (Diffusion-Controlled Zone) — intraparticle diffusion (the
    12-shell particle FVM), driving residual hexane to ppm.
  This solve is **expensive (~seconds)**, so it is re-run only **periodically**
  (`dt_resolve_interval_s`, ~120 s of sim time), and its per-tray results are
  cached as the targets.
- **DC trays** — via the **air-contact contactor** (`core/dc.py`), cheap, so
  recomputed **every tick** (Section 7).

**(b) A transport lag — how fast the tray actually gets there.**
Every tick (~0.2 s) each state relaxes toward its target with a first-order lag
set by the tray's **residence time** `τ`:

```
φ_new = φ_target + (φ_old − φ_target) · exp(−Δt / τ)      for φ ∈ {T, X1, X2}
M_new = M_target + (M_old − M_target) · exp(−Δt / τ),   M_target = ṁ_in · τ,  ṁ_out = M / τ
```

This is what gives the twin its **dynamics** — a step in steam or feed
propagates down the tower over minutes, not instantly. `τ` itself comes from
the sweep-arm speed and gate opening (Section 6).

The **solid cascades tray-to-tray**: each tray's relaxed exit `(T, X1, X2)` and
outflow `ṁ_out` become the **inlet of the tray below**, top to bottom, through
both the DT and the DC.

> **Why not solve the full model every tick?** One `solve_dt` call costs
> **~3–15 s**; the tick budget is **~0.2 s** — ~50–300× too slow for real-time.
> It's also unnecessary: the DT's steady operating point moves on its ~20–30 min
> residence timescale, so refreshing every ~120 s loses nothing physical, while
> the per-tick lag still captures fast steam/feed steps. The solve's rich
> internal profile is used only to *set* each tray's target, then discarded (kept
> solely for the visualization panel) — the engine carries just the lumped scalar
> between ticks.

> **The DT bed as a continuous column (an idealization).** For FTRZ/DCZ the solve
> stacks the trays' *solid beds* into one continuous counter-current bed (solid
> down, vapor up) and locates the zones by the solid's *state* along that height —
> so a zone can span more than one tray. `bed_height_m` is the **packed-bed depth
> only**; the inter-tray **headspace is not a modeled region** (vapor is assumed to
> pass through the gap unchanged — nothing transforms where there's no solid).
> Each cell is still mapped back to its host tray so it draws that tray's steam
> duty. (PHZ stays discrete — marched tray-by-tray.)

---

## 6. Are we accounting for mixing inside a tray? (the rotating arms)

**Short answer: yes — as a *well-mixed* assumption, justified by the arms;
no — we do not resolve the arms' motion or intra-tray gradients explicitly.**

Each DT tray has central-shaft **sweep/rake arms** rotating relatively fast.
Their real job is twofold, and the model captures **both effects**, without
simulating the mechanics:

1. **Transport toward the discharge gate → sets residence time `τ`.**
   Faster arms push meal to the gate sooner (shorter `τ`); a narrower
   `gate_opening` holds it back (longer `τ`, higher bed level). In
   `Model._stage_tau`:
   ```
   τ  =  base_residence / (rpm / 3) / gate_norm
   ```
   So sweep speed and gate directly set the transport lag and the steady holdup
   `M = ṁ_in · τ`.

2. **Agitation → stronger heat/mass transfer.**
   A mechanically swept bed continuously renews gas–solid contact — a
   *different, stronger transfer regime* than passive flow through a static
   packed bed. This enters the DT transfer coefficient directly
   (`bed_transport_coefficients`):
   ```
   hQ  ←  hQ_packed-bed · (1 + gain · rpm²)
   ```
   (`hM` is then re-derived from the enhanced `hQ` via Chilton–Colburn.)

**What the well-mixed abstraction buys us, and its limits.** Because the arms
homogenize the bed vigorously, treating each tray as a **single lumped,
well-mixed volume** (one `T`, one `X1`, one `X2`) is a reasonable and standard
simplification — that is *precisely* the physical justification for the 0-D
per-tray model. What we therefore do **not** resolve:

- lateral/circumferential temperature or concentration gradients across a tray,
- individual particle trajectories or the discrete "push" of each arm pass,
- back-mixing between trays (flow is one-way, top→bottom).

We *do* resolve, inside the quasi-steady DT map only, the **vertical
(bed-depth) axial profile** (`nz` cells) and **intraparticle diffusion** in the
DCZ (12 shells) — steady spatial *structure*, not a transient mixing model.

> **"Well-mixed" and `nz` aren't in conflict — they're two layers.**
> "Well-mixed per tray" is the *transient* assumption (`model.py`): the state
> carried tick-to-tick is one lumped scalar per tray. `nz` is the mesh of the
> *steady* target solve (`dt_solver.py`), which treats the column as an
> axially-graded counter-current bed (hexane-rich/cool at the top → lean/hot at
> the bottom) — deliberately **not** well-mixed axially, because you must resolve
> that gradient to place the zone transitions. `nz` is a numerical resolution
> knob, **not the tray count** (here `nz_dcz=8`, `nz_ftrz=nz_phz=10` for 4 DT
> trays). The steady solve compresses its `nz`-cell profile into a per-tray
> **exit** value, which the lumped transient tray then chases — so the two
> fidelities meet exactly at the tray boundary.

---

## 7. How is a DC (dryer/cooler) tray modeled?

The DC uses a **first-principles falling-rate air contactor** with a **closed
two-sided mass/energy balance** (`core/dc.py`, following Luz 2010 / Silva
2012). Soybean-meal drying is **entirely falling-rate** (diffusion-limited) —
so the *rate*, not the air's saturation capacity, governs. Per stage:

- **Moisture (solid), a well-mixed CSTR at steady state:**
  ```
  X1_target = (X1_in + K·τ·X_e) / (1 + K·τ)
  ```
  `K` = Luz mass-transfer coefficient (small, ~8.4e-3 /s), `X_e` = Luz
  temperature-dependent equilibrium moisture, `τ` = residence time. Small `K·τ`
  ⇒ only a *fraction* of removable moisture leaves per pass ⇒ meal dries
  gradually and stays warm.
- **Moisture (air), exact mass closure:**
  ```
  Y_out = Y_in + ṁ_evap / ṁ_air,dry      (dry air conserved; humidity carries every evaporated kg)
  ```
- **Energy, a coupled two-phase balance:** the solid temperature comes from a
  Newton-cooling balance convectively coupled (`UA`) to the outlet air, and the
  outlet air temperature is pinned by an **adiabatic total-enthalpy balance**
  `H_in = H_out`. The hot air supplies the evaporation's latent load, so the
  meal settles *warm* rather than crashing to wet-bulb.
- **Hexane:** first-order air-stripping, `X2_target = X2_in · exp(−k · ṁ_air/ṁ_dry)`.

A key emergent, physically-correct result: actively drawing moisture off cools
the meal (latent heat leaves with the vapor). At realistic air:solid ratios the
dryer meal settles **warm (~85 °C), not hot (~105 °C)**; the **cooler**, with a
much larger ambient-air flow, then carries the meal down to ~38–40 °C.

---

## 8. How are the domains coupled? (the whole picture)

```
             ┌─────────────────────── SOLID (cascades top → bottom) ───────────────────────┐
 feed ──▶ PD1 ─(T,X1,X2,ṁ)▶ PD2 ─▶ MN1 ─▶ SP1 ═══(rotary valve, solid only)═══▶ DR1 ─▶ CL1 ─▶ product
             │        │        │       ▲                                         ↕         ↕
   indirect ─┘        │        │       │ direct steam                       heated air  ambient air
   steam (W)          └── indirect ────┘                                      (crossflow, well-mixed,
                                        ▲                                       once-through per stage)
             ┌──── VAPOR (rises bottom → top, counter-current) ────┘
             │   DCZ vapor_out → FTRZ vapor_in ; sparge steam sets the bottom BC
             └──────────────────────────────────────────────────────────────┘
                       ╳ no gas crosses the rotary valve ╳
```

The couplings, concretely:

1. **Solid cascade (both sides):** each tray's exit is the next tray's inlet —
   `T`, `X1`, `X2`, and flow `ṁ`, top to bottom. This is the backbone.
2. **Vapor counter-current (DT only):** vapor rises through the DT trays; in
   the solve, the DCZ vapor outlet feeds the FTRZ vapor inlet, and the sparge
   steam mixes in at the bottom to set the boundary condition. Vapor and solid
   move in **opposite directions** — hot, hexane-lean vapor near the bottom,
   cooler hexane-rich vapor near the top.
3. **Mechanical valve isolation:** the rotary valve passes solid but **blocks
   gas**, so the DC air stream is a completely separate atmosphere — this is why
   the DC needs its *own* air-side balance and never sees hexane vapor.
4. **Air crossflow (DC only):** each DC stage contacts a **once-through** air
   stream (heated air for the dryer, ambient for the cooler). DC stages are
   **not** chained on the air side — each has its own inlet air.
5. **Duties couple in as sources:** indirect steam is a per-tray volumetric heat
   source in the DT energy balance; direct steam is a mass+energy source at the
   sparge tray; air flow/temperature are the DC drivers.
6. **Timescale coupling:** the slow DT zonal re-solve refreshes the DT targets
   every ~120 s; the fast per-tick transport lag moves every tray toward its
   current target; the two run concurrently so the twin stays real-time.

---

## 9. Conservation & sanity checks

The model is built around **closed balances**, verified independently in
`core/balance.py` (`test_balance.py`):

- Each DT zone (PHZ/FTRZ/DCZ) has an independent hexane / water / energy
  residual check, reconstructed from boundary states only.
- Each DC stage closes a **two-sided** water + total-enthalpy balance to
  machine precision.
- A cheap always-on plant-wide **mass-inventory** diagnostic flags anything
  accumulating.

---

### Where to read the code

| Concern | Module |
|---------|--------|
| Tray cascade, transport lag, holdup, KPIs | `core/model.py` |
| DT zonal quasi-steady solve (PHZ/FTRZ/DCZ) | `core/dt_solver.py`, `core/zones/*` |
| Intraparticle diffusion (12-shell FVM) | `core/zones/particle.py` |
| DC air contactor (dryer/cooler) | `core/dc.py` |
| Thermophysical / sorption / drying correlations | `core/thermo.py` |
| Conservation checks | `core/balance.py` |
| Design-decision history & rationale | `DECISIONS.md` |
