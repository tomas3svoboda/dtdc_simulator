"""Unit tests for `core/dc.py` — the DC (dryer/cooler) air-contacting stage
balance, BuildSpec §7.10, M3a. Shape/boundedness/direction checks (no
literature figures cited for this section — Coletto doesn't cover DC at all,
and BuildSpec explicitly leaves correlation details `DECIDE`), same
methodology as every other zone module in this project.
"""

from __future__ import annotations

import pytest

from dtdc_simulator.core import dc, thermo

ANTOINE_WATER = thermo.AntoineParams(A=5.11564, B=1687.54, C=-42.98)
CONSTANTS = dc.DCConstants(
    cp_solid=1800.0,
    cp_water_liquid=4186.0,
    dH_vap_water=2.26e6,
    antoine_water=ANTOINE_WATER,
    dc_hexane_strip_k=0.3,
    luikov=thermo.LuikovParams(A1=0.880, A2=12.184),
)
M_DRY_KG_S = 25.0


def test_saturation_humidity_ratio_increases_with_temperature() -> None:
    y_cool = dc.saturation_humidity_ratio(300.0, ANTOINE_WATER)
    y_warm = dc.saturation_humidity_ratio(350.0, ANTOINE_WATER)
    assert 0.0 < y_cool < y_warm


def test_zero_air_flow_leaves_solid_unchanged() -> None:
    T_eq, X1_eq, X2_eq, air_T_out, air_humidity_out = dc.air_contact_equilibrium(
        T_in=320.0,
        X1_in=0.15,
        X2_in=0.02,
        air_T=380.0,
        air_flow_kg_s=0.0,
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert T_eq == pytest.approx(320.0)
    assert X1_eq == pytest.approx(0.15)
    assert X2_eq == pytest.approx(0.02)
    # No air flow -- the air's own reported exit state is a no-op too.
    assert air_T_out == pytest.approx(380.0)
    assert air_humidity_out == pytest.approx(0.01)


def test_high_air_flow_never_overshoots_past_air_temperature() -> None:
    # Real bug, found and fixed this session while recalibrating heated_air_flow/
    # ambient_air_flow against a real SCADA reference: at air flow rates large enough
    # that the air stream's own heat-capacity rate (air_flow*cp_air) exceeds the
    # solid's own (m_dry*C_wet) -- a realistic regime once air flow was retuned up
    # from an under-scaled placeholder -- the OLD formula let T_eq cool PAST the
    # air's own (cooler) inlet temperature, a thermodynamic impossibility for a
    # passive contactor (confirmed directly: dropped to -12.6 C against a 25 C air
    # supply before the fix). `air_contact_equilibrium` now uses the minimum of the
    # two streams' own heat-capacity rates (the standard effectiveness-NTU
    # convention), which bounds T_eq to the achievable range by construction.
    T_eq, X1_eq, X2_eq, air_T_out, air_humidity_out = dc.air_contact_equilibrium(
        T_in=380.0,
        X1_in=0.09,
        X2_in=0.0001,
        air_T=298.0,
        air_flow_kg_s=200.0,  # >> M_DRY_KG_S=25 -- air's own capacity rate dominates
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert 298.0 <= T_eq <= 380.0  # never overshoots past the air's own inlet temperature
    assert 298.0 <= air_T_out <= 380.0  # the air side is bounded the same way


def test_hot_air_on_a_nearly_dry_solid_produces_net_warming() -> None:
    # UPDATED (isotherm follow-up session): with the OLD air-side-mass
    # -balance target, a "nearly dry" solid could only ever lose more
    # moisture (one-way drying assumption) -- warming came from availability
    # -limited evaporation shutting off. With the REAL isotherm target
    # (thermo.luikov_equilibrium_moisture), this air (350 K, humidity_in=
    # 0.01) is only mildly dry in a RELATIVE-humidity sense -- its own
    # equilibrium moisture (~2.2%) sits ABOVE this X1_in (0.05%, essentially
    # bone dry), so the solid genuinely ADSORBS moisture instead (confirmed
    # directly: X1_eq > X1_in here) -- a real, physically expected
    # consequence of a bidirectional isotherm (same as DCZ's own moisture
    # mechanism), not a bug. Net warming still holds, now from BOTH sensible
    # heating AND adsorption's own exothermic release (a stronger effect
    # than sensible heating alone, not a weaker one).
    T_eq, X1_eq, X2_eq, _, _ = dc.air_contact_equilibrium(
        T_in=320.0,
        X1_in=0.0005,
        X2_in=0.02,
        air_T=350.0,
        air_flow_kg_s=8.0,
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert T_eq > 320.0  # net warming
    assert X1_eq > 0.0005  # adsorption: solid is drier than this air's own isotherm equilibrium
    assert 0.0 <= X2_eq < 0.02  # hexane stripped, never negative


def test_moisture_rich_hot_air_stays_at_the_constant_rate_plateau() -> None:
    # The energy-limited regime itself, documented as expected (not a bug):
    # temperature holds near T_in (all sensible heat consumed evaporating
    # moisture) while moisture still drops -- the same "constant-rate drying
    # period" physically expected of an evaporatively-cooled wet surface.
    T_eq, X1_eq, _, _, _ = dc.air_contact_equilibrium(
        T_in=320.0,
        X1_in=0.15,
        X2_in=0.02,
        air_T=350.0,
        air_flow_kg_s=8.0,
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert T_eq == pytest.approx(320.0, abs=1.0e-6)
    assert 0.0 <= X1_eq < 0.15


def test_air_above_waters_boiling_point_stays_energy_bounded() -> None:
    # A real failure mode this fixes (see dc.py's own inline comment):
    # air_T=380K sits above water's 1-atm boiling point, where the
    # single-component saturation-humidity formula has no physical ceiling.
    # Before the energy cap, this drove T_eq to ~180K (unphysical -- below
    # BOTH T_in and air_T with no external refrigeration). The fix bounds
    # evaporation by the sensible heat actually available, landing at the
    # "constant-rate-period" plateau (T_eq == T_in, all sensible heat
    # consumed by latent) rather than overshooting past it.
    T_eq, X1_eq, X2_eq, _, _ = dc.air_contact_equilibrium(
        T_in=320.0,
        X1_in=0.15,
        X2_in=0.02,
        air_T=380.0,
        air_flow_kg_s=8.0,
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert T_eq >= 320.0 - 1.0e-6  # never dips below T_in
    assert T_eq < 380.0  # never exceeds the air's own temperature either
    assert 0.0 <= X1_eq < 0.15


def test_cool_air_cools_the_solid_toward_air_temperature() -> None:
    T_eq, X1_eq, X2_eq, _, _ = dc.air_contact_equilibrium(
        T_in=350.0,
        X1_in=0.12,
        X2_in=0.005,
        air_T=298.0,
        air_flow_kg_s=8.0,
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert 298.0 < T_eq < 350.0  # cools toward, doesn't overshoot, the air's own temperature side
    assert 0.0 <= X2_eq < 0.005


def test_moisture_removal_never_exceeds_available_moisture() -> None:
    # Tiny initial moisture, huge air flow -- must clamp, not go negative.
    T_eq, X1_eq, X2_eq, _, _ = dc.air_contact_equilibrium(
        T_in=320.0,
        X1_in=0.001,
        X2_in=0.02,
        air_T=400.0,
        air_flow_kg_s=500.0,
        air_humidity_in=0.005,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert X1_eq >= 0.0


def test_hexane_and_moisture_bounded_in_unit_interval() -> None:
    for air_flow in (0.0, 1.0, 8.0, 50.0):
        T_eq, X1_eq, X2_eq, _, _ = dc.air_contact_equilibrium(
            T_in=320.0,
            X1_in=0.15,
            X2_in=0.02,
            air_T=380.0,
            air_flow_kg_s=air_flow,
            air_humidity_in=0.01,
            m_dry_kg_s=M_DRY_KG_S,
            c=CONSTANTS,
        )
        assert 0.0 <= X1_eq <= 1.0
        assert 0.0 <= X2_eq <= 1.0
        assert T_eq == T_eq  # not NaN


def test_more_air_flow_strips_more_hexane() -> None:
    _, _, X2_low, _, _ = dc.air_contact_equilibrium(
        T_in=320.0,
        X1_in=0.15,
        X2_in=0.02,
        air_T=380.0,
        air_flow_kg_s=2.0,
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    _, _, X2_high, _, _ = dc.air_contact_equilibrium(
        T_in=320.0,
        X1_in=0.15,
        X2_in=0.02,
        air_T=380.0,
        air_flow_kg_s=16.0,
        air_humidity_in=0.01,
        m_dry_kg_s=M_DRY_KG_S,
        c=CONSTANTS,
    )
    assert X2_high < X2_low
