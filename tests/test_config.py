from pathlib import Path

import pytest

from dtdc_simulator.config.loader import load_material_properties, load_scenario
from dtdc_simulator.config.schema import PhysicalParams, ScenarioConfig

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "scenarios" / "soybean_default.yaml"
PROPERTIES_DIR = ROOT / "properties"


def test_load_scenario_ok() -> None:
    cfg = load_scenario(SCENARIO_PATH)
    assert isinstance(cfg, ScenarioConfig)
    assert cfg.material == "soybean"
    assert cfg.geometry.n_stages == 8
    assert cfg.disturbance_defaults.feed_temperature == pytest.approx(322.15)
    assert cfg.disturbance_defaults.feed_moisture == pytest.approx(0.124)
    assert cfg.disturbance_defaults.feed_hexane == pytest.approx(0.388)
    assert cfg.disturbance_defaults.feed_oil == pytest.approx(0.0137)
    assert (cfg.model.dt_nz_phz, cfg.model.dt_nz_ftrz, cfg.model.dt_nz_dcz) == (20, 20, 20)


def test_scenario_resolves_physical_from_properties_dir() -> None:
    """scenarios/soybean_default.yaml has no inline `physical:` block (BuildSpec
    §11); load_scenario must resolve it from properties/soybean.yaml."""
    cfg = load_scenario(SCENARIO_PATH, properties_dir=PROPERTIES_DIR)
    assert cfg.physical.material_name == "soybean"
    assert cfg.physical.dH_vap_hexane > 0


def test_load_material_properties_standalone() -> None:
    props = load_material_properties("soybean", properties_dir=PROPERTIES_DIR)
    assert isinstance(props, PhysicalParams)
    assert 0 < props.bed_porosity < 1


def test_unknown_stage_id_in_indirect_steam_rejected() -> None:
    cfg = load_scenario(SCENARIO_PATH)
    raw = cfg.model_dump()
    raw["operating_defaults"]["indirect_steam"]["NOPE"] = 1.0
    with pytest.raises(Exception):
        ScenarioConfig.model_validate(raw)


def test_undersample_constraint_rejected() -> None:
    cfg = load_scenario(SCENARIO_PATH)
    raw = cfg.model_dump()
    raw["sim"]["speed_factor"] = 1000.0
    raw["sim"]["max_control_interval_s"] = 1.0
    with pytest.raises(Exception):
        ScenarioConfig.model_validate(raw)


def test_porosity_out_of_range_rejected() -> None:
    cfg = load_scenario(SCENARIO_PATH)
    raw = cfg.model_dump()
    raw["physical"]["bed_porosity"] = 1.5
    with pytest.raises(Exception):
        ScenarioConfig.model_validate(raw)


def test_topology_has_one_linear_transfer_per_stage() -> None:
    cfg = load_scenario(SCENARIO_PATH)
    assert len(cfg.topology.solid_transfers) == len(cfg.geometry.stages)
    assert [stage.vapor_path.value for stage in cfg.geometry.stages[:3]] == [
        "BYPASS",
        "BYPASS",
        "BYPASS",
    ]
    assert not cfg.topology.solid_transfers[0].controlled


def test_control_seed_for_passive_transfer_rejected() -> None:
    cfg = load_scenario(SCENARIO_PATH)
    raw = cfg.model_dump()
    raw["operating_defaults"]["transfer_device_position"]["PD1_TO_PD2"] = 50.0
    with pytest.raises(Exception):
        ScenarioConfig.model_validate(raw)


def test_non_linear_transfer_reference_rejected() -> None:
    cfg = load_scenario(SCENARIO_PATH)
    raw = cfg.model_dump()
    raw["topology"]["solid_transfers"][0]["to_stage"] = "MN1"
    with pytest.raises(Exception):
        ScenarioConfig.model_validate(raw)
