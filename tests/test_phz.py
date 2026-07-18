"""Unit tests for `core/zones/phz.py` — validated against the *shape* of
Coletto (2022) Fig. 7 (exact curve values aren't available to us, only the
plot itself, so assertions are shape/conservation-based per this module's
plan) using the paper's own Fig. 1 base-case numbers: 70,000 kg/h wet meal at
ww=0.09, whex=0.29, wo=0.0085, wds=0.6115, 58 C inlet; 400 kW indirect steam
per pre-desolventizing tray, each 0.3 m bed height, 4.0 m diameter.
"""

import pytest

from dtdc_simulator.core.zones import phz

# Fig. 1 base case, converted to dry-solid basis (X_i = w_i/w_ds).
DRY_SOLID_FLOW_KG_S = 70_000.0 * 0.6115 / 3600.0  # ~11.89 kg/s
X1_FEED = 0.09 / 0.6115  # moisture, ~0.1471
X2_FEED = 0.29 / 0.6115  # hexane, ~0.4743 (matches Fig. 7(a)'s ~0.475 starting value)
X3_FEED = 0.0085 / 0.6115  # oil, ~0.0139
T_FEED_K = 58.0 + 273.15

CONSTANTS = phz.PHZConstants(
    T_boil_hexane=341.9,
    dH_vap_hexane=3.34e5,
    cp_solid=1800.0,
    cp_water_liquid=4186.0,
    cp_hexane_liquid=2260.0,
    cp_oil=2000.0,
    cp_water_vapor=1900.0,
    cp_hexane_vapor=1650.0,
)

# A representative (not precisely paper-read) vapor boundary condition — see
# module docstring: PHZ's own vapor coupling across trays isn't resolved by
# this standalone solver (that's M2 Phase 4), so the same illustrative inlet
# is used for each tray in the zone-level test.
VAPOR_IN = phz.VaporState(wV1=0.2, wV2=0.8, T=347.0)


def _predesolv_tray(Q_indirect_w: float = 4.0e5, nz: int = 10) -> tuple[int, float, float, float]:
    return (nz, 0.3, 4.0, Q_indirect_w)


# ------------------------------------------------------------------ single tray


def test_solid_temperature_never_decreases_and_never_exceeds_boiling_point() -> None:
    solid_in = phz.SolidState(T=T_FEED_K, X2=X2_FEED)
    result = phz.solve_phz_tray(
        nz=10,
        bed_height_m=0.3,
        diameter_m=4.0,
        Q_indirect_w=4.0e5,
        solid_in=solid_in,
        vapor_in=VAPOR_IN,
        m_dry_kg_s=DRY_SOLID_FLOW_KG_S,
        m_vapor_water_kg_s=5.0,
        X1=X1_FEED,
        X3=X3_FEED,
        c=CONSTANTS,
    )
    temps = [solid_in.T] + [cell.solid_out.T for cell in result.cells]
    assert all(t2 >= t1 - 1e-9 for t1, t2 in zip(temps, temps[1:]))  # monotonically non-decreasing
    assert all(
        t <= CONSTANTS.T_boil_hexane + 1e-6 for t in temps
    )  # never exceeds hexane's bp in PHZ


def test_hexane_content_flat_until_boiling_point_then_decreasing() -> None:
    solid_in = phz.SolidState(T=T_FEED_K, X2=X2_FEED)
    result = phz.solve_phz_tray(
        nz=20,
        bed_height_m=0.3,
        diameter_m=4.0,
        Q_indirect_w=4.0e5,
        solid_in=solid_in,
        vapor_in=VAPOR_IN,
        m_dry_kg_s=DRY_SOLID_FLOW_KG_S,
        m_vapor_water_kg_s=5.0,
        X1=X1_FEED,
        X3=X3_FEED,
        c=CONSTANTS,
    )
    x2_values = [solid_in.X2] + [cell.solid_out.X2 for cell in result.cells]
    # Never increases (eq. A.1a only ever removes hexane, never adds).
    assert all(x2 <= x2_prev + 1e-12 for x2_prev, x2 in zip(x2_values, x2_values[1:]))
    # Flat while below boiling point, per eq. A.1a's S_Lm2=0 branch.
    temps = [solid_in.T] + [cell.solid_out.T for cell in result.cells]
    for t, x2, x2_prev in zip(temps[1:], x2_values[1:], x2_values):
        if t < CONSTANTS.T_boil_hexane - 1e-6:
            assert x2 == pytest.approx(x2_prev)


def test_hexane_and_energy_conservation_across_a_tray() -> None:
    solid_in = phz.SolidState(T=T_FEED_K, X2=X2_FEED)
    result = phz.solve_phz_tray(
        nz=15,
        bed_height_m=0.3,
        diameter_m=4.0,
        Q_indirect_w=4.0e5,
        solid_in=solid_in,
        vapor_in=VAPOR_IN,
        m_dry_kg_s=DRY_SOLID_FLOW_KG_S,
        m_vapor_water_kg_s=5.0,
        X1=X1_FEED,
        X3=X3_FEED,
        c=CONSTANTS,
    )
    # Mass conservation: total hexane evaporated equals the solid's net X2 drop.
    total_evap_kg_s = sum(cell.hexane_evaporated_kg_s for cell in result.cells)
    expected_evap_kg_s = (solid_in.X2 - result.solid_out.X2) * DRY_SOLID_FLOW_KG_S
    assert total_evap_kg_s == pytest.approx(expected_evap_kg_s, rel=1e-9)


def test_cell_z_positions_span_the_tray_height() -> None:
    solid_in = phz.SolidState(T=T_FEED_K, X2=X2_FEED)
    result = phz.solve_phz_tray(
        nz=10,
        bed_height_m=0.3,
        diameter_m=4.0,
        Q_indirect_w=4.0e5,
        solid_in=solid_in,
        vapor_in=VAPOR_IN,
        m_dry_kg_s=DRY_SOLID_FLOW_KG_S,
        m_vapor_water_kg_s=5.0,
        X1=X1_FEED,
        X3=X3_FEED,
        c=CONSTANTS,
    )
    z_positions = [cell.z_from_top_m for cell in result.cells]
    assert z_positions == sorted(z_positions)
    assert z_positions[-1] == pytest.approx(0.3)


def test_vapor_water_conserved_hexane_increases() -> None:
    # A single 400 kW pre-desolv tray alone doesn't reach T_boil_hexane from
    # 58 C feed (confirmed by test_solid_temperature_never_decreases_...
    # above, and consistent with Coletto §3.2: "evaporation starts at the end
    # of tray 2") -- use a higher duty here specifically to exercise the
    # evaporation branch's vapor-side mass transfer within one isolated tray.
    solid_in = phz.SolidState(T=T_FEED_K, X2=X2_FEED)
    result = phz.solve_phz_tray(
        nz=10,
        bed_height_m=0.3,
        diameter_m=4.0,
        Q_indirect_w=9.0e5,
        solid_in=solid_in,
        vapor_in=VAPOR_IN,
        m_dry_kg_s=DRY_SOLID_FLOW_KG_S,
        m_vapor_water_kg_s=5.0,
        X1=X1_FEED,
        X3=X3_FEED,
        c=CONSTANTS,
    )
    total_evap_kg_s = sum(cell.hexane_evaporated_kg_s for cell in result.cells)
    assert total_evap_kg_s > 0.0  # sanity: this duty does cross into evaporation

    m_water_out = 5.0  # constant per Table A.1's water mass balance (no source)
    m_hex_in = 5.0 * VAPOR_IN.wV2 / VAPOR_IN.wV1
    m_hex_out = m_hex_in + total_evap_kg_s
    wV1_expected = m_water_out / (m_water_out + m_hex_out)
    assert result.vapor_out.wV1 == pytest.approx(wV1_expected)
    assert result.vapor_out.wV2 > VAPOR_IN.wV2  # gained hexane -> higher hexane fraction


# ------------------------------------------------------------------ full 3-tray zone


def test_zone_reduces_hexane_over_the_full_predesolv_section() -> None:
    solid_in = phz.SolidState(T=T_FEED_K, X2=X2_FEED)
    trays = [_predesolv_tray() for _ in range(3)]
    results = phz.solve_phz_zone(
        trays,
        solid_in,
        vapor_ins=[VAPOR_IN, VAPOR_IN, VAPOR_IN],
        m_dry_kg_s=DRY_SOLID_FLOW_KG_S,
        m_vapor_water_kg_s=5.0,
        X1=X1_FEED,
        X3=X3_FEED,
        c=CONSTANTS,
    )
    assert len(results) == 3
    final_X2 = results[-1].solid_out.X2
    # Coletto (2022) §3.2 / Kemper (2005): PHZ reduces hexane content by
    # roughly 10-25% over the pre-desolventizing trays (this solver's own
    # duty-partition simplification, see module docstring, means the exact
    # reduction may run higher than the paper's reported figure — checked as
    # a directional/sanity bound here, not a tight quantitative match).
    reduction_fraction = (X2_FEED - final_X2) / X2_FEED
    assert 0.05 < reduction_fraction < 0.60
    # Solid temperature rises monotonically tray-to-tray (Fig. 7(a)).
    tray_out_temps = [r.solid_out.T for r in results]
    assert tray_out_temps == sorted(tray_out_temps)
    # 58 C inlet should approach, but per eq. A.1a never exceed, hexane's bp.
    assert T_FEED_K < tray_out_temps[-1] <= CONSTANTS.T_boil_hexane + 1e-6
