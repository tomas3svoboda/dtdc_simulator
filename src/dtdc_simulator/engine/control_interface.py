"""PLC-facing SISO loop catalog over the simulator's internal MV registry.

The numerical model retains fine-grained internal actuator keys.  This module
groups and names them as plant-recognizable base-layer loops with the familiar
SP/PV/OP/Mode contract.  It contains no OPC UA or UI dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from dtdc_simulator.config.schema import ScenarioConfig, StageRole


class Aggregation(str, Enum):
    SINGLE = "SINGLE"
    TOTAL = "TOTAL"
    COMMON = "COMMON"


@dataclass(frozen=True)
class ControlBinding:
    tag: str
    description: str
    engineering_units: str
    mv_keys: tuple[str, ...]
    aggregation: Aggregation = Aggregation.SINGLE
    allocation_weights: tuple[float, ...] = ()
    display_scale: float = 1.0
    display_offset: float = 0.0

    def to_display(self, value: float) -> float:
        return value * self.display_scale + self.display_offset

    def from_display(self, value: float) -> float:
        return (value - self.display_offset) / self.display_scale


@dataclass(frozen=True)
class ControlLoopSnapshot:
    tag: str
    description: str
    engineering_units: str
    mode: str
    sp: float
    pv: float
    op: float
    minimum: float
    maximum: float
    status: str
    actuator_keys: tuple[str, ...]


def _weights(values: list[float]) -> tuple[float, ...]:
    positive = [max(value, 0.0) for value in values]
    total = sum(positive)
    if total <= 0.0:
        return tuple(1.0 / len(positive) for _ in positive)
    return tuple(value / total for value in positive)


def build_control_catalog(config: ScenarioConfig) -> tuple[ControlBinding, ...]:
    """Build a deterministic, scenario-specific PLC loop catalog.

    Zone steam and common-shaft loops keep fixed allocation weights from the
    validated operating seed.  A setpoint change therefore cannot silently
    rewrite the equipment split merely because an individual internal value
    happened to change during a previous run.
    """

    od = config.operating_defaults
    stages = config.geometry.stages
    pred_ids = [stage.id for stage in stages if stage.role is StageRole.PREDESOLV]
    toast_ids = [
        stage.id for stage in stages if stage.role in {StageRole.MAIN, StageRole.SPARGE}
    ]
    sparge_ids = [stage.id for stage in stages if stage.role is StageRole.SPARGE]
    all_ids = [stage.id for stage in stages]

    bindings: list[ControlBinding] = [
        ControlBinding(
            "FIC_DT_FEED",
            "Wet meal dry-solids feed flow",
            "kg/s",
            ("feed_flow_rate",),
        )
    ]

    def add_total(
        tag: str,
        description: str,
        units: str,
        keys: list[str],
        seeds: list[float],
        scale: float = 1.0,
    ) -> None:
        if keys:
            bindings.append(
                ControlBinding(
                    tag,
                    description,
                    units,
                    tuple(keys),
                    Aggregation.TOTAL,
                    _weights(seeds),
                    display_scale=scale,
                )
            )

    add_total(
        "FIC_DT_PD_IND_STM",
        "Predesolventizer indirect-steam duty",
        "kW",
        [f"indirect_steam/{stage_id}" for stage_id in pred_ids],
        [od.indirect_steam.get(stage_id, 0.0) for stage_id in pred_ids],
        1.0e-3,
    )
    add_total(
        "FIC_DT_MN_IND_STM",
        "Main/toast indirect-steam duty",
        "kW",
        [f"indirect_steam/{stage_id}" for stage_id in toast_ids],
        [od.indirect_steam.get(stage_id, 0.0) for stage_id in toast_ids],
        1.0e-3,
    )
    add_total(
        "FIC_DT_DIRECT_STM",
        "Sparge direct-steam mass flow",
        "kg/s",
        [f"direct_steam/{stage_id}" for stage_id in sparge_ids],
        [od.direct_steam.get(stage_id, 0.0) for stage_id in sparge_ids],
    )

    if all_ids:
        bindings.append(
            ControlBinding(
                "SIC_DT_SHAFT",
                "Common central-shaft speed",
                "rpm",
                tuple(f"sweep_arm_speed/{stage_id}" for stage_id in all_ids),
                Aggregation.COMMON,
            )
        )

    bindings.extend(
        [
            ControlBinding(
                "TIC_DC_DRY_AIR",
                "Dryer inlet-air temperature",
                "degC",
                ("heated_air_temp",),
                display_offset=-273.15,
            ),
            ControlBinding(
                "FIC_DC_DRY_AIR",
                "Dryer air mass flow",
                "kg/s",
                ("heated_air_flow",),
            ),
            ControlBinding(
                "FIC_DC_COOL_AIR",
                "Cooler air mass flow",
                "kg/s",
                ("ambient_air_flow",),
            ),
        ]
    )

    for boundary in config.topology.solid_transfers:
        if not boundary.controlled:
            continue
        bindings.append(
            ControlBinding(
                f"ZIC_{boundary.id}",
                f"Solids-transfer device position: {boundary.from_stage} to "
                f"{boundary.to_stage or 'product'}",
                "%",
                (f"transfer_device_position/{boundary.id}",),
            )
        )

    return tuple(bindings)
