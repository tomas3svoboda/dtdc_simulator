"""Node-browser model for the OPC UA debug panel (Phase 3).

`browser_rows(envelope, snapshot)` returns a flat, filterable list of the
address space's integration-relevant leaf variables — every canonical stage
measurement, control loop, disturbance input and KPI — each tagged active vs
placeholder with its current value and OPC UA quality. This is derived purely
from the envelope + a facade snapshot (the same inputs the server push uses), so
the browser works whether or not the server is running and needs no OPC UA
client round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass

from dtdc_simulator.config.envelope import EquipmentEnvelope
from dtdc_simulator.engine.facade import Snapshot
from dtdc_simulator.interfaces.opcua.address_space import STAGE_SIGNAL_ATTR, compute_active_mask

_PLACEHOLDER = "—"


@dataclass(frozen=True)
class BrowserRow:
    category: str  # "Measurement" | "Control" | "Input" | "KPI"
    path: str  # canonical address-space path
    active: bool
    value: str  # formatted value, or "—" when placeholder
    quality: str  # "Good" | "Bad"


def _fmt(value: float) -> str:
    return f"{value:.4g}"


def _row(category: str, path: str, active: bool, value: str) -> BrowserRow:
    return BrowserRow(
        category, path, active, value if active else _PLACEHOLDER, "Good" if active else "Bad"
    )


def browser_rows(envelope: EquipmentEnvelope, snapshot: Snapshot) -> list[BrowserRow]:
    mask = compute_active_mask(envelope, snapshot)
    outputs = snapshot.outputs
    rows: list[BrowserRow] = []

    # Measurements/Stage/<CANON>/<signal>
    for stage in envelope.canonical_stages():
        cid = stage.canonical_id
        build_id = mask.stage.get(cid)
        active = build_id is not None and outputs is not None and build_id in outputs.stage_T
        for signal in stage.signals:
            value = _fmt(getattr(outputs, STAGE_SIGNAL_ATTR[signal])[build_id]) if active else ""
            rows.append(_row("Measurement", f"Measurements/Stage/{cid}/{signal}", active, value))

    # Measurements/KPI/<name>
    from dtdc_simulator.interfaces.opcua.server import _kpi_values

    kpi_map = _kpi_values(outputs) if outputs is not None else None
    for name in envelope.kpis:
        active = kpi_map is not None
        value = _fmt(kpi_map[name]) if active else ""
        rows.append(_row("KPI", f"Measurements/KPI/{name}", active, value))

    # Control/<tag>/{Mode,SP,PV,OP}
    for tag in envelope.canonical_control_tags():
        build_tag = mask.control.get(tag)
        active = build_tag is not None
        loop = snapshot.control_loops[build_tag] if active else None
        fields = (
            (
                ("Mode", loop.mode),
                ("SP", _fmt(loop.sp)),
                ("PV", _fmt(loop.pv)),
                ("OP", _fmt(loop.op)),
            )
            if loop is not None
            else (("Mode", ""), ("SP", ""), ("PV", ""), ("OP", ""))
        )
        for field, value in fields:
            rows.append(_row("Control", f"Control/{tag}/{field}", active, value))

    # SimulationInputs/<dv> (always active)
    for dv in envelope.disturbances:
        present = dv.key in snapshot.dvs
        value = _fmt(snapshot.dvs[dv.key]) if present else ""
        rows.append(_row("Input", f"SimulationInputs/{dv.key}", present, value))

    return rows
