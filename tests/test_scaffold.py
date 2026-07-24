"""Scenario scaffolding (Phase 4): compact DesignSpec -> valid ScenarioConfig."""

from __future__ import annotations

import pytest

from dtdc_simulator.config.design_rules import has_errors, validate_design
from dtdc_simulator.config.scaffold import DesignSpec, scaffold_scenario
from dtdc_simulator.config.schema import ClockKind, SolidTransferDeviceType, StageRole
from dtdc_simulator.engine.facade import RuntimeFacade


def test_default_spec_is_valid_and_assembles() -> None:
    cfg = scaffold_scenario(DesignSpec())
    assert [s.id for s in cfg.geometry.stages] == [
        "PD1",
        "PD2",
        "PD3",
        "MN1",
        "MN2",
        "SP1",
        "DR1",
        "CL1",
    ]
    assert not has_errors(validate_design(cfg))

    cfg.sim.clock = ClockKind.FREERUN
    cfg.model.dt_nz_phz = 5
    cfg.model.dt_nz_ftrz = 5
    cfg.model.dt_nz_dcz = 4
    facade = RuntimeFacade()
    facade.configure(cfg)
    facade.assemble()  # would raise if the scaffold produced an unassemblable config
    assert facade.get_snapshot().stage_order == [
        "PD1",
        "PD2",
        "PD3",
        "MN1",
        "MN2",
        "SP1",
        "DR1",
        "CL1",
    ]


@pytest.mark.parametrize(
    "spec",
    [
        DesignSpec(n_predesolv=1, n_main=1, n_sparge=1, n_dryer=0, n_cooler=0),  # minimal DT-only
        DesignSpec(n_predesolv=7, n_main=4, n_sparge=1, n_dryer=3, n_cooler=2),  # envelope maxima
        DesignSpec(n_predesolv=2, n_main=2, n_sparge=1, n_dryer=1, n_cooler=0),  # dryer, no cooler
    ],
)
def test_custom_layouts_validate_clean(spec: DesignSpec) -> None:
    cfg = scaffold_scenario(spec)
    assert not has_errors(validate_design(cfg))


def test_transfer_device_selection() -> None:
    cfg = scaffold_scenario(DesignSpec())
    by_id = {t.id: t for t in cfg.topology.solid_transfers}
    # within a zone -> passive; DT zone crossing -> controlled gate
    assert by_id["PD1_TO_PD2"].device_type is SolidTransferDeviceType.PASSIVE_SWEPT_PORT
    assert by_id["PD3_TO_MN1"].device_type is SolidTransferDeviceType.CONTROLLED_GATE
    # DT -> DC and product outlet -> sealed rotary airlock
    assert by_id["SP1_TO_DR1"].device_type is SolidTransferDeviceType.ROTARY_AIRLOCK
    assert by_id["SP1_TO_DR1"].vapor_seal
    assert by_id["CL1_PRODUCT"].to_stage is None
    assert by_id["CL1_PRODUCT"].vapor_seal


def test_dt_only_outlet_is_sealed() -> None:
    cfg = scaffold_scenario(DesignSpec(n_dryer=0, n_cooler=0))
    outlet = cfg.topology.solid_transfers[-1]
    assert outlet.from_stage == "SP1"
    assert outlet.to_stage is None
    assert outlet.vapor_seal


def test_indirect_steam_split_and_units() -> None:
    spec = DesignSpec(n_predesolv=2, predesolv_indirect_total_w=2.0e6, feed_temperature_c=49.0)
    cfg = scaffold_scenario(spec)
    pred = {s.id for s in cfg.geometry.stages if s.role is StageRole.PREDESOLV}
    pred_total = sum(v for k, v in cfg.operating_defaults.indirect_steam.items() if k in pred)
    assert pred_total == pytest.approx(2.0e6)
    # temperatures converted to Kelvin
    assert cfg.disturbance_defaults.feed_temperature == pytest.approx(49.0 + 273.15)
