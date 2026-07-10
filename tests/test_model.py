from dtdc_simulator.config.builder import assemble_model
from dtdc_simulator.config.loader import load_scenario

SCENARIO_PATH = "scenarios/soybean_default.yaml"


def _load():
    cfg = load_scenario(SCENARIO_PATH)
    return assemble_model(cfg), cfg


def _default_inputs(cfg):
    from dtdc_simulator.core.model import Inputs

    od = cfg.operating_defaults
    dd = cfg.disturbance_defaults
    return Inputs(
        feed_flow_rate=od.feed_flow_rate,
        feed_temperature=od.feed_temperature,
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


def test_init_state_matches_seed() -> None:
    (model, x0), cfg = _load()
    assert (x0.X2 == cfg.disturbance_defaults.feed_hexane).all()
    assert (x0.C_TIA == 1.0).all()
    assert (x0.S_prot == 1.0).all()


def test_step_is_deterministic() -> None:
    (model, x0), cfg = _load()
    u = _default_inputs(cfg)
    x1, y1 = model.step(x0, u, 0.0, 1.0)
    x2, y2 = model.step(x0, u, 0.0, 1.0)
    assert (x1.T == x2.T).all()
    assert y1.kpi_residual_hexane_ppm == y2.kpi_residual_hexane_ppm


def test_more_steam_raises_temperature() -> None:
    (model, x0), cfg = _load()
    u_cold = _default_inputs(cfg)
    u_cold.indirect_steam = {k: 0.0 for k in u_cold.indirect_steam}
    u_cold.direct_steam = {k: 0.0 for k in u_cold.direct_steam}

    u_hot = _default_inputs(cfg)
    u_hot.indirect_steam = {k: 3.0e6 for k in u_hot.indirect_steam}

    x_cold = x0
    x_hot = x0.copy()
    for _ in range(500):
        x_cold, _ = model.step(x_cold, u_cold, 0.0, 5.0)
        x_hot, _ = model.step(x_hot, u_hot, 0.0, 5.0)

    assert x_hot.T[-1] > x_cold.T[-1]


def test_tia_decays_faster_when_hot() -> None:
    (model, x0), cfg = _load()
    u = _default_inputs(cfg)
    u.indirect_steam = {k: 3.0e6 for k in u.indirect_steam}

    x = x0.copy()
    for _ in range(500):
        x, _ = model.step(x, u, 0.0, 5.0)

    assert x.C_TIA[-1] < 1.0
    assert x.S_prot[-1] <= 1.0
