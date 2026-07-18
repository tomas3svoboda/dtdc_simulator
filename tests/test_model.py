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
        ambient_air_temp=od.ambient_air_temp,
        ambient_air_flow=od.ambient_air_flow,
        feed_moisture=dd.feed_moisture,
        feed_hexane=dd.feed_hexane,
        ambient_temp=dd.ambient_temp,
        ambient_humidity=dd.ambient_humidity,
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
    profile at the DT's own exit tray.

    Not asserting `dt_converged` here: the scenario's own real-time tuning
    (M3a follow-up "A2", `dt_outer_tol=0.05`/`dt_outer_max_iter=20`) is
    deliberately loose enough that the formal convergence flag often reads
    False within so few iterations even though the profile itself is already
    close to its asymptote (verified directly this session -- within ~1K/
    ~2ppm of the tight-tolerance answer at these settings) -- `converged`
    is a diagnostic for `SolverStress`, not a precondition for the profile
    being directionally meaningful, which is what this test actually checks.
    """
    (model, x0), cfg = loaded

    u_cold = _with_fast_resolve(_default_inputs(cfg))
    u_cold.indirect_steam = {k: v * 0.5 for k, v in u_cold.indirect_steam.items()}
    u_hot = _with_fast_resolve(_default_inputs(cfg))
    u_hot.indirect_steam = {k: v * 2.0 for k, v in u_hot.indirect_steam.items()}

    x_cold, _ = model.step(x0, u_cold, 1.0, 1.0)  # t=1.0 >> the tiny resolve interval
    x_hot, _ = model.step(x0, u_hot, 1.0, 1.0)

    assert x_hot.dt_target_T[-1] > x_cold.dt_target_T[-1]


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
