"""Dryer/Cooler (DC) air-contacting stages — BuildSpec §7.10. Not covered by
Coletto (2022); the DT-side model is `core/dt_solver.py`. M3a (BuildSpec §14):
one well-mixed 0-D air-solid contactor balance, shared by DRYER and COOLER
("use the same per-stage balance structure with air as the gas phase",
§7.10) — only the air-stream arguments differ between the two roles.

MOISTURE TARGET (updated, DCZ moisture follow-up session): solid-side
moisture equilibrium now comes from the SAME real water-sorption isotherm
DCZ's own moisture balance uses (`core/zones/dcz.py`'s "MOISTURE (H2O)
BALANCE" section, `thermo.luikov_equilibrium_moisture` — Gianini et al. 2006,
soybean meal sampled directly from a desolventizer/toaster's own outlet), not
the earlier air-side-mass-balance-derived target (a constant-rate
drying-period assumption: the solid can always supply whatever moisture the
air's own humidity deficit calls for, up to the moisture actually present).
The isotherm target is a genuine equilibrium (hygroscopic solid <-> local air
humidity), so it naturally captures the falling-rate regime near the dry end
that the old air-side balance couldn't (no separate mechanism needed for
that). The existing `effectiveness`/energy-cap RELAXATION machinery is
unchanged — only what `X1` relaxes TOWARD changed, not the shape of the
approach to it.

Both mass and energy transfer use one shared "effectiveness"
(`1 - exp(-air_flow/m_dry)`), the same exponential-approach-to-equilibrium
shape the M0 placeholder already used for temperature — generalized here to
a real psychrometric mass balance instead of a fitted rate, and reused for
temperature so the two are mutually consistent (moisture removed is
credited its latent heat in the same balance that sets the exit
temperature).

BEHAVIOR NOTE, found by testing (not a bug once understood): water's latent
heat (~2.26 MJ/kg) is expensive relative to a typical air stream's sensible
capacity (~1 kJ/(kg K)). At realistic dryer air:solid mass ratios (this
project's own scenario sits around ~0.2-0.3), evaporation is usually
ENERGY-limited, not humidity-driving-force-limited or moisture-availability
-limited (see the energy cap below) -- meaning `T_eq` commonly lands very
close to `T_in` (all sensible heat consumed evaporating moisture) rather
than showing dramatic warming, exactly the "constant-rate drying period"
physically expected of an evaporatively-cooled wet surface. Net warming only
shows through once moisture is scarce enough that availability, not energy,
becomes the binding constraint. See `tests/test_dc.py` for both regimes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from dtdc_simulator.core import thermo

CP_AIR_J_KG_K = 1005.0  # [STD] dry air specific heat
M_AIR_KG_MOL = 0.02896  # [STD] dry air molar mass
_EPS = 1.0e-9


@dataclass(frozen=True)
class DCConstants:
    cp_solid: float
    cp_water_liquid: float
    dH_vap_water: float
    antoine_water: thermo.AntoineParams
    dc_hexane_strip_k: float  # [PLACE], see air_contact_equilibrium docstring
    luikov: thermo.LuikovParams  # solid-side moisture isotherm, see module docstring


def saturation_humidity_ratio(
    T: float, antoine_water: thermo.AntoineParams, P: float = thermo.ATM_PRESSURE_PA
) -> float:
    """Y_sat(T) = (Mw/Mair) * Psat(T) / (P - Psat(T)) -- standard psychrometric
    saturation humidity ratio [kg water / kg dry air], reusing the existing
    Antoine correlation (`thermo.antoine_pressure_pa`) unchanged."""
    p_sat = thermo.antoine_pressure_pa(T, antoine_water)
    p_sat = min(p_sat, P - _EPS)  # guard: stay below total pressure (avoid a negative denominator)
    return (thermo.M_WATER / M_AIR_KG_MOL) * p_sat / (P - p_sat)


def _air_water_activity(
    Y: float, T: float, antoine_water: thermo.AntoineParams, P: float = thermo.ATM_PRESSURE_PA
) -> float:
    """a_w = p_water/p_water,sat(T) from an air stream's own humidity ratio Y
    [kg water/kg dry air] -- the algebraic inverse of `saturation_humidity_
    ratio` (solves the SAME `Y = (Mw/Mair)*p_w/(P-p_w)` relation for `p_w`
    instead of for `Y`), not the DCZ-specific `thermo.water_activity` (that
    one's `Y_V2` convention is hexane-vapor-relative, meaningless for an
    air/water stream with no hexane in it)."""
    mw_over_mair = thermo.M_WATER / M_AIR_KG_MOL
    p_w = Y * P / (mw_over_mair + Y)
    return p_w / thermo.antoine_pressure_pa(T, antoine_water)


def air_contact_equilibrium(
    T_in: float,
    X1_in: float,
    X2_in: float,
    air_T: float,
    air_flow_kg_s: float,
    air_humidity_in: float,
    m_dry_kg_s: float,
    c: DCConstants,
) -> tuple[float, float, float, float, float]:
    """One well-mixed 0-D air-solid contactor balance -- returns `(T_eq,
    X1_eq, X2_eq, air_T_out, air_humidity_out)`. `T_eq`/`X1_eq`/`X2_eq` are
    the SOLID's own equilibrium targets the caller's existing first-order
    lag/holdup relaxation (`core/model.py::Model.step`) relaxes toward,
    exactly the same role `_stage_equilibrium`'s old placeholder branches
    played. `air_T_out`/`air_humidity_out` (added for `core/balance.py`'s
    two-sided mass/energy conservation checks -- previously this function
    reported only the solid side, making the air side unverifiable) are the
    air stream's own exit state: `air_humidity_out` from the SAME
    `m_evap_kg_s` mass balance `X1_eq` already uses (air gains exactly what
    the solid loses, or loses exactly what the solid gains); `air_T_out`
    from the SAME sensible-heat exchange `Q_sensible_w` already uses (energy
    -balance consistent: `air_T - Q_sensible_w/C_air`).
    Deliberately does NOT model the evaporated moisture's own enthalpy
    joining the air stream (a documented simplification, same category as
    DC's own missing hexane latent heat below) -- `air_T_out` reflects only
    the SENSIBLE exchange with the solid, not vapor addition's own sensible
    +latent contribution to the air. See module docstring for the moisture
    -equilibrium simplification and the shared-effectiveness rationale.

    `Q_sensible_w` USES THE MINIMUM HEAT-CAPACITY-RATE STREAM (`C_min =
    min(C_air, C_solid)`, the standard heat-exchanger effectiveness-NTU
    convention), NOT unconditionally the air's own rate -- found this
    session (a real bug, not a tuning issue): the old formula
    (`effectiveness*air_flow_kg_s*CP_AIR_J_KG_K*(air_T-T_in)`) computed the
    energy the AIR stream itself could give up/absorb, then applied that
    DIRECTLY to the SOLID's own (potentially much smaller) thermal mass with
    no cross-check -- confirmed to let the solid's own `T_eq` overshoot PAST
    `air_T` (e.g. cooling BELOW the ambient air's own temperature, a
    thermodynamic impossibility for a passive contactor) whenever
    `air_flow_kg_s*CP_AIR_J_KG_K` exceeds `m_dry_kg_s*C_wet` -- a real risk
    at the realistic (not tiny) air:solid ratios a genuine dryer/cooler
    needs (found while recalibrating `ambient_air_flow`/`heated_air_flow`
    against real SCADA reference values). Using `C_min` guarantees `T_eq`
    stays within `[min(T_in,air_T), max(T_in,air_T)]` by construction
    (`effectiveness<=1` and `C_min<=C_solid` together bound the achievable
    temperature change to at most the full driving-force gap).
    """
    m_dry_safe = max(m_dry_kg_s, _EPS)
    effectiveness = 1.0 - math.exp(-air_flow_kg_s / m_dry_safe)
    C_wet = c.cp_solid + X1_in * c.cp_water_liquid  # J/(kg dry solid . K), needed here now
    C_air = air_flow_kg_s * CP_AIR_J_KG_K
    C_solid = m_dry_safe * max(C_wet, _EPS)
    C_min = min(C_air, C_solid)
    Q_sensible_w = effectiveness * C_min * (air_T - T_in)

    # Isotherm target (see module docstring): a_w from the INCOMING air's own
    # state (T,humidity) -- the same "driving condition" role `air_T` plays
    # for `T_eq` below, not a derived exit condition. Above water's own
    # boiling point at 1 atm (the scenario's own `heated_air_temp`, 380 K,
    # sits there), `p_water,sat(T)` is large, so `a_w` naturally comes out
    # very small (hot dry air) and the isotherm target goes to near-zero --
    # this replaces the OLD `saturation_humidity_ratio`-based approach's own
    # need for a special "no physical ceiling above bp" workaround with a
    # target that's just correctly dry in that regime by construction.
    a_w_air = _air_water_activity(air_humidity_in, air_T, c.antoine_water)
    a_w_air = min(max(a_w_air, 1.0e-9), thermo.LUIKOV_MAX_VALIDATED_UR)
    X1_isotherm_target = thermo.luikov_equilibrium_moisture(a_w_air, c.luikov)
    X1_relaxed = X1_in + effectiveness * (X1_isotherm_target - X1_in)
    m_evap_kg_s = X1_in - X1_relaxed  # positive = evaporating, negative = adsorbing

    # NOTE (a real failure mode caught by testing, not assumed away, kept
    # from the earlier air-side-balance version): evaporation can't exceed
    # the SENSIBLE heat actually available (Q_sensible_w, at most) -- taken
    # at face value, the isotherm's own driving force alone could imply
    # evaporating far more moisture than the air stream can thermodynamically
    # supply the latent heat for, driving T_eq to an unphysical result
    # (confirmed on the old mechanism: dropped to ~180 K on a simple
    # hot-dry-air case). Only applies to EVAPORATION -- adsorption is
    # exothermic (releases latent heat, doesn't consume sensible heat), so it
    # has no equivalent cap; the `effectiveness`-bounded relaxation above is
    # already self-limiting for that direction.
    if m_evap_kg_s > 0.0:
        m_evap_energy_cap_kg_s = max(Q_sensible_w, 0.0) / c.dH_vap_water
        m_evap_kg_s = min(m_evap_kg_s, X1_in * m_dry_safe, m_evap_energy_cap_kg_s)
    X1_eq = X1_in - m_evap_kg_s / m_dry_safe

    Q_latent_w = m_evap_kg_s * c.dH_vap_water
    T_eq = T_in + (Q_sensible_w - Q_latent_w) / C_solid

    X2_eq = X2_in * math.exp(-c.dc_hexane_strip_k * air_flow_kg_s / m_dry_safe)

    air_T_out = air_T - Q_sensible_w / max(C_air, _EPS)
    air_flow_safe = max(air_flow_kg_s, _EPS)
    air_humidity_out = air_humidity_in + m_evap_kg_s / air_flow_safe

    return (
        T_eq,
        min(max(X1_eq, 0.0), 1.0),
        min(max(X2_eq, 0.0), 1.0),
        air_T_out,
        max(air_humidity_out, 0.0),
    )
