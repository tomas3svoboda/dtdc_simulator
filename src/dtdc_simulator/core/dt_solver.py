"""Integrated DT (PHZ + FTRZ + DCZ) solve — Coletto, Bandoni & Blanco (2022)
§7.8/Fig. 5 (BuildSpec), the last slice of M2 (BuildSpec §14): "the
tray-by-tray fixed-point sweep with under-relaxation and warm-start."
Standalone, pure, unit-tested — not yet wired into `core/model.py`'s
real-time `step()` (that is M3, BuildSpec §14: transport lag, per-tick
warm-starting from engine state).

KEY DESIGN INSIGHT (found while tracing the actual data dependencies between
`zones/phz.py`, `zones/ftrz.py`, `zones/dcz.py`'s own signatures, not assumed
upfront): PHZ's solid solve depends only on `Q_indirect` and the top feed BC
-- NOT on vapor state (already true of the standalone `phz.py`: vapor is
informational-only there). So PHZ is solved ONCE, up front and is capped by
the physical PREDESOLV hardware boundary. Crossing the receding-front critical
content changes the jacket-driven mechanism *inside* that section; it never
moves direct-steam FTRZ into a sealed PREDESOLV tray. Cold, boiling-point, or
superheated PHZ outlet states are handed to FTRZ at the first countercurrent
tray; no outer-loop dependency is introduced. The *only* two coupling
scalars across the remaining FTRZ/DCZ interface are `DCZZoneResult.vapor_out`
(top face) feeding FTRZ's `vapor_in` (bottom BC), and
`FTRZZoneResult.solid_out.T` (bottom-most cell) feeding DCZ's `T_L_sup`. This
collapses Fig. 5's "tray-by-tray fixed-point sweep" to a two-variable
Gauss-Seidel loop, spanning trays rather than iterating within each one --
simpler than a literal reading might suggest, and still faithful to it
("solve FTRZ before DCZ... repeat until profiles converge").

BOUNDARIES:
- PHZ/FTRZ interface (`L_PHZ`): fixed at the bottom of the last contiguous
  PREDESOLV tray. `X2,cr` is an internal PHZ mechanism switch (constant-rate
  to jacket-driven falling-rate evaporation), not a steam-contact boundary.
- FTRZ/DCZ interface (`L_FTRZ`): solved endogenously by
  `solve_ftrz_zone`. Every resolved outer pass rebuilds the DCZ mesh at that
  current physical boundary and interpolates the complete lagged DCZ state
  onto the new cell centres. Extreme operation therefore cannot retain a
  first-iterate geometry.

DUTY APPORTIONMENT (bed height as a genuine spatial domain across trays, not
one lumped quantity): each real tray's own `Q_indirect_w` is a *total*
wattage (BuildSpec §5.1). Every zone that occupies part of a tray's height
draws the *same* uniform volumetric density `q_Iv = Q_indirect_w/(A_bed*
tray_height)` as the rest of that tray (eq. A.2a's own convention, applied
consistently instead of inventing a second quantity) -- this is what RESOLVES
two prior gaps left open at M2 Phases 2-3:
- FTRZ's own `Q_cond_w` (no formula in the paper, only a qualitative
  description) is now literally the host tray's `q_Iv`.
- DCZ's own `q_Iv_w_m3` (a single scalar in Phase 3, smearing every tray DCZ
  spans into one artificial average) now accepts a per-cell profile
  (`zones/dcz.py`, this phase), built here by mapping each DCZ cell's
  z-position to whichever real tray contains it.
Neither zone's duty is double-counted: a tray split between FTRZ and DCZ
contributes disjoint sub-heights to each, both scaled from the same
uniform density.

BED TRANSPORT COEFFICIENTS (`hQ`, `hM`, `aV`) -- LITERATURE GAP, confirmed
absent (not assumed) by a targeted re-search of every PDF in
`literature_sources/`, including the supplementary material: Coletto's own
Nuε-Reε correlation (eq. B.7) is cited to Faner's unpublished 2008 PhD
thesis, and `aV` (specific interfacial area, eq. A.35) to Rhodes (2008), a
textbook -- neither source, nor a defining formula for either quantity,
appears anywhere in what we have. Closure, confirmed with the user: standard
packed-bed correlations, clearly tagged `[DERIVED]` like every
other genuinely-unrecoverable constant already in this codebase --
```
aV    = 3*(1 - eps_b) / rP                                  [DERIVED, packed spheres]
Reeps = rho_V * uV,superficial * (2*rP) / (mu_V * eps_b)     [DERIVED, voidage-corrected Re]
PrV   = cp_V * mu_V / kV                                     [STD]
```
feeding the EXISTING, unchanged `thermo.nu_from_reynolds`/`hq_from_nu`/
`schmidt_number`/`hm_from_hq` chain (eqs. B.7-B.10). `kV` (bulk interstitial
vapor thermal conductivity) is itself not a field anywhere in the existing
config -- reused here from `ParticleConstants.k_pg` (the pore-gas thermal
conductivity is the same hexane/steam vapor mixture, just resolved at the
particle-pore scale rather than the bed-interstitial scale; a documented,
cheap approximation rather than inventing an unrelated number).

CROSS-CHECKED, NOT UPGRADED (2026-07-14): two further Faner publications were
tracked down and read after this closure shipped (a 2019 J. Food Process Eng.
journal article and a 2006 conference paper) -- neither is the actual 2008
thesis Coletto cites, and neither contains eq. B.7's 0.6949/0.579
coefficients or a Rhodes-type `aV` derivation. A separate, independent
reconstruction attempt (reasoning from Rhodes' own superficial-vs-interstitial
velocity convention and standard packed-bed pressure-drop correlations, not a
transcription of either source) converged on the SAME two formulas already
implemented here (`Reε = ρ·us·dp/(μ·ε)`, its own top-ranked candidate; `aV =
6(1-ε)/dp`, algebraically identical to `3(1-ε)/rP`) -- a reassuring second
line of reasoning, not a primary-source verification. Still tagged
`[DERIVED]`, not upgraded to `[PAPER]`: that reconstruction's own
stated confidence tops out at "moderate" for `Reε`, and it does not resolve
`sorption_C0`/`sorption_C1` (still unrecoverable) at all.

INTRAPARTICLE VOLUME -- deliberately DCZ-only. PHZ (sensible-heat/surface
-flash limited) and FTRZ (Receding Front explicitly neglects dry-shell mass
-transfer resistance, §2.3) are lumped-particle by Coletto's own zone
philosophy (§7.0); DCZ alone resolves the 12-layer spherical FVM
(`zones/particle.py`) because it is the one zone where intraparticle
diffusion is rate-limiting. No change needed to make this true -- it already
falls out of composing the three zone modules as published.

SPARGE (direct steam) BC -- scoped to the reference geometry: the SPARGE tray
is the bottommost DT tray, so direct steam sets DCZ's own bottom boundary
condition (`vapor_inf`) via a mass/energy mix with whatever "clean" vapor
arrives from below the DT (documented boundary assumption: direct steam
enters as saturated steam at `T_boil_water`, mixed by a mass-weighted
temperature average against the incoming clean vapor -- both streams are
overwhelmingly water vapor at this point, so a shared `cp_V` is a reasonable
simplification, not derived from the paper). `solve_dt` raises if direct
steam is configured on any tray other than the last -- a genuine point-source
term mid-domain is future work, not implemented here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from scipy.optimize import brentq

from dtdc_simulator.core import thermo
from dtdc_simulator.core.zones import dcz
from dtdc_simulator.core.zones import ftrz
from dtdc_simulator.core.zones import particle as pt
from dtdc_simulator.core.zones import phz as phz_mod

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DTTray:
    """One real DT tray (PREDESOLV/MAIN/SPARGE), top-to-bottom solid order."""

    id: str
    role: str  # "PREDESOLV" | "MAIN" | "SPARGE"
    diameter_m: float
    bed_height_m: float
    Q_indirect_w: float
    direct_steam_kg_s: float = 0.0  # SPARGE only; must be the LAST tray if nonzero


@dataclass(frozen=True)
class SolidFeed:
    T: float  # K
    X1: float  # moisture, kg/kg dry solid
    X2: float  # hexane, kg/kg dry solid
    X3: float  # oil, kg/kg dry solid
    m_dry_kg_s: float


@dataclass(frozen=True)
class VaporFeed:
    """ "Clean" vapor arriving at the very bottom of the DT (from below the
    SPARGE tray, e.g. the rotary lock / DC side), BEFORE direct steam mixes
    in -- see module docstring on the sparge BC."""

    m_water_kg_s: float
    m_hex_kg_s: float
    T: float  # K


@dataclass(frozen=True)
class DTSolverConstants:
    phz: phz_mod.PHZConstants
    ftrz: ftrz.FTRZConstants  # carries T_boil_hexane/T_boil_water/bed_porosity/gab/oil/rho_ps/...
    particle: pt.ParticleConstants  # carries D_eff/r_P/Np/alpha_ps/alpha_pg/rho_ps/k_ps/k_pg/...
    D_ax: float  # m2/s, axial dispersion (DCZ hexane balance, eq. A.36)
    k_mixL: float  # W/(m K), bed-scale solid/gas mixture conductivity (eq. A.32)
    rho_V: float  # kg/m3, vapor reference density
    cp_V: float  # J/(kg K), vapor specific heat
    mu_V: float  # Pa.s, vapor viscosity
    D_HW: float  # m2/s, hexane-water diffusivity (Schmidt number, eq. B.10)
    T_direct_steam: float  # K, SPARGE steam's own supply temperature (NOT ftrz.T_boil_water,
    # which is water's atmospheric bp -- used for the DT's OWN internal vapor space, a
    # different quantity from what supply steam itself arrives at; see the sparge BC below)
    sweep_arm_transfer_gain: float  # -, hQ/hM sensitivity to sweep-arm agitation, see
    # bed_transport_coefficients' own docstring
    luikov: thermo.LuikovParams  # water sorption/desorption isotherm, DCZ's subsaturated regime
    water_diffusivity: float  # m2/s, water's own intraparticle diffusivity (Touffet et al. 2026)
    # -- NOT hM (hexane-vapor-tuned, ~25-100x too fast once coupled to an energy balance, see
    # zones/dcz.py's own docstring for the confirmed inversion and why diffusivity, not a
    # convective coefficient, is the right basis)
    antoine_hexane: thermo.AntoineParams  # hexane saturation pressure -- for the DT dome/vapor
    # temperature (binary hexane-water dew point of the exiting vapor, see the axial-profile build)
    pressure_pa: float = thermo.ATM_PRESSURE_PA  # lower-DT internal operating pressure for the
    # water dew-point / activity calc (FTRZ + DCZ). Above atmospheric per the sparge-tray
    # pressure drop (Kemper 2019, 0.35-0.70 kg/cm2). The DOME stays at atmospheric (its own
    # binary dew point, `_binary_dew_T`, is unchanged). Default atmospheric.

    @property
    def k_V(self) -> float:
        """Bulk interstitial vapor thermal conductivity -- reused from the
        particle pore-gas conductivity (same gas, different scale); see
        module docstring."""
        return self.particle.k_pg


# ---------------------------------------------------------------------------
# Bed transport coefficients (hQ, hM, aV) -- see module docstring
# ---------------------------------------------------------------------------


def bed_transport_coefficients(
    u_V_superficial_m_s: float, c: DTSolverConstants, sweep_arm_rpm: float = 0.0
) -> tuple[float, float, float]:
    """(hQ, hM, aV) from mean bed/vapor conditions -- Faner Nu-Re correlation
    (eq. B.7-B.9), closed with the standard packed-bed `aV`/`Reeps` formulas
    documented in this module's docstring (a confirmed literature gap, not an
    assumed one).

    The optional sweep-arm multiplier is retained for compatibility and
    controlled experiments, but the authoritative scenario sets its gain to
    zero. The release model therefore uses Coletto B.7-B.9 without an
    empirical agitation enhancement.
    """
    r_P = c.particle.r_P
    eps_b = c.ftrz.bed_porosity
    alpha_L = 1.0 - eps_b
    aV = 3.0 * (1.0 - eps_b) / r_P
    Re_eps = c.rho_V * u_V_superficial_m_s * (2.0 * r_P) / (c.mu_V * eps_b)
    Pr_V = c.cp_V * c.mu_V / c.k_V
    Nu_eps = thermo.nu_from_reynolds(Re_eps, Pr_V)
    hQ = thermo.hq_from_nu(Nu_eps, r_P, c.k_V, alpha_V=eps_b, alpha_L=alpha_L)
    hQ *= 1.0 + c.sweep_arm_transfer_gain * sweep_arm_rpm**2
    Sc = thermo.schmidt_number(c.mu_V, c.rho_V, c.D_HW)
    hM = thermo.hm_from_hq(hQ, c.rho_V, c.cp_V, Pr_V, Sc)
    return hQ, hM, aV


# ---------------------------------------------------------------------------
# PHZ pass (solved once, up front -- see module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PHZPassResult:
    tray_results: tuple[phz_mod.PHZTrayResult, ...]  # fully-PHZ trays, top to bottom
    boundary_tray_index: int  # index into the caller's `trays` list
    z_star_m: float  # PHZ's own share of the boundary tray's height
    boundary_tray_result: phz_mod.PHZTrayResult  # the boundary tray's own PHZ portion
    # (height z_star_m), kept for the axial profile (DTAxialProfile) -- everything else here
    # already only needed the boundary tray's own EXIT state (`exit_state` below), but the
    # profile needs its per-cell interior too.
    # At T_boil_hexane; X2 may exceed X2,cr at the PREDESOLV hardware boundary.
    exit_state: phz_mod.SolidState
    L_PHZ_m: float


def _phz_pass(
    trays: list[DTTray],
    solid_feed: SolidFeed,
    vapor_hint: phz_mod.VaporState,
    m_vapor_water_kg_s: float,
    nz_phz: int,
    c: DTSolverConstants,
) -> PHZPassResult:
    predesolv_indices = [i for i, tray in enumerate(trays) if tray.role == "PREDESOLV"]
    if not predesolv_indices:
        raise ValueError("PHZ requires at least one PREDESOLV tray")
    expected_prefix = list(range(predesolv_indices[-1] + 1))
    if predesolv_indices != expected_prefix:
        raise ValueError("PREDESOLV trays must form a contiguous prefix at the top of the DT")

    solid = phz_mod.SolidState(T=solid_feed.T, X2=solid_feed.X2)
    completed: list[phz_mod.PHZTrayResult] = []

    def _x2_cr(T: float) -> float:
        return thermo.x2_critical(
            c.particle.alpha_pg,
            thermo.rho_hexane_liquid(T),
            c.particle.alpha_ps,
            c.particle.rho_ps,
            empirical=c.particle.x2_critical_empirical,
        )

    x2_cr = _x2_cr(c.phz.T_boil_hexane)
    x2_eq = thermo.x2_equilibrium(
        max(vapor_hint.T, c.phz.T_boil_hexane),
        solid_feed.X3,
        c.ftrz.gab,
        c.ftrz.oil,
        c.particle.alpha_pg,
        c.particle.alpha_ps,
        c.particle.rho_ps,
    )

    for idx in predesolv_indices:
        tray = trays[idx]
        result = phz_mod.solve_phz_tray(
            nz_phz,
            tray.bed_height_m,
            tray.diameter_m,
            tray.Q_indirect_w,
            solid,
            vapor_hint,
            solid_feed.m_dry_kg_s,
            m_vapor_water_kg_s,
            solid_feed.X1,
            solid_feed.X3,
            c.phz,
            X2_critical=x2_cr,
            X2_equilibrium=x2_eq,
        )
        completed.append(result)
        solid = result.solid_out

    # Hardware boundary: direct steam cannot contact the sealed PREDESOLV
    # beds, so FTRZ always begins at the first countercurrent tray.  Reaching
    # X2_cr inside PREDESOLV now changes the local jacket-driven mechanism
    # (constant -> falling rate); it does not move the hardware handoff.
    last_idx = predesolv_indices[-1]
    last_tray = trays[last_idx]
    last_result = completed.pop()
    return PHZPassResult(
        tray_results=tuple(completed),
        boundary_tray_index=last_idx,
        z_star_m=last_tray.bed_height_m,
        boundary_tray_result=last_result,
        exit_state=last_result.solid_out,
        L_PHZ_m=sum(trays[i].bed_height_m for i in predesolv_indices),
    )


# ---------------------------------------------------------------------------
# DCZ domain assembly (per-tray duty apportionment -- see module docstring)
# ---------------------------------------------------------------------------


def _tray_q_Iv(tray: DTTray, A_bed_m2: float) -> float:
    return tray.Q_indirect_w / (A_bed_m2 * tray.bed_height_m) if tray.bed_height_m > 0.0 else 0.0


def _average_q_Iv_over_depth(trays: list[DTTray], depth_m: float, A_bed_m2: float) -> float:
    """Length/volume-average jacket heat density over a zone from its top.

    This lets the thin FTRZ cross a real-tray boundary without inheriting the
    first tray's duty density over its entire length.  The FTRZ free-boundary
    iteration calls it with its current length guess.
    """
    if depth_m <= 0.0:
        return _tray_q_Iv(trays[0], A_bed_m2)
    remaining_m = depth_m
    integral_w_m2 = 0.0
    covered_m = 0.0
    for tray in trays:
        share_m = min(max(remaining_m, 0.0), tray.bed_height_m)
        integral_w_m2 += _tray_q_Iv(tray, A_bed_m2) * share_m
        covered_m += share_m
        remaining_m -= share_m
        if remaining_m <= 1.0e-12:
            break
    if remaining_m > 1.0e-9:
        raise ValueError(
            f"FTRZ length ({depth_m:.4f} m) exceeds the remaining DT bed height ({covered_m:.4f} m)"
        )
    return integral_w_m2 / max(covered_m, 1.0e-12)


def _build_dcz_domain(
    remaining: list[DTTray],
    L_FTRZ_m: float,
    A_bed_m2: float,
    hQ: float,
    hM: float,
    aV: float,
    c: DTSolverConstants,
    nz_dcz: int,
) -> tuple[dcz.DCZConstants, tuple[float, ...]]:
    total_remaining_m = sum(tray.bed_height_m for tray in remaining)
    if L_FTRZ_m >= total_remaining_m:
        raise ValueError(
            f"FTRZ length ({L_FTRZ_m:.4f} m) leaves no DCZ domain within "
            f"the remaining DT bed ({total_remaining_m:.4f} m)"
        )

    segments: list[tuple[float, float, float]] = []
    ftrz_left_m = L_FTRZ_m
    z = 0.0
    for tray in remaining:
        ftrz_share_m = min(max(ftrz_left_m, 0.0), tray.bed_height_m)
        dcz_share_m = tray.bed_height_m - ftrz_share_m
        ftrz_left_m -= ftrz_share_m
        if dcz_share_m <= 1.0e-12:
            continue
        q_Iv_tray = _tray_q_Iv(tray, A_bed_m2)
        segments.append((z, z + dcz_share_m, q_Iv_tray))
        z += dcz_share_m
    L_DCZ_total_m = z

    dz = L_DCZ_total_m / nz_dcz
    profile: list[float] = []
    for j in range(nz_dcz):
        z_center = (j + 0.5) * dz
        q = segments[-1][2]  # fallback: last segment (guards float roundoff at the very bottom)
        for start, end, seg_q in segments:
            if start - 1.0e-9 <= z_center < end + 1.0e-9:
                q = seg_q
                break
        profile.append(q)

    dcz_c = dcz.DCZConstants(
        diameter_m=remaining[0].diameter_m,
        bed_height_m=L_DCZ_total_m,
        hM=hM,
        hQ=hQ,
        aV=aV,
        D_ax=c.D_ax,
        k_mixL=c.k_mixL,
        rho_V=c.rho_V,
        cp_V=c.cp_V,
        alpha_V=c.ftrz.bed_porosity,
        alpha_L=1.0 - c.ftrz.bed_porosity,
        particle=c.particle,
        dH_vap_water=c.ftrz.vapor_enthalpy_ref.dH_vap_water,
        antoine_water=c.ftrz.antoine_water,
        luikov=c.luikov,
        water_diffusivity=c.water_diffusivity,
        vapor_enthalpy_ref=c.ftrz.vapor_enthalpy_ref,
        pressure_pa=c.pressure_pa,
    )
    return dcz_c, tuple(profile)


def _adapt_relaxation(
    value: float,
    residual: float,
    previous_residual: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """Safeguarded residual-monotonic damping update for the outer coupling."""
    if not math.isfinite(residual) or residual > 1.20 * previous_residual:
        return max(minimum, 0.7 * value)
    if residual < 0.995 * previous_residual:
        return min(maximum, 1.05 * value)
    return value


# ---------------------------------------------------------------------------
# Result + top-level solve
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraySummary:
    id: str
    T: float  # K, exit (bottom-face) solid temperature
    X1: float  # kg/kg dry solid, exit moisture
    X2: float  # kg/kg dry solid, exit hexane


@dataclass(frozen=True)
class DTAxialProfile:
    """Per-cell axial profile spanning the WHOLE DT (PHZ -> FTRZ -> DCZ, top to
    bottom), for visualization (the HMI's own "profile along the tower", not
    consumed by `Model.step` -- that only needs `tray_summaries`' per-tray exit
    values). Parallel tuples, one entry per cell, ordered top-to-bottom by
    `z_m` (cumulative distance from the DT's own top face).

    `vapor_flow_kg_s` is a real, cell-varying quantity throughout: PHZ/FTRZ
    evaporation/condensation and DCZ particle transfer/water sorption all
    change the explicit component flows as vapor travels upward.
    """

    z_m: tuple[float, ...]
    zone: tuple[str, ...]  # "PHZ" | "FTRZ" | "DCZ"
    stage_id: tuple[str, ...]  # which real tray this cell belongs to
    solid_T: tuple[float, ...]  # K
    solid_X1: tuple[float, ...]  # kg/kg dry solid, moisture
    solid_X2: tuple[float, ...]  # kg/kg dry solid, hexane
    vapor_T: tuple[float, ...]
    vapor_flow_kg_s: tuple[float, ...]  # total (water + hexane)
    vapor_hexane_frac: tuple[float, ...]  # mass fraction, 0-1
    vapor_water_frac: tuple[float, ...]  # mass fraction, 0-1
    mechanism: tuple[str, ...]  # local transfer closure, finer than hardware zone


@dataclass(frozen=True)
class DTCouplingResiduals:
    solid_interface_T: float = math.inf
    vapor_interface_T: float = math.inf
    vapor_interface_hexane_fraction: float = math.inf
    vapor_interface_water_flow: float = math.inf
    vapor_interface_hexane_flow: float = math.inf
    ftrz_length: float = math.inf


@dataclass(frozen=True)
class DTResult:
    phz: PHZPassResult
    ftrz: ftrz.FTRZZoneResult
    dcz: dcz.DCZZoneResult
    tray_summaries: tuple[TraySummary, ...]
    axial_profile: DTAxialProfile
    L_PHZ_m: float
    L_FTRZ_m: float
    L_DCZ_m: float
    hQ: float
    hM: float
    aV: float
    outer_iterations: int
    converged: bool
    coupling_residuals: DTCouplingResiduals = DTCouplingResiduals()

    @property
    def solid_exit_X2(self) -> float:
        """Hexane leaving the whole DT (kg/kg dry solid) -- the DCZ's own
        volumetric-mean exit value, Coletto's own KPI (Fig. 9(a))."""
        return self.dcz.solid_out_X2


def validate_dt_result(result: DTResult, solid_feed: SolidFeed, c: DTSolverConstants) -> None:
    """Raise ``ValueError`` unless a result is safe to publish.

    ``solve_dt`` intentionally returns its best iterate when it reaches the
    outer-iteration cap.  That is useful diagnostically, but a digital-twin
    acceptance boundary must not replace its last trusted state with either a
    nonconverged iterate or a numerically finite yet physically impossible
    profile.

    The lower temperature bound expresses the no-refrigeration character of
    the DT: jackets, condensing steam, and hot vapor cannot cool the bulk meal
    below the colder of the incoming meal and hexane's boiling temperature.
    In particular, a water dew-point closure may describe an interface but
    cannot overwrite the FTRZ bulk temperature governed by Coletto A.17.
    """
    if not result.converged:
        raise ValueError("DT solve did not converge")
    if not result.dcz.converged:
        raise ValueError("DT solve contains a nonconverged DCZ inner solution")
    if result.dcz.residuals.maximum_scaled > 1.0:
        raise ValueError("DT solve contains unresolved DCZ fixed-point residuals")

    profile = result.axial_profile
    parallel_fields = (
        profile.z_m,
        profile.zone,
        profile.stage_id,
        profile.solid_T,
        profile.solid_X1,
        profile.solid_X2,
        profile.vapor_T,
        profile.vapor_flow_kg_s,
        profile.vapor_hexane_frac,
        profile.vapor_water_frac,
        profile.mechanism,
    )
    n = len(profile.z_m)
    if n == 0 or any(len(values) != n for values in parallel_fields):
        raise ValueError("DT axial profile fields must be nonempty and parallel")
    if any(not math.isfinite(value) for value in profile.z_m):
        raise ValueError("DT axial coordinates must be finite")
    if any(b < a for a, b in zip(profile.z_m, profile.z_m[1:])):
        raise ValueError("DT axial coordinates must be ordered top-to-bottom")

    numeric_profile_fields = (
        profile.solid_T,
        profile.solid_X1,
        profile.solid_X2,
        profile.vapor_T,
        profile.vapor_flow_kg_s,
        profile.vapor_hexane_frac,
        profile.vapor_water_frac,
    )
    if any(not math.isfinite(value) for values in numeric_profile_fields for value in values):
        raise ValueError("DT axial profile contains a non-finite state")

    min_bulk_T = min(solid_feed.T, c.ftrz.T_boil_hexane) - 1.0
    if any(value < min_bulk_T for value in profile.solid_T):
        raise ValueError("DT bulk-meal temperature fell below its physical lower bound")
    if any(value <= 0.0 for value in profile.vapor_T):
        raise ValueError("DT vapor temperature must remain above absolute zero")
    if any(value <= 0.0 for value in profile.vapor_flow_kg_s):
        raise ValueError("DT vapor flow must remain positive")
    for values, name in (
        (profile.solid_X1, "solid moisture"),
        (profile.solid_X2, "solid hexane"),
        (profile.vapor_hexane_frac, "vapor hexane fraction"),
        (profile.vapor_water_frac, "vapor water fraction"),
    ):
        if any(value < -1.0e-9 or value > 1.0 + 1.0e-9 for value in values):
            raise ValueError(f"DT {name} left the physical [0, 1] range")
    if any(
        abs(hexane + water - 1.0) > 1.0e-6
        for hexane, water in zip(profile.vapor_hexane_frac, profile.vapor_water_frac)
    ):
        raise ValueError("DT binary-vapor mass fractions do not sum to one")

    if not result.tray_summaries:
        raise ValueError("DT result must contain tray summaries")
    for summary in result.tray_summaries:
        if not all(math.isfinite(value) for value in (summary.T, summary.X1, summary.X2)):
            raise ValueError(f"DT tray {summary.id} contains a non-finite state")
        if summary.T < min_bulk_T:
            raise ValueError(f"DT tray {summary.id} temperature is below its physical lower bound")
        if not (0.0 <= summary.X1 <= 1.0 and 0.0 <= summary.X2 <= 1.0):
            raise ValueError(f"DT tray {summary.id} composition left the physical [0, 1] range")

    lengths = (result.L_PHZ_m, result.L_FTRZ_m, result.L_DCZ_m)
    if any(not math.isfinite(value) or value < 0.0 for value in lengths):
        raise ValueError("DT zone lengths must be finite and non-negative")
    if not all(math.isfinite(value) and value > 0.0 for value in (result.hQ, result.hM, result.aV)):
        raise ValueError("DT transport coefficients must be finite and positive")
    if abs(result.L_FTRZ_m - result.ftrz.L_FTRZ_m) > 1.0e-3:
        raise ValueError("DT FTRZ result and moving-boundary geometry are inconsistent")
    if (
        result.dcz.warm_start is None
        or abs(result.L_DCZ_m - result.dcz.warm_start.bed_height_m) > 1.0e-9
    ):
        raise ValueError("DT DCZ result and remeshed geometry are inconsistent")


def solve_dt(
    trays: list[DTTray],
    solid_feed: SolidFeed,
    vapor_feed_below: VaporFeed,
    c: DTSolverConstants,
    nz_phz: int = 20,
    nz_ftrz: int = 20,
    nz_dcz: int = 20,
    outer_relaxation: float = 0.5,
    outer_tol: float = 1.0e-5,
    outer_max_iter: int = 100,
    dcz_inner_max_iter: int = 100,
    dcz_continuation_max_blocks: int = 100,
    warm_start_vapor_in: ftrz.VaporState | None = None,
    warm_start_T_L_sup: float | None = None,
    sweep_arm_rpm: float = 0.0,
    residual_log: list[tuple[int, DTCouplingResiduals, float, float, float]] | None = None,
) -> DTResult:
    """Solve the integrated DT: PHZ once, then FTRZ<->DCZ Gauss-Seidel (see
    module docstring for the full design). `warm_start_vapor_in`/
    `warm_start_T_L_sup` seed the outer loop's coupling variables (M2's own
    stated "with... warm-start" acceptance criterion) -- the hook a future
    real-time wrapper (M3) will drive tick-to-tick; unused here beyond
    speeding up this one steady solve's own convergence.
    """
    if not trays:
        raise ValueError("solve_dt requires at least one DT tray")
    if solid_feed.X3 < 0.0:
        raise ValueError("feed oil content X3 must be non-negative")

    # Oil is a live feed disturbance, not a frozen material constant. PHZ
    # already consumes ``solid_feed.X3`` directly; give FTRZ's equilibrium
    # relation and DCZ's particle inventory the same current value. ``replace``
    # keeps the caller-owned frozen constants immutable across solves.
    c = replace(
        c,
        ftrz=replace(c.ftrz, X3=solid_feed.X3),
        particle=replace(c.particle, X3=solid_feed.X3),
    )
    diameters = {t.diameter_m for t in trays}
    if len(diameters) != 1:
        raise ValueError("dt_solver currently requires a uniform tray diameter across the DT")
    diameter_m = next(iter(diameters))
    A_bed_m2 = math.pi / 4.0 * diameter_m**2

    sparge_indices = [i for i, t in enumerate(trays) if t.direct_steam_kg_s > 0.0]
    if sparge_indices and sparge_indices != [len(trays) - 1]:
        raise ValueError(
            "direct (sparge) steam is only supported on the bottommost DT tray in "
            "this phase -- see dt_solver.py module docstring"
        )
    m_dir_kg_s = trays[-1].direct_steam_kg_s

    # --- sparge / bottom BC (mass+energy mix, see module docstring) ---
    m_clean_kg_s = vapor_feed_below.m_water_kg_s + vapor_feed_below.m_hex_kg_s
    m_water_bottom = vapor_feed_below.m_water_kg_s + m_dir_kg_s
    m_hex_bottom = vapor_feed_below.m_hex_kg_s
    m_vapor_total_kg_s = m_water_bottom + m_hex_bottom
    if m_dir_kg_s > 0.0:
        T_bottom = (m_clean_kg_s * vapor_feed_below.T + m_dir_kg_s * c.T_direct_steam) / (
            m_clean_kg_s + m_dir_kg_s
        )
    else:
        T_bottom = vapor_feed_below.T
    vapor_inf = dcz.VaporState(wV2=m_hex_bottom / m_vapor_total_kg_s, T=T_bottom)

    # --- bed transport coefficients (shared by FTRZ + DCZ) ---
    u_V_superficial = m_vapor_total_kg_s / (c.rho_V * A_bed_m2)
    hQ, hM, aV = bed_transport_coefficients(u_V_superficial, c, sweep_arm_rpm)

    # --- PHZ pass (once) ---
    vapor_hint = phz_mod.VaporState(
        wV1=vapor_feed_below.m_water_kg_s / m_clean_kg_s,
        wV2=vapor_feed_below.m_hex_kg_s / m_clean_kg_s,
        T=vapor_feed_below.T,
    )
    phz_result = _phz_pass(trays, solid_feed, vapor_hint, vapor_feed_below.m_water_kg_s, nz_phz, c)

    boundary_tray = trays[phz_result.boundary_tray_index]
    remaining: list[DTTray] = []
    host_remainder_m = boundary_tray.bed_height_m - phz_result.z_star_m
    if host_remainder_m > 1.0e-9:
        frac = (
            host_remainder_m / boundary_tray.bed_height_m
            if boundary_tray.bed_height_m > 0.0
            else 0.0
        )
        remaining.append(
            replace(
                boundary_tray,
                bed_height_m=host_remainder_m,
                Q_indirect_w=boundary_tray.Q_indirect_w * frac,
            )
        )
    remaining.extend(trays[phz_result.boundary_tray_index + 1 :])
    if not remaining:
        raise ValueError(
            "PHZ boundary lands exactly at the last tray's bottom -- no FTRZ/DCZ "
            "domain remains for the given trays/duties"
        )

    def ftrz_q_Iv(length_m: float) -> float:
        return _average_q_Iv_over_depth(remaining, length_m, A_bed_m2)

    X2_sup = phz_result.exit_state.X2

    # --- FTRZ<->DCZ Gauss-Seidel outer loop ---
    vapor_in = warm_start_vapor_in or ftrz.VaporState(
        m_water_kg_s=(1.0 - vapor_inf.wV2) * m_vapor_total_kg_s,
        m_hex_kg_s=vapor_inf.wV2 * m_vapor_total_kg_s,
        T=vapor_inf.T,
    )
    T_L_sup = warm_start_T_L_sup if warm_start_T_L_sup is not None else c.ftrz.T_boil_hexane

    L_FTRZ_m: float | None = None
    dcz_c: dcz.DCZConstants | None = None
    q_Iv_profile: tuple[float, ...] | None = None
    ftrz_result: ftrz.FTRZZoneResult | None = None
    dcz_result: dcz.DCZZoneResult | None = None
    dcz_warm_start: dcz.DCZWarmStart | None = None
    converged = False
    coupling_residuals = DTCouplingResiduals()
    iterations = 0
    base_relaxation = min(max(outer_relaxation, 1.0e-3), 1.0)
    temperature_relaxation = base_relaxation
    hexane_relaxation = base_relaxation
    water_relaxation = min(base_relaxation, 0.15)
    previous_temperature_residual = math.inf
    previous_hexane_residual = math.inf
    previous_water_residual = math.inf
    previous_raw_L_FTRZ_m: float | None = None

    for iterations in range(1, outer_max_iter + 1):
        ftrz_result = ftrz.solve_ftrz_zone(
            nz=nz_ftrz,
            X2_sup=X2_sup,
            m_dry_kg_s=solid_feed.m_dry_kg_s,
            vapor_in=vapor_in,
            q_Iv_w_m3=ftrz_q_Iv,
            hQ=hQ,
            hM=hM,
            aV_m2_per_m3=aV,
            diameter_m=diameter_m,
            c=c.ftrz,
            X1_sup=solid_feed.X1,
            T_solid_sup=phz_result.exit_state.T,
        )

        raw_L_FTRZ_m = ftrz_result.L_FTRZ_m
        L_FTRZ_m = raw_L_FTRZ_m
        d_length = (
            math.inf
            if previous_raw_L_FTRZ_m is None
            else abs(raw_L_FTRZ_m - previous_raw_L_FTRZ_m)
        )
        previous_raw_L_FTRZ_m = raw_L_FTRZ_m
        dcz_c, q_Iv_profile = _build_dcz_domain(
            remaining, L_FTRZ_m, A_bed_m2, hQ, hM, aV, c, nz_dcz
        )

        # A cap-limited inner result is not a valid outer-map evaluation.
        # Continue it in bounded blocks from the complete lagged state while
        # keeping the FTRZ boundary and DCZ mesh fixed.  Outer iteration
        # accounting therefore means what it says: one count per fully
        # resolved FTRZ<->DCZ map, not one count per arbitrary inner block.
        for _ in range(dcz_continuation_max_blocks):
            dcz_result = dcz.solve_dcz_zone(
                nz=nz_dcz,
                m_dry_kg_s=solid_feed.m_dry_kg_s,
                m_vapor_kg_s=m_vapor_total_kg_s,
                T_L_sup=T_L_sup,
                vapor_inf=vapor_inf,
                q_Iv_w_m3=q_Iv_profile,
                c=dcz_c,
                X1_in=solid_feed.X1 + ftrz_result.solid_out.X1,
                outer_max_iter=dcz_inner_max_iter,
                outer_relaxation=outer_relaxation,
                warm_start=dcz_warm_start,
            )
            dcz_warm_start = dcz_result.warm_start
            if dcz_result.converged:
                break
        if not dcz_result.converged:
            break

        new_T_L_sup = ftrz_result.solid_out.T
        dcz_top = dcz_result.vapor_out
        # DCZ now marches explicit water and hexane component flows. Use
        # those solved boundary values directly instead of reconstructing a
        # total flow from solid moisture and then splitting it by wV2.
        new_vapor_in = ftrz.VaporState(
            m_water_kg_s=dcz_result.vapor_water_out_kg_s,
            m_hex_kg_s=dcz_result.vapor_hexane_out_kg_s,
            T=dcz_top.T,
        )

        d_T = abs(new_T_L_sup - T_L_sup)
        # `vapor_in`'s own total mass no longer necessarily equals the fixed
        # whole-DT `m_vapor_total_kg_s` once DCZ condensation is subtracted
        # (see `new_vapor_in` above) -- use its own actual total here.
        d_wV2 = abs(
            dcz_top.wV2 - vapor_in.m_hex_kg_s / (vapor_in.m_water_kg_s + vapor_in.m_hex_kg_s)
        )
        d_TV = abs(dcz_top.T - vapor_in.T)
        d_m_water = abs(new_vapor_in.m_water_kg_s - vapor_in.m_water_kg_s)
        d_m_hex = abs(new_vapor_in.m_hex_kg_s - vapor_in.m_hex_kg_s)
        coupling_residuals = DTCouplingResiduals(
            solid_interface_T=d_T,
            vapor_interface_T=d_TV,
            vapor_interface_hexane_fraction=d_wV2,
            vapor_interface_water_flow=d_m_water,
            vapor_interface_hexane_flow=d_m_hex,
            ftrz_length=d_length,
        )

        T_L_sup = T_L_sup + temperature_relaxation * (new_T_L_sup - T_L_sup)
        vapor_in = ftrz.VaporState(
            m_water_kg_s=vapor_in.m_water_kg_s
            + water_relaxation * (new_vapor_in.m_water_kg_s - vapor_in.m_water_kg_s),
            m_hex_kg_s=vapor_in.m_hex_kg_s
            + hexane_relaxation * (new_vapor_in.m_hex_kg_s - vapor_in.m_hex_kg_s),
            T=vapor_in.T + temperature_relaxation * (new_vapor_in.T - vapor_in.T),
        )

        temperature_residual = max(d_T, d_TV)
        hexane_residual = max(d_wV2, d_m_hex)
        water_residual = d_m_water
        temperature_relaxation = _adapt_relaxation(
            temperature_relaxation,
            temperature_residual,
            previous_temperature_residual,
            minimum=0.05,
            maximum=base_relaxation,
        )
        hexane_relaxation = _adapt_relaxation(
            hexane_relaxation,
            hexane_residual,
            previous_hexane_residual,
            minimum=0.05,
            maximum=base_relaxation,
        )
        water_relaxation = _adapt_relaxation(
            water_relaxation,
            water_residual,
            previous_water_residual,
            minimum=0.025,
            maximum=min(base_relaxation, 0.25),
        )
        previous_temperature_residual = temperature_residual
        previous_hexane_residual = hexane_residual
        previous_water_residual = water_residual
        if residual_log is not None:
            residual_log.append(
                (
                    iterations,
                    coupling_residuals,
                    temperature_relaxation,
                    hexane_relaxation,
                    water_relaxation,
                )
            )

        # Finite-rate FTRZ water uptake makes the total vapor flow a genuine
        # coupling variable: matching only temperature and composition can
        # falsely declare convergence while kg/s is still changing.  Include
        # both component flow residuals in the same physical outer gate.
        if (
            max(d_T, d_TV, d_wV2, d_m_water, d_m_hex) <= outer_tol
            and d_length <= min(outer_tol, 1.0e-3)
        ):
            converged = True
            break

    assert ftrz_result is not None and dcz_result is not None and L_FTRZ_m is not None

    # --- per-real-tray exit (bottom-face) summaries ---
    tray_summaries: list[TraySummary] = []
    for r, t in zip(phz_result.tray_results, trays[: phz_result.boundary_tray_index]):
        tray_summaries.append(
            TraySummary(id=t.id, T=r.solid_out.T, X1=solid_feed.X1, X2=r.solid_out.X2)
        )
    boundary_tray = trays[phz_result.boundary_tray_index]
    boundary_is_full_tray = abs(phz_result.z_star_m - boundary_tray.bed_height_m) <= 1.0e-9
    if boundary_is_full_tray:
        tray_summaries.append(
            TraySummary(
                id=boundary_tray.id,
                T=phz_result.exit_state.T,
                X1=solid_feed.X1,
                X2=phz_result.exit_state.X2,
            )
        )

    # X1 is carried unchanged through PHZ (its own scope excludes a moisture
    # balance) and FTRZ's own solid_out.X1 is a DELTA accumulated from zero
    # (see module docstring) -- the true absolute moisture entering DCZ is
    # their sum (this is what `X1_in` above already fed into the final
    # `dcz_result`). DCZ now DOES carry its own moisture state (see
    # zones/dcz.py's module docstring, "MOISTURE (H2O) BALANCE") -- each
    # DCZ-spanned tray below uses that cell's own accumulated `X1_bulk`
    # rather than this entry value carried forward unchanged.

    geometry = pt.build_shell_geometry(c.particle.r_P, c.particle.Np)
    dz_dcz = dcz_c.bed_height_m / nz_dcz

    def _dcz_cell_at_bottom_face(z_local_m: float) -> dcz.DCZCellResult:
        # `z_local_m` is a tray's own bottom-face position in DCZ's local
        # z-coordinate (0 at the FTRZ/DCZ interface); the small epsilon keeps
        # a boundary landing exactly on a cell edge in the tray whose bottom
        # face it actually is, not the next tray's first cell.
        j = min(max(int((z_local_m - 1.0e-9) / dz_dcz), 0), nz_dcz - 1)
        return dcz_result.cells[j]

    def _ftrz_cell_at_bottom_face(depth_m: float) -> ftrz.FTRZCellResult:
        z = 0.0
        for cell in ftrz_result.cells:
            z += cell.dz_m
            if depth_m <= z + 1.0e-9:
                return cell
        return ftrz_result.cells[-1]

    # A thin free-boundary zone may cross a real-tray boundary.  Sample every
    # tray's physical bottom face from whichever zone actually contains it,
    # rather than assuming FTRZ fits wholly inside ``remaining[0]``.
    depth_from_ftrz_top_m = 0.0
    for tray in remaining:
        depth_from_ftrz_top_m += tray.bed_height_m
        if depth_from_ftrz_top_m <= L_FTRZ_m + 1.0e-9:
            cell = _ftrz_cell_at_bottom_face(depth_from_ftrz_top_m)
            tray_summaries.append(
                TraySummary(
                    id=tray.id,
                    T=cell.solid.T,
                    X1=solid_feed.X1 + cell.solid.X1,
                    X2=cell.solid.X2,
                )
            )
            continue
        cell = _dcz_cell_at_bottom_face(depth_from_ftrz_top_m - L_FTRZ_m)
        tray_summaries.append(
            TraySummary(
                id=tray.id,
                T=dcz.bulk_temperature(cell, geometry),
                X1=cell.X1_bulk,
                X2=cell.X2_bulk,
            )
        )

    # --- whole-DT axial profile (visualization only, see DTAxialProfile docstring) ---
    def _remaining_stage_id_at(depth_from_ftrz_top_m: float) -> str:
        z = 0.0
        for tray in remaining:
            if z - 1.0e-9 <= depth_from_ftrz_top_m < z + tray.bed_height_m + 1.0e-9:
                return tray.id
            z += tray.bed_height_m
        return remaining[-1].id

    def _dcz_stage_id_at(z_local_m: float) -> str:
        """Map a DCZ-local position back to its real tray."""
        return _remaining_stage_id_at(L_FTRZ_m + z_local_m)

    prof_z: list[float] = []
    prof_zone: list[str] = []
    prof_stage: list[str] = []
    prof_solid_T: list[float] = []
    prof_solid_X1: list[float] = []
    prof_solid_X2: list[float] = []
    prof_vapor_T: list[float] = []
    prof_vapor_flow: list[float] = []
    prof_vapor_hex: list[float] = []
    prof_vapor_water: list[float] = []
    prof_mechanism: list[str] = []

    def _append_profile_point(
        z: float,
        zone_name: str,
        stage: str,
        s_T: float,
        s_X1: float,
        s_X2: float,
        v_T: float,
        v_flow: float,
        v_hex: float,
        v_water: float,
        mechanism: str,
    ) -> None:
        prof_z.append(z)
        prof_zone.append(zone_name)
        prof_stage.append(stage)
        prof_solid_T.append(s_T)
        prof_solid_X1.append(s_X1)
        prof_solid_X2.append(s_X2)
        prof_vapor_T.append(v_T)
        prof_vapor_flow.append(v_flow)
        prof_vapor_hex.append(v_hex)
        prof_vapor_water.append(v_water)
        prof_mechanism.append(mechanism)

    # PHZ vapor -- PHYSICAL (not the old per-cell "informational" placeholder).
    # PHZ's SOLID solve is legitimately vapor-decoupled (the bed is jacket-heated,
    # so its temperature/hexane profile doesn't depend on the gas). But the vapor
    # RISING through PHZ physically carries hexane: it arrives hexane-rich from the
    # FTRZ flash front below (`ftrz_result.vapor_out`) and picks up MORE from each
    # PHZ cell's own surface evaporation (`hexane_evaporated_kg_s`, already computed
    # by `solve_phz_tray`) on the way up to the DT vapor outlet. The old trace used
    # each PHZ cell's decoupled `vapor_out` (which read ~pure water and did NOT
    # carry the solvent leaving the top) -- a display artifact, fixed here.
    #
    # Construction: water-vapor flow is CONSTANT across PHZ (X1 is carried unchanged
    # -- no water exchange with the solid here), equal to the FTRZ outlet water flow.
    # Hexane flow leaving a cell's top face = FTRZ hexane + every PHZ cell at or below
    # it (all cells the rising vapor has already passed), so we accumulate bottom-to-top.
    # The vapor TEMPERATURE keeps each cell's own local estimate (`vapor_out.T`, which
    # tracks the solid temperature) -- a rigorous PHZ vapor energy balance is out of
    # scope and unneeded, since the solid solve doesn't consume it.
    phz_vapor_in = ftrz_result.vapor_out  # hexane-rich vapor entering PHZ from the flash front
    m_water_phz = phz_vapor_in.m_water_kg_s  # constant across PHZ
    m_hex_ftrz = phz_vapor_in.m_hex_kg_s

    phz_points: list[tuple[float, str, phz_mod.PHZCellResult]] = []
    z_cum = 0.0
    for tray, result in zip(trays[: phz_result.boundary_tray_index], phz_result.tray_results):
        for cell in result.cells:
            phz_points.append((z_cum + cell.z_from_top_m, tray.id, cell))
        z_cum += tray.bed_height_m
    boundary_tray = trays[phz_result.boundary_tray_index]
    for cell in phz_result.boundary_tray_result.cells:
        phz_points.append((z_cum + cell.z_from_top_m, boundary_tray.id, cell))

    # Suffix-sum the hexane evaporated: cell i's top-face vapor carries the FTRZ
    # hexane plus cells i..last (bottom-to-top accumulation in the vapor's direction).
    hex_flow_at = [0.0] * len(phz_points)
    running_hex = m_hex_ftrz
    for i in range(len(phz_points) - 1, -1, -1):
        running_hex += phz_points[i][2].hexane_evaporated_kg_s
        hex_flow_at[i] = running_hex

    antoine_water = c.ftrz.antoine_water
    antoine_hexane = c.antoine_hexane
    P_atm = thermo.ATM_PRESSURE_PA

    def _binary_dew_T(m_hex: float, m_water: float, fallback: float) -> float:
        """DT vapor temperature = the BINARY (hexane + water) dew point of the
        exiting vapor: the highest temperature at which either component starts
        condensing (its partial pressure reaches saturation) -- the physical
        temperature of the vapor as it leaves the bed. For water-rich vapor the
        water dew point governs (the AOCS/Kemper 2019 dome relation: more water ->
        higher dome); for hexane-rich vapor the hexane dew point governs, so the
        result naturally floors near the ~61 C hexane-water heteroazeotrope
        instead of dropping unphysically toward the water dew point of a nearly
        dry-of-water vapor."""
        n_hex = m_hex / thermo.M_HEXANE
        n_water = m_water / thermo.M_WATER
        n_tot = n_hex + n_water
        if n_tot <= 0.0:
            return fallback
        dews = []
        if m_water > 1.0e-9:
            y_w = n_water / n_tot
            dews.append(
                brentq(
                    lambda T: thermo.antoine_pressure_pa(T, antoine_water) - y_w * P_atm,
                    230.0,
                    470.0,
                )
            )
        if m_hex > 1.0e-9:
            y_h = n_hex / n_tot
            dews.append(
                brentq(
                    lambda T: thermo.antoine_pressure_pa(T, antoine_hexane) - y_h * P_atm,
                    230.0,
                    470.0,
                )
            )
        return max(dews) if dews else fallback

    for i, (z, stage, cell) in enumerate(phz_points):
        m_hex = hex_flow_at[i]
        v_flow = m_water_phz + m_hex
        # PHZ vapor TEMPERATURE is a vapor-liquid-equilibrium quantity (the binary
        # dew point above), NOT the bed solid temperature. The PHZ solid solve is
        # legitimately vapor-decoupled (jacket-heated), but the DT DOME temperature
        # (this trace's top point) is exactly the AOCS/Kemper (2019) dome relation.
        # Reporting the solid temp instead read the top-of-bed feed temperature
        # (~59 C, BELOW the ~61 C heteroazeotrope floor -- physically impossible)
        # as the "dome". The dew point connects continuously to the FTRZ vapor
        # below (whose own T equals its dew point), so no per-tray "sawtooth".
        v_T = _binary_dew_T(m_hex, m_water_phz, cell.solid_out.T)
        _append_profile_point(
            z,
            "PHZ",
            stage,
            cell.solid_out.T,
            solid_feed.X1,
            cell.solid_out.X2,
            v_T,
            v_flow,
            m_hex / v_flow if v_flow > 0.0 else 0.0,
            m_water_phz / v_flow if v_flow > 0.0 else 1.0,
            cell.regime,
        )
    z_cum = phz_result.L_PHZ_m  # authoritative, avoids float drift from the sum above

    # FTRZ may cross a real-tray boundary. NOTE `cell.solid.X1` is the net
    # water transferred within FTRZ only (bulk condensation plus finite-rate
    # sorption, initialized at 0 and accumulated top-to-bottom), so the
    # displayed TOTAL adds the feed moisture carried through PHZ.
    ftrz_depth_m = 0.0
    for cell in ftrz_result.cells:
        z_cum += cell.dz_m
        ftrz_depth_m += cell.dz_m
        v_flow = cell.vapor_out.m_water_kg_s + cell.vapor_out.m_hex_kg_s
        _append_profile_point(
            z_cum,
            "FTRZ",
            _remaining_stage_id_at(ftrz_depth_m),
            cell.solid.T,
            solid_feed.X1 + cell.solid.X1,
            cell.solid.X2,
            cell.vapor_out.T,
            v_flow,
            cell.vapor_out.m_hex_kg_s / v_flow,
            cell.vapor_out.m_water_kg_s / v_flow,
            "STEAM_FLASH",
        )
    z_cum = phz_result.L_PHZ_m + L_FTRZ_m

    # DCZ: explicit component flows vary cell by cell.
    for j, cell in enumerate(dcz_result.cells):
        z_local_end = (j + 1) * dz_dcz
        z_local_center = (j + 0.5) * dz_dcz
        stage = _dcz_stage_id_at(z_local_center)
        _append_profile_point(
            z_cum + z_local_end,
            "DCZ",
            stage,
            dcz.bulk_temperature(cell, geometry),
            cell.X1_bulk,
            cell.X2_bulk,
            cell.vapor_top.T,
            cell.vapor_flow_kg_s,
            cell.vapor_top.wV2,
            1.0 - cell.vapor_top.wV2,
            "DIFFUSION_CONTROLLED",
        )

    axial_profile = DTAxialProfile(
        z_m=tuple(prof_z),
        zone=tuple(prof_zone),
        stage_id=tuple(prof_stage),
        solid_T=tuple(prof_solid_T),
        solid_X1=tuple(prof_solid_X1),
        solid_X2=tuple(prof_solid_X2),
        vapor_T=tuple(prof_vapor_T),
        vapor_flow_kg_s=tuple(prof_vapor_flow),
        vapor_hexane_frac=tuple(prof_vapor_hex),
        vapor_water_frac=tuple(prof_vapor_water),
        mechanism=tuple(prof_mechanism),
    )

    return DTResult(
        phz=phz_result,
        ftrz=ftrz_result,
        dcz=dcz_result,
        tray_summaries=tuple(tray_summaries),
        axial_profile=axial_profile,
        L_PHZ_m=phz_result.L_PHZ_m,
        L_FTRZ_m=L_FTRZ_m,
        L_DCZ_m=dcz_c.bed_height_m,
        hQ=hQ,
        hM=hM,
        aV=aV,
        outer_iterations=iterations,
        converged=converged,
        coupling_residuals=coupling_residuals,
    )
