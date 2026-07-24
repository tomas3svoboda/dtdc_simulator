"""Equipment envelope — the fixed maximal DTDC the strict OPC UA address space
and the design validator are built against (Phase 1 foundation).

The envelope is loaded from ``envelope.yaml`` (repo root). It is pure config
data + canonical-name derivation: it must never import ``engine/``,
``interfaces/`` or ``asyncua`` (BuildSpec §3, §15). The OPC UA superset renders
the *envelope* (not the loaded scenario), so a client's tag map is written once
against the canonical names below and never needs remapping when the plant is
reconfigured — a build only flips each node active vs placeholder.

Caps are literature-derived; see ``app_specifications/DTDC_Equipment_Envelope.md``
for the cited rationale (Kemper 2019, Ch. 4). DC steam-drying trays are
deliberately NOT modeled in this version (see that doc / DECISIONS.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from dtdc_simulator.config.schema import StageRole, VaporPath

DEFAULT_ENVELOPE_PATH = "envelope.yaml"

# Unit-level (not per-stage) manipulated variables, present in every build. The
# canonical MV key is the bare name (no "/<stage>" suffix). Kept in registry
# order (engine/facade.py) for determinism.
_UNIT_MV_ORDER = ("feed_flow_rate", "heated_air_temp", "heated_air_flow", "ambient_air_flow")

# Prefix of the per-boundary solids-transfer control loop (one canonical slot
# per canonical stage outlet). See EquipmentEnvelope.boundary_control_prefix.
_BOUNDARY_LOOP_PREFIX = "ZIC_"
_BOUNDARY_TEMPLATE_MARKER = "<"  # the yaml lists a "ZIC_<boundary>" template row


@dataclass(frozen=True)
class CanonicalStage:
    """One fixed stage slot in the superset (e.g. ``PD1``)."""

    canonical_id: str
    role: StageRole
    section: str
    prefix: str
    index: int  # 1-based within its zone
    vapor_path: VaporPath
    signals: tuple[str, ...]
    per_stage_actuators: tuple[str, ...]


class EnvelopeZone(BaseModel):
    role: StageRole
    section: str
    id_prefix: str = Field(min_length=1)
    min_count: int = Field(ge=0)
    max_count: int = Field(gt=0)
    vapor_path: VaporPath
    stage_signals: list[str] = Field(min_length=1)
    per_stage_actuators: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _min_le_max(self) -> "EnvelopeZone":
        if self.min_count > self.max_count:
            raise ValueError(
                f"zone {self.role.value}: min_count ({self.min_count}) > "
                f"max_count ({self.max_count})"
            )
        return self

    def canonical_ids(self) -> list[str]:
        return [f"{self.id_prefix}{i}" for i in range(1, self.max_count + 1)]


class EnvelopeActuator(BaseModel):
    key: str
    units: str = ""
    min: float | None = None
    max: float | None = None


class EnvelopeDisturbance(BaseModel):
    key: str
    units: str = ""


class EnvelopeControlLoop(BaseModel):
    tag: str
    units: str = ""
    active_when: str = "always"  # documentation only; runtime activity is snapshot-derived
    desc: str = ""

    @property
    def is_boundary_template(self) -> bool:
        return _BOUNDARY_TEMPLATE_MARKER in self.tag


class EquipmentEnvelope(BaseModel):
    """Top-level envelope document (``envelope.yaml``)."""

    version: int = Field(ge=1)
    zones: list[EnvelopeZone]
    unit_actuators: list[EnvelopeActuator] = Field(default_factory=list)
    disturbances: list[EnvelopeDisturbance] = Field(default_factory=list)
    kpis: list[str] = Field(default_factory=list)
    control_loops: list[EnvelopeControlLoop] = Field(default_factory=list)

    @field_validator("zones")
    @classmethod
    def _unique_roles_and_prefixes(cls, zones: list[EnvelopeZone]) -> list[EnvelopeZone]:
        if not zones:
            raise ValueError("envelope must declare at least one zone")
        roles = [z.role for z in zones]
        prefixes = [z.id_prefix for z in zones]
        if len(set(roles)) != len(roles):
            raise ValueError(f"duplicate zone roles: {roles}")
        if len(set(prefixes)) != len(prefixes):
            raise ValueError(f"duplicate zone id prefixes: {prefixes}")
        return zones

    # ---------------------------------------------------------- canonical names
    def canonical_stages(self) -> list[CanonicalStage]:
        """The fixed ordered list of stage slots (17 for the shipped envelope)."""
        stages: list[CanonicalStage] = []
        for zone in self.zones:
            for i in range(1, zone.max_count + 1):
                stages.append(
                    CanonicalStage(
                        canonical_id=f"{zone.id_prefix}{i}",
                        role=zone.role,
                        section=zone.section,
                        prefix=zone.id_prefix,
                        index=i,
                        vapor_path=zone.vapor_path,
                        signals=tuple(zone.stage_signals),
                        per_stage_actuators=tuple(zone.per_stage_actuators),
                    )
                )
        return stages

    def canonical_stage_ids(self) -> list[str]:
        return [s.canonical_id for s in self.canonical_stages()]

    def canonical_mv_keys(self) -> list[str]:
        """Every canonical raw-MV key the Diagnostics superset exposes, in a
        deterministic order: unit MVs, then per-stage actuators grouped by
        actuator kind. Per-boundary transfer positions are keyed by canonical
        *from-stage* (``transfer_device_position/<canonical_stage>``)."""
        keys: list[str] = list(_UNIT_MV_ORDER)
        stages = self.canonical_stages()
        # indirect_steam / direct_steam / sweep_arm_speed: one per stage that
        # declares the actuator.
        for actuator in ("indirect_steam", "direct_steam", "sweep_arm_speed"):
            for stage in stages:
                if actuator in stage.per_stage_actuators:
                    keys.append(f"{actuator}/{stage.canonical_id}")
        # transfer position: one canonical slot per stage outlet.
        for stage in stages:
            keys.append(f"transfer_device_position/{stage.canonical_id}")
        return keys

    def fixed_control_loops(self) -> list[EnvelopeControlLoop]:
        return [loop for loop in self.control_loops if not loop.is_boundary_template]

    @property
    def boundary_control_prefix(self) -> str:
        return _BOUNDARY_LOOP_PREFIX

    def boundary_control_tags(self) -> list[str]:
        """One canonical ``ZIC_<canonical_stage>`` per stage outlet."""
        return [f"{_BOUNDARY_LOOP_PREFIX}{sid}" for sid in self.canonical_stage_ids()]

    def canonical_control_tags(self) -> list[str]:
        return [loop.tag for loop in self.fixed_control_loops()] + self.boundary_control_tags()

    def control_loop_meta(self) -> dict[str, tuple[str, str]]:
        """Canonical control tag -> (engineering units, description). Static
        metadata known for every loop, active or placeholder, so an inactive
        loop still advertises what it would carry. The per-boundary ``ZIC_*``
        loops inherit the ``ZIC_<boundary>`` template row's units/description."""
        meta: dict[str, tuple[str, str]] = {}
        template: EnvelopeControlLoop | None = None
        for loop in self.control_loops:
            if loop.is_boundary_template:
                template = loop
            else:
                meta[loop.tag] = (loop.units, loop.desc)
        if template is not None:
            for tag in self.boundary_control_tags():
                meta[tag] = (template.units, template.desc)
        return meta


def load_envelope(path: str | Path = DEFAULT_ENVELOPE_PATH) -> EquipmentEnvelope:
    """Load and validate the equipment envelope from YAML."""
    with Path(path).open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return EquipmentEnvelope.model_validate(raw)
