"""Optional Numba backend for complete DCZ particle cascades."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from dtdc_simulator.core.zones.particle import ParticleConstants, ParticleState, ShellGeometry

try:
    from numba import njit
except ImportError:  # pragma: no cover - exercised in environments without the optional extra
    njit = None

NUMBA_AVAILABLE = njit is not None
JIT_DISABLED = os.environ.get("DTDC_DISABLE_JIT", "").lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class CascadeInvariants:
    initial_w: np.ndarray
    initial_T: np.ndarray
    volumes: np.ndarray
    face_areas: np.ndarray


def build_invariants(
    initial_particle: "ParticleState",
    geometry: "ShellGeometry",
) -> CascadeInvariants:
    """Materialize immutable cascade arrays once for a complete DCZ solve."""
    arrays = (
        np.asarray(initial_particle.wpg2, dtype=np.float64),
        np.asarray(initial_particle.Tp, dtype=np.float64),
        np.asarray(geometry.volumes, dtype=np.float64),
        np.asarray(geometry.face_areas, dtype=np.float64),
    )
    for array in arrays:
        array.setflags(write=False)
    return CascadeInvariants(*arrays)


if njit is not None:

    @njit(cache=True)
    def _energy_cascade_kernel(
        initial_T: np.ndarray,
        particle_w: np.ndarray,
        previous_rates: np.ndarray,
        vapor_T: np.ndarray,
        axial_sources: np.ndarray,
        moisture: np.ndarray,
        volumes: np.ndarray,
        face_areas: np.ndarray,
        dr: float,
        dt: float,
        hQ: float,
        alpha_pg: float,
        rho_pg: float,
        cp_pg: float,
        alpha_ps: float,
        rho_ps: float,
        cp_ps: float,
        cp_water_liquid: float,
        k_pg: float,
        k_ps: float,
        gab_Xm: float,
        gab_C0: float,
        gab_dHC_R: float,
        gab_K0: float,
        gab_dHK_R: float,
        oil_A0: float,
        oil_B: float,
        dH_vap_hexane: float,
        sorption_C0: float,
        sorption_C1: float,
        minimum_T: float,
        maximum_T: float,
    ) -> np.ndarray:
        nz, layers = particle_w.shape
        output_T = np.empty((nz, layers), dtype=np.float64)
        k_mix = alpha_pg * k_pg + alpha_ps * k_ps

        for j in range(nz):
            sub = np.zeros(layers, dtype=np.float64)
            diagonal = np.empty(layers, dtype=np.float64)
            sup = np.zeros(layers, dtype=np.float64)
            rhs = np.empty(layers, dtype=np.float64)
            Cv = (
                alpha_pg * rho_pg * cp_pg
                + alpha_ps * rho_ps * cp_ps
                + alpha_ps * rho_ps * moisture[j] * cp_water_liquid
            )

            for i in range(layers):
                old_T = initial_T[i] if j == 0 else output_T[j - 1, i]
                activity = particle_w[j, i]
                C = gab_C0 * np.exp(gab_dHC_R / old_T)
                K = gab_K0 * np.exp(gab_dHK_R / old_T)
                clamped_activity = activity
                if K > 0.0 and K * clamped_activity > 0.999:
                    clamped_activity = 0.999 / K
                N = gab_Xm * C * K * clamped_activity
                d1 = 1.0 - K * clamped_activity
                d2 = 1.0 - K * clamped_activity + C * K * clamped_activity
                D = d1 * d2
                N_prime = gab_Xm * C * K
                D_prime = -K * d2 + d1 * (-K + C * K)
                W2 = N / D
                dW2 = (N_prime * D - N * D_prime) / (D * D)
                dqo = (
                    oil_A0 * oil_B * activity ** (oil_B - 1.0)
                    if activity > 0.0
                    else 0.0
                )
                W2_floored = max(W2, 0.02 * gab_Xm)
                dH_s = dH_vap_hexane + sorption_C0 * W2_floored**sorption_C1
                rate = previous_rates[j, i]
                sorption_source = alpha_ps * rho_ps * (
                    dW2 * rate * dH_s + dqo * rate * dH_vap_hexane
                )
                source = sorption_source + axial_sources[j]

                volume = volumes[i]
                diag_i = Cv * volume / dt
                rhs_i = diag_i * old_T + source * volume
                if i > 0:
                    coeff_in = k_mix * face_areas[i - 1] / dr
                    sub[i] = -coeff_in
                    diag_i += coeff_in
                if i < layers - 1:
                    coeff_out = k_mix * face_areas[i] / dr
                    sup[i] = -coeff_out
                    diag_i += coeff_out
                else:
                    coeff_surface = hQ * face_areas[layers - 1]
                    diag_i += coeff_surface
                    rhs_i += coeff_surface * vapor_T[j]
                diagonal[i] = diag_i
                rhs[i] = rhs_i

            cp = np.zeros(layers, dtype=np.float64)
            dp = np.empty(layers, dtype=np.float64)
            beta = diagonal[0]
            cp[0] = sup[0] / beta
            dp[0] = rhs[0] / beta
            for i in range(1, layers):
                beta = diagonal[i] - sub[i] * cp[i - 1]
                if i < layers - 1:
                    cp[i] = sup[i] / beta
                dp[i] = (rhs[i] - sub[i] * dp[i - 1]) / beta
            output_T[j, layers - 1] = dp[layers - 1]
            for i in range(layers - 2, -1, -1):
                output_T[j, i] = dp[i] - cp[i] * output_T[j, i + 1]
            for i in range(layers):
                output_T[j, i] = min(max(output_T[j, i], minimum_T), maximum_T)
        return output_T

    @njit(cache=True)
    def _mass_cascade_kernel(
        initial_w: np.ndarray,
        particle_T: np.ndarray,
        vapor_w: np.ndarray,
        volumes: np.ndarray,
        face_areas: np.ndarray,
        dr: float,
        dt: float,
        hM: float,
        rho_V: float,
        D_eff: float,
        alpha_pg: float,
        rho_pg: float,
        alpha_ps: float,
        rho_ps: float,
        X3: float,
        gab_Xm: float,
        gab_C0: float,
        gab_dHC_R: float,
        gab_K0: float,
        gab_dHK_R: float,
        oil_A0: float,
        oil_B: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        nz, layers = particle_T.shape
        output_w = np.empty((nz, layers), dtype=np.float64)
        rates = np.empty((nz, layers), dtype=np.float64)
        x2_bulk = np.empty(nz, dtype=np.float64)
        total_volume = 0.0
        for i in range(layers):
            total_volume += volumes[i]
        const_diff = alpha_pg * rho_pg * D_eff

        for j in range(nz):
            sub = np.zeros(layers, dtype=np.float64)
            diagonal = np.empty(layers, dtype=np.float64)
            sup = np.zeros(layers, dtype=np.float64)
            rhs = np.empty(layers, dtype=np.float64)

            for i in range(layers):
                old_w = initial_w[i] if j == 0 else output_w[j - 1, i]
                temperature = particle_T[j, i]
                C = gab_C0 * np.exp(gab_dHC_R / temperature)
                K = gab_K0 * np.exp(gab_dHK_R / temperature)
                activity = old_w
                if K > 0.0 and K * activity > 0.999:
                    activity = 0.999 / K
                x = K * activity
                denominator = (1.0 - x) * (1.0 - x + C * x)
                gab_slope = (
                    gab_Xm
                    * C
                    * K
                    * (1.0 + (C - 1.0) * x * x)
                    / (denominator * denominator)
                )
                oil_slope = (
                    oil_A0 * oil_B * old_w ** (oil_B - 1.0) if old_w > 0.0 else 0.0
                )
                slope = gab_slope + X3 * oil_slope
                accumulation = alpha_pg * rho_pg + alpha_ps * rho_ps * slope
                accumulation_volume_rate = volumes[i] * accumulation / dt
                diag_i = accumulation_volume_rate
                rhs[i] = accumulation_volume_rate * old_w
                if i > 0:
                    coeff_in = const_diff * face_areas[i - 1] / dr
                    sub[i] = -coeff_in
                    diag_i += coeff_in
                if i < layers - 1:
                    coeff_out = const_diff * face_areas[i] / dr
                    sup[i] = -coeff_out
                    diag_i += coeff_out
                else:
                    coeff_surface = hM * rho_V * face_areas[layers - 1]
                    diag_i += coeff_surface
                    rhs[i] += coeff_surface * vapor_w[j]
                diagonal[i] = diag_i

            # Thomas solve, identical operation order to the Python reference.
            cp = np.zeros(layers, dtype=np.float64)
            dp = np.empty(layers, dtype=np.float64)
            beta = diagonal[0]
            cp[0] = sup[0] / beta
            dp[0] = rhs[0] / beta
            for i in range(1, layers):
                beta = diagonal[i] - sub[i] * cp[i - 1]
                if i < layers - 1:
                    cp[i] = sup[i] / beta
                dp[i] = (rhs[i] - sub[i] * dp[i - 1]) / beta
            output_w[j, layers - 1] = dp[layers - 1]
            for i in range(layers - 2, -1, -1):
                output_w[j, i] = dp[i] - cp[i] * output_w[j, i + 1]

            bulk_sum = 0.0
            for i in range(layers):
                old_w = initial_w[i] if j == 0 else output_w[j - 1, i]
                value = output_w[j, i]
                if value < 0.0:
                    value = 0.0
                elif value > 1.0:
                    value = 1.0
                output_w[j, i] = value
                rates[j, i] = (value - old_w) / dt

                temperature = particle_T[j, i]
                C = gab_C0 * np.exp(gab_dHC_R / temperature)
                K = gab_K0 * np.exp(gab_dHK_R / temperature)
                activity = value
                if K > 0.0 and K * activity > 0.999:
                    activity = 0.999 / K
                gab_value = (
                    gab_Xm
                    * C
                    * K
                    * activity
                    / (
                        (1.0 - K * activity)
                        * (1.0 - K * activity + C * K * activity)
                    )
                )
                oil_value = oil_A0 * value**oil_B
                bulk_sum += (gab_value + X3 * oil_value) * volumes[i]
            x2_bulk[j] = bulk_sum / total_volume
        return output_w, rates, x2_bulk


def energy_cascade(
    initial_particle: "ParticleState",
    particles: list["ParticleState"],
    previous_rates: list[tuple[float, ...]],
    vapor_T: list[float],
    axial_sources: tuple[float, ...],
    moisture: list[float],
    hQ: float,
    dt: float,
    geometry: "ShellGeometry",
    constants: "ParticleConstants",
    minimum_T: float,
    maximum_T: float,
    invariants: CascadeInvariants | None = None,
) -> np.ndarray | None:
    """Return compiled particle temperatures, or ``None`` when unavailable."""
    if not NUMBA_AVAILABLE or JIT_DISABLED:
        return None
    cached = invariants or build_invariants(initial_particle, geometry)
    return _energy_cascade_kernel(
        cached.initial_T,
        np.asarray([particle.wpg2 for particle in particles], dtype=np.float64),
        np.asarray(previous_rates, dtype=np.float64),
        np.asarray(vapor_T, dtype=np.float64),
        np.asarray(axial_sources, dtype=np.float64),
        np.asarray(moisture, dtype=np.float64),
        cached.volumes,
        cached.face_areas,
        geometry.dr,
        dt,
        hQ,
        constants.alpha_pg,
        constants.rho_pg,
        constants.cp_pg,
        constants.alpha_ps,
        constants.rho_ps,
        constants.cp_ps,
        constants.cp_water_liquid,
        constants.k_pg,
        constants.k_ps,
        constants.gab.Xm,
        constants.gab.C0,
        constants.gab.dHC_R,
        constants.gab.K0,
        constants.gab.dHK_R,
        constants.oil.A0,
        constants.oil.B,
        constants.dH_vap_hexane,
        constants.sorption_C0,
        constants.sorption_C1,
        minimum_T,
        maximum_T,
    )


def mass_cascade(
    initial_particle: "ParticleState",
    particles: list["ParticleState"],
    vapor_wV2: list[float],
    hM: float,
    rho_V: float,
    dt: float,
    geometry: "ShellGeometry",
    constants: "ParticleConstants",
    invariants: CascadeInvariants | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Return compiled cascade arrays, or ``None`` when the backend is unavailable."""
    if not NUMBA_AVAILABLE or JIT_DISABLED:
        return None
    cached = invariants or build_invariants(initial_particle, geometry)
    particle_T = np.asarray([particle.Tp for particle in particles], dtype=np.float64)
    return _mass_cascade_kernel(
        cached.initial_w,
        particle_T,
        np.asarray(vapor_wV2, dtype=np.float64),
        cached.volumes,
        cached.face_areas,
        geometry.dr,
        dt,
        hM,
        rho_V,
        constants.D_eff,
        constants.alpha_pg,
        constants.rho_pg,
        constants.alpha_ps,
        constants.rho_ps,
        constants.X3,
        constants.gab.Xm,
        constants.gab.C0,
        constants.gab.dHC_R,
        constants.gab.K0,
        constants.gab.dHK_R,
        constants.oil.A0,
        constants.oil.B,
    )


def warm_up(
    initial_particle: "ParticleState",
    geometry: "ShellGeometry",
    constants: "ParticleConstants",
) -> bool:
    """Compile the backend once during initialization; return whether it is active."""
    result = mass_cascade(
        initial_particle,
        [initial_particle],
        [0.5],
        1.0e-3,
        1.0,
        1.0,
        geometry,
        constants,
    )
    return result is not None
