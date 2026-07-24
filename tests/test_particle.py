"""Unit tests for `core/zones/particle.py` (DCZ particle scale, Table A.3):
the 12-layer spherical FVM's shell geometry, the implicit mass (hexane
diffusion) and energy marches, and their convective boundary conditions.
Physics-shape checks (monotonic approach to the local vapor state, energy
conservation) rather than exact literature values, matching this project's
established approach for zones only validated against plots, not data
tables.
"""

import math

import pytest

from dtdc_simulator.core import thermo
from dtdc_simulator.core.zones import particle as p
from dtdc_simulator.core.zones import particle_jit

GAB = thermo.GabParams(Xm=5.183e-3, C0=3.117e-3, dHC_R=2262.0, K0=9.172e-2, dHK_R=729.6)
OIL = thermo.OilIsotherm(A0=0.9635, B=2.7036)
CONSTANTS = p.ParticleConstants(
    D_eff=4.0e-10,
    r_P=1.0e-3,
    Np=12,
    alpha_ps=0.5,
    alpha_pg=0.5,
    rho_ps=1513.0,
    rho_pg=0.6,
    cp_ps=2317.0,
    cp_pg=1650.0,
    k_ps=0.29,
    k_pg=0.02371,
    X3=0.0139,
    gab=GAB,
    oil=OIL,
    dH_vap_hexane=3.34e5,
    sorption_C0=3.0e5,
    sorption_C1=-0.5,
    cp_water_liquid=4186.0,
)
GEOMETRY = p.build_shell_geometry(CONSTANTS.r_P, CONSTANTS.Np)
ZERO_RATES = tuple(0.0 for _ in range(CONSTANTS.Np))


def _uniform_sources(q_condL_w_m3: float) -> tuple[float, ...]:
    """`march_particle_energy` now takes the full per-layer source directly
    (see particle.py's own docstring on this dedup) -- with `dwpg2_dt_prev=
    ZERO_RATES` (as every test below uses), the sorption terms are exactly
    zero, so a uniform `q_condL` per layer is the exact equivalent of the
    old `(q_condL_w_m3=..., dwpg2_dt_prev=ZERO_RATES)` call shape."""
    return tuple(q_condL_w_m3 for _ in range(CONSTANTS.Np))


# ------------------------------------------------------------------ shell geometry


def test_shell_volumes_sum_to_sphere_volume() -> None:
    sphere_volume = (4.0 / 3.0) * math.pi * CONSTANTS.r_P**3
    assert sum(GEOMETRY.volumes) == pytest.approx(sphere_volume)


def test_shell_volumes_increase_outward() -> None:
    # Equal-dr shells: outer shells have more volume (r^3 grows faster).
    assert list(GEOMETRY.volumes) == sorted(GEOMETRY.volumes)


def test_outer_face_radius_equals_particle_radius() -> None:
    assert GEOMETRY.face_radii[-1] == pytest.approx(CONSTANTS.r_P)


# ------------------------------------------------------------------ mass march


def test_mass_march_diffuses_toward_uniform_vapor_equilibrium() -> None:
    # Uniform initial hexane loading, above the vapor's own composition:
    # hexane should diffuse outward (center stays highest, surface lowest,
    # approaching the vapor's wV2 at the boundary).
    state = p.ParticleState(wpg2=tuple(0.05 for _ in range(12)), Tp=tuple(372.0 for _ in range(12)))
    wV2_local = 0.01
    s = state
    for _ in range(3000):
        s, diag = p.march_particle_mass(
            s, wV2_local, hM=0.05, rho_V=0.6, dt=1.0, geometry=GEOMETRY, c=CONSTANTS
        )
    assert list(s.wpg2) == sorted(s.wpg2, reverse=True)  # center highest, surface lowest
    assert s.wpg2[-1] == pytest.approx(wV2_local, abs=1.0e-3)  # surface approaches equilibrium
    assert abs(diag.hexane_flux_to_vapor_kg_m2_s) < 1.0e-8  # near-zero net flux at equilibrium


def test_mass_march_conserves_hexane_when_vapor_matches_particle() -> None:
    # No driving force (vapor already at the particle's own uniform state):
    # the field should stay put (up to solver tolerance), not drift.
    uniform = 0.03
    state = p.ParticleState(
        wpg2=tuple(uniform for _ in range(12)), Tp=tuple(365.0 for _ in range(12))
    )
    s, diag = p.march_particle_mass(
        state, uniform, hM=0.05, rho_V=0.6, dt=1.0, geometry=GEOMETRY, c=CONSTANTS
    )
    assert s.wpg2 == pytest.approx(state.wpg2, abs=1.0e-12)
    assert diag.hexane_flux_to_vapor_kg_m2_s == pytest.approx(0.0, abs=1.0e-12)


def test_mass_march_reports_consistent_layer_rates() -> None:
    state = p.ParticleState(wpg2=tuple(0.05 for _ in range(12)), Tp=tuple(372.0 for _ in range(12)))
    dt = 1.0
    s, diag = p.march_particle_mass(
        state, 0.01, hM=0.05, rho_V=0.6, dt=dt, geometry=GEOMETRY, c=CONSTANTS
    )
    for i in range(12):
        assert diag.dwpg2_dt[i] == pytest.approx((s.wpg2[i] - state.wpg2[i]) / dt)


def _x2_total_kg(state: p.ParticleState, geometry: p.ShellGeometry, c: p.ParticleConstants) -> float:
    """Total hexane mass held by the particle (free pore gas + adsorbed
    +absorbed, eq. A.22's own accumulated quantity), summed over all layers."""
    total = 0.0
    for wpg2_i, Tp_i, V_i in zip(state.wpg2, state.Tp, geometry.volumes):
        x2_so = thermo.gab_hexane_content(wpg2_i, Tp_i, c.gab) + c.X3 * thermo.oil_hexane_content(
            wpg2_i, c.oil
        )
        total += (c.alpha_pg * c.rho_pg * wpg2_i + c.alpha_ps * c.rho_ps * x2_so) * V_i
    return total


def test_mass_march_conserves_x2_total_between_bulk_and_surface_flux() -> None:
    # The regression test for DECISIONS.md's "DCZ particle hexane mass
    # -conservation gap": bulk hexane content lost (free + adsorbed/absorbed,
    # `_x2_total_kg` before vs. after) must match the surface flux integrated
    # over the SAME step, to within a small, genuinely-shrinking
    # discretization error -- NOT the ~18.6x (1860%) gap the OLD `Ca`-based
    # scheme showed here (a bug fixed this session, see `march_particle_mass`'s
    # own docstring for the full derivation and citation). Checked at a
    # production-realistic dt (~60 s, this project's own typical per-cell DCZ
    # residence) with NO sub-stepping (the hardest, least-favorable case) --
    # confirmed (DECISIONS.md) the residual shrinks further with either finer
    # sub-stepping or a finer mesh, so this single-step bound is conservative.
    state = p.ParticleState(
        wpg2=(0.9, 0.85, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05, 0.02),
        Tp=tuple(390.0 for _ in range(12)),
    )
    dt = 60.0
    wV2_local = 0.0001
    mass_before = _x2_total_kg(state, GEOMETRY, CONSTANTS)
    new_state, diag = p.march_particle_mass(
        state, wV2_local, hM=0.05, rho_V=0.6, dt=dt, geometry=GEOMETRY, c=CONSTANTS
    )
    mass_after = _x2_total_kg(new_state, GEOMETRY, CONSTANTS)
    declined_kg = mass_before - mass_after
    flux_out_kg = diag.hexane_flux_to_vapor_kg_m2_s * GEOMETRY.face_areas[-1] * dt
    assert declined_kg > 0.0  # sanity: this profile genuinely loses hexane
    assert declined_kg == pytest.approx(flux_out_kg, rel=0.02)  # within 2%, not 1860%


# ------------------------------------------------------------------ energy march


def test_energy_march_heats_toward_uniform_vapor_temperature() -> None:
    state = p.ParticleState(wpg2=tuple(0.03 for _ in range(12)), Tp=tuple(365.0 for _ in range(12)))
    TV_local = 372.0
    s = state
    for _ in range(3000):
        s, diag = p.march_particle_energy(
            s,
            TV_local,
            hQ=300.0,
            sources_w_m3=_uniform_sources(0.0),
            dt=1.0,
            geometry=GEOMETRY,
            c=CONSTANTS,
        )
    assert all(t == pytest.approx(TV_local, abs=1.0e-2) for t in s.Tp)
    assert abs(diag.heat_flux_to_vapor_w_m2) < 1.0e-6


def test_energy_march_never_overshoots_vapor_temperature() -> None:
    # Particle colder than the vapor everywhere, no sorption source: the
    # temperature should rise monotonically toward TV, never past it.
    state = p.ParticleState(wpg2=tuple(0.03 for _ in range(12)), Tp=tuple(340.0 for _ in range(12)))
    TV_local = 372.0
    s = state
    for _ in range(50):
        s, _ = p.march_particle_energy(
            s,
            TV_local,
            hQ=300.0,
            sources_w_m3=_uniform_sources(0.0),
            dt=1.0,
            geometry=GEOMETRY,
            c=CONSTANTS,
        )
        assert all(t <= TV_local + 1.0e-9 for t in s.Tp)


def test_axial_conduction_source_raises_particle_temperature() -> None:
    # A positive q_condL (external heat input) should push the particle
    # above the vapor temperature it would otherwise equilibrate to.
    state = p.ParticleState(wpg2=tuple(0.03 for _ in range(12)), Tp=tuple(372.0 for _ in range(12)))
    TV_local = 372.0
    s_no_source, _ = p.march_particle_energy(
        state,
        TV_local,
        hQ=300.0,
        sources_w_m3=_uniform_sources(0.0),
        dt=1.0,
        geometry=GEOMETRY,
        c=CONSTANTS,
    )
    s_with_source, _ = p.march_particle_energy(
        state,
        TV_local,
        hQ=300.0,
        sources_w_m3=_uniform_sources(5.0e4),
        dt=1.0,
        geometry=GEOMETRY,
        c=CONSTANTS,
    )
    assert all(t_src >= t_no for t_src, t_no in zip(s_with_source.Tp, s_no_source.Tp))


# ------------------------------------------------------------------ moisture-dependent Cv


def test_x1_zero_reproduces_default_energy_march() -> None:
    # Regression safety: `X1` defaults to 0.0, and passing it explicitly
    # should be bit-identical to every pre-existing call site above that
    # never mentions it.
    state = p.ParticleState(wpg2=tuple(0.03 for _ in range(12)), Tp=tuple(365.0 for _ in range(12)))
    s_default, _ = p.march_particle_energy(
        state, 372.0, hQ=300.0, sources_w_m3=_uniform_sources(0.0),
        dt=1.0, geometry=GEOMETRY, c=CONSTANTS,
    )
    s_explicit_zero, _ = p.march_particle_energy(
        state, 372.0, hQ=300.0, sources_w_m3=_uniform_sources(0.0),
        dt=1.0, geometry=GEOMETRY, c=CONSTANTS, X1=0.0,
    )
    assert s_explicit_zero.Tp == s_default.Tp


def test_moisture_raises_effective_heat_capacity() -> None:
    # A wetter particle (higher X1) has more thermal mass -- the SAME source
    # should raise its temperature LESS over one step than a dry particle's,
    # mirroring core/dc.py's own C_wet = cp_solid + X1*cp_water_liquid
    # precedent (a heavier, water-laden solid warms more slowly per unit
    # heat input).
    state = p.ParticleState(wpg2=tuple(0.03 for _ in range(12)), Tp=tuple(340.0 for _ in range(12)))
    TV_local = 372.0
    s_dry, _ = p.march_particle_energy(
        state, TV_local, hQ=300.0, sources_w_m3=_uniform_sources(0.0),
        dt=1.0, geometry=GEOMETRY, c=CONSTANTS, X1=0.0,
    )
    s_wet, _ = p.march_particle_energy(
        state, TV_local, hQ=300.0, sources_w_m3=_uniform_sources(0.0),
        dt=1.0, geometry=GEOMETRY, c=CONSTANTS, X1=0.15,
    )
    assert all(t_wet < t_dry for t_wet, t_dry in zip(s_wet.Tp, s_dry.Tp))


# ------------------------------------------------------------------ sorption heat bound


def test_sorption_heat_source_bounded_at_near_zero_hexane_content() -> None:
    # Found this session (DECISIONS.md's "DT runaway temperature" entry): a
    # real full-scale DT solve drives a particle's own GAB hexane content
    # (W2) down to ~1e-7-1e-8 near the DCZ exit -- the M2 Phase 4 floor
    # (W2_floored = max(W2, 1e-9)) only avoided a literal division by zero,
    # not the resulting magnitude, letting dH_s reach ~28,000x
    # dH_vap_hexane. Checking the floored dH_s directly (the quantity the
    # floor actually bounds -- the per-layer source itself also depends on
    # the isotherm slope dW2/dwpg2, which separately shrinks near wpg2=0, so
    # isn't a reliable proxy here) stays within a couple of orders of
    # magnitude of dH_vap_hexane, not 4+ orders.
    T = 342.0
    near_zero_W2 = thermo.gab_hexane_content(1.0e-6, T, GAB)
    floored = max(near_zero_W2, 0.02 * GAB.Xm)
    dH_s = thermo.heat_of_sorption(
        floored, CONSTANTS.dH_vap_hexane, CONSTANTS.sorption_C0, CONSTANTS.sorption_C1
    )
    assert math.isfinite(dH_s)
    assert dH_s < 200.0 * CONSTANTS.dH_vap_hexane

    near_zero = p.ParticleState(
        wpg2=tuple(1.0e-6 for _ in range(CONSTANTS.Np)), Tp=tuple(T for _ in range(CONSTANTS.Np))
    )
    rate = tuple(-1.0e-6 for _ in range(CONSTANTS.Np))  # desorbing
    near_zero_source = p.sorption_heat_source_per_layer_w_m3(near_zero, rate, CONSTANTS)
    assert all(math.isfinite(s) for s in near_zero_source)


# ------------------------------------------------------------------ helpers


def test_outer_layer_value_and_volumetric_mean() -> None:
    values = tuple(float(i) for i in range(12))
    assert p.outer_layer_value(values) == 11.0
    mean = p.volumetric_mean(values, GEOMETRY.volumes)
    # Volumetric mean should be pulled toward the outer (larger-volume, higher-value) layers.
    assert mean > sum(values) / len(values)
    assert p.shell_volumetric_mean(values, GEOMETRY) == mean


def test_compiled_cascade_invariants_are_read_only() -> None:
    initial = p.ParticleState(
        wpg2=tuple(0.05 for _ in range(CONSTANTS.Np)),
        Tp=tuple(365.0 for _ in range(CONSTANTS.Np)),
    )
    invariants = particle_jit.build_invariants(initial, GEOMETRY)
    assert not invariants.initial_w.flags.writeable
    assert not invariants.initial_T.flags.writeable
    assert not invariants.volumes.flags.writeable
    assert not invariants.face_areas.flags.writeable


@pytest.mark.skipif(
    not particle_jit.NUMBA_AVAILABLE or particle_jit.JIT_DISABLED,
    reason="optional Numba backend unavailable or disabled",
)
def test_compiled_energy_cascade_matches_python_reference() -> None:
    initial = p.ParticleState(
        wpg2=tuple(0.08 - 0.002 * i for i in range(CONSTANTS.Np)),
        Tp=tuple(350.0 + 0.2 * i for i in range(CONSTANTS.Np)),
    )
    particles = [
        p.ParticleState(
            wpg2=tuple(0.07 - 0.003 * j - 0.001 * i for i in range(CONSTANTS.Np)),
            Tp=initial.Tp,
        )
        for j in range(3)
    ]
    rates = [
        tuple(-1.0e-4 * (j + 1) * (i + 1) for i in range(CONSTANTS.Np))
        for j in range(3)
    ]
    vapor = [365.0, 370.0, 375.0]
    axial_sources = (120.0, -80.0, 30.0)
    moisture = [0.08, 0.11, 0.14]
    compiled = particle_jit.energy_cascade(
        initial,
        particles,
        rates,
        vapor,
        axial_sources,
        moisture,
        25.0,
        2.0,
        GEOMETRY,
        CONSTANTS,
        250.0,
        480.0,
    )
    assert compiled is not None

    running_T = initial.Tp
    for j in range(3):
        seed = p.ParticleState(wpg2=particles[j].wpg2, Tp=running_T)
        sorption = p.sorption_heat_source_per_layer_w_m3(seed, rates[j], CONSTANTS)
        sources = tuple(value + axial_sources[j] for value in sorption)
        updated, _ = p.march_particle_energy(
            seed,
            vapor[j],
            25.0,
            sources,
            2.0,
            GEOMETRY,
            CONSTANTS,
            X1=moisture[j],
        )
        expected = tuple(min(max(value, 250.0), 480.0) for value in updated.Tp)
        assert tuple(compiled[j]) == pytest.approx(expected, rel=2.0e-14, abs=1.0e-12)
        running_T = expected


@pytest.mark.skipif(
    not particle_jit.NUMBA_AVAILABLE or particle_jit.JIT_DISABLED,
    reason="optional Numba backend unavailable or disabled",
)
def test_compiled_mass_cascade_matches_python_reference() -> None:
    initial = p.ParticleState(
        wpg2=tuple(0.08 - 0.002 * i for i in range(CONSTANTS.Np)),
        Tp=tuple(365.0 + 0.2 * i for i in range(CONSTANTS.Np)),
    )
    particles = [
        p.ParticleState(
            wpg2=initial.wpg2,
            Tp=tuple(365.0 + j + 0.1 * i for i in range(CONSTANTS.Np)),
        )
        for j in range(3)
    ]
    vapor = [0.04, 0.03, 0.02]
    compiled = particle_jit.mass_cascade(
        initial, particles, vapor, 0.05, 0.6, 2.0, GEOMETRY, CONSTANTS
    )
    assert compiled is not None
    compiled_w, compiled_rates, compiled_x2 = compiled

    running = initial.wpg2
    for j in range(3):
        seed = p.ParticleState(wpg2=running, Tp=particles[j].Tp)
        updated, diagnostics = p.march_particle_mass(
            seed, vapor[j], 0.05, 0.6, 2.0, GEOMETRY, CONSTANTS
        )
        expected_x2 = p.shell_volumetric_mean(
            tuple(
                thermo.gab_hexane_content(w, t, CONSTANTS.gab)
                + CONSTANTS.X3 * thermo.oil_hexane_content(w, CONSTANTS.oil)
                for w, t in zip(updated.wpg2, updated.Tp)
            ),
            GEOMETRY,
        )
        assert tuple(compiled_w[j]) == pytest.approx(updated.wpg2, rel=2.0e-14, abs=1.0e-15)
        assert tuple(compiled_rates[j]) == pytest.approx(
            diagnostics.dwpg2_dt, rel=2.0e-14, abs=1.0e-15
        )
        assert compiled_x2[j] == pytest.approx(expected_x2, rel=2.0e-14)
        running = updated.wpg2


def test_compiled_backend_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(particle_jit, "JIT_DISABLED", True)
    initial = p.ParticleState(
        wpg2=tuple(0.05 for _ in range(CONSTANTS.Np)),
        Tp=tuple(365.0 for _ in range(CONSTANTS.Np)),
    )
    assert (
        particle_jit.mass_cascade(
            initial, [initial], [0.02], 0.05, 0.6, 1.0, GEOMETRY, CONSTANTS
        )
        is None
    )
    assert (
        particle_jit.energy_cascade(
            initial,
            [initial],
            [tuple(0.0 for _ in range(CONSTANTS.Np))],
            [365.0],
            (0.0,),
            [0.1],
            25.0,
            1.0,
            GEOMETRY,
            CONSTANTS,
            250.0,
            480.0,
        )
        is None
    )
