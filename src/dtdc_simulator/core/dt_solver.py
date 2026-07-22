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
informational-only there). So PHZ, and the PHZ/FTRZ boundary location, are
solved ONCE, up front -- no outer-loop dependency. The *only* two coupling
scalars across the remaining FTRZ/DCZ interface are `DCZZoneResult.vapor_out`
(top face) feeding FTRZ's `vapor_in` (bottom BC), and
`FTRZZoneResult.solid_out.T` (bottom-most cell) feeding DCZ's `T_L_sup`. This
collapses Fig. 5's "tray-by-tray fixed-point sweep" to a two-variable
Gauss-Seidel loop, spanning trays rather than iterating within each one --
simpler than a literal reading might suggest, and still faithful to it
("solve FTRZ before DCZ... repeat until profiles converge").

FREE BOUNDARIES:
- PHZ/FTRZ interface (`L_PHZ`, where `X2` crosses `X2,cr(T)`): located by
  marching real trays top-down at full height (`zones/phz.py::solve_phz_tray`,
  unchanged) until one tray's exit drops to/below `X2,cr`; within that one
  "boundary tray," `brentq` locates the precise sub-height `z*` (re-solving
  `solve_phz_tray` at trial heights, cheap since PHZ has no internal
  iteration) -- mirroring the same rigor `ftrz.py` already applies to its own
  free boundary `L_FTRZ`.
- FTRZ/DCZ interface (`L_FTRZ`): already solved endogenously by
  `solve_ftrz_zone`'s own internal fixed point (unchanged). This module only
  freezes it (from the *first* outer-loop FTRZ solve) to fix DCZ's own mesh
  geometry for the rest of the Gauss-Seidel loop -- re-meshing DCZ's `nz`
  cells every single outer pass over a geometry that barely moves (FTRZ is
  "order cm" against tray heights of 0.3-1.0 m) is unnecessary cost and a
  needless source of noise in the DCZ<->particle Gauss-Seidel's own
  convergence. A DOCUMENTED simplification, not an oversight.

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
packed-bed correlations, clearly tagged `[DERIVED]`/`[PLACE]` like every
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
`[DERIVED]`/`[PLACE]`, not upgraded to `[PAPER]`: that reconstruction's own
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

    SWEEP-ARM AGITATION ENHANCEMENT (found this session, DECISIONS.md's "DCZ
    hot-temperature root cause" entry): the Nu-Re correlation above describes
    passive, FLOW-DRIVEN convection through a static packed bed -- but a real
    DT bed is continuously mechanically swept/agitated by the central-shaft
    arms (the `sweep_arm_speed` MV), a fundamentally different, much stronger
    heat/mass-transfer regime (continuously-renewed gas-solid contact, not
    boundary-layer-limited flow). `ModelParams.sweep_arm_transfer_gain`
    already existed in `config/schema.py` with exactly this stated purpose
    ("hQ/hM sensitivity to sweep-arm speed") but was never actually wired to
    anything -- confirmed by a repo-wide search before adding this. Routing
    agitation through the SAME Re-Nu correlation (e.g. adding an
    arm-tip-speed contribution to `u_V_superficial_m_s`) was tried and
    rejected: at this scenario's own tray radius, tip speed is only
    comparable in magnitude to the vapor's own superficial velocity, so it
    barely moves Nu (a ~38% bump at rpm=3, via Re^0.579) -- nowhere close to
    what direct instrumentation (this session) showed was needed to bring
    DCZ's converged temperature into its own validated band (~380-383 K,
    matching literature_sources/Svoboda_Case_for_Advanced_Process_Control_
    VRX-DTDC_Concept.pdf's own SD-tray reading, ~102 C) and let condensation
    actually trigger. A mechanically-agitated bed isn't a stronger FLOW, it's
    a different transfer REGIME -- so this is a direct, separate multiplier
    on hQ (hM is then re-derived from the enhanced hQ via the SAME existing
    Chilton-Colburn analogy below, keeping that relationship internally
    consistent) rather than forcing it through the flow correlation.
    `sweep_arm_transfer_gain` retuned from its old (never-effective, always
    0.2) placeholder to reproduce that validated target -- still `[PLACE]`,
    no fitted agitated-bed correlation exists in this project's literature,
    but now at least load-bearing and empirically anchored.
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
    exit_state: phz_mod.SolidState  # X2 <= X2,cr, T == T_boil_hexane
    L_PHZ_m: float


def _phz_pass(
    trays: list[DTTray],
    solid_feed: SolidFeed,
    vapor_hint: phz_mod.VaporState,
    m_vapor_water_kg_s: float,
    nz_phz: int,
    c: DTSolverConstants,
) -> PHZPassResult:
    solid = phz_mod.SolidState(T=solid_feed.T, X2=solid_feed.X2)
    completed: list[phz_mod.PHZTrayResult] = []
    cumulative_height = 0.0

    def _x2_cr(T: float) -> float:
        return thermo.x2_critical(
            c.particle.alpha_pg,
            thermo.rho_hexane_liquid(T),
            c.particle.alpha_ps,
            c.particle.rho_ps,
            empirical=c.particle.x2_critical_empirical,
        )

    for idx, tray in enumerate(trays):
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
        )
        exit_state = result.solid_out
        if exit_state.X2 > _x2_cr(exit_state.T):
            completed.append(result)
            cumulative_height += tray.bed_height_m
            solid = exit_state
            continue

        def residual(
            h: float, _tray: DTTray = tray, _solid_in: phz_mod.SolidState = solid
        ) -> float:
            frac = h / _tray.bed_height_m if _tray.bed_height_m > 0.0 else 0.0
            r = phz_mod.solve_phz_tray(
                nz_phz,
                h,
                _tray.diameter_m,
                _tray.Q_indirect_w * frac,
                _solid_in,
                vapor_hint,
                solid_feed.m_dry_kg_s,
                m_vapor_water_kg_s,
                solid_feed.X1,
                solid_feed.X3,
                c.phz,
            )
            return r.solid_out.X2 - _x2_cr(r.solid_out.T)

        z_star = 0.0 if residual(0.0) <= 0.0 else brentq(residual, 0.0, tray.bed_height_m)
        frac = z_star / tray.bed_height_m if tray.bed_height_m > 0.0 else 0.0
        boundary_result = phz_mod.solve_phz_tray(
            nz_phz,
            z_star,
            tray.diameter_m,
            tray.Q_indirect_w * frac,
            solid,
            vapor_hint,
            solid_feed.m_dry_kg_s,
            m_vapor_water_kg_s,
            solid_feed.X1,
            solid_feed.X3,
            c.phz,
        )
        return PHZPassResult(
            tray_results=tuple(completed),
            boundary_tray_index=idx,
            z_star_m=z_star,
            boundary_tray_result=boundary_result,
            exit_state=boundary_result.solid_out,
            L_PHZ_m=cumulative_height + z_star,
        )

    raise ValueError(
        "PHZ never reaches X2_cr within the given trays -- add more PREDESOLV/MAIN "
        "trays or increase indirect steam duty"
    )


# ---------------------------------------------------------------------------
# DCZ domain assembly (per-tray duty apportionment -- see module docstring)
# ---------------------------------------------------------------------------


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
    host = remaining[0]
    host_q_Iv = (
        host.Q_indirect_w / (A_bed_m2 * host.bed_height_m) if host.bed_height_m > 0.0 else 0.0
    )
    host_remainder_m = host.bed_height_m - L_FTRZ_m

    segments: list[tuple[float, float, float]] = [(0.0, host_remainder_m, host_q_Iv)]
    z = host_remainder_m
    for tray in remaining[1:]:
        q_Iv_tray = (
            tray.Q_indirect_w / (A_bed_m2 * tray.bed_height_m) if tray.bed_height_m > 0.0 else 0.0
        )
        segments.append((z, z + tray.bed_height_m, q_Iv_tray))
        z += tray.bed_height_m
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
        diameter_m=host.diameter_m,
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

    `vapor_flow_kg_s` is a REAL, cell-varying quantity in PHZ/FTRZ (hexane
    evaporation/condensation changes the vapor's own total mass flow as it
    travels) but a FIXED *input* to DCZ's own zone solve (`solve_dcz_zone`
    takes one scalar `m_vapor_kg_s`, not a per-cell profile) -- so this trace
    is flat across the DCZ segment by construction, not a rendering artifact.
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

    @property
    def solid_exit_X2(self) -> float:
        """Hexane leaving the whole DT (kg/kg dry solid) -- the DCZ's own
        volumetric-mean exit value, Coletto's own KPI (Fig. 9(a))."""
        return self.dcz.solid_out_X2


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
    warm_start_vapor_in: ftrz.VaporState | None = None,
    warm_start_T_L_sup: float | None = None,
    sweep_arm_rpm: float = 0.0,
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

    host = remaining[0]
    host_q_Iv = (
        host.Q_indirect_w / (A_bed_m2 * host.bed_height_m) if host.bed_height_m > 0.0 else 0.0
    )
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
    converged = False
    iterations = 0

    for iterations in range(1, outer_max_iter + 1):
        ftrz_result = ftrz.solve_ftrz_zone(
            nz=nz_ftrz,
            X2_sup=X2_sup,
            m_dry_kg_s=solid_feed.m_dry_kg_s,
            vapor_in=vapor_in,
            q_Iv_w_m3=host_q_Iv,
            hQ=hQ,
            aV_m2_per_m3=aV,
            diameter_m=diameter_m,
            c=c.ftrz,
            X1_sup=solid_feed.X1,
        )

        if L_FTRZ_m is None:
            L_FTRZ_m = ftrz_result.L_FTRZ_m
            if L_FTRZ_m >= host.bed_height_m:
                raise ValueError(
                    f"FTRZ length ({L_FTRZ_m:.4f} m) exceeds its host tray {host.id}'s "
                    f"own remaining height ({host.bed_height_m:.4f} m) -- geometry/duty "
                    "inputs are not physically consistent with a thin FTRZ"
                )
            dcz_c, q_Iv_profile = _build_dcz_domain(
                remaining, L_FTRZ_m, A_bed_m2, hQ, hM, aV, c, nz_dcz
            )

        dcz_result = dcz.solve_dcz_zone(
            nz=nz_dcz,
            m_dry_kg_s=solid_feed.m_dry_kg_s,
            m_vapor_kg_s=m_vapor_total_kg_s,
            T_L_sup=T_L_sup,
            vapor_inf=vapor_inf,
            q_Iv_w_m3=q_Iv_profile,
            c=dcz_c,
            # X1 entering DCZ is FTRZ's own exit moisture, computable each
            # outer pass since FTRZ (above) already solved this iteration --
            # see the tray-summary loop below for the OUTPUT-side use of the
            # same quantity.
            X1_in=solid_feed.X1 + ftrz_result.solid_out.X1,
            # Defaults to solve_dcz_zone's own default (100) -- exposed as a
            # tunable rather than hardcoded because DCZ's OWN inner
            # Gauss-Seidel loop doesn't strictly need to fully re-converge
            # every single outer dt_solver pass (THIS loop keeps re-solving
            # it with updated T_L_sup/vapor_inf until ITS OWN convergence
            # criterion is met). Empirically, though, lowering this cap for
            # speed measurably changes the converged profile near the FTRZ
            # /DCZ interface (confirmed: dropping to 30 breaks strict
            # monotonicity of the reported hexane profile there) -- so the
            # default stays conservative; only lower it deliberately, e.g.
            # for a future real-time (M3) wrapper's own speed/accuracy
            # trade-off, not silently for this module's own convenience.
            outer_max_iter=dcz_inner_max_iter,
        )

        new_T_L_sup = ftrz_result.solid_out.T
        dcz_top = dcz_result.vapor_out
        # Water NET transferred from vapor to solid within DCZ (moisture
        # balance, see zones/dcz.py's own module docstring) leaves the total
        # vapor mass reaching FTRZ's own bottom BC lower than the fixed
        # whole-DT total -- account for it here rather than silently
        # reintroducing it at the handoff. Found this session (a second,
        # related gap the same design pass that found dcz.py's own
        # condensation/wV2 gap also turned up): using ONLY
        # `dcz_result.total_condensed_kg_s` here undercounts this -- that
        # property sums ONLY the supersaturated-condensation branch, not the
        # (now real, isotherm-driven) subsaturated adsorption/desorption
        # branch, which in the common (non-supersaturated) operating regime
        # is often the ONLY branch doing anything at all (confirmed directly
        # during the direct_steam inversion work: `total_condensed_kg_s` was
        # zero in the real scenario while `X1` still moved measurably) --
        # meaning this correction was previously a near no-op in the typical
        # case, silently reintroducing isotherm-adsorbed water into the
        # vapor stream it had just left. The SOLID's own net X1 change
        # (dry-solid-mass-scaled) captures BOTH branches' combined effect
        # exactly, independent of which branch(es) contributed.
        X1_in_to_dcz = solid_feed.X1 + ftrz_result.solid_out.X1
        total_water_to_solid_kg_s = solid_feed.m_dry_kg_s * (
            dcz_result.solid_out_X1 - X1_in_to_dcz
        )
        m_vapor_into_ftrz_kg_s = m_vapor_total_kg_s - total_water_to_solid_kg_s
        new_vapor_in = ftrz.VaporState(
            m_water_kg_s=(1.0 - dcz_top.wV2) * m_vapor_into_ftrz_kg_s,
            m_hex_kg_s=dcz_top.wV2 * m_vapor_into_ftrz_kg_s,
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

        T_L_sup = T_L_sup + outer_relaxation * (new_T_L_sup - T_L_sup)
        vapor_in = ftrz.VaporState(
            m_water_kg_s=vapor_in.m_water_kg_s
            + outer_relaxation * (new_vapor_in.m_water_kg_s - vapor_in.m_water_kg_s),
            m_hex_kg_s=vapor_in.m_hex_kg_s
            + outer_relaxation * (new_vapor_in.m_hex_kg_s - vapor_in.m_hex_kg_s),
            T=vapor_in.T + outer_relaxation * (new_vapor_in.T - vapor_in.T),
        )

        if max(d_T, d_TV, d_wV2) <= outer_tol:
            converged = True
            break

    assert ftrz_result is not None and dcz_result is not None and L_FTRZ_m is not None

    # --- per-real-tray exit (bottom-face) summaries ---
    tray_summaries: list[TraySummary] = []
    for r, t in zip(phz_result.tray_results, trays[: phz_result.boundary_tray_index]):
        tray_summaries.append(
            TraySummary(id=t.id, T=r.solid_out.T, X1=solid_feed.X1, X2=r.solid_out.X2)
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

    # The boundary (host) tray's own physical bottom face sits at DCZ-local
    # z = host.bed_height_m - L_FTRZ_m (DCZ's z=0 is the FTRZ/DCZ interface,
    # i.e. right where FTRZ finishes within this same tray).
    z_local = host.bed_height_m - L_FTRZ_m
    host_cell = _dcz_cell_at_bottom_face(z_local)
    tray_summaries.append(
        TraySummary(
            id=host.id,
            T=dcz.bulk_temperature(host_cell, geometry),
            X1=host_cell.X1_bulk,
            X2=host_cell.X2_bulk,
        )
    )
    for tray in remaining[1:]:
        z_local += tray.bed_height_m
        cell = _dcz_cell_at_bottom_face(z_local)
        tray_summaries.append(
            TraySummary(
                id=tray.id,
                T=dcz.bulk_temperature(cell, geometry),
                X1=cell.X1_bulk,
                X2=cell.X2_bulk,
            )
        )

    # --- whole-DT axial profile (visualization only, see DTAxialProfile docstring) ---
    def _dcz_stage_id_at(z_local_m: float) -> str:
        """Same half-open-interval bucketing `_build_dcz_domain`'s own q_Iv
        profile already uses, just keyed on real tray id instead of duty."""
        z = 0.0
        for tray in remaining:
            if z - 1.0e-9 <= z_local_m < z + tray.bed_height_m + 1.0e-9:
                return tray.id
            z += tray.bed_height_m
        return remaining[-1].id

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
            dews.append(brentq(lambda T: thermo.antoine_pressure_pa(T, antoine_water) - y_w * P_atm, 230.0, 470.0))
        if m_hex > 1.0e-9:
            y_h = n_hex / n_tot
            dews.append(brentq(lambda T: thermo.antoine_pressure_pa(T, antoine_hexane) - y_h * P_atm, 230.0, 470.0))
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
        )
    z_cum = phz_result.L_PHZ_m  # authoritative, avoids float drift from the sum above

    # FTRZ: thin zone, entirely within the boundary/host tray (solve_dt already
    # raises above if L_FTRZ_m doesn't fit inside it). NOTE `cell.solid.X1` is the
    # water CONDENSED within FTRZ only (it initializes at 0 and accumulates
    # condensate top-to-bottom -- see zones/ftrz.py) -- so the displayed TOTAL
    # moisture adds back the feed moisture the solid already carried through PHZ
    # (`solid_feed.X1`, carried unchanged). Without this the profile drops to ~0%
    # at the flash front and jumps back up at DCZ -- a display artifact, not real.
    for cell in ftrz_result.cells:
        z_cum += cell.dz_m
        v_flow = cell.vapor_out.m_water_kg_s + cell.vapor_out.m_hex_kg_s
        _append_profile_point(
            z_cum,
            "FTRZ",
            host.id,
            cell.solid.T,
            solid_feed.X1 + cell.solid.X1,
            cell.solid.X2,
            cell.vapor_out.T,
            v_flow,
            cell.vapor_out.m_hex_kg_s / v_flow,
            cell.vapor_out.m_water_kg_s / v_flow,
        )
    z_cum = phz_result.L_PHZ_m + L_FTRZ_m

    # DCZ: vapor_flow_kg_s is the zone's own fixed scalar input, not a per-cell
    # solved quantity (see DTAxialProfile docstring).
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
            m_vapor_total_kg_s,
            cell.vapor_top.wV2,
            1.0 - cell.vapor_top.wV2,
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
    )
