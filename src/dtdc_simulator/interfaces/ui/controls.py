"""The compact global operator-panel slider strip: MVs the operator drives
(teal, `theme.TEAL`) and disturbance/error variables (amber, `theme.AMBER`) --
visually distinct colors so a viewer can tell "what I'm setting" from "what's
being thrown at the process" at a glance, per the redesign brief. Per-tray MVs
(indirect/direct steam, gate opening) live inline on the tower itself
(`tower.py`); this strip only holds the MVs that aren't per-tray, plus every DV.

Talks only to `RuntimeFacade` (BuildSpec §3); the one `config.schema` import
below is the same "read scenario defaults into a slider" pattern the
pre-redesign dashboard already used (`config/` is plain pydantic data, not
`core/`, so this doesn't violate the layering rule).
"""

from __future__ import annotations

from nicegui import ui

from dtdc_simulator.config.schema import ScenarioConfig
from dtdc_simulator.engine.facade import MV_LIMITS, RuntimeFacade
from dtdc_simulator.interfaces.ui import theme


class ControlsView:
    def __init__(self, facade: RuntimeFacade, container: ui.column) -> None:
        self._facade = facade
        with container:
            with ui.card().classes("w-full"):
                ui.label("Manipulated Variables").classes("text-sm font-semibold").style(
                    f"color:{theme.TEAL}"
                )
                with ui.row().classes("w-full gap-6 items-end flex-wrap"):
                    self.feed_flow_slider = self._mv_slider(
                        "feed_flow_rate", "Feed flow [kg/s]"
                    )
                    self.heated_air_temp_slider = self._mv_slider(
                        "heated_air_temp",
                        "Dryer air temp [°C]",
                        to_display=theme.k_to_c,
                        from_display=theme.c_to_k,
                    )
                    self.heated_air_flow_slider = self._mv_slider(
                        "heated_air_flow", "Dryer air flow [kg/s]"
                    )
                    self.ambient_air_flow_slider = self._mv_slider(
                        "ambient_air_flow", "Cooler air flow [kg/s]"
                    )

            with ui.card().classes("w-full"):
                ui.label("Disturbances / Error Variables").classes(
                    "text-sm font-semibold"
                ).style(f"color:{theme.AMBER}")
                with ui.row().classes("w-full gap-6 items-end flex-wrap"):
                    self.hexane_slider = self._dv_slider(
                        "feed_hexane", "Feed hexane [%]", 10, 50, 26, scale=100.0
                    )
                    self.moisture_slider = self._dv_slider(
                        "feed_moisture", "Feed moisture [%]", 5, 25, 7, scale=100.0
                    )
                    self.feed_temp_slider = self._dv_slider(
                        "feed_temperature",
                        "Feed temperature [°C]",
                        40,
                        80,
                        57,
                        to_si=theme.c_to_k,
                        from_si=theme.k_to_c,
                    )
                    self.ambient_air_temp_slider = self._dv_slider(
                        "ambient_air_temp",
                        "Ambient air temp [°C]",
                        -20,
                        45,
                        25,
                        to_si=theme.c_to_k,
                        from_si=theme.k_to_c,
                    )
                    self.ambient_humidity_slider = self._dv_slider(
                        "ambient_relative_humidity",
                        "Ambient relative humidity [%]",
                        0,
                        100,
                        50,
                        scale=100.0,
                    )

    def _mv_slider(self, key: str, label: str, to_display=None, from_display=None) -> ui.slider:
        lo, hi, _rate = MV_LIMITS[key]
        to_display = to_display or (lambda v: v)
        from_display = from_display or (lambda v: v)

        def on_change(e, k=key, conv=from_display) -> None:
            self._facade.set_mv_manual_setpoint(k, conv(e.value))

        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-xs text-gray-500")
            slider = (
                ui.slider(
                    min=to_display(lo),
                    max=to_display(hi),
                    value=to_display((lo + hi) / 2.0),
                    on_change=on_change,
                )
                .classes("w-48")
                .props("label-always color=primary")  # theme.inject_theme() maps primary->TEAL
            )
        return slider

    def _dv_slider(
        self,
        key: str,
        label: str,
        lo: float,
        hi: float,
        default: float,
        scale: float = 1.0,
        to_si=None,
        from_si=None,
    ) -> ui.slider:
        to_si = to_si or (lambda v: v / scale)

        def on_change(e, k=key, conv=to_si) -> None:
            self._facade.set_dv(k, conv(e.value))

        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-xs text-gray-500")
            slider = (
                ui.slider(min=lo, max=hi, value=default, on_change=on_change)
                .classes("w-48")
                .props("label-always color=warning")  # theme.inject_theme() maps warning->AMBER
            )
        return slider

    def apply_scenario_defaults(self, cfg: ScenarioConfig) -> None:
        """Seed every slider's displayed value from the just-loaded scenario
        (mirrors the pre-redesign dashboard's own `do_load()` behavior)."""
        od = cfg.operating_defaults
        dd = cfg.disturbance_defaults
        self.feed_flow_slider.value = od.feed_flow_rate
        self.heated_air_temp_slider.value = round(theme.k_to_c(od.heated_air_temp), 1)
        self.heated_air_flow_slider.value = od.heated_air_flow
        self.ambient_air_flow_slider.value = od.ambient_air_flow

        self.hexane_slider.value = round(dd.feed_hexane * 100.0, 1)
        self.moisture_slider.value = round(dd.feed_moisture * 100.0, 1)
        self.feed_temp_slider.value = round(theme.k_to_c(dd.feed_temperature), 1)
        self.ambient_air_temp_slider.value = round(theme.k_to_c(dd.ambient_air_temp), 1)
        self.ambient_humidity_slider.value = round(dd.ambient_relative_humidity * 100.0, 1)
