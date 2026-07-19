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


def test_manual_mode_ignores_auto_writes() -> None:
    facade = _facade_freerun()
    facade.run()
    key = "feed_flow_rate"
    facade.set_mv_mode(key, Mode.MANUAL)
    facade.set_mv_manual_setpoint(key, 25.0)
    facade.set_mv_auto_setpoint(key, 99.0)  # must be ignored while MANUAL
    facade.tick()
    assert facade.get_snapshot().mvs[key].effective_value == 25.0
