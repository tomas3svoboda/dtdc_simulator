"""Bed scale of the Diffusion-Controlled Zone (DCZ) -- Coletto, Bandoni &
Blanco (2022), Table A.4 / §2.4.2 / §A.3.2-A.3.3, coupled to the particle
scale (`core/zones/particle.py`) via the REAL algorithm published in the
paper's supplementary material (Fig. 3, "Primary Internal Loop") -- not a
reconstruction. M2 Phase 3 (BuildSpec §14): standalone, pure, unit-tested --
not yet wired into `core/model.py`'s tray-by-tray sweep (M2 Phase 4).

THE PRIMARY INTERNAL LOOP (exactly Fig. 3 of the supplementary material),
iteration index `s`, across all `nz` axial cells at once each step:
  1. Energy balance at particle scale -- march every cell's particle
     temperature field top-to-bottom, using the *current* vapor temperature
     profile and the *previous* iteration's mass-march rate (sorption heat).
  2. Energy balance at bed scale -- integrate bottom-to-top using the
     just-updated particle surface temperatures, producing a new TV profile.
  3. Mass balance at particle scale -- march every cell's particle hexane
     field top-to-bottom, using the just-updated TV profile (isotherm is
     T-dependent) and the *previous* iteration's wV2 profile.
  4. Mass balance at bed scale -- integrate bottom-to-top using the
     just-updated particle surface hexane fractions, producing a new wV2
     profile.
  5. Convergence check: max deviation in wpg2 and Tp, across *all* cells and
     *all* particle layers, against `outer_tol`. Not converged -> repeat.

DOCUMENTED RESOLUTION -- bed-scale marching direction and sign (not a
literal transcription of eq. A.35): the source material states `alphaV *
||uV|| * rho * dphi/dz = -kappa_phi*aV*alphaL*(phiV-phiL) + S*_phiV`, but
doesn't fully spell out (in what's available to us) the vector orientation
of `r-hat` or how the vapor's own upward (decreasing-z) velocity enters the
z-derivative's sign. Rather than risk a silent sign error from an
ambiguous convention, this module marches bottom-to-top cell-by-cell adding
the PHYSICALLY-motivated transfer (vapor gains hexane/heat from the
particle as it flows from a cell's bottom face to its top face) -- the same
category of documented gap-closure as FTRZ's eq. A.18 units resolution.

Axial-dispersion (mass, eq. A.36) and axial-conduction (energy, eq. A.32)
correction source terms are computed from the *previous* iteration's own
converged-so-far axial profile (a 3-point Laplacian -- the main paper's own
§3.3 remark that `q_condL` "is calculated using the temperature difference
between two cells") -- lagged by one outer pass, the same technique used for
`L_FTRZ`'s free boundary.

BED-SCALE MARCHING IS IMPLICIT PER CELL, not the naive explicit (forward)
step eq. A.35 might otherwise suggest: the particle<->vapor transfer
coefficient (`hM`/`hQ * aV * alphaL`) is typically stiff relative to a
practical cell size -- its own relaxation length can be much shorter than
`dz` -- so an explicit step diverges (confirmed by hitting exactly this
during development: cell-to-cell values blew up by many orders of
magnitude). A per-cell backward-Euler-style relaxation step is
unconditionally stable for this linear form, mirroring why the particle
-scale march is implicit for the same underlying stiffness reason.

ENERGY-BALANCE FIX (M2 Phase 4, `core/dt_solver.py`'s integration work):
M2 Phase 3 shipped this module WITHOUT eq. A.34/A.37's `SVm2*Ĥ2`/`ṁ'ax,net*
Ĥ2` enthalpy-transport terms, on the documented grounds that implementing
them literally (as an absolute mass-flux-times-enthalpy quantity) caused
runaway heating. Integrating this module into the full DT solve (realistic
`Q_indirect` magnitudes, not the small illustrative ones this module's own
tests use) surfaced that dropping them isn't the bounded "runs a bit cooler"
approximation it was believed to be: confirmed by direct instrumentation,
the particle<->vapor system has no energy floor without them and drifts to
UNBOUNDED cooling over enough outer iterations, eventually crashing the GAB
isotherm's own validity range. Fixed by restoring the missing term, but as a
different (and correct) quantity: not the bed-scale *surface* mass flux
`SVm2` (tried first, negligible effect -- a particle's own diffusive
relaxation is far slower than one axial cell's residence time, so the
surface flux badly lags the true internal desorption rate), but the
particle-VOLUME-integrated sorption/desorption sink (exactly what step 1
already subtracts from the particle via eq. A.30's first two terms),
credited back to the vapor at the SAME cell with the opposite sign -- see
`march_particle_energy`'s step-1 call site below and `zones/particle.py::
sorption_heat_sink_volumetric_mean_w_m3`'s own docstring for the full
derivation. Being an EXACT transfer between the two already-computed energy
balances (not an independently-derived absolute term), it cannot manufacture
a runaway the way the first attempt did.

MOISTURE (H2O) BALANCE (found this session, not part of M2 Phase 3/4's own
scope): this module originally carried NO water balance at all -- the
`direct_steam` MV (SP1's sparge injection, mixed into the DT's bottom vapor
BC in `core/dt_solver.py`) flowed through DCZ's vapor stream without ever
being able to condense onto the solid, so changing it had literally no
effect on any tray's reported moisture. Coletto's own DCZ sub-model is
hexane-only (§7.6). A first version (M2-follow-up "H2O balance") added
bulk-vapor dew-point condensation only (mirroring `zones/ftrz.py`'s own
V-SCAL/V-SAT switch) -- correct as far as it went, but a binary
supersaturated-or-not threshold can't respond to the DEGREE of humidity,
only to crossing a cutoff, which produced a real, confirmed bug: `direct_
steam` (injected hotter than the surrounding vapor) moved the boundary
FURTHER from that cutoff as it increased, so more steam meant LESS
condensation -- backwards from real plants. Fixed by adding a genuine
sorption/desorption isotherm for the (much more common) subsaturated
regime, once a real water-sorption isotherm for soybean meal was found
(`literature_sources/Gianini_Study_of_the_equilibrium_isotherms_of_soybean_
meal.pdf` -- measured on meal sampled directly from a desolventizer/
toaster's own outlet; `thermo.LuikovParams`/`thermo.luikov_equilibrium_
moisture`) -- structurally the SAME architecture hexane's own GAB isotherm
already uses (`wpg2` -> `W2(a_h,T)`), just lumped/lagged rather than
radially resolved (no diffusivity data exists for water in this matrix to
justify a 12-layer FVM the way hexane gets one).

Two mutually-exclusive regimes per cell, both driven by the SAME water
-activity variable (`thermo.water_activity`, mathematically identical to
"is this cell below its own dew point," just reframed):
- `a_w >= 1` (supersaturated): condensation, exactly as before -- step 2's
  existing per-cell implicit relaxation computes a candidate new vapor
  temperature from convective/duty/sorption sources; if that candidate falls
  below the local dew point, cap it there and back-solve (closed-form, the
  relaxation equation is linear in its source term) the condensed-water mass
  the SAME equation implies, clamped to the water actually available at that
  point in the march.
- `a_w < 1` (subsaturated): bidirectional adsorption/desorption toward the
  isotherm's own equilibrium `Xe(a_w)`, an implicit relaxation over the
  cell's own residence `dt` (mirrors the particle-scale marches' own
  backward-Euler pattern) using `hM*aV` as the equilibration rate -- no
  water-specific mass-transfer coefficient exists in this project's
  literature (same gap `hQ`/`hM` had before the sweep-arm-agitation fix), so
  this reuses `hM`/`aV` as-is, dimensionally already a first-order volumetric
  rate constant generic to whichever species crosses the same interface.

Both regimes accumulate top-to-bottom (solid flow order) into each cell's
own `X1_bulk`, starting from what FTRZ handed off (`X1_in`) -- replacing
what `core/dt_solver.py` used to carry forward from FTRZ completely
unchanged through every DCZ-spanned tray.

EXTRAPOLATION CAVEAT, stated not hidden: the Gianini isotherm's own tested
range is 15-70 C; DCZ's own operating temperatures currently reach higher.
The cited paper's own finding that temperature barely affects the isotherm
in ITS tested range is reassuring but doesn't cover that gap.

DOCUMENTED SIMPLIFICATION: total vapor mass flow (hence `u_V`/`hQ`/`hM`/`aV`)
stays the fixed, once-computed bed-transport quantity it already was --
condensation/sorption are tracked for the X1/mass-accounting purposes above
but do NOT feed back into the transport coefficients, the SAME simplification
already accepted for FTRZ's own hexane evaporation (a comparatively larger
relative mass-flow change than DCZ's water condensation, by inspection of
this scenario's own flows) without incident.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from dtdc_simulator.core import thermo
from dtdc_simulator.core.zones import particle as pt

# Free-water floor for the evaporative-pinning cap (see solve_dcz_zone step 2): above
# this moisture the meal holds free/loosely-bound water that flashes off to pin its
# temperature at the water saturation point; below it the meal is effectively dry and
# the toasting vapor superheats normally. ~2x the GAB monolayer (Xm~0.05-0.08), the
# capillary/free-water threshold. A magnitude, not a sharp physical constant.
_WET_PIN_FLOOR = 0.12

# Physical bounds for the particle temperature (K), a robustness clamp on the coupled
# water-sorption <-> energy march (see solve_dcz_zone step 1). The DT operates entirely
# within ~ -20..205 C; at every realistic operating point the particle sits far inside
# this band, so the clamp only engages for a deeply off-design iterate (e.g. an under-set
# sparge far below the ~110 kg/t design rate) where the lagged latent-heat feedback would
# otherwise run the temperature away toward 0 K / overflow. Bounding keeps the iterate
# finite so it degrades to a converged-but-imperfect result instead of crashing.
_TP_MIN_K = 250.0
_TP_MAX_K = 480.0


def _dominant_mode_extrapolation(
    x_k: "np.ndarray", f_k: "np.ndarray", f_prev: "np.ndarray", boost_max: float
) -> "np.ndarray | None":
    """Geometric (single-dominant-mode) extrapolation of a fixed-point iterate.

    After a Picard iteration's fast modes decay, its residual ``f = H(x) − x`` is
    dominated by ONE slow eigenmode with eigenvalue λ near 1 (the DCZ energy
    coupling, ρ≈0.9998), so ``f_k ≈ λ f_{k-1}``. We estimate λ from the residual
    alignment (a Rayleigh quotient) and take the geometric-series step toward the
    fixed point, ``x* = x_k + f_k / (1 − λ)``, with the boost ``1/(1−λ)`` capped
    at `boost_max`. Returns the extrapolated iterate, or ``None`` if the residual
    does not look like a clean slow mode (then the caller takes a plain step).

    This deliberately replaces multi-vector Anderson: the DCZ map is piecewise
    (water condensation/isotherm branches switch as cells cross their dew point),
    which makes Anderson's least-squares over residual DIFFERENCES ill-behaved. A
    single scalar extrapolation along the current residual direction is far more
    robust on such a map while still collapsing the O(10^4)-iteration crawl."""
    denom = float(f_prev @ f_prev)
    if denom <= 0.0:
        return None
    lam = float(f_k @ f_prev) / denom
    if not (0.5 < lam < 0.9999):
        return None  # not a clean, contracting slow mode -> plain step
    boost = min(1.0 / (1.0 - lam), boost_max)
    x_next = x_k + f_k * boost
    if not np.all(np.isfinite(x_next)):
        return None
    return x_next


# Physical convergence tolerances for the DCZ inner loop -- the solve is
# "converged" when its REPORTED EXIT KPIs (bottom-cell residual hexane, moisture
# and temperature) have stopped moving to within these, between consecutive
# passes. This replaces the old per-particle-layer 1e-5 criterion, which chased
# the near-neutral numerical mode (sub-0.01 K / sub-ppm changes that never move
# the reported outputs) and so never tripped within a practical iteration cap.
# These thresholds trip when the physics has actually settled (COAMO: ~cap 100).
EXIT_TOL_T = 1.0e-2  # K, bottom-cell bulk temperature stability
EXIT_TOL_X1 = 1.0e-4  # kg/kg dry solid, bottom-cell moisture stability
EXIT_TOL_X2 = 1.0e-6  # kg/kg dry solid (~1 ppm), bottom-cell residual-hexane stability


@dataclass(frozen=True)
class DCZConstants:
    diameter_m: float
    bed_height_m: float  # L_DCZ -- fixed geometry, NOT a free boundary (unlike L_FTRZ)
    hM: float  # m/s, explicit input (Re_epsilon correlation gap, FTRZ precedent)
    hQ: float  # W/(m2 K), explicit input
    aV: float  # m2/m3, specific interfacial area, explicit input
    D_ax: float  # m2/s, axial dispersion (vapor hexane, eq. A.36)
    k_mixL: float  # W/(m K), bed-scale solid/gas mixture conductivity (eq. A.32)
    rho_V: float  # kg/m3, vapor density (constant, per Coletto's own assumption)
    cp_V: float  # J/(kg K), vapor specific heat
    alpha_V: float  # bed voidage occupied by vapor
    alpha_L: float  # bed solid volume fraction (= 1 - alpha_V)
    particle: pt.ParticleConstants
    dH_vap_water: float  # J/kg, latent heat of water condensation (moisture balance, see below)
    antoine_water: thermo.AntoineParams  # dew-point calc, same params `zones/ftrz.py` uses
    luikov: thermo.LuikovParams  # subsaturated-regime sorption/desorption isotherm, see below
    water_diffusivity: float  # m2/s, water's own intraparticle diffusivity -- NOT hM, see
    # module docstring's "MOISTURE (H2O) BALANCE" section for why
    vapor_enthalpy_ref: thermo.VaporEnthalpyRef  # hexane specific enthalpy Ĥ2 for the
    # bed energy source SVm2·Ĥ2 (eq. A.34) -- same datum machinery zones/ftrz.py uses
    pressure_pa: float = thermo.ATM_PRESSURE_PA  # DT internal operating pressure for the
    # water dew-point / activity calc: the lower DT runs above atmospheric (sparge-tray
    # pressure drop 0.35-0.70 kg/cm2, Kemper 2019), which raises a_w = y_water*P/P_sat(T)
    # toward 1 so the near-saturated-steam meal ADSORBS water instead of drying. See ftrz.py.


@dataclass(frozen=True)
class VaporState:
    wV2: float  # hexane mass fraction in the vapor
    T: float  # K


@dataclass(frozen=True)
class DCZCellResult:
    vapor_top: VaporState  # this cell's vapor state facing the zone top
    particle: pt.ParticleState  # representative particle's full radial state at this cell
    X2_bulk: float  # diagnostic: volumetric-mean adsorbed+absorbed hexane (Fig. 9(a)-style)
    X1_bulk: float  # solid moisture (kg/kg dry solid) -- see module docstring's moisture section
    condensed_water_kg_s: float  # this cell's own condensation rate (diagnostic)


@dataclass(frozen=True)
class DCZZoneResult:
    cells: tuple[DCZCellResult, ...]  # top-to-bottom, matching phz.py/ftrz.py's convention
    iterations: int

    @property
    def vapor_out(self) -> VaporState:
        return self.cells[0].vapor_top

    @property
    def solid_out_X2(self) -> float:
        return self.cells[-1].X2_bulk

    @property
    def solid_out_X1(self) -> float:
        return self.cells[-1].X1_bulk

    @property
    def total_condensed_kg_s(self) -> float:
        return sum(cell.condensed_water_kg_s for cell in self.cells)


def bulk_temperature(cell: DCZCellResult, geometry: pt.ShellGeometry) -> float:
    """Volumetric-mean particle temperature (eq. 8) for a cell -- the bulk
    solid temperature a caller would report/compare against Fig. 9."""
    return pt.volumetric_mean(cell.particle.Tp, geometry.volumes)


def axial_laplacian(profile: tuple[float, ...], dz: float) -> tuple[float, ...]:
    """d2(profile)/dz2 via a 3-point finite difference, zero-gradient
    (Neumann) closure at both zone ends -- shared by the axial-dispersion
    (mass) and axial-conduction (energy) correction source terms."""
    n = len(profile)
    result = []
    for j in range(n):
        left = profile[j - 1] if j > 0 else profile[j]
        right = profile[j + 1] if j < n - 1 else profile[j]
        result.append((right - 2.0 * profile[j] + left) / (dz * dz))
    return tuple(result)


def solve_dcz_zone(
    nz: int,
    m_dry_kg_s: float,
    m_vapor_kg_s: float,
    T_L_sup: float,
    vapor_inf: VaporState,
    q_Iv_w_m3: float | tuple[float, ...],
    c: DCZConstants,
    X1_in: float = 0.0,
    outer_max_iter: int = 100,
    outer_tol: float = 1.0e-5,
    outer_relaxation: float = 0.5,
    residual_log: list[tuple[int, float, float]] | None = None,
) -> DCZZoneResult:
    """Solve the DCZ, `nz` axial cells top (FTRZ handoff) to bottom, via the
    Primary Internal Loop above. The particle's own initial condition at
    zone entry is `wpg2=1.0` uniformly (pores saturated with hexane vapor,
    `a_h=1` -- exactly FTRZ's own termination condition, `X2=X2,eq(TV,inf)`,
    which by eq. 5's definition IS the `a_h=1` state) and `Tp=T_L_sup`
    uniformly (FTRZ's exit solid temperature). `X1_in` is the solid moisture
    (kg/kg dry solid) entering DCZ from FTRZ above -- see module docstring's
    moisture-balance section for how it's accumulated forward through DCZ.

    `outer_relaxation` under-relaxes the vapor profile updates (steps 2 and
    4) between passes -- reusing `ModelParams.outer_relaxation`'s existing
    convention from the tray-sweep design (§7.9). Needed in practice: without
    it, this Gauss-Seidel coupling was observed during development to drift
    for hundreds of iterations before settling (the vapor and particle
    profiles overshooting and correcting each other pass-over-pass) rather
    than converging cleanly.

    `q_Iv_w_m3` may be a single scalar (uniform volumetric indirect heat
    across the whole zone, e.g. for standalone/illustrative use -- the
    original M2 Phase 3 convention, unchanged) or a length-`nz` per-cell
    profile (top-to-bottom, matching this function's own `j` cell indexing
    below): DCZ commonly spans several real trays with materially different
    `Q_indirect` (M2 Phase 4's `core/dt_solver.py` builds this profile from
    each tray's own duty divided by its own volume, rather than smearing all
    spanned trays' duties into one artificial average).
    """
    A_bed = math.pi / 4.0 * c.diameter_m**2
    dz = c.bed_height_m / nz
    q_Iv_profile = (
        q_Iv_w_m3 if isinstance(q_Iv_w_m3, tuple) else tuple(q_Iv_w_m3 for _ in range(nz))
    )
    if len(q_Iv_profile) != nz:
        raise ValueError(f"q_Iv_w_m3 profile length ({len(q_Iv_profile)}) must equal nz ({nz})")
    u_L = m_dry_kg_s / (c.particle.alpha_ps * c.alpha_L * c.particle.rho_ps * A_bed)
    u_V = m_vapor_kg_s / (c.rho_V * A_bed)
    dt = dz / u_L

    geometry = pt.build_shell_geometry(c.particle.r_P, c.particle.Np)
    zero_rates = tuple(0.0 for _ in range(c.particle.Np))

    initial_particle = pt.ParticleState(
        wpg2=tuple(1.0 for _ in range(c.particle.Np)),
        Tp=tuple(T_L_sup for _ in range(c.particle.Np)),
    )
    particles: list[pt.ParticleState] = [initial_particle for _ in range(nz)]
    vapor_wV2 = [vapor_inf.wV2 for _ in range(nz)]  # cell j's top-facing value
    vapor_T = [vapor_inf.T for _ in range(nz)]
    dwpg2_dt_prev: list[tuple[float, ...]] = [zero_rates for _ in range(nz)]
    kappa_w = 15.0 * c.water_diffusivity / c.particle.r_P**2  # 1/s, water's own bed-scale
    # equilibration rate -- the Glueckauf linear-driving-force (LDF) approximation for a
    # diffusing sphere (a standard, well-established result, not invented for this project).
    # Found this session (DECISIONS.md): reusing hexane's own hM*aV here (~0.05-0.17 m/s * ~1800
    # m2/m3, a Faner-correlation value tuned for hexane VAPOR transport) was ~25-100x too fast
    # once this relaxation was genuinely coupled to an energy balance (see `water_latent_w_m3`
    # below) -- confirmed directly: it inverted the basic "more indirect duty -> hotter DT exit"
    # relationship (doubled duty gave a COOLER converged profile than halved duty). The water
    # -specific CONVECTIVE coefficient tried next (literature_sources/Touffet_Moisture_sorption_
    # and_diffusion_in_pellet_animal_feed.pdf, 2026, dynamic vapor sorption on pelleted animal
    # feed) turned out to be insufficient too -- their own Biot-number analysis explains why:
    # INTERNAL particle diffusion, not external convection, is the actual rate-limiting step
    # (confirmed directly: even their measured k, combined with this project's own bed geometry,
    # still implied near-instant equilibration, ~500x faster than DCZ's own per-cell residence).
    # Using their own diffusion coefficient instead (D, not k) via the LDF form above lands in a
    # genuinely meaningful, neither-instant-nor-negligible band relative to DCZ's own residence
    # time -- confirmed this session, see DECISIONS.md for the measured comparison. NOT combined
    # with the convective coefficient in a full two-resistance series model (internal diffusion
    # dominates enough here that the convective term's own contribution is negligible by
    # comparison) -- a leaner, still real-data-grounded fix.
    X1_profile = [X1_in for _ in range(nz)]  # solid moisture, top (j=0) to bottom (j=nz-1)
    # Water saturation temperature at the local (elevated, sparge) pressure -- the ceiling a
    # WET meal surface can reach: free/loosely-bound water flashes off (evaporative pinning)
    # rather than letting the meal superheat past it. Used both to cap the surface temperature
    # at which the moisture equilibrium a_w is evaluated (step 4.5) and to pin the bed
    # temperature while the meal stays wet (step 2). A wet meal in near-saturated steam sits at
    # saturation, NOT at the superheated toasting-vapor bulk temperature. (Y_V2=0 -> pure water.)
    T_sat_water = thermo.dew_point_temperature(0.0, c.antoine_water, P=c.pressure_pa)
    water_latent_w_m3 = [0.0 for _ in range(nz)]  # lagged one iteration, "cold start" zeros
    # Water REMOVED from (adsorption, >0) or ADDED to (desorption, <0) the vapor phase by the
    # isotherm relaxation below, kg/(s*m3), same lag category as `water_latent_w_m3` -- without
    # this, step 4's own vapor mass balance never reflects the isotherm branch's mass transfer at
    # all, so adsorption could pull an unbounded amount of "moisture" out of a fixed local wV2
    # without ever depleting it (and desorption could add moisture without ever diluting the
    # local humidity) -- confirmed this session as the actual root cause of `water_latent_w_m3`
    # reaching multiples of `q_Iv_profile`'s own magnitude (a genuine mass-conservation gap, not
    # just a magnitude-tuning issue): the condensation branches already debit `water_remaining_kg_s`
    # for exactly this reason, but the subsaturated isotherm branch had no equivalent.
    water_mass_rate_w_m3 = [0.0 for _ in range(nz)]

    iterations = 0
    # Previous-pass exit KPIs (bottom cell) for the physical convergence test;
    # seeded at inf so the first pass can never trip.
    prev_x2_exit = math.inf
    prev_T_exit = math.inf
    prev_x1_exit = math.inf

    # Convergence acceleration of the vapor TEMPERATURE profile -- the
    # near-neutral mode (ρ≈0.9998) lives entirely in the energy coupling
    # (vapor_T <-> particle Tp); the hexane mass profile (vapor_wV2) already
    # converges fast, so it is left on plain relaxed Picard. See
    # `_dominant_mode_extrapolation`.
    #  - `acc_start`: plain Picard first, so the fast modes decay and the
    #    residual becomes a clean single slow mode before we extrapolate (an
    #    early extrapolation can overshoot to a cold T where the GAB isotherm is
    #    invalid, a_h ≥ 1/K).
    #  - `acc_boost_max`: cap on the geometric boost 1/(1−λ).
    #  - residual-decrease safeguard + `acc_cooldown`: if an accelerated step
    #    grew the residual, take several plain passes before re-engaging. So the
    #    accelerator can never do worse than base Picard.
    #  - `[_T_lo, _T_hi]`: physical guard band; an out-of-band extrapolation is
    #    discarded. `_reset_prev`: force a plain pass after each extrapolation so
    #    the next λ estimate uses clean consecutive residuals.
    acc_start = 8
    acc_boost_max = 40.0
    _T_lo = min(vapor_inf.T, T_L_sup) - 40.0
    _T_hi = max(vapor_inf.T, T_L_sup) + 80.0
    acc_f_prev: np.ndarray | None = None
    acc_prev_res = math.inf
    acc_cooldown = 0

    for iterations in range(1, outer_max_iter + 1):
        # Accelerator iterate x_k = the vapor TEMPERATURE profile ENTERING this
        # pass. The relaxed iteration body below is the fixed-point map H; its
        # output h_k is captured after step 4.5 and fed to the extrapolation.
        acc_x_k = np.array(vapor_T)
        # -- axial correction sources, lagged from the previous iteration's profile --
        Tp_bulk_profile = tuple(
            pt.volumetric_mean(particles[j].Tp, geometry.volumes) for j in range(nz)
        )
        q_condL = axial_laplacian(Tp_bulk_profile, dz)
        q_condL = tuple(c.k_mixL * q for q in q_condL)
        m_ax_net = axial_laplacian(tuple(vapor_wV2), dz)
        m_ax_net = tuple(c.alpha_V * c.D_ax * c.rho_V * m for m in m_ax_net)

        # 1. energy balance at particle scale (top -> bottom). Each outer
        # pass re-marches a FRESH Tp cascade from the zone's own entry
        # condition through all nz cells in sequence -- cell j's particle
        # must reflect (j+1)*dt of accumulated residence time, not "however
        # many outer iterations have run so far" (those are a convergence
        # device for the vapor<->particle coupling, not a proxy for
        # residence time; conflating the two was caught and fixed during
        # development -- an earlier draft let each cell's state persist and
        # advance by one dt per OUTER iteration, so every cell ended up with
        # identical residence time regardless of axial position). Each
        # cell's own wpg2 (from the previous iteration's mass cascade, step
        # 3 below) is carried through unchanged -- only Tp cascades here.
        new_particles_energy = []
        running_Tp = initial_particle.Tp
        for j in range(nz):
            seed = pt.ParticleState(wpg2=particles[j].wpg2, Tp=running_Tp)
            # eq. A.30 sorption/desorption heat source for the PARTICLE energy
            # march (step 1). No longer also fed to the vapor balance -- the
            # vapor now uses Coletto's own SVm2·Ĥ2 mass-enthalpy term (step 2),
            # not this particle-volume sorption sink.
            sorption_sources = pt.sorption_heat_source_per_layer_w_m3(
                seed, dwpg2_dt_prev[j], c.particle
            )
            full_sources = tuple(s + q_condL[j] for s in sorption_sources)
            updated, _ = pt.march_particle_energy(
                seed,
                vapor_T[j],
                c.hQ,
                full_sources,
                dt,
                geometry,
                c.particle,
                X1=X1_profile[j],  # lagged one outer iteration, same category as dwpg2_dt_prev
            )
            # Robustness clamp to a physical DT temperature band (see _TP_MIN_K/_TP_MAX_K):
            # keeps the coupled sorption<->energy iterate finite at deeply off-design inputs
            # so it degrades gracefully instead of running away to overflow. Never engages
            # at realistic operating points.
            if updated.Tp[0] < _TP_MIN_K or updated.Tp[-1] > _TP_MAX_K or any(
                t < _TP_MIN_K or t > _TP_MAX_K for t in updated.Tp
            ):
                updated = pt.ParticleState(
                    wpg2=updated.wpg2,
                    Tp=tuple(min(max(t, _TP_MIN_K), _TP_MAX_K) for t in updated.Tp),
                )
            new_particles_energy.append(updated)
            running_Tp = updated.Tp
        particles = new_particles_energy

        # 2. energy balance at bed scale (bottom -> top). A per-cell IMPLICIT
        # (backward) relaxation step -- not the naive explicit forward step
        # eq. A.35 might suggest -- because the particle<->vapor transfer
        # coefficient is typically stiff relative to a practical cell size
        # (its own relaxation length can be much shorter than dz); backward
        # Euler is unconditionally stable for this linear relaxation form,
        # mirroring why the particle-scale march is implicit for the same
        # stiffness reason.
        denom_e = c.alpha_V * u_V * c.rho_V * c.cp_V
        new_vapor_T = [0.0] * nz
        condensed_kg_s = [0.0] * nz
        T_running = vapor_inf.T
        water_remaining_kg_s = (1.0 - vapor_inf.wV2) * m_vapor_kg_s
        for j in range(nz - 1, -1, -1):
            Tp12 = pt.outer_layer_value(particles[j].Tp)
            wpg2_12 = pt.outer_layer_value(particles[j].wpg2)
            kappa_e = c.hQ * c.aV * c.alpha_L
            # eq. A.34/A.37 vapor energy source, EXACTLY as Coletto prints it
            # (D1, GROUNDING_MATRIX.md):
            #   S_VQ = −a_v α_L J_QR·ř  +  SVm2·Ĥ2  +  q̇_Iv  +  ṁ'_ax·Ĥ2
            # - `−a_v α_L J_QR` (particle<->vapor convection) is the
            #   `kappa_e*(Tp12 − TV)` relaxation just below.
            # - `SVm2·Ĥ2 + ṁ'_ax·Ĥ2` is the enthalpy carried by the hexane MASS
            #   transferred into the vapor. `SVm2` is the SAME bed<->particle
            #   hexane transfer the mass balance (step 4) uses, `hM ρV aV αL
            #   (wpg2_12 − wV2)`; `ṁ'_ax` is the axial hexane flux `m_ax_net`;
            #   `Ĥ2` is the hexane specific vapor enthalpy (same datum machinery
            #   zones/ftrz.py uses, evaluated at the local vapor temperature).
            # This REPLACES the previous non-paper `−sorption_sink·α_L`
            # substitution, which injected the particle's FULL heat of sorption
            # (mostly latent) into the vapor's SENSIBLE temperature balance
            # (A.25 LHS is ρV·CPV·TV) -> the over-heating fixed point. Paired
            # with particle.py's restored literal A.30 sign; validated as a PAIR.
            # `water_latent_w_m3[j]` is the project's own (non-Coletto) water
            # sorption/condensation latent term, lagged one outer iteration.
            # Ĥ2 is the hexane's SENSIBLE vapor enthalpy relative to the local
            # vapor temperature: the bed-scale transfer moves hexane that is
            # ALREADY vapor (pore gas -> bulk vapor), so no phase change occurs
            # here -- the desorption phase change lives entirely at the particle
            # scale (A.30). It arrives at the particle-surface temperature Tp12
            # and equilibrates into the vapor at TV, contributing only
            # `cp_hex·(Tp12 − TV)` of sensible heat. (A.25's LHS is the SENSIBLE
            # ρV·CPV·TV; putting the hexane's LATENT `dH_vap_hexane` here instead
            # would inject phase-change energy into a sensible balance -- tested
            # directly this session: it drove the DT meal to ~187 C. The latent
            # heat is carried by the composition wV2, tracked by the mass
            # balance A.24, not by this temperature balance.)
            cp_hex = c.vapor_enthalpy_ref.cp_hexane_vapor
            SVm2 = c.hM * c.rho_V * c.aV * c.alpha_L * (wpg2_12 - vapor_wV2[j])
            source = (
                q_Iv_profile[j]
                + (SVm2 + m_ax_net[j]) * cp_hex * (Tp12 - vapor_T[j])
                + water_latent_w_m3[j]
            )
            T_in = T_running

            # MOISTURE (H2O) BALANCE, see module docstring. Two distinct
            # supersaturation checks are needed, not one:
            wV2_j = vapor_wV2[j]
            Y_V2_j = wV2_j / (1.0 - wV2_j)
            T_dew_j = thermo.dew_point_temperature(Y_V2_j, c.antoine_water, P=c.pressure_pa)

            # (a) the INFLOW itself may already be below its own dew point
            # (e.g. SP1's own steam+upstream-vapor mix, `dt_solver.py`'s
            # `T_bottom`) -- a coarse cell's own strong indirect duty can
            # then re-superheat it within that SAME cell, so checking only
            # the cell's OUTPUT (b, below) would silently miss condensation
            # that physically must happen right at entry, before any
            # convective heating. Flash-condense against T_in alone (no
            # kappa_e/source terms -- those belong to the cell's own march,
            # not this pre-conditioning step): released latent heat reheats
            # the remaining (approximately unchanged mass) water stream by
            # `T_dew_j - T_in`.
            if T_in < T_dew_j and water_remaining_kg_s > 0.0:
                m_flash_kg_s = water_remaining_kg_s * c.cp_V * (T_dew_j - T_in) / c.dH_vap_water
                m_flash_kg_s = min(max(m_flash_kg_s, 0.0), water_remaining_kg_s)
                water_remaining_kg_s -= m_flash_kg_s
                condensed_kg_s[j] += m_flash_kg_s
                T_in = T_dew_j

            relax_factor = 1.0 + dz * kappa_e / denom_e
            T_candidate = (T_in + dz * (kappa_e * Tp12 + source) / denom_e) / relax_factor

            # (b) the cell's OWN convective/duty/sorption balance may cool it
            # back below the (possibly just-corrected) dew point -- cap and
            # back-solve the condensed mass the SAME relaxation equation
            # implies, rather than let the vapor cool below what its own
            # composition allows. Closed-form (the equation is linear in
            # `source`), not a root-find -- unlike `zones/ftrz.py`'s own
            # V-SAT branch, which needs `brentq` because its energy balance
            # is nonlinear in the condensed mass (composition-dependent
            # enthalpy); this one isn't.
            if T_candidate < T_dew_j and water_remaining_kg_s > 0.0:
                source_cond_needed_w_m3 = (
                    (T_dew_j * relax_factor - T_in) * denom_e / dz - kappa_e * Tp12 - source
                )
                m_cond_kg_s = source_cond_needed_w_m3 * A_bed * dz / c.dH_vap_water
                m_cond_kg_s = min(max(m_cond_kg_s, 0.0), water_remaining_kg_s)
                water_remaining_kg_s -= m_cond_kg_s
                condensed_kg_s[j] += m_cond_kg_s
                source_cond_actual_w_m3 = m_cond_kg_s * c.dH_vap_water / (A_bed * dz)
                T_running = (
                    T_in + dz * (kappa_e * Tp12 + source + source_cond_actual_w_m3) / denom_e
                ) / relax_factor
            else:
                T_running = T_candidate
            # Evaporative pinning (see T_sat_water above): while the meal still holds free/
            # loosely-bound water, its surface and the near-saturated vapor in contact cannot
            # superheat past the water saturation temperature at the local (sparge) pressure --
            # excess heat flashes meal moisture to steam rather than raising T. Applied while
            # the (lagged) meal moisture stays above the free-water floor; mirrors the FTRZ
            # T_L pinning. The latent heat absorbing the excess is the wet meal's own phase-
            # change buffer (documented simplification, same category as the FTRZ's algebraic
            # T_L cap); the moisture the meal then holds is set by the isotherm at this pinned,
            # near-saturated a_w in step 4.5. Once the meal genuinely dries below the floor the
            # cap releases and the toasting vapor superheats normally.
            if X1_profile[j] > _WET_PIN_FLOOR and T_running > T_sat_water:
                T_running = T_sat_water
            new_vapor_T[j] = T_running
        vapor_T = [vapor_T[j] + outer_relaxation * (new_vapor_T[j] - vapor_T[j]) for j in range(nz)]

        # 3. mass balance at particle scale (top -> bottom). Same cascade
        # logic as step 1: a fresh wpg2 cascade from the zone entry
        # condition through all nz cells, each seeded with that cell's own
        # (just-updated in step 1) Tp.
        new_particles_mass = []
        new_rates = []
        running_wpg2 = initial_particle.wpg2
        for j in range(nz):
            seed = pt.ParticleState(wpg2=running_wpg2, Tp=particles[j].Tp)
            updated, diag = pt.march_particle_mass(
                seed, vapor_wV2[j], c.hM, c.rho_V, dt, geometry, c.particle
            )
            new_particles_mass.append(updated)
            new_rates.append(diag.dwpg2_dt)
            running_wpg2 = updated.wpg2
        particles = new_particles_mass
        dwpg2_dt_prev = new_rates

        # 4. mass balance at bed scale (bottom -> top), same implicit
        # per-cell relaxation as step 2, for the same stiffness reason.
        # `+ water_mass_rate_w_m3[j]` (lagged, from step 4.5) `+
        # condensed_kg_s[j]/(A_bed*dz)` (SAME iteration -- step 2 already
        # computed `condensed_kg_s` above, no lag needed here): `vapor_wV2`
        # is HEXANE's own mass fraction (see `water_remaining_kg_s = (1-wV2)*
        # m_vapor_kg_s` above), so water LEAVING the vapor -- via adsorption
        # OR condensation, same direction for both -- raises hexane's own
        # share of what remains, i.e. RAISES `wV2` (desorption does the
        # opposite). Found this session (DECISIONS.md): condensation's own
        # mass removal was credited into the SOLID's moisture (step 4.5) and
        # into THIS cell's own energy balance (step 2), but never actually
        # debited from `vapor_wV2` here -- the exact same class of gap the
        # isotherm branch had before `water_mass_rate_w_m3` was added, just
        # for the other (supersaturated) branch. See `water_mass_rate_w_m3`'s
        # own init comment above for why this class of term matters at all
        # (mass conservation, not an optional refinement).
        denom_m = c.alpha_V * u_V * c.rho_V
        kappa_m = c.hM * c.rho_V * c.aV * c.alpha_L
        new_vapor_wV2 = [0.0] * nz
        wV2_running = vapor_inf.wV2
        for j in range(nz - 1, -1, -1):
            wpg2_12 = pt.outer_layer_value(particles[j].wpg2)
            source_m = (
                kappa_m * wpg2_12
                + m_ax_net[j]
                + water_mass_rate_w_m3[j]
                + condensed_kg_s[j] / (A_bed * dz)
            )
            wV2_running = (wV2_running + dz * source_m / denom_m) / (1.0 + dz * kappa_m / denom_m)
            new_vapor_wV2[j] = wV2_running
        # Clamp to the physical [0,1] domain, same precedent (and same
        # reason) as `march_particle_mass`'s own `wpg2_clamped`: the linear
        # relaxation can drift past a boundary -- found this session via
        # `core/balance.py`'s independent mass-conservation check, on a
        # strongly-DESORBING illustrative case (a near-hexane-free vapor
        # inlet, `wV2~0.0001`, diluted further by a large net desorption
        # flux): reported `wV2` went slightly negative (order 1e-4). This
        # clamp keeps `wV2` in its physical domain regardless, but is NOT a
        # full fix -- the same investigation confirmed a materially larger,
        # separate, and still-UNRESOLVED gap in how much hexane this cascade
        # (via `kappa_m*wpg2_12`) credits into the vapor at all, traced to
        # `march_particle_mass`'s own FVM (see that module's docstring and
        # DECISIONS.md's "DCZ particle hexane mass-conservation gap" entry).
        vapor_wV2 = [
            min(1.0 - 1.0e-9, max(0.0, vapor_wV2[j] + outer_relaxation * (new_vapor_wV2[j] - vapor_wV2[j])))
            for j in range(nz)
        ]

        # 4.5. solid moisture at bed scale (top -> bottom, matching solid
        # flow direction -- see module docstring's "MOISTURE (H2O) BALANCE").
        # Uses THIS iteration's own just-updated `vapor_wV2`/`vapor_T` (steps
        # 2/4 above) and `condensed_kg_s` (step 2's own supersaturation
        # branches); produces `water_latent_w_m3`, consumed by step 2 of the
        # NEXT outer iteration (the same one-iteration lag `q_condL`/
        # `m_ax_net` already use) so the isotherm-driven regime's own latent
        # heat genuinely feeds back into temperature, not just mass -- unlike
        # the condensation branches (already energy-coupled within step 2
        # itself), the subsaturated isotherm relaxation has no such coupling
        # without this separate pass.
        new_X1_profile = [0.0] * nz
        new_water_latent_w_m3 = [0.0] * nz
        new_water_mass_rate_w_m3 = [0.0] * nz
        X1_running = X1_in
        # Water-availability budget (mass conservation): the solid can adsorb at
        # most the water the vapor carries into the zone. Without this cap a
        # high-a_w regime (near-saturated steam, e.g. under an elevated DT
        # pressure) drives the subsaturated adsorption below to pull MORE water
        # onto the meal than exists in the vapor, sending the vapor's own water
        # flow negative (found via the FTRZ handoff crash under a raised
        # pressure). Condensation (step 2) is already limited by its own
        # `water_remaining_kg_s`; this budget adds the same discipline to the
        # subsaturated branch and shares the running total across both.
        #
        # The whole zone's condensation is PRE-COUNTED into the shared budget here,
        # before the isotherm adsorption draws on it: the vapor rises bottom->top, so
        # water condensed low (at the sparge) is gone before it reaches the meal higher
        # up. Counting condensation only as each condensed cell is REACHED walking the
        # solid top->bottom instead let a top isotherm cell adsorb against the FULL
        # budget before a bottom condensed cell debited it -- both branches drawing the
        # same water, so the meal could gain more than the steam actually supplied
        # (found at an under-set sparge: DT exit ~29%wb, ~2.8 kg/s water conjured from
        # nothing, dome vapor water driven to 0). Pre-counting caps the combined
        # condensation+adsorption at the actual inflowing vapor water. At the calibrated
        # operating point the sparge is strong enough that the meal reaches ~19% via the
        # isotherm alone and this is slack; it binds only when the sparge is weak.
        water_available_kg_s = (1.0 - vapor_inf.wV2) * m_vapor_kg_s
        cum_water_to_solid_kg_s = sum(condensed_kg_s)
        for j in range(nz):
            if condensed_kg_s[j] > 0.0:
                # a_w >= 1 (supersaturated) this cell -- mass-conservative
                # credit from the EXACT energy balance step 2 already solved.
                # `new_water_latent_w_m3[j]`/`new_water_mass_rate_w_m3[j]`
                # stay 0.0 here: step 2's own `source_cond_actual_w_m3` (line
                # ~471) already credited this SAME condensed mass's latent
                # heat into the vapor energy balance THIS iteration, and
                # `water_remaining_kg_s` already debited it from the vapor
                # mass balance THIS iteration too -- recomputing either here
                # would double-count the identical condensation event
                # (confirmed directly: caused `water_latent_w_m3` to grow to
                # several times `q_Iv_profile`'s own magnitude and invert the
                # basic more-duty-=hotter relationship in
                # `test_model.py::test_more_steam_raises_dt_target_temperature`).
                X1_new = X1_running + condensed_kg_s[j] / m_dry_kg_s
                # (condensed mass already pre-counted into cum_water_to_solid_kg_s above)
            else:
                # a_w < 1 (subsaturated) -- bidirectional adsorption/
                # desorption toward the isotherm's own equilibrium, an
                # implicit relaxation over this cell's own residence `dt`,
                # the same form the particle-scale marches already use.
                Y_V2_j = vapor_wV2[j] / (1.0 - vapor_wV2[j])
                # Evaporative pinning: the WET meal surface sits at the water saturation
                # temperature (min of the superheated toasting vapor and T_sat), NOT the bulk
                # vapor temp -- so a_w = y_water*P/P_sat(T_sat) = y_water, holding ~19% in
                # near-pure steam instead of drying against a 120+ C vapor. Mirrors the FTRZ
                # T_L pinning (zones/ftrz.py). Once the meal genuinely dries out and the vapor
                # is hexane-rich, y_water (hence a_w) falls and real drying resumes.
                T_surface = min(vapor_T[j], T_sat_water)
                a_w = thermo.water_activity(Y_V2_j, T_surface, c.antoine_water, P=c.pressure_pa)
                # Clamp to the Gianini paper's OWN highest tested UR (0.799,
                # its KCl data point), not 1.0 -- DCZ's vapor is nearly pure
                # steam, so a_w sits close to 1 almost everywhere this zone
                # operates, exactly the isotherm's own UNTESTED tail (the
                # fitted curve climbs steeply toward its asymptote A1=0.88 as
                # a_w->1 with zero supporting data there -- confirmed this
                # session: evaluating it unclamped gave Xe>0.5, a pure
                # extrapolation artifact). Beyond this ceiling, additional
                # moisture only comes from the a_w>=1 condensation branch.
                a_w = min(max(a_w, 1.0e-9), thermo.LUIKOV_MAX_VALIDATED_UR)
                Xe = thermo.luikov_equilibrium_moisture(a_w, c.luikov)
                X1_new = (X1_running + dt * kappa_w * Xe) / (1.0 + dt * kappa_w)
                mass_rate_kg_s = m_dry_kg_s * (X1_new - X1_running)
                # ADSORPTION (mass_rate > 0) is capped to the vapor water still
                # available; DESORPTION (< 0) returns water to the vapor and is
                # unconstrained (it replenishes the budget).
                if mass_rate_kg_s > 0.0:
                    headroom_kg_s = max(water_available_kg_s - cum_water_to_solid_kg_s, 0.0)
                    if mass_rate_kg_s > headroom_kg_s:
                        mass_rate_kg_s = headroom_kg_s
                        X1_new = X1_running + mass_rate_kg_s / m_dry_kg_s
                cum_water_to_solid_kg_s += mass_rate_kg_s
                new_water_latent_w_m3[j] = mass_rate_kg_s * c.dH_vap_water / (A_bed * dz)
                new_water_mass_rate_w_m3[j] = mass_rate_kg_s / (A_bed * dz)
            new_X1_profile[j] = X1_new
            X1_running = X1_new
        X1_profile = [
            X1_profile[j] + outer_relaxation * (new_X1_profile[j] - X1_profile[j])
            for j in range(nz)
        ]
        water_latent_w_m3 = [
            water_latent_w_m3[j] + outer_relaxation * (new_water_latent_w_m3[j] - water_latent_w_m3[j])
            for j in range(nz)
        ]
        water_mass_rate_w_m3 = [
            water_mass_rate_w_m3[j]
            + outer_relaxation * (new_water_mass_rate_w_m3[j] - water_mass_rate_w_m3[j])
            for j in range(nz)
        ]

        # -- convergence acceleration of the vapor TEMPERATURE profile --
        # h_k = H(x_k): the vapor_T the relaxed iteration body just produced.
        # f_k = h_k − x_k is the fixed-point residual; ||f_k|| → 0 at convergence.
        acc_h_k = np.array(vapor_T)  # h_k = H(x_k)
        f_k = acc_h_k - acc_x_k
        res_vap = float(np.max(np.abs(f_k)))
        apply_acc = iterations >= acc_start and acc_cooldown == 0 and acc_f_prev is not None
        if apply_acc and res_vap > 1.05 * acc_prev_res:
            # Residual GREW -> the previous extrapolation overshot. Take several
            # plain (relaxed-Picard) passes to re-settle before re-engaging.
            acc_cooldown = acc_start
            acc_f_prev = None
            apply_acc = False
        if acc_cooldown > 0:
            acc_cooldown -= 1
        if apply_acc:
            x_next = _dominant_mode_extrapolation(acc_x_k, f_k, acc_f_prev, acc_boost_max)
            if x_next is not None and all(
                math.isfinite(t) and _T_lo <= t <= _T_hi for t in x_next
            ):
                vapor_T = [float(t) for t in x_next]
                acc_f_prev = None  # force a plain pass next, for a clean λ estimate
            else:
                acc_f_prev = f_k
        else:
            acc_f_prev = f_k
        # vapor_wV2 is ALWAYS the plain relaxed step (not accelerated -- init note).
        acc_prev_res = res_vap

        # 5. physical convergence check: the bottom cell's REPORTED exit KPIs
        # (residual hexane X2_bulk, moisture X1, bulk temperature) stable to
        # within their physical tolerances between passes. (`outer_tol` is
        # retained in the signature for backward compatibility but is superseded
        # by these KPI thresholds -- see EXIT_TOL_* and their comment.)
        last = particles[-1]
        x2_exit = pt.volumetric_mean(
            tuple(_x2_so(w, t, c.particle) for w, t in zip(last.wpg2, last.Tp)),
            geometry.volumes,
        )
        T_exit = pt.volumetric_mean(last.Tp, geometry.volumes)
        x1_exit = X1_profile[-1]
        d_x2 = abs(x2_exit - prev_x2_exit)
        d_T = abs(T_exit - prev_T_exit)
        d_x1 = abs(x1_exit - prev_x1_exit)
        prev_x2_exit, prev_T_exit, prev_x1_exit = x2_exit, T_exit, x1_exit
        if residual_log is not None:
            residual_log.append((iterations, d_x2, d_T))
        if d_x2 <= EXIT_TOL_X2 and d_T <= EXIT_TOL_T and d_x1 <= EXIT_TOL_X1:
            break

    cells = []
    for j in range(nz):
        X2_bulk = pt.volumetric_mean(
            tuple(
                _x2_so(wpg2_i, Tp_i, c.particle)
                for wpg2_i, Tp_i in zip(particles[j].wpg2, particles[j].Tp)
            ),
            geometry.volumes,
        )
        # X1_profile/condensed_kg_s are already the FINAL outer iteration's
        # own converged values (step 4.5 above, top-j=0-to-bottom-j=nz-1,
        # same order as this loop) -- no separate recomputation needed here.
        cells.append(
            DCZCellResult(
                vapor_top=VaporState(wV2=vapor_wV2[j], T=vapor_T[j]),
                particle=particles[j],
                X2_bulk=X2_bulk,
                X1_bulk=X1_profile[j],
                condensed_water_kg_s=condensed_kg_s[j],
            )
        )
    return DCZZoneResult(cells=tuple(cells), iterations=iterations)


def _x2_so(wpg2: float, T: float, pc: pt.ParticleConstants) -> float:
    """X2,so(wpg2,T) (eq. A.26) -- used only for the zone's own `X2_bulk`
    diagnostic (Fig. 9(a)-style reporting), dropping the free pore-gas
    hexane term (eq. 5's `Ypg2` -- the smallest of the three contributions
    at equilibrium) as a documented reporting simplification."""
    return thermo.gab_hexane_content(wpg2, T, pc.gab) + pc.X3 * thermo.oil_hexane_content(
        wpg2, pc.oil
    )
