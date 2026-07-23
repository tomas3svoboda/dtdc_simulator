import pytest

from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.schema import ClockKind
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.engine.mv import Mode
from dtdc_simulator.engine.state_machine import SimState

SCENARIO_PATH = "scenarios/soybean_default.yaml"


def _facade_freerun() -> RuntimeFacade:
    cfg = load_scenario(SCENARIO_PATH)
    cfg.sim.clock = ClockKind.FREERUN
    # M3a: assemble() now runs one real solve_dt() call inside init_state(),
    # and FreeRunClock's sim_time genuinely advances tick-to-tick (unlike
    # test_model.py's direct model.step() calls, which hold t constant) --
    # so a long tick loop here WILL eventually cross dt_resolve_interval_s
    # and trigger further real solves. These tests exercise facade PLUMBING
    # (state transitions, MV routing), not DT physics correctness (that's
    # test_dt_solver.py's/test_model.py's job) -- so an even coarser mesh
    # than the scenario's own real-time default is an appropriate trade,
    # kept local to this test file's own config copy.
    cfg.model.dt_nz_phz = 5
    cfg.model.dt_nz_ftrz = 5
    cfg.model.dt_nz_dcz = 4
    facade = RuntimeFacade()
    facade.configure(cfg)
    facade.assemble()
    return facade


def test_lifecycle_transitions() -> None:
    facade = _facade_freerun()
    assert facade.state is SimState.READY
    facade.run()
    assert facade.state is SimState.RUNNING
    facade.pause()
    assert facade.state is SimState.PAUSED
    facade.run()
    assert facade.state is SimState.RUNNING
    facade.stop()
    assert facade.state is SimState.STOPPED
    facade.reset()
    assert facade.state is SimState.READY
    assert facade.get_snapshot().sim_time == 0.0


def test_tick_only_advances_while_running() -> None:
    facade = _facade_freerun()
    facade.tick()
    assert facade.get_snapshot().sim_time == 0.0  # READY, not RUNNING: no-op
    facade.run()
    facade.tick()
    assert facade.get_snapshot().sim_time > 0.0


def test_auto_mode_drives_pv_apc_style() -> None:
    facade = _facade_freerun()
    facade.run()
    # Assert on the DT PEAK temperature, not a single tray: individual trays are variously
    # pinned -- the MAIN/SPARGE (DCZ) trays at the direct-steam saturation temperature, and a
    # PREDESOLV tray sitting at the hexane boiling point is evaporation-pinned (extra jacket
    # duty goes into hexane latent heat, not temperature). The DT peak still rises with total
    # jacket duty (see tests/test_model.py::test_more_steam_raises_dt_target_temperature).
    dt_stages = [sid for sid, role in facade.get_snapshot().stage_roles.items()
                 if role in ("PREDESOLV", "MAIN", "SPARGE")]
    for sid in dt_stages:
        facade.set_mv_mode(f"indirect_steam/{sid}", Mode.AUTO)
        facade.set_mv_auto_setpoint(f"indirect_steam/{sid}", 3.0e6)
    peak_before = max(facade.get_snapshot().outputs.stage_T[s] for s in dt_stages)
    for _ in range(300):
        facade.tick()
    peak_after = max(facade.get_snapshot().outputs.stage_T[s] for s in dt_stages)
    assert peak_after > peak_before


def test_feed_oil_is_a_live_dv() -> None:
    """M4 (GUI redesign): feed oil (X3) is a live disturbance now -- registered
    in the DV registry, seeded from the scenario, and settable at runtime."""
    facade = _facade_freerun()
    dvs = facade.get_snapshot().dvs
    assert "feed_oil" in dvs
    facade.set_dv("feed_oil", 0.025)
    assert facade.get_snapshot().dvs["feed_oil"] == 0.025


def test_snapshot_exposes_distinct_indirect_and_direct_steam_conditions() -> None:
    facade = _facade_freerun()
    steam = facade.get_snapshot().steam

    assert steam is not None
    assert steam.supply_barg == pytest.approx(6.9)
    assert steam.direct_contact_barg == pytest.approx(3.0)
    assert steam.supply_T_K > steam.direct_contact_T_K


def test_group_manual_setpoint_broadcasts_to_all_stages() -> None:
    """M4: one global 'arm rotation speed' slider drives every per-stage
    sweep_arm_speed MV via set_mv_group_manual_setpoint, leaving per-stage
    keys individually addressable (OPC UA granularity)."""
    facade = _facade_freerun()
    arm_keys = [k for k in facade.mv_keys() if k.split("/", 1)[0] == "sweep_arm_speed"]
    assert arm_keys  # scenario has per-stage sweep arms
    facade.set_mv_group_manual_setpoint("sweep_arm_speed", 7.0)
    mvs = facade.get_snapshot().mvs
    for key in arm_keys:
        assert mvs[key].manual_setpoint == 7.0


def test_weighted_group_total_preserves_jacket_split() -> None:
    facade = _facade_freerun()
    keys = ["indirect_steam/MN1", "indirect_steam/MN2", "indirect_steam/SP1"]
    before = facade.get_snapshot().mvs
    ratio = [before[key].manual_setpoint / before[keys[0]].manual_setpoint for key in keys]

    facade.set_mv_weighted_group_manual_total(keys, 4.0e5)
    after = facade.get_snapshot().mvs
    values = [after[key].manual_setpoint for key in keys]

    assert sum(values) == pytest.approx(4.0e5)
    assert [value / values[0] for value in values] == pytest.approx(ratio)


def test_weighted_group_total_redistributes_after_saturation() -> None:
    facade = _facade_freerun()
    keys = ["indirect_steam/MN1", "indirect_steam/MN2", "indirect_steam/SP1"]
    before = facade.get_snapshot().mvs
    total_capacity = sum(before[key].max for key in keys)

    facade.set_mv_weighted_group_manual_total(keys, total_capacity)
    after = facade.get_snapshot().mvs

    assert sum(after[key].manual_setpoint for key in keys) == pytest.approx(total_capacity)
    for key in keys:
        assert after[key].manual_setpoint == pytest.approx(after[key].max)


def test_manual_mode_ignores_auto_writes() -> None:
    facade = _facade_freerun()
    facade.run()
    key = "feed_flow_rate"
    facade.set_mv_mode(key, Mode.MANUAL)
    facade.set_mv_manual_setpoint(key, 25.0)
    facade.set_mv_auto_setpoint(key, 99.0)  # must be ignored while MANUAL
    facade.tick()
    assert facade.get_snapshot().mvs[key].effective_value == 25.0


def test_plc_catalog_exposes_fixed_zone_allocation_and_real_transfer_devices() -> None:
    facade = _facade_freerun()
    snap = facade.get_snapshot()

    assert "FIC_DT_PD_IND_STM" in snap.control_loops
    assert "FIC_DT_DIRECT_STM" in snap.control_loops
    assert "SIC_DT_SHAFT" in snap.control_loops
    assert "ZIC_MN1_TO_MN2" in snap.control_loops
    assert "ZIC_PD1_TO_PD2" not in snap.control_loops

    facade.set_control_mode("FIC_DT_PD_IND_STM", Mode.AUTO)
    facade.set_control_setpoint("FIC_DT_PD_IND_STM", 1800.0)  # kW
    after = facade.get_snapshot()
    loop = after.control_loops["FIC_DT_PD_IND_STM"]
    assert loop.mode == "AUTO"
    assert loop.sp == pytest.approx(1800.0)
    pd_keys = loop.actuator_keys
    assert [after.mvs[key].auto_setpoint for key in pd_keys] == pytest.approx(
        [600_000.0, 600_000.0, 600_000.0]
    )


def test_common_shaft_loop_broadcasts_one_plc_setpoint() -> None:
    facade = _facade_freerun()
    facade.set_control_mode("SIC_DT_SHAFT", Mode.AUTO)
    facade.set_control_setpoint("SIC_DT_SHAFT", 4.25)
    snap = facade.get_snapshot()

    assert snap.control_loops["SIC_DT_SHAFT"].sp == pytest.approx(4.25)
    for key in snap.control_loops["SIC_DT_SHAFT"].actuator_keys:
        assert snap.mvs[key].auto_setpoint == pytest.approx(4.25)
