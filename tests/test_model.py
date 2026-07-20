import pytest

from dtdc_simulator.config.builder import assemble_model
from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.core.model import Inputs

SCENARIO_PATH = "scenarios/soybean_default.yaml"


@pytest.fixture(scope="module")
def loaded():
    """`assemble_model` now runs one real `solve_dt` call inside `init_state`
    (M3a) -- expensive enough (~10s+) that it must be computed ONCE and
    shared read-only across this file's tests, not per-test. `model`/`x0`
    are never mutated in place by any test below (only `.copy()`'d), so
    sharing is safe."""
    cfg = load_scenario(SCENARIO_PATH)
    return assemble_model(cfg), cfg


def _default_inputs(cfg):
    od = cfg.operating_defaults
    dd = cfg.disturbance_defaults
    return Inputs(
        feed_flow_rate=od.feed_flow_rate,
        feed_temperature=dd.feed_temperature,
        indirect_steam=dict(od.indirect_steam),
        direct_steam=dict(od.direct_steam),
        sweep_arm_speed=dict(od.sweep_arm_speed),
        gate_opening=dict(od.gate_opening),
        heated_air_temp=od.heated_air_temp,
        heated_air_flow=od.heated_air_flow,
        ambient_air_temp=dd.ambient_air_temp,
        ambient_air_flow=od.ambient_air_flow,
        feed_moisture=dd.feed_moisture,
        feed_hexane=dd.feed_hexane,
        ambient_relative_humidity=dd.ambient_relative_humidity,
    )


def _with_fast_resolve(u: Inputs) -> Inputs:
    """`dt_resolve_interval_s` is a HOT `Inputs` field (M3a follow-up "C"),
    not a cold `ModelConstants` one -- setting it near-zero on a specific
    `Inputs` instance is all that's needed to deterministically trigger
    `solve_dt` on a single `step()` call, for tests that need to observe the
    DT-role targets respond to different inputs without paying for a long
    (many-tick) loop to cross the real resolve cadence."""
    u.dt_resolve_interval_s = 1.0e-6
    return u


def test_init_state_matches_seed(loaded) -> None:
    """M3a: init_state() now seeds DT-role stages from a real solve_dt() at
    the operating defaults (BuildSpec §4), not a uniform copy of the feed
    state -- so X2 differs (processed) rather than matching feed_hexane
    everywhere."""
    (model, x0), cfg = loaded
    # The DT genuinely desolventizes: exit hexane content is well below feed,
    # for the DT's own exit tray (dt_target_X2's last entry -- NOT
    # necessarily x0.X2's last entry, since the scenario's own last STAGE is
    # CL1, a downstream COOLER seeded at the raw feed state, not the DT).
    assert x0.dt_target_X2[-1] < cfg.disturbance_defaults.feed_hexane
    assert x0.dt_outer_iterations > 0
    assert x0.dt_target_T.shape == x0.dt_target_X1.shape == x0.dt_target_X2.shape


def test_step_is_deterministic(loaded) -> None:
    # t=0.0 on both calls, matching x0's own dt_last_solve_sim_time (0.0) --
    # this deliberately does NOT cross the resolve cadence (see module
    # docstring pattern throughout this file: t=0.0 held constant is the
    # cheap default; only tests that need a resolve advance t or use
    # _fast_resolve_model).
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    x1, y1 = model.step(x0, u, 0.0, 1.0)
    x2, y2 = model.step(x0, u, 0.0, 1.0)
    assert (x1.T == x2.T).all()
    assert y1.kpi_residual_hexane_ppm == y2.kpi_residual_hexane_ppm


def test_more_steam_raises_dt_target_temperature(loaded) -> None:
    """Direct, cheap test of the DT-role target mechanism itself (one
    solve_dt call per trajectory, not a long tick loop): halved vs doubled
    indirect steam duty should produce a cooler vs hotter converged DT
    profile.

    Asserts on the DT's PEAK tray temperature, not its exit tray: the exit
    (SPARGE/DCZ) tray is pinned near the direct-steam saturation temperature
    (~100 C) by steam condensation equilibrium, so it barely moves with
    indirect steam -- the PREDESOLV/MAIN trays are where indirect (jacket)
    duty actually lands (verified directly: doubling indirect steam takes the
    PREDESOLV trays from ~62-67 C to ~104 C while SP1 stays ~100 C). Using
    `max()` captures the real, physically-meaningful response.

    Not asserting `dt_converged` here: the scenario's own real-time tuning
    (M3a follow-up "A2", `dt_outer_tol=0.05`/`dt_outer_max_iter=20`) is
    deliberately loose enough that the formal convergence flag often reads
    False within so few iterations even though the profile itself is already
    close to its asymptote -- `converged` is a diagnostic for `SolverStress`,
    not a precondition for the profile being directionally meaningful, which
    is what this test actually checks.
    """
    (model, x0), cfg = loaded

    u_cold = _with_fast_resolve(_default_inputs(cfg))
    u_cold.indirect_steam = {k: v * 0.5 for k, v in u_cold.indirect_steam.items()}
    u_hot = _with_fast_resolve(_default_inputs(cfg))
    u_hot.indirect_steam = {k: v * 2.0 for k, v in u_hot.indirect_steam.items()}

    x_cold, _ = model.step(x0, u_cold, 1.0, 1.0)  # t=1.0 >> the tiny resolve interval
    x_hot, _ = model.step(x0, u_hot, 1.0, 1.0)

    assert max(x_hot.dt_target_T) > max(x_cold.dt_target_T)


def test_energy_and_vapor_kpis(loaded) -> None:
    """M4 (GUI redesign): the new process-dashboard KPIs are derived purely
    from the current tick's inputs + last DT axial-profile solve. Check the
    definitional identities hold (total = sum of parts, indirect = input sum)
    and every new field is finite + non-negative."""
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    _, y = model.step(x0, u, 0.0, 1.0)

    assert y.kpi_indirect_heating_kw == pytest.approx(sum(u.indirect_steam.values()) / 1.0e3)
    assert y.kpi_direct_steam_kg_s == pytest.approx(sum(u.direct_steam.values()))
    expected_total = (
        y.kpi_indirect_heating_kw
        + y.kpi_drying_air_heating_kw
        + y.kpi_direct_steam_kg_s * cfg.physical.dH_vap_water / 1.0e3
    )
    assert y.kpi_total_energy_kw == pytest.approx(expected_total)

    # Per-stage solid outflow reported for every stage, non-negative.
    assert set(y.stage_solid_out_kg_s) == set(y.stage_T)
    assert all(v >= 0.0 for v in y.stage_solid_out_kg_s.values())

    # Vapor leaves the DT top (init solve populated the profile), and condenser
    # duty follows from it; exhaust hexane echoes the DRYER air-side value.
    assert y.kpi_outlet_vapor_kg_s > 0.0
    for value in (
        y.kpi_outlet_vapor_hexane_kg_s,
        y.kpi_outlet_vapor_water_kg_s,
        y.kpi_condenser_duty_kw,
        y.kpi_exhaust_hexane_ppm,
    ):
        assert value == value  # not NaN
        assert value >= 0.0


def test_closed_gate_stops_discharge_and_floods(loaded) -> None:
    """A shut rotary valve (gate_opening=0) must genuinely STOP that stage's
    solid discharge (not merely slow it, as the old M/tau residence model did)
    -- so material accumulates and the tray floods past 100%."""
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    first = model.stages[0].id
    u.gate_opening = dict(u.gate_opening)
    u.gate_opening[first] = 0.0

    x, y = x0.copy(), None
    for _ in range(200):
        x, y = model.step(x, u, 0.0, 5.0)

    assert y.stage_solid_out_kg_s[first] == pytest.approx(0.0, abs=1.0e-9)
    # Capacity-capped: a full tray sits AT ~100% (surplus is rejected upstream),
    # it doesn't climb unboundedly past it.
    assert y.stage_level_pct[first] >= 99.5


def test_backpressure_floods_the_tray_above(loaded) -> None:
    """Back-pressure cascade: when a tray floods and can't accept its inflow,
    the surplus is rejected back into the tray ABOVE it, which then floods too
    -- so a shut gate mid-column backs material up toward the feed."""
    (model, x0), cfg = loaded
    ids = [s.id for s in model.stages]
    blocked, upstream = ids[2], ids[1]
    u = _default_inputs(cfg)
    u.gate_opening = dict(u.gate_opening)
    u.gate_opening[blocked] = 0.0

    x, y = x0.copy(), None
    for _ in range(400):
        x, y = model.step(x, u, 0.0, 5.0)

    assert y.stage_solid_out_kg_s[blocked] == pytest.approx(0.0, abs=1.0e-9)
    assert y.stage_level_pct[blocked] >= 99.5
    assert y.stage_level_pct[upstream] >= 99.5  # back-pressure floods the tray above


def test_gate_controls_level_uniformly_across_trays(loaded) -> None:
    """Discharge is driven by bed LEVEL, not absolute holdup, so the SAME gate
    opening settles every tray to the SAME level despite a ~7x spread in tray
    capacity (bed depths 0.15 m -> 1.0 m). (With the old `m_out = M*k` law a
    deep tray sat near-empty and a shallow one near-full at the same gate.)"""
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    u.gate_opening = {s.id: 30.0 for s in model.stages}  # same gate on every stage

    x = x0.copy()
    for _ in range(3000):
        x, y = model.step(x, u, 0.0, 4.0)

    levels = [y.stage_level_pct[s.id] for s in model.stages]
    assert max(levels) - min(levels) < 1.0  # uniform, regardless of tray depth


def test_narrower_gate_raises_steady_state_bed_level(loaded) -> None:
    """gate_opening (§5.2: "sets inter-stage solid flow / holdup (level)") was
    previously read into Inputs and never used anywhere. Confirm it now
    genuinely affects the bed-holdup mass balance: a narrower gate should
    back meal up (higher steady-state level) relative to a wider one. Fully
    independent of the DT solve/resolve cadence (bed holdup uses `_stage_tau`
    only) -- t=0.0 held constant, no resolve fires, stays cheap."""
    (model, x0), cfg = loaded

    u_narrow = _default_inputs(cfg)
    u_narrow.gate_opening = {k: 20.0 for k in u_narrow.gate_opening}
    u_wide = _default_inputs(cfg)
    u_wide.gate_opening = {k: 80.0 for k in u_wide.gate_opening}

    x_narrow, x_wide = x0.copy(), x0.copy()
    for _ in range(500):
        x_narrow, y_narrow = model.step(x_narrow, u_narrow, 0.0, 5.0)
        x_wide, y_wide = model.step(x_wide, u_wide, 0.0, 5.0)

    assert y_narrow.stage_level_pct["MN1"] > y_wide.stage_level_pct["MN1"]


def test_mass_inventory_holdup_settles_at_steady_state(loaded) -> None:
    """Outputs.mass_inventory (mass/energy balance quality gate follow-up,
    DECISIONS.md): a cheap, always-on plant-wide holdup diagnostic -- "is
    anything accumulating." t=0.0 held constant (no resolve fires, same cheap
    pattern as the tests above) so the DT-role targets stay fixed and the
    per-stage T/X1/X2/M relaxations alone should settle to their own steady
    state well within 500 ticks (tau's own scale is minutes, dt=5s here).
    Checks the tick-to-tick CHANGE in each holdup total shrinks to near zero
    -- the "should read ~0 in steady state" signal `MassInventory`'s own
    docstring describes -- not that the RAW totals themselves are any
    particular value.
    """
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)

    x = x0.copy()
    prev = None
    for _ in range(500):
        x, y = model.step(x, u, 0.0, 5.0)
        prev = y.mass_inventory

    # One more tick: holdup totals should barely move now.
    x, y = model.step(x, u, 0.0, 5.0)
    mi = y.mass_inventory
    assert abs(mi.total_dry_solid_holdup_kg - prev.total_dry_solid_holdup_kg) < 1.0e-3
    assert abs(mi.total_hexane_holdup_kg - prev.total_hexane_holdup_kg) < 1.0e-4
    assert abs(mi.total_water_holdup_kg - prev.total_water_holdup_kg) < 1.0e-4

    # Sanity: all reported quantities are finite and non-negative.
    for value in (
        mi.total_dry_solid_holdup_kg,
        mi.total_hexane_holdup_kg,
        mi.total_water_holdup_kg,
        mi.feed_dry_solid_kg_s,
        mi.feed_hexane_kg_s,
        mi.feed_water_kg_s,
        mi.product_dry_solid_kg_s,
        mi.product_hexane_kg_s,
        mi.product_water_kg_s,
    ):
        assert value == value  # not NaN
        assert value >= 0.0
