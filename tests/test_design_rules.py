"""Design-realism validator (Phase 2): the shipped scenario is clean; targeted
unrealistic/unsafe designs are flagged with the right severity."""

from __future__ import annotations

import copy

from dtdc_simulator.config.design_rules import (
    Severity,
    errors,
    has_errors,
    validate_design,
    warnings,
)
from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.schema import ScenarioConfig


def _base_raw() -> dict:
    return copy.deepcopy(load_scenario("scenarios/soybean_default.yaml").model_dump())


def _make(
    stages: list[dict], transfers: list[dict], *, indirect=None, direct=None
) -> ScenarioConfig:
    """Build a schema-valid ScenarioConfig with a custom stage list + cascade,
    reusing the base physical/model/disturbance/sim blocks."""
    raw = _base_raw()
    raw["geometry"]["stages"] = stages
    raw["topology"]["solid_transfers"] = transfers
    od = raw["operating_defaults"]
    ids = [s["id"] for s in stages]
    od["sweep_arm_speed"] = {i: 3.0 for i in ids}
    od["indirect_steam"] = indirect or {}
    od["direct_steam"] = direct or {}
    od["transfer_device_position"] = {t["id"]: 50 for t in transfers if t.get("controlled")}
    return ScenarioConfig.model_validate(raw)


def _gate(bid: str, frm: str, to: str | None) -> dict:
    return {
        "id": bid,
        "from_stage": frm,
        "to_stage": to,
        "device_type": "CONTROLLED_GATE",
        "controlled": True,
    }


def _airlock(bid: str, frm: str, to: str | None) -> dict:
    return {
        "id": bid,
        "from_stage": frm,
        "to_stage": to,
        "device_type": "ROTARY_AIRLOCK",
        "controlled": True,
        "vapor_seal": True,
    }


def _codes(issues) -> set[str]:
    return {i.code for i in issues}


def test_shipped_scenario_is_clean() -> None:
    issues = validate_design(load_scenario("scenarios/soybean_default.yaml"))
    assert issues == []


def test_zone_order_violation() -> None:
    # MAIN declared before PREDESOLV.
    cfg = _make(
        stages=[
            {"id": "MN1", "role": "MAIN", "diameter_m": 6.0, "bed_height_m": 1.2},
            {"id": "PD1", "role": "PREDESOLV", "diameter_m": 6.0, "bed_height_m": 0.6},
            {"id": "SP1", "role": "SPARGE", "diameter_m": 6.0, "bed_height_m": 1.5},
        ],
        transfers=[
            _gate("MN1_TO_PD1", "MN1", "PD1"),
            _gate("PD1_TO_SP1", "PD1", "SP1"),
            _airlock("SP1_PROD", "SP1", None),
        ],
        indirect={"MN1": 8.0e4, "PD1": 7.6e5, "SP1": 3.0e4},
        direct={"SP1": 3.9},
    )
    issues = validate_design(cfg)
    assert "ZONE_ORDER" in _codes(issues)
    assert has_errors(issues)


def test_sparge_cap_exceeded() -> None:
    cfg = _make(
        stages=[
            {"id": "PD1", "role": "PREDESOLV", "diameter_m": 6.0, "bed_height_m": 0.6},
            {"id": "MN1", "role": "MAIN", "diameter_m": 6.0, "bed_height_m": 1.2},
            {"id": "SP1", "role": "SPARGE", "diameter_m": 6.0, "bed_height_m": 1.5},
            {"id": "SP2", "role": "SPARGE", "diameter_m": 6.0, "bed_height_m": 1.5},
        ],
        transfers=[
            _gate("PD1_TO_MN1", "PD1", "MN1"),
            _gate("MN1_TO_SP1", "MN1", "SP1"),
            _gate("SP1_TO_SP2", "SP1", "SP2"),
            _airlock("SP2_PROD", "SP2", None),
        ],
        indirect={"PD1": 7.6e5, "MN1": 8.0e4, "SP1": 3.0e4, "SP2": 3.0e4},
        direct={"SP1": 3.9, "SP2": 3.9},
    )
    codes = _codes(validate_design(cfg))
    assert "ZONE_CAP_EXCEEDED" in codes  # SPARGE cap is 1


def test_main_below_minimum() -> None:
    cfg = _make(
        stages=[
            {"id": "PD1", "role": "PREDESOLV", "diameter_m": 6.0, "bed_height_m": 0.6},
            {"id": "SP1", "role": "SPARGE", "diameter_m": 6.0, "bed_height_m": 1.5},
        ],
        transfers=[_gate("PD1_TO_SP1", "PD1", "SP1"), _airlock("SP1_PROD", "SP1", None)],
        indirect={"PD1": 7.6e5, "SP1": 3.0e4},
        direct={"SP1": 3.9},
    )
    codes = _codes(validate_design(cfg))
    assert "ZONE_BELOW_MIN" in codes  # MAIN min is 1


def test_dt_to_dc_must_be_vapor_sealed() -> None:
    raw = _base_raw()
    for b in raw["topology"]["solid_transfers"]:
        if b["id"] == "SP1_TO_DR1":
            b["device_type"] = "CONTROLLED_GATE"
            b["vapor_seal"] = False
    issues = validate_design(ScenarioConfig.model_validate(raw))
    dt_dc = [i for i in issues if i.code == "DT_TO_DC_VAPOR_SEAL"]
    assert len(dt_dc) == 1
    assert dt_dc[0].severity is Severity.ERROR
    assert dt_dc[0].location == "SP1_TO_DR1"


def test_non_canonical_id_warns_not_blocks() -> None:
    raw = _base_raw()
    raw["geometry"]["stages"][0]["id"] = "FOO"
    for b in raw["topology"]["solid_transfers"]:
        if b["from_stage"] == "PD1":
            b["from_stage"] = "FOO"
    raw["operating_defaults"]["indirect_steam"] = {
        ("FOO" if k == "PD1" else k): v
        for k, v in raw["operating_defaults"]["indirect_steam"].items()
    }
    raw["operating_defaults"]["sweep_arm_speed"] = {
        ("FOO" if k == "PD1" else k): v
        for k, v in raw["operating_defaults"]["sweep_arm_speed"].items()
    }
    issues = validate_design(ScenarioConfig.model_validate(raw))
    canon = [i for i in issues if i.code == "NON_CANONICAL_STAGE_ID"]
    assert canon and canon[0].severity is Severity.WARNING
    assert not has_errors(issues)  # non-canonical ids are allowed (bound by role+order)


def test_physical_range_warnings() -> None:
    raw = _base_raw()
    raw["geometry"]["stages"][0]["diameter_m"] = 20.0  # absurd
    raw["operating_defaults"]["feed_flow_rate"] = 500.0  # absurd
    issues = validate_design(ScenarioConfig.model_validate(raw))
    codes = _codes(issues)
    assert "DIAMETER_RANGE" in codes
    assert "FEED_FLOW_RANGE" in codes
    # sanity-band violations never block assembly
    assert not has_errors(issues)
    assert set(warnings(issues)) == set(issues)


def test_error_filters() -> None:
    raw = _base_raw()
    for b in raw["topology"]["solid_transfers"]:
        if b["id"] == "SP1_TO_DR1":
            b["device_type"] = "CONTROLLED_GATE"
            b["vapor_seal"] = False
    issues = validate_design(ScenarioConfig.model_validate(raw))
    assert has_errors(issues)
    assert all(i.severity is Severity.ERROR for i in errors(issues))
