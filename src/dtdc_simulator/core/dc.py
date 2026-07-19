"""Dryer/Cooler (DC) air-contacting stages — BuildSpec §7.10. Not covered by
Coletto (2022); the DT-side model is `core/dt_solver.py`. One well-mixed 0-D
air-solid contactor balance, shared by DRYER and COOLER ("use the same
per-stage balance structure with air as the gas phase", §7.10) — only the
air-stream arguments differ between the two roles.

FIRST-PRINCIPLES REWRITE (this session, following Luz et al. 2010 and Silva
et al. 2012 — see `literature_sources/`): the previous model treated the
dryer as a WET-SURFACE, CONSTANT-RATE contactor that evaporatively cooled the
meal to the air's adiabatic-saturation temperature and relaxed solid moisture
most of the way toward the air-humidity isotherm equilibrium each pass. That
is physically wrong for soybean meal, and produced two reported symptoms:
(1) hot (~105 C) meal entering a ~107 C dryer read ~43 C on the tray, and
(2) the air side didn't conserve — inlet air state didn't reconcile with the
outlet.

Both Luz (2010) and Silva (2012) establish that soybean-meal drying is
ENTIRELY falling-rate (internal-diffusion-controlled) — there is NO
constant-rate period, and the drying RATE, not the air's saturation capacity,
governs. The rate is `K*(X1 - X_e)` with a SMALL `K` (`thermo.
luz_mass_transfer_coefficient`, ~8.44e-3/s), so per residence time only a
fraction of removable moisture actually leaves, the latent load is modest,
and the meal STAYS HOT in the dryer (Luz's own industrial case: 90 C air ->
89 C solid out). The COOLER (ambient air) then carries the actual cooling.

This module now models each DC stage as a well-mixed CSTR at its own steady
state, with a CLOSED two-sided mass/energy balance:

  * MOISTURE (solid): falling-rate CSTR — `X1_eq = (X1_in + K*tau*X_e)/(1 +
    K*tau)`, `K = thermo.luz_mass_transfer_coefficient`, `X_e = thermo.
    luz_equilibrium_moisture` (the temperature-dependent DRYING isotherm, Luz
    eq. 5 — NOT the DT-internal Gianini/Luikov desorption isotherm). Capped by
    the air's own saturation carrying-capacity (so a no-air stage dries
    nothing) and by the moisture actually present.
  * MOISTURE (air): `Y_out = Y_in + m_evap/m_air_dry` — dry air is conserved
    EXACTLY; the humidity accounts for every kg the solid loses (or gains, in
    the adsorption regime). This is the closed air mass balance.
  * ENERGY (solid): sensible pickup from the air (effectiveness-NTU, bounded
    by the minimum heat-capacity-rate stream `C_min`) minus the now-small
    latent load — meal heats toward hot dryer air, cools toward cold cooler
    air, in one uniform formula (no separate evaporative-cooling branch).
  * ENERGY (air): outlet air temperature is closed by the ADIABATIC
    total-enthalpy balance (`_close_air_temperature`), so the two-sided
    energy balance holds to machine precision — `core/balance.py::
    dc_stage_balance` verifies it independently from the reported boundary
    states.

Hexane air-stripping (`X2`) is unchanged — a first-order decay in the
air:solid ratio, orthogonal to the water/energy physics above.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from dtdc_simulator.core import thermo

CP_AIR_J_KG_K = 1005.0  # [STD] dry air specific heat
M_AIR_KG_MOL = 0.02896  # [STD] dry air molar mass
_EPS = 1.0e-9
_T_ENTHALPY_REF_K = 273.15  # 0 C datum for the closed two-sided enthalpy balance
_FIXED_POINT_ITERS = 6  # inner passes for the weak X_e(T_s)/K(X1) coupling (converges fast)


@dataclass(frozen=True)
class DCConstants:
    cp_solid: float
    cp_water_liquid: float
    dH_vap_water: float  # J/kg, latent heat of water (referenced at the 0 C enthalpy datum)
    antoine_water: thermo.AntoineParams
    dc_hexane_strip_k: float  # [PLACE], see air_contact_equilibrium docstring
    luz: thermo.LuzDryingParams  # soybean-meal air-drying K + isotherm (Luz 2010), see thermo.py
    cp_water_vapor: float  # J/(kg K), humid-air enthalpy for the closed energy balance


def saturation_humidity_ratio(
    T: float, antoine_water: thermo.AntoineParams, P: float = thermo.ATM_PRESSURE_PA
) -> float:
    """Y_sat(T) = (Mw/Mair) * Psat(T) / (P - Psat(T)) -- standard psychrometric
    saturation humidity ratio [kg water / kg dry air], reusing the existing
    Antoine correlation (`thermo.antoine_pressure_pa`) unchanged."""
    p_sat = thermo.antoine_pressure_pa(T, antoine_water)
    p_sat = min(p_sat, P - _EPS)  # guard: stay below total pressure (avoid a negative denominator)
    return (thermo.M_WATER / M_AIR_KG_MOL) * p_sat / (P - p_sat)


def air_relative_humidity(
    Y: float, T: float, antoine_water: thermo.AntoineParams, P: float = thermo.ATM_PRESSURE_PA
) -> float:
    """ur = p_water/p_water,sat(T) from an air stream's own humidity ratio Y
    [kg water/kg dry air] -- solves the same `Y = (Mw/Mair)*p_w/(P-p_w)`
    relation `saturation_humidity_ratio` inverts, for `p_w`, then divides by
    the saturation pressure at the local air temperature. This is the local
    air relative humidity the Luz drying isotherm (`thermo.
    luz_equilibrium_moisture`) takes as its activity argument. Above water's
    boiling point at `P` (hot dryer air), `p_sat` is large, so `ur` comes out
    correctly tiny and the isotherm equilibrium goes to near-zero -- hot dry
    air genuinely equilibrates against a nearly bone-dry solid."""
    mw_over_mair = thermo.M_WATER / M_AIR_KG_MOL
    p_w = Y * P / (mw_over_mair + Y)
    return p_w / thermo.antoine_pressure_pa(T, antoine_water)


def solid_stream_enthalpy_w(m_dry: float, T: float, X1: float, c: DCConstants) -> float:
    """Enthalpy flow [W] of a dry-solid + liquid-moisture stream, 0 C datum."""
    return m_dry * (c.cp_solid + X1 * c.cp_water_liquid) * (T - _T_ENTHALPY_REF_K)


def air_stream_enthalpy_w(m_air_dry: float, T: float, Y: float, c: DCConstants) -> float:
    """Enthalpy flow [W] of a dry-air + water-vapor stream, 0 C datum. Vapor
    carries its latent heat (`dH_vap_water`, at the datum) plus its sensible
    heat -- the standard psychrometric moist-air enthalpy convention. Shared
    with `core/balance.py::dc_stage_balance` so the closure check uses the
    identical accounting the outlet temperature was solved against."""
    return m_air_dry * (
        CP_AIR_J_KG_K * (T - _T_ENTHALPY_REF_K)
        + Y * (c.dH_vap_water + c.cp_water_vapor * (T - _T_ENTHALPY_REF_K))
    )


def _close_air_temperature(
    m_dry: float,
    T_in: float,
    X1_in: float,
    air_flow_kg_s: float,
    air_T: float,
    air_humidity_in: float,
    T_s: float,
    X1_eq: float,
    air_humidity_out: float,
    c: DCConstants,
) -> float:
    """Outlet air temperature that closes the ADIABATIC total-enthalpy balance
    `H_in = H_out` given the (independently-set) solid outlet state. Makes the
    two-sided energy balance hold to machine precision by construction; the
    solid's own temperature `T_s` is set by the sensible-pickup/latent-load
    model in `air_contact_equilibrium`, and this absorbs the remainder into
    the honest energy-conserving air exit."""
    h_in = solid_stream_enthalpy_w(m_dry, T_in, X1_in, c) + air_stream_enthalpy_w(
        air_flow_kg_s, air_T, air_humidity_in, c
    )
    h_solid_out = solid_stream_enthalpy_w(m_dry, T_s, X1_eq, c)
    # air_out = m_air*(cp_air + Y_out*cp_v)*(T_out - Tref) + m_air*Y_out*dH_vap
    coeff = air_flow_kg_s * (CP_AIR_J_KG_K + air_humidity_out * c.cp_water_vapor)
    latent = air_flow_kg_s * air_humidity_out * c.dH_vap_water
    return _T_ENTHALPY_REF_K + (h_in - h_solid_out - latent) / max(coeff, _EPS)


def air_contact_equilibrium(
    T_in: float,
    X1_in: float,
    X2_in: float,
    air_T: float,
    air_flow_kg_s: float,
    air_humidity_in: float,
    m_dry_kg_s: float,
    residence_s: float,
    c: DCConstants,
) -> tuple[float, float, float, float, float]:
    """One well-mixed 0-D air-solid contactor at its steady state -- returns
    `(T_eq, X1_eq, X2_eq, air_T_out, air_humidity_out)`. `T_eq`/`X1_eq`/
    `X2_eq` are the SOLID's own steady-state exit targets the caller's
    first-order lag/holdup relaxation (`core/model.py::Model.step`) then
    relaxes toward (exactly the role the DT-side per-tray targets play);
    `air_T_out`/`air_humidity_out` are the air stream's own exit state, used
    by `core/model.py`'s air-outlet readout and by `core/balance.py`'s
    two-sided conservation check.

    `residence_s` is the stage's own solid residence time (`Model._stage_tau`,
    the same holdup time the lag relaxation uses) -- it sets `K*tau`, i.e. how
    far down the falling-rate curve the meal actually gets in one pass. This
    is the crux of the falling-rate model: with a small `K` and a finite
    residence, the meal removes only a FRACTION of its removable moisture and
    stays hot, rather than racing to the (near-zero, for hot dry air)
    isotherm equilibrium. See the module docstring.
    """
    m_dry_safe = max(m_dry_kg_s, _EPS)
    tau = max(residence_s, 0.0)

    # No air flow -> no contact: the solid and air both leave unchanged (guards
    # the coupled solve below against a zero air heat-capacity/enthalpy rate).
    if air_flow_kg_s < _EPS:
        X2_eq = X2_in
        return (T_in, min(max(X1_in, 0.0), 1.0), min(max(X2_eq, 0.0), 1.0), air_T, air_humidity_in)

    # Air-solid heat-transfer conductance UA [W/K] from the NTU convention
    # (NTU = air:solid ratio, effectiveness 1-exp(-NTU) as before), scaled by
    # the minimum heat-capacity-rate stream. This is what lets the HOT air
    # supply the evaporation's latent load (keeping the meal warm at high air
    # flow) instead of the solid's own sensible heat funding it alone.
    C_wet_in = c.cp_solid + max(X1_in, 0.0) * c.cp_water_liquid
    C_solid = m_dry_safe * max(C_wet_in, _EPS)
    C_air = air_flow_kg_s * (CP_AIR_J_KG_K + air_humidity_in * c.cp_water_vapor)
    UA = (air_flow_kg_s / m_dry_safe) * min(C_air, C_solid)

    # Local air relative humidity -> Luz drying-isotherm equilibrium moisture.
    ur = air_relative_humidity(air_humidity_in, air_T, c.antoine_water)

    # Physical caps on the moisture transfer: the air can absorb at most its
    # own saturation deficit, can give up at most the moisture it carries, and
    # the solid can lose at most the moisture it holds.
    Y_sat = saturation_humidity_ratio(air_T, c.antoine_water)
    m_evap_air_cap = max(Y_sat - air_humidity_in, 0.0) * air_flow_kg_s
    m_adsorb_air_cap = air_humidity_in * air_flow_kg_s
    m_solid_avail = max(X1_in, 0.0) * m_dry_safe

    # Total enthalpy entering the stage (0 C datum) -- fixed; the outlet air
    # temperature is solved to conserve it exactly (closing the balance).
    h_in = solid_stream_enthalpy_w(m_dry_safe, T_in, X1_in, c) + air_stream_enthalpy_w(
        air_flow_kg_s, air_T, air_humidity_in, c
    )
    tref = _T_ENTHALPY_REF_K

    # Coupled moisture + two-phase energy balance, iterated for the weak
    # X_e(T_s)/K(X1) coupling (converges in a few passes). At each pass:
    #   moisture -> m_evap, X1_eq (falling-rate CSTR, capped)
    #   energy   -> T_s from a Newton-cooling solid balance convectively
    #               coupled (UA) to the OUTLET air temperature, which is itself
    #               pinned by adiabatic enthalpy conservation T_a_out = A0 - A1*T_s
    T_s = T_in
    m_evap = 0.0
    X1_eq = X1_in
    air_humidity_out = air_humidity_in
    for _ in range(_FIXED_POINT_ITERS):
        X_e = thermo.luz_equilibrium_moisture(T_s, ur, c.luz)
        K = thermo.luz_mass_transfer_coefficient(air_T, X1_eq, c.luz)
        Ktau = K * tau
        X1_cstr = (X1_in + Ktau * X_e) / (1.0 + Ktau)
        m_evap = m_dry_safe * (X1_in - X1_cstr)
        if m_evap >= 0.0:
            m_evap = min(m_evap, m_evap_air_cap, m_solid_avail)  # drying
        else:
            m_evap = max(m_evap, -m_adsorb_air_cap)  # adsorption (humid air onto a dry solid)
        X1_eq = X1_in - m_evap / m_dry_safe
        Q_latent = m_evap * c.dH_vap_water

        air_humidity_out = max(air_humidity_in + m_evap / air_flow_kg_s, 0.0)
        C_wet_out = c.cp_solid + X1_eq * c.cp_water_liquid
        # Air enthalpy closure, linear in T_s: T_a_out = A0 - A1*T_s.
        denom_a = air_flow_kg_s * (CP_AIR_J_KG_K + air_humidity_out * c.cp_water_vapor)
        A1 = m_dry_safe * C_wet_out / denom_a
        A0 = tref + (
            h_in + m_dry_safe * C_wet_out * tref - air_flow_kg_s * air_humidity_out * c.dH_vap_water
        ) / denom_a
        # Solid energy: m_dry*(C_wet_out*(T_s-Tref) - C_wet_in*(T_in-Tref))
        #             = UA*(T_a_out - T_s) - Q_latent, with T_a_out = A0 - A1*T_s.
        num = (
            UA * A0 - Q_latent + m_dry_safe * C_wet_out * tref + m_dry_safe * C_wet_in * (T_in - tref)
        )
        den = m_dry_safe * C_wet_out + UA * (A1 + 1.0)
        T_s = num / den

    # Outlet air temperature closes the adiabatic enthalpy balance exactly
    # (H_in == H_out) given the converged solid outlet state.
    air_T_out = _close_air_temperature(
        m_dry_safe, T_in, X1_in, air_flow_kg_s, air_T, air_humidity_in, T_s, X1_eq, air_humidity_out, c
    )

    # Residual hexane air-stripping (first-order in the air:solid ratio) --
    # unchanged; orthogonal to the water/energy balance above.
    X2_eq = X2_in * math.exp(-c.dc_hexane_strip_k * air_flow_kg_s / m_dry_safe)

    return (
        T_s,
        min(max(X1_eq, 0.0), 1.0),
        min(max(X2_eq, 0.0), 1.0),
        air_T_out,
        air_humidity_out,
    )
