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
     particle's conserved hexane loss and the same-pass water transfer,
     producing explicit component-flow and derived-composition profiles.
  5. Convergence check on every exported boundary plus maximum full vapor
     temperature/component-flow profile changes. Not converged -> repeat from
     the complete lagged state.

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

Two additive mechanisms use the same water-activity state. Supersaturated
vapor is pinned to its dew point and the required bulk condensate is
back-solved from the cell energy balance. The resulting wet solid then makes
a finite-rate bidirectional adjustment toward `Xe(a_w)` over its local
residence. The condensation active-set mass is relaxed between iterations;
an actively condensing saturated cell cannot evaporate its new free
condensate against the clamped bound-water isotherm.

Both regimes accumulate top-to-bottom (solid flow order) into each cell's
own `X1_bulk`, starting from what FTRZ handed off (`X1_in`) -- replacing
what `core/dt_solver.py` used to carry forward from FTRZ completely
unchanged through every DCZ-spanned tray.

EXTRAPOLATION CAVEAT, stated not hidden: the Gianini isotherm's own tested
range is 15-70 C; DCZ's own operating temperatures currently reach higher.
The cited paper's own finding that temperature barely affects the isotherm
in ITS tested range is reassuring but doesn't cover that gap.

VARIABLE VAPOR FLOW (2026-07-23): water and hexane are marched as explicit
component mass flows. Condensation/sorption therefore reduce water kg/s and
particle transfer increases hexane kg/s cell by cell; local vapor heat
capacity/velocity use the resulting total flow. `hQ`/`hM`/`aV` remain the
once-computed bed coefficients supplied by the caller, but the former
fixed-total-flow composition approximation is removed.
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
EXIT_TOL_FLOW = 1.0e-4  # kg/s, top component-flow stability
PROFILE_TOL_T = 2.0e-2  # K, full vapor-temperature profile fixed-point residual
PROFILE_TOL_FLOW = 2.0e-3  # kg/s, full internal component-flow profile stability


@dataclass(frozen=True)
class DCZResiduals:
    """Final fixed-point residuals for every state exported by the DCZ map."""

    solid_out_X2: float = math.inf
    solid_out_T: float = math.inf
    solid_out_X1: float = math.inf
    vapor_top_T: float = math.inf
    vapor_top_water_flow: float = math.inf
    vapor_top_hexane_flow: float = math.inf
    vapor_profile_T_max: float = math.inf
    vapor_profile_water_flow_max: float = math.inf
    vapor_profile_hexane_flow_max: float = math.inf

    @property
    def maximum_scaled(self) -> float:
        """Largest residual normalized by its physical convergence tolerance."""
        return max(
            self.solid_out_X2 / EXIT_TOL_X2,
            self.solid_out_T / EXIT_TOL_T,
            self.solid_out_X1 / EXIT_TOL_X1,
            self.vapor_top_T / PROFILE_TOL_T,
            self.vapor_top_water_flow / EXIT_TOL_FLOW,
            self.vapor_top_hexane_flow / EXIT_TOL_FLOW,
            self.vapor_profile_T_max / PROFILE_TOL_T,
            self.vapor_profile_water_flow_max / PROFILE_TOL_FLOW,
            self.vapor_profile_hexane_flow_max / PROFILE_TOL_FLOW,
        )


@dataclass(frozen=True)
class DCZWarmStart:
    """Complete lagged state of the nested DCZ fixed-point iteration.

    The physical residence-time marches still start from the zone inlet on
    every pass.  This object only preserves the iteration's lagged profiles,
    so reusing it cannot accidentally add residence time.
    """

    bed_height_m: float
    particles: tuple[pt.ParticleState, ...]
    vapor_T: tuple[float, ...]
    vapor_water_kg_s: tuple[float, ...]
    vapor_hexane_kg_s: tuple[float, ...]
    X1: tuple[float, ...]
    condensed_water_kg_s: tuple[float, ...]
    water_latent_w_m3: tuple[float, ...]
    dwpg2_dt: tuple[tuple[float, ...], ...]
    temperature_relaxation: float = 0.5
    hexane_relaxation: float = 0.5
    water_relaxation: float = 0.1
    water_active_set: tuple[tuple[bool, bool], ...] | None = None


@dataclass
class _AdaptiveRelaxation:
    """Residual-monotonic scalar damping for one coupled variable family."""

    value: float
    minimum: float
    maximum: float
    previous_norm: float = math.inf

    def observe(self, norm: float, active_set_changed: bool = False) -> None:
        if not math.isfinite(norm):
            self.value = max(self.minimum, 0.5 * self.value)
        elif active_set_changed or norm > 1.05 * self.previous_norm:
            self.value = max(self.minimum, 0.5 * self.value)
        elif norm < 0.85 * self.previous_norm:
            self.value = min(self.maximum, 1.08 * self.value)
        self.previous_norm = norm


def _interp_profile(
    values: tuple[float, ...], source_height_m: float, target_n: int, target_height_m: float
) -> list[float]:
    """Interpolate cell-centred warm data after the moving boundary remeshes DCZ."""
    source_n = len(values)
    if source_n == 0:
        raise ValueError("DCZ warm-start profiles must be nonempty")
    source_z = (np.arange(source_n, dtype=float) + 0.5) * source_height_m / source_n
    target_z = (np.arange(target_n, dtype=float) + 0.5) * target_height_m / target_n
    return [
        float(v)
        for v in np.interp(target_z, source_z, np.asarray(values, dtype=float), left=values[0], right=values[-1])
    ]


def _resample_warm_start(
    warm: DCZWarmStart, target_n: int, target_height_m: float, particle_layers: int
) -> DCZWarmStart:
    if len(warm.particles) == 0 or any(
        len(p.wpg2) != particle_layers or len(p.Tp) != particle_layers for p in warm.particles
    ):
        raise ValueError("DCZ warm-start particle shape does not match the configured mesh")

    def particle_component(name: str, layer: int) -> tuple[float, ...]:
        return tuple(getattr(p, name)[layer] for p in warm.particles)

    wpg_layers = [
        _interp_profile(particle_component("wpg2", layer), warm.bed_height_m, target_n, target_height_m)
        for layer in range(particle_layers)
    ]
    temp_layers = [
        _interp_profile(particle_component("Tp", layer), warm.bed_height_m, target_n, target_height_m)
        for layer in range(particle_layers)
    ]
    rate_layers = [
        _interp_profile(
            tuple(rates[layer] for rates in warm.dwpg2_dt),
            warm.bed_height_m,
            target_n,
            target_height_m,
        )
        for layer in range(particle_layers)
    ]
    particles = tuple(
        pt.ParticleState(
            wpg2=tuple(wpg_layers[layer][j] for layer in range(particle_layers)),
            Tp=tuple(temp_layers[layer][j] for layer in range(particle_layers)),
        )
        for j in range(target_n)
    )
    return DCZWarmStart(
        bed_height_m=target_height_m,
        particles=particles,
        vapor_T=tuple(_interp_profile(warm.vapor_T, warm.bed_height_m, target_n, target_height_m)),
        vapor_water_kg_s=tuple(
            _interp_profile(warm.vapor_water_kg_s, warm.bed_height_m, target_n, target_height_m)
        ),
        vapor_hexane_kg_s=tuple(
            _interp_profile(warm.vapor_hexane_kg_s, warm.bed_height_m, target_n, target_height_m)
        ),
        X1=tuple(_interp_profile(warm.X1, warm.bed_height_m, target_n, target_height_m)),
        condensed_water_kg_s=tuple(
            _interp_profile(
                warm.condensed_water_kg_s, warm.bed_height_m, target_n, target_height_m
            )
        ),
        water_latent_w_m3=tuple(
            _interp_profile(warm.water_latent_w_m3, warm.bed_height_m, target_n, target_height_m)
        ),
        dwpg2_dt=tuple(
            tuple(rate_layers[layer][j] for layer in range(particle_layers))
            for j in range(target_n)
        ),
        temperature_relaxation=warm.temperature_relaxation,
        hexane_relaxation=warm.hexane_relaxation,
        water_relaxation=warm.water_relaxation,
        # A remesh changes cell identity, so recompute the active set rather
        # than interpolating categorical flags.
        water_active_set=warm.water_active_set if len(warm.particles) == target_n else None,
    )


@dataclass(frozen=True)
class DCZConstants:
    diameter_m: float
    bed_height_m: float  # L_DCZ for this solve; caller may remesh as L_FTRZ moves
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
    vapor_water_kg_s: float  # explicit water flow leaving this cell toward the top
    vapor_hexane_kg_s: float  # explicit hexane flow leaving this cell toward the top

    @property
    def vapor_flow_kg_s(self) -> float:
        return self.vapor_water_kg_s + self.vapor_hexane_kg_s


@dataclass(frozen=True)
class DCZZoneResult:
    cells: tuple[DCZCellResult, ...]  # top-to-bottom, matching phz.py/ftrz.py's convention
    iterations: int
    converged: bool = True
    residuals: DCZResiduals = DCZResiduals()
    warm_start: DCZWarmStart | None = None

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

    @property
    def vapor_water_out_kg_s(self) -> float:
        return self.cells[0].vapor_water_kg_s

    @property
    def vapor_hexane_out_kg_s(self) -> float:
        return self.cells[0].vapor_hexane_kg_s

    @property
    def vapor_flow_out_kg_s(self) -> float:
        return self.cells[0].vapor_flow_kg_s


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
    residual_log: list[tuple[int, float, float, float, float, float]] | None = None,
    warm_start: DCZWarmStart | None = None,
    adaptive_damping: bool = True,
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
    dt = dz / u_L

    geometry = pt.build_shell_geometry(c.particle.r_P, c.particle.Np)
    zero_rates = tuple(0.0 for _ in range(c.particle.Np))

    initial_particle = pt.ParticleState(
        wpg2=tuple(1.0 for _ in range(c.particle.Np)),
        Tp=tuple(T_L_sup for _ in range(c.particle.Np)),
    )
    m_water_bottom_kg_s = (1.0 - vapor_inf.wV2) * m_vapor_kg_s
    m_hex_bottom_kg_s = vapor_inf.wV2 * m_vapor_kg_s
    if warm_start is None:
        particles: list[pt.ParticleState] = [initial_particle for _ in range(nz)]
        vapor_m_water = [m_water_bottom_kg_s for _ in range(nz)]
        vapor_m_hex = [m_hex_bottom_kg_s for _ in range(nz)]
        vapor_T = [vapor_inf.T for _ in range(nz)]
        dwpg2_dt_prev: list[tuple[float, ...]] = [zero_rates for _ in range(nz)]
        X1_profile = [X1_in for _ in range(nz)]
        water_latent_w_m3 = [0.0 for _ in range(nz)]
        condensed_profile_kg_s = [0.0 for _ in range(nz)]
    else:
        warm = _resample_warm_start(warm_start, nz, c.bed_height_m, c.particle.Np)
        particles = list(warm.particles)
        vapor_m_water = [max(value, 0.0) for value in warm.vapor_water_kg_s]
        vapor_m_hex = [max(value, 0.0) for value in warm.vapor_hexane_kg_s]
        vapor_T = list(warm.vapor_T)
        dwpg2_dt_prev = list(warm.dwpg2_dt)
        X1_profile = list(warm.X1)
        water_latent_w_m3 = list(warm.water_latent_w_m3)
        condensed_profile_kg_s = [max(value, 0.0) for value in warm.condensed_water_kg_s]
    vapor_wV2 = [
        vapor_m_hex[j] / max(vapor_m_water[j] + vapor_m_hex[j], 1.0e-12) for j in range(nz)
    ]
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
    # Water saturation temperature at the local (elevated, sparge) pressure -- the ceiling a
    # WET meal surface can reach: free/loosely-bound water flashes off (evaporative pinning)
    # rather than letting the meal superheat past it. Used both to cap the surface temperature
    # at which the moisture equilibrium a_w is evaluated (step 4.5) and to pin the bed
    # temperature while the meal stays wet (step 2). A wet meal in near-saturated steam sits at
    # saturation, NOT at the superheated toasting-vapor bulk temperature. (Y_V2=0 -> pure water.)
    T_sat_water = thermo.dew_point_temperature(0.0, c.antoine_water, P=c.pressure_pa)
    # Sorption latent heat remains one energy iteration lagged; its component
    # mass is marched in the same pass after step 4.5.

    iterations = 0
    # Previous-pass exit KPIs (bottom cell) for the physical convergence test;
    # seeded at inf so the first pass can never trip.
    prev_x2_exit = math.inf
    prev_T_exit = math.inf
    prev_x1_exit = math.inf
    prev_water_out = math.inf
    prev_hex_out = math.inf
    prev_vapor_top_T = math.inf
    residuals = DCZResiduals()
    converged = False

    initial_alpha = min(max(outer_relaxation, 1.0e-3), 1.0)
    temperature_alpha = warm.temperature_relaxation if warm_start is not None else initial_alpha
    hexane_alpha = warm.hexane_relaxation if warm_start is not None else initial_alpha
    water_alpha = warm.water_relaxation if warm_start is not None else min(initial_alpha, 0.10)
    temperature_damping = _AdaptiveRelaxation(temperature_alpha, 0.05, initial_alpha)
    hexane_damping = _AdaptiveRelaxation(hexane_alpha, 0.05, initial_alpha)
    # Water is the active-set-limited family.  Start conservatively even if
    # temperature/hexane can safely use the historical 0.5 Picard step.
    water_damping = _AdaptiveRelaxation(water_alpha, 0.001, min(initial_alpha, 0.25))
    previous_water_active_set: tuple[tuple[bool, bool], ...] | None = (
        warm.water_active_set if warm_start is not None else None
    )

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
        old_vapor_T = tuple(vapor_T)
        old_vapor_m_water = tuple(vapor_m_water)
        old_vapor_m_hex = tuple(vapor_m_hex)
        old_X1_profile = tuple(X1_profile)
        old_condensed_profile = tuple(condensed_profile_kg_s)
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
        new_vapor_T = [0.0] * nz
        raw_condensed_kg_s = [0.0] * nz
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
                raw_condensed_kg_s[j] += m_flash_kg_s
                T_in = T_dew_j

            local_vapor_flow_kg_s = max(vapor_m_water[j] + vapor_m_hex[j], 1.0e-9)
            local_u_V = local_vapor_flow_kg_s / (c.rho_V * A_bed)
            denom_e = c.alpha_V * local_u_V * c.rho_V * c.cp_V
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
                raw_condensed_kg_s[j] += m_cond_kg_s
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
        alpha_temperature = temperature_damping.value
        vapor_T = [
            vapor_T[j] + alpha_temperature * (new_vapor_T[j] - vapor_T[j]) for j in range(nz)
        ]
        alpha_water = water_damping.value
        condensed_profile_kg_s = [
            condensed_profile_kg_s[j]
            + alpha_water * (raw_condensed_kg_s[j] - condensed_profile_kg_s[j])
            for j in range(nz)
        ]

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

        # 4. hexane component balance at bed scale (bottom -> top). The
        # particle cascade is the authoritative interphase ledger: vapor
        # gains exactly the dry-solid-flow-scaled X2 loss of each cell. This
        # is the discrete conservative counterpart of A.24's SVm2 source and
        # avoids evaluating the same interface twice with mismatched closures.
        x2_particle = [
            pt.volumetric_mean(
                tuple(_x2_so(w, t, c.particle) for w, t in zip(cell.wpg2, cell.Tp)),
                geometry.volumes,
            )
            for cell in particles
        ]
        x2_zone_in = thermo.x2_equilibrium(
            T_L_sup,
            c.particle.X3,
            c.particle.gab,
            c.particle.oil,
            c.particle.alpha_pg,
            c.particle.alpha_ps,
            c.particle.rho_ps,
        )
        hexane_release_kg_s = [
            m_dry_kg_s * ((x2_zone_in if j == 0 else x2_particle[j - 1]) - x2_particle[j])
            for j in range(nz)
        ]
        new_vapor_m_hex = [0.0] * nz
        hex_running_kg_s = m_hex_bottom_kg_s
        for j in range(nz - 1, -1, -1):
            hex_running_kg_s = max(
                hex_running_kg_s + hexane_release_kg_s[j] + m_ax_net[j] * A_bed * dz,
                0.0,
            )
            new_vapor_m_hex[j] = hex_running_kg_s

        alpha_hexane = hexane_damping.value
        vapor_m_hex = [
            max(
                vapor_m_hex[j]
                + alpha_hexane * (new_vapor_m_hex[j] - vapor_m_hex[j]),
                0.0,
            )
            for j in range(nz)
        ]
        vapor_wV2 = [
            vapor_m_hex[j] / max(vapor_m_water[j] + vapor_m_hex[j], 1.0e-12)
            for j in range(nz)
        ]

        # 4.5. solid moisture at bed scale (top -> bottom, matching solid
        # flow direction -- see module docstring's "MOISTURE (H2O) BALANCE").
        # Uses THIS iteration's own just-updated `vapor_wV2`/`vapor_T` (steps
        # 2/4 above) and `condensed_profile_kg_s` (step 2's relaxed supersaturation
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
        cum_water_to_solid_kg_s = sum(condensed_profile_kg_s)
        water_limited_cells: list[bool] = []
        for j in range(nz):
            # Bulk condensation is an immediate vapor-energy event. Credit
            # its relaxed active-set mass first, then let the wet solid make
            # a finite-rate bidirectional adjustment toward Xe. Keeping the
            # mechanisms additive removes the discontinuous either/or branch
            # that produced the low-duty three-cycle.
            X1_after_condensation = X1_running + condensed_profile_kg_s[j] / m_dry_kg_s
            Y_V2_j = vapor_wV2[j] / max(1.0 - vapor_wV2[j], 1.0e-12)
            T_surface = min(vapor_T[j], T_sat_water)
            a_w = thermo.water_activity(Y_V2_j, T_surface, c.antoine_water, P=c.pressure_pa)
            a_w = min(max(a_w, 1.0e-9), thermo.LUIKOV_MAX_VALIDATED_UR)
            Xe = thermo.luikov_equilibrium_moisture(a_w, c.luikov)
            X1_new = (X1_after_condensation + dt * kappa_w * Xe) / (1.0 + dt * kappa_w)
            mass_rate_kg_s = m_dry_kg_s * (X1_new - X1_after_condensation)
            # A cell actively condensing bulk water is saturated; do not let
            # the clamped bound-water isotherm immediately evaporate that
            # same free condensate. Positive sorption remains additive.
            if condensed_profile_kg_s[j] > 1.0e-12 and mass_rate_kg_s < 0.0:
                mass_rate_kg_s = 0.0
                X1_new = X1_after_condensation
            if mass_rate_kg_s > 0.0:
                headroom_kg_s = max(water_available_kg_s - cum_water_to_solid_kg_s, 0.0)
                water_limited = mass_rate_kg_s > headroom_kg_s
                if mass_rate_kg_s > headroom_kg_s:
                    mass_rate_kg_s = headroom_kg_s
                    X1_new = X1_after_condensation + mass_rate_kg_s / m_dry_kg_s
            else:
                water_limited = False
            water_limited_cells.append(water_limited)
            cum_water_to_solid_kg_s += mass_rate_kg_s
            new_water_latent_w_m3[j] = mass_rate_kg_s * c.dH_vap_water / (A_bed * dz)
            new_water_mass_rate_w_m3[j] = mass_rate_kg_s / (A_bed * dz)
            new_X1_profile[j] = X1_new
            X1_running = X1_new
        X1_profile = [
            X1_profile[j] + alpha_water * (new_X1_profile[j] - X1_profile[j])
            for j in range(nz)
        ]
        water_latent_w_m3 = [
            water_latent_w_m3[j]
            + alpha_water * (new_water_latent_w_m3[j] - water_latent_w_m3[j])
            for j in range(nz)
        ]

        # March this pass's water transfer immediately. This is still a
        # Picard coupling because activity used the entering component-flow
        # profile, but mass and solid moisture now describe the same pass.
        new_vapor_m_water = [0.0] * nz
        water_running_kg_s = m_water_bottom_kg_s
        for j in range(nz - 1, -1, -1):
            water_sink_kg_s = (
                new_water_mass_rate_w_m3[j] * A_bed * dz + condensed_profile_kg_s[j]
            )
            water_running_kg_s = max(water_running_kg_s - water_sink_kg_s, 0.0)
            new_vapor_m_water[j] = water_running_kg_s
        vapor_m_water = [
            max(
                vapor_m_water[j]
                + alpha_water * (new_vapor_m_water[j] - vapor_m_water[j]),
                0.0,
            )
            for j in range(nz)
        ]
        vapor_wV2 = [
            vapor_m_hex[j] / max(vapor_m_water[j] + vapor_m_hex[j], 1.0e-12)
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

        # 5. Honest fixed-point convergence check.  The previous implementation
        # checked the bottom meal and top component flows but omitted the top
        # vapor temperature consumed by solve_dt.  A very slow thermal mode
        # could therefore be labelled converged while that boundary was still
        # moving by several kelvin.  Check both exported boundaries and maximum
        # full-profile residuals of all vapor states.
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
        d_water_out = abs(vapor_m_water[0] - prev_water_out)
        d_hex_out = abs(vapor_m_hex[0] - prev_hex_out)
        d_vapor_top_T = abs(vapor_T[0] - prev_vapor_top_T)
        d_vapor_T_profile = max(abs(vapor_T[j] - old_vapor_T[j]) for j in range(nz))
        d_vapor_water_profile = max(
            abs(vapor_m_water[j] - old_vapor_m_water[j]) for j in range(nz)
        )
        d_vapor_hex_profile = max(
            abs(vapor_m_hex[j] - old_vapor_m_hex[j]) for j in range(nz)
        )
        residuals = DCZResiduals(
            solid_out_X2=d_x2,
            solid_out_T=d_T,
            solid_out_X1=d_x1,
            vapor_top_T=d_vapor_top_T,
            vapor_top_water_flow=d_water_out,
            vapor_top_hexane_flow=d_hex_out,
            vapor_profile_T_max=d_vapor_T_profile,
            vapor_profile_water_flow_max=d_vapor_water_profile,
            vapor_profile_hexane_flow_max=d_vapor_hex_profile,
        )
        prev_x2_exit, prev_T_exit, prev_x1_exit = x2_exit, T_exit, x1_exit
        prev_water_out, prev_hex_out = vapor_m_water[0], vapor_m_hex[0]
        prev_vapor_top_T = vapor_T[0]
        if residual_log is not None:
            residual_log.append((iterations, d_x2, d_T, d_x1, d_water_out, d_hex_out))

        water_active_set = tuple(
            (raw_condensed_kg_s[j] > 1.0e-12, water_limited_cells[j]) for j in range(nz)
        )
        active_set_changed = (
            previous_water_active_set is not None
            and water_active_set != previous_water_active_set
        )
        previous_water_active_set = water_active_set
        if adaptive_damping:
            temperature_damping.observe(d_vapor_T_profile)
            hexane_damping.observe(d_vapor_hex_profile)
            water_norm = max(
                d_vapor_water_profile / PROFILE_TOL_FLOW,
                max(
                    abs(new_X1_profile[j] - old_X1_profile[j]) / EXIT_TOL_X1
                    for j in range(nz)
                ),
                max(
                    abs(raw_condensed_kg_s[j] - old_condensed_profile[j]) / PROFILE_TOL_FLOW
                    for j in range(nz)
                ),
            )
            water_damping.observe(water_norm, active_set_changed)

        if residuals.maximum_scaled <= 1.0:
            converged = True
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
        # X1_profile/condensed_profile_kg_s are already the FINAL outer iteration's
        # own converged values (step 4.5 above, top-j=0-to-bottom-j=nz-1,
        # same order as this loop) -- no separate recomputation needed here.
        cells.append(
            DCZCellResult(
                vapor_top=VaporState(wV2=vapor_wV2[j], T=vapor_T[j]),
                particle=particles[j],
                X2_bulk=X2_bulk,
                X1_bulk=X1_profile[j],
                condensed_water_kg_s=condensed_profile_kg_s[j],
                vapor_water_kg_s=vapor_m_water[j],
                vapor_hexane_kg_s=vapor_m_hex[j],
            )
        )
    warm = DCZWarmStart(
        bed_height_m=c.bed_height_m,
        particles=tuple(particles),
        vapor_T=tuple(vapor_T),
        vapor_water_kg_s=tuple(vapor_m_water),
        vapor_hexane_kg_s=tuple(vapor_m_hex),
        X1=tuple(X1_profile),
        condensed_water_kg_s=tuple(condensed_profile_kg_s),
        water_latent_w_m3=tuple(water_latent_w_m3),
        dwpg2_dt=tuple(dwpg2_dt_prev),
        temperature_relaxation=temperature_damping.value,
        hexane_relaxation=hexane_damping.value,
        water_relaxation=water_damping.value,
        water_active_set=previous_water_active_set,
    )
    return DCZZoneResult(
        cells=tuple(cells),
        iterations=iterations,
        converged=converged,
        residuals=residuals,
        warm_start=warm,
    )


def _x2_so(wpg2: float, T: float, pc: pt.ParticleConstants) -> float:
    """X2,so(wpg2,T) (eq. A.26) -- used only for the zone's own `X2_bulk`
    diagnostic (Fig. 9(a)-style reporting), dropping the free pore-gas
    hexane term (eq. 5's `Ypg2` -- the smallest of the three contributions
    at equilibrium) as a documented reporting simplification."""
    return thermo.gab_hexane_content(wpg2, T, pc.gab) + pc.X3 * thermo.oil_hexane_content(
        wpg2, pc.oil
    )
