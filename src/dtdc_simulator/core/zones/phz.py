"""Pre-Heating Zone (PHZ) sub-model — Coletto, Bandoni & Blanco (2022), §2.2 and
§7.3/Table A.1 (BuildSpec). M2 Phase 1 (BuildSpec §14): a standalone, pure,
unit-tested per-tray solver — not yet wired into `core/model.py` (that's M2
Phase 4, the tray-by-tray fixed-point sweep).

Per §2.1, each tray in a zone is discretized into `nz` axial cells of uniform
height; indirect steam duty is per-tray (`Q_indirect_w`, matching the existing
`indirect_steam` MV) and its volumetric rate is spread uniformly across that
tray's cells (eq. A.2a). Only the solid's temperature and hexane content are
tracked (Table A.1 has no water mass balance for the PHZ solid — moisture is
carried at a constant value throughout, only used for the mixture heat
capacity); hexane evaporation switches on only once a cell reaches
`T_boil_hexane` (eq. A.1a).

GAPS THE PAPER LEAVES IMPLICIT for PHZ specifically (flagged, not hidden):
- The "mixture" heat capacity §2.1 specifies (eqs. B.1-B.6) combines solid +
  interstitial pore vapor; here we use only the solid-stream heat capacity
  (`core.thermo.cp_l`), neglecting the interstitial vapor's contribution — its
  mass is tiny relative to the solid at typical bed voidage. Flagged for
  refinement if validation against Fig. 7 needs it.
- The paper gives explicit vapor energy-source closures for FTRZ (eq. A.10-11)
  and DCZ (eq. A.34) but not PHZ. The vapor temperature update here is a
  documented placeholder (a fractional approach toward the solid's cell
  temperature) — not derived from the paper. The solid-side profile (the one
  BuildSpec's M2 acceptance criteria and Fig. 7(a) validate) does not depend on
  this choice; only the vapor profile (Fig. 7(b), secondary) does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from dtdc_simulator.core import thermo

VAPOR_SOLID_CONTACT_FRACTION = 0.3  # [PLACE] not from the paper — see module docstring


@dataclass(frozen=True)
class PHZConstants:
    T_boil_hexane: float
    dH_vap_hexane: float
    cp_solid: float
    cp_water_liquid: float
    cp_hexane_liquid: float
    cp_oil: float
    cp_water_vapor: float
    cp_hexane_vapor: float


@dataclass(frozen=True)
class SolidState:
    T: float  # K
    X2: float  # kg hexane / kg dry solid


@dataclass(frozen=True)
class VaporState:
    wV1: float  # water mass fraction
    wV2: float  # hexane mass fraction
    T: float  # K


@dataclass(frozen=True)
class PHZCellResult:
    solid_out: SolidState
    vapor_out: VaporState
    hexane_evaporated_kg_s: float
    z_from_top_m: float  # position of this cell's solid outlet, top of tray = 0


@dataclass(frozen=True)
class PHZTrayResult:
    cells: tuple[PHZCellResult, ...]

    @property
    def solid_out(self) -> SolidState:
        return self.cells[-1].solid_out

    @property
    def vapor_out(self) -> VaporState:
        return self.cells[-1].vapor_out


def _mixture_cp_per_kg_dry_solid(X1: float, X2: float, X3: float, c: PHZConstants) -> float:
    """Effective heat capacity of the wet solid stream, J/(kg dry solid . K)
    (eq. B.5 applied to the solid's own composition; interstitial pore-vapor
    contribution neglected — see module docstring). Delegates to
    `thermo.mixture_cp_per_kg_dry_solid` (promoted there so `core/balance.py`'s
    independent energy checks share the exact same formula) -- this stays as
    a thin, PHZConstants-shaped wrapper purely for this module's own call
    -site convenience."""
    return thermo.mixture_cp_per_kg_dry_solid(
        X1, X2, X3, c.cp_water_liquid, c.cp_hexane_liquid, c.cp_oil, c.cp_solid
    )


def solve_phz_cell(
    solid_in: SolidState,
    vapor_in: VaporState,
    q_cell_w: float,
    m_dry_kg_s: float,
    m_vapor_water_kg_s: float,
    X1: float,
    X3: float,
    c: PHZConstants,
) -> PHZCellResult:
    """One PHZ cell: solid sensibly heats to `T_boil_hexane` (eq. A.1a's
    `S_Lm2=0` branch), then evaporates hexane isothermally (the other branch).
    `m_dry_kg_s` is the (constant) dry-solid mass flow; `m_vapor_water_kg_s` is
    the (constant, per Table A.1's water mass balance) vapor water mass flow.
    """
    cp_mix = _mixture_cp_per_kg_dry_solid(X1, solid_in.X2, X3, c)
    E_preheat_w = m_dry_kg_s * cp_mix * max(c.T_boil_hexane - solid_in.T, 0.0)

    if q_cell_w < E_preheat_w:
        T_out = solid_in.T + q_cell_w / (m_dry_kg_s * cp_mix)
        X2_out = solid_in.X2
        hexane_evap_kg_s = 0.0
    else:
        q_remaining_w = q_cell_w - E_preheat_w
        hexane_evap_kg_s = q_remaining_w / c.dH_vap_hexane
        X2_out = solid_in.X2 - hexane_evap_kg_s / m_dry_kg_s
        if X2_out < 0.0:
            # More heat than available hexane — shouldn't happen at PHZ's
            # intended duties (Coletto §2.2: PHZ always exits with X2 > 0),
            # but clamp rather than produce a negative hexane content.
            hexane_evap_kg_s = solid_in.X2 * m_dry_kg_s
            X2_out = 0.0
        T_out = c.T_boil_hexane

    solid_out = SolidState(T=T_out, X2=X2_out)

    # Vapor mass balance: water conserved (Table A.1), hexane gains exactly
    # what the solid lost.
    m_hex_in_kg_s = m_vapor_water_kg_s * vapor_in.wV2 / vapor_in.wV1
    m_hex_out_kg_s = m_hex_in_kg_s + hexane_evap_kg_s
    m_vapor_total_out = m_vapor_water_kg_s + m_hex_out_kg_s
    wV1_out = m_vapor_water_kg_s / m_vapor_total_out
    wV2_out = m_hex_out_kg_s / m_vapor_total_out

    # Vapor temperature: documented placeholder (see module docstring) — moves
    # a fraction of the way toward the solid's own (post-step) temperature.
    T_vapor_out = vapor_in.T + (T_out - vapor_in.T) * VAPOR_SOLID_CONTACT_FRACTION

    vapor_out = VaporState(wV1=wV1_out, wV2=wV2_out, T=T_vapor_out)
    return PHZCellResult(
        solid_out=solid_out,
        vapor_out=vapor_out,
        hexane_evaporated_kg_s=hexane_evap_kg_s,
        z_from_top_m=0.0,  # filled in by solve_phz_tray, which knows cell height
    )


def solve_phz_tray(
    nz: int,
    bed_height_m: float,
    diameter_m: float,
    Q_indirect_w: float,
    solid_in: SolidState,
    vapor_in: VaporState,
    m_dry_kg_s: float,
    m_vapor_water_kg_s: float,
    X1: float,
    X3: float,
    c: PHZConstants,
) -> PHZTrayResult:
    """Solve one PHZ tray, discretized into `nz` cells (eq. A.2a: `q_Iv =
    Q_I/(A_bed*L_PHZ)`, spread uniformly — for a uniform cell height this
    reduces to an equal per-cell split of `Q_indirect_w`, independent of
    `A_bed`, which is why it's not used in the energy split below; kept as a
    parameter so `bed_height_m` can be used for cell z-position bookkeeping).

    Solid marches top-to-bottom (cell 1 first). Vapor is passed alongside in
    the same loop as an approximation — see `solve_phz_zone`'s docstring for
    why true counter-current vapor coupling across trays is deferred to M2
    Phase 4, not attempted here.
    """
    dz = bed_height_m / nz
    q_cell_w = Q_indirect_w / nz  # uniform split of the tray's total duty across its nz cells

    cells: list[PHZCellResult] = []
    solid = solid_in
    vapor = vapor_in
    for i in range(nz):
        result = solve_phz_cell(solid, vapor, q_cell_w, m_dry_kg_s, m_vapor_water_kg_s, X1, X3, c)
        result = replace(result, z_from_top_m=(i + 1) * dz)
        cells.append(result)
        solid = result.solid_out
        vapor = result.vapor_out

    return PHZTrayResult(cells=tuple(cells))


def solve_phz_zone(
    trays: list[tuple[int, float, float, float]],
    solid_in: SolidState,
    vapor_ins: list[VaporState],
    m_dry_kg_s: float,
    m_vapor_water_kg_s: float,
    X1: float,
    X3: float,
    c: PHZConstants,
) -> list[PHZTrayResult]:
    """Chain multiple PHZ trays by their SOLID stream only (e.g. PD1->PD2->
    PD3): each tray's solid outlet feeds the next tray's solid inlet.

    Vapor is *not* chained tray-to-tray here: it physically flows opposite to
    the solid (bottom tray's outlet feeds the tray above it), which this
    standalone solver doesn't attempt to resolve self-consistently — that's
    exactly the counter-current fixed-point coupling M2 Phase 4 (BuildSpec
    §7.8/Fig. 5: tray-by-tray, outer-iterated) exists to solve. Instead each
    tray's vapor inlet is supplied explicitly via `vapor_ins` (one entry per
    tray, same order as `trays`), e.g. from interpolated literature/base-case
    values — mirroring how Coletto's own Fig. 5 algorithm *initializes* these
    boundary conditions before the outer loop converges them.

    `trays`: list of `(nz, bed_height_m, diameter_m, Q_indirect_w)` per tray,
    in solid-flow order (top tray first).
    """
    if len(trays) != len(vapor_ins):
        raise ValueError(f"trays ({len(trays)}) and vapor_ins ({len(vapor_ins)}) must match 1:1")

    results: list[PHZTrayResult] = []
    solid = solid_in
    for (nz, bed_height_m, diameter_m, Q_indirect_w), vapor_in in zip(trays, vapor_ins):
        tray_result = solve_phz_tray(
            nz,
            bed_height_m,
            diameter_m,
            Q_indirect_w,
            solid,
            vapor_in,
            m_dry_kg_s,
            m_vapor_water_kg_s,
            X1,
            X3,
            c,
        )
        results.append(tray_result)
        solid = tray_result.solid_out
    return results
