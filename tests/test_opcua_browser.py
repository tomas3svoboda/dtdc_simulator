"""Node-browser model (Phase 3): active vs placeholder rows with quality."""

from __future__ import annotations

from dtdc_simulator.config.envelope import load_envelope
from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.schema import ClockKind
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.interfaces.opcua.browser import browser_rows


def _facade() -> RuntimeFacade:
    cfg = load_scenario("scenarios/soybean_default.yaml")
    cfg.sim.clock = ClockKind.FREERUN
    cfg.model.dt_nz_phz = 5
    cfg.model.dt_nz_ftrz = 5
    cfg.model.dt_nz_dcz = 4
    f = RuntimeFacade()
    f.configure(cfg)
    f.assemble()
    return f


def test_browser_rows_active_placeholder_and_categories() -> None:
    rows = browser_rows(load_envelope(), _facade().get_snapshot())
    by_path = {r.path: r for r in rows}

    active = by_path["Measurements/Stage/PD1/T"]
    assert active.active and active.quality == "Good" and active.value != "—"

    placeholder = by_path["Measurements/Stage/PD7/T"]  # never active in the default build
    assert not placeholder.active
    assert placeholder.quality == "Bad"
    assert placeholder.value == "—"

    assert {r.category for r in rows} == {"Measurement", "KPI", "Control", "Input"}
    # a fixed control loop is active; a boundary loop with no controlled outlet is not
    assert by_path["Control/FIC_DT_FEED/PV"].active
    assert not by_path["Control/ZIC_PD1/PV"].active  # PD1 outlet is a passive swept port
