"""NiceGUI adapter — setup screen + runtime dashboard (BuildSpec §10).

Talks only to `RuntimeFacade`; must not import `core/` (BuildSpec §3, §15).
The OPC UA server is started as an asyncio task inside NiceGUI's own event
loop (`app.on_startup`) so the whole process needs only one asyncio loop;
the tick loop itself runs in its own worker thread (BuildSpec §8.3).

The dashboard has two views:

  * "Overview" — a compact two-column tower schematic (DT: PREDESOLV/MAIN/
    SPARGE trays; DC: DRYER/COOLER, separate per the physical vessel split),
    each tray showing live T/hexane/moisture plus a bed-level bar, next
    to two profile charts that plot temperature and quality *along the
    tower* (x-axis = stage position) — how process engineers read a DTDC's
    state at a glance.
  * "Trends & Controls" — the temperature-vs-time history, MV drive control,
    and the full MV table, for anyone driving the process rather than just
    observing it.

All internal facade/model plumbing stays SI (Kelvin); this module converts
to °C only at display time (see `_k_to_c`/`_c_to_k`).
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque

from nicegui import app, ui

from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.engine.mv import Mode
from dtdc_simulator.engine.state_machine import SimState

logger = logging.getLogger(__name__)

HISTORY_LEN = 300
SYNC_INTERVAL_S = 0.3
OUTLET_WINDOW_S = 3600.0  # moving window for the outlet quality trend chart

_KPI_TILES = (
    ("kpi_residual_hexane_ppm", "Residual hexane [ppm]", "{:.0f}"),
    ("kpi_meal_moisture_pct", "Meal moisture [%]", "{:.2f}"),
    ("kpi_steam_consumption_kg_per_t", "Steam [kg/t]", "{:.2f}"),
    ("kpi_throughput_t_per_day", "Throughput [t/day]", "{:.1f}"),
)

DT_ROLES = ("PREDESOLV", "MAIN", "SPARGE")
DC_ROLES = ("DRYER", "COOLER")

_ROLE_STYLE = {
    "PREDESOLV": {"border": "border-amber-400", "badge": "amber"},
    "MAIN": {"border": "border-orange-500", "badge": "orange"},
    "SPARGE": {"border": "border-red-500", "badge": "red"},
    "DRYER": {"border": "border-blue-500", "badge": "blue"},
    "COOLER": {"border": "border-cyan-500", "badge": "cyan"},
}
_FEED_BORDER = "border-slate-400"

# Hex colors shared by the tower's role badges/borders and the profile
# charts' background bands, so the same role always reads as the same color
# across both representations.
_ROLE_HEX = {
    "PREDESOLV": "#f59e0b",
    "MAIN": "#f97316",
    "SPARGE": "#ef4444",
    "DRYER": "#3b82f6",
    "COOLER": "#06b6d4",
    "FEED": "#94a3b8",
    "": "#94a3b8",
}

# Cold -> hot gradient stops (feed inlet ~280 K, toasting stages ~400 K) used
# to tint each stage card by its live temperature, independent of its role
# color, so a viewer can spot the hottest/coldest points at a glance.
_HEAT_STOPS = [
    (0.0, (37, 99, 235)),  # blue-600
    (0.5, (250, 204, 21)),  # yellow-400
    (1.0, (220, 38, 38)),  # red-600
]
_HEAT_LO_K = 280.0
_HEAT_HI_K = 400.0

# Siemens-inspired flat industrial palette (not literal trademarked assets —
# a Siemens-petrol/graphite/flat-card look consistent with common HMI style).
_SIEMENS_TEAL = "#009999"
_SIEMENS_DARK = "#1B1B1B"
_SIEMENS_BG = "#F2F2F2"
_SIEMENS_BORDER = "#E0E0E0"
_SIEMENS_AMBER = "#F2A900"
_SIEMENS_RED = "#E2001A"

_K_OFFSET = 273.15


def _k_to_c(k: float) -> float:
    return k - _K_OFFSET


def _c_to_k(c: float) -> float:
    return c + _K_OFFSET


# Per-MV-key display unit map: a handful of MVs are stored in Kelvin (SI)
# internally but should read in °C in the generic MV table / drive control.
_MV_UNITS = {
    "feed_flow_rate": "kg/s",
    "heated_air_temp": "°C",
    "heated_air_flow": "kg/s",
    "ambient_air_temp": "°C",
    "ambient_air_flow": "kg/s",
    "indirect_steam": "W",
    "direct_steam": "kg/s",
    "sweep_arm_speed": "rpm",
    "gate_opening": "%",
}
_MV_TEMP_K_PREFIXES = {"heated_air_temp", "ambient_air_temp"}


def _mv_prefix(key: str) -> str:
    return key.split("/", 1)[0]


def _mv_unit(key: str) -> str:
    return _MV_UNITS.get(_mv_prefix(key), "")


def _mv_to_display(key: str, value: float) -> float:
    return _k_to_c(value) if _mv_prefix(key) in _MV_TEMP_K_PREFIXES else value


def _mv_from_display(key: str, value: float) -> float:
    return _c_to_k(value) if _mv_prefix(key) in _MV_TEMP_K_PREFIXES else value


def _heat_color(temp_k: float, alpha: float = 1.0) -> str:
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


def _role_bands(roles: list[str]) -> list[list[dict]]:
    """Group contiguous same-role x-axis positions into colored markArea bands
    so the profile charts visually mirror the tower's stage layout."""
    bands: list[list[dict]] = []
    i = 0
    n = len(roles)
    while i < n:
        role = roles[i]
        j = i
        while j + 1 < n and roles[j + 1] == role:
            j += 1
        color = _ROLE_HEX.get(role, "#94a3b8")
        bands.append(
            [
                {"xAxis": i - 0.5, "itemStyle": {"color": color, "opacity": 0.10}},
                {"xAxis": j + 0.5},
            ]
        )
        i = j + 1
    return bands


def _compact_metric(unit: str) -> ui.label:
    """A dense single-line `value unit` readout (vs. a label-above-value
    stack) so tray cards stay short enough that the whole tower fits a
    normal viewport without scrolling."""
    return ui.label(f"- {unit}").classes("font-mono text-xs whitespace-nowrap")


def _inject_siemens_theme() -> None:
    ui.colors(
        primary=_SIEMENS_TEAL,
        secondary=_SIEMENS_DARK,
        accent=_SIEMENS_TEAL,
        positive=_SIEMENS_TEAL,
        warning=_SIEMENS_AMBER,
        negative=_SIEMENS_RED,
    )
    ui.add_head_html(f"""
    <style>
      body {{ background-color: {_SIEMENS_BG}; }}
      .q-card {{
        box-shadow: none !important;
        border: 1px solid {_SIEMENS_BORDER};
        border-radius: 4px;
      }}
    </style>
    """)


def create_app(
    facade: RuntimeFacade, default_scenario: str, opcua_endpoint: str | None = None
) -> None:
    """Register the OPC UA startup task (if any) and the single dashboard page.
    Caller is responsible for calling `ui.run(...)` afterward."""

    if opcua_endpoint:
        from dtdc_simulator.interfaces.opcua.server import serve as opcua_serve
        import asyncio

        async def _start_opcua() -> None:
            while facade.state in (SimState.UNCONFIGURED, SimState.CONFIGURED):
                await asyncio.sleep(0.2)  # wait for setup to assemble MV/DV/stage keys
            try:
                await opcua_serve(facade, opcua_endpoint)
            except Exception:
                logger.exception("OPC UA server crashed")

        app.on_startup(_start_opcua)

    @ui.page("/")
    def index() -> None:
        history_t: deque[float] = deque(maxlen=HISTORY_LEN)
        history_T: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
        # (sim_time, value) pairs, trimmed to a rolling OUTLET_WINDOW_S regardless of
        # sample count — unlike history_T above, count alone doesn't bound this to a
        # fixed sim-time span since speed_factor (and thus sim-seconds/tick) can change.
        outlet_hex_history: deque[tuple[float, float]] = deque()
        outlet_moisture_history: deque[tuple[float, float]] = deque()

        _inject_siemens_theme()

        with (
            ui.header()
            .classes("items-center justify-between px-4")
            .style(f"background-color: {_SIEMENS_DARK};")
        ):
            with ui.column().classes("gap-0"):
                ui.label("DTDC Real-Time Simulator").classes("text-xl font-bold text-white")
                ui.label("Digital Twin — Desolventizer / Toaster / Dryer / Cooler").classes(
                    "text-xs text-gray-300"
                )
            # Lets the process be stopped from the browser instead of only via an
            # external kill/Task Manager -- otherwise a stray dashboard process
            # keeps holding its port and the next `main.py` launch fails to bind.
            with ui.dialog() as shutdown_dialog, ui.card():
                ui.label("Shut down the DTDC simulator process?")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=shutdown_dialog.close).props("flat")
                    ui.button(
                        "Shut down", on_click=lambda: (ui.notify("Shutting down…"), app.shutdown())
                    ).props("color=negative")
            ui.button("Shutdown", on_click=shutdown_dialog.open).props(
                "flat color=negative icon=power_settings_new"
            )

        setup_container = ui.column().classes("w-full gap-4 p-4")
        dashboard_container = ui.column().classes("w-full gap-4 p-4")
        dashboard_container.visible = False

        with setup_container, ui.card().classes("w-full max-w-2xl"):
            ui.label("Setup").classes("text-lg font-semibold")
            path_input = ui.input("Scenario YAML path", value=default_scenario).classes("w-full")
            start_empty_checkbox = ui.checkbox(
                "Start empty (watch material propagate through the unit)"
            )
            error_label = ui.label("").classes("text-red-600")

            def do_load() -> None:
                try:
                    cfg = load_scenario(path_input.value)
                    cfg.sim.dt_start_empty = start_empty_checkbox.value
                    facade.configure(cfg)
                    facade.assemble()
                    error_label.text = ""
                    hexane_slider.value = round(cfg.disturbance_defaults.feed_hexane * 100.0, 1)
                    moisture_slider.value = round(cfg.disturbance_defaults.feed_moisture * 100.0, 1)
                    feed_temp_slider.value = round(
                        _k_to_c(cfg.disturbance_defaults.feed_temperature), 1
                    )
                    resolve_interval_input.value = cfg.operating_defaults.dt_resolve_interval_s
                except (
                    Exception
                ) as exc:  # noqa: BLE001 - surface load/validation errors to the user
                    error_label.text = f"{type(exc).__name__}: {exc}"

            ui.button("Validate & Assemble", on_click=do_load)

        with dashboard_container:
            with ui.row().classes("w-full items-center gap-4"):
                state_label = ui.label()
                sim_time_label = ui.label()
                speed_label = ui.label()
                undersample_badge = ui.badge("UNDERSAMPLE").props("color=warning outline")
                undersample_badge.visible = False
                solver_badge = ui.badge("SOLVER STRESS").props("color=negative outline")
                solver_badge.visible = False

            with ui.row().classes("w-full items-center gap-4"):
                ui.button("Run", on_click=facade.run)
                ui.button("Pause", on_click=facade.pause)
                ui.button("Stop", on_click=facade.stop)
                ui.button("Reset", on_click=facade.reset)
                ui.button("Reconfigure", on_click=lambda: facade.reconfigure()).props("outline")
                ui.label("Speed factor")
                ui.slider(
                    min=0,
                    max=30,
                    step=0.5,
                    value=20.0,
                    on_change=lambda e: facade.set_speed_factor(float(e.value)),
                ).classes("w-48").props("label-always")
                # M3a follow-up ("C"): live-tunable DT resolve cadence -- min=120
                # matches the schema/facade-enforced floor (config/schema.py's
                # OperatingDefaults.dt_resolve_interval_s, engine/facade.py's
                # set_dt_resolve_interval_s). The label makes the resulting
                # wall-clock gap (dt_resolve_interval_s/speed_factor) visible
                # live instead of hidden in a YAML file -- directly what was
                # asked for ("write somewhere what the time discretization
                # step is").
                ui.label("DT resolve interval [s]")
                resolve_interval_input = ui.number(
                    min=120,
                    step=10,
                    value=400.0,
                    on_change=lambda e: facade.set_dt_resolve_interval_s(float(e.value)),
                ).classes("w-24")
                resolve_gap_label = ui.label().classes("text-xs text-gray-500")
                ui.label("Global mode")
                ui.toggle(
                    {Mode.MANUAL.value: "MANUAL", Mode.AUTO.value: "AUTO"},
                    value=Mode.MANUAL.value,
                    on_change=lambda e: facade.set_global_mode(Mode(e.value)),
                )

            with ui.card().classes("w-full"):
                ui.label("Feed Conditions").classes("text-sm font-semibold text-gray-600")
                with ui.row().classes("w-full gap-8 items-center flex-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label("Hexane [%]").classes("text-xs text-gray-500")
                        hexane_slider = (
                            ui.slider(
                                min=10,
                                max=50,
                                step=1,
                                value=26,
                                on_change=lambda e: facade.set_dv(
                                    "feed_hexane", float(e.value) / 100.0
                                ),
                            )
                            .classes("w-48")
                            .props("label-always")
                        )
                    with ui.column().classes("gap-0"):
                        ui.label("Moisture [%]").classes("text-xs text-gray-500")
                        moisture_slider = (
                            ui.slider(
                                min=5,
                                max=25,
                                step=1,
                                value=7,
                                on_change=lambda e: facade.set_dv(
                                    "feed_moisture", float(e.value) / 100.0
                                ),
                            )
                            .classes("w-48")
                            .props("label-always")
                        )
                    with ui.column().classes("gap-0"):
                        ui.label("Feed temperature [°C]").classes("text-xs text-gray-500")
                        feed_temp_slider = (
                            ui.slider(
                                min=40,
                                max=80,
                                step=1,
                                value=57,
                                on_change=lambda e: facade.set_dv(
                                    "feed_temperature", _c_to_k(float(e.value))
                                ),
                            )
                            .classes("w-48")
                            .props("label-always")
                        )

            with ui.row().classes("w-full gap-4"):
                with ui.card().classes("w-48"):
                    ui.label("Feed flow [kg/s]").classes("text-xs text-gray-500")
                    feed_flow_kpi = ui.label("-").classes("text-xl font-mono")
                kpi_labels: dict[str, ui.label] = {}
                for key, title, _fmt in _KPI_TILES:
                    with ui.card().classes("w-48"):
                        ui.label(title).classes("text-xs text-gray-500")
                        kpi_labels[key] = ui.label("-").classes("text-xl font-mono")

            with ui.tabs().classes("w-full") as view_tabs:
                overview_tab = ui.tab("Overview")
                trends_tab = ui.tab("Trends & Controls")

            with ui.tab_panels(view_tabs, value=overview_tab).classes("w-full"):
                with ui.tab_panel(overview_tab):
                    with ui.row().classes("w-full gap-4 items-start flex-wrap"):
                        # ---- left: compact 2-column tower (DT | DC) ----
                        with ui.column().classes("gap-2").style("flex: 0 0 660px"):
                            with ui.row().classes("items-center justify-between w-full"):
                                ui.label("Tower").classes("text-lg font-semibold")
                                with ui.row().classes("items-center gap-2"):
                                    ui.label("Cold").classes("text-xs text-gray-500")
                                    ui.element("div").style(
                                        "height:10px; width:120px; border-radius:4px; "
                                        "background: linear-gradient(to right, "
                                        "rgb(37,99,235), rgb(250,204,21), rgb(220,38,38));"
                                    )
                                    ui.label("Hot").classes("text-xs text-gray-500")
                            # Bounded + internally scrollable: the tower's own trays are at
                            # most one small scroll away, but the rest of the dashboard
                            # (controls, KPIs, charts) never needs the page to scroll for it.
                            with (
                                ui.row()
                                .classes("w-full gap-3 items-start")
                                .style("max-height: 640px; overflow-y: auto;")
                            ):
                                with ui.column().classes("gap-1").style("flex: 1 1 0%"):
                                    ui.label("DT — Desolventizer / Toaster").classes(
                                        "text-xs font-semibold text-gray-600 uppercase"
                                    )
                                    dt_column = ui.column().classes("w-full gap-1")
                                with ui.column().classes("gap-1").style("flex: 1 1 0%"):
                                    ui.label("DC — Dryer / Cooler").classes(
                                        "text-xs font-semibold text-gray-600 uppercase"
                                    )
                                    dc_column = ui.column().classes("w-full gap-1")

                        # ---- right: spatial profiles along the tower ----
                        with ui.column().classes("gap-4").style("flex: 1 1 0%; min-width: 420px"):
                            ui.label("Temperature Profile Along Tower").classes(
                                "text-lg font-semibold"
                            )
                            temp_profile_plot = ui.echart(
                                {
                                    "grid": {"containLabel": True},
                                    "xAxis": {"type": "category", "data": []},
                                    "yAxis": {
                                        "type": "value",
                                        "name": "T [°C]",
                                        "scale": True,
                                        "minInterval": 1,
                                    },
                                    "series": [
                                        {
                                            "name": "Temperature",
                                            "type": "line",
                                            "areaStyle": {"opacity": 0.08},
                                            "smooth": True,
                                            "data": [],
                                            "markArea": {"data": []},
                                        }
                                    ],
                                    "tooltip": {"trigger": "axis"},
                                }
                            ).classes("w-full h-56")

                            ui.label("Quality Profiles Along Tower").classes(
                                "text-lg font-semibold"
                            )
                            hexane_profile_plot = ui.echart(
                                {
                                    "grid": {"containLabel": True},
                                    "xAxis": {"type": "category", "data": []},
                                    "yAxis": {
                                        "type": "value",
                                        "name": "Hexane [ppm]",
                                        "scale": True,
                                        "splitNumber": 3,
                                    },
                                    "series": [
                                        {
                                            "name": "Hexane",
                                            "type": "line",
                                            "itemStyle": {"color": "#3b5bdb"},
                                            "areaStyle": {"opacity": 0.08},
                                            "data": [],
                                            "markArea": {"data": []},
                                        }
                                    ],
                                    "tooltip": {"trigger": "axis"},
                                }
                            ).classes("w-full h-48")

                            moisture_profile_plot = ui.echart(
                                {
                                    "grid": {"containLabel": True},
                                    "xAxis": {"type": "category", "data": []},
                                    "yAxis": {
                                        "type": "value",
                                        "name": "Moisture [%]",
                                        "scale": True,
                                        "splitNumber": 3,
                                    },
                                    "series": [
                                        {
                                            "name": "Moisture",
                                            "type": "line",
                                            "itemStyle": {"color": "#94a300"},
                                            "areaStyle": {"opacity": 0.08},
                                            "data": [],
                                            "markArea": {"data": []},
                                        }
                                    ],
                                    "tooltip": {"trigger": "axis"},
                                }
                            ).classes("w-full h-48")

                with ui.tab_panel(trends_tab):
                    with ui.column().classes("w-full gap-4"):
                        ui.label("Stage Temperature Trend (time history)").classes(
                            "text-lg font-semibold"
                        )
                        plot = ui.echart(
                            {
                                "xAxis": {"type": "value", "name": "sim time [s]"},
                                "yAxis": {
                                    "type": "value",
                                    "name": "T [°C]",
                                    "scale": True,
                                    "minInterval": 1,
                                },
                                "series": [],
                                "legend": {"data": []},
                                "tooltip": {"trigger": "axis"},
                            }
                        ).classes("w-full h-64")

                        ui.label("Outlet Quality Trend (last hour)").classes(
                            "text-lg font-semibold"
                        )
                        outlet_trend_plot = ui.echart(
                            {
                                "xAxis": {"type": "value", "name": "sim time [s]"},
                                "yAxis": [
                                    {"type": "value", "name": "Hexane [ppm]", "scale": True},
                                    {"type": "value", "name": "Moisture [%]", "scale": True},
                                ],
                                "series": [
                                    {
                                        "name": "Outlet Hexane",
                                        "type": "line",
                                        "yAxisIndex": 0,
                                        "showSymbol": False,
                                        "data": [],
                                    },
                                    {
                                        "name": "Outlet Moisture",
                                        "type": "line",
                                        "yAxisIndex": 1,
                                        "showSymbol": False,
                                        "data": [],
                                    },
                                ],
                                "legend": {"data": ["Outlet Hexane", "Outlet Moisture"]},
                                "tooltip": {"trigger": "axis"},
                            }
                        ).classes("w-full h-64")

                        ui.label("Drive a manipulated variable").classes("text-lg font-semibold")
                        with ui.row().classes("w-full items-center gap-4"):
                            mv_select = ui.select(options=[], label="MV key").classes("w-64")
                            mv_mode_toggle = ui.toggle(
                                {Mode.MANUAL.value: "MANUAL", Mode.AUTO.value: "AUTO"},
                                value=Mode.MANUAL.value,
                            )
                            mv_setpoint_input = ui.number(
                                label="Manual setpoint", value=0.0
                            ).classes("w-40")

                            def _update_setpoint_label() -> None:
                                key = mv_select.value
                                unit = _mv_unit(key) if key else ""
                                label = f"Manual setpoint [{unit}]" if unit else "Manual setpoint"
                                mv_setpoint_input.props(f"label='{label}'")

                            mv_select.on_value_change(_update_setpoint_label)

                            def apply_mv() -> None:
                                key = mv_select.value
                                if not key:
                                    return
                                facade.set_mv_mode(key, Mode(mv_mode_toggle.value))
                                raw = float(mv_setpoint_input.value or 0.0)
                                facade.set_mv_manual_setpoint(key, _mv_from_display(key, raw))

                            ui.button("Apply", on_click=apply_mv)

                        ui.label("Manipulated Variables").classes("text-lg font-semibold")
                        mv_table = ui.table(
                            columns=[
                                {"name": "key", "label": "MV", "field": "key", "align": "left"},
                                {"name": "mode", "label": "Mode", "field": "mode"},
                                {"name": "manual", "label": "Manual SP", "field": "manual"},
                                {"name": "auto", "label": "Auto SP", "field": "auto"},
                                {
                                    "name": "effective",
                                    "label": "Effective",
                                    "field": "effective",
                                },
                                {"name": "limits", "label": "Limits", "field": "limits"},
                            ],
                            rows=[],
                            row_key="key",
                            pagination=10,
                        ).classes("w-full")

        # `ui.tab_panels` keeps inactive panels mounted-but-hidden (keep_alive),
        # so charts inside a panel that starts hidden are born at zero size and
        # never draw. Force an ECharts resize whenever the visible tab changes.
        def _resize_charts() -> None:
            for chart in (
                temp_profile_plot,
                hexane_profile_plot,
                moisture_profile_plot,
                plot,
                outlet_trend_plot,
            ):
                chart.run_chart_method("resize")

        view_tabs.on_value_change(_resize_charts)

        known_mv_keys: list[str] = []
        built_stage_order: list[str] = []
        feed_widgets: dict[str, ui.label] = {}
        product_widgets: dict[str, ui.label] = {}
        tower_widgets: dict[str, dict[str, object]] = {}
        tower_cards: dict[str, ui.card] = {}
        feed_card: ui.card | None = None
        product_card: ui.card | None = None
        profile_categories: list[str] = []
        profile_role_bands: list[list[dict]] = []

        def _build_stage_card(
            container: ui.column, sid: str, role: str
        ) -> tuple[ui.card, dict[str, object]]:
            style = _ROLE_STYLE.get(role, {"border": "border-gray-400", "badge": "grey"})
            widgets: dict[str, object] = {}
            with (
                container,
                ui.card()
                .classes(f"w-full border-l-4 {style['border']}")
                .style("padding: 6px 10px;") as card,
            ):
                with ui.row().classes("items-center justify-between w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(sid).classes("font-bold text-xs")
                        ui.badge(role).props(f"color={style['badge']}").classes("text-[10px]")
                    flood_badge = ui.badge("FLOOD").props("color=negative")
                    flood_badge.visible = False
                    widgets["flood"] = flood_badge
                with ui.row().classes("gap-3 flex-wrap mt-1"):
                    widgets["T"] = _compact_metric("°C")
                    widgets["hex"] = _compact_metric("ppm")
                    widgets["water"] = _compact_metric("% H2O")
                    if role in DT_ROLES:
                        widgets["steam"] = _compact_metric("kW")
                    if role == "SPARGE":
                        widgets["direct"] = _compact_metric("kg/s")
                    if role in ("DRYER", "COOLER"):
                        widgets["air"] = _compact_metric("°C / kg/s")
                with ui.row().classes("items-center gap-2 w-full mt-1"):
                    bar_bg = (
                        ui.element("div")
                        .classes("flex-1")
                        .style(
                            f"height:6px; border-radius:3px; background:{_SIEMENS_BORDER}; "
                            "overflow:hidden;"
                        )
                    )
                    with bar_bg:
                        fill = ui.element("div").style(
                            f"height:100%; width:0%; background:{_SIEMENS_TEAL};"
                        )
                    widgets["level_fill"] = fill
                    widgets["level_label"] = ui.label("- %").classes(
                        "text-[10px] font-mono text-gray-500 whitespace-nowrap"
                    )
            return card, widgets

        def build_tower(stage_order: list[str], stage_roles: dict[str, str]) -> None:
            nonlocal feed_card, product_card, profile_categories, profile_role_bands
            dt_column.clear()
            dc_column.clear()
            feed_widgets.clear()
            product_widgets.clear()
            tower_widgets.clear()
            tower_cards.clear()

            dc_stage_ids = [sid for sid in stage_order if stage_roles.get(sid) in DC_ROLES]
            product_column = dc_column if dc_stage_ids else dt_column

            with dt_column:
                with (
                    ui.card()
                    .classes(f"w-full border-l-4 {_FEED_BORDER}")
                    .style("padding: 6px 10px;") as feed_card
                ):
                    with ui.row().classes("items-center justify-between"):
                        ui.label("FEED").classes("font-bold text-xs")
                        ui.badge("IN").props("color=grey")
                    with ui.row().classes("gap-3 flex-wrap mt-1"):
                        feed_widgets["flow"] = _compact_metric("kg/s")
                        feed_widgets["T"] = _compact_metric("°C")
                        feed_widgets["hex"] = _compact_metric("% hex")
                        feed_widgets["water"] = _compact_metric("% H2O")

            for sid in stage_order:
                role = stage_roles.get(sid, "")
                target = dc_column if role in DC_ROLES else dt_column
                card, widgets = _build_stage_card(target, sid, role)
                tower_widgets[sid] = widgets
                tower_cards[sid] = card

            with product_column:
                with (
                    ui.card()
                    .classes(f"w-full border-l-4 {_FEED_BORDER}")
                    .style("padding: 6px 10px;") as product_card
                ):
                    with ui.row().classes("items-center justify-between"):
                        ui.label("PRODUCT").classes("font-bold text-xs")
                        ui.badge("OUT").props("color=grey")
                    with ui.row().classes("gap-3 flex-wrap mt-1"):
                        product_widgets["T"] = _compact_metric("°C")
                        product_widgets["hex"] = _compact_metric("ppm")
                        product_widgets["water"] = _compact_metric("% H2O")

            profile_categories = ["FEED"] + list(stage_order)
            roles_for_bands = ["FEED"] + [stage_roles.get(sid, "") for sid in stage_order]
            profile_role_bands = _role_bands(roles_for_bands)
            for plot_ in (
                temp_profile_plot,
                hexane_profile_plot,
                moisture_profile_plot,
            ):
                plot_.options["xAxis"]["data"] = profile_categories
                plot_.options["series"][0]["markArea"]["data"] = profile_role_bands

        def sync() -> None:
            snap = facade.get_snapshot()
            is_dashboard = snap.state not in (SimState.UNCONFIGURED, SimState.CONFIGURED)
            setup_container.visible = not is_dashboard
            dashboard_container.visible = is_dashboard
            if not is_dashboard:
                return

            state_label.text = f"State: {snap.state.value}"
            sim_time_label.text = f"Sim time: {snap.sim_time:.1f} s"
            speed_label.text = f"Actual speed: {snap.actual_speed:.2f}x"
            undersample_badge.visible = snap.undersample_warning
            solver_badge.visible = snap.solver_stress
            wall_gap_s = snap.dt_resolve_interval_s / max(snap.speed_factor, 1.0e-9)
            resolve_gap_label.text = f"(~{wall_gap_s:.0f}s wall-clock between DT updates)"
            if snap.outputs is not None:
                solver_badge.tooltip(
                    f"DT solve: {'converged' if snap.outputs.dt_solver_converged else 'NOT converged'} "
                    f"({snap.outputs.dt_solver_outer_iterations} outer iterations)"
                )

            nonlocal known_mv_keys, built_stage_order
            mv_keys = list(snap.mvs.keys())
            if mv_keys != known_mv_keys:
                known_mv_keys = mv_keys
                mv_select.set_options(mv_keys, value=mv_keys[0] if mv_keys else None)
                _update_setpoint_label()

            if snap.stage_order != built_stage_order:
                built_stage_order = list(snap.stage_order)
                build_tower(snap.stage_order, snap.stage_roles)

            mv_table.rows = [
                {
                    "key": k,
                    "mode": mv.mode.value,
                    "manual": round(_mv_to_display(k, mv.manual_setpoint), 4),
                    "auto": round(_mv_to_display(k, mv.auto_setpoint), 4),
                    "effective": round(_mv_to_display(k, mv.effective_value), 4),
                    "limits": (
                        f"[{_mv_to_display(k, mv.min):g}, {_mv_to_display(k, mv.max):g}] "
                        f"{_mv_unit(k)}"
                    ),
                }
                for k, mv in snap.mvs.items()
            ]
            mv_table.update()

            feed_flow = snap.mvs["feed_flow_rate"].effective_value
            feed_flow_kpi.text = f"{feed_flow:.2f}"
            feed_temperature_k = snap.dvs["feed_temperature"]
            feed_hex_pct = snap.dvs["feed_hexane"] * 100
            feed_water_pct = snap.dvs["feed_moisture"] * 100

            if feed_widgets:
                feed_widgets["flow"].text = f"{feed_flow:.2f} kg/s"
                feed_widgets["T"].text = f"{_k_to_c(feed_temperature_k):.1f} °C"
                feed_widgets["hex"].text = f"{feed_hex_pct:.2f} % hex"
                feed_widgets["water"].text = f"{feed_water_pct:.2f} % H2O"
            if feed_card is not None:
                feed_card.style(
                    replace=f"background-color: {_heat_color(feed_temperature_k, 0.12)}"
                )

            outputs = snap.outputs
            if outputs is None:
                return

            for key, _title, fmt in _KPI_TILES:
                kpi_labels[key].text = fmt.format(getattr(outputs, key))

            for sid in snap.stage_order:
                widgets = tower_widgets.get(sid)
                if not widgets:
                    continue
                t_val = outputs.stage_T[sid]
                widgets["T"].text = f"{_k_to_c(t_val):.1f} °C"
                widgets["hex"].text = f"{outputs.stage_X_hex_ppm[sid]:.0f} ppm"
                widgets["water"].text = f"{outputs.stage_X_w_pct[sid]:.2f} % H2O"
                card = tower_cards.get(sid)
                if card is not None:
                    card.style(replace=f"background-color: {_heat_color(t_val, 0.12)}")

                level_pct = outputs.stage_level_pct[sid]
                overfilled = level_pct > 100.0
                widgets["level_fill"].style(
                    replace=(
                        f"height:100%; width:{min(max(level_pct, 0.0), 100.0):.0f}%; "
                        f"background:{_SIEMENS_RED if overfilled else _SIEMENS_TEAL};"
                    )
                )
                widgets["level_label"].text = f"{level_pct:.0f} %"
                widgets["flood"].visible = overfilled

                role = snap.stage_roles.get(sid, "")
                if "steam" in widgets:
                    kw = snap.mvs[f"indirect_steam/{sid}"].effective_value / 1000.0
                    widgets["steam"].text = f"{kw:.0f} kW"
                if "direct" in widgets:
                    widgets["direct"].text = (
                        f"{snap.mvs[f'direct_steam/{sid}'].effective_value:.2f} kg/s"
                    )
                if "air" in widgets:
                    if role == "DRYER":
                        t = snap.mvs["heated_air_temp"].effective_value
                        f = snap.mvs["heated_air_flow"].effective_value
                    else:
                        t = snap.mvs["ambient_air_temp"].effective_value
                        f = snap.mvs["ambient_air_flow"].effective_value
                    widgets["air"].text = f"{_k_to_c(t):.0f} °C / {f:.1f} kg/s"

            if snap.stage_order and product_widgets:
                last_sid = snap.stage_order[-1]
                product_widgets["T"].text = f"{_k_to_c(outputs.stage_T[last_sid]):.1f} °C"
                product_widgets["hex"].text = f"{outputs.stage_X_hex_ppm[last_sid]:.0f} ppm"
                product_widgets["water"].text = f"{outputs.stage_X_w_pct[last_sid]:.2f} % H2O"
                if product_card is not None:
                    product_card.style(
                        replace=(
                            f"background-color: {_heat_color(outputs.stage_T[last_sid], 0.12)}"
                        )
                    )

                outlet_hex_history.append((snap.sim_time, outputs.stage_X_hex_ppm[last_sid]))
                outlet_moisture_history.append((snap.sim_time, outputs.stage_X_w_pct[last_sid]))
                window_start = snap.sim_time - OUTLET_WINDOW_S
                for hist in (outlet_hex_history, outlet_moisture_history):
                    while hist and hist[0][0] < window_start:
                        hist.popleft()
                outlet_trend_plot.options["series"][0]["data"] = list(outlet_hex_history)
                outlet_trend_plot.options["series"][1]["data"] = list(outlet_moisture_history)
                outlet_trend_plot.update()

            if profile_categories:
                temp_data = [_k_to_c(feed_temperature_k)] + [
                    _k_to_c(outputs.stage_T[sid]) for sid in snap.stage_order
                ]
                temp_profile_plot.options["series"][0]["data"] = temp_data
                temp_profile_plot.update()

                hex_data = [feed_hex_pct * 1.0e4] + [
                    outputs.stage_X_hex_ppm[sid] for sid in snap.stage_order
                ]
                water_data = [feed_water_pct] + [
                    outputs.stage_X_w_pct[sid] for sid in snap.stage_order
                ]
                hexane_profile_plot.options["series"][0]["data"] = hex_data
                hexane_profile_plot.update()
                moisture_profile_plot.options["series"][0]["data"] = water_data
                moisture_profile_plot.update()

            history_t.append(snap.sim_time)
            for sid, t_val in outputs.stage_T.items():
                history_T[sid].append(_k_to_c(t_val))

            plot.options["series"] = [
                {
                    "name": sid,
                    "type": "line",
                    "showSymbol": False,
                    "data": list(zip(history_t, history_T[sid])),
                }
                for sid in outputs.stage_T
            ]
            plot.options["legend"] = {"data": list(outputs.stage_T.keys())}
            plot.update()

        ui.timer(SYNC_INTERVAL_S, sync)
