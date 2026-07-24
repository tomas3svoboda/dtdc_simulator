"""Design-realism validation (Phase 2) — a higher-level check on top of the
pydantic schema validators.

`config/schema.py` already enforces *structural* consistency (one outlet per
stage, a linear cascade in declaration order, steam keys reference the right
role, controlled transfers carry seed positions, the undersampling constraint).
This module adds *design-realism* rules the schema deliberately leaves open:

  * envelope conformance — per-zone tray counts within the literature caps
    (`envelope.yaml`), the canonical zone order (PREDESOLV→MAIN→SPARGE→DRYER→
    COOLER), and canonical stage ids;
  * process safety — the DT→DC transfer must be vapour-sealed (hexane vapour
    into the air-fluidised dryer is an explosion hazard);
  * physical sanity — tray dimensions / operating seeds inside industrial bands.

`validate_design` returns structured `DesignIssue`s (ERROR blocks, WARNING
informs) so the configuration wizard (Phase 4) can surface them live and gate
its "Assemble" button. Pure config layer: no engine/interfaces/asyncua imports.
Range bands are documented against Kemper (2019) / the equipment-envelope doc.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum

from dtdc_simulator.config.envelope import EquipmentEnvelope, load_envelope
from dtdc_simulator.config.schema import DC_ROLES, DT_ROLES, ScenarioConfig, StageRole


class Severity(str, Enum):
    ERROR = "ERROR"  # unrealistic/unsafe design — blocks assemble / wizard
    WARNING = "WARNING"  # allowed but flagged for the operator


@dataclass(frozen=True)
class DesignIssue:
    severity: Severity
    code: str  # stable machine code, e.g. "ZONE_CAP_EXCEEDED"
    message: str
    location: str = ""  # stage id / boundary id / field the issue attaches to


# --- physical-realism sanity bands (WARNING only) --------------------------
# bed_height_m is MAX HOLDUP CAPACITY (~2x the loaded depth), so these are ~2x
# Kemper (2019) p.112 loaded ranges: predesolv 150-300 mm, countercurrent/sparge
# 1000-1200 mm, DC drying/cooling ~250 mm.
_BED_HEIGHT_M: dict[StageRole, tuple[float, float]] = {
    StageRole.PREDESOLV: (0.1, 1.5),
    StageRole.MAIN: (0.3, 3.0),
    StageRole.SPARGE: (0.3, 3.0),
    StageRole.DRYER: (0.1, 1.5),
    StageRole.COOLER: (0.1, 1.5),
}
_DIAMETER_M = (1.0, 10.0)  # industrial DT/DC vessel range (sanity band)
_FEED_FLOW_KG_S = (1.0, 100.0)  # matches the engine MV domain (facade.MV_LIMITS)
_DIRECT_STEAM_KG_S = (3.0, 5.0)  # validated model domain (DECISIONS.md / MV_LIMITS)
_INDIRECT_STEAM_W = (0.0, 3.0e6)


def validate_design(
    config: ScenarioConfig, envelope: EquipmentEnvelope | None = None
) -> list[DesignIssue]:
    """Validate a (schema-valid) scenario against the equipment envelope and
    industrial-realism rules. Returns issues most-severe-first is NOT guaranteed;
    filter with `errors()` / `has_errors()`."""
    env = envelope if envelope is not None else load_envelope()
    issues: list[DesignIssue] = []
    stages = config.geometry.stages

    role_order = [z.role for z in env.zones]
    role_index = {role: i for i, role in enumerate(role_order)}
    caps = {z.role: (z.min_count, z.max_count) for z in env.zones}
    prefixes = {z.role: z.id_prefix for z in env.zones}

    _check_zone_counts(stages, caps, issues)
    _check_zone_order(stages, role_index, issues)
    _check_canonical_ids(stages, prefixes, issues)
    _check_vapor_seal(config, issues)
    _check_sparge_steam(config, issues)
    _check_physical_ranges(config, issues)
    return issues


def _check_zone_counts(stages, caps, issues: list[DesignIssue]) -> None:
    counts = Counter(s.role for s in stages)
    for role, (lo, hi) in caps.items():
        n = counts.get(role, 0)
        if n > hi:
            issues.append(
                DesignIssue(
                    Severity.ERROR,
                    "ZONE_CAP_EXCEEDED",
                    f"{role.value}: {n} trays exceeds the envelope cap of {hi}",
                    role.value,
                )
            )
        if n < lo:
            issues.append(
                DesignIssue(
                    Severity.ERROR,
                    "ZONE_BELOW_MIN",
                    f"{role.value}: {n} trays is below the required minimum of {lo}",
                    role.value,
                )
            )


def _check_zone_order(stages, role_index, issues: list[DesignIssue]) -> None:
    # Roles must appear top-to-bottom in canonical zone order (indices
    # non-decreasing): PREDESOLV -> MAIN -> SPARGE -> DRYER -> COOLER.
    seq = [(s.id, role_index[s.role]) for s in stages]
    for (prev_id, prev_i), (cur_id, cur_i) in zip(seq, seq[1:]):
        if cur_i < prev_i:
            issues.append(
                DesignIssue(
                    Severity.ERROR,
                    "ZONE_ORDER",
                    f"stage {cur_id} ({_role_at(role_index, cur_i)}) may not follow "
                    f"{prev_id} ({_role_at(role_index, prev_i)}): zones must run "
                    "PREDESOLV → MAIN → SPARGE → DRYER → COOLER",
                    cur_id,
                )
            )


def _role_at(role_index, i: int) -> str:
    for role, idx in role_index.items():
        if idx == i:
            return role.value
    return "?"


def _check_canonical_ids(stages, prefixes, issues: list[DesignIssue]) -> None:
    seen: dict[StageRole, int] = defaultdict(int)
    for stage in stages:
        seen[stage.role] += 1
        canonical = f"{prefixes[stage.role]}{seen[stage.role]}"
        if stage.id != canonical:
            issues.append(
                DesignIssue(
                    Severity.WARNING,
                    "NON_CANONICAL_STAGE_ID",
                    f"stage id '{stage.id}' is not the canonical id '{canonical}' for its "
                    f"(role, position); it will map to the '{canonical}' OPC UA node",
                    stage.id,
                )
            )


def _check_vapor_seal(config: ScenarioConfig, issues: list[DesignIssue]) -> None:
    role_of = {s.id: s.role for s in config.geometry.stages}
    for boundary in config.topology.solid_transfers:
        if boundary.to_stage is None:  # final product outlet
            if not boundary.vapor_seal:
                issues.append(
                    DesignIssue(
                        Severity.WARNING,
                        "PRODUCT_OUTLET_SEAL",
                        f"product outlet '{boundary.id}' is not vapour-sealed; a rotary "
                        "airlock / sealed dump hopper is standard practice",
                        boundary.id,
                    )
                )
            continue
        from_role = role_of.get(boundary.from_stage)
        to_role = role_of.get(boundary.to_stage)
        if from_role in DT_ROLES and to_role in DC_ROLES and not boundary.vapor_seal:
            issues.append(
                DesignIssue(
                    Severity.ERROR,
                    "DT_TO_DC_VAPOR_SEAL",
                    f"DT→DC transfer '{boundary.id}' must be vapour-sealed (rotary airlock): "
                    "solvent vapour entering the air-fluidised dryer is an explosion hazard",
                    boundary.id,
                )
            )


def _check_sparge_steam(config: ScenarioConfig, issues: list[DesignIssue]) -> None:
    direct = config.operating_defaults.direct_steam
    for stage in config.geometry.stages:
        if stage.role is StageRole.SPARGE and direct.get(stage.id, 0.0) <= 0.0:
            issues.append(
                DesignIssue(
                    Severity.WARNING,
                    "SPARGE_NO_DIRECT_STEAM",
                    f"sparge tray '{stage.id}' has no direct-steam seed; it supplies ~75% of "
                    "the desolventizing heat",
                    stage.id,
                )
            )


def _check_physical_ranges(config: ScenarioConfig, issues: list[DesignIssue]) -> None:
    for stage in config.geometry.stages:
        lo, hi = _BED_HEIGHT_M[stage.role]
        if not (lo <= stage.bed_height_m <= hi):
            issues.append(
                DesignIssue(
                    Severity.WARNING,
                    "BED_HEIGHT_RANGE",
                    f"{stage.id}: bed height {stage.bed_height_m} m is outside the typical "
                    f"{lo}-{hi} m (max-holdup) band for a {stage.role.value} tray",
                    stage.id,
                )
            )
        if not (_DIAMETER_M[0] <= stage.diameter_m <= _DIAMETER_M[1]):
            issues.append(
                DesignIssue(
                    Severity.WARNING,
                    "DIAMETER_RANGE",
                    f"{stage.id}: diameter {stage.diameter_m} m is outside the industrial "
                    f"{_DIAMETER_M[0]}-{_DIAMETER_M[1]} m range",
                    stage.id,
                )
            )

    od = config.operating_defaults
    if not (_FEED_FLOW_KG_S[0] <= od.feed_flow_rate <= _FEED_FLOW_KG_S[1]):
        issues.append(
            DesignIssue(
                Severity.WARNING,
                "FEED_FLOW_RANGE",
                f"feed flow {od.feed_flow_rate} kg/s is outside the "
                f"{_FEED_FLOW_KG_S[0]}-{_FEED_FLOW_KG_S[1]} kg/s model domain",
                "feed_flow_rate",
            )
        )
    for sid, value in od.direct_steam.items():
        if value > 0.0 and not (_DIRECT_STEAM_KG_S[0] <= value <= _DIRECT_STEAM_KG_S[1]):
            issues.append(
                DesignIssue(
                    Severity.WARNING,
                    "DIRECT_STEAM_RANGE",
                    f"{sid}: direct steam {value} kg/s is outside the validated "
                    f"{_DIRECT_STEAM_KG_S[0]}-{_DIRECT_STEAM_KG_S[1]} kg/s domain",
                    sid,
                )
            )
    for sid, value in od.indirect_steam.items():
        if not (_INDIRECT_STEAM_W[0] <= value <= _INDIRECT_STEAM_W[1]):
            issues.append(
                DesignIssue(
                    Severity.WARNING,
                    "INDIRECT_STEAM_RANGE",
                    f"{sid}: indirect steam {value} W is outside the "
                    f"0-{_INDIRECT_STEAM_W[1]:.0f} W band",
                    sid,
                )
            )


# --- convenience filters (for the wizard's gate) ---------------------------
def errors(issues: list[DesignIssue]) -> list[DesignIssue]:
    return [i for i in issues if i.severity is Severity.ERROR]


def warnings(issues: list[DesignIssue]) -> list[DesignIssue]:
    return [i for i in issues if i.severity is Severity.WARNING]


def has_errors(issues: list[DesignIssue]) -> bool:
    return any(i.severity is Severity.ERROR for i in issues)
