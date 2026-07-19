"""NiceGUI adapter — setup screen + single-page runtime dashboard (BuildSpec §10).

Talks only to `RuntimeFacade`; must not import `core/` (BuildSpec §3, §15).
The OPC UA server is started as an asyncio task inside NiceGUI's own event
loop (`app.on_startup`) so the whole process needs only one asyncio loop;
the tick loop itself runs in its own worker thread (BuildSpec §8.3).

The dashboard is a single clean page (no tabs): the tower schematic + DT
zone-resolved profile charts + the MV/DV operator slider strip are always
visible; a collapsed "Advanced" drawer at the bottom holds the time-history
trend charts and the generic MV table/drive control for anyone who needs
them (kept, not dropped, in the GUI redesign -- see the plan's own rationale).

All internal facade/model plumbing stays SI (Kelvin); this module (and its
`theme`/`tower`/`dt_profiles`/`controls` siblings) convert to °C only at
display time (see `theme.k_to_c`/`theme.c_to_k`).
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque

from nicegui import app, ui

from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.engine.mv import Mode
from dtdc_simulator.engine.state_machine import SimState
from dtdc_simulator.interfaces.ui import theme
from dtdc_simulator.interfaces.ui.controls import ControlsView
from dtdc_simulator.interfaces.ui.dt_profiles import DTProfileView
from dtdc_simulator.interfaces.ui.tower import TowerView

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

# Per-MV-key display unit map for the Advanced drawer's generic MV table --
# a handful of MVs are stored in Kelvin (SI) internally but should read in °C.
_MV_UNITS = {
    "feed_flow_rate": "kg/s",
    "heated_air_temp": "°C",
    "heated_air_flow": "kg/s",
    "ambient_air_flow": "kg/s",
    "indirect_steam": "W",
    "direct_steam": "kg/s",
    "sweep_arm_speed": "rpm",
    "gate_opening": "%",
}
_MV_TEMP_K_PREFIXES = {"heated_air_temp"}


def _mv_prefix(key: str) -> str:
    return key.split("/", 1)[0]


def _mv_unit(key: str) -> str:
    return _MV_UNITS.get(_mv_prefix(key), "")


def _mv_to_display(key: str, value: float) -> float:
    return theme.k_to_c(value) if _mv_prefix(key) in _MV_TEMP_K_PREFIXES else value


def _mv_from_display(key: str, value: float) -> float:
    return theme.c_to_k(value) if _mv_prefix(key) in _MV_TEMP_K_PREFIXES else value


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
        # sample count -- speed_factor (and thus sim-seconds/tick) can change, so
        # count alone doesn't bound this to a fixed sim-time span.
        outlet_hex_history: deque[tuple[float, float]] = deque()
        outlet_moisture_history: deque[tuple[float, float]] = deque()

        theme.inject_theme()

        with (
            ui.header()
            .classes("items-center justify-between px-4")
            .style(f"background-color: {theme.DARK};")
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
        dashboard_container = ui.column().classes("w-full gap-3 p-4")
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
                    controls_view.apply_scenario_defaults(cfg)
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
                # set_dt_resolve_interval_s).
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

            with ui.row().classes("w-full gap-4"):
                with ui.card().classes("w-48"):
                    ui.label("Feed flow [kg/s]").classes("text-xs text-gray-500")
                    feed_flow_kpi = ui.label("-").classes("text-xl font-mono")
                kpi_labels: dict[str, ui.label] = {}
                for key, title, _fmt in _KPI_TILES:
                    with ui.card().classes("w-48"):
                        ui.label(title).classes("text-xs text-gray-500")
                        kpi_labels[key] = ui.label("-").classes("text-xl font-mono")

            controls_container = ui.column().classes("w-full gap-2")
            controls_view = ControlsView(facade, controls_container)

            with ui.row().classes("w-full gap-4 items-start flex-wrap"):
                # ---- left: compact 2-column tower (DT | DC) ----
                with ui.column().classes("gap-2").style("flex: 0 0 620px"):
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
                    # never needs the page to scroll for it.
                    with (
                        ui.row()
                        .classes("w-full gap-3 items-start")
                        .style("max-height: 700px; overflow-y: auto;")
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

                tower_view = TowerView(facade, dt_column, dc_column)

                # ---- right: DT zone-resolved axial profiles ----
                profile_container = ui.column().classes("gap-2").style("flex: 1 1 0%; min-width: 480px")
                profile_view = DTProfileView(profile_container)

            with ui.expansion("Advanced: trends & full MV table", value=False).classes(
                "w-full"
            ) as advanced_expansion:
                with ui.column().classes("w-full gap-4"):
                    ui.label("Stage Temperature Trend (time history)").classes(
                        "text-lg font-semibold"
                    )
                    trend_plot = ui.echart(
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

                    ui.label("Outlet Quality Trend (last hour)").classes("text-lg font-semibold")
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

        # A collapsed `ui.expansion`'s content is born at zero size, so charts
        # inside it never draw until it's actually opened -- same fix the
        # pre-redesign dashboard applied to its (now-removed) tab panels.
        def _resize_advanced_charts() -> None:
            if advanced_expansion.value:
                trend_plot.run_chart_method("resize")
                outlet_trend_plot.run_chart_method("resize")

        advanced_expansion.on_value_change(_resize_advanced_charts)

        known_mv_keys: list[str] = []
        built_stage_order: list[str] = []

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
                tower_view.build(snap.stage_order, snap.stage_roles, snap.mvs)

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

            feed_flow_kpi.text = f"{snap.mvs['feed_flow_rate'].effective_value:.2f}"

            tower_view.sync(snap, snap.outputs)
            profile_view.sync(snap)

            outputs = snap.outputs
            if outputs is None:
                return

            for key, _title, fmt in _KPI_TILES:
                kpi_labels[key].text = fmt.format(getattr(outputs, key))

            if snap.stage_order:
                last_sid = snap.stage_order[-1]
                outlet_hex_history.append((snap.sim_time, outputs.stage_X_hex_ppm[last_sid]))
                outlet_moisture_history.append((snap.sim_time, outputs.stage_X_w_pct[last_sid]))
                window_start = snap.sim_time - OUTLET_WINDOW_S
                for hist in (outlet_hex_history, outlet_moisture_history):
                    while hist and hist[0][0] < window_start:
                        hist.popleft()
                outlet_trend_plot.options["series"][0]["data"] = list(outlet_hex_history)
                outlet_trend_plot.options["series"][1]["data"] = list(outlet_moisture_history)
                outlet_trend_plot.update()

            history_t.append(snap.sim_time)
            for sid, t_val in outputs.stage_T.items():
                history_T[sid].append(theme.k_to_c(t_val))

            trend_plot.options["series"] = [
                {
                    "name": sid,
                    "type": "line",
                    "showSymbol": False,
                    "data": list(zip(history_t, history_T[sid])),
                }
                for sid in outputs.stage_T
            ]
            trend_plot.options["legend"] = {"data": list(outputs.stage_T.keys())}
            trend_plot.update()

        ui.timer(SYNC_INTERVAL_S, sync)
