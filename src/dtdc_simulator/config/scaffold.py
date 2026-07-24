"""Scenario scaffolding (Phase 4) — turn a compact `DesignSpec` (the handful of
choices the configuration wizard collects) into a complete, schema-valid
`ScenarioConfig`.

The wizard only asks for equipment layout + a few operating seeds; everything
else (canonical stage ids, the solids-transfer topology, numerical/model params)
is generated here from literature-faithful defaults. Stage ids and device types
are chosen so the result passes `config/design_rules.validate_design`:

  * ids are canonical (PD1.., MN1.., SP1, DR1.., CL1..);
  * within a zone: passive swept ports; DT zone crossings: controlled gates;
  * the DT→DC transfer and the product outlet: vapour-sealed rotary airlocks.

The model/sim parameter blocks are sourced from a validated template scenario
(default `scenarios/soybean_default.yaml`) since they are numerical/physics
settings the wizard should not expose. Pure config layer: no engine/interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from dtdc_simulator.config.loader import load_material_properties
from dtdc_simulator.config.schema import ScenarioConfig

DEFAULT_TEMPLATE = "scenarios/soybean_default.yaml"


def _c2k(celsius: float) -> float:
    return celsius + 273.15


@dataclass
class DesignSpec:
    """A DTDC unit design in operator-friendly units (temperatures in °C).

    Defaults reproduce the shipped soybean base case, so a wizard user who
    changes nothing gets a valid, sensible unit.
    """

    material: str = "soybean"

    # equipment layout (bounded by the envelope caps in the wizard)
    n_predesolv: int = 3
    n_main: int = 2
    n_sparge: int = 1
    n_dryer: int = 1
    n_cooler: int = 1

    # geometry defaults, per section / role
    dt_diameter_m: float = 6.0
    dc_diameter_m: float = 4.0
    predesolv_bed_m: float = 0.6
    main_bed_m: float = 1.2
    sparge_bed_m: float = 1.5
    dryer_bed_m: float = 0.6
    cooler_bed_m: float = 0.6

    # operating seeds
    feed_flow_rate: float = 25.0  # kg/s dry solid
    predesolv_indirect_total_w: float = 2.3e6  # split across predesolv trays
    main_indirect_total_w: float = 0.2e6  # split across main + sparge trays
    direct_steam_kg_s: float = 3.9  # sparge tray
    heated_air_temp_c: float = 75.0  # dryer inlet air
    heated_air_flow: float = 80.0  # kg/s, dryer
    ambient_air_flow: float = 250.0  # kg/s, cooler

    # feed / weather disturbances
    feed_temperature_c: float = 49.0
    feed_moisture: float = 0.124  # kg/kg dry solid
    feed_hexane: float = 0.388
    feed_oil: float = 0.0137
    ambient_air_temp_c: float = 25.0
    ambient_rh: float = 0.5  # 0-1

    start_empty: bool = False


@dataclass
class _StageDef:
    id: str
    role: str
    diameter_m: float
    bed_height_m: float


_DT_ROLES = {"PREDESOLV", "MAIN", "SPARGE"}
_DC_ROLES = {"DRYER", "COOLER"}


def _stages(spec: DesignSpec) -> list[_StageDef]:
    stages: list[_StageDef] = []
    for i in range(1, spec.n_predesolv + 1):
        stages.append(_StageDef(f"PD{i}", "PREDESOLV", spec.dt_diameter_m, spec.predesolv_bed_m))
    for i in range(1, spec.n_main + 1):
        stages.append(_StageDef(f"MN{i}", "MAIN", spec.dt_diameter_m, spec.main_bed_m))
    for i in range(1, spec.n_sparge + 1):
        stages.append(_StageDef(f"SP{i}", "SPARGE", spec.dt_diameter_m, spec.sparge_bed_m))
    for i in range(1, spec.n_dryer + 1):
        stages.append(_StageDef(f"DR{i}", "DRYER", spec.dc_diameter_m, spec.dryer_bed_m))
    for i in range(1, spec.n_cooler + 1):
        stages.append(_StageDef(f"CL{i}", "COOLER", spec.dc_diameter_m, spec.cooler_bed_m))
    return stages


def _transfer(from_id: str, from_role: str, to_id: str | None, to_role: str | None) -> dict:
    """Pick a solids-transfer device that matches plant practice and passes the
    design validator: sealed rotary airlock DT→DC and at the product outlet;
    controlled gate at DT zone crossings; passive swept port within a zone."""
    bid = f"{from_id}_TO_{to_id}" if to_id is not None else f"{from_id}_PRODUCT"
    if to_id is None or (from_role in _DT_ROLES and to_role in _DC_ROLES):
        return {
            "id": bid,
            "from_stage": from_id,
            "to_stage": to_id,
            "device_type": "ROTARY_AIRLOCK",
            "controlled": True,
            "vapor_seal": True,
        }
    if from_role in _DT_ROLES and to_role in _DT_ROLES and from_role != to_role:
        return {
            "id": bid,
            "from_stage": from_id,
            "to_stage": to_id,
            "device_type": "CONTROLLED_GATE",
            "controlled": True,
        }
    return {
        "id": bid,
        "from_stage": from_id,
        "to_stage": to_id,
        "device_type": "PASSIVE_SWEPT_PORT",
        "controlled": False,
        "fixed_position_pct": 50,
    }


def _split(total: float, ids: list[str]) -> dict[str, float]:
    if not ids:
        return {}
    share = total / len(ids)
    return {sid: share for sid in ids}


def scaffold_scenario(
    spec: DesignSpec, template_path: str | Path = DEFAULT_TEMPLATE
) -> ScenarioConfig:
    """Build a complete, schema-valid `ScenarioConfig` from a `DesignSpec`."""
    with Path(template_path).open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    stages = _stages(spec)
    pred_ids = [s.id for s in stages if s.role == "PREDESOLV"]
    toast_ids = [s.id for s in stages if s.role in ("MAIN", "SPARGE")]
    sparge_ids = [s.id for s in stages if s.role == "SPARGE"]

    # solids-transfer cascade: each stage -> the next, last -> product
    transfers: list[dict] = []
    for cur, nxt in zip(stages, stages[1:]):
        transfers.append(_transfer(cur.id, cur.role, nxt.id, nxt.role))
    transfers.append(_transfer(stages[-1].id, stages[-1].role, None, None))
    controlled_ids = [t["id"] for t in transfers if t.get("controlled")]

    raw["material"] = spec.material
    raw["physical"] = load_material_properties(spec.material).model_dump()
    raw["geometry"] = {
        "stages": [
            {"id": s.id, "role": s.role, "diameter_m": s.diameter_m, "bed_height_m": s.bed_height_m}
            for s in stages
        ]
    }
    raw["topology"] = {"solid_transfers": transfers}

    indirect = _split(spec.predesolv_indirect_total_w, pred_ids)
    indirect.update(_split(spec.main_indirect_total_w, toast_ids))
    raw["operating_defaults"] = {
        "feed_flow_rate": spec.feed_flow_rate,
        "indirect_steam": indirect,
        "direct_steam": {sid: spec.direct_steam_kg_s for sid in sparge_ids},
        "sweep_arm_speed": {s.id: 3.0 for s in stages},
        "transfer_device_position": {bid: 50.0 for bid in controlled_ids},
        "heated_air_temp": _c2k(spec.heated_air_temp_c),
        "heated_air_flow": spec.heated_air_flow,
        "ambient_air_flow": spec.ambient_air_flow,
        "dt_resolve_interval_s": raw.get("operating_defaults", {}).get(
            "dt_resolve_interval_s", 400.0
        ),
    }
    raw["disturbance_defaults"] = {
        "feed_temperature": _c2k(spec.feed_temperature_c),
        "feed_moisture": spec.feed_moisture,
        "feed_hexane": spec.feed_hexane,
        "feed_oil": spec.feed_oil,
        "ambient_air_temp": _c2k(spec.ambient_air_temp_c),
        "ambient_relative_humidity": spec.ambient_rh,
    }
    raw.setdefault("sim", {})["dt_start_empty"] = spec.start_empty

    return ScenarioConfig.model_validate(raw)
