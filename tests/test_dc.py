"""Unit tests for `core/dc.py` — the DC (dryer/cooler) air-contacting stage,
BuildSpec §7.10. First-principles falling-rate contactor with a CLOSED
two-sided mass/energy balance (Luz 2010 / Silva 2012 — see `core/dc.py`'s own
module docstring). Coletto doesn't cover DC, so these are physical
shape/direction/conservation checks plus the two literature-cited soybean-meal
drying correlations (`thermo.luz_*`), not figure-for-figure reproductions.
"""

from __future__ import annotations

import pytest

from dtdc_simulator.core import balance, dc, thermo

ANTOINE_WATER = thermo.AntoineParams(A=5.11564, B=1687.54, C=-42.98)
LUZ = thermo.LuzDryingParams(
    k_a2=-0.33e-11, k_b2=4.60e-9, k_a1=7.0e-8, k_b1=1.42e-5, k_c=8.44e-3, xe_num=0.834, xe_coef=0.036
)
CONSTANTS = dc.DCConstants(
    cp_solid=1800.0,
    cp_water_liquid=4186.0,
    dH_vap_water=2.26e6,
    antoine_water=ANTOINE_WATER,
    dc_hexane_strip_k=1.0,
    luz=LUZ,
    cp_water_vapor=1900.0,
)
M_DRY_KG_S = 25.0
TAU_S = 135.0
# Ambient absolute humidity, 25 C / 50% RH — the DC's own default weather air.
Y_AMBIENT = dc.saturation_humidity_ratio(298.0, ANTOINE_WATER) * 0.50


def _equilibrium(**kw):
    """`air_contact_equilibrium` with the test defaults, overridable per-call."""
    args = dict(
        T_in=378.0,
        X1_in=0.10,
        X2_in=0.0138,
        air_T=380.0,
        air_flow_kg_s=60.0,
        air_humidity_in=Y_AMBIENT,
        m_dry_kg_s=M_DRY_KG_S,
        residence_s=TAU_S,
        c=CONSTANTS,
    )
    args.update(kw)
    return dc.air_contact_equilibrium(**args)


def _closure(**kw):
    """Run a stage and independently check its two-sided mass/energy balance."""
    args = dict(
        T_in=378.0,
        X1_in=0.10,
        X2_in=0.0138,
        air_T=380.0,
        air_flow_kg_s=60.0,
        air_humidity_in=Y_AMBIENT,
        m_dry_kg_s=M_DRY_KG_S,
        residence_s=TAU_S,
        c=CONSTANTS,
    )
    args.update(kw)
    result = dc.air_contact_equilibrium(**args)
    resid = balance.dc_stage_balance(
        args["T_in"],
        args["X1_in"],
        args["X2_in"],
        args["air_T"],
        args["air_flow_kg_s"],
        args["air_humidity_in"],
        args["m_dry_kg_s"],
        result,
        args["c"],
    )
    return result, resid


# ------------------------------------------------------------- psychrometrics


def test_saturation_humidity_ratio_increases_with_temperature() -> None:
    y_cool = dc.saturation_humidity_ratio(300.0, ANTOINE_WATER)
    y_warm = dc.saturation_humidity_ratio(350.0, ANTOINE_WATER)
    assert 0.0 < y_cool < y_warm


def test_air_relative_humidity_matches_saturation_at_the_saturation_ratio() -> None:
    y_sat = dc.saturation_humidity_ratio(320.0, ANTOINE_WATER)
    assert dc.air_relative_humidity(y_sat, 320.0, ANTOINE_WATER) == pytest.approx(1.0, rel=1e-6)


def test_air_relative_humidity_falls_as_air_is_heated() -> None:
    # Same absolute humidity, hotter air -> lower relative humidity (drier).
    ur_cool = dc.air_relative_humidity(0.01, 300.0, ANTOINE_WATER)
    ur_hot = dc.air_relative_humidity(0.01, 380.0, ANTOINE_WATER)
    assert ur_hot < ur_cool


# ------------------------------------------------------- Luz drying correlations


def test_luz_mass_transfer_coefficient_is_positive_and_diffusion_scaled() -> None:
    # Falling-rate coefficient, dominated by k_c (~8.44e-3/s) across the band.
    k = thermo.luz_mass_transfer_coefficient(380.0, 0.15, LUZ)
    assert 5.0e-3 < k < 2.0e-2


def test_luz_equilibrium_moisture_rises_with_humidity() -> None:
    xe_dry = thermo.luz_equilibrium_moisture(320.0, 0.05, LUZ)
    xe_humid = thermo.luz_equilibrium_moisture(320.0, 0.80, LUZ)
    assert 0.0 < xe_dry < xe_humid < LUZ.xe_num


def test_luz_equilibrium_moisture_falls_with_temperature() -> None:
    # Hotter solid holds less equilibrium moisture at the same air humidity.
    xe_warm = thermo.luz_equilibrium_moisture(310.0, 0.40, LUZ)
    xe_hot = thermo.luz_equilibrium_moisture(360.0, 0.40, LUZ)
    assert xe_hot < xe_warm


# --------------------------------------------------------- stage: no-air no-op


def test_zero_air_flow_leaves_solid_and_air_unchanged() -> None:
    T_eq, X1_eq, X2_eq, air_T_out, air_humidity_out = _equilibrium(air_flow_kg_s=0.0)
    assert T_eq == pytest.approx(378.0)
    assert X1_eq == pytest.approx(0.10)
    assert X2_eq == pytest.approx(0.0138)
    assert air_T_out == pytest.approx(380.0)
    assert air_humidity_out == pytest.approx(Y_AMBIENT)


# --------------------------------------------------- DRYER: dries, stays warm


def test_dryer_dries_the_meal_but_keeps_it_warm() -> None:
    # Realistic dryer: ~102 C, ~9.5% meal meeting ~107 C air at 60 kg/s. The
    # meal must LOSE moisture (falling-rate drying) yet stay WARM -- evaporative
    # cooling is real but modest here (the air supplies most of the latent load
    # via the coupled balance), so the meal settles well above the old model's
    # ~43 C wet-bulb crash. Protects against reintroducing that bug.
    T_eq, X1_eq, _X2_eq, air_T_out, air_humidity_out = _equilibrium(
        T_in=375.15, X1_in=0.095, air_T=380.0
    )
    assert X1_eq < 0.095  # dried
    assert 273.15 + 70.0 < T_eq < 380.0  # warm, below the hot air (some evaporative cooling)
    assert air_T_out < 380.0  # air cooled as it heated/dried the meal
    assert air_humidity_out > Y_AMBIENT  # air gained the evaporated moisture


def test_more_residence_time_dries_more() -> None:
    # Falling-rate kinetics: longer contact -> further down the drying curve.
    _, X1_short, _, _, _ = _equilibrium(residence_s=40.0)
    _, X1_long, _, _, _ = _equilibrium(residence_s=400.0)
    assert X1_long < X1_short < 0.10


# --------------------------------------------------- COOLER: cools toward air


def test_cool_air_cools_the_solid_toward_air_temperature() -> None:
    T_eq, _X1_eq, X2_eq, air_T_out, _ = _equilibrium(T_in=378.0, air_T=298.0, air_humidity_in=Y_AMBIENT)
    assert 298.0 < T_eq < 378.0  # cools toward, never past, the cold air
    assert 298.0 < air_T_out < 378.0  # air warms toward, never past, the hot meal
    assert 0.0 <= X2_eq < 0.0138


def test_more_cooling_air_cools_the_meal_further() -> None:
    T_low_air, *_ = _equilibrium(T_in=378.0, air_T=298.0, air_flow_kg_s=60.0)
    T_high_air, *_ = _equilibrium(T_in=378.0, air_T=298.0, air_flow_kg_s=400.0)
    assert T_high_air < T_low_air  # more air -> closer to the 25 C ambient


# ------------------------------------------------------ closed conservation


@pytest.mark.parametrize(
    "kw",
    [
        {},  # dryer base case
        {"T_in": 375.15, "X1_in": 0.095},  # realistic dryer inlet
        {"air_T": 298.0, "air_flow_kg_s": 400.0},  # cooler, high air flow
        {"air_T": 298.0, "X1_in": 0.02},  # cooler onto a dry meal (adsorption)
        {"air_flow_kg_s": 5.0},  # air-starved
        {"X1_in": 0.30, "air_flow_kg_s": 200.0},  # very wet meal, lots of air
    ],
)
def test_two_sided_mass_and_energy_balance_closes(kw) -> None:
    _result, resid = _closure(**kw)
    # Water: air gains exactly what the solid loses (machine precision).
    assert abs(resid.water_kg_s) < 1.0e-9
    # Energy: adiabatic total-enthalpy balance H_in == H_out (machine precision
    # relative to the MW-scale enthalpy flows through the stage).
    assert abs(resid.energy_w) < 1.0e-3


# ----------------------------------------------------- adsorption (re-wetting)


def test_dry_meal_in_humid_air_adsorbs_moisture_bounded() -> None:
    # A COOLER stage on meal dried below its ambient-humidity isotherm
    # equilibrium picks moisture back UP from the humid ambient air -- real,
    # bounded, energy-consistent (no runaway exothermic heating).
    T_eq, X1_eq, _, _, air_humidity_out = _equilibrium(
        T_in=319.15, X1_in=0.02, air_T=298.0, air_flow_kg_s=400.0
    )
    assert X1_eq > 0.02  # adsorbed
    assert air_humidity_out < Y_AMBIENT  # air gave up moisture to the solid
    assert T_eq < 319.15 + 30.0  # no absurd heat release


# ------------------------------------------------------------- boundedness


def test_hexane_and_moisture_bounded_in_unit_interval() -> None:
    for air_flow in (0.0, 1.0, 8.0, 60.0, 400.0):
        T_eq, X1_eq, X2_eq, _, _ = _equilibrium(air_flow_kg_s=air_flow)
        assert 0.0 <= X1_eq <= 1.0
        assert 0.0 <= X2_eq <= 1.0
        assert T_eq == T_eq  # not NaN


def test_moisture_removal_never_exceeds_available_moisture() -> None:
    _T_eq, X1_eq, _X2_eq, _, _ = _equilibrium(X1_in=0.001, air_T=400.0, air_flow_kg_s=500.0)
    assert X1_eq >= 0.0


def test_more_air_flow_strips_more_hexane() -> None:
    _, _, X2_low, _, _ = _equilibrium(air_flow_kg_s=2.0)
    _, _, X2_high, _, _ = _equilibrium(air_flow_kg_s=16.0)
    assert X2_high < X2_low
