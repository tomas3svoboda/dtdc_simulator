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

# --- M4 (GUI redesign): HMI ink + surface + status tokens ---------------------
# The redesign keeps the flat Siemens-petrol/graphite look but adds the extra
# tokens a process-overview dashboard needs: text "ink" levels (values/labels
# wear these, never a series color), panel surfaces, a header gradient, and a
# RESERVED status palette (good/warning/critical) used ONLY for in-/out-of-spec
# state on KPI tiles + safety limits -- never as a data-series color.
INK = "#0F172A"  # primary text (slate-900)
MUTED = "#64748B"  # secondary/label text (slate-500)
PANEL = "#FFFFFF"  # card/panel surface
HEADER_FROM = "#0F2E2E"  # deep petrol -> graphite header gradient
HEADER_TO = "#1B1B1B"
WARN = AMBER  # RESERVED status palette (KpiTile out-of-spec tint) -- never a series color
CRIT = RED

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


class KpiTile:
    """One KPI band tile: a small graphite-on-white card with an uppercase
    label, a big mono value, and a unit. `set(value_text, status)` updates it;
    `status` (None|"warn"|"crit") tints ONLY the value + a left accent bar,
    from the RESERVED status palette -- the label/unit stay in ink so identity
    is never color-alone (dataviz: text wears text tokens, not series color)."""

    def __init__(self, title: str, unit: str) -> None:
        with ui.element("div").classes("kpi-tile") as card:
            self._card = card
            ui.label(title).classes("kpi-title")
            with ui.row().classes("items-baseline gap-1 no-wrap"):
                self._value = ui.label("--").classes("kpi-value")
                ui.label(unit).classes("kpi-unit")

    def set(self, value_text: str, status: str | None = None) -> None:
        self._value.text = value_text
        color = {"warn": WARN, "crit": CRIT}.get(status or "", INK)
        accent = {"warn": WARN, "crit": CRIT}.get(status or "", TEAL)
        self._value.style(replace=f"color:{color};")
        self._card.style(replace=f"border-left:3px solid {accent};")


def kpi_tile(title: str, unit: str) -> KpiTile:
    return KpiTile(title, unit)


def flow_arrow(down: bool = True, color: str = TEAL) -> ui.label:
    """A centered stream-flow connector for the tower schematic: a chevron
    pointing DOWN (solid falling tray->tray) or UP (vapor rising), with a
    live-updatable flow-rate caption returned to the caller. Purely visual --
    it reads `Outputs.stage_solid_out_kg_s` at sync time."""
    with ui.row().classes("w-full items-center justify-center gap-1").style("margin:-2px 0;"):
        ui.label("▼" if down else "▲").style(
            f"color:{color}; font-size:11px; line-height:1;"
        )
        caption = ui.label("").classes("font-mono").style(
            f"color:{MUTED}; font-size:10px; line-height:1;"
        )
    return caption


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
      body {{ background-color: {BG}; color: {INK}; }}
      .q-card {{
        box-shadow: none !important;
        border: 1px solid {BORDER};
        border-radius: 4px;
      }}
      /* --- M4 HMI process-overview tokens --- */
      .hmi-header {{
        background: linear-gradient(90deg, {HEADER_FROM}, {HEADER_TO});
      }}
      .hmi-panel {{
        background: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 10px 12px;
      }}
      .hmi-section-title {{
        font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
        text-transform: uppercase; color: {MUTED};
      }}
      .kpi-tile {{
        background: {PANEL};
        border: 1px solid {BORDER};
        border-left: 3px solid {TEAL};
        border-radius: 6px;
        padding: 6px 10px;
        min-width: 132px;
        display: flex; flex-direction: column; gap: 2px;
      }}
      .kpi-title {{
        font-size: 10px; font-weight: 600; letter-spacing: 0.04em;
        text-transform: uppercase; color: {MUTED}; white-space: nowrap;
      }}
      .kpi-value {{ font-family: monospace; font-size: 20px; font-weight: 700; line-height: 1.1; color: {INK}; }}
      .kpi-unit {{ font-size: 10px; color: {MUTED}; white-space: nowrap; }}
      /* `label-always` floats its value pill above the thumb; without this
         gap it overlaps whatever label sits directly above the slider
         (every MV/DV slider in controls.py/tower.py places one there). The
         `dense` tray sliders (tower.py) pack tighter, so the pill needs even
         more clearance there than the regular ones (controls.py). */
      .q-slider {{ margin-top: 22px; }}
      .q-slider--dense {{ margin-top: 30px; }}
    </style>
    """)


# Alternating subtle slab fills for the per-tray bands (neutral slate, so the
# physical trays read as distinct "decks" without competing with the data trace
# or the zone-boundary colors).
_TRAY_BAND_FILL = ("rgba(100,116,139,0.00)", "rgba(100,116,139,0.09)")


def tray_bands(z_m: list[float], stage_id: list[str]) -> list[list[dict]]:
    """Group the DT axial profile into one alternating-shaded markArea band per
    REAL TRAY (PD1/PD2/MN1/SP1/...), each labelled with its tray id -- so every
    physical vessel reads as a distinct horizontal deck on the profile (a
    natural tower view). Kept deliberately light (neutral alternating fill, one
    small left-side label) so it shows the trays without the heavier zone
    color-bands that used to fill the plot. Zone regime is annotated separately
    and lightly by `zone_lines` (right side), so the two don't collide.
    `z_m`/`stage_id` are `DTAxialProfile`'s own parallel arrays."""
    bands: list[list[dict]] = []
    i = 0
    n = len(stage_id)
    idx = 0
    while i < n:
        s = stage_id[i]
        j = i
        while j + 1 < n and stage_id[j + 1] == s:
            j += 1
        start = z_m[i - 1] if i > 0 else 0.0  # extend to the previous point: a seamless band
        bands.append(
            [
                {
                    "yAxis": start,
                    "itemStyle": {"color": _TRAY_BAND_FILL[idx % 2]},
                    "label": {
                        "show": True,
                        "formatter": s,
                        "position": "insideLeft",
                        "color": "#475569",
                        "fontSize": 9,
                        "fontWeight": "bold",
                    },
                },
                {"yAxis": z_m[j]},
            ]
        )
        idx += 1
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


def zone_lines(z_m: list[float], zone: list[str]) -> list[dict]:
    """A dashed colored markLine (no label) at each drying-zone TRANSITION
    (PHZ->FTRZ, FTRZ->DCZ) -- shows WHERE the regime changes; the zone NAMES are
    drawn separately by `zone_label_areas` (at each zone's midpoint) so a thin
    FTRZ zone doesn't make its boundary labels collide. `z_m`/`zone` are
    `DTAxialProfile`'s own parallel arrays."""
    out: list[dict] = []
    i = 0
    n = len(zone)
    first = True
    while i < n:
        z = zone[i]
        j = i
        while j + 1 < n and zone[j + 1] == z:
            j += 1
        if not first:
            color = ZONE_HEX.get(z, "#94a3b8")
            out.append(
                {
                    "yAxis": z_m[i - 1],
                    "lineStyle": {"color": color, "type": "dashed", "width": 1.2},
                    "label": {"show": False},
                }
            )
        first = False
        i = j + 1
    return out


def zone_label_areas(z_m: list[float], zone: list[str]) -> list[list[dict]]:
    """Transparent markArea per drying zone (PHZ/FTRZ/DCZ) carrying just the zone
    NAME, drawn on the RIGHT at each zone's vertical MIDPOINT (`insideRight`) --
    rendered on a dedicated helper series so it coexists with the tray-slab
    markArea, and so the names stay spread apart (opposite side from the LEFT
    tray labels) even when the FTRZ zone is thin. `z_m`/`zone` are
    `DTAxialProfile`'s own parallel arrays."""
    areas: list[list[dict]] = []
    i = 0
    n = len(zone)
    while i < n:
        z = zone[i]
        j = i
        while j + 1 < n and zone[j + 1] == z:
            j += 1
        start = z_m[i - 1] if i > 0 else 0.0
        color = ZONE_HEX.get(z, "#94a3b8")
        areas.append(
            [
                {
                    "yAxis": start,
                    "itemStyle": {"color": "transparent"},
                    "label": {
                        "show": True,
                        "formatter": z,
                        "position": "insideRight",
                        "color": color,
                        "fontSize": 10,
                        "fontWeight": "bold",
                    },
                },
                {"yAxis": z_m[j]},
            ]
        )
        i = j + 1
    return areas
