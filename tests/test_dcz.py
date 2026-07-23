"""Unit tests for `core/zones/dcz.py` (Diffusion-Controlled Zone, dual-scale):
the Primary Internal Loop's residence-time cascade, the Rhodes-type bed
integration, and the zone's overall shape vs. Coletto (2022) §3.4/Fig. 9
(hexane content plunges through the zone; particle radial profile is the
"typical mass transfer profile" -- high at the center, low at the surface).
Shape/order-of-magnitude checks, not exact literature values -- only the
plot is available to us, same reasoning as `test_phz.py`/`test_ftrz.py`.

Note on temperature (UPDATED, M2 Phase 4): eq. A.34/A.37's enthalpy
-transport source terms, dropped in M2 Phase 3, are now RESTORED (see
`dcz.py`'s own module docstring for the full story -- the drop was found,
via `core/dt_solver.py`'s integration work, to cause unbounded cooling
rather than the bounded "runs a bit cooler" effect originally believed).
Restoring them changes this zone's own quantitative behavior even in
isolation: with `q_Iv=0.0` and closely-matched boundary temperatures (this
module's own illustrative inputs), particle temperature no longer drifts
artificially cold, so the GAB/oil isotherms' own temperature dependence
drives a SMALLER hexane reduction than the old (energy-non-conservative)
behavior produced -- a genuine physics change, not a regression. Tests still
check bounded, physically-directional shape, not tight literature values.
"""

import pytest

from dtdc_simulator.core import thermo
from dtdc_simulator.core.zones import dcz
from dtdc_simulator.core.zones import particle as pt

GAB = thermo.GabParams(Xm=5.183e-3, C0=3.117e-3, dHC_R=2262.0, K0=9.172e-2, dHK_R=729.6)
OIL = thermo.OilIsotherm(A0=0.9635, B=2.7036)
PARTICLE_CONSTANTS = pt.ParticleConstants(
    D_eff=4.0e-10,
    r_P=1.0e-3,
    Np=12,
    alpha_ps=0.5,
    alpha_pg=0.5,
    rho_ps=1513.0,
    rho_pg=0.6,
    cp_ps=2317.0,
    cp_pg=1650.0,
    k_ps=0.29,
    k_pg=0.02371,
    X3=0.0139,
    gab=GAB,
    oil=OIL,
    dH_vap_hexane=3.34e5,
    sorption_C0=3.0e5,
    sorption_C1=-0.5,
    cp_water_liquid=4186.0,
)
ANTOINE_WATER = thermo.AntoineParams(A=5.11564, B=1687.54, C=-42.98)
CONSTANTS = dcz.DCZConstants(
    diameter_m=4.0,
    bed_height_m=1.8,  # ~3 trays, matching the paper's own DCZ span
    hM=0.05,
    hQ=300.0,
    aV=3000.0,
    D_ax=1.022e-3,
    k_mixL=0.24,
    rho_V=0.6,
    cp_V=1926.0,
    alpha_V=0.4,
    alpha_L=0.6,
    particle=PARTICLE_CONSTANTS,
    dH_vap_water=2.26e6,
    antoine_water=ANTOINE_WATER,
    luikov=thermo.LuikovParams(A1=0.880, A2=12.184),
    # Derated 20x from Touffet's own 60 C peak measurement (6.2e-10) -- see
    # properties/soybean.yaml's own `water_diffusivity` comment for why: the
    # un-derated value produced a `water_latent_w_m3` source comparable to or
    # larger than typical indirect-steam duty, dominating the DCZ energy
    # balance and inverting the basic more-duty-=hotter relationship.
    water_diffusivity=3.1e-11,
    vapor_enthalpy_ref=thermo.VaporEnthalpyRef(
        dH_vap_water=2.26e6,
        cp_water_vapor=1900.0,
        T_boil_water=373.15,
        dH_vap_hexane=3.34e5,
        cp_hexane_vapor=1650.0,
        T_boil_hexane=341.95,
    ),
)
M_DRY_KG_S = 11.89
M_VAPOR_KG_S = 5.0
# Raised from the M2 Phase 3 illustrative 371-372 K (found this session,
# moisture-balance work): both sat just below pure water's own dew point at
# 1 atm (~373.15 K), so the new condensation mechanism triggered at nearly
# every cell in what these "no external heat" shape tests intend as a
# closely-matched-boundary, condensation-FREE illustrative case -- moved
# comfortably above the dew point instead (dedicated condensation tests
# below use their own, deliberately-supersaturated inputs).
T_L_SUP = 390.0
VAPOR_INF = dcz.VaporState(wV2=0.0001, T=389.0)
X1_IN = 0.15


def _solve(
    nz: int = 10, outer_max_iter: int = 150, vapor_inf: dcz.VaporState = VAPOR_INF
) -> dcz.DCZZoneResult:
    return dcz.solve_dcz_zone(
        nz=nz,
        m_dry_kg_s=M_DRY_KG_S,
        m_vapor_kg_s=M_VAPOR_KG_S,
        T_L_sup=T_L_SUP,
        vapor_inf=vapor_inf,
        q_Iv_w_m3=0.0,
        c=CONSTANTS,
        X1_in=X1_IN,
        outer_max_iter=outer_max_iter,
        outer_tol=1.0e-4,
        outer_relaxation=0.5,
    )


# ------------------------------------------------------------------ zone-level shape


def test_hexane_content_decreases_from_top_to_bottom() -> None:
    # Coletto (2022) §3.4/Fig. 9(a): solid enters DCZ at ~4060 ppm and exits
    # at ~100 ppm -- checked here as a monotonic reduction (order of
    # magnitude), not the exact ppm figures (illustrative hM/hQ/aV inputs,
    # same caveat as FTRZ's own q_Iv/hQ). Threshold lowered TWICE now:
    # first (M2 Phase 4) after the energy-balance fix stopped an artificial
    # cold drift; second (this session, DECISIONS.md's "DCZ particle hexane
    # mass-conservation gap" entry) after fixing `march_particle_mass`'s own
    # FVM -- the OLD scheme transferred hexane to the vapor ~18.6x FASTER
    # than it actually should have (a confirmed, now-fixed mass
    # -conservation bug, not a tuning choice), so this zone's own reduction
    # was artificially inflated. The NEW, mass-conservative scheme genuinely
    # transfers less over the SAME fixed iteration budget -- still a
    # substantial, real, monotonic reduction, just physically correct now.
    result = _solve()
    x2_values = [cell.X2_bulk for cell in result.cells]
    assert x2_values == sorted(x2_values, reverse=True)
    reduction = (x2_values[0] - x2_values[-1]) / x2_values[0]
    assert reduction > 0.10


def test_particle_radial_profile_is_the_typical_mass_transfer_shape() -> None:
    # Fig. 9(b): hexane content in the particle's gas phase decreases from
    # center to surface as the solid descends -- the classic diffusion
    # -controlled radial profile.
    result = _solve()
    exit_profile = result.cells[-1].particle.wpg2
    assert list(exit_profile) == sorted(exit_profile, reverse=True)
    assert exit_profile[0] > exit_profile[-1]


def test_particle_state_stays_within_physical_bounds() -> None:
    result = _solve()
    for cell in result.cells:
        assert all(0.0 <= w <= 1.0 for w in cell.particle.wpg2)
        assert all(t > 0.0 for t in cell.particle.Tp)


def test_temperature_does_not_run_away() -> None:
    # See module docstring: the restored enthalpy-transport terms (M2 Phase
    # 4) close the energy leak that used to cause unbounded cooling -- this
    # now checks genuine boundedness, not a lucky iteration-count cutoff.
    result = _solve()
    for cell in result.cells:
        for t in cell.particle.Tp:
            assert 300.0 < t < 400.0
        assert 300.0 < cell.vapor_top.T < 400.0


def test_outer_loop_respects_iteration_cap() -> None:
    result = _solve(outer_max_iter=20)
    assert result.iterations <= 20


# ------------------------------------------------------------------ helpers


def test_axial_laplacian_zero_for_uniform_profile() -> None:
    profile = tuple(5.0 for _ in range(10))
    laplacian = dcz.axial_laplacian(profile, dz=0.1)
    assert all(v == pytest.approx(0.0) for v in laplacian)


def test_axial_laplacian_matches_hand_computation_at_interior_point() -> None:
    profile = (1.0, 2.0, 4.0, 7.0, 11.0)
    dz = 0.5
    laplacian = dcz.axial_laplacian(profile, dz)
    # Interior point j=2: (right - 2*mid + left) / dz^2 = (7 - 2*4 + 2) / 0.25
    assert laplacian[2] == pytest.approx((7.0 - 2.0 * 4.0 + 2.0) / (dz * dz))


def test_bulk_temperature_matches_volumetric_mean() -> None:
    result = _solve(nz=3, outer_max_iter=20)
    geometry = pt.build_shell_geometry(PARTICLE_CONSTANTS.r_P, PARTICLE_CONSTANTS.Np)
    cell = result.cells[0]
    expected = pt.volumetric_mean(cell.particle.Tp, geometry.volumes)
    assert dcz.bulk_temperature(cell, geometry) == pytest.approx(expected)


def test_vapor_out_and_solid_out_x2_properties() -> None:
    result = _solve(nz=3, outer_max_iter=20)
    assert result.vapor_out == result.cells[0].vapor_top
    assert result.solid_out_X2 == result.cells[-1].X2_bulk


def test_explicit_component_flows_close_water_and_hexane_boundaries() -> None:
    result = _solve(outer_max_iter=500)
    water_in = (1.0 - VAPOR_INF.wV2) * M_VAPOR_KG_S
    hexane_in = VAPOR_INF.wV2 * M_VAPOR_KG_S
    water_to_solid = M_DRY_KG_S * (result.solid_out_X1 - X1_IN)
    x2_in = thermo.x2_equilibrium(
        T_L_SUP,
        PARTICLE_CONSTANTS.X3,
        PARTICLE_CONSTANTS.gab,
        PARTICLE_CONSTANTS.oil,
        PARTICLE_CONSTANTS.alpha_pg,
        PARTICLE_CONSTANTS.alpha_ps,
        PARTICLE_CONSTANTS.rho_ps,
    )
    hexane_from_solid = M_DRY_KG_S * (x2_in - result.solid_out_X2)

    assert result.iterations < 500
    assert water_in - result.vapor_water_out_kg_s == pytest.approx(water_to_solid, abs=1.0e-9)
    assert result.vapor_hexane_out_kg_s - hexane_in == pytest.approx(
        hexane_from_solid, abs=1.0e-3
    )
    for cell in result.cells:
        assert cell.vapor_water_kg_s >= 0.0
        assert cell.vapor_hexane_kg_s >= 0.0
        assert cell.vapor_top.wV2 == pytest.approx(
            cell.vapor_hexane_kg_s / cell.vapor_flow_kg_s
        )


# ------------------------------------------------------------------ moisture (H2O) balance


def test_subsaturated_moisture_relaxes_toward_isotherm_and_couples_to_temperature() -> None:
    # RE-BASELINED (was xfail through the DCZ Coletto-faithful rework). The
    # DCZ is the sparge RE-WETTING zone: VAPOR_INF here is ~pure steam at
    # 389 K (a_w high throughout), which puts the meal's own Luikov/GAB water
    # isotherm target ABOVE any physical inlet moisture -- a sweep this session
    # confirmed the solid ADSORBS across the whole 0.02-0.60 X1 range (there is
    # no desorbing inlet at these conditions). So the old premise ("a moist
    # inlet desorbs DOWN toward the target") is gone: it was an artefact of the
    # pre-rework, energy-non-conservative cold drift. This matches the
    # calibrated story in DECISIONS.md -- "more sparge steam correctly RAISES
    # moisture; the meal reaches ~19 %wb via the isotherm."
    #
    # What this still guards -- the subsaturated-regime isotherm relaxation and
    # its temperature coupling:
    #   (1) DIRECTION -- a solid entering below the isotherm target relaxes UP
    #       (adsorbs), for both a bone-dry (X1=0.02) and a moist (X1=0.15)
    #       inlet.
    #   (2) ORDERING -- the moister inlet stays moister at the outlet: one
    #       residence pass relaxes TOWARD, but does not collapse ONTO, a single
    #       equilibrium. The isotherm's own latent heat raises T, which lowers
    #       a_w and the local target (a genuine feedback, DECISIONS.md's "DCZ
    #       moisture latent heat"), so the two starts are not driven together.
    #   (3) COUPLING stays BOUNDED -- that adsorption-heat -> T -> a_w loop
    #       converges well inside the iteration cap (no runaway/oscillation)
    #       and leaves every cell temperature physical.
    # A little SURFACE condensation is allowed even though the BULK vapor is
    # subsaturated (389 K >> ~373 K dew point): evaporative-pinning evaluates
    # a_w at the cooler wet-surface saturation temperature, so a fraction of a
    # percent of the vapour's water can pin/condense at the surface -- physical,
    # and distinct from the bulk-supersaturation condensation the dedicated
    # tests below exercise.
    water_in_kg_s = (1.0 - VAPOR_INF.wV2) * M_VAPOR_KG_S

    dry_result = dcz.solve_dcz_zone(
        nz=10,
        m_dry_kg_s=M_DRY_KG_S,
        m_vapor_kg_s=M_VAPOR_KG_S,
        T_L_sup=T_L_SUP,
        vapor_inf=VAPOR_INF,
        q_Iv_w_m3=0.0,
        c=CONSTANTS,
        X1_in=0.02,  # bone-dry inlet, far below the isotherm target
        outer_max_iter=3000,
        outer_tol=1.0e-4,
        outer_relaxation=0.5,
    )
    moist_result = dcz.solve_dcz_zone(
        nz=10,
        m_dry_kg_s=M_DRY_KG_S,
        m_vapor_kg_s=M_VAPOR_KG_S,
        T_L_sup=T_L_SUP,
        vapor_inf=VAPOR_INF,
        q_Iv_w_m3=0.0,
        c=CONSTANTS,
        X1_in=0.15,  # moist inlet, still below the (high) isotherm target
        outer_max_iter=3000,
        outer_tol=1.0e-4,
        outer_relaxation=0.5,
    )

    # (3) the coupled loop converged, comfortably inside the cap
    assert dry_result.iterations < 3000
    assert moist_result.iterations < 3000

    # (1) both relax UP toward the isotherm target (adsorb / re-wet)
    assert dry_result.solid_out_X1 > 0.02
    assert moist_result.solid_out_X1 > 0.15

    # (2) ordering preserved: the moister inlet stays the moister outlet
    assert moist_result.solid_out_X1 > dry_result.solid_out_X1

    # (3) temperature coupling stays physical in every cell (no runaway)
    for res in (dry_result, moist_result):
        for cell in res.cells:
            assert 300.0 < cell.vapor_top.T < 400.0
            assert all(300.0 < tp < 400.0 for tp in cell.particle.Tp)

    # surface condensation, if any, is a small fraction of the vapour's water
    # (bulk stays subsaturated -- this is NOT the supersaturation regime)
    for res in (dry_result, moist_result):
        assert res.total_condensed_kg_s < 0.05 * water_in_kg_s


def test_condensation_when_vapor_supersaturated() -> None:
    # Vapor entering ALREADY below its own dew point (mirrors a real SPARGE
    # tray: injected steam mixed with cooler background vapor can land
    # sub-saturated at the blend temperature, see dcz.py's own module
    # docstring) -- the inflow-boundary flash check (step 2, part 'a') must
    # trigger immediately, independent of the isotherm's own kappa_w
    # magnitude.
    #
    # NOTE on `hM`/`weak_hM`: an earlier version of this test isolated the
    # condensation mechanism from the isotherm's latent-heat buffering via a
    # deliberately weak `hM`. That hack is gone: `kappa_w` (the isotherm's
    # own relaxation rate) no longer derives from `hM` at all -- it's the
    # Glueckauf LDF `15*D/r_P**2` (see dcz.py's own kappa_w derivation
    # comment). Placing vapor_inf below its own dew point instead triggers
    # the boundary flash check directly, before any isotherm buffering can
    # act, so it needs no such isolation.
    antoine_water = ANTOINE_WATER
    Y_V2 = 0.0001 / (1.0 - 0.0001)
    T_dew = thermo.dew_point_temperature(Y_V2, antoine_water)
    hot_vapor_inf = dcz.VaporState(wV2=0.0001, T=T_dew - 5.0)
    result = dcz.solve_dcz_zone(
        nz=10,
        m_dry_kg_s=M_DRY_KG_S,
        m_vapor_kg_s=M_VAPOR_KG_S,
        T_L_sup=340.0,
        vapor_inf=hot_vapor_inf,
        q_Iv_w_m3=0.0,
        c=CONSTANTS,
        X1_in=0.10,
        outer_max_iter=2000,
        outer_tol=1.0e-4,
        outer_relaxation=0.5,
    )
    assert result.iterations < 2000
    assert result.total_condensed_kg_s > 0.0
    # Never condenses more water than actually flows in.
    m_water_bottom = (1.0 - hot_vapor_inf.wV2) * M_VAPOR_KG_S
    assert result.total_condensed_kg_s <= m_water_bottom + 1.0e-9
    for cell in result.cells:
        assert 0.0 <= cell.X1_bulk <= 1.0
    for cell in result.cells:
        assert cell.condensed_water_kg_s >= 0.0


def test_cap_limited_solve_reports_failure_and_can_resume_complete_state() -> None:
    incomplete = _solve(outer_max_iter=1)
    assert not incomplete.converged
    assert incomplete.warm_start is not None
    assert incomplete.residuals.maximum_scaled > 1.0

    resumed = dcz.solve_dcz_zone(
        nz=10,
        m_dry_kg_s=M_DRY_KG_S,
        m_vapor_kg_s=M_VAPOR_KG_S,
        T_L_sup=T_L_SUP,
        vapor_inf=VAPOR_INF,
        q_Iv_w_m3=0.0,
        c=CONSTANTS,
        X1_in=0.15,
        outer_max_iter=3000,
        outer_relaxation=0.5,
        warm_start=incomplete.warm_start,
    )
    assert resumed.converged
    assert resumed.residuals.maximum_scaled <= 1.0
