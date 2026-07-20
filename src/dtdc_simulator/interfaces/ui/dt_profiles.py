"""The DT's own axial profile charts -- vapor phase (T/flow/hexane/water) and
meal bed (T/hexane/moisture), both plotted against real height down the DT
(PHZ -> FTRZ -> DCZ), not the single lumped per-tray average the pre-redesign
dashboard showed. Source data is `Outputs.dt_axial_profile`
(`core/dt_solver.py::DTAxialProfile`), which only refreshes when `solve_dt`
reruns (`dt_resolve_interval_s` sim-time cadence, §7.9's quasi-steady map) --
hence the "updated Ns ago" badge instead of a continuously-moving trace.

Talks only to `RuntimeFacade`/`Snapshot` (BuildSpec §3): never imports `core/`
directly (the `outputs.dt_axial_profile` object it reads is just data handed
through the facade, same as every other `Outputs` field the UI already uses).
"""

from __future__ import annotations

from nicegui import ui

from dtdc_simulator.interfaces.ui import theme


def _chart(title: str, height_axis_name: str = "Height [m]") -> ui.echart:
    """One profile chart drawn as a TOWER strip: height runs down the (inverted)
    Y axis -- 0 m (DT top / feed) at the top, increasing downward -- and the
    plotted quantity runs along the X axis. This mirrors the physical vessel, so
    the vapor and meal-bed traces read top-to-bottom the way the tower actually
    stacks. `title` is a plain HTML caption above the chart (an ECharts axis
    `name` long enough to matter gets clipped by `containLabel`). The meal-bed
    charts pass `height_axis_name="Packed bed height [m]"` (they use the
    live-level-scaled packed depth, see `theme.packed_heights`)."""
    with ui.column().classes("gap-0 flex-1").style("min-width: 150px;"):
        ui.label(title).classes("text-xs text-gray-500 text-center w-full")
        chart = ui.echart(
            {
                "grid": {"containLabel": True, "top": 8, "bottom": 8, "left": 8, "right": 12},
                "yAxis": {
                    "type": "value",
                    "name": height_axis_name,
                    "inverse": True,  # DT top (z=0) at the top of the plot
                    "nameTextStyle": {"fontSize": 9},
                    "axisLabel": {"fontSize": 8, "hideOverlap": True},
                },
                # Narrow charts: shrink tick labels, thin them out, and drop any
                # that would still overlap (the hexane-ppm axis runs to ~5e5).
                "xAxis": {
                    "type": "value",
                    "scale": True,
                    "splitNumber": 3,
                    "axisLabel": {"fontSize": 8, "hideOverlap": True},
                },
                "series": [
                    {
                        "type": "line",
                        "showSymbol": False,
                        "lineStyle": {"width": 2, "color": theme.TEAL},
                        "itemStyle": {"color": theme.TEAL},
                        "data": [],
                        "markArea": {"data": []},  # physical tray slabs (+ tray-id labels, left)
                        "markLine": {  # PHZ/FTRZ/DCZ zone-boundary lines (no labels)
                            "symbol": "none",
                            "silent": True,
                            "data": [],
                        },
                    },
                    {
                        # Helper series (no data): carries the zone-name labels on
                        # a second markArea so they can coexist with the tray-slab
                        # markArea above and sit at each zone's own midpoint (right).
                        "type": "line",
                        "silent": True,
                        "data": [],
                        "markArea": {"data": []},
                    },
                ],
                "tooltip": {"trigger": "axis"},
            }
        ).classes("w-full h-80")
    return chart


class DTProfileView:
    """Static chart layout (doesn't depend on stage structure) + a `sync()`
    that refreshes data/zone-bands every tick from the latest snapshot."""

    def __init__(self, container: ui.column) -> None:
        with container:
            with ui.row().classes("items-center gap-2"):
                ui.label("DT Vapor Profile").classes("text-lg font-semibold")
                self._updated_badge = ui.label("").classes("text-xs text-gray-500")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                self._vapor_T = _chart("T [°C]")
                self._vapor_flow = _chart("Flow [kg/s]")
                self._vapor_hex = _chart("Hexane [%]")
                self._vapor_water = _chart("Water [%]")

            ui.label("DT Meal-Bed Profile").classes("text-lg font-semibold mt-2")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                self._solid_T = _chart("T [°C]", "Packed bed height [m]")
                self._solid_hex = _chart("Hexane [ppm]", "Packed bed height [m]")
                self._solid_moist = _chart("Moisture [%]", "Packed bed height [m]")

    def sync(self, snap) -> None:
        outputs = snap.outputs
        if outputs is None:
            return
        profile = outputs.dt_axial_profile
        zone = list(profile.zone)
        stage_id = list(profile.stage_id)
        age_s = snap.sim_time - outputs.dt_last_solve_sim_time
        self._updated_badge.text = f"(profile last resolved {age_s:.0f}s of sim time ago)"

        # Vapor charts: GEOMETRIC height (vapor fills the whole tray). Meal charts:
        # PACKED height, each tray compressed by its live fill level (theme.packed_heights),
        # so the bed-profile axis reads the actual packed solid depth and moves with holdup.
        z_geom = list(profile.z_m)
        z_packed = theme.packed_heights(z_geom, stage_id, outputs.stage_level_pct)
        # series[0]: profile trace + physical TRAY slabs (markArea) + ZONE-boundary
        # lines (markLine). series[1]: ZONE-name labels (markArea, right/midpoint).
        bands_geom = theme.tray_bands(z_geom, stage_id)
        lines_geom = theme.zone_lines(z_geom, zone)
        zlabels_geom = theme.zone_label_areas(z_geom, zone)
        bands_packed = theme.tray_bands(z_packed, stage_id)
        lines_packed = theme.zone_lines(z_packed, zone)
        zlabels_packed = theme.zone_label_areas(z_packed, zone)

        def _set(chart: ui.echart, xs: list[float], z, bands, lines, zlabels) -> None:
            # height on the (inverted) Y axis, quantity on X -> (value, height) pairs
            chart.options["series"][0]["data"] = list(zip(xs, z))
            chart.options["series"][0]["markArea"]["data"] = bands
            chart.options["series"][0]["markLine"]["data"] = lines
            chart.options["series"][1]["markArea"]["data"] = zlabels
            chart.update()

        def _set_vapor(chart: ui.echart, xs: list[float]) -> None:
            _set(chart, xs, z_geom, bands_geom, lines_geom, zlabels_geom)

        def _set_meal(chart: ui.echart, xs: list[float]) -> None:
            _set(chart, xs, z_packed, bands_packed, lines_packed, zlabels_packed)

        _set_vapor(self._vapor_T, [theme.k_to_c(v) for v in profile.vapor_T])
        _set_vapor(self._vapor_flow, list(profile.vapor_flow_kg_s))
        _set_vapor(self._vapor_hex, [v * 100.0 for v in profile.vapor_hexane_frac])
        _set_vapor(self._vapor_water, [v * 100.0 for v in profile.vapor_water_frac])

        _set_meal(self._solid_T, [theme.k_to_c(v) for v in profile.solid_T])
        _set_meal(self._solid_hex, [v * 1.0e6 for v in profile.solid_X2])
        _set_meal(self._solid_moist, [v * 100.0 for v in profile.solid_X1])
