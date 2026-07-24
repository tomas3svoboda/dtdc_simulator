"""Equipment-envelope loading + canonical-name derivation (Phase 1 foundation)."""

from __future__ import annotations

import pytest

from dtdc_simulator.config.envelope import load_envelope
from dtdc_simulator.config.schema import StageRole


def test_envelope_loads_with_literature_caps() -> None:
    env = load_envelope()
    caps = {z.role: z.max_count for z in env.zones}
    assert caps[StageRole.PREDESOLV] == 7  # Kemper p.109
    assert caps[StageRole.MAIN] == 4  # Kemper p.111
    assert caps[StageRole.SPARGE] == 1  # Kemper p.111
    assert caps[StageRole.DRYER] == 3
    assert caps[StageRole.COOLER] == 2


def test_no_steam_dryer_zone() -> None:
    # This version does not model DC steam-drying trays (DECISIONS.md 2026-07-24).
    roles = {z.role for z in load_envelope().zones}
    assert roles == {
        StageRole.PREDESOLV,
        StageRole.MAIN,
        StageRole.SPARGE,
        StageRole.DRYER,
        StageRole.COOLER,
    }


def test_canonical_stage_ids_ordered_and_unique() -> None:
    ids = load_envelope().canonical_stage_ids()
    assert ids[:3] == ["PD1", "PD2", "PD3"]
    assert ids[-1] == "CL2"
    assert len(ids) == 17
    assert len(set(ids)) == len(ids)


def test_canonical_mv_keys_cover_actuators() -> None:
    keys = set(load_envelope().canonical_mv_keys())
    assert {"feed_flow_rate", "heated_air_temp", "heated_air_flow", "ambient_air_flow"} <= keys
    assert "indirect_steam/PD1" in keys
    assert "direct_steam/SP1" in keys
    assert "sweep_arm_speed/CL2" in keys
    assert "transfer_device_position/SP1" in keys
    # indirect steam is a DT-only actuator: no cooler slot
    assert "indirect_steam/CL1" not in keys


def test_control_tag_superset() -> None:
    env = load_envelope()
    tags = set(env.canonical_control_tags())
    assert "FIC_DT_FEED" in tags
    assert "FIC_DT_DIRECT_STM" in tags
    assert "ZIC_MN1" in tags  # one boundary loop per canonical stage outlet
    assert len(env.boundary_control_tags()) == 17
    # every tag has metadata (units/description)
    meta = env.control_loop_meta()
    assert set(meta) == tags


def test_min_le_max_validation() -> None:
    from dtdc_simulator.config.envelope import EnvelopeZone

    with pytest.raises(ValueError):
        EnvelopeZone(
            role=StageRole.MAIN,
            section="DT",
            id_prefix="MN",
            min_count=5,
            max_count=4,
            vapor_path="THROUGH_BED",
            stage_signals=["T"],
        )
