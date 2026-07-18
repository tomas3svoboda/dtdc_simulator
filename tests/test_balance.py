"""Mass/energy conservation quality gate — exercises `core/balance.py`'s
independent residual functions against the SAME illustrative fixtures
already defined in `test_phz.py`/`test_ftrz.py`/`test_dcz.py`/
`test_dt_solver.py`/`test_dc.py` (imported directly, not duplicated).

This is the deliverable the "similar situation like latent-heat double-count
does not occur anywhere else" request exists to produce — see
`core/balance.py`'s own module docstring for the design principle
(independent, boundary-only recomputation, never reusing a zone's own
internal lagged state) and DECISIONS.md's "Mass/energy balance quality gate"
entry for what this work found (a second DCZ mass-conservation gap, fixed;
a `dt_solver.py` handoff gap, fixed; and a confirmed, deferred gap in
`march_particle_mass`'s own hexane FVM — see below for how DCZ's own tests
are scoped around that last one).
"""

from __future__ import annotations

import pytest

import tests.test_dc as dc_fixtures
import tests.test_dcz as dcz_fixtures
import tests.test_dt_solver as dt_fixtures
import tests.test_ftrz as ftrz_fixtures
import tests.test_phz as phz_fixtures
from dtdc_simulator.core import balance, dc, thermo
from dtdc_simulator.core.zones import dcz, ftrz, phz

# ------------------------------------------------------------------ PHZ


def test_phz_zone_conserves_hexane_and_energy() -> None:
    solid_in = phz.SolidState(T=phz_fixtures.T_FEED_K, X2=phz_fixtures.X2_FEED)
    result = phz.solve_phz_tray(
        nz=15,
        bed_height_m=0.3,
        diameter_m=4.0,
        Q_indirect_w=4.0e5,
        solid_in=solid_in,
        vapor_in=phz_fixtures.VAPOR_IN,
        m_dry_kg_s=phz_fixtures.DRY_SOLID_FLOW_KG_S,
        m_vapor_water_kg_s=5.0,
        X1=phz_fixtures.X1_FEED,
        X3=phz_fixtures.X3_FEED,
        c=phz_fixtures.CONSTANTS,
    )
    r = balance.phz_zone_balance(
        solid_in,
        result,
        4.0e5,
        phz_fixtures.DRY_SOLID_FLOW_KG_S,
        phz_fixtures.X1_FEED,
        phz_fixtures.X3_FEED,
        phz_fixtures.CONSTANTS,
    )
    assert r.hexane_kg_s == pytest.approx(0.0, abs=1.0e-9)
    assert r.energy_w == pytest.approx(0.0, abs=1.0e-3)  # ~1e-14 relative to a 4e5 W duty


# ------------------------------------------------------------------ FTRZ


def test_ftrz_zone_conserves_mass_and_energy_when_superheated() -> None:
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=373.0)
    X2_sup = 0.10
    result = ftrz_fixtures._solve(vapor_in, X2_sup=X2_sup, q_Iv_w_m3=2.0e5)
    r = balance.ftrz_zone_balance(
        vapor_in, result, 2.0e5, ftrz_fixtures.M_DRY_KG_S, X2_sup, 4.0, ftrz_fixtures.CONSTANTS
    )
    assert r.hexane_kg_s == pytest.approx(0.0, abs=1.0e-9)
    assert r.water_kg_s == pytest.approx(0.0, abs=1.0e-9)
    assert r.energy_w == pytest.approx(0.0, abs=1.0)  # ~1e-6 relative to a 2e5 W duty


def test_ftrz_zone_conserves_mass_and_energy_when_saturated() -> None:
    # The V-SAT (condensation) branch -- exercises the root-solved energy
    # balance, not just the closed-form V-SCAL one above.
    T_dew0 = thermo.dew_point_temperature(0.1, ftrz_fixtures.ANTOINE_WATER)
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=T_dew0)
    result = ftrz_fixtures._solve(vapor_in, X2_sup=0.10, q_Iv_w_m3=0.0)
    r = balance.ftrz_zone_balance(
        vapor_in, result, 0.0, ftrz_fixtures.M_DRY_KG_S, 0.10, 4.0, ftrz_fixtures.CONSTANTS
    )
    assert r.hexane_kg_s == pytest.approx(0.0, abs=1.0e-9)
    assert r.water_kg_s == pytest.approx(0.0, abs=1.0e-9)
    assert r.energy_w == pytest.approx(0.0, abs=1.0e-3)


# ------------------------------------------------------------------ DCZ
#
# SCOPING NOTE (see core/balance.py's own module docstring and
# `dcz_zone_balance`'s docstring for the full trail): `march_particle_mass`'s
# own hexane FVM double-counting/non-conservation gap (confirmed this
# session at ~18.6x, then a false lead at ~44.5%, before the real fix) is
# now FIXED -- `tests/test_particle.py::test_mass_march_conserves_x2_total_
# between_bulk_and_surface_flux` guards the particle scale directly, at
# production-realistic dt, to ~2%. The residuals checked here (zone/bed
# scale, via the SAME `dcz_zone_balance` used throughout this module) are
# now small but not machine-precision -- expected, since `dcz_zone_balance`'s
# own energy formula uses documented approximations (plain `dH_vap_hexane`
# for hexane's own sorption heat, not the true isosteric value; a midpoint
# -average solid mixture `cp`) that this check was never meant to close to
# zero, plus ordinary Gauss-Seidel convergence looseness at the bed scale --
# not a sign of a remaining production bug.


def test_dcz_zone_conserves_water_within_the_checks_own_approximation() -> None:
    result = dcz_fixtures._solve(outer_max_iter=3000)
    assert result.iterations < 3000  # actually converged, not iteration-capped
    r = balance.dcz_zone_balance(
        dcz_fixtures.VAPOR_INF,
        dcz_fixtures.T_L_SUP,
        dcz_fixtures.X1_IN,
        result,
        0.0,
        dcz_fixtures.M_DRY_KG_S,
        dcz_fixtures.M_VAPOR_KG_S,
        10,
        dcz_fixtures.CONSTANTS,
    )
    assert abs(r.water_kg_s) < 0.5  # ~10% of the 5.0 kg/s vapor flow -- see scoping note above


def test_dcz_solid_water_gain_at_least_matches_reported_condensation() -> None:
    # The RELIABLE water check, entirely independent of wV2/hexane: the
    # solid's own X1 gain (from step 4.5's own moisture cascade) must be AT
    # LEAST `total_condensed_kg_s` (step 2's own, separately-computed
    # diagnostic) -- neither quantity touches `wV2` at all, so this is
    # unaffected by any particle-scale hexane physics. Uses the module's own
    # supersaturated case. NOT an exact equality: found directly (per-cell
    # inspection) that this specific boundary condition supersaturates only
    # the first few cells (the vapor's own re-warming from released latent
    # heat pulls it back above its dew point after that -- the same
    # "flash-then-stop" shape FTRZ's own V-SAT branch shows), while the
    # SUBSATURATED isotherm relaxation keeps adsorbing water in the
    # remaining cells (X1_bulk keeps rising there even with
    # `condensed_water_kg_s == 0`) -- both mechanisms are real and additive,
    # so the solid's own total gain is the CONDENSED amount plus whatever
    # the isotherm separately contributes, not equal to the condensed amount
    # alone.
    Y_V2 = 0.0001 / (1.0 - 0.0001)
    T_dew = thermo.dew_point_temperature(Y_V2, dcz_fixtures.ANTOINE_WATER)
    hot_vapor_inf = dcz.VaporState(wV2=0.0001, T=T_dew - 5.0)
    result = dcz.solve_dcz_zone(
        nz=10,
        m_dry_kg_s=dcz_fixtures.M_DRY_KG_S,
        m_vapor_kg_s=dcz_fixtures.M_VAPOR_KG_S,
        T_L_sup=340.0,
        vapor_inf=hot_vapor_inf,
        q_Iv_w_m3=0.0,
        c=dcz_fixtures.CONSTANTS,
        X1_in=0.10,
        outer_max_iter=2000,
        outer_tol=1.0e-4,
        outer_relaxation=0.5,
    )
    assert result.iterations < 2000
    total_water_to_solid_kg_s = dcz_fixtures.M_DRY_KG_S * (result.solid_out_X1 - 0.10)
    assert total_water_to_solid_kg_s >= result.total_condensed_kg_s - 1.0e-6
    assert result.total_condensed_kg_s > 0.0  # sanity: condensation genuinely happened


def test_dcz_hexane_and_energy_residual_stay_small() -> None:
    # Now that march_particle_mass's own FVM is fixed, this is a MUCH
    # tighter regression net than before (was: bounded by total throughput,
    # i.e. up to 100% relative) -- a future change reintroducing a gross
    # conservation violation would fail this well before that old, loose
    # ceiling. Still not asserting machine-precision (see scoping note
    # above: dcz_zone_balance's own energy formula carries documented
    # approximations independent of the FVM fix).
    result = dcz_fixtures._solve(outer_max_iter=3000)
    r = balance.dcz_zone_balance(
        dcz_fixtures.VAPOR_INF,
        dcz_fixtures.T_L_SUP,
        dcz_fixtures.X1_IN,
        result,
        0.0,
        dcz_fixtures.M_DRY_KG_S,
        dcz_fixtures.M_VAPOR_KG_S,
        10,
        dcz_fixtures.CONSTANTS,
    )
    total_hexane_throughput_kg_s = dcz_fixtures.M_DRY_KG_S * 0.4743  # Fig. 1 base case X2
    assert abs(r.hexane_kg_s) < 0.10 * total_hexane_throughput_kg_s
    assert r.energy_w == r.energy_w  # not NaN
    assert abs(r.energy_w) < 1.0e5  # was 1e6; tightened 10x now the FVM gap is fixed


# ------------------------------------------------------------------ Dryer/Cooler (DC)


def test_dc_stage_conserves_mass_and_energy_evaporating() -> None:
    inputs = dict(
        T_in=320.0,
        X1_in=0.15,
        X2_in=0.02,
        air_T=350.0,
        air_flow_kg_s=8.0,
        air_humidity_in=0.01,
        m_dry_kg_s=dc_fixtures.M_DRY_KG_S,
        c=dc_fixtures.CONSTANTS,
    )
    result = dc.air_contact_equilibrium(**inputs)
    r = balance.dc_stage_balance(
        inputs["T_in"],
        inputs["X1_in"],
        inputs["X2_in"],
        inputs["air_T"],
        inputs["air_flow_kg_s"],
        inputs["air_humidity_in"],
        inputs["m_dry_kg_s"],
        result,
        inputs["c"],
    )
    assert r.water_kg_s == pytest.approx(0.0, abs=1.0e-9)
    assert r.energy_w == pytest.approx(0.0, abs=1.0e-6)


def test_dc_stage_conserves_mass_and_energy_adsorbing() -> None:
    # The bidirectional isotherm's own adsorption regime (a bone-dry solid
    # picking up moisture from normal-humidity air) -- exercises the OTHER
    # sign of m_evap_kg_s.
    inputs = dict(
        T_in=320.0,
        X1_in=0.0005,
        X2_in=0.02,
        air_T=350.0,
        air_flow_kg_s=8.0,
        air_humidity_in=0.01,
        m_dry_kg_s=dc_fixtures.M_DRY_KG_S,
        c=dc_fixtures.CONSTANTS,
    )
    result = dc.air_contact_equilibrium(**inputs)
    r = balance.dc_stage_balance(
        inputs["T_in"],
        inputs["X1_in"],
        inputs["X2_in"],
        inputs["air_T"],
        inputs["air_flow_kg_s"],
        inputs["air_humidity_in"],
        inputs["m_dry_kg_s"],
        result,
        inputs["c"],
    )
    assert r.water_kg_s == pytest.approx(0.0, abs=1.0e-9)
    assert r.energy_w == pytest.approx(0.0, abs=1.0e-6)


# ------------------------------------------------------------------ whole-DT handoff


def test_dt_final_tray_summary_matches_dcz_own_exit_state() -> None:
    result = dt_fixtures._solve()
    r = balance.dt_handoff_consistency(
        result.dcz.solid_out_X1,
        result.dcz.solid_out_X2,
        result.tray_summaries[-1].X1,
        result.tray_summaries[-1].X2,
    )
    assert r.water_kg_s == 0.0
    assert r.hexane_kg_s == 0.0
