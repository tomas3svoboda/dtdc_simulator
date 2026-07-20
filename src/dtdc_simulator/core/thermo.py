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


def hexane_activity_from_loading(W2: float, T: float, p: GabParams, a_h_max: float = 1.0) -> float:
    """Inverse of `gab_hexane_content`: the hexane activity `a_h` whose GAB
    equilibrium loading equals `W2` (kg/kg dry solid) at temperature `T`. The
    isotherm is monotone in `a_h` on (0, 1/K), so the root is unique; clamped to
    `(0, min(a_h_max, 1/K))`. Used by the DC hexane desorption driving force
    (`core/dc.py`): the solid's own equilibrium hexane partial pressure is
    `a_h * p_sat(T)`, the "escaping tendency" that collapses at low temperature."""
    if W2 <= 0.0:
        return 0.0
    K = p.K0 * math.exp(p.dHK_R / T)
    upper = min(a_h_max, 1.0 / K) - 1.0e-9
    if upper <= 1.0e-12:
        return 0.0
    if gab_hexane_content(upper, T, p) <= W2:
        return upper
    from scipy.optimize import brentq

    return brentq(lambda a: gab_hexane_content(a, T, p) - W2, 1.0e-12, upper)


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


def x2_critical(
    alpha_pg: float,
    rho_hexL: float,
    alpha_ps: float,
    rho_ps: float,
    empirical: float | None = None,
) -> float:
    """X2,cr -- the critical hexane content at the constant-rate -> falling-rate
    (surface-evaporation -> receding-front) transition.

    `empirical`, when given, is returned directly (temperature-independent): use
    it to supply Faner, Perez & Crapiste (2019)'s MEASURED soybean value
    (X_c ~= 0.20), which is the constant->falling transition actually observed in
    superheated-hexane desolventizing. The default `None` falls back to Coletto
    eq. 4 -- the THEORETICAL pore-liquid-saturation content
    `(alpha_pg*rho_hexL)/(alpha_ps*rho_ps)` (~0.43), i.e. the content at which the
    pores are just full of liquid hexane. The two disagree ~2x because capillarity
    keeps the particle surface wetted (constant rate) well below full pore
    saturation; Faner's empirical value is the physically-observed transition and
    is preferred for soybean. See DECISIONS.md."""
    if empirical is not None:
        return empirical
    return (alpha_pg * rho_hexL) / (alpha_ps * rho_ps)


def ypg2_equilibrium(rho_hexV: float, alpha_ps: float, rho_ps: float) -> float:
    """Ypg2,eq (eq. 6) — dry-basis gas-phase hexane content when the pores hold
    only saturated hexane VAPOR."""
    return rho_hexV * (1.0 - alpha_ps) / (rho_ps * alpha_ps)


def x2_so_and_slope(
    a_h: float,
    T: float,
    X3: float,
    gab: GabParams,
    oil: OilIsotherm,
    h: float = 1.0e-5,
) -> tuple[float, float]:
    """X2,so(a_h) = W2(a_h,T) + X3*qo(a_h) (eq. A.26) and its slope dX2,so/da_h
    (the DCZ particle scale's `Ca`, eq. A.28, treats the local pore-gas mass
    fraction `wpg2` itself as the isotherm's activity variable `a_h` -- the
    same convention `x2_equilibrium`/`x2_critical` already use at the fixed
    point `a_h=1`, generalized here to an arbitrary local value).

    The slope is a centered finite difference on `gab_hexane_content`/
    `oil_hexane_content` (both already smooth, closed-form isotherms) rather
    than a hand-derived analytical derivative -- keeps this module's isotherm
    functions as the single source of truth instead of duplicating their
    algebra a second time as derivatives.
    """

    def x2_so(a: float) -> float:
        return gab_hexane_content(a, T, gab) + X3 * oil_hexane_content(a, oil)

    value = x2_so(a_h)
    step = min(h, a_h, 1.0 - a_h)
    if step <= 0.0:
        # a_h sits exactly on a boundary (0 or 1): fall back to a one-sided
        # difference of a tiny step into the valid interior.
        step = h
        if a_h <= 0.0:
            slope = (x2_so(a_h + step) - value) / step
        else:
            slope = (value - x2_so(a_h - step)) / step
        return value, slope
    slope = (x2_so(a_h + step) - x2_so(a_h - step)) / (2.0 * step)
    return value, slope


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


def mixture_cp_per_kg_dry_solid(
    X1: float,
    X2: float,
    X3: float,
    cp_water_liquid: float,
    cp_hexane_liquid: float,
    cp_oil: float,
    cp_solid: float,
) -> float:
    """Effective heat capacity of the wet solid stream, J/(kg dry solid . K)
    -- `cp_l` (eq. B.5) applied to the solid's own composition (X1/X2/X3 are
    kg-per-kg-dry-solid ratios, converted to the mass fractions `cp_l` itself
    expects). Promoted from `zones/phz.py`'s own private
    `_mixture_cp_per_kg_dry_solid` (that module's only prior caller, which now
    delegates here) so `core/balance.py`'s independent solid-side energy
    checks can share the exact same, already-tested constitutive formula
    without duplicating it -- a balance CHECK duplicating physics ad hoc would
    itself be a maintenance/drift risk, whereas this is pure composition
    -weighting, not a suspect balance term."""
    m_total = 1.0 + X1 + X2 + X3
    w_water, w_hexane, w_oil, w_ds = X1 / m_total, X2 / m_total, X3 / m_total, 1.0 / m_total
    cp_per_kg_wet = cp_l(
        w_water, w_hexane, w_oil, w_ds, (cp_water_liquid, cp_hexane_liquid, cp_oil, cp_solid)
    )
    return cp_per_kg_wet * m_total  # J/(kg WET . K) -> J/(kg DRY solid . K)


def cp_vip(w_water_vapor: float, w_hexane_vapor: float, cps: tuple[float, float]) -> float:
    """CPVip (eq. B.6) — intra-particle vapor specific heat, mass-fraction
    -weighted sum over the 2 vapor components (water, hexane), matching
    `cps = (cp_water_vapor, cp_hexane_vapor)`."""
    weights = (w_water_vapor, w_hexane_vapor)
    return sum(w * cp for w, cp in zip(weights, cps))


def nu_from_reynolds(Re: float, Pr: float) -> float:
    """Nu = 2.0 + 0.6 * Re^0.5 * Pr^(1/3) -- the canonical Ranz-Marshall
    single-sphere correlation, which Faner, Perez & Crapiste (2019) use for
    oilseed-meal desolventizing (their eq. 11). Replaces the earlier
    `0.6949*Re^0.579*Pr^(1/3)` form (Coletto's eq. B.7, cited to Faner's
    unrecoverable 2008 thesis): the `+2.0` conduction floor is the physically
    correct low-Re limit for a sphere. Verified NOT to move the tuning
    (DT exit ~977->982 ppm, DCZ temperature band ~unchanged) -- the sweep-arm
    agitation enhancement (`bed_transport_coefficients`) dominates the base
    correlation at these conditions, so this is a fidelity refinement, not a
    re-tune. See DECISIONS.md."""
    return 2.0 + 0.6 * Re**0.5 * Pr ** (1.0 / 3.0)


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


def water_activity(
    Y_V2: float, T: float, antoine_water: AntoineParams, P: float = ATM_PRESSURE_PA
) -> float:
    """a_w = y_water*P / P_water,sat(T) -- water's own partial pressure
    (Raoult's law, same convention as `dew_point_temperature`) relative to
    its saturation pressure at the LOCAL temperature. Deliberately NOT
    clamped to [0,1]: `a_w >= 1` is mathematically the same condition as
    `T <= dew_point_temperature(Y_V2, ...)` (both say "water can't stay
    vapor here") -- callers route that case to condensation, not this
    isotherm (see `luikov_equilibrium_moisture`'s own docstring)."""
    return _y_water_mole_fraction(Y_V2) * P / antoine_pressure_pa(T, antoine_water)


@dataclass(frozen=True)
class LuikovParams:
    """Modified LUIKOV (1978) desorption isotherm coefficients -- Gianini,
    Luz, Sousa, Jorge & Paraíso (2006), *Ciênc. Tecnol. Aliment.* 26(2):
    408-413, Table 7, fit to soybean meal sampled DIRECTLY from a
    desolventizer/toaster's own outlet (not a generic food-science
    isotherm) across a combined 15-70 °C dataset -- temperature-INDEPENDENT
    by design (the paper's own finding: T has no significant effect on
    equilibrium moisture in that range, confirmed both graphically and
    statistically, Tables 5 vs. 7)."""

    A1: float
    A2: float


LUIKOV_MAX_VALIDATED_UR = 0.799
"""Gianini et al.'s own highest tested water activity (their KCl saturated
-salt-solution data point) -- callers must clamp `a_w` here before calling
`luikov_equilibrium_moisture` (see that function's own docstring): the fitted
curve climbs steeply toward its asymptote `A1` as `a_w -> 1` with zero
supporting data past this point (confirmed: evaluating it unclamped where the
local vapor phase sits close to saturation gives `Xe > 0.5`, a pure
extrapolation artifact, not a real equilibrium). Shared here (not left as a
private per-caller constant) because more than one caller now needs the exact
same bound (`core/zones/dcz.py`'s DCZ moisture balance, `core/dc.py`'s
dryer/cooler air-contact isotherm)."""


def luikov_equilibrium_moisture(a_w: float, p: LuikovParams) -> float:
    """Xe(a_w) = A1 / (1 + A2*ln(1/a_w)) -- equilibrium solid moisture (kg
    water/kg dry solid) at water activity `a_w`. Valid domain is (0, 1),
    matching the cited paper's own tested range (its own UR never exceeded
    ~0.8) -- callers must keep `a_w` there themselves (e.g. `min(a_w, 1.0 -
    eps)`); `a_w >= 1` is a genuinely different regime (condensation, not
    sorption) with its own separate mechanism, not this isotherm extrapolated
    past its own domain.

    EXTRAPOLATION CAVEAT, stated not hidden: the cited paper's own
    temperature range (15-70 °C / 288-343 K) sits below this project's own
    DT-internal operating temperatures (DCZ currently reaches ~380 K+). Its
    finding that temperature barely matters is reassuring but doesn't cover
    that gap -- applying it there is a documented assumption, not a
    validated one.
    """
    return p.A1 / (1.0 + p.A2 * math.log(1.0 / a_w))


# ---------------------------------------------------------------------------
# Soybean-meal air-drying correlations (Luz et al.) -- used by the DC
# (dryer/cooler) stage, `core/dc.py`. NOT for the DT-internal DCZ zone, which
# keeps the Gianini/Luikov desorption isotherm above: these are purpose-built
# for the falling-rate AIR-drying regime (25-100 C, ambient/heated air over a
# hygroscopic bed), the exact regime the DT-internal (superheated-vapor,
# hexane-dominated) sorption model does NOT cover.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LuzDryingParams:
    """Soybean-meal air-drying correlations from Luz, dos Santos Conceição,
    de Matos Jorge, Paraíso & Andrade (2010) *Food Bioprod. Process.* 88:
    90-98 ("Dynamic modeling and control of soybean meal drying in a direct
    rotary dryer"), themselves citing Luz et al. (2006a/c).

    MASS-TRANSFER COEFFICIENT K [1/s] -- Luz eq. (4), the FRACTIONAL-moisture
    form (X_s in kg water/kg dry solid, not %):

        K = (k_a2*T_a + k_b2)*X_s^2 + (k_a1*T_a + k_b1)*X_s + k_c

    Both Luz and Silva et al. (2012) establish soybean-meal drying is
    ENTIRELY falling-rate (internal-diffusion-controlled) -- there is no
    constant-rate period -- so this rate, NOT the air's saturation capacity,
    sets how fast moisture leaves. The temperature terms are near-negligible
    next to `k_c` (K ~ 8.44e-3 /s over the whole band), so evaluating `T_a`
    in K here rather than Luz's own °C shifts K by <0.1% -- immaterial, and
    kept in K for SI consistency with the rest of `core/`.

    EQUILIBRIUM MOISTURE X_e [kg water/kg dry solid] -- Luz eq. (5), a
    modified-Halsey isotherm (Luz et al. 2006a):

        X_e = xe_num / (1 + xe_coef * T_s * ln(1/ur))

    with `T_s` the SOLID temperature (K) and `ur` the local air relative
    humidity (0-1). Unlike the temperature-INDEPENDENT Gianini/Luikov
    isotherm (`LuikovParams`, fit to desolventizer-outlet meal at 15-70 C),
    this one is explicitly temperature-dependent and was regressed against
    the drying process itself -- the appropriate `X_e` for the DC regime."""

    k_a2: float  # 1/(s.K), coeff of T_a in the X_s^2 term        (Luz: -0.33e-11 in 1/(s.°C))
    k_b2: float  # 1/s, constant of the X_s^2 term                (Luz: 4.60e-9)
    k_a1: float  # 1/(s.K), coeff of T_a in the X_s term          (Luz: 7e-8 in 1/(s.°C))
    k_b1: float  # 1/s, constant of the X_s term                  (Luz: 1.42e-5)
    k_c: float  # 1/s, constant term (dominant)                   (Luz: 8.44e-3)
    xe_num: float  # kg/kg dry solid, isotherm numerator          (Luz: 0.834)
    xe_coef: float  # 1/K, isotherm temperature/activity factor   (Luz: 0.036)


def luz_mass_transfer_coefficient(T_a: float, X_s: float, p: LuzDryingParams) -> float:
    """K(T_a, X_s) [1/s] -- Luz eq. (4). `T_a` air temperature (K), `X_s`
    solid moisture (kg water/kg dry solid). Clamped at `X_s >= 0`; the
    quadratic is monotone-safe over the physical moisture range."""
    x = max(X_s, 0.0)
    return (p.k_a2 * T_a + p.k_b2) * x * x + (p.k_a1 * T_a + p.k_b1) * x + p.k_c


def luz_equilibrium_moisture(T_s: float, ur: float, p: LuzDryingParams) -> float:
    """X_e(T_s, ur) [kg water/kg dry solid] -- Luz eq. (5). `T_s` solid
    temperature (K), `ur` local air relative humidity. `ur` is clamped to
    (0, 1) open interval: `ur -> 1` gives the isotherm's own moisture ceiling
    (`xe_num`), `ur -> 0` gives bone-dry equilibrium, both finite."""
    ur_c = min(max(ur, 1.0e-6), 1.0 - 1.0e-6)
    return p.xe_num / (1.0 + p.xe_coef * T_s * math.log(1.0 / ur_c))


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
