# DTDC Equipment Envelope — literature-derived design bounds

**Status:** Phase 0 deliverable (foundation for the strict OPC UA interface,
the design validator, and the configuration wizard). Machine-readable form:
[`envelope.yaml`](../envelope.yaml).

## Why an envelope exists

Today the OPC UA address space is rebuilt from whatever scenario is assembled
([`interfaces/opcua/server.py`](../src/dtdc_simulator/interfaces/opcua/server.py)),
so its node set changes with every reconfiguration and a client's tag map
breaks. The envelope is a fixed spec of the **largest realistic DTDC**. The
server renders the envelope — not the scenario — into a node tree, so every
possible node exists once at a canonical path and a build only flips each node
**active** vs **placeholder** (present, `StatusCode = Bad_NotConnected`,
`Present = false`). Map once, never remap.

For the caps to be defensible rather than arbitrary, they are taken from the
industrial design literature already in this repo.

## Primary source

Kemper, T.G. (2019). *Solvent Extraction*, Chapter 4 of **Edible Oil
Processing**, 2nd ed. (Hamm, Hamilton & Calliauw, eds.), Wiley. The author is
Global Technical Director, Solvent Extraction, Desmet Ballestra — an OEM
reference. In repo: [`literature_sources/Kemper_Solvent_Extraction.pdf`](../literature_sources/Kemper_Solvent_Extraction.pdf).

Kemper states the vessel tray taxonomy directly:

- **DT** (p.108): "The DT has three different types of tray: predesolventising
  trays, countercurrent trays and a sparge tray."
- **DC** (p.114): "The DC has three different types of tray: steam-drying
  trays, air-drying trays and air-cooling trays."

## Derived caps (per zone)

| Model role | Kemper tray type | Literature range | Envelope cap | Citation |
|---|---|---|---|---|
| `PREDESOLV` | predesolventising | 1–7 | **7** | Kemper p.109: "A DT may have as many as seven predesolventising trays, or as few as one." |
| `MAIN` | countercurrent | 1–4 | **4** | Kemper p.111: "A DT will have from one to four countercurrent trays." |
| `SPARGE` | sparge | 1 | **1** | Kemper p.111 §4.3.3 describes a single sparge tray. |
| `DRYER` | air-drying (DC) | plural; Fig 4.7 shows 2 | **3** | Kemper p.114–115 §4.4.2; Fig 4.7 (2 air trays) + one slot headroom. |
| `COOLER` | air-cooling (DC) | plural | **2** | Kemper p.116 §4.4.3. |

Maximal DT = 7 + 4 + 1 = **12 trays**; maximal modeled DC = 3 + 2 = **5 trays**;
**17 canonical stage slots** total.

Per-zone minima (`min_count`, enforced later by the design validator, not by the
OPC UA superset): PREDESOLV 1, MAIN 1, SPARGE 1, DRYER 0, COOLER 0. The DC is
optional — a DT-only unit is a valid build.

## Cross-checks (independent of Kemper)

- **Paraíso et al. (2008), COAMO plant** — the strongest measured industrial
  dataset, a **seven-stage DT** (S1–S7 temperature profile 90/100/101/108/110/
  115/118 °C). Source: [`literature_sources/Svoboda_Industrial_DTDC_Model_Calibration_Targets.pdf`](../literature_sources/Svoboda_Industrial_DTDC_Model_Calibration_Targets.pdf)
  §1, Table 2. Fits the envelope (e.g. 2 PD + 4 MN + 1 SP, or 3 PD + 3 MN + 1 SP).
- **Coletto et al. (2022)** — the model's own physics base case, a **6-tray**
  industrial DT. Fits (e.g. 3 PD + 2 MN + 1 SP).
- **Current shipped build** — `scenarios/soybean_default.yaml`: 3 PD + 2 MN +
  1 SP + 1 DR + 1 CL. Fits every cap.

No configuration in the repo's literature or scenarios exceeds these caps.

## Decision — DC steam-drying trays are NOT supported in this version

Kemper p.114 §4.4.1: "A DC may have as many as five steam-drying trays, or as
none." These are conductive/jacketed trays (like DT predesolventising trays,
185 °C steam surface) that sit in the DC **above** the air-drying trays. The
current model has **no** steam-drying-tray role — its DC is air-contact only
([`core/dc.py`](../src/dtdc_simulator/core/dc.py)) — so `DRYER` maps to Kemper's
*air-drying* trays.

**Decided (2026-07-24): defer.** This version of the application **does not
support DC steam-drying trays**. The envelope has no `STEAM_DRYER` zone and the
OPC UA superset emits no steam-drying nodes. Reserving nodes for physics the
core cannot populate is speculative; the envelope is versioned (`version: 1`),
so adding the zone later is an explicit, traceable bump (and a one-time client
remap at that point). See `DECISIONS.md` (2026-07-24 entry).

## What the envelope fixes besides zones

Config-independent nodes that are already present in every build, listed in the
envelope for completeness so the whole superset lives in one place:

- **Unit-level actuators:** `feed_flow_rate`, `heated_air_temp`,
  `heated_air_flow`, `ambient_air_flow` (limits from [`engine/facade.py`](../src/dtdc_simulator/engine/facade.py) `MV_LIMITS`).
- **Disturbances (6):** feed temperature/moisture/hexane/oil, ambient air
  temperature, ambient relative humidity.
- **KPIs (13):** residual hexane, meal moisture, steam consumption, throughput,
  exhaust hexane, direct steam, indirect/drying-air/total energy, outlet vapour
  (total/hexane/water), condenser duty.
- **Control-loop superset:** `FIC_DT_FEED`, `FIC_DT_PD_IND_STM`,
  `FIC_DT_MN_IND_STM`, `FIC_DT_DIRECT_STM`, `SIC_DT_SHAFT`, `TIC_DC_DRY_AIR`,
  `FIC_DC_DRY_AIR`, `FIC_DC_COOL_AIR`, and one `ZIC_<boundary>` per controlled
  solids-transfer boundary (canonical boundary list finalized in Phase 1).

## Supporting Kemper design data (for Phase 2 realism rules)

Captured now so the validator's physical-range checks are also cited:

- Indirect (jacket) steam ≈ 10.5 kg/cm² → 185 °C surface (p.109/111/114).
- Direct steam supplied at 10.5 kg/cm², throttled past the sparge control valve
  to 0.35–0.70 kg/cm² / 150–160 °C, sparge surface ≈ 155 °C (p.111 §4.3.3).
- Predesolventising beds 150–300 mm; countercurrent/sparge beds 1000–1200 mm;
  DC drying/cooling beds ≈ 250 mm (p.112, and Svoboda Table 4).
- DT exit ≈ 105–110 °C, 17–21 %wb moisture, 100–500 ppm hexane; dome 66–78 °C
  (typical 71 °C) (p.112–113).
- DC inlet ≈ 108 °C / 18–20 % moisture → product within ~10 °C of ambient,
  ~12.5 % (trading-rule limit) (p.114–116).
- Plant scale: most soybean plants 3000–5000 t/d, some to 20 000 t/d (p.99).

## References

1. Kemper, T.G. (2019). Solvent Extraction. In *Edible Oil Processing*, 2nd ed.,
   Ch. 4. Wiley. `literature_sources/Kemper_Solvent_Extraction.pdf`.
2. Paraíso, P.R., Cauneto, H., Zemp, R.J., Andrade, C.M.G. (2008). Modeling and
   simulation of the soybean oil meal desolventizing-toasting process. *J. Food
   Eng.* 86, 334–341. (via the Svoboda calibration-targets report,
   `literature_sources/Svoboda_Industrial_DTDC_Model_Calibration_Targets.pdf`).
3. Coletto, Bandoni & Blanco (2022). *J. Food Eng.* 318, 110870 (+ supp.).
   `literature_sources/Coletto_*.pdf`.
