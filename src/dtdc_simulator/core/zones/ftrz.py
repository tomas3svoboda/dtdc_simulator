"""Flashing and Temperature-Raising Zone (FTRZ) sub-model — Coletto, Bandoni &
Blanco (2022), §2.3/§2.3.1/§2.3.5 and §7.5/Table A.2 (BuildSpec). M2 Phase 2
(BuildSpec §14): standalone, pure, unit-tested — not yet wired into
`core/model.py` (M2 Phase 4, the tray-by-tray fixed-point sweep).

KEY SIMPLIFICATION (found while re-deriving the equations for this module, not
an assumption): the solid-side profile is entirely algebraic given the vapor
-side solve, not a second independent solve —
- hexane content follows the *uniform-removal* assumption (eq. A.6, same
  technique `zones/phz.py` already uses): constant per cell, with the zone
  -exit value `X2,inf = X2,eq(T_V,inf)` (`core.thermo.x2_equilibrium`, whose
  `a_h=1` convention already matches the paper's "pores saturated with gas
  hexane" definition of this term, §2.3.5/eq. A.6);
- temperature is given directly by eq. A.17: `T_L = w_h*T_boil_hexane +
  (1-w_h)*T_V`, with the wet-core fraction `w_h` from the Receding Front
  radius (eq. 3) — algebraically, `w_h = (r_fr/r_P)^3`, so the cube in eq. 3
  cancels exactly against this mass-fraction relation (see
  `wet_core_fraction`);
- water gain combines root-solved V-SAT bulk condensation with finite-rate
  surface sorption toward the Luikov equilibrium over each cell's local
  residence time (mass-conservative).

So only ONE sequential march is needed: the vapor stream, mass + energy,
tracking the V-SCAL (superheated) -> V-SAT (on the dew curve) transition
against `core.thermo.dew_point_temperature`. Vapor's own inlet is at the
BOTTOM of the zone (z=L_FTRZ, arriving from the DCZ below), so this march
proceeds bottom-to-top; the public API re-indexes to top-to-bottom (matching
`zones/phz.py`'s convention: `cells[0]` nearest the solid inlet).

FREE BOUNDARY: `L_FTRZ = sum(dz_j)` (eq. A.21, from the per-cell thicknesses,
eq. A.18) is solved via the fixed-point iteration the paper itself describes
("L_FTRZ is updated after each iteration") in `solve_ftrz_zone`. Each cell's
thickness in turn depends on `q_cell_w`, which is sized from the CURRENT
guess of `L_FTRZ` (via `q_Iv_w_m3 * A_bed * (L_FTRZ/nz)`) — so the loop
remains genuinely circular even though (per the resolution below) `q_Iv_w_m3`
itself is now a plain constant, not something recomputed from a shrinking/
growing absolute wattage each pass.

DOCUMENTED GAPS/CHOICES (confirmed with the user, flagged not hidden):
- `q_Iv_w_m3` (this zone's own volumetric indirect-heat density) and `hQ`
  (convective heat-transfer coefficient) are explicit input parameters here,
  not derived from bed conditions internally — both are computed once by the
  caller (`core/dt_solver.py`, M2 Phase 4) and passed in. `q_Iv_w_m3`
  RESOLVES what M2 Phase 2 originally left as an absolute, undivided
  `Q_cond_w` (the paper gives no formula for it, only a qualitative
  axial-conduction description): the DT solver assigns FTRZ the *same*
  uniform volumetric heat density as the rest of its host tray
  (`Q_indirect_remaining/(A_bed*L_remaining)`, eq. A.2a's own convention,
  applied consistently) rather than inventing a separate quantity — so this
  module no longer needs to back a density out of an absolute wattage divided
  by its own not-yet-converged length. `hQ` still needs the same
  `Re_epsilon`/Faner-correlation machinery flagged as a literature gap since
  M1 (now closed with a documented standard-packed-bed placeholder in
  `dt_solver.py`, not here).
- The paper gives explicit vapor energy-source closures for hexane's mass
  transfer (eq. A.6-A.7) but not an explicit V-SCAL temperature-evolution
  formula. This module mixes the newly-evaporated hexane in at the wet core's
  own temperature (`T_boil_hexane`, since that's what's actually evaporating)
  using `core.thermo`'s vapor-enthalpy machinery, plus the cell's share of
  `q_Iv`; once that candidate state would fall below its own dew point, the
  cell switches to V-SAT and solves for the condensed water mass from the
  full energy balance instead (see `solve_ftrz_cell`).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from scipy.optimize import brentq

from dtdc_simulator.core import thermo


@dataclass(frozen=True)
class FTRZConstants:
    T_boil_hexane: float
    T_boil_water: float
    dH_vap_hexane: float
    cp_water_liquid: float
    gab: thermo.GabParams
    oil: thermo.OilIsotherm
    antoine_water: thermo.AntoineParams
    vapor_enthalpy_ref: thermo.VaporEnthalpyRef
    alpha_pg: float
    alpha_ps: float
    rho_ps: float
    X3: float  # oil fraction, kg/kg dry solid
    bed_porosity: float  # eps_b; alpha_L (bed-scale solid volume fraction) = 1 - bed_porosity
    water_diffusivity: float  # m2/s, water intraparticle diffusivity
    particle_radius: float  # m, flake diffusion-length scale
    # Empirical critical solvent content (Faner 2019, ~0.20 soybean). None -> use the
    # theoretical pore-saturation eq. 4 (thermo.x2_critical). See DECISIONS.md.
    x2_critical_empirical: float | None = None
    # DT internal operating pressure (Pa) for the water dew-point calc. The lower
    # DT runs ABOVE atmospheric -- the sparge tray apertures create a 0.35-0.70
    # kg/cm2 pressure drop (Kemper 2019), raising the water saturation temperature
    # to ~108-115 C so live steam condenses onto the 68-108 C meal here (the meal
    # exits the top countercurrent tray at 17-21% moisture). Default atmospheric.
    pressure_pa: float = thermo.ATM_PRESSURE_PA
    # Water sorption isotherm (Gianini et al. 2006, fit to real DT-outlet meal) for
    # the moisture-raising balance below -- ADDITION beyond Coletto (hexane-only DT).
    # None disables the water balance (unit tests that predate it). See solve_ftrz_zone.
    luikov: thermo.LuikovParams | None = None
    # Dry-solid heat capacity (J/kg-K), for the condensation-latent energy coupling
    # (the steam that condenses onto the cool meal is what raises its temperature).
    cp_solid: float = 0.0
    cp_hexane_liquid: float = 0.0
    cp_oil: float = 0.0


@dataclass(frozen=True)
class SolidState:
    T: float  # K
    X1: float  # moisture, kg/kg dry solid
    X2: float  # hexane, kg/kg dry solid


@dataclass(frozen=True)
class VaporState:
    m_water_kg_s: float
    m_hex_kg_s: float
    T: float  # K

    @property
    def Y_V2(self) -> float:
        """Hexane content in water basis (eq. A.2b)."""
        return self.m_hex_kg_s / self.m_water_kg_s


@dataclass(frozen=True)
class FTRZCellResult:
    solid: SolidState  # algebraic solid state at this cell's axial position
    # Water-interface closure temperature.  This may sit at the local water
    # dew point and is used only by the finite-rate sorption calculation; it is
    # deliberately separate from the bulk-solid temperature given by A.17.
    water_surface_T: float
    vapor_out: VaporState  # vapor leaving this cell (toward the top)
    dz_m: float
    condensed_water_kg_s: float  # net water transferred to solid (bulk + sorption)
    bulk_condensed_water_kg_s: float  # V-SAT condensation solved in the vapor energy balance
    sorbed_water_kg_s: float  # finite-rate isotherm transfer; negative means desorption
    sensible_heat_to_solid_w: float  # cold-meal heat debited from vapor; always >= 0
    is_saturated: bool  # False = V-SCAL, True = V-SAT


def wet_core_fraction(X2: float, X2_cr: float, X2_eq: float) -> float:
    """w_h — mass fraction of hexane-wet core (eq. 3, `(r_fr/r_P)^3`, which
    cancels algebraically against the cube in the paper's radius form)."""
    if X2 > X2_cr:
        return 1.0
    if X2_cr <= X2_eq:
        return 0.0
    return min(max((X2 - X2_eq) / (X2_cr - X2_eq), 0.0), 1.0)


def solid_temperature(
    X2: float, X2_cr: float, X2_eq: float, T_boil_hexane: float, T_V: float
) -> float:
    """T_L (eq. A.17)."""
    w_h = wet_core_fraction(X2, X2_cr, X2_eq)
    return w_h * T_boil_hexane + (1.0 - w_h) * T_V


def water_transfer_rate_s(
    water_diffusivity_m2_s: float,
    particle_radius_m: float,
    hM_m_s: float,
    aV_m2_m3: float,
) -> float:
    """Overall first-order FTRZ water-transfer rate.

    The intraparticle term is the Glueckauf linear-driving-force closure
    ``15 D_water / r_P**2`` already used by the DCZ.  ``hM*aV`` supplies the
    external bed-film rate.  Treating those mechanisms as resistances in
    series prevents either one from being silently assumed instantaneous.
    A non-positive physical input disables transfer rather than dividing by
    zero.
    """
    if (
        water_diffusivity_m2_s <= 0.0
        or particle_radius_m <= 0.0
        or hM_m_s <= 0.0
        or aV_m2_m3 <= 0.0
    ):
        return 0.0
    k_internal = 15.0 * water_diffusivity_m2_s / particle_radius_m**2
    k_external = hM_m_s * aV_m2_m3
    return 1.0 / (1.0 / k_internal + 1.0 / k_external)


def relax_moisture(X1: float, X1_equilibrium: float, contact_time_s: float, rate_s: float) -> float:
    """Analytic LDF relaxation over one cell, bounded by ``X1_equilibrium``."""
    if contact_time_s <= 0.0 or rate_s <= 0.0:
        return X1
    approach = -math.expm1(-rate_s * contact_time_s)
    return X1 + approach * (X1_equilibrium - X1)


def _energy_balance_residual(
    m_cond_kg_s: float,
    m_water_before: float,
    m_hex_after_mt: float,
    H_flow_before_w: float,
    q_cell_w: float,
    hexane_enthalpy_in_w: float,
    sensible_heat_to_solid_w: float,
    c: FTRZConstants,
) -> tuple[float, float, float]:
    """Residual of the cell energy balance when pinned to the dew curve
    (V-SAT): returns `(residual, T_after, Y_V2_after)` for a candidate
    `m_cond_kg_s`."""
    m_water_after = m_water_before - m_cond_kg_s
    Y_V2_after = m_hex_after_mt / m_water_after
    T_after = thermo.dew_point_temperature(Y_V2_after, c.antoine_water, P=c.pressure_pa)
    H_vbw_after = thermo.vapor_enthalpy_water_basis(Y_V2_after, T_after, c.vapor_enthalpy_ref)
    condensate_enthalpy_out_w = m_cond_kg_s * c.cp_water_liquid * (T_after - c.T_boil_water)
    lhs = m_water_after * H_vbw_after
    rhs = (
        H_flow_before_w
        + q_cell_w
        + hexane_enthalpy_in_w
        - sensible_heat_to_solid_w
        - condensate_enthalpy_out_w
    )
    return lhs - rhs, T_after, Y_V2_after


def solve_ftrz_cell(
    vapor_in: VaporState,
    hexane_evap_kg_s: float,
    q_cell_w: float,
    c: FTRZConstants,
    sensible_heat_to_solid_w: float = 0.0,
) -> tuple[VaporState, float, bool]:
    """One FTRZ cell, vapor-marching order (bottom to top): hexane mass
    transfer (uniform per cell, eq. A.6-A.7) always occurs; the resulting
    energy balance is first tried as V-SCAL (superheated, eq. A.5-style
    enthalpy mixing), and only if that candidate would fall on/below its own
    dew point does the cell switch to V-SAT (condensation, root-solved
    against the full energy balance while pinned to the dew curve).

    Returns `(vapor_out, condensed_water_kg_s, is_saturated)`.
    """
    m_hex_after_mt = vapor_in.m_hex_kg_s + hexane_evap_kg_s
    H_flow_before_w = vapor_in.m_water_kg_s * thermo.vapor_enthalpy_water_basis(
        vapor_in.Y_V2, vapor_in.T, c.vapor_enthalpy_ref
    )
    hexane_enthalpy_in_w = hexane_evap_kg_s * c.dH_vap_hexane  # evaporates at T_boil_hexane

    # V-SCAL candidate: no condensation, water flow unchanged.
    Y_V2_candidate = m_hex_after_mt / vapor_in.m_water_kg_s
    H_flow_candidate_w = (
        H_flow_before_w + q_cell_w + hexane_enthalpy_in_w - sensible_heat_to_solid_w
    )
    T_candidate = thermo.temperature_from_vapor_enthalpy(
        H_flow_candidate_w / vapor_in.m_water_kg_s, Y_V2_candidate, c.vapor_enthalpy_ref
    )
    T_dew_candidate = thermo.dew_point_temperature(Y_V2_candidate, c.antoine_water, P=c.pressure_pa)

    if T_candidate > T_dew_candidate:
        vapor_out = VaporState(
            m_water_kg_s=vapor_in.m_water_kg_s, m_hex_kg_s=m_hex_after_mt, T=T_candidate
        )
        return vapor_out, 0.0, False

    # V-SAT: solve for the condensed water mass that keeps the cell on the
    # dew curve while satisfying the energy balance.
    upper_bound = vapor_in.m_water_kg_s * 0.999

    def residual(m_cond: float) -> float:
        return _energy_balance_residual(
            m_cond,
            vapor_in.m_water_kg_s,
            m_hex_after_mt,
            H_flow_before_w,
            q_cell_w,
            hexane_enthalpy_in_w,
            sensible_heat_to_solid_w,
            c,
        )[0]

    # Bracket guard: the enthalpy-based V-SAT balance is referenced to water's
    # ATMOSPHERIC bp datum, so under an elevated dew point (raised P, or a
    # water-rich handoff from the DCZ below) condensing even the whole water
    # stream may not reach the dew curve -- brentq would then raise. The real
    # surface-water deposition is now handled by the sorption post-pass in
    # solve_ftrz_zone (keyed to the SOLID surface T_L, not this bulk-vapor
    # criterion), so this branch is a fallback: if no bracketed root exists,
    # keep the cell V-SCAL (no bulk condensation) rather than crash.
    if residual(0.0) * residual(upper_bound) > 0.0:
        vapor_out = VaporState(
            m_water_kg_s=vapor_in.m_water_kg_s, m_hex_kg_s=m_hex_after_mt, T=T_candidate
        )
        return vapor_out, 0.0, False

    m_cond = brentq(residual, 0.0, upper_bound)
    _, T_after, Y_V2_after = _energy_balance_residual(
        m_cond,
        vapor_in.m_water_kg_s,
        m_hex_after_mt,
        H_flow_before_w,
        q_cell_w,
        hexane_enthalpy_in_w,
        sensible_heat_to_solid_w,
        c,
    )
    m_water_after = vapor_in.m_water_kg_s - m_cond
    vapor_out = VaporState(
        m_water_kg_s=m_water_after, m_hex_kg_s=m_water_after * Y_V2_after, T=T_after
    )
    return vapor_out, m_cond, True


def cell_thickness_m(
    hexane_evap_kg_s: float,
    dH_vap_hexane: float,
    condensed_water_kg_s: float,
    dH_vap_water: float,
    hQ: float,
    T_V: float,
    T_L: float,
    A_bed_m2: float,
    alpha_L: float,
    aV_m2_per_m3: float,
    sensible_heat_w: float = 0.0,
    minimum_thickness_m: float = 1.0e-12,
) -> float:
    """Delta_z (eq. A.18): the cell thickness needed for the condensation
    -release + convective heat fluxes to deliver exactly enough energy to
    evaporate this cell's fixed hexane increment.

    NOTE on units (a documented resolution, not a verified-exact transcription
    — see module docstring on `aV`/`hQ`): `J_Q,cv = hQ*(T_V-T_L)` is
    unambiguously a flux (W/m^2, eq. A.20). For `J_Q,cs` (eq. A.19) to combine
    with it in the same units without `dz` circularly appearing on both
    sides, it's taken here as the cell's condensation heat-release rate
    (`dH_vap_water * condensed_water_kg_s`, W — independent of `dz`, since
    `condensed_water_kg_s` already comes out of `solve_ftrz_cell`'s energy
    balance) divided by the bed cross-sectional area, `J_Q,cs =
    dH_vap_water*condensed_water_kg_s / A_bed` (W/m^2).
    """
    required_heat_w = hexane_evap_kg_s * dH_vap_hexane + sensible_heat_w
    # A sufficiently superheated incoming matrix can fund this cell's pore-
    # solvent evaporation load. The limiting front then collapses toward zero
    # thickness; retain a tiny positive cell for ordered profile bookkeeping.
    if required_heat_w <= 0.0:
        return minimum_thickness_m

    J_Q_cv = hQ * (T_V - T_L)
    J_Q_cs = dH_vap_water * condensed_water_kg_s / A_bed_m2
    denominator = A_bed_m2 * alpha_L * aV_m2_per_m3 * (J_Q_cs + J_Q_cv)
    if denominator <= 0.0:
        raise ValueError(
            "FTRZ heat-transfer driving force must be positive "
            f"(T_V={T_V:.6g} K, T_L={T_L:.6g} K, J_Q_cv={J_Q_cv:.6g} W/m2, "
            f"J_Q_cs={J_Q_cs:.6g} W/m2, condensed_water={condensed_water_kg_s:.6g} kg/s, "
            f"required_heat={required_heat_w:.6g} W)"
        )
    return required_heat_w / denominator


@dataclass(frozen=True)
class FTRZZoneResult:
    cells: tuple[FTRZCellResult, ...]  # top-to-bottom order, matching zones/phz.py's convention
    L_FTRZ_m: float
    iterations: int

    @property
    def solid_out(self) -> SolidState:
        return self.cells[-1].solid

    @property
    def vapor_out(self) -> VaporState:
        return self.cells[0].vapor_out


def _thickness_bracket(
    residual_at: Callable[[float], float],
    minimum_dz: float,
    warm_dz: float | None,
) -> tuple[float, float] | None:
    """Bracket the positive cell root, preferring a safeguarded local search."""

    def search(candidates: tuple[float, ...]) -> tuple[float, float] | None:
        previous_valid: tuple[float, float] | None = None
        for candidate in candidates:
            try:
                residual = residual_at(candidate)
            except ValueError:
                continue
            if abs(residual) <= 1.0e-14:
                return candidate, candidate
            if previous_valid is not None and residual * previous_valid[1] < 0.0:
                return previous_valid[0], candidate
            previous_valid = candidate, residual
        return None

    if warm_dz is not None and math.isfinite(warm_dz) and warm_dz > 0.0:
        local_candidates = tuple(
            sorted(
                {
                    max(minimum_dz, warm_dz * factor)
                    for factor in (0.5, 1.0, 2.0)
                }
            )
        )
        local = search(local_candidates)
        if local is not None:
            return local

    global_candidates = tuple(
        multiplier * 10.0**exponent
        for exponent in range(-12, 2)
        for multiplier in (1.0, 2.0, 5.0)
    )
    return search(global_candidates)


def solve_ftrz_zone(
    nz: int,
    X2_sup: float,
    m_dry_kg_s: float,
    vapor_in: VaporState,  # zone's own inlet, at the BOTTOM (from the DCZ below)
    q_Iv_w_m3: float | Callable[[float], float],
    hQ: float,
    hM: float,
    aV_m2_per_m3: float,
    diameter_m: float,
    c: FTRZConstants,
    X1_sup: float = 0.0,  # feed moisture (kg/kg dry) descending in -- baseline for the sorption delta
    T_solid_sup: float | None = None,
    L_FTRZ_initial_guess_m: float = 0.02,
    max_outer_iter: int = 50,
    outer_tol_m: float = 1.0e-6,
) -> FTRZZoneResult:
    """Solve the FTRZ, discretized into `nz` cells of thickness computed from
    the energy balance (eq. A.18), via the fixed-point iteration the paper
    itself describes for the free boundary `L_FTRZ` ("updated after each
    iteration", §A.2.2): guess `L_FTRZ` -> obtain its local/length-averaged
    `q_Iv_w_m3` -> march the vapor bottom-to-top while solving each variable
    cell's implicit `dz_j = A.18(q_Iv*A_bed*dz_j)` closure -> recompute
    `L_FTRZ = sum(dz_j)` (eq. A.21) -> repeat until it stops moving. The outer
    iteration remains necessary when the free boundary crosses real trays and
    therefore changes the length-averaged duty density.
    """
    A_bed_m2 = math.pi / 4.0 * diameter_m**2
    alpha_L = 1.0 - c.bed_porosity

    X2_inf = thermo.x2_equilibrium(vapor_in.T, c.X3, c.gab, c.oil, c.alpha_pg, c.alpha_ps, c.rho_ps)
    hexane_evap_kg_s = m_dry_kg_s * (X2_sup - X2_inf) / nz
    T_solid_in = c.T_boil_hexane if T_solid_sup is None else T_solid_sup
    cp_feed = (
        c.cp_solid + X1_sup * c.cp_water_liquid + X2_sup * c.cp_hexane_liquid + c.X3 * c.cp_oil
    )
    cold_sensible_total_w = m_dry_kg_s * cp_feed * max(c.T_boil_hexane - T_solid_in, 0.0)
    total_hexane_latent_w = m_dry_kg_s * max(X2_sup - X2_inf, 0.0) * c.dH_vap_hexane
    # A superheated matrix may conduct stored energy inward to the wet core,
    # reducing the external heat needed for evaporation. It cannot contribute
    # more than the latent load present, and it is NOT injected into the vapor
    # a second time: the evaporated hexane already carries that enthalpy.
    matrix_credit_total_w = min(
        m_dry_kg_s * cp_feed * max(T_solid_in - c.T_boil_hexane, 0.0),
        total_hexane_latent_w,
    )
    vapor_sensible_load_cell_w = cold_sensible_total_w / nz
    front_sensible_adjustment_cell_w = (cold_sensible_total_w - matrix_credit_total_w) / nz
    matrix_temperature_drop_k = (
        matrix_credit_total_w / (m_dry_kg_s * cp_feed) if m_dry_kg_s * cp_feed > 0.0 else 0.0
    )

    L_FTRZ = L_FTRZ_initial_guess_m
    cells_bottom_to_top: list[FTRZCellResult] = []
    previous_cell_dz: tuple[float, ...] | None = None
    iterations = 0
    for iterations in range(1, max_outer_iter + 1):
        q_density_w_m3 = q_Iv_w_m3(L_FTRZ) if callable(q_Iv_w_m3) else q_Iv_w_m3
        cells_bottom_to_top = []
        vapor = vapor_in
        for k in range(nz):
            # The driving force behind this cell's heat/mass transfer (hence
            # T_L, X2_cr, and dz below) reflects the solid as it ENTERS the
            # cell, still carrying its wet core — using the EXIT state here
            # would make the bottommost cell (which exits exactly at X2_inf,
            # the zone's asymptotic floor) show w_h=0 and T_L=T_V exactly,
            # a zero driving force despite a finite amount of hexane still
            # being removed within that cell (division by zero in
            # `cell_thickness_m`). k=0 is the bottommost cell.
            X2_entrance = X2_inf + (k + 1) * (hexane_evap_kg_s / m_dry_kg_s)
            progress_from_top = (nz - k) / nz
            # Reported/exit state (matching zones/phz.py's "cell holds the
            # state after passing through it" convention): one increment
            # below the entrance value.
            X2_here = X2_inf + k * (hexane_evap_kg_s / m_dry_kg_s)

            def evaluate_thickness(
                candidate_dz_m: float,
            ) -> tuple[float, VaporState, float, bool, float]:
                # The mesh is energy-driven and therefore nonuniform. Jacket
                # duty must follow this cell's own candidate volume, not the
                # uniform L/nz share of the previous free-boundary iterate.
                # Solving dz = A.18(duty(dz)) removes that mesh-dependent
                # lag at the PHZ/FTRZ handover.
                candidate_q_w = q_density_w_m3 * A_bed_m2 * candidate_dz_m
                candidate_vapor, candidate_condensed, candidate_sat = solve_ftrz_cell(
                    vapor,
                    hexane_evap_kg_s,
                    candidate_q_w,
                    c,
                    sensible_heat_to_solid_w=vapor_sensible_load_cell_w,
                )
                candidate_T_L_front = min(
                    solid_temperature(
                        X2_entrance,
                        thermo.x2_critical(
                            c.alpha_pg,
                            thermo.rho_hexane_liquid(candidate_vapor.T),
                            c.alpha_ps,
                            c.rho_ps,
                            empirical=c.x2_critical_empirical,
                        ),
                        X2_inf,
                        c.T_boil_hexane,
                        candidate_vapor.T,
                    ),
                    vapor_in.T,
                )
                predicted_dz = cell_thickness_m(
                    hexane_evap_kg_s,
                    c.dH_vap_hexane,
                    candidate_condensed,
                    c.vapor_enthalpy_ref.dH_vap_water,
                    hQ,
                    candidate_vapor.T,
                    candidate_T_L_front,
                    A_bed_m2,
                    alpha_L,
                    aV_m2_per_m3,
                    sensible_heat_w=front_sensible_adjustment_cell_w,
                    minimum_thickness_m=1.0e-9 / nz,
                )
                return (
                    candidate_dz_m - predicted_dz,
                    candidate_vapor,
                    candidate_condensed,
                    candidate_sat,
                    candidate_T_L_front,
                )

            minimum_dz = 1.0e-9 / nz
            cell_heat_requirement_w = (
                hexane_evap_kg_s * c.dH_vap_hexane
                + front_sensible_adjustment_cell_w
            )
            collapsed_heat_tolerance_w = max(
                1.0e-9, 1.0e-12 * abs(hexane_evap_kg_s * c.dH_vap_hexane)
            )
            bracket: tuple[float, float] | None = (
                (minimum_dz, minimum_dz)
                if cell_heat_requirement_w <= collapsed_heat_tolerance_w
                else None
            )
            if bracket is None:
                warm_dz = previous_cell_dz[k] if previous_cell_dz is not None else None
                bracket = _thickness_bracket(
                    lambda candidate: evaluate_thickness(candidate)[0],
                    minimum_dz,
                    warm_dz,
                )
            if bracket is None:
                raise ValueError(
                    "FTRZ cell duty/thickness closure has no positive root; "
                    f"bottom-index={k}/{nz - 1}, vapor_in_T={vapor.T:.6g} K, "
                    f"X2_entrance={X2_entrance:.6g}, X2_inf={X2_inf:.6g}, "
                    f"L_guess={L_FTRZ:.6g} m, q_density={q_density_w_m3:.6g} W/m3"
                )
            dz = (
                bracket[0]
                if bracket[0] == bracket[1]
                else brentq(lambda value: evaluate_thickness(value)[0], *bracket)
            )
            _, vapor_out, condensed_kg_s, is_sat, T_L_front = evaluate_thickness(dz)
            if T_solid_in <= c.T_boil_hexane:
                # Cold meal approaches the receding-front temperature.
                T_L = T_L_front + (1.0 - progress_from_top) * (
                    T_solid_in - c.T_boil_hexane
                )
            else:
                # A hot matrix cools only by the energy actually credited to
                # pore evaporation; a vanishing front cannot dump all stored
                # sensible heat into the vapor.
                T_matrix = T_solid_in - progress_from_top * matrix_temperature_drop_k
                T_L = max(T_L_front, T_matrix)
            cells_bottom_to_top.append(
                FTRZCellResult(
                    solid=SolidState(T=T_L, X1=0.0, X2=X2_here),  # X1 filled in below
                    water_surface_T=T_L,
                    vapor_out=vapor_out,
                    dz_m=dz,
                    condensed_water_kg_s=condensed_kg_s,
                    bulk_condensed_water_kg_s=condensed_kg_s,
                    sorbed_water_kg_s=0.0,
                    sensible_heat_to_solid_w=vapor_sensible_load_cell_w,
                    is_saturated=is_sat,
                )
            )
            vapor = vapor_out

        L_FTRZ_new = sum(cell.dz_m for cell in cells_bottom_to_top)
        previous_cell_dz = tuple(cell.dz_m for cell in cells_bottom_to_top)
        if abs(L_FTRZ_new - L_FTRZ) < outer_tol_m:
            L_FTRZ = L_FTRZ_new
            break
        L_FTRZ = L_FTRZ_new

    # --- Water balance: surface sorption/condensation keyed to the SOLID SURFACE
    # (ADDITION beyond Coletto's hexane-only DT; the moisture-raising the DT exists
    # to do). Walk top-to-bottom (the solid's own descent). At each cell the cool
    # descending meal equilibrates toward its sorption isotherm Xe(a_w), with a_w
    # evaluated at the SOLID SURFACE temperature -- NOT the ascending steam's bulk
    # temperature, which stays superheated and would never condense (the real
    # mechanism is film condensation onto the 68-108 C meal surface, A&G / Kemper /
    # Paraiso; Gianini 2006 measured 19% wb at a_w=0.799 straight from a DT outlet).
    #
    # WATER-INTERFACE CLOSURE (an extension beyond Coletto's A.17): a wetted
    # interface cannot superheat past the local water dew point.  Keep this
    # interfacial temperature separate from the BULK meal temperature: A.17
    # remains the bulk-solid energy closure, while min(T_bulk, T_dew,water) is
    # used only to evaluate water activity and finite-rate sorption.
    #
    # Sorption is finite-rate.  Internal flake diffusion (15*D_water/r_P**2) and
    # external film transfer (hM*aV) act as series resistances; each cell only
    # approaches Xe for its own local inventory/throughput residence time.  The
    # sorbed water is debited from / released to the ascending steam (bounded by
    # what the steam carries). cells_bottom_to_top is in vapor-march order, so
    # walk it in reverse for the solid's top-to-bottom direction.
    cells_top_to_bottom = list(reversed(cells_bottom_to_top))
    n = len(cells_top_to_bottom)
    X1_abs = X1_sup
    sorbed_kg_s = [0.0] * n  # this cell's SORPTION water onto the meal (0 = V-SAT-only fallback)
    net_water_kg_s = [0.0] * n  # net water onto meal (sorption OR V-SAT), for the record
    water_surface_T = [0.0] * n
    delta_X1 = [0.0] * n
    water_taken_kg_s = 0.0  # cumulative isotherm water pulled from the ascending steam
    # Binary-VLE water-saturation floor: while liquid water is present on the cool meal
    # surface, the vapor in contact stays in equilibrium with it -- its water partial
    # pressure can't fall below the meal's own `a_w*p_sat,water(T_surface)` (the same
    # Luikov activity the sorption uses; a_w clamped to the isotherm's validated ceiling).
    # So the stream leaving the (coldest) top always keeps at least that saturation water
    # flow, and water/hexane always COEXIST -- the exit vapor is never pure hexane. This is
    # the binary VLE the mass-balance-only condensation otherwise ignores; without it a
    # weak/under-set sparge lets the DCZ+FTRZ strip the vapor to 0% water (physically
    # impossible). SLACK at realistic operation: the meal's own Xe limit stops the sorption
    # while the vapor is still well above saturation, so the floor doesn't bind there
    # (verified: calibrated dome stays ~11% water, unchanged). y_w and the floor are clamped
    # so a hot surface mid-iteration (p_sat -> P) can't blow the budget up or negative.
    m_water_floor = 0.0
    if c.luikov is not None and n > 0 and vapor_in.m_water_kg_s > 0.0:
        top = cells_top_to_bottom[0]
        T_dew_top = thermo.dew_point_temperature(
            top.vapor_out.Y_V2, c.antoine_water, P=c.pressure_pa
        )
        T_surf_top = min(top.solid.T, T_dew_top)
        p_sat_w = thermo.antoine_pressure_pa(T_surf_top, c.antoine_water)
        y_w = min(
            thermo.LUIKOV_MAX_VALIDATED_UR * p_sat_w / c.pressure_pa, 0.9
        )  # a_w*p_sat/P, clamped <1
        if y_w > 0.0:
            n_hex_top = top.vapor_out.m_hex_kg_s / thermo.M_HEXANE
            m_water_floor = min(
                n_hex_top * y_w / (1.0 - y_w) * thermo.M_WATER, 0.9 * vapor_in.m_water_kg_s
            )
    total_bulk_condensed_kg_s = sum(cell.bulk_condensed_water_kg_s for cell in cells_top_to_bottom)
    # Bulk V-SAT condensation was already removed during the vapor march.  Only
    # the remaining vapor above the VLE floor is available to the sorption pass.
    vapor_water_budget = max(vapor_in.m_water_kg_s - total_bulk_condensed_kg_s - m_water_floor, 0.0)
    alpha_L = 1.0 - c.bed_porosity
    dry_solid_velocity_m_s = m_dry_kg_s / (c.alpha_ps * alpha_L * c.rho_ps * A_bed_m2)
    transfer_rate_s = water_transfer_rate_s(
        c.water_diffusivity, c.particle_radius, hM, aV_m2_per_m3
    )
    for i, cell in enumerate(cells_top_to_bottom):
        water_surface_T[i] = cell.solid.T
        bulk_condensed = cell.bulk_condensed_water_kg_s
        X1_abs += bulk_condensed / m_dry_kg_s
        net_water_kg_s[i] = bulk_condensed
        if c.luikov is not None:
            Y_V2 = cell.vapor_out.Y_V2
            T_dew = thermo.dew_point_temperature(Y_V2, c.antoine_water, P=c.pressure_pa)
            T_L_wet = min(cell.solid.T, T_dew)  # evaporative pinning
            water_surface_T[i] = T_L_wet
            a_w = thermo.water_activity(Y_V2, T_L_wet, c.antoine_water, P=c.pressure_pa)
            a_w = min(max(a_w, 1.0e-9), thermo.LUIKOV_MAX_VALIDATED_UR)
            Xe = thermo.luikov_equilibrium_moisture(a_w, c.luikov)
            contact_time_s = cell.dz_m / dry_solid_velocity_m_s
            X1_relaxed = relax_moisture(X1_abs, Xe, contact_time_s, transfer_rate_s)
            dwater = m_dry_kg_s * (X1_relaxed - X1_abs)  # >0 adsorb, <0 desorb
            if dwater > 0.0:
                dwater = min(dwater, max(vapor_water_budget - water_taken_kg_s, 0.0))
            X1_abs += dwater / m_dry_kg_s
            water_taken_kg_s += dwater
            sorbed_kg_s[i] = dwater
            net_water_kg_s[i] += dwater
        delta_X1[i] = X1_abs - X1_sup  # zone reports the moisture GAIN; caller adds feed baseline

    # Debit the sorbed water from the ASCENDING steam (mass conservation): the vapor
    # rises bottom->top, so the stream leaving cell i's top face has lost the water
    # sorbed by every cell at/below it -- suffix sum from i. (V-SAT condensate is
    # already reflected in the marched vapor_out, so only the sorption term is debited
    # here.) The zone-inlet budget cap above keeps the dome (i=0) stream non-negative.
    suffix = [0.0] * n
    acc = 0.0
    for i in range(n - 1, -1, -1):
        acc += sorbed_kg_s[i]
        suffix[i] = acc
    finished_cells = []
    for i, cell in enumerate(cells_top_to_bottom):
        v = cell.vapor_out
        debited_water = max(v.m_water_kg_s - suffix[i], 0.0)
        finished_cells.append(
            FTRZCellResult(
                # Coletto A.17 remains the bulk-meal temperature.  The separate
                # water interface above must never overwrite this energy state.
                solid=SolidState(T=cell.solid.T, X1=delta_X1[i], X2=cell.solid.X2),
                water_surface_T=water_surface_T[i],
                vapor_out=VaporState(m_water_kg_s=debited_water, m_hex_kg_s=v.m_hex_kg_s, T=v.T),
                dz_m=cell.dz_m,
                condensed_water_kg_s=net_water_kg_s[i],
                bulk_condensed_water_kg_s=cell.bulk_condensed_water_kg_s,
                sorbed_water_kg_s=sorbed_kg_s[i],
                sensible_heat_to_solid_w=cell.sensible_heat_to_solid_w,
                is_saturated=cell.is_saturated,
            )
        )

    return FTRZZoneResult(cells=tuple(finished_cells), L_FTRZ_m=L_FTRZ, iterations=iterations)
