from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.schema import ClockKind
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.engine.mv import Mode
from dtdc_simulator.engine.state_machine import SimState

SCENARIO_PATH = "scenarios/soybean_default.yaml"


def _facade_freerun() -> RuntimeFacade:
    cfg = load_scenario(SCENARIO_PATH)
    cfg.sim.clock = ClockKind.FREERUN
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
    key = "indirect_steam/MN1"
    facade.set_mv_mode(key, Mode.AUTO)
    facade.set_mv_auto_setpoint(key, 3.0e6)
    T_before = facade.get_snapshot().outputs.stage_T["MN1"]
    for _ in range(300):
        facade.tick()
    T_after = facade.get_snapshot().outputs.stage_T["MN1"]
    assert T_after > T_before


def test_manual_mode_ignores_auto_writes() -> None:
    facade = _facade_freerun()
    facade.run()
    key = "feed_flow_rate"
    facade.set_mv_mode(key, Mode.MANUAL)
    facade.set_mv_manual_setpoint(key, 25.0)
    facade.set_mv_auto_setpoint(key, 99.0)  # must be ignored while MANUAL
    facade.tick()
    assert facade.get_snapshot().mvs[key].effective_value == 25.0
