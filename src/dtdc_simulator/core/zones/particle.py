"""Particle scale of the Diffusion-Controlled Zone (DCZ) — Coletto, Bandoni &
Blanco (2022), Table A.3 / §2.4.1 / §A.3.1, and their supplementary material
(Fig. 3's "energy balance at particle scale" / "mass balance at particle
scale" steps). M2 Phase 3 (BuildSpec §14): standalone, pure, unit-tested — not
yet wired into `core/zones/dcz.py`'s bed-scale coupling or `core/model.py`.

Each particle is discretized into `Np` (Coletto: 12) equal-thickness
(`dr = rP/Np`) spherical FVM shells (standard control-volume spherical
discretization, undisputed by the source material — it only shows layers in a
sketch, Fig. 3(b), without specifying equal-`dr` vs. equal-volume). Two
separate marches are provided — `march_particle_mass` and
`march_particle_energy` — because the real DCZ algorithm (the supplementary
material's Fig. 3) solves them as separate Gauss-Seidel steps within one
outer iteration (energy first, then mass, each using the other's frozen
value from the appropriate point in that same iteration), not a single
combined step.

MODELING SIMPLIFICATIONS (documented, not hidden):
- The convective boundary condition at r=rP (eq. A.22/A.23's BCs) uses the
  outermost shell's own volume-averaged state as a stand-in for the true
  surface value (`wpg2R`/`TpR`) — standard for a reasonably fine control
  -volume mesh, and how such BCs are conventionally applied in FVM without a
  dedicated zero-thickness surface node.
- `Ca` (eq. A.28, the isotherm's local slope `dX2,so/dwpg2`) is evaluated
  per layer from the *local* (wpg2, Tp) via `core.thermo.x2_so_and_slope`,
  then face-averaged (arithmetic mean of the two neighboring layers) for the
  FVM's diffusive coefficient — a standard, simple closure for a
  concentration-dependent diffusivity.
- Thermal conductivity and heat capacity are constant per Coletto's own
  stated assumption (§2.4.3: "solvent diffusivity, specific heat, and
  thermal conductivity are constant inside the particle"), so no face
  -averaging subtlety is needed there (unlike `Ca`, which genuinely varies
  point-to-point with the isotherm).
- The sorption/desorption heat source (eq. A.30's first two terms) needs
  `dW2/dt` and `dqo/dt` *separately* (they carry different latent heats).
  Since the energy march runs before `march_particle_mass` within one outer
  DCZ iteration (per the real algorithm), callers build this source (via
  `sorption_heat_source_per_layer_w_m3`, then pass it into
  `march_particle_energy`) from the *previous* iteration's own mass-march
  rate (`dwpg2_dt_prev`, a diagnostic returned by `march_particle_mass`) as a
  lagged (Picard-style) estimate of the current rate — the two converge
  together as the outer loop converges. On the very first outer iteration
  there is no such rate yet; callers pass zeros (same "cold start" precedent
  as `L_FTRZ`'s free-boundary iteration).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from dtdc_simulator.core import thermo


@dataclass(frozen=True)
class ParticleConstants:
    D_eff: float  # m2/s, intraparticle hexane diffusivity
    r_P: float  # m, particle radius
    Np: int  # number of spherical FVM layers (Coletto: 12)
    alpha_ps: float
    alpha_pg: float
    rho_ps: float
    rho_pg: float  # pore-gas (~hexane vapor) reference density, kg/m3
    cp_ps: float  # J/(kg K)
    cp_pg: float
    k_ps: float  # W/(m K)
    k_pg: float
    X3: float  # oil fraction, kg/kg dry solid
    gab: thermo.GabParams
    oil: thermo.OilIsotherm
    dH_vap_hexane: float
    sorption_C0: float
    sorption_C1: float
    cp_water_liquid: float  # J/(kg K), for the moisture-weighted Cv term below


@dataclass(frozen=True)
class ShellGeometry:
    dr: float
    face_radii: tuple[float, ...]  # Np entries: Np-1 internal faces, then the outer surface
    face_areas: tuple[float, ...]  # matches face_radii
    volumes: tuple[float, ...]  # Np shell volumes, center (index 0) to surface


def build_shell_geometry(r_P: float, Np: int) -> ShellGeometry:
    dr = r_P / Np
    face_radii = tuple((i + 1) * dr for i in range(Np))
    face_areas = tuple(4.0 * math.pi * r * r for r in face_radii)
    volumes = tuple(
        (4.0 / 3.0) * math.pi * (((i + 1) * dr) ** 3 - (i * dr) ** 3) for i in range(Np)
    )
    return ShellGeometry(dr=dr, face_radii=face_radii, face_areas=face_areas, volumes=volumes)


@dataclass(frozen=True)
class ParticleState:
    wpg2: tuple[float, ...]  # pore-gas hexane mass fraction per layer (index 0 = center)
    Tp: tuple[float, ...]  # K, temperature per layer (shared by solid/oil/gas phases)


def outer_layer_value(values: tuple[float, ...]) -> float:
    """The 12th (outermost) layer's value — the particle<->vapor coupling
    point the bed scale needs (`wpg2,12`/`Tp,12`, confirmed by the paper's own
    wording)."""
    return values[-1]


def volumetric_mean(values: tuple[float, ...], volumes: tuple[float, ...]) -> float:
    """<phi> (eq. 8): volumetric mean of a per-layer particle property,
    mapping the particle's radial field onto a single bed-solid value."""
    total_v = sum(volumes)
    return sum(v * vol for v, vol in zip(values, volumes)) / total_v


def _accumulation_jacobian_per_layer(
    state: ParticleState, c: ParticleConstants
) -> tuple[float, ...]:
    """M_i = dX2,total/dwpg2|_local (eq. A.22's own accumulation term,
    X2,total = alpha_pg*rho_pg*wpg2 + alpha_ps*rho_ps*X2,so, differentiated
    via the chain rule) at each layer, from the local isotherm slope --
    replaces the old `Ca` (eq. A.28) entirely, see
    `march_particle_mass`'s own docstring for why."""
    ms = []
    for wpg2_i, Tp_i in zip(state.wpg2, state.Tp):
        _, slope = thermo.x2_so_and_slope(wpg2_i, Tp_i, c.X3, c.gab, c.oil)
        ms.append(c.alpha_pg * c.rho_pg + c.alpha_ps * c.rho_ps * slope)
    return tuple(ms)


def _component_slopes(
    wpg2: float, T: float, c: ParticleConstants, h: float = 1.0e-5
) -> tuple[float, float]:
    """(dW2/dwpg2, dqo/dwpg2) separately (unlike `thermo.x2_so_and_slope`,
    which only returns their combined sum) -- eq. A.30's sorption-heat source
    needs them apart since they carry different latent heats."""

    def w2(a: float) -> float:
        return thermo.gab_hexane_content(a, T, c.gab)

    def qo(a: float) -> float:
        return thermo.oil_hexane_content(a, c.oil)

    step = min(h, wpg2, 1.0 - wpg2)
    if step <= 0.0:
        step = h
        if wpg2 <= 0.0:
            return (w2(wpg2 + step) - w2(wpg2)) / step, (qo(wpg2 + step) - qo(wpg2)) / step
        return (w2(wpg2) - w2(wpg2 - step)) / step, (qo(wpg2) - qo(wpg2 - step)) / step
    dW2 = (w2(wpg2 + step) - w2(wpg2 - step)) / (2.0 * step)
    dqo = (qo(wpg2 + step) - qo(wpg2 - step)) / (2.0 * step)
    return dW2, dqo


def sorption_heat_source_per_layer_w_m3(
    state: ParticleState,
    dwpg2_dt_prev: tuple[float, ...],
    c: ParticleConstants,
) -> tuple[float, ...]:
    """S_Q's sorption/desorption terms only (eq. A.30's first two terms) per
    layer -- the axial-conduction correction (`q_condL`, eq. A.30's third
    term) is a separate, caller-supplied addition (see `march_particle_energy`),
    kept apart so this expensive isotherm-slope computation is done ONCE per
    layer per march and reused for both the particle's own energy-balance
    source AND `zones/dcz.py`'s bed-scale energy credit (see that module's
    step 1), instead of computing it twice. PROFILED (M2 Phase 4 follow-up,
    2026-07-14): this was previously computed via two separate calls (one
    inside `march_particle_energy`, one inside a now-removed
    `sorption_heat_sink_volumetric_mean_w_m3` wrapper) -- confirmed by
    `cProfile` to be the single largest cost in the whole DT solve (~14 of
    ~31s in a cold `assemble_model` run). Callers now compute this once and
    pass the (sorption-only) result to `march_particle_energy` themselves,
    plus their own `q_condL` addition -- a pure performance refactor,
    bit-identical results, no physics change.

    SIGN, DOCUMENTED RESOLUTION (not a literal transcription): eq. A.30 as
    published reads `S_Q = -alpha_ps*rho_ps*(dW2/dt)*dH_s - ...`. Applied
    literally with `dW2/dt` as the plain rate of change of adsorbed hexane,
    that sign makes DESORPTION (`dW2/dt<0`, the dominant process throughout
    the DCZ) a net HEAT SOURCE and adsorption a heat sink -- backwards from
    basic sorption thermodynamics (adsorption is exothermic, desorption is
    endothermic) and confirmed backwards empirically during development
    (an isolated sanity check with near-equal vapor/solid boundary
    temperatures and no external heat input showed the particle running
    away to hundreds of degrees above BOTH boundary temperatures, sourced
    entirely by this term). Implemented here with the physically-consistent
    sign instead: desorption (`dW2/dt<0`) is a heat SINK.
    """
    sources = []
    for wpg2_i, Tp_i, rate_i in zip(state.wpg2, state.Tp, dwpg2_dt_prev):
        dW2_dwpg2, dqo_dwpg2 = _component_slopes(wpg2_i, Tp_i, c)
        dW2_dt = dW2_dwpg2 * rate_i
        dqo_dt = dqo_dwpg2 * rate_i
        W2_i = thermo.gab_hexane_content(wpg2_i, Tp_i, c.gab)
        # eq. A.31's power law (`sorption_C1` typically negative) is
        # mathematically singular at exactly zero coverage. M2 Phase 4
        # floored W2 at 1e-9 purely to avoid a literal ZeroDivisionError, not
        # to bound the resulting MAGNITUDE -- fine for the small illustrative
        # zones tested at the time, but a real full-scale DT solve (found
        # this session, DECISIONS.md's "DT runaway temperature" entry) drives
        # W2 down to ~8.5e-8 near the DCZ exit (its whole job is stripping
        # the last traces of hexane), where the 1e-9 floor let dH_s reach
        # ~9.5e9 J/kg -- ~28,000x dH_vap_hexane, versus the ~4-30x the cited
        # Cardarelli & Crapiste (1996) "rises well above the heat of
        # vaporization at low coverage" finding (see
        # `test_heat_of_sorption_exceeds_latent_heat_at_low_moisture`)
        # actually supports. `sorption_C0`/`sorption_C1` remain uncalibrated
        # [PLACE] (the underlying thesis is unrecoverable, see properties/
        # soybean.yaml's own note) so there's no "correct" value to floor at;
        # 2% of the GAB monolayer capacity (`gab.Xm`) is a physically
        # motivated low-coverage scale (rather than an arbitrary constant)
        # that keeps dH_s within ~2 orders of magnitude of dH_vap_hexane
        # (order 90x at this scenario's Xm) instead of 4+ orders, while
        # leaving both of that test's own checkpoints (W2=0.001, W2=0.1)
        # unaffected.
        W2_floored = max(W2_i, 0.02 * c.gab.Xm)
        dH_s = thermo.heat_of_sorption(W2_floored, c.dH_vap_hexane, c.sorption_C0, c.sorption_C1)
        s_q = (
            c.alpha_ps * c.rho_ps * dW2_dt * dH_s + c.alpha_ps * c.rho_ps * dqo_dt * c.dH_vap_hexane
        )
        sources.append(s_q)
    return tuple(sources)


# NOTE on the vapor-side energy credit this sorption source also feeds
# (`zones/dcz.py`'s step 1, eq. A.34's dropped `SVm2*Ĥ2` term): crediting the
# FULL sink (including eq. A.31's isosteric "excess" term, not only the base
# latent heat `dH_vap_hexane`) was chosen over a latent-heat-only credit
# because the latter was tested and still diverges to unbounded cooling
# (under-credits). Verified to converge (not diverge) across the parameter
# ranges exercised in `tests/test_dt_solver.py`, but at higher `hM` it
# converges to a noticeably elevated absolute temperature (order 400+ K)
# rather than staying close to the zone's own boundary temperatures --
# plausibly an artifact of `hQ`/`hM`/`aV` still being `[DERIVED]`/`[PLACE]`
# placeholders (not fitted to real bed conditions), not of this credit's own
# definition, but that's a plausibility argument, not a proof. Matching this
# to the *surface* mass-transfer flux (`SVm2` literally, eq. A.33) instead of
# this particle-VOLUME-integrated quantity was tried first and had negligible
# effect: the two are only equal at radial steady state, and a particle's own
# diffusive relaxation timescale (`rP^2/D_eff`, tens of minutes at typical
# DCZ parameters) is far longer than one axial cell's residence time, so most
# of the sink is still mid-transit toward the surface, not yet in NET
# agreement with what's instantaneously crossing it. Open follow-up work, not
# settled here.


@dataclass(frozen=True)
class MassDiagnostics:
    dwpg2_dt: tuple[float, ...]  # per-layer implied rate over this step, 1/s
    hexane_flux_to_vapor_kg_m2_s: float  # positive = leaving the particle (J_M2R.r)


def march_particle_mass(
    state: ParticleState,
    wV2_local: float,
    hM: float,
    rho_V: float,
    dt: float,
    geometry: ShellGeometry,
    c: ParticleConstants,
) -> tuple[ParticleState, MassDiagnostics]:
    """One implicit (backward Euler) timestep of eq. A.22's radial hexane
    diffusion, discretized directly in `X2,total`-conservative form (NOT via
    eq. A.29's own `Ca`-simplified, `wpg2`-only form -- see the mass
    -conservation history below for why that distinction matters).

    `X2,total = alpha_pg*rho_pg*wpg2 + alpha_ps*rho_ps*X2,so(wpg2,T)` (eq.
    A.22's own accumulated quantity: hexane held as free pore gas PLUS
    hexane adsorbed/absorbed in the solid and oil phases) is the finite
    -volume method's own accumulation variable here, not `wpg2` alone. Its
    own rate of change is linearized via the frozen-coefficient Jacobian
    `M_i = dX2,total/dwpg2|_local` (`_accumulation_jacobian_per_layer`,
    evaluated from the state entering this step, same stability rationale as
    every other frozen-coefficient march in this codebase) -- but, UNLIKE
    the old `Ca`-based scheme, this Jacobian appears ONLY in the accumulation
    term. The DIFFUSIVE flux terms use the literal, CONSTANT coefficient
    `alpha_pg*rho_pg*D_eff` straight from eq. A.22 (no face-averaging of a
    spatially-varying coefficient needed at all -- it's uniform), and the
    boundary convective term uses eq. A.22's own literal boundary condition
    (`hM*rho_V*(wV2-wpg2R)`) unmodified.

    MASS-CONSERVATION HISTORY (this session, DECISIONS.md's "DCZ particle
    hexane mass-conservation gap" entry has the full trail): the ORIGINAL
    scheme (before this fix) discretized eq. A.29 instead -- Coletto's own
    algebraic simplification of eq. A.22 that rewrites it purely in terms of
    `wpg2`, using `Ca` (eq. A.28) as an "effective diffusivity". That
    simplification is faithful for the INTERIOR, but the paper never
    re-derives eq. A.22's own boundary condition for it, and two different
    attempts to retrofit one (unscaled, then scaled by `Ca` alone) both
    failed a rigorous test: holding total elapsed time FIXED and refining
    the number of internal sub-steps (which isolates true discretization
    error from a structural bug) showed the mass-balance residual staying
    PERFECTLY FLAT regardless of sub-step count for both attempts -- proof
    neither was a temporal truncation error, i.e. neither was structurally
    correct. Re-deriving the wpg2-only form by hand surfaced the reason:
    going from eq. A.22 to eq. A.29, `Ca` is itself a function of `wpg2`
    (spatially varying), so `nabla.(Ca*D_eff*nabla(wpg2))` is NOT simply
    `Ca*D_eff*nabla^2(wpg2)` (product rule: there's a missing
    `nabla(Ca).nabla(wpg2)` cross term) -- eq. A.29, taken at face value, is
    Coletto's own chosen PDE form, not a rigorous algebraic identity with an
    equally rigorous boundary condition supplied alongside it. Discretizing
    eq. A.22 directly instead (this function, now) sidesteps the whole
    question -- the boundary condition needs no transformation at all, it's
    already stated in terms of eq. A.22's own flux. Verified with the SAME
    fixed-total-time/sub-step methodology that falsified the two prior
    attempts: at a production-realistic dt (~60 s, single step, no
    sub-stepping), the residual is ~1.4% (was 1860% before this fix); it
    shrinks CLEANLY and MONOTONICALLY with both finer sub-stepping (same
    total time) and finer radial mesh (same total time, properly time
    -resolved at each mesh) -- the signature of a correctly-conservative
    scheme, not a masked structural error.
    """
    Np = c.Np
    dr = geometry.dr
    M = _accumulation_jacobian_per_layer(state, c)
    const_diff = c.alpha_pg * c.rho_pg * c.D_eff  # eq. A.22's own diffusive flux coefficient,
    # CONSTANT (not face-averaged) -- unlike the old Ca-based scheme, nothing here varies
    # spatially, since M (the only local/nonlinear factor) lives solely in the accumulation term.

    A = np.zeros((Np, Np))
    b = np.zeros(Np)
    for i in range(Np):
        diag = geometry.volumes[i] * M[i] / dt
        b[i] = geometry.volumes[i] * M[i] / dt * state.wpg2[i]
        if i > 0:
            coeff_in = const_diff * geometry.face_areas[i - 1] / dr
            A[i, i - 1] += -coeff_in
            diag += coeff_in
        if i < Np - 1:
            coeff_out = const_diff * geometry.face_areas[i] / dr
            A[i, i + 1] += -coeff_out
            diag += coeff_out
        else:
            # eq. A.22's own boundary condition (Table A.3), literal and unscaled --
            # convective mass transfer from the vapor stream, `hM*rho_V*(wV2-wpg2R)`.
            coeff_surf = hM * rho_V * geometry.face_areas[Np - 1]
            diag += coeff_surf
            b[i] += coeff_surf * wV2_local
        A[i, i] += diag

    wpg2_new = np.linalg.solve(A, b)
    # Clamp to the physical [0,1] domain: the linear solve can drift a few ULPs
    # past a boundary (e.g. wpg2=1.0 exactly, the DCZ's own initial condition),
    # which would otherwise reject a valid isotherm evaluation downstream.
    wpg2_clamped = tuple(min(1.0, max(0.0, float(x))) for x in wpg2_new)
    dwpg2_dt = tuple((wpg2_clamped[i] - state.wpg2[i]) / dt for i in range(Np))
    surf_flux = hM * rho_V * (wpg2_clamped[-1] - wV2_local)
    new_state = ParticleState(wpg2=wpg2_clamped, Tp=state.Tp)
    return new_state, MassDiagnostics(dwpg2_dt=dwpg2_dt, hexane_flux_to_vapor_kg_m2_s=surf_flux)


@dataclass(frozen=True)
class EnergyDiagnostics:
    heat_flux_to_vapor_w_m2: float  # positive = leaving the particle (J_QR.r)


def march_particle_energy(
    state: ParticleState,
    TV_local: float,
    hQ: float,
    sources_w_m3: tuple[float, ...],
    dt: float,
    geometry: ShellGeometry,
    c: ParticleConstants,
    X1: float = 0.0,
) -> tuple[ParticleState, EnergyDiagnostics]:
    """One implicit timestep of eq. A.23's radial energy diffusion (constant
    conductivity/heat capacity per Coletto's own assumption), with the
    convective BC (r=rP, coupling to the local vapor temperature) and the
    volumetric source `S_Q` (eq. A.30, full per-layer source: sorption/
    desorption heat plus axial-conduction contribution) -- PRE-COMPUTED by
    the caller (typically `sorption_heat_source_per_layer_w_m3(...)` plus the
    caller's own `q_condL` addition) rather than computed here, so a caller
    that also needs the sorption-only part (e.g. `zones/dcz.py`'s bed-scale
    energy credit) doesn't pay for the expensive isotherm-slope evaluation
    twice. See `sorption_heat_source_per_layer_w_m3`'s own docstring for why
    this was split out (a profiled, confirmed cost dedup, M2 Phase 4
    follow-up).

    `X1` (default 0.0, backward compatible): one lumped bound-moisture value
    for the WHOLE particle (not per-layer -- DCZ's own moisture mechanism has
    no radial resolution for water, see `zones/dcz.py`'s module docstring),
    lagged one outer iteration same as `dwpg2_dt_prev` above. Raises the
    effective volumetric heat capacity by the bound water's own mass-weighted
    share, mirroring `core/dc.py`'s own `C_wet = cp_solid + X1*cp_water_liquid`
    precedent exactly, just per particle volume instead of per unit dry mass."""
    Np = c.Np
    dr = geometry.dr
    Cv = (
        c.alpha_pg * c.rho_pg * c.cp_pg
        + c.alpha_ps * c.rho_ps * c.cp_ps
        + c.alpha_ps * c.rho_ps * X1 * c.cp_water_liquid
    )
    k_mix = c.alpha_pg * c.k_pg + c.alpha_ps * c.k_ps
    sources = sources_w_m3

    A = np.zeros((Np, Np))
    b = np.zeros(Np)
    for i in range(Np):
        V_i = geometry.volumes[i]
        diag = Cv * V_i / dt
        rhs = Cv * V_i / dt * state.Tp[i] + sources[i] * V_i
        if i > 0:
            coeff_in = k_mix * geometry.face_areas[i - 1] / dr
            A[i, i - 1] += -coeff_in
            diag += coeff_in
        if i < Np - 1:
            coeff_out = k_mix * geometry.face_areas[i] / dr
            A[i, i + 1] += -coeff_out
            diag += coeff_out
        else:
            coeff_surf = hQ * geometry.face_areas[Np - 1]
            diag += coeff_surf
            rhs += coeff_surf * TV_local
        A[i, i] += diag
        b[i] = rhs

    Tp_new = np.linalg.solve(A, b)
    surf_flux = hQ * (float(Tp_new[-1]) - TV_local)
    new_state = ParticleState(wpg2=state.wpg2, Tp=tuple(float(x) for x in Tp_new))
    return new_state, EnergyDiagnostics(heat_flux_to_vapor_w_m2=surf_flux)
