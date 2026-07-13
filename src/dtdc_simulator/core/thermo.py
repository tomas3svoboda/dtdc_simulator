"""Pure thermophysical / sorption / VLLE property functions — Coletto, Bandoni &
Blanco (2022) *J. Food Eng.* 318, 110870, Appendix A (sorption/critical-hexane,
eqs. 3-7 and A.31) and Appendix B (thermophysical correlations, B.1-B.12).

BuildSpec §3 invariant: this module must never import `config/`, `engine/`, or do
file/network I/O — it mirrors `core/model.py`'s purity constraint. Callers (in
practice `config/builder.py`, later `core/model.py`) translate validated cold
config into the plain dataclasses defined here.

This is BuildSpec §14 **M1**: thermo/sorption/properties only — pure, unit
-tested, no solver. It does not implement the zone sub-models (PHZ/FTRZ/DCZ,
§14 M2) or touch `core/model.py`.

PROVENANCE — read before trusting any number fed into these functions: the
soybean parameters in `properties/soybean.yaml` are tagged `[PAPER]`/`[PLACE]`/
`[STD]`/`[DERIVED]`; notably the oil-sorption power-law (`A0`, `B`) and the
heat-of-sorption constants (`sorption_C0`, `sorption_C1`) are cited by Coletto
(2022) to two unpublished PhD theses (Cardarelli 1998; Faner 2008) not available
to us — their *functional forms* below are implemented exactly as published, but
fed with `[PLACE]` order-of-magnitude constants until real data surfaces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

R_GAS = 8.314462618  # J/(mol K)
M_HEXANE = 0.08618  # kg/mol, n-hexane (C6H14)
M_WATER = 0.018015  # kg/mol
ATM_PRESSURE_PA = 101325.0


# ---------------------------------------------------------------------------
# Sorption isotherms (§7.4; Cardarelli & Crapiste 1996 eqs. [2]-[4]; Coletto eq. 7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GabParams:
    """GAB isotherm coefficients. `C`/`K` are exponential (van 't Hoff-type) in
    temperature — NOT linear; `Xm` is temperature-independent (Cardarelli &
    Crapiste 1996, Table 2)."""

    Xm: float  # kg/kg dry solid, monolayer capacity (their Hm)
    C0: float
    dHC_R: float  # K, delta_H_C / R
    K0: float
    dHK_R: float  # K, delta_H_K / R


@dataclass(frozen=True)
class OilIsotherm:
    A0: float
    B: float


def gab_hexane_content(a_h: float, T: float, p: GabParams) -> float:
    """W2_eq(a_h, T) — hexane adsorbed on the particle solid phase, kg/kg dry
    solid (Cardarelli & Crapiste 1996, eq. [2], with C/K per eqs. [3]-[4]).

    Valid only for `K*a_h < 1` (the GAB model's multilayer term diverges at the
    boundary, same as the water-sorption GAB literature it's adapted from).
    """
    if not 0.0 <= a_h <= 1.0:
        raise ValueError(f"hexane activity a_h must be in [0,1], got {a_h}")
    C = p.C0 * math.exp(p.dHC_R / T)
    K = p.K0 * math.exp(p.dHK_R / T)
    if K * a_h >= 1.0:
        raise ValueError(
            f"GAB isotherm invalid at a_h={a_h}, T={T}: K*a_h={K * a_h:.4f} >= 1 "
            "(outside the model's valid activity range at this temperature)"
        )
    return p.Xm * C * K * a_h / ((1.0 - K * a_h) * (1.0 - K * a_h + C * K * a_h))


def oil_hexane_content(a_h: float, p: OilIsotherm) -> float:
    """qo(a_h) = A0 * a_h^B — hexane absorbed in the oil phase, kg/kg oil (eq. 7)."""
    if not 0.0 <= a_h <= 1.0:
        raise ValueError(f"hexane activity a_h must be in [0,1], got {a_h}")
    return p.A0 * a_h**p.B


# ---------------------------------------------------------------------------
# Heat of sorption (eq. A.31)
# ---------------------------------------------------------------------------


def heat_of_sorption(W2: float, dH_lv2: float, sorption_C0: float, sorption_C1: float) -> float:
    """delta_H_s = delta_H_lv2 + C0*W2^C1 — net isosteric heat of sorption plus
    the latent heat of vaporization (eq. A.31)."""
    return dH_lv2 + sorption_C0 * W2**sorption_C1


# ---------------------------------------------------------------------------
# Critical / equilibrium hexane content (eqs. 4-6)
# ---------------------------------------------------------------------------


def rho_hexane_liquid(T: float) -> float:
    """Daubert & Danner correlation for liquid n-hexane density, kg/m3 (eqs.
    B.11-B.12). `T` in K; 507.6 K is n-hexane's critical temperature."""
    f1 = 1.0 + (1.0 - T / 507.6) ** 0.27537
    return 61.034 / (0.26411**f1)


def rho_hexane_vapor(T: float, P: float = ATM_PRESSURE_PA) -> float:
    """Ideal-gas density of pure hexane vapor at temperature T (K) and
    pressure P (Pa) — same ideal-gas form as eq. B.3."""
    return P * M_HEXANE / (R_GAS * T)


def x2_critical(alpha_pg: float, rho_hexL: float, alpha_ps: float, rho_ps: float) -> float:
    """X2,cr (eq. 4) — hexane content once the particle pores are just
    saturated with LIQUID hexane (surface hexane just gone)."""
    return (alpha_pg * rho_hexL) / (alpha_ps * rho_ps)


def ypg2_equilibrium(rho_hexV: float, alpha_ps: float, rho_ps: float) -> float:
    """Ypg2,eq (eq. 6) — dry-basis gas-phase hexane content when the pores hold
    only saturated hexane VAPOR."""
    return rho_hexV * (1.0 - alpha_ps) / (rho_ps * alpha_ps)


def x2_equilibrium(
    T: float,
    X3: float,
    gab: GabParams,
    oil: OilIsotherm,
    alpha_pg: float,
    alpha_ps: float,
    rho_ps: float,
    P: float = ATM_PRESSURE_PA,
) -> float:
    """X2,eq(T) (eq. 5) — hexane content when the pores hold only saturated
    hexane vapor, in equilibrium with adsorbed/absorbed hexane at that same
    saturation condition (a_h = 1 by definition of "pores saturated with gas
    hexane", per Coletto §2.3.2)."""
    W2_eq = gab_hexane_content(1.0, T, gab)
    qo_eq = oil_hexane_content(1.0, oil)
    rho_hexV = rho_hexane_vapor(T, P)
    Ypg2_eq = ypg2_equilibrium(rho_hexV, alpha_ps, rho_ps)
    return W2_eq + X3 * qo_eq + Ypg2_eq


# ---------------------------------------------------------------------------
# Thermophysical properties — Appendix B (implement exactly)
# ---------------------------------------------------------------------------


def rho_vip(yV1: float, yV2: float, T: float, P: float = ATM_PRESSURE_PA) -> float:
    """rho_Vip (eq. B.3) — interparticle vapor density, ideal-gas mixture of
    water (mol frac yV1) and hexane (mol frac yV2) at temperature T (K)."""
    return (yV1 * M_WATER + yV2 * M_HEXANE) * P / (R_GAS * T)


def rho_lmix(alpha_L: float, rho_L: float, alpha_V: float, rho_vip_: float) -> float:
    """rho_Lmix (eq. B.1) — cake/inter-particle mixture density in the PHZ."""
    return alpha_L * rho_L + alpha_V * rho_vip_


def rho_l(alpha_ps: float, rho_ps: float, X1: float, X2: float, X3: float) -> float:
    """rho_L (eq. B.2) — descending solid (wet meal) density."""
    return alpha_ps * rho_ps * (1.0 + X1 + X2 + X3)


def cp_l(
    w_water: float,
    w_hexane: float,
    w_oil: float,
    w_ds: float,
    cps: tuple[float, float, float, float],
) -> float:
    """CPL (eq. B.5) — descending-solid specific heat, mass-fraction-weighted
    sum over the 4 components (water, hexane, oil, dry solid), in that order,
    matching `cps = (cp_water_liquid, cp_hexane_liquid, cp_oil, cp_solid)`."""
    weights = (w_water, w_hexane, w_oil, w_ds)
    return sum(w * cp for w, cp in zip(weights, cps))


def cp_vip(w_water_vapor: float, w_hexane_vapor: float, cps: tuple[float, float]) -> float:
    """CPVip (eq. B.6) — intra-particle vapor specific heat, mass-fraction
    -weighted sum over the 2 vapor components (water, hexane), matching
    `cps = (cp_water_vapor, cp_hexane_vapor)`."""
    weights = (w_water_vapor, w_hexane_vapor)
    return sum(w * cp for w, cp in zip(weights, cps))


def cp_lmix(
    alpha_L: float,
    rho_L: float,
    cp_L: float,
    alpha_V: float,
    rho_V: float,
    cp_Vip: float,
    rho_Lmix_: float,
) -> float:
    """CPLmix (eq. B.4) — cake/inter-particle mixture specific heat in the PHZ."""
    return (alpha_L * rho_L * cp_L + alpha_V * rho_V * cp_Vip) / rho_Lmix_


def nu_from_reynolds(Re: float, Pr: float) -> float:
    """Nu_epsilon = 0.6949 * Re^0.579 * Pr^(1/3) — Faner's correlation (eq. B.7)."""
    return 0.6949 * Re**0.579 * Pr ** (1.0 / 3.0)


def hq_from_nu(Nu: float, r_P: float, k_V: float, alpha_V: float, alpha_L: float) -> float:
    """hQ from Nu_epsilon = 2*hQ*rP/kV * (alphaV/alphaL) (eq. B.8), solved for hQ."""
    return Nu * k_V * alpha_L / (2.0 * r_P * alpha_V)


def schmidt_number(mu_V: float, rho_V: float, D_HW: float) -> float:
    """Sc_p = muV / (rhoV * D_HW) (eq. B.10)."""
    return mu_V / (rho_V * D_HW)


def hm_from_hq(hQ: float, rho_V: float, cp_V: float, Pr: float, Sc: float) -> float:
    """hM from the Chilton-Colburn analogy (eq. B.9)."""
    return (hQ / (rho_V * cp_V)) * (Pr / Sc) ** (2.0 / 3.0)


# ---------------------------------------------------------------------------
# VLLE dew-point curve (§A.2.3 / §2.3.4) — V-SCAL (superheated) -> V-SAT (dew curve)
#
# The paper describes the physics (vapor enters FTRZ superheated, cools until it
# hits the dew point for its current composition, then evolves along the dew
# curve as water condenses) but does not spell out the solve algorithm or the
# enthalpy datum. Two decisions made here (confirmed with the user):
#
# 1. Dew point = single-condensable-component Raoult's law on WATER only
#    (y_water*P = P_water,sat(T_dew)), not the two-liquid azeotrope co-boiling
#    point — valid because the paper explicitly restricts FTRZ to "hexane content
#    below the azeotropic value," where the only liquid that forms is water and
#    hexane stays entirely in the vapor as an inert carrier. This also matches
#    the paper's own Fig. 8(b), where the reported FTRZ dew temperature dips
#    below 100 C as hexane dilutes the vapor and suppresses water's partial
#    pressure.
# 2. Enthalpy datum: liquid water at its own boiling point and liquid hexane at
#    its own boiling point are the zero-enthalpy references for the respective
#    vapor-phase components (standard humid-gas-type convention). This is a
#    reasonable, documented construction filling a gap the paper leaves
#    implicit -- not a value stated in the paper itself.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AntoineParams:
    A: float
    B: float
    C: float


def antoine_pressure_pa(T: float, p: AntoineParams) -> float:
    """log10(P[bar]) = A - B/(C+T) -> saturation pressure in Pa."""
    return (10.0 ** (p.A - p.B / (p.C + T))) * 1.0e5


def _y_water_mole_fraction(Y_V2: float) -> float:
    """Y_V2 = kg hexane / kg water (water basis, eq. A.2b) -> mole fraction of
    water in the (water + hexane) vapor."""
    mole_ratio_hex_to_water = Y_V2 * M_WATER / M_HEXANE
    return 1.0 / (1.0 + mole_ratio_hex_to_water)


def dew_point_temperature(
    Y_V2: float,
    antoine_water: AntoineParams,
    P: float = ATM_PRESSURE_PA,
    T_bounds: tuple[float, float] = (230.0, 450.0),
) -> float:
    """T_dew(Y_V2) solving y_water*P = P_water,sat(T_dew) (Raoult's law, water
    condensing from a hexane-diluted vapor acting as an inert carrier)."""
    from scipy.optimize import brentq

    target = _y_water_mole_fraction(Y_V2) * P

    def residual(T: float) -> float:
        return antoine_pressure_pa(T, antoine_water) - target

    return brentq(residual, *T_bounds)


@dataclass(frozen=True)
class VaporEnthalpyRef:
    """Zero-enthalpy datum for the water-basis vapor enthalpy curve: liquid
    water at its own bp, liquid hexane at its own bp."""

    dH_vap_water: float
    cp_water_vapor: float
    T_boil_water: float
    dH_vap_hexane: float
    cp_hexane_vapor: float
    T_boil_hexane: float


def vapor_enthalpy_water_basis(Y_V2: float, T_V: float, ref: VaporEnthalpyRef) -> float:
    """Hvbw = f(YV2, TV) (eq. A.5) in the superheated (V-SCAL) regime: a
    mass-fraction-weighted (water-basis) sum of the water and hexane vapor
    specific enthalpies, each referenced to its own liquid at its own bp."""
    H_water = ref.dH_vap_water + ref.cp_water_vapor * (T_V - ref.T_boil_water)
    H_hexane = ref.dH_vap_hexane + ref.cp_hexane_vapor * (T_V - ref.T_boil_hexane)
    return H_water + Y_V2 * H_hexane


def temperature_from_vapor_enthalpy(H_vbw: float, Y_V2: float, ref: VaporEnthalpyRef) -> float:
    """Inverse of `vapor_enthalpy_water_basis`: T_V from (Hvbw, YV2). Closed
    -form since the enthalpy is linear in T_V (BuildSpec §7.4 asks for this
    inverse alongside the forward curve)."""
    a = (ref.dH_vap_water - ref.cp_water_vapor * ref.T_boil_water) + Y_V2 * (
        ref.dH_vap_hexane - ref.cp_hexane_vapor * ref.T_boil_hexane
    )
    b = ref.cp_water_vapor + Y_V2 * ref.cp_hexane_vapor
    return (H_vbw - a) / b


def dew_point_enthalpy_water_basis(
    Y_V2: float,
    antoine_water: AntoineParams,
    ref: VaporEnthalpyRef,
    P: float = ATM_PRESSURE_PA,
) -> float:
    """Hvbw = f(YV2) (eq. A.5) once the vapor is on the dew curve (V-SAT): the
    same enthalpy curve evaluated at T = T_dew(YV2), collapsing the remaining
    degree of freedom exactly as §A.2.3 describes."""
    T_dew = dew_point_temperature(Y_V2, antoine_water, P)
    return vapor_enthalpy_water_basis(Y_V2, T_dew, ref)
