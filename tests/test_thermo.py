"""Unit tests for `core/thermo.py` — BuildSpec §14 M1 acceptance criteria:
isotherm monotonic and matches the Cardarelli & Crapiste (1996) shape; property
correlations reproduce paper values; X2,cr/X2,eq finite and correctly ordered.
"""

import math

import pytest

from dtdc_simulator.core import thermo as th

# Real soybean GAB parameters, Cardarelli & Crapiste (1996) Table 2 (see
# properties/soybean.yaml for the same values with provenance comments).
SOYBEAN_GAB = th.GabParams(Xm=5.183e-3, C0=3.117e-3, dHC_R=2262.0, K0=9.172e-2, dHK_R=729.6)
SOYBEAN_OIL = th.OilIsotherm(A0=0.8, B=1.0)  # [PLACE], no source available
ANTOINE_WATER = th.AntoineParams(A=5.11564, B=1687.54, C=-42.98)
ANTOINE_HEXANE = th.AntoineParams(A=4.00266, B=1171.53, C=-48.784)


# ------------------------------------------------------------------ GAB isotherm


def test_gab_monotonic_increasing_with_activity() -> None:
    T = 338.15  # 65 C, matches a Cardarelli & Crapiste experimental isotherm
    activities = [0.1, 0.3, 0.5, 0.7, 0.85]
    values = [th.gab_hexane_content(a, T, SOYBEAN_GAB) for a in activities]
    assert values == sorted(values)
    assert all(v > 0 for v in values)


def test_gab_decreases_with_temperature_at_fixed_activity() -> None:
    a_h = 0.5
    v_low_T = th.gab_hexane_content(a_h, 323.15, SOYBEAN_GAB)  # 50 C
    v_high_T = th.gab_hexane_content(a_h, 368.15, SOYBEAN_GAB)  # 95 C
    assert v_high_T < v_low_T


def test_gab_soybean_order_of_magnitude_matches_fig4() -> None:
    # Cardarelli & Crapiste (1996) Fig. 4: soybean at 65 C, a_h~0.6 -> H~0.4 g/100g dm.
    H = th.gab_hexane_content(0.6, 338.15, SOYBEAN_GAB)
    assert 0.001 < H < 0.01  # 0.1-1.0 g/100g dm, same order of magnitude as the paper's chart


def test_gab_invalid_above_saturation_raises() -> None:
    with pytest.raises(ValueError):
        th.gab_hexane_content(1.5, 338.15, SOYBEAN_GAB)


def test_oil_hexane_content_monotonic_and_bounded() -> None:
    values = [th.oil_hexane_content(a, SOYBEAN_OIL) for a in (0.1, 0.5, 0.9)]
    assert values == sorted(values)
    assert th.oil_hexane_content(0.0, SOYBEAN_OIL) == 0.0


# ------------------------------------------------------------------ heat of sorption


def test_heat_of_sorption_exceeds_latent_heat_at_low_moisture() -> None:
    # Cardarelli & Crapiste (1996): "at low coverage the net heat of sorption
    # rises well above the heat of vaporization."
    dH_lv2 = 3.34e5
    low_W2 = th.heat_of_sorption(0.001, dH_lv2, sorption_C0=3.0e5, sorption_C1=-0.5)
    high_W2 = th.heat_of_sorption(0.1, dH_lv2, sorption_C0=3.0e5, sorption_C1=-0.5)
    assert low_W2 > high_W2 > dH_lv2


# ------------------------------------------------------------------ critical/equilibrium hexane


def test_rho_hexane_liquid_reasonable_and_decreasing_with_temperature() -> None:
    rho_bp = th.rho_hexane_liquid(341.9)
    rho_hot = th.rho_hexane_liquid(400.0)
    assert 550.0 < rho_bp < 650.0  # matches properties/soybean.yaml's ~615 kg/m3 reference
    assert rho_hot < rho_bp  # liquid density falls approaching the critical point (507.6 K)


def test_x2_critical_and_equilibrium_finite_and_ordered() -> None:
    T = 341.9
    rho_hexL = th.rho_hexane_liquid(T)
    alpha_pg, alpha_ps, rho_ps = 0.5, 0.5, 1513.0

    x2_cr = th.x2_critical(alpha_pg, rho_hexL, alpha_ps, rho_ps)
    x2_eq = th.x2_equilibrium(
        T,
        X3=0.01,
        gab=SOYBEAN_GAB,
        oil=SOYBEAN_OIL,
        alpha_pg=alpha_pg,
        alpha_ps=alpha_ps,
        rho_ps=rho_ps,
    )

    assert math.isfinite(x2_cr)
    assert math.isfinite(x2_eq)
    assert x2_eq <= x2_cr  # §2.3.2: X2,eq is the vapor-only-saturation floor below X2,cr


# ------------------------------------------------------------------ thermophysical properties (B.1-B.12)


def test_rho_l_and_rho_lmix() -> None:
    rho_L = th.rho_l(alpha_ps=0.5, rho_ps=1513.0, X1=0.1, X2=0.2, X3=0.01)
    assert rho_L == pytest.approx(0.5 * 1513.0 * 1.31)

    rho_vip = th.rho_vip(yV1=0.8, yV2=0.2, T=373.15)
    rho_mix = th.rho_lmix(alpha_L=0.5, rho_L=rho_L, alpha_V=0.5, rho_vip_=rho_vip)
    assert rho_mix == pytest.approx(0.5 * rho_L + 0.5 * rho_vip)


def test_cp_l_and_cp_vip_weighted_sums() -> None:
    cp_L = th.cp_l(0.1, 0.2, 0.01, 0.69, (4186.0, 2260.0, 2000.0, 1800.0))
    expected = 0.1 * 4186.0 + 0.2 * 2260.0 + 0.01 * 2000.0 + 0.69 * 1800.0
    assert cp_L == pytest.approx(expected)

    cp_V = th.cp_vip(0.8, 0.2, (1900.0, 1650.0))
    assert cp_V == pytest.approx(0.8 * 1900.0 + 0.2 * 1650.0)


def test_faner_nusselt_and_heat_transfer_coefficient() -> None:
    Nu = th.nu_from_reynolds(Re=70.0, Pr=1.0)
    assert Nu == pytest.approx(0.6949 * 70.0**0.579)

    hQ = th.hq_from_nu(Nu, r_P=1.0e-3, k_V=0.03, alpha_V=0.6, alpha_L=0.4)
    assert hQ > 0.0


def test_chilton_colburn_mass_transfer_coefficient() -> None:
    Sc = th.schmidt_number(mu_V=1.3e-5, rho_V=0.6, D_HW=1.0e-5)
    assert Sc > 0.0
    hM = th.hm_from_hq(hQ=50.0, rho_V=0.6, cp_V=1900.0, Pr=1.0, Sc=Sc)
    assert hM > 0.0


# ------------------------------------------------------------------ VLLE dew-point curve


def test_dew_point_at_zero_hexane_is_waters_boiling_point() -> None:
    # Y_V2=0 (no hexane) -> water condenses at its own normal boiling point.
    T_dew = th.dew_point_temperature(0.0, ANTOINE_WATER)
    assert T_dew == pytest.approx(373.15, abs=0.5)


def test_dew_point_drops_as_hexane_dilutes_the_vapor() -> None:
    # Matches Coletto (2022) Fig. 8(b): the FTRZ dew temperature dips below
    # 100 C as hexane dilutes the vapor and suppresses water's partial pressure.
    T_dew_dilute = th.dew_point_temperature(2.0, ANTOINE_WATER)  # hexane-rich
    T_dew_pure = th.dew_point_temperature(0.0, ANTOINE_WATER)
    assert T_dew_dilute < T_dew_pure


def test_vapor_enthalpy_round_trips_through_its_inverse() -> None:
    ref = th.VaporEnthalpyRef(
        dH_vap_water=2.26e6,
        cp_water_vapor=1900.0,
        T_boil_water=373.15,
        dH_vap_hexane=3.34e5,
        cp_hexane_vapor=1650.0,
        T_boil_hexane=341.9,
    )
    Y_V2, T_V = 0.3, 380.0
    H = th.vapor_enthalpy_water_basis(Y_V2, T_V, ref)
    T_recovered = th.temperature_from_vapor_enthalpy(H, Y_V2, ref)
    assert T_recovered == pytest.approx(T_V)


def test_dew_point_enthalpy_matches_curve_at_its_own_dew_temperature() -> None:
    ref = th.VaporEnthalpyRef(
        dH_vap_water=2.26e6,
        cp_water_vapor=1900.0,
        T_boil_water=373.15,
        dH_vap_hexane=3.34e5,
        cp_hexane_vapor=1650.0,
        T_boil_hexane=341.9,
    )
    Y_V2 = 0.5
    T_dew = th.dew_point_temperature(Y_V2, ANTOINE_WATER)
    H_sat = th.dew_point_enthalpy_water_basis(Y_V2, ANTOINE_WATER, ref)
    assert H_sat == pytest.approx(th.vapor_enthalpy_water_basis(Y_V2, T_dew, ref))
