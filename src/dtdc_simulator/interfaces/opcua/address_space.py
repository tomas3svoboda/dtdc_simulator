"""Active-mask binding for the strict superset address space (Phase 1).

The OPC UA server renders the *equipment envelope* (``config/envelope.py``) into
a fixed node tree — every canonical stage/actuator/loop always present. This
module maps that fixed superset onto whatever scenario is currently assembled:

  * ``compute_active_mask(envelope, snapshot)`` decides which canonical slots are
    **active** (bound to a live build quantity) vs **placeholder**, and records
    the canonical→build key mapping used by the push/pull passes.
  * the ``placeholder_*`` / status helpers produce the ``Bad_NotConnected``
    ``DataValue`` a placeholder node carries, so a DCS client sees bad quality
    (and the sibling ``Present`` flag) instead of a stale or misleading number.

Binding is by (role, order): the k-th build stage of a role fills that zone's
k-th canonical slot, so the superset is stable even if a scenario uses
non-canonical stage ids. Pure logic + asyncua ``ua`` value helpers; no node I/O.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from asyncua import ua

from dtdc_simulator.config.envelope import EquipmentEnvelope
from dtdc_simulator.engine.facade import Snapshot

# Stage signal name -> the Outputs attribute (dict keyed by build stage id) it
# is sourced from. Shared by the server push and the GUI node browser.
STAGE_SIGNAL_ATTR = {
    "T": "stage_T",
    "X_hex": "stage_X_hex_ppm",
    "X_w": "stage_X_w_pct",
    "VaporTemp": "stage_vapor_temp",
    "Level": "stage_level_pct",
}

# A placeholder node reports this quality; per OPC UA convention its value reads
# back null. Reactivating simply writes a plain (Good) value over it.
_BAD = ua.StatusCode(ua.StatusCodes.BadNotConnected)


def placeholder(vtype: ua.VariantType, sentinel: object = None) -> ua.DataValue:
    """A ``Bad_NotConnected`` DataValue of the given variant type."""
    return ua.DataValue(Value=ua.Variant(sentinel, vtype), StatusCode=_BAD)


def placeholder_double() -> ua.DataValue:
    return placeholder(ua.VariantType.Double, 0.0)


def placeholder_string() -> ua.DataValue:
    return placeholder(ua.VariantType.String, "")


@dataclass
class ActiveMask:
    """Canonical→build resolution for one assembled scenario.

    Every dict is keyed by *canonical* name; the value is the matching *build*
    key when the slot is active, or ``None`` when it is a placeholder.
    """

    stage: dict[str, str | None] = field(default_factory=dict)
    mv: dict[str, str | None] = field(default_factory=dict)
    control: dict[str, str | None] = field(default_factory=dict)
    manifest_json: str = "{}"

    def stage_active(self, canonical_id: str) -> bool:
        return self.stage.get(canonical_id) is not None

    def active_stage_ids(self) -> list[str]:
        return [c for c, b in self.stage.items() if b is not None]


def compute_active_mask(envelope: EquipmentEnvelope, snapshot: Snapshot) -> ActiveMask:
    """Resolve the fixed superset against the currently assembled scenario."""
    # 1. Bind canonical stage slots to build stages by (role, order).
    build_by_role: dict[str, list[str]] = defaultdict(list)
    for sid in snapshot.stage_order:
        build_by_role[snapshot.stage_roles.get(sid, "")].append(sid)

    stage_map: dict[str, str | None] = {}
    for zone in envelope.zones:
        build_ids = build_by_role.get(zone.role.value, [])
        for i in range(1, zone.max_count + 1):
            cid = f"{zone.id_prefix}{i}"
            stage_map[cid] = build_ids[i - 1] if (i - 1) < len(build_ids) else None

    # 2. Resolve raw-MV keys. Per-stage keys follow their stage's binding;
    # transfer positions are keyed by canonical from-stage and resolve through
    # that stage's outgoing (controlled) boundary.
    boundary_from = {b.from_stage: b for b in snapshot.transfer_boundaries}
    build_mvs = snapshot.mvs
    mv_map: dict[str, str | None] = {}
    for ckey in envelope.canonical_mv_keys():
        if "/" not in ckey:  # unit-level MV, canonical key == build key
            mv_map[ckey] = ckey if ckey in build_mvs else None
            continue
        prefix, cstage = ckey.split("/", 1)
        bid = stage_map.get(cstage)
        if bid is None:
            mv_map[ckey] = None
            continue
        if prefix == "transfer_device_position":
            boundary = boundary_from.get(bid)
            bkey = f"transfer_device_position/{boundary.id}" if boundary is not None else None
            mv_map[ckey] = bkey if (bkey is not None and bkey in build_mvs) else None
        else:
            bkey = f"{prefix}/{bid}"
            mv_map[ckey] = bkey if bkey in build_mvs else None

    # 3. Resolve control loops. Fixed zone/unit tags are canonical already;
    # ZIC_<canonical_stage> resolves through the stage's controlled outlet.
    build_loops = snapshot.control_loops
    control_map: dict[str, str | None] = {}
    for loop in envelope.fixed_control_loops():
        control_map[loop.tag] = loop.tag if loop.tag in build_loops else None
    for cstage in envelope.canonical_stage_ids():
        ctag = f"{envelope.boundary_control_prefix}{cstage}"
        bid = stage_map.get(cstage)
        btag = None
        if bid is not None:
            boundary = boundary_from.get(bid)
            if boundary is not None:
                candidate = f"{envelope.boundary_control_prefix}{boundary.id}"
                if candidate in build_loops:
                    btag = candidate
        control_map[ctag] = btag

    # 4. Machine-readable manifest for Config/BuildManifest.
    manifest = {
        "envelope_version": envelope.version,
        "role_counts": {role: len(ids) for role, ids in sorted(build_by_role.items()) if role},
        "active_stages": [c for c, b in stage_map.items() if b is not None],
        "stage_binding": {c: b for c, b in stage_map.items() if b is not None},
        "active_control_loops": sorted(t for t, b in control_map.items() if b is not None),
    }
    return ActiveMask(
        stage=stage_map,
        mv=mv_map,
        control=control_map,
        manifest_json=json.dumps(manifest, sort_keys=True),
    )
