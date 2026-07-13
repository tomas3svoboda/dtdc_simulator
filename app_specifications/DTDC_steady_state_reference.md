# DTDC steady-state reference (literature)

Companion to `DTDC_Simulator_BuildSpec.md` and `scenarios/soybean_default.yaml`. Grounds this
project's operating defaults and validation expectations in published/typical soybean DT(DC)
operating data, separate from the Coletto (2022) zonal-model figures already cited in the
BuildSpec (those are particle/zone-scale, not whole-tower).

Sources: saVRee "Desolventiser Toaster Drier Cooler (DTDC) Explained"
(https://savree.com/en/encyclopedia/desolventiser-toaster-drier-cooler-dtdc), AOCS "Meal
Desolventizing, Toasting, Drying and Cooling", and patent-literature figures for wet-flake
composition entering the DT (US 4332092, US 4496599 families).

## Whole-tower profile (soybean)

| Point | Temperature | Moisture | Hexane |
|---|---|---|---|
| Feed (wet flakes, ex-extractor) | 55-60 °C | 5-10 % | 25-35 % |
| Pre-desolventizing (jacket side) | steam ~185 °C (10 barg); meal asymptotes toward hexane bp (68.7 °C) | rising | dropping fast |
| Main/countercurrent exit | ~100 °C | 17-22 % | dropping |
| Sparge tray (~70% of total desolv. heat) | — | — | — |
| DT exit (post-sparge) | 105-110 °C | 18-22 % | <500-800 ppm |
| Dryer exit | cooling begins | ~13 % | further reduced |
| Cooler exit (final product) | 35-40 °C | ~11-12.5 % (soybean meal trading limit ~12.5%) | <300-500 ppm |

## This scenario's defaults vs. literature

`scenarios/soybean_default.yaml`'s `disturbance_defaults` (feed conditions, live-adjustable via
the UI sliders / OPC UA DVs):
- `feed_temperature: 330.0` K = 56.85 °C — within the 55-60 °C literature range.
- `feed_moisture: 0.07` (7%) — within the 5-10% range (previously 0.12/12%, revised down).
- `feed_hexane: 0.26` (26%) — within the 25-35% range.

UI sliders intentionally span wider than the literature nominal (10-50% hexane, 5-25% moisture,
40-80 °C feed temp) so upset/off-spec scenarios can be explored, not just the steady operating
point.

## Why the model previously looked "stuck" at high hexane

Two separate issues, both since fixed:

**1. It really did take a long time to settle (pacing, not a bug).**
`core/model.py` (M0 placeholder physics, not yet the Coletto dual-scale zonal model — see its
module docstring and `DECISIONS.md`) chains a first-order-lag relaxation per stage. `base_residence_s
= 90 s` (a documented `ModelParams` field, see the scenario YAML) cascaded across 8 stages settles
over roughly 20-40 *simulated* minutes — in the same ballpark as a real DT's ~20-30 minute
residence time, so not unreasonable physically. But the scenario's old default `sim.speed_factor =
1.0` (real time) meant 20-40 *minutes of wall-clock time* to watch it settle — impractical
interactively. Fix: `sim.speed_factor` default raised to `20.0` (still safely under the
`speed_factor * dt_wall_s <= max_control_interval_s` undersample constraint: `20*0.2=4.0 <=
10.0`), so the full transient plays out in ~60-90 real seconds by default. Drop it back to `1.0`
(or drive the in-app speed slider) for true real-time behavior.

**2. The temperature equilibrium itself *was* wrong — every DT stage was capped at hexane's
boiling point (68.7 °C) forever, even the heaviest-duty sparge tray.** The original per-stage
equilibrium formula treated hexane's boiling point as a hard ceiling regardless of how much steam
duty was applied. Fixed by replacing it with a mechanistic sequential flash/sensible-heat energy
balance (see `DECISIONS.md`, "DT equilibrium replaced with a mechanistic energy balance") driven
only by `dH_vap_hexane`/`dH_vap_water`/`T_boil_hexane`/`cp_solid`/`cp_water_liquid`/`cp_oil` and a
`T_boil_water` solved from the `antoine_water` correlation — no fitted curve or hand-picked
ceiling. At this scenario's duties, the sparge tray now correctly settles around **~100 °C** once
fully desolventized, matching the ~105-110 °C literature DT-exit figure above.

Separately, `gate_opening` — the MV the BuildSpec §5.2 defines as controlling "inter-stage solid
flow / holdup (level)" — had no effect anywhere in the placeholder model before this change. A
real bed-holdup mass balance (`State.M`, `Outputs.stage_level_pct`) was added so gate_opening (and
sweep_arm_speed) now genuinely set both residence time and bed level, and the UI can show a live
per-tray level bar.
