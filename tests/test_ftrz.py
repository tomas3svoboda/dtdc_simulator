"""Unit tests for `core/zones/ftrz.py` — validated against the *shape* of
Coletto (2022) Fig. 8 / §3.3 (exact curve values aren't available to us, only
the plot, same reasoning as `test_phz.py`): hexane content plunges sharply,
moisture rises, solid temperature approaches (never exceeds) the vapor inlet
temperature, and the zone is thin (paper reports <2 cm at its own base case).
"""

from dataclasses import replace

import pytest

from dtdc_simulator.core import thermo
from dtdc_simulator.core.zones import ftrz

GAB = thermo.GabParams(Xm=5.183e-3, C0=3.117e-3, dHC_R=2262.0, K0=9.172e-2, dHK_R=729.6)
OIL = thermo.OilIsotherm(A0=0.8, B=1.0)
ANTOINE_WATER = thermo.AntoineParams(A=5.11564, B=1687.54, C=-42.98)
VAPOR_REF = thermo.VaporEnthalpyRef(
    dH_vap_water=2.26e6,
    cp_water_vapor=1900.0,
    T_boil_water=373.15,
    dH_vap_hexane=3.34e5,
    cp_hexane_vapor=1650.0,
    T_boil_hexane=341.9,
)
CONSTANTS = ftrz.FTRZConstants(
    T_boil_hexane=341.9,
    T_boil_water=373.15,
    dH_vap_hexane=3.34e5,
    cp_water_liquid=4186.0,
    gab=GAB,
    oil=OIL,
    antoine_water=ANTOINE_WATER,
    vapor_enthalpy_ref=VAPOR_REF,
    alpha_pg=0.5,
    alpha_ps=0.5,
    rho_ps=1513.0,
    X3=0.0139,
    bed_porosity=0.4,
    water_diffusivity=3.1e-11,
    particle_radius=1.0e-3,
)
M_DRY_KG_S = 11.89  # Fig. 1 base case, dry-solid basis (see test_phz.py)


def _solve(
    vapor_in: ftrz.VaporState,
    X2_sup: float,
    q_Iv_w_m3: float,
    nz: int = 20,
    constants: ftrz.FTRZConstants = CONSTANTS,
    X1_sup: float = 0.0,
    T_solid_sup: float | None = None,
) -> ftrz.FTRZZoneResult:
    return ftrz.solve_ftrz_zone(
        nz=nz,
        X2_sup=X2_sup,
        m_dry_kg_s=M_DRY_KG_S,
        vapor_in=vapor_in,
        q_Iv_w_m3=q_Iv_w_m3,
        hQ=500.0,
        hM=0.01,
        aV_m2_per_m3=1000.0,
        diameter_m=4.0,
        c=constants,
        X1_sup=X1_sup,
        T_solid_sup=T_solid_sup,
    )


# ------------------------------------------------------------------ wet_core_fraction / T_L


def test_wet_core_fraction_bounds() -> None:
    assert ftrz.wet_core_fraction(X2=0.5, X2_cr=0.1, X2_eq=0.01) == 1.0  # still fully wet
    assert ftrz.wet_core_fraction(X2=0.01, X2_cr=0.1, X2_eq=0.01) == pytest.approx(
        0.0
    )  # at eq floor
    mid = ftrz.wet_core_fraction(X2=0.055, X2_cr=0.1, X2_eq=0.01)
    assert 0.0 < mid < 1.0


def test_solid_temperature_bounded_by_hexane_bp_and_vapor_temp() -> None:
    T_hex_bp, T_v = 341.9, 373.15
    T_l_wet = ftrz.solid_temperature(X2=0.5, X2_cr=0.1, X2_eq=0.01, T_boil_hexane=T_hex_bp, T_V=T_v)
    assert T_l_wet == pytest.approx(T_hex_bp)  # fully wet core -> pinned at hexane's bp
    T_l_dry = ftrz.solid_temperature(
        X2=0.005, X2_cr=0.1, X2_eq=0.01, T_boil_hexane=T_hex_bp, T_V=T_v
    )
    assert T_l_dry == pytest.approx(T_v)  # fully dry -> equals vapor temperature


def test_superheated_matrix_credit_is_mesh_independent_and_not_added_twice_to_vapor() -> None:
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=373.0)
    constants = replace(
        CONSTANTS,
        cp_solid=2317.0,
        cp_hexane_liquid=2260.0,
        cp_oil=2000.0,
    )
    coarse = _solve(
        vapor_in,
        X2_sup=0.03,
        q_Iv_w_m3=0.0,
        nz=10,
        constants=constants,
        T_solid_sup=400.0,
    )
    fine = _solve(
        vapor_in,
        X2_sup=0.03,
        q_Iv_w_m3=0.0,
        nz=40,
        constants=constants,
        T_solid_sup=400.0,
    )

    assert coarse.L_FTRZ_m == pytest.approx(1.0e-9)
    assert fine.L_FTRZ_m == pytest.approx(coarse.L_FTRZ_m)
    assert max(cell.vapor_out.T for cell in fine.cells) < 450.0
    assert all(cell.sensible_heat_to_solid_w == 0.0 for cell in fine.cells)


# ------------------------------------------------------------------ single-cell regimes


def test_cell_stays_superheated_when_far_from_dew_point() -> None:
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=373.0)
    vapor_out, condensed, is_sat = ftrz.solve_ftrz_cell(
        vapor_in, hexane_evap_kg_s=0.045, q_cell_w=0.0, c=CONSTANTS
    )
    assert is_sat is False
    assert condensed == 0.0
    assert vapor_out.m_water_kg_s == vapor_in.m_water_kg_s  # water conserved, no condensation


def test_cell_condenses_water_once_past_its_dew_point() -> None:
    T_dew0 = thermo.dew_point_temperature(0.1, ANTOINE_WATER)
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=T_dew0)
    vapor_out, condensed, is_sat = ftrz.solve_ftrz_cell(
        vapor_in, hexane_evap_kg_s=0.045, q_cell_w=0.0, c=CONSTANTS
    )
    assert is_sat is True
    assert condensed > 0.0
    assert vapor_out.m_water_kg_s == pytest.approx(vapor_in.m_water_kg_s - condensed)
    # Still (approximately) on its own dew curve.
    assert vapor_out.T == pytest.approx(
        thermo.dew_point_temperature(vapor_out.Y_V2, ANTOINE_WATER), abs=1e-6
    )


# ------------------------------------------------------------------ full zone


def test_zone_reduces_hexane_and_conserves_mass() -> None:
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=373.0)
    X2_sup = 0.10
    result = _solve(vapor_in, X2_sup=X2_sup, q_Iv_w_m3=2.0e5)

    X2_inf = thermo.x2_equilibrium(
        vapor_in.T, CONSTANTS.X3, GAB, OIL, CONSTANTS.alpha_pg, CONSTANTS.alpha_ps, CONSTANTS.rho_ps
    )
    # Hexane reduction should be dramatic across FTRZ (Coletto §3.3: "plunges
    # by about 99%") -- checked as a strong reduction, not an exact percentage.
    reduction = (X2_sup - result.solid_out.X2) / X2_sup
    assert reduction > 0.5

    total_evap_kg_s = M_DRY_KG_S * (X2_sup - X2_inf)
    solid_x2_values = [cell.solid.X2 for cell in result.cells]
    # Monotonically decreasing top-to-bottom (uniform-removal construction).
    assert solid_x2_values == sorted(solid_x2_values, reverse=True)
    assert solid_x2_values[0] == pytest.approx(
        X2_sup - total_evap_kg_s / len(result.cells) / M_DRY_KG_S
    )


def test_zone_length_is_thin_relative_to_a_full_tray() -> None:
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=373.0)
    result = _solve(vapor_in, X2_sup=0.10, q_Iv_w_m3=2.0e5)
    # Coletto (2022) §3.3: "a very thin FTRZ is predicted (less than 2 cm)".
    # Our own energy/geometry inputs are illustrative (q_Iv/hQ/aV aren't
    # derived from bed conditions at this phase -- see module docstring), so
    # this checks the same order of magnitude (a small fraction of a typical
    # 0.3-1.0 m tray height), not an exact centimeter figure.
    assert 0.0 < result.L_FTRZ_m < 0.5
    assert result.iterations < 50  # the outer L_FTRZ fixed-point loop converged


def test_zone_solid_temperature_approaches_but_never_exceeds_vapor_inlet() -> None:
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=373.0)
    result = _solve(vapor_in, X2_sup=0.10, q_Iv_w_m3=2.0e5)
    temps = [cell.solid.T for cell in result.cells]
    assert temps == sorted(temps)  # rises monotonically top-to-bottom
    assert all(t <= vapor_in.T + 1e-6 for t in temps)


def test_condensation_raises_solid_moisture_when_the_zone_saturates() -> None:
    # Anchor the vapor inlet exactly at its own dew point (see module
    # development notes) so the zone actually crosses into V-SAT and
    # condenses water, exercising that branch end-to-end.
    T_dew0 = thermo.dew_point_temperature(0.1, ANTOINE_WATER)
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=T_dew0)
    result = _solve(vapor_in, X2_sup=0.10, q_Iv_w_m3=0.0)

    assert any(cell.is_saturated for cell in result.cells)
    assert result.solid_out.X1 > 0.0  # solid gained moisture from condensation

    total_condensed_kg_s = sum(cell.condensed_water_kg_s for cell in result.cells)
    assert result.solid_out.X1 == pytest.approx(total_condensed_kg_s / M_DRY_KG_S)
    assert result.vapor_out.m_water_kg_s == pytest.approx(
        vapor_in.m_water_kg_s - total_condensed_kg_s
    )


# ------------------------------------------------------------------ finite-rate water transfer


def test_water_transfer_rate_combines_internal_and_external_resistances() -> None:
    k_internal = 15.0 * 3.1e-11 / 1.0e-3**2
    k_external = 0.01 * 1000.0
    rate = ftrz.water_transfer_rate_s(3.1e-11, 1.0e-3, 0.01, 1000.0)

    assert 0.0 < rate < min(k_internal, k_external)
    assert rate == pytest.approx(1.0 / (1.0 / k_internal + 1.0 / k_external))
    assert ftrz.water_transfer_rate_s(0.0, 1.0e-3, 0.01, 1000.0) == 0.0


def test_moisture_relaxation_is_rate_limited_and_bounded() -> None:
    X1_initial = 0.10
    X1_equilibrium = 0.25

    unchanged = ftrz.relax_moisture(X1_initial, X1_equilibrium, 30.0, 0.0)
    short = ftrz.relax_moisture(X1_initial, X1_equilibrium, 30.0, 0.01)
    long = ftrz.relax_moisture(X1_initial, X1_equilibrium, 300.0, 0.01)

    assert unchanged == X1_initial
    assert X1_initial < short < long < X1_equilibrium


def test_finite_rate_sorption_responds_to_water_diffusivity_and_conserves_mass() -> None:
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=373.0)
    luikov = thermo.LuikovParams(A1=0.880, A2=12.184)
    slow_constants = replace(CONSTANTS, luikov=luikov, water_diffusivity=3.1e-11)
    fast_constants = replace(CONSTANTS, luikov=luikov, water_diffusivity=3.1e-7)

    slow = _solve(vapor_in, X2_sup=0.10, q_Iv_w_m3=2.0e5, constants=slow_constants, X1_sup=0.10)
    fast = _solve(vapor_in, X2_sup=0.10, q_Iv_w_m3=2.0e5, constants=fast_constants, X1_sup=0.10)

    assert 0.0 < slow.solid_out.X1 < fast.solid_out.X1
    for result in (slow, fast):
        vapor_water_loss = vapor_in.m_water_kg_s - result.vapor_out.m_water_kg_s
        assert vapor_water_loss == pytest.approx(M_DRY_KG_S * result.solid_out.X1, abs=1.0e-10)
        assert vapor_water_loss == pytest.approx(
            sum(cell.condensed_water_kg_s for cell in result.cells), abs=1.0e-10
        )


def test_water_interface_dew_point_does_not_replace_bulk_a17_temperature() -> None:
    """A hexane-rich vapor can have a water dew point below hexane's boiling
    point. That value belongs to the water interface/sorption closure; it must
    not overwrite the bulk meal temperature supplied by Coletto A.17."""
    constants = replace(
        CONSTANTS,
        luikov=thermo.LuikovParams(A1=0.880, A2=12.184),
    )
    result = _solve(
        ftrz.VaporState(m_water_kg_s=1.0, m_hex_kg_s=10.0, T=380.0),
        X2_sup=0.26,
        q_Iv_w_m3=2.0e5,
        constants=constants,
        X1_sup=0.12,
        T_solid_sup=constants.T_boil_hexane,
    )

    assert any(cell.water_surface_T < constants.T_boil_hexane for cell in result.cells)
    assert all(cell.solid.T >= constants.T_boil_hexane for cell in result.cells)
    assert all(cell.solid.T >= cell.water_surface_T for cell in result.cells)


def test_zero_sorption_rate_preserves_bulk_condensation_without_double_counting() -> None:
    T_dew0 = thermo.dew_point_temperature(0.1, ANTOINE_WATER)
    vapor_in = ftrz.VaporState(m_water_kg_s=5.0, m_hex_kg_s=0.5, T=T_dew0)
    constants = replace(
        CONSTANTS,
        luikov=thermo.LuikovParams(A1=0.880, A2=12.184),
        water_diffusivity=0.0,
    )
    result = _solve(vapor_in, X2_sup=0.10, q_Iv_w_m3=0.0, constants=constants)

    total_bulk = sum(cell.bulk_condensed_water_kg_s for cell in result.cells)
    assert total_bulk > 0.0
    assert sum(cell.sorbed_water_kg_s for cell in result.cells) == 0.0
    assert result.solid_out.X1 == pytest.approx(total_bulk / M_DRY_KG_S)
