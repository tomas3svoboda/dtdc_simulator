# Equation Grounding Matrix

**Purpose.** Verify every governing equation in the physical core against its cited
source, so we know precisely what is a faithful transcription vs. a deviation vs. an
unrecoverable placeholder — *before* recalibrating any constant. This exists because the
convergence investigation found that the DCZ energy balance produces a physically wrong
(over-heating) converged solution, and the cause turned out to be **equation-level
deviations from the paper**, not just mis-valued constants.

**Method.** Each equation was cross-checked against the extracted PDF text of the primary
sources (not against the code's own docstrings, which are what we're auditing). The
Coletto main-paper appendix (A.1–A.37, B.1–B.12) is text-extractable; the Coletto
*supplementary* material's algorithm figures are images (not extractable), but its
**Table 1 of parameters IS extractable** and provides many numeric values.

**Legend**
| Tag | Meaning |
|---|---|
| ✅ PAPER | Faithful transcription of a cited equation/value |
| ⚠️ DEVIATION | Differs from the cited paper (whether or not documented in-code) |
| 🔵 DERIVED | Standard result not in the paper, filling a genuine gap |
| 🟡 PLACE | Placeholder value, genuinely unrecoverable (theses we don't have) |
| ➕ ADDITION | Project extension beyond Coletto (water balance, DC) — judged vs *its own* source |

---

## 0. Actionable findings (ranked by impact)

| # | Finding | Verdict | Impact |
|---|---|---|---|
| **D1** | **DCZ bed-scale vapor energy source (eq. A.34).** Code ([dcz.py](src/dtdc_simulator/core/zones/dcz.py)) substituted `−sorption_sink·α_L` (particle's full heat of sorption) for the paper's `SVm2·Ĥ2` mass-enthalpy term. | ✅ **RESOLVED** | Coletto's DCZ bed energy balance is **temperature-based** (A.25: `∇·(αV·ρV·CPV·TV·uV)=SVQ`), *not* enthalpy-based (FTRZ is; DCZ isn't — my earlier claim was wrong). Restored the paper's `SVm2·Ĥ2` with `Ĥ2` as the datum-consistent **sensible** hexane enthalpy `cp_hex·(Tp12−TV)` (the bed transfer is vapor→vapor; the phase change is at the particle scale A.30). Latent-laden `Ĥ2` was tested → 187 °C runaway. Result: physical (meal 109 °C / 226 ppm at COAMO), no overheating, deviation removed. |
| **D2** | **Particle sorption source sign (eq. A.30).** Paper prints `S_Q = −α_ps ρ_ps (∂W2/∂t) ΔĤ_s − …`; code flips to `+`. | ✅ **RESOLVED — paper typo** | Implementing the literal paper sign (with the exact `SVm2·Ĥ2`) was tested directly → **187 °C meal runaway**: the printed sign makes desorption *exothermic*, backwards from standard sorption thermodynamics (and Cardarelli 1996). The code's flip (S_Q has the same sign as ∂W2/∂t → desorption cools) is the physically-correct sign and is kept, now documented as a confirmed paper sign error. |
| **D3** | **Heat-transfer correlation (eq. B.7).** Code claimed it "unrecoverable" and substituted Ranz–Marshall; B.7 *is* printed in the paper. | ✅ **RESOLVED** | Restored `Nuε = 0.6949·Reε^0.579·Pr^⅓` exactly ([thermo.py:333](src/dtdc_simulator/core/thermo.py#L333)). Scorecard effect **negligible** (meal Δ0.01 °C) — because D4's `sweep_arm_transfer_gain` dominates hQ and masks the base correlation entirely. That masking is the real story → see D4. |
| **D4** | **`sweep_arm_transfer_gain`.** `hQ *= (1 + gain·rpm²)` ([dt_solver.py:245](src/dtdc_simulator/core/dt_solver.py#L245)), default 1.0 → **10× hQ** at 3 rpm. No such term in Coletto. | ✅ **RESOLVED — removed** | Confirmed a **D1-bug crutch**: with D1 fixed, setting gain=0 (pure B.7 hQ) barely moves the DT (meal 109.0→109.5 °C, residual 225→213 ppm) — the 10× enhancement it provided was compensating for the overheating energy balance, not real physics. Set to 0 (Coletto-faithful). The DCZ temperature/residual are diffusion-limited (D_eff, r_P), not hQ/hM-limited, so the transport-coefficient magnitude barely matters here. Code term left inert (gain=0); candidate for full removal in cleanup. |
| **D5** | **Supplementary Table 1 values.** | ✅ **VERIFIED faithful** | Config already matches Table 1 for every applicable value (D_eff, D_ax, D_HW, cp_solid, cp_vapor, mu_vapor, k_ps, k_pg, k_mixL, rho_ps, alpha_ps, A0, B). Builder hardcodes nothing physical but atm pressure. **Exception:** `particle_radius = 0.2 mm` vs cited **1 mm** — reduced 5× as a hand-tuned calibration knob (1 mm → ~12000 ppm); a `[DERIVED]`-tagged value that is really calibration. Flag for Phase 3/4. |
| **D6** | **`Reε` and `aV` definitions.** `Reε = ρ_V u_s (2r_P)/(μ_V ε)` and `aV = 3(1−ε_b)/r_P` ([dt_solver.py:240](src/dtdc_simulator/core/dt_solver.py#L240)). | 🔵 DERIVED | Genuinely absent from the paper (only B.7 *uses* Reε; aV cited to Rhodes textbook). Standard packed-bed forms; legitimately derived. Keep, but tag honestly. |

**Bottom line for the plan:** we are (at least partly) in the "equations are wrong" world,
not the "just recalibrate constants" world. **D1 must be fixed before any convergence
acceleration or constant recalibration.** D2/D3/D4 are entangled with it.

---

## 1. Isotherms & thermodynamics — `thermo.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| `gab_hexane_content` | GAB W2(a_h,T) | Cardarelli & Crapiste 1996 (have PDF) | ✅ PAPER (form) | GAB rational form correct; params (Xm,C0,dHC_R,K0,dHK_R) — verify values vs 1996 paper |
| `gab_hexane_content_and_slope` | analytic dW2/da_h | — | ✅ (exact derivative) | Our own analytic derivative; matches FD to 1e-9 (speedup work) |
| `_gab_clamp_activity` | `K·a_h ≤ 0.999` divergence guard | — | 🔵 GUARD (2026-07-22) | clamps at the GAB multilayer divergence instead of raising; engages only for a cold off-design transient, never at a calibrated point |
| `oil_hexane_content` | `qo = A0·a_h^B` (eq. 7) | Cardarelli & Crapiste 1996 | ✅ PAPER | **A0=0.9635, B=2.7036 given in supplementary Table 1** — verify code/config match |
| `heat_of_sorption` | `ΔH_s = ΔH_lv2 + C0·W2^C1` (eq. A.31) | Coletto A.31 | ✅ PAPER (form) | C0,C1 **not** in supp Table 1 → 🟡 PLACE (Cardarelli 1998 / Faner 2008 theses) |
| `x2_critical` | eq. 4 `(α_pg ρ_hexL)/(α_ps ρ_ps)` | Coletto eq. 4 | ✅ PAPER | but default overridden by empirical Faner-2019 X_c≈0.20 → ⚠️ DEVIATION (documented choice) |
| `x2_equilibrium` | eq. 5/6 (pores saturated w/ gas hexane, a_h=1) | Coletto eq. 5–6 | ✅ PAPER | |
| `antoine_pressure_pa` | `log10(P)=A−B/(C+T)` | standard | ✅ standard | |
| `dew_point_temperature` | inverse Antoine (Raoult, water) | standard | ✅ standard | now closed-form (speedup work), was brentq |
| `rho_hexane_liquid` | eq. B.11–B.12 (Daubert & Danner) | Coletto B.11–B.12 | ✅ PAPER | |
| `luikov_equilibrium_moisture` | water sorption isotherm | Gianini (have PDF) | ➕ ADDITION | Not in Coletto (hexane-only); for the DC/direct-steam water balance |

## 2. Bed transport coefficients — `thermo.py` + `dt_solver.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| `nu_from_reynolds` | `2 + 0.6·Re^0.5·Pr^⅓` (Ranz–Marshall) | Faner 2019 eq. 11 | ⚠️ **D3** | Paper's B.7 (`0.6949·Reε^0.579·Pr^⅓`) is available and *not* used |
| `hq_from_nu` | eq. B.8 `Nuε = 2 hQ rP/kV · αV/αL` | Coletto B.8 | ✅ PAPER | |
| `hm_from_hq` | eq. B.9 Chilton–Colburn | Coletto B.9 | ✅ PAPER | |
| `schmidt_number` | eq. B.10 `μV/(ρV D_HW)` | Coletto B.10 | ✅ PAPER | |
| `bed_transport_coefficients` `aV`,`Reε` | packed-sphere forms | Rhodes 2008 (textbook) | 🔵 **D6** DERIVED | genuinely absent from paper |
| `bed_transport_coefficients` `hQ *= 1+gain·rpm²` | sweep-arm enhancement | — | ➕/🟡 **D4** | invention, load-bearing on tuning |

## 3. Pre-Heating Zone — `phz.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| `solve_phz_cell` (sensible heat → isothermal hexane evap) | eq. A.1a branch | Coletto §2.2 / A.1 | ✅ PAPER | solid energy balance faithful |
| mixture cp (solid only) | eq. B.5 (partial) | Coletto B.5 | ⚠️ minor | neglects interstitial-vapor cp (documented, tiny) |
| `VAPOR_SOLID_CONTACT_FRACTION = 0.3` | vapor T update | — | 🟡 PLACE | not from paper (no PHZ vapor closure given); solid profile independent of it |

## 4. Flashing & Temperature-Raising Zone — `ftrz.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| `wet_core_fraction` | Receding-Front `w_h=(r_fr/r_P)^3` (eq. 3) | Coletto eq. 3 | ✅ PAPER | |
| `solid_temperature` | `T_L = w_h T_bh + (1−w_h) T_V` (eq. A.17) | Coletto A.17 | ✅ PAPER | |
| uniform hexane removal per cell | eq. A.6 | Coletto A.6 / §2.3.5 | ✅ PAPER | |
| V-SCAL/V-SAT dew-curve tracking | §2.3.4 / A.2.3 | Coletto | ✅ PAPER (structure) | |
| V-SCAL T-evolution mixing | — | — | ⚠️ | no explicit paper formula; documented construction |
| `cell_thickness_m` (eq. A.18) `J_Q,cs` units | eq. A.18–A.20 | Coletto | ⚠️ DEVIATION (units) | documented "not a verified-exact transcription" |
| `x2_critical_empirical` | Faner 2019 X_c≈0.20 | Faner 2019 (have PDF) | ⚠️ choice | overrides eq. 4 (documented) |
| water surface sorption `Xe(a_w(T_L))` | — | Gianini 2006 + A&G/Kemper | ➕ ADDITION (2026-07-22) | condensation keyed to the SOLID surface (not bulk vapor); the DT moisture rise, absent from Coletto's hexane-only DT |
| evaporative pinning `T_L=min(A.17, T_dew)` | extends eq. A.17 for water | A&G/Kemper/Paraíso | ➕ ADDITION (2026-07-22) | a wet surface can't superheat past `T_sat,water`; A.17 is the hexane-only closure |
| binary-VLE water floor `p_w ≥ a_w·p_sat` | Raoult (immiscible) + Luikov `a_w` | standard + Gianini | ➕ ADDITION (2026-07-22) | vapor never pure-hexane; SLACK at design (Xe limits first), clamped vs `p_sat→P` |

## 5. DCZ particle scale — `particle.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| `march_particle_mass` | eq. A.22 (X2,total-conservative FVM) | Coletto A.22 | ✅ PAPER | reworked to A.22 direct form (mass-conservation fix); tridiagonal solve is ours |
| `_accumulation_jacobian_per_layer` | `Ca`/M_i (eq. A.28) | Coletto A.28 | ✅ PAPER | |
| `march_particle_energy` | eq. A.23 (radial energy FVM) | Coletto A.23 | ✅ PAPER | convective BC + source |
| `sorption_heat_source_per_layer_w_m3` | eq. A.30 | Coletto A.30 | ⚠️ **D2** | **sign flipped** from paper |
| `heat_of_sorption` use, W2 floor | eq. A.31 + 2%·Xm floor | Coletto A.31 | ✅ PAPER + 🔵 floor | floor is ours (low-coverage singularity guard) |

## 6. DCZ bed scale — `dcz.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| bed mass balance (implicit per-cell) | eq. A.24/A.33/A.35–36 | Coletto | ✅ PAPER (structure) | implicit relaxation is a documented stable-scheme choice |
| bed energy balance convective term | `−a_v α_L J_QR` (eq. A.34 term 1) | Coletto A.34 | ✅ PAPER | `κ_e = hQ aV αL` |
| bed energy **mass-enthalpy** source | `S_Vm2·Ĥ2 + ṁ'_ax·Ĥ2` (eq. A.34) | Coletto A.34 | ⚠️ **D1** | **replaced by `−sorption_sink·αL`** — the over-heating cause |
| `q̇_Iv` indirect | eq. A.34 term 3 | Coletto A.34 | ✅ PAPER | |
| axial dispersion/conduction (Laplacian, lagged) | eq. A.32/A.36 | Coletto | ✅ PAPER (structure) | 1-iter lag is a documented Picard choice |
| water condensation + Luikov sorption branch | — | Gianini + ours | ➕ ADDITION | not in Coletto (hexane-only DCZ); for direct steam |
| `kappa_w = 15 D_water/r_P²` (LDF) | Glueckauf LDF | standard | 🔵 DERIVED | water intraparticle rate |
| evaporative pinning (cap T at `T_sat` while wet) | extends A.25 for water | A&G/Kemper | ➕ ADDITION (2026-07-22) | holds toasting meal ~112 °C / 19 %wb instead of a 123 °C runaway that dried it out |
| water-budget pre-count (double-draw fix) | mass conservation | — | ➕ ADDITION (2026-07-22) | shares one condensation+adsorption budget so the meal can't gain more water than the vapor supplies; no-op at the calibrated point |
| particle-`Tp` clamp 250–480 K | robustness | — | 🔵 GUARD (2026-07-22) | off-design only; graceful degradation, never engages at a calibrated point |

## 7. Dryer–Cooler — `dc.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| `air_contact_equilibrium` (0-D well-mixed, NTU) | drying falling-rate | Luz / Silva (have PDFs) | ➕ ADDITION | **Entirely outside Coletto** (DT-only paper). Judge vs its own sources + COAMO/Kemper DC targets |
| `desorb_hexane` | GAB escaping-tendency + EPA AP-42 anchor | Cardarelli GAB + EPA | ➕ ADDITION | DC hexane = "weak polishing term" per calibration report |

## 8. Integration & solver — `dt_solver.py`, `model.py`

| Code | Implements | Source | Verdict | Notes |
|---|---|---|---|---|
| PHZ-once + FTRZ↔DCZ Gauss-Seidel | Fig. 5 tray sweep | Coletto supp Fig. 5 | ✅ PAPER (structure) | our two-variable reduction is a documented, faithful simplification |
| DCZ "Primary Internal Loop" | supp Fig. 3 | Coletto supp Fig. 3 | ✅ PAPER (structure) | **but does not converge** (ρ≈0.9998) — the separate rigor issue |
| two-timescale lag relaxation (`exp(−dt/τ)`) | §7.9 transport lag | Coletto §7.9 | ✅ (engineering) | our real-time wrapper |

---

## Source availability

| Source | Have? | Provides |
|---|---|---|
| Coletto main (JFE 2022) | ✅ | A.1–A.37, B.1–B.12, eqs 1–7 |
| Coletto supplementary | ✅ (figures as images) | algorithm figures (not extractable); **Table 1 parameter values (extractable)** |
| Cardarelli & Crapiste 1996 (JAOCS) | ✅ | GAB params, heat-of-sorption form |
| Cardarelli et al. 2002 (JFE) | ✅ | DCZ dual-scale basis |
| Faner et al. 2019 (JFPE) | ✅ | Ranz–Marshall use, X_c≈0.20 |
| Gianini (soybean water isotherm) | ✅ | Luikov params |
| **Cardarelli 1998** — *Modelado del proceso de desolventizado de harinas vegetales*, PhD thesis, Universidad Nacional del Sur (UNS), Bahía Blanca, AR | ❌ | **C0, C1 sorption-heat constants** |
| **Faner 2008** — *Desolventizado de harinas oleaginosas con vapor sobrecalentado*, PhD thesis, Universidad Nacional del Sur (UNS), Bahía Blanca, AR | ❌ | B.7 experimental basis (formula itself is in Coletto) |

Neither thesis is strictly required to *correct* the deviations (D1–D3 are recoverable from
the equations we have). They would only pin the genuinely-placeheld C0/C1 sorption-heat
constants (D-tier), which otherwise become calibration targets.
