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
- moisture gain equals whatever water the vapor condensed in that cell
  (mass-conservative).

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
    vapor_out: VaporState  # vapor leaving this cell (toward the top)
    dz_m: float
    condensed_water_kg_s: float
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


def _energy_balance_residual(
    m_cond_kg_s: float,
    m_water_before: float,
    m_hex_after_mt: float,
    H_flow_before_w: float,
    q_cell_w: float,
    hexane_enthalpy_in_w: float,
    c: FTRZConstants,
) -> tuple[float, float, float]:
    """Residual of the cell energy balance when pinned to the dew curve
    (V-SAT): returns `(residual, T_after, Y_V2_after)` for a candidate
    `m_cond_kg_s`."""
    m_water_after = m_water_before - m_cond_kg_s
    Y_V2_after = m_hex_after_mt / m_water_after
    T_after = thermo.dew_point_temperature(Y_V2_after, c.antoine_water)
    H_vbw_after = thermo.vapor_enthalpy_water_basis(Y_V2_after, T_after, c.vapor_enthalpy_ref)
    condensate_enthalpy_out_w = m_cond_kg_s * c.cp_water_liquid * (T_after - c.T_boil_water)
    lhs = m_water_after * H_vbw_after
    rhs = H_flow_before_w + q_cell_w + hexane_enthalpy_in_w - condensate_enthalpy_out_w
    return lhs - rhs, T_after, Y_V2_after


def solve_ftrz_cell(
    vapor_in: VaporState,
    hexane_evap_kg_s: float,
    q_cell_w: float,
    c: FTRZConstants,
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
    H_flow_candidate_w = H_flow_before_w + q_cell_w + hexane_enthalpy_in_w
    T_candidate = thermo.temperature_from_vapor_enthalpy(
        H_flow_candidate_w / vapor_in.m_water_kg_s, Y_V2_candidate, c.vapor_enthalpy_ref
    )
    T_dew_candidate = thermo.dew_point_temperature(Y_V2_candidate, c.antoine_water)

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
            c,
        )[0]

    m_cond = brentq(residual, 0.0, upper_bound)
    _, T_after, Y_V2_after = _energy_balance_residual(
        m_cond,
        vapor_in.m_water_kg_s,
        m_hex_after_mt,
        H_flow_before_w,
        q_cell_w,
        hexane_enthalpy_in_w,
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
    J_Q_cv = hQ * (T_V - T_L)
    J_Q_cs = dH_vap_water * condensed_water_kg_s / A_bed_m2
    denominator = A_bed_m2 * alpha_L * aV_m2_per_m3 * (J_Q_cs + J_Q_cv)
    return hexane_evap_kg_s * dH_vap_hexane / denominator


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


def solve_ftrz_zone(
    nz: int,
    X2_sup: float,
    m_dry_kg_s: float,
    vapor_in: VaporState,  # zone's own inlet, at the BOTTOM (from the DCZ below)
    q_Iv_w_m3: float,
    hQ: float,
    aV_m2_per_m3: float,
    diameter_m: float,
    c: FTRZConstants,
    L_FTRZ_initial_guess_m: float = 0.02,
    max_outer_iter: int = 50,
    outer_tol_m: float = 1.0e-6,
) -> FTRZZoneResult:
    """Solve the FTRZ, discretized into `nz` cells of thickness computed from
    the energy balance (eq. A.18), via the fixed-point iteration the paper
    itself describes for the free boundary `L_FTRZ` ("updated after each
    iteration", §A.2.2): guess `L_FTRZ` -> size each cell's duty from the
    constant `q_Iv_w_m3` and the current guess (`q_Iv_w_m3*A_bed*(L_FTRZ/nz)`)
    -> march the vapor bottom-to-top solving each cell (`solve_ftrz_cell` +
    `cell_thickness_m`) -> recompute `L_FTRZ = sum(dz_j)` (eq. A.21) -> repeat
    until it stops moving.
    """
    A_bed_m2 = math.pi / 4.0 * diameter_m**2
    alpha_L = 1.0 - c.bed_porosity

    X2_inf = thermo.x2_equilibrium(vapor_in.T, c.X3, c.gab, c.oil, c.alpha_pg, c.alpha_ps, c.rho_ps)
    hexane_evap_kg_s = m_dry_kg_s * (X2_sup - X2_inf) / nz

    L_FTRZ = L_FTRZ_initial_guess_m
    cells_bottom_to_top: list[FTRZCellResult] = []
    iterations = 0
    for iterations in range(1, max_outer_iter + 1):
        q_cell_w = (
            q_Iv_w_m3 * A_bed_m2 * (L_FTRZ / nz)
        )  # uniform share per cell of this iteration's guess

        cells_bottom_to_top = []
        vapor = vapor_in
        for k in range(nz):
            vapor_out, condensed_kg_s, is_sat = solve_ftrz_cell(
                vapor, hexane_evap_kg_s, q_cell_w, c
            )
            # The driving force behind this cell's heat/mass transfer (hence
            # T_L, X2_cr, and dz below) reflects the solid as it ENTERS the
            # cell, still carrying its wet core — using the EXIT state here
            # would make the bottommost cell (which exits exactly at X2_inf,
            # the zone's asymptotic floor) show w_h=0 and T_L=T_V exactly,
            # a zero driving force despite a finite amount of hexane still
            # being removed within that cell (division by zero in
            # `cell_thickness_m`). k=0 is the bottommost cell.
            X2_entrance = X2_inf + (k + 1) * (hexane_evap_kg_s / m_dry_kg_s)
            X2_cr = thermo.x2_critical(
                c.alpha_pg, thermo.rho_hexane_liquid(vapor_out.T), c.alpha_ps, c.rho_ps
            )
            T_L = solid_temperature(X2_entrance, X2_cr, X2_inf, c.T_boil_hexane, vapor_out.T)
            # Reported/exit state (matching zones/phz.py's "cell holds the
            # state after passing through it" convention): one increment
            # below the entrance value.
            X2_here = X2_inf + k * (hexane_evap_kg_s / m_dry_kg_s)
            dz = cell_thickness_m(
                hexane_evap_kg_s,
                c.dH_vap_hexane,
                condensed_kg_s,
                c.vapor_enthalpy_ref.dH_vap_water,
                hQ,
                vapor_out.T,
                T_L,
                A_bed_m2,
                alpha_L,
                aV_m2_per_m3,
            )
            cells_bottom_to_top.append(
                FTRZCellResult(
                    solid=SolidState(T=T_L, X1=0.0, X2=X2_here),  # X1 filled in below
                    vapor_out=vapor_out,
                    dz_m=dz,
                    condensed_water_kg_s=condensed_kg_s,
                    is_saturated=is_sat,
                )
            )
            vapor = vapor_out

        L_FTRZ_new = sum(cell.dz_m for cell in cells_bottom_to_top)
        if abs(L_FTRZ_new - L_FTRZ) < outer_tol_m:
            L_FTRZ = L_FTRZ_new
            break
        L_FTRZ = L_FTRZ_new

    # Accumulate solid moisture from condensed water, top-to-bottom (X1 rises
    # as the solid descends and picks up condensate) -- cells_bottom_to_top is
    # in vapor-march (bottom-to-top) order, so walk it in reverse for the
    # solid's own (top-to-bottom) direction.
    cells_top_to_bottom = list(reversed(cells_bottom_to_top))
    X1 = 0.0
    finished_cells: list[FTRZCellResult] = []
    for cell in cells_top_to_bottom:
        X1 = X1 + cell.condensed_water_kg_s / m_dry_kg_s
        finished_cells.append(
            FTRZCellResult(
                solid=SolidState(T=cell.solid.T, X1=X1, X2=cell.solid.X2),
                vapor_out=cell.vapor_out,
                dz_m=cell.dz_m,
                condensed_water_kg_s=cell.condensed_water_kg_s,
                is_saturated=cell.is_saturated,
            )
        )

    return FTRZZoneResult(cells=tuple(finished_cells), L_FTRZ_m=L_FTRZ, iterations=iterations)
