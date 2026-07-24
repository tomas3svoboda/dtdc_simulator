import math
import random

import numpy as np
import pytest

from dtdc_simulator.config.builder import assemble_model
from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.core.model import Inputs, StageRole
from dtdc_simulator.core.zones import particle_jit

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
        transfer_device_position=dict(od.transfer_device_position),
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


def _assert_dynamic_physics(model, previous, current, outputs, u, dt: float) -> None:
    """Per-tick safety and dry-mass checks shared by transient trajectories."""
    for values in (current.M, current.T, current.X1, current.X2, current.solid_out):
        assert np.all(np.isfinite(values))
    assert np.all(current.M >= -1.0e-9)
    assert np.all(current.solid_out >= -1.0e-9)
    assert np.all((current.X1 >= 0.0) & (current.X1 <= 1.0))
    assert np.all((current.X2 >= 0.0) & (current.X2 <= 1.0))
    assert np.all((current.T >= 230.0) & (current.T <= 500.0))

    # Internal transfers cancel in the plant-wide dry-solid balance. The only
    # possible deficit is feed rejected at the top capacity boundary.
    accumulation = float(np.sum(current.M) - np.sum(previous.M)) / dt
    accounted_feed = accumulation + float(current.solid_out[-1])
    assert accounted_feed >= -1.0e-7
    assert accounted_feed <= u.feed_flow_rate + 1.0e-7

    profile = outputs.dt_axial_profile
    for values in (
        profile.z_m,
        profile.solid_T,
        profile.solid_X1,
        profile.solid_X2,
        profile.vapor_T,
        profile.vapor_flow_kg_s,
        profile.vapor_hexane_frac,
        profile.vapor_water_frac,
    ):
        assert all(math.isfinite(float(value)) for value in values)
    assert all(value >= 0.0 for value in profile.vapor_flow_kg_s)
    assert all(0.0 <= value <= 1.0 for value in profile.solid_X1)
    assert all(0.0 <= value <= 1.0 for value in profile.solid_X2)
    assert all(0.0 <= value <= 1.0 for value in profile.vapor_hexane_frac)
    assert all(0.0 <= value <= 1.0 for value in profile.vapor_water_frac)


def _run_dynamic_ticks(model, x, u, start: int, stop: int):
    outputs = None
    for t in range(start, stop + 1):
        previous = x
        x, outputs = model.step(x, u, float(t), 1.0)
        _assert_dynamic_physics(model, previous, x, outputs, u, 1.0)
    assert outputs is not None
    return x, outputs


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
    assert x0.dt_converged
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


def test_dt_micro_throughflow_uses_predesolv_boundary_discharge(loaded) -> None:
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    x = x0.copy()
    pd_indices = [
        i for i, stage in enumerate(model.stages) if stage.role is StageRole.PREDESOLV
    ]

    # Initialization fallback before the macro flow field has populated.
    assert model._dt_micro_throughflow(x, u) == pytest.approx(u.feed_flow_rate)

    x.solid_out[pd_indices[-1]] = 23.75
    assert model._dt_micro_throughflow(x, u) == pytest.approx(23.75)


def test_high_feed_step_resolves_at_actual_dt_boundary_flow(loaded) -> None:
    """Accumulating feed must not be treated as throughflow in the micro solve."""
    (model, x0), cfg = loaded
    u = _with_fast_resolve(_default_inputs(cfg))
    u.feed_flow_rate = 32.3
    x = x0.copy()
    pd_indices = [
        i for i, stage in enumerate(model.stages) if stage.role is StageRole.PREDESOLV
    ]
    x.solid_out[pd_indices[-1]] = 25.3

    x_next, outputs = model.step(x, u, 1.0, 1.0)

    assert x_next.dt_converged
    assert outputs.dt_solver_converged
    assert outputs.dt_micro_throughflow_kg_s == pytest.approx(
        model._dt_micro_throughflow(x_next, u)
    )
    assert outputs.dt_micro_throughflow_kg_s < u.feed_flow_rate
    assert x_next.dt_last_solve_sim_time == pytest.approx(1.0)


def test_full_high_feed_step_and_reversal_remain_dynamically_resolved(loaded) -> None:
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    u.dt_resolve_interval_s = 120.0
    u.feed_flow_rate = 32.3

    x, outputs = _run_dynamic_ticks(model, x0.copy(), u, 1, 120)
    assert x.dt_converged
    assert x.dt_last_solve_sim_time == pytest.approx(120.0)
    assert outputs.dt_micro_throughflow_kg_s < u.feed_flow_rate

    u.feed_flow_rate = 25.0
    x, outputs = _run_dynamic_ticks(model, x, u, 121, 240)
    assert x.dt_converged
    assert x.dt_last_solve_sim_time == pytest.approx(240.0)
    # After a downward step the accumulated bed can temporarily discharge
    # faster than the newly offered feed.
    assert outputs.dt_micro_throughflow_kg_s >= 25.0


def test_rapid_slider_changes_publish_only_latest_operating_point(loaded) -> None:
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    u.dt_resolve_interval_s = 120.0
    x = x0.copy()
    original_attempt = x.dt_last_attempt_sim_time

    for t in range(1, 121):
        if t == 30:
            u.feed_flow_rate = 29.0
            u.feed_temperature += 5.0
        elif t == 60:
            u.feed_flow_rate = 23.0
            u.feed_moisture = 0.10
        elif t == 90:
            u.feed_flow_rate = 27.0
            u.feed_hexane = 0.34
        previous = x
        x, outputs = model.step(x, u, float(t), 1.0)
        _assert_dynamic_physics(model, previous, x, outputs, u, 1.0)
        if t < 120:
            assert x.dt_last_attempt_sim_time == original_attempt

    assert x.dt_last_attempt_sim_time == pytest.approx(120.0)
    assert x.dt_converged
    assert x.dt_last_solve_sim_time == pytest.approx(120.0)


def test_infeasible_steam_step_keeps_profile_atomic_then_recovers(loaded) -> None:
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    u.dt_resolve_interval_s = 120.0
    u.indirect_steam = {key: value * 0.55 for key, value in u.indirect_steam.items()}
    u.direct_steam = {key: value * 0.65 for key, value in u.direct_steam.items()}
    accepted_profile = x0.dt_axial_profile

    x, _ = _run_dynamic_ticks(model, x0.copy(), u, 1, 120)
    assert not x.dt_converged
    assert x.dt_axial_profile is accepted_profile
    assert x.dt_last_solve_sim_time == pytest.approx(0.0)

    defaults = _default_inputs(cfg)
    u.indirect_steam = defaults.indirect_steam
    u.direct_steam = defaults.direct_steam
    x, _ = _run_dynamic_ticks(model, x, u, 121, 240)
    assert x.dt_converged
    assert x.dt_axial_profile is not accepted_profile
    assert x.dt_last_solve_sim_time == pytest.approx(240.0)


def test_empty_start_and_fixed_seed_disturbances_preserve_tick_invariants(loaded) -> None:
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    u.dt_resolve_interval_s = 120.0
    x = x0.copy()
    x.M[:] = 0.0
    x.solid_out[:] = 0.0

    x, _ = _run_dynamic_ticks(model, x, u, 1, 120)
    assert np.sum(x.M) > 0.0

    rng = random.Random(20260724)
    for block in range(4):
        u.feed_flow_rate = rng.uniform(22.0, 30.0)
        u.feed_temperature = rng.uniform(318.15, 338.15)
        u.feed_moisture = rng.uniform(0.09, 0.15)
        u.feed_hexane = rng.uniform(0.28, 0.38)
        indirect_factor = rng.uniform(0.85, 1.15)
        direct_factor = rng.uniform(0.9, 1.1)
        defaults = _default_inputs(cfg)
        u.indirect_steam = {
            key: value * indirect_factor for key, value in defaults.indirect_steam.items()
        }
        u.direct_steam = {
            key: value * direct_factor for key, value in defaults.direct_steam.items()
        }
        start = 121 + block * 120
        x, _ = _run_dynamic_ticks(model, x, u, start, start + 119)

    u = _default_inputs(cfg)
    u.dt_resolve_interval_s = 120.0
    x, _ = _run_dynamic_ticks(model, x, u, 601, 720)
    assert x.dt_converged


def test_dynamic_micro_coupling_matches_python_fallback(loaded, monkeypatch) -> None:
    (model, x0), cfg = loaded
    pd_indices = [
        i for i, stage in enumerate(model.stages) if stage.role is StageRole.PREDESOLV
    ]
    x_jit = x0.copy()
    x_python = x0.copy()
    x_jit.solid_out[pd_indices[-1]] = 25.3
    x_python.solid_out[pd_indices[-1]] = 25.3
    u_jit = _with_fast_resolve(_default_inputs(cfg))
    u_python = _with_fast_resolve(_default_inputs(cfg))
    u_jit.feed_flow_rate = u_python.feed_flow_rate = 32.3

    x_jit, _ = model.step(x_jit, u_jit, 1.0, 1.0)
    monkeypatch.setattr(particle_jit, "JIT_DISABLED", True)
    x_python, _ = model.step(x_python, u_python, 1.0, 1.0)

    assert x_jit.dt_converged and x_python.dt_converged
    assert x_jit.dt_outer_iterations == x_python.dt_outer_iterations
    assert x_jit.dt_target_T == pytest.approx(x_python.dt_target_T, rel=1.0e-12, abs=1.0e-10)
    assert x_jit.dt_target_X1 == pytest.approx(x_python.dt_target_X1, rel=1.0e-12, abs=1.0e-12)
    assert x_jit.dt_target_X2 == pytest.approx(x_python.dt_target_X2, rel=1.0e-12, abs=1.0e-12)


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

    Both solves must also be accepted: directional comparisons may not consume
    a diagnostic best iterate that failed the publication boundary.
    """
    (model, x0), cfg = loaded

    u_cold = _with_fast_resolve(_default_inputs(cfg))
    u_cold.indirect_steam = {k: v * 0.5 for k, v in u_cold.indirect_steam.items()}
    u_hot = _with_fast_resolve(_default_inputs(cfg))
    u_hot.indirect_steam = {k: v * 2.0 for k, v in u_hot.indirect_steam.items()}

    x_cold, _ = model.step(x0, u_cold, 1.0, 1.0)  # t=1.0 >> the tiny resolve interval
    x_hot, _ = model.step(x0, u_hot, 1.0, 1.0)

    assert x_cold.dt_converged
    assert x_hot.dt_converged
    assert max(x_hot.dt_target_T) > max(x_cold.dt_target_T)


def test_high_feed_high_level_case_converges_to_physical_profile(loaded) -> None:
    """The former 40.4 kg/s failure now resolves through adaptive continuation."""
    (model, x0), cfg = loaded
    x = x0.copy()
    for i, stage in enumerate(model.stages):
        if stage.role in (StageRole.PREDESOLV, StageRole.MAIN, StageRole.SPARGE):
            x.M[i] = 0.81 * model._stage_M_max(stage)

    u = _with_fast_resolve(_default_inputs(cfg))
    u.feed_flow_rate = 40.4
    pd_ids = [stage.id for stage in model.stages if stage.role is StageRole.PREDESOLV]
    pd_total_w = 2.07 * cfg.physical.dH_vap_water
    for stage_id in pd_ids:
        u.indirect_steam[stage_id] = pd_total_w / len(pd_ids)

    accepted_profile = x.dt_axial_profile
    x_next, outputs = model.step(x, u, 1.0, 1.0)

    assert x_next.dt_converged
    assert x_next.dt_last_attempt_sim_time == pytest.approx(1.0)
    assert x_next.dt_last_solve_sim_time == pytest.approx(1.0)
    assert x_next.dt_axial_profile is not accepted_profile
    assert outputs.dt_axial_profile is x_next.dt_axial_profile
    assert min(outputs.dt_axial_profile.solid_T) >= u.feed_temperature - 1.0
    assert max(outputs.dt_axial_profile.solid_T) < 450.0
    assert all(0.0 <= value <= 1.0 for value in outputs.dt_axial_profile.solid_X1)
    assert all(0.0 <= value <= 1.0 for value in outputs.dt_axial_profile.solid_X2)


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
    expected_stage_vapor_flow: dict[str, float] = {}
    for sid, flow in zip(
        y.dt_axial_profile.stage_id,
        y.dt_axial_profile.vapor_flow_kg_s,
    ):
        expected_stage_vapor_flow.setdefault(sid, flow)
    assert y.stage_vapor_flow_kg_s == pytest.approx(expected_stage_vapor_flow)
    for value in (
        y.kpi_outlet_vapor_hexane_kg_s,
        y.kpi_outlet_vapor_water_kg_s,
        y.kpi_condenser_duty_kw,
        y.kpi_exhaust_hexane_ppm,
    ):
        assert value == value  # not NaN
        assert value >= 0.0


def test_closed_controlled_transfer_stops_discharge_and_floods(loaded) -> None:
    """A shut controlled transfer must genuinely STOP its source stage's
    solid discharge (not merely slow it, as the old M/tau residence model did)
    -- so material accumulates and the tray floods past 100%."""
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    transfer = next(item for item in model.solid_transfers if item.controlled)
    source = transfer.from_stage
    u.transfer_device_position = dict(u.transfer_device_position)
    u.transfer_device_position[transfer.id] = 0.0

    x, y = x0.copy(), None
    for _ in range(200):
        x, y = model.step(x, u, 0.0, 5.0)

    assert y.stage_solid_out_kg_s[source] == pytest.approx(0.0, abs=1.0e-9)
    # Capacity-capped: a full tray sits AT ~100% (surplus is rejected upstream),
    # it doesn't climb unboundedly past it.
    assert y.stage_level_pct[source] >= 99.5


def test_backpressure_floods_the_tray_above(loaded) -> None:
    """Back-pressure cascade: when a tray floods and can't accept its inflow,
    the surplus is rejected back into the tray ABOVE it, which then floods too
    -- so a shut gate mid-column backs material up toward the feed."""
    (model, x0), cfg = loaded
    transfer = next(item for item in model.solid_transfers if item.id == "MN2_TO_SP1")
    blocked, upstream = transfer.from_stage, "MN1"
    u = _default_inputs(cfg)
    u.transfer_device_position = dict(u.transfer_device_position)
    u.transfer_device_position[transfer.id] = 0.0

    x, y = x0.copy(), None
    for _ in range(400):
        x, y = model.step(x, u, 0.0, 5.0)

    assert y.stage_solid_out_kg_s[blocked] == pytest.approx(0.0, abs=1.0e-9)
    assert y.stage_level_pct[blocked] >= 99.5
    assert y.stage_level_pct[upstream] >= 99.5  # back-pressure floods the tray above


def test_controlled_transfers_control_level_uniformly(loaded) -> None:
    """Discharge is driven by bed LEVEL, not absolute holdup, so the SAME gate
    opening settles every tray to the SAME level despite a ~7x spread in tray
    capacity (bed depths 0.15 m -> 1.0 m). (With the old `m_out = M*k` law a
    deep tray sat near-empty and a shallow one near-full at the same gate.)"""
    (model, x0), cfg = loaded
    u = _default_inputs(cfg)
    u.transfer_device_position = {
        transfer.id: 30.0 for transfer in model.solid_transfers if transfer.controlled
    }

    x = x0.copy()
    for _ in range(3000):
        x, y = model.step(x, u, 0.0, 4.0)

    controlled_sources = [
        transfer.from_stage for transfer in model.solid_transfers if transfer.controlled
    ]
    levels = [y.stage_level_pct[stage_id] for stage_id in controlled_sources]
    assert max(levels) - min(levels) < 1.0  # uniform, regardless of tray depth


def test_narrower_gate_raises_steady_state_bed_level(loaded) -> None:
    """Transfer-device position must genuinely affect the bed-holdup balance.
    Confirm that it now
    genuinely affects the bed-holdup mass balance: a narrower gate should
    back meal up (higher steady-state level) relative to a wider one. Fully
    independent of the DT solve/resolve cadence (bed holdup uses `_stage_tau`
    only) -- t=0.0 held constant, no resolve fires, stays cheap."""
    (model, x0), cfg = loaded

    u_narrow = _default_inputs(cfg)
    u_narrow.transfer_device_position = {k: 20.0 for k in u_narrow.transfer_device_position}
    u_wide = _default_inputs(cfg)
    u_wide.transfer_device_position = {k: 80.0 for k in u_wide.transfer_device_position}

    x_narrow, x_wide = x0.copy(), x0.copy()
    for _ in range(500):
        x_narrow, y_narrow = model.step(x_narrow, u_narrow, 0.0, 5.0)
        x_wide, y_wide = model.step(x_wide, u_wide, 0.0, 5.0)

    assert y_narrow.stage_level_pct["MN1"] > y_wide.stage_level_pct["MN1"]


def test_faster_sweep_arm_lowers_uncontrolled_tray_levels(loaded) -> None:
    """The sweep arm is the tray's mechanical conveyor: at the same feed and
    drop-hole opening, faster rotation must increase discharge capacity and
    therefore lower the settled bed level, including passive boundaries."""
    (model, x0), cfg = loaded
    u_slow = _default_inputs(cfg)
    u_slow.sweep_arm_speed = {k: 1.5 for k in u_slow.sweep_arm_speed}
    u_fast = _default_inputs(cfg)
    u_fast.sweep_arm_speed = {k: 6.0 for k in u_fast.sweep_arm_speed}

    x_slow, x_fast = x0.copy(), x0.copy()
    for _ in range(800):
        x_slow, y_slow = model.step(x_slow, u_slow, 0.0, 5.0)
        x_fast, y_fast = model.step(x_fast, u_fast, 0.0, 5.0)

    for stage_id in ("PD1", "PD2", "PD3", "DR1", "CL1"):
        assert y_fast.stage_level_pct[stage_id] < y_slow.stage_level_pct[stage_id]


def test_warmer_dryer_air_removes_more_hexane(loaded) -> None:
    """Residual solvent volatility must rise, not fall, with dryer-air heat."""
    (model, x0), cfg = loaded
    dryer = next(stage for stage in model.stages if stage.role is StageRole.DRYER)
    inlet_index = list(model.stages).index(dryer) - 1
    T_in, X1_in, X2_in = x0.T[inlet_index], x0.X1[inlet_index], x0.X2[inlet_index]
    u_cold = _default_inputs(cfg)
    u_cold.heated_air_temp = 310.0
    u_hot = _default_inputs(cfg)
    u_hot.heated_air_temp = 370.0

    cold = model._dc_equilibrium(dryer, T_in, X1_in, X2_in, u_cold, model._stage_tau(dryer, u_cold))
    hot = model._dc_equilibrium(dryer, T_in, X1_in, X2_in, u_hot, model._stage_tau(dryer, u_hot))

    assert hot[0] > cold[0]
    assert hot[2] < cold[2]


def test_dt_update_retries_without_stale_warm_start(loaded) -> None:
    """A valid operator move must not leave the old DT hexane profile pinned
    merely because the preceding operating point is a poor initial guess."""
    (model, x0), cfg = loaded
    u = _with_fast_resolve(_default_inputs(cfg))
    u.indirect_steam = {
        stage_id: (0.0 if stage_id.startswith("PD") else duty)
        for stage_id, duty in u.indirect_steam.items()
    }

    x_next, _ = model.step(x0, u, 1.0, 1.0)

    assert x_next.dt_converged
    assert x_next.dt_target_X2.tolist() != x0.dt_target_X2.tolist()


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
