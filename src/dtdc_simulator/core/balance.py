"""Independent mass/energy conservation residual checks — one function per
zone (`core/zones/phz.py`, `ftrz.py`, `dcz.py`, `core/dc.py`), plus one for
the whole-DT handoff arithmetic in `core/dt_solver.py`.

WHY THIS MODULE EXISTS: a real bug shipped in `zones/dcz.py`'s
`solve_dcz_zone` (see DECISIONS.md's "DCZ moisture latent heat" entries) — a
latent-heat term got credited into the vapor energy balance twice, once
directly within an outer iteration, once again one-iteration-lagged. It was
caught indirectly (two unrelated sanity tests broke), not by any dedicated
conservation check — none existed anywhere in this codebase. Every
"conservation" test that DID exist reused the same internal value on both
sides of its own assertion (not an independent recomputation), and none of
them checked energy (joules) at all, only mass.

DESIGN PRINCIPLE, deliberately followed throughout this module: every
function here takes ONLY a zone's *external* boundary (its own feed/exit
states, duties, steam) plus its already-computed result object — never a
zone's own INTERNAL lagged/iterative state (`water_latent_w_m3`, `q_condL`,
`sorption_sink_w_m3`, etc.). A bug living in that internal state can cancel
out against the identical bug in a check that reuses it; a check built only
from boundary conditions and reported outputs cannot silently agree with a
wrong internal computation. Where a formula here necessarily reuses
production code, it is *only* ever a well-isolated, independently-tested
CONSTITUTIVE primitive (`thermo.cp_l`, `thermo.mixture_cp_per_kg_dry_solid`,
`thermo.vapor_enthalpy_water_basis`, `thermo.x2_equilibrium`) — pure
properties of matter, not a zone's own suspect balance logic.

TOLERANCE PHILOSOPHY: mass residuals should be exact to numerical-solver
precision (these zones' own internal mass bookkeeping is closed-form or
tightly-converged) — tests assert tight relative tolerances. Energy residuals
are exact for PHZ/FTRZ (whose own energy balances are closed-form/root-solved
within a single pass, not iteratively lagged) but are a documented
APPROXIMATION for DCZ specifically: hexane's own true isosteric heat of
sorption (`thermo.heat_of_sorption`, eq. A.31) varies nonlinearly with
coverage and can run several times `dH_vap_hexane` at low coverage (see
DECISIONS.md's "DT runaway temperature" entry) — `dcz_zone_balance` uses
plain `dH_vap_hexane` for this term (same simplification PHZ/FTRZ already
use for their own hexane terms), so its OWN energy residual carries real,
expected slop attributable to that approximation, not necessarily a bug. The
WATER/latent-heat term (the actual class of bug this module exists to catch)
is NOT approximated — it uses the exact `dH_vap_water` DCZ itself uses.

RESOLVED (found empirically while building this module, then fixed the same
session — see `march_particle_mass`'s own docstring and DECISIONS.md's "DCZ
particle hexane mass-conservation gap" entry for the full diagnostic trail):
`core/zones/particle.py`'s `march_particle_mass` used to not conserve hexane
mass between its own bulk-content (`X2,so`) and surface-flux diagnostics —
confirmed via an isolated, bed-scale-independent test at a STABLE ~18.6x
ratio, later traced to discretizing eq. A.29 (Coletto's own `Ca`-simplified
form of the governing equation) without a correctly-derived boundary
condition for that simplified form. Fixed by discretizing the original eq.
A.22 directly instead, using `X2,total` as the finite-volume method's own
accumulation variable — verified via a fixed-total-time/sub-step
convergence test to ~1-2% at production-realistic timesteps, cleanly
shrinking further with either finer sub-stepping or a finer mesh (the
signature of a genuinely conservative scheme). `dcz_zone_balance`'s own
`hexane_kg_s`/`water_kg_s`/`energy_w` residuals are now small (order a few
percent, not orders of magnitude) — the SMALL remaining slop reflects this
function's OWN documented approximations (plain `dH_vap_hexane`, a midpoint
-average solid `cp`) and ordinary bed-scale Gauss-Seidel convergence
looseness, not the FVM gap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from dtdc_simulator.core import dc as dc_mod
from dtdc_simulator.core import thermo
from dtdc_simulator.core.zones import dcz
from dtdc_simulator.core.zones import ftrz as ftrz_mod
from dtdc_simulator.core.zones import particle as pt
from dtdc_simulator.core.zones import phz as phz_mod


@dataclass(frozen=True)
class MassEnergyResidual:
    """`(should-be - actually-reported)` for each conserved quantity — ~0
    means the zone conserves that quantity within numerical precision.
    `hexane_kg_s`/`water_kg_s` are 0.0 when a zone doesn't track that
    species at all (e.g. PHZ has no water balance, Table A.1)."""

    hexane_kg_s: float = 0.0
    water_kg_s: float = 0.0
    energy_w: float = 0.0


# ---------------------------------------------------------------------------
# PHZ
# ---------------------------------------------------------------------------


def phz_zone_balance(
    solid_in: phz_mod.SolidState,
    result: phz_mod.PHZTrayResult,
    Q_indirect_w: float,
    m_dry_kg_s: float,
    X1: float,
    X3: float,
    c: phz_mod.PHZConstants,
) -> MassEnergyResidual:
    """Hexane mass + solid-side energy, checked per-cell (not just zone
    entry/exit) so the residual can't hide a per-cell sign/term error that
    happens to cancel out zone-wide. Vapor-side energy is deliberately NOT
    checked — PHZ's vapor temperature is an explicitly documented
    non-physical placeholder (`zones/phz.py`'s own module docstring),
    asserting energy balance against it would assert a known-false premise.
    """
    nz = len(result.cells)
    q_cell_w = Q_indirect_w / nz

    hexane_residual = m_dry_kg_s * (solid_in.X2 - result.solid_out.X2) - sum(
        cell.hexane_evaporated_kg_s for cell in result.cells
    )

    energy_residual = 0.0
    T_prev, X2_prev = solid_in.T, solid_in.X2
    for cell in result.cells:
        cp_mix = thermo.mixture_cp_per_kg_dry_solid(
            X1, X2_prev, X3, c.cp_water_liquid, c.cp_hexane_liquid, c.cp_oil, c.cp_solid
        )
        expected_w = (
            m_dry_kg_s * cp_mix * (cell.solid_out.T - T_prev)
            + cell.hexane_evaporated_kg_s * c.dH_vap_hexane
        )
        energy_residual += q_cell_w - expected_w
        T_prev, X2_prev = cell.solid_out.T, cell.solid_out.X2

    return MassEnergyResidual(hexane_kg_s=hexane_residual, energy_w=energy_residual)


# ---------------------------------------------------------------------------
# FTRZ
# ---------------------------------------------------------------------------


def ftrz_zone_balance(
    vapor_in: ftrz_mod.VaporState,
    result: ftrz_mod.FTRZZoneResult,
    q_Iv_w_m3: float,
    m_dry_kg_s: float,
    X2_sup: float,
    diameter_m: float,
    c: ftrz_mod.FTRZConstants,
) -> MassEnergyResidual:
    """Hexane + water mass (the two-sided "solid gained what vapor lost"
    identity, generalizing `test_ftrz.py`'s own strongest existing check)
    and vapor-side energy via `thermo.vapor_enthalpy_water_basis` — the SAME
    enthalpy machinery `solve_ftrz_cell` itself uses (appropriate to reuse:
    it's a well-isolated, independently-tested primitive, not this zone's
    own per-cell logic), applied here only at the zone's own EXTERNAL
    boundary, not to any of FTRZ's internal per-cell state.
    """
    X2_inf = thermo.x2_equilibrium(
        vapor_in.T, c.X3, c.gab, c.oil, c.alpha_pg, c.alpha_ps, c.rho_ps
    )
    total_hexane_evap_kg_s = m_dry_kg_s * (X2_sup - X2_inf)
    hexane_residual = (
        result.vapor_out.m_hex_kg_s - vapor_in.m_hex_kg_s
    ) - total_hexane_evap_kg_s

    total_condensed_kg_s = sum(cell.condensed_water_kg_s for cell in result.cells)
    water_residual = (vapor_in.m_water_kg_s - result.vapor_out.m_water_kg_s) - (
        m_dry_kg_s * result.solid_out.X1
    )
    # (also implies total_condensed_kg_s == m_dry*X1 when this is ~0; kept as
    # one combined identity rather than two, since either alone implies the
    # other given how `solve_ftrz_zone` accumulates X1 from condensed mass.)

    A_bed_m2 = math.pi / 4.0 * diameter_m**2
    total_duty_w = q_Iv_w_m3 * A_bed_m2 * result.L_FTRZ_m
    total_hexane_enthalpy_in_w = total_hexane_evap_kg_s * c.dH_vap_hexane
    total_condensate_enthalpy_out_w = sum(
        cell.condensed_water_kg_s * c.cp_water_liquid * (cell.vapor_out.T - c.T_boil_water)
        for cell in result.cells
    )
    H_in_w = vapor_in.m_water_kg_s * thermo.vapor_enthalpy_water_basis(
        vapor_in.Y_V2, vapor_in.T, c.vapor_enthalpy_ref
    )
    H_out_w = result.vapor_out.m_water_kg_s * thermo.vapor_enthalpy_water_basis(
        result.vapor_out.Y_V2, result.vapor_out.T, c.vapor_enthalpy_ref
    )
    energy_residual = H_out_w - (
        H_in_w + total_duty_w + total_hexane_enthalpy_in_w - total_condensate_enthalpy_out_w
    )
    _ = total_condensed_kg_s  # documented above; not asserted separately

    return MassEnergyResidual(
        hexane_kg_s=hexane_residual, water_kg_s=water_residual, energy_w=energy_residual
    )


# ---------------------------------------------------------------------------
# DCZ -- the flagship check (this is where the bug was)
# ---------------------------------------------------------------------------


def dcz_zone_balance(
    vapor_inf: dcz.VaporState,
    T_L_sup: float,
    X1_in: float,
    result: dcz.DCZZoneResult,
    q_Iv_w_m3: float | tuple[float, ...],
    m_dry_kg_s: float,
    m_vapor_kg_s: float,
    nz: int,
    c: dcz.DCZConstants,
) -> MassEnergyResidual:
    """Independent hexane + water mass balance (solid-side X1/X2 change vs.
    vapor-side content implied by `vapor_inf`/`vapor_out`), plus an energy
    balance using DCZ's OWN lumped-`cp_V` + explicit-latent-heat accounting
    convention (matching `solve_dcz_zone`'s own step 2 `source` term shape:
    `q_Iv - hexane_sorption_sink + water_latent`) rather than a more
    detailed per-species vapor enthalpy model DCZ doesn't itself use (which
    would flag spurious residuals unrelated to any real bug). Raises if
    `result` did not actually converge (`iterations < outer_max_iter` is the
    caller's own responsibility to have confirmed -- under-relaxation means
    intermediate iterations are not balance-conservative by construction,
    see `solve_dcz_zone`'s own module docstring).

    `X2_in` (the solid's own hexane content entering DCZ) is not a parameter
    of `solve_dcz_zone` at all -- it's implicitly `wpg2=1.0` uniformly (pores
    saturated with hexane vapor, `a_h=1`), i.e. `thermo.x2_equilibrium`
    evaluated at `T_L_sup` (exactly FTRZ's own termination condition) -- so
    it's recomputed here the same way, not taken as a redundant caller
    -supplied value that could drift out of sync with what was actually
    solved.

    RESOLVED GAP (found via this function itself, empirically, building this
    quality gate, then fixed the same session -- see DECISIONS.md's "DCZ
    particle hexane mass-conservation gap" entry for the full diagnostic
    trail): `hexane_kg_s` used to be far from zero -- an isolated single
    -particle test (no bed-scale coupling at all) showed `march_particle_
    mass`'s own FVM NOT conserving `X2,so` (adsorbed+absorbed hexane, eq.
    A.26) against its own `hexane_flux_to_vapor_kg_m2_s` surface-flux
    diagnostic, a STABLE ~18.6x ratio ruling out a simple timestep error.
    Traced to discretizing eq. A.29 (Coletto's own `Ca`-simplified form)
    without a correctly-derived boundary condition for that simplified form
    -- fixed by discretizing the original eq. A.22 directly (`X2,total` as
    the FVM's own accumulation variable), verified via a proper fixed-total
    -time/sub-step convergence test to ~1-2% at production timesteps.

    PRACTICAL CONSEQUENCE for this function's OWN other residuals:
    `water_kg_s`, as computed here, DERIVES the vapor's own water content
    from its TOTAL mass via `(1 - wV2)`, so it still inherits SOME of
    `wV2`'s own remaining (now small) imprecision whenever hexane transfer
    is non-negligible -- callers should still expect a few-percent-scale
    residual here, not machine precision, but no longer the order-of
    -magnitude noise the unresolved FVM gap used to cause. The solid-side
    `total_water_to_solid_kg_s` (computed independent of `wV2` entirely)
    remains the most reliable water cross-check when available. `energy_w`
    carries its own separate, DELIBERATE approximation (plain `dH_vap_
    hexane` for hexane's sorption heat, not the true isosteric value -- see
    this module's own top-level docstring) independent of the FVM fix.
    """
    A_bed = math.pi / 4.0 * c.diameter_m**2
    dz = c.bed_height_m / nz
    q_Iv_profile = q_Iv_w_m3 if isinstance(q_Iv_w_m3, tuple) else tuple(q_Iv_w_m3 for _ in range(nz))

    X2_in = thermo.x2_equilibrium(
        T_L_sup,
        c.particle.X3,
        c.particle.gab,
        c.particle.oil,
        c.particle.alpha_pg,
        c.particle.alpha_ps,
        c.particle.rho_ps,
    )
    X2_out = result.solid_out_X2
    X1_out = result.solid_out_X1

    # Net water moved from vapor TO solid, combining BOTH the supersaturated
    # (condensation) and subsaturated (isotherm sorption/desorption)
    # mechanisms -- read from the SOLID's own X1 change, independent of
    # DCZ's own internal vapor-side wV2 bookkeeping (the thing being
    # checked), not `result.total_condensed_kg_s` alone (which only covers
    # the condensation branch -- see `dt_solver.py`'s own matching fix this
    # session, DECISIONS.md).
    total_water_to_solid_kg_s = m_dry_kg_s * (X1_out - X1_in)

    water_in_bottom_kg_s = (1.0 - vapor_inf.wV2) * m_vapor_kg_s
    water_out_top_kg_s = (1.0 - result.vapor_out.wV2) * (m_vapor_kg_s - total_water_to_solid_kg_s)
    water_residual = (water_in_bottom_kg_s - water_out_top_kg_s) - total_water_to_solid_kg_s

    hexane_from_solid_kg_s = m_dry_kg_s * (X2_in - X2_out)
    hexane_in_bottom_kg_s = vapor_inf.wV2 * m_vapor_kg_s
    hexane_out_top_kg_s = result.vapor_out.wV2 * (m_vapor_kg_s - total_water_to_solid_kg_s)
    hexane_residual = (hexane_out_top_kg_s - hexane_in_bottom_kg_s) - hexane_from_solid_kg_s

    total_duty_w = sum(q * A_bed * dz for q in q_Iv_profile)
    geometry = pt.build_shell_geometry(c.particle.r_P, c.particle.Np)
    T_solid_out = dcz.bulk_temperature(result.cells[-1], geometry)
    # `ParticleConstants` carries no `cp_hexane_liquid`/`cp_oil` (unlike
    # PHZConstants), so a full 4-component `thermo.mixture_cp_per_kg_dry_
    # solid` isn't available here -- matches the SAME solid+moisture-only
    # approximation `zones/particle.py`'s own `march_particle_energy` already
    # uses for its moisture-dependent `Cv` (added this session's earlier
    # Part 4 work): hexane's own liquid-phase sensible-heat contribution is
    # omitted there too, not a new gap introduced by this check.
    cp_mix_avg = c.particle.cp_ps + 0.5 * (X1_in + X1_out) * c.particle.cp_water_liquid
    solid_sensible_delta_w = m_dry_kg_s * cp_mix_avg * (T_solid_out - T_L_sup)
    vapor_sensible_delta_w = m_vapor_kg_s * c.cp_V * (result.vapor_out.T - vapor_inf.T)
    latent_heat_water_w = total_water_to_solid_kg_s * c.dH_vap_water
    latent_heat_hexane_w = hexane_from_solid_kg_s * c.particle.dH_vap_hexane

    energy_residual = (total_duty_w + latent_heat_water_w - latent_heat_hexane_w) - (
        solid_sensible_delta_w + vapor_sensible_delta_w
    )

    return MassEnergyResidual(
        hexane_kg_s=hexane_residual, water_kg_s=water_residual, energy_w=energy_residual
    )


# ---------------------------------------------------------------------------
# Dryer/Cooler (DC)
# ---------------------------------------------------------------------------


def dc_stage_balance(
    T_in: float,
    X1_in: float,
    X2_in: float,
    air_T: float,
    air_flow_kg_s: float,
    air_humidity_in: float,
    m_dry_kg_s: float,
    result: tuple[float, float, float, float, float, float],
    c: dc_mod.DCConstants,
    ignore_hexane_latent_heat: bool = True,
) -> MassEnergyResidual:
    """Full two-sided water-mass + total-energy conservation check for the
    rewritten `core/dc.py` (first-principles falling-rate contactor with a
    CLOSED balance -- see that module's own docstring). Enabled by
    `air_contact_equilibrium`'s extended return signature (`T_eq, X1_eq,
    X2_eq, air_T_out, air_humidity_out`, `result` here).

    WATER: the air gains exactly what the solid loses --
    `(Y_out - Y_in)*m_air == m_dry*(X1_in - X1_eq)` -- exact by construction
    (`air_contact_equilibrium` sets `Y_out = Y_in + m_evap/m_air`), so this
    residual is machine-precision zero unless that relation is broken.

    ENERGY: adiabatic two-sided total-enthalpy balance `H_in == H_out`,
    reconstructed INDEPENDENTLY from the reported boundary states via the
    SAME `dc.solid_stream_enthalpy_w`/`dc.air_stream_enthalpy_w` primitives
    `air_contact_equilibrium` solved `air_T_out` against (a well-isolated
    constitutive property, appropriate to reuse per this module's own design
    principle). `air_T_out` is defined to close this, so the residual is
    machine-precision zero -- a genuine check that the reported outlet states
    are mutually energy-consistent, and that the model no longer needs the
    old evaporative-cooling special case (which left the air side unbalanced
    in the drying regime).

    `ignore_hexane_latent_heat` defaults `True` and MUST stay so: `DCConstants`
    still carries no `dH_vap_hexane` (a pre-existing, documented
    simplification -- hexane air-stripping in DC costs no energy in this
    model; `dc.py`'s own `air_T_out` solve likewise ignores it, so both sides
    of this check omit it consistently). The flag keeps that gap visible in
    the signature rather than hidden in a loose tolerance.
    """
    _T_eq, X1_eq, _X2_eq, air_T_out, air_humidity_out, _air_hex = result
    m_dry_safe = max(m_dry_kg_s, 1.0e-9)
    # X1_eq = X1_in - m_evap_kg_s/m_dry_safe (air_contact_equilibrium's own
    # relation) -- solved back for m_evap_kg_s, NOT simply X1_in-X1_eq (that
    # forgets the m_dry_safe scaling entirely).
    m_evap_kg_s = (X1_in - X1_eq) * m_dry_safe  # positive = evaporating

    water_residual = (air_humidity_out - air_humidity_in) * air_flow_kg_s - m_evap_kg_s

    h_in = dc_mod.solid_stream_enthalpy_w(m_dry_safe, T_in, X1_in, c) + dc_mod.air_stream_enthalpy_w(
        air_flow_kg_s, air_T, air_humidity_in, c
    )
    h_out = dc_mod.solid_stream_enthalpy_w(
        m_dry_safe, _T_eq, X1_eq, c
    ) + dc_mod.air_stream_enthalpy_w(air_flow_kg_s, air_T_out, air_humidity_out, c)
    energy_residual = h_in - h_out
    if not ignore_hexane_latent_heat:
        raise NotImplementedError(
            "core/dc.py's DCConstants carries no dH_vap_hexane -- hexane's own "
            "evaporation energy cost isn't modeled yet, so this check can't verify it"
        )

    return MassEnergyResidual(water_kg_s=water_residual, energy_w=energy_residual)


# ---------------------------------------------------------------------------
# Whole-DT handoff arithmetic (core/dt_solver.py)
# ---------------------------------------------------------------------------


def dt_handoff_consistency(
    dcz_solid_out_X1: float,
    dcz_solid_out_X2: float,
    tray_summary_last_X1: float,
    tray_summary_last_X2: float,
) -> MassEnergyResidual:
    """Checks the final `TraySummary` (`dt_solver.py`'s own public output
    contract) correctly reports DCZ's own exit state -- the last real tray
    is DCZ-spanned by construction (`solve_dt`'s own geometry), so these
    should match exactly. Deliberately NOT a second independent re
    -derivation of each zone's own physics (`phz_zone_balance`/
    `ftrz_zone_balance`/`dcz_zone_balance` already do that); this only
    checks the FINAL assembly step doesn't introduce its own mistake.
    """
    x1_residual = tray_summary_last_X1 - dcz_solid_out_X1
    x2_residual = tray_summary_last_X2 - dcz_solid_out_X2
    return MassEnergyResidual(hexane_kg_s=x2_residual, water_kg_s=x1_residual)
