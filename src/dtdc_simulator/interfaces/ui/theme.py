"""Shared look-and-feel for the NiceGUI dashboard (BuildSpec §10) -- the flat
Siemens-inspired industrial palette (not literal trademarked assets, a
Siemens-petrol/graphite/flat-card look consistent with common HMI style), the
temperature heat gradient used to tint both the tower and its stage cards, and
the K<->C conversions every module needs at display time (BuildSpec §15: the
facade/model stay SI, only the UI converts).

`tower.py`/`dt_profiles.py`/`controls.py`/`app.py` all import from here rather
than each other, so there's a single source of truth for color and no
circular imports between the UI submodules.
"""

from __future__ import annotations

from nicegui import ui

TEAL = "#009999"  # MV slider / primary accent color
DARK = "#1B1B1B"
BG = "#F2F2F2"
BORDER = "#E0E0E0"
AMBER = "#F2A900"  # DV (disturbance) slider color -- theme's own "caution" hue
RED = "#E2001A"

# Zone colors for the DT axial-profile charts (PHZ/FTRZ/DCZ) -- kept in one
# place so a zone always reads as the same color across every chart.
ZONE_HEX = {
    "PHZ": "#f59e0b",
    "FTRZ": "#ef4444",
    "DCZ": "#3b82f6",
}

ROLE_STYLE = {
    "PREDESOLV": {"border": "border-amber-400", "badge": "amber"},
    "MAIN": {"border": "border-orange-500", "badge": "orange"},
    "SPARGE": {"border": "border-red-500", "badge": "red"},
    "DRYER": {"border": "border-blue-500", "badge": "blue"},
    "COOLER": {"border": "border-cyan-500", "badge": "cyan"},
}
FEED_BORDER = "border-slate-400"

# Cold -> hot gradient stops (feed inlet ~280 K, toasting stages ~400 K) used
# to tint each stage/cell by its live temperature, independent of its role
# color, so a viewer can spot the hottest/coldest points at a glance.
_HEAT_STOPS = [
    (0.0, (37, 99, 235)),  # blue-600
    (0.5, (250, 204, 21)),  # yellow-400
    (1.0, (220, 38, 38)),  # red-600
]
_HEAT_LO_K = 280.0
_HEAT_HI_K = 400.0

_K_OFFSET = 273.15


def k_to_c(k: float) -> float:
    return k - _K_OFFSET


def c_to_k(c: float) -> float:
    return c + _K_OFFSET


def heat_color(temp_k: float, alpha: float = 1.0) -> str:
    frac = max(0.0, min(1.0, (temp_k - _HEAT_LO_K) / (_HEAT_HI_K - _HEAT_LO_K)))
    for (f0, c0), (f1, c1) in zip(_HEAT_STOPS, _HEAT_STOPS[1:]):
        if frac <= f1:
            local = 0.0 if f1 == f0 else (frac - f0) / (f1 - f0)
            r = round(c0[0] + (c1[0] - c0[0]) * local)
            g = round(c0[1] + (c1[1] - c0[1]) * local)
            b = round(c0[2] + (c1[2] - c0[2]) * local)
            return f"rgba({r},{g},{b},{alpha})"
    r, g, b = _HEAT_STOPS[-1][1]
    return f"rgba({r},{g},{b},{alpha})"


def compact_metric(unit: str) -> ui.label:
    """A dense single-line `value unit` readout (vs. a label-above-value
    stack) so tray cards stay short enough that the whole tower fits a
    normal viewport without scrolling."""
    return ui.label(f"- {unit}").classes("font-mono text-xs whitespace-nowrap")


def inject_theme() -> None:
    ui.colors(
        primary=TEAL,
        secondary=DARK,
        accent=TEAL,
        positive=TEAL,
        warning=AMBER,
        negative=RED,
    )
    ui.add_head_html(f"""
    <style>
      body {{ background-color: {BG}; }}
      .q-card {{
        box-shadow: none !important;
        border: 1px solid {BORDER};
        border-radius: 4px;
      }}
      /* `label-always` floats its value pill above the thumb; without this
         gap it overlaps whatever label sits directly above the slider
         (every MV/DV slider in controls.py/tower.py places one there). The
         `dense` tray sliders (tower.py) pack tighter, so the pill needs even
         more clearance there than the regular ones (controls.py). */
      .q-slider {{ margin-top: 22px; }}
      .q-slider--dense {{ margin-top: 30px; }}
    </style>
    """)


def zone_bands(z_m: list[float], zone: list[str]) -> list[list[dict]]:
    """Group contiguous same-zone samples of a continuous (value-axis) DT
    axial profile into colored, NAME-LABELLED markArea bands (PHZ/FTRZ/DCZ),
    so the vapor and meal-bed profile charts visually mirror the DT's own
    physical layout AND say which regime each band is. The zone name is drawn
    at the TOP of each band (tray-boundary labels go at the bottom, see
    `tray_marklines`, so the two don't collide). `z_m`/`zone` are
    `DTAxialProfile`'s own parallel arrays."""
    bands: list[list[dict]] = []
    i = 0
    n = len(zone)
    while i < n:
        z = zone[i]
        j = i
        while j + 1 < n and zone[j + 1] == z:
            j += 1
        start = z_m[i - 1] if i > 0 else 0.0  # extend to the previous point: a seamless band
        color = ZONE_HEX.get(z, "#94a3b8")
        bands.append(
            [
                {
                    "yAxis": start,
                    "itemStyle": {"color": color, "opacity": 0.10},
                    "label": {
                        "show": True,
                        "formatter": z,
                        "position": "insideRight",
                        "color": color,
                        "fontSize": 9,
                        "fontWeight": "bold",
                    },
                },
                {"yAxis": z_m[j]},
            ]
        )
        i = j + 1
    return bands


def packed_heights(
    z_m: list[float], stage_id: list[str], level_pct: dict[str, float]
) -> list[float]:
    """Rescale the profile's GEOMETRIC axial height (each tray spanning its full
    bed_height) to the ACTUAL packed-solid depth, compressing every tray's
    segment by its live fill level (`level_pct`, from the holdup mass balance).
    The result's total = sum(level_i * bed_height_i), so the meal-bed profile's
    height axis tracks how full the trays actually are and shrinks/grows live as
    material backs up or drains. (The profile SHAPE within each tray comes from
    the last quasi-steady solve at the full bed height -- this only compresses
    the height coordinate, a display approximation, not a re-solve.)"""
    n = len(z_m)
    out = [0.0] * n
    packed_cum = 0.0
    i = 0
    while i < n:
        s = stage_id[i]
        j = i
        while j + 1 < n and stage_id[j + 1] == s:
            j += 1
        z_start = z_m[i - 1] if i > 0 else 0.0
        geo_span = z_m[j] - z_start
        frac = min(max(level_pct.get(s, 100.0) / 100.0, 0.0), 1.5)  # allow >100% (flood)
        packed_span = geo_span * frac
        for k in range(i, j + 1):
            local = (z_m[k] - z_start) / geo_span if geo_span > 0.0 else 0.0
            out[k] = packed_cum + local * packed_span
        packed_cum += packed_span
        i = j + 1
    return out


def tray_marklines(z_m: list[float], stage_id: list[str]) -> list[dict]:
    """Horizontal dashed markLine at each REAL TRAY boundary of the DT axial
    profile, labelled with the tray id (PD1/PD2/MN1/SP1) at the LEFT of the
    plot (zone-name labels sit at the right, see `zone_bands`, so the two don't
    collide). The charts plot height on the (inverted) Y axis, so tray
    boundaries are horizontal lines — a natural tower view. Derived from the
    profile's own per-cell `stage_id` transitions, so it stays correct as the
    PHZ/FTRZ/DCZ zone boundaries move across trays. `z_m`/`stage_id` are
    `DTAxialProfile`'s own parallel arrays."""
    lines: list[dict] = []
    i = 0
    n = len(stage_id)
    while i < n:
        s = stage_id[i]
        j = i
        while j + 1 < n and stage_id[j + 1] == s:
            j += 1
        start = z_m[i - 1] if i > 0 else 0.0
        lines.append(
            {
                "yAxis": start,
                "lineStyle": {"color": "#94a3b8", "type": "dashed", "width": 1},
                "label": {
                    "show": True,
                    "formatter": s,
                    "position": "insideStartTop",
                    "color": "#64748b",
                    "fontSize": 9,
                },
            }
        )
        i = j + 1
    return lines
