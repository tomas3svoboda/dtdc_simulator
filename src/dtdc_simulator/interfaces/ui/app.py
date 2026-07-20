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
from dtdc_simulator.interfaces.ui.dt_profiles import DTProfileView
from dtdc_simulator.interfaces.ui.tower import TowerView

logger = logging.getLogger(__name__)

HISTORY_LEN = 12000  # safety cap on trend-history length (real trimming is by TREND_WINDOW_S)
SYNC_INTERVAL_S = 0.3
TREND_WINDOW_S = 1800.0  # sliding sim-time window shown on ALL trend charts (stage-temp + outlet)

# M4 (GUI redesign): the process-overview KPI band. Each entry is
# (Outputs attribute, title, unit, format, status_fn) where status_fn(value)
# returns None|"warn"|"crit" for the RESERVED status tint (out-of-spec / safety
# limit) -- most KPIs are informational (status_fn=None).


def _meal_hexane_status(v: float) -> str | None:
    # Residual solvent in finished meal: typical spec well under ~1000 ppm.
    return "crit" if v > 1000.0 else "warn" if v > 700.0 else None


def _exhaust_hexane_status(v: float) -> str | None:
    # DC exhaust air vs the ~1100 ppm (10% LEL) safety limit; warn at 50% LEL.
    return "crit" if v > 1100.0 else "warn" if v > 550.0 else None


_KPI_SPEC = (
    ("kpi_residual_hexane_ppm", "Residual hexane (meal)", "ppm", "{:.0f}", _meal_hexane_status),
    ("kpi_meal_moisture_pct", "Meal moisture", "%", "{:.2f}", None),
    ("kpi_exhaust_hexane_ppm", "Exhaust-air hexane", "ppm", "{:.0f}", _exhaust_hexane_status),
    ("kpi_direct_steam_kg_s", "Direct steam", "kg/s", "{:.2f}", None),
    ("kpi_indirect_heating_kw", "Indirect heating", "kW", "{:.0f}", None),
    ("kpi_drying_air_heating_kw", "Drying-air heating", "kW", "{:.0f}", None),
    ("kpi_total_energy_kw", "Total energy", "kW", "{:.0f}", None),
    ("kpi_outlet_vapor_kg_s", "Outlet vapour", "kg/s", "{:.2f}", None),
    ("kpi_outlet_vapor_hexane_kg_s", "Vapour hexane", "kg/s", "{:.3f}", None),
    ("kpi_outlet_vapor_water_kg_s", "Vapour water", "kg/s", "{:.3f}", None),
    ("kpi_condenser_duty_kw", "Condenser duty", "kW", "{:.0f}", None),
    ("kpi_throughput_t_per_day", "Throughput", "t/day", "{:.1f}", None),
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
        # (sim_time, value) pairs, trimmed to a rolling TREND_WINDOW_S regardless of
        # sample count -- speed_factor (and thus sim-seconds/tick) can change, so
        # count alone doesn't bound this to a fixed sim-time span.
        outlet_hex_history: deque[tuple[float, float]] = deque()
        outlet_moisture_history: deque[tuple[float, float]] = deque()

        theme.inject_theme()

        with ui.header().classes("items-center justify-between px-4 hmi-header"):
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
                    # Operator sliders are (re)built + seeded from the assembled
                    # snapshot inside TowerView.build() on the next sync -- no
                    # separate seeding pass needed.
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
                # Plant-wide mass-inventory readout (the always-on conservation
                # diagnostic, Outputs.mass_inventory): total dry-solid holdup and
                # the net feed-minus-product accumulation rate (~0 at steady state;
                # non-zero => material backing up / draining somewhere).
                inventory_label = ui.label().tooltip(
                    "Total dry-solid holdup in the unit, and the net feed - product "
                    "rate (≈0 at steady state)"
                )
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

            # ---- KPI band -------------------------------------------------
            ui.label("Process KPIs").classes("hmi-section-title")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                kpi_tiles: dict[str, theme.KpiTile] = {}
                for attr, title, unit, _fmt, _status in _KPI_SPEC:
                    kpi_tiles[attr] = theme.kpi_tile(title, unit)

            # ---- Process schematic + profiles/trends, split 50/50 ---------
            with ui.row().classes("w-full gap-4 items-start flex-wrap"):
                # ---- left: the single stacked tower column (operator sliders
                # are folded onto the relevant trays inside TowerView) ----
                with ui.column().classes("gap-1").style("flex: 1 1 460px; min-width: 460px"):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label("Process Overview").classes("text-lg font-semibold")
                        with ui.row().classes("items-center gap-2"):
                            ui.label("Cold").classes("text-xs text-gray-500")
                            ui.element("div").style(
                                "height:10px; width:120px; border-radius:4px; "
                                "background: linear-gradient(to right, "
                                "rgb(37,99,235), rgb(250,204,21), rgb(220,38,38));"
                            )
                            ui.label("Hot").classes("text-xs text-gray-500")
                    tower_column = ui.column().classes("w-full gap-1")

                tower_view = TowerView(facade, tower_column)

                # ---- right: DT axial profiles + live trend plots, filling the
                # space beside the tall tower column (equal 50/50 width) ----
                profile_container = ui.column().classes("gap-2").style("flex: 1 1 460px; min-width: 460px")
                profile_view = DTProfileView(profile_container)
                with profile_container:
                    ui.label("Trends").classes("hmi-section-title mt-3")
                    ui.label("Stage Temperature Trend [°C]").classes("text-sm font-semibold")
                    trend_plot = ui.echart(
                        {
                            "xAxis": {"type": "value", "name": "sim time [s]", "scale": True},
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
                    ).classes("w-full h-56")

                    # Two SINGLE-axis charts, not one dual-y-axis chart: hexane
                    # (ppm) and moisture (%) share no scale, so a shared y-axis
                    # would be misleading (dataviz: never a dual-axis chart).
                    ui.label("Outlet Quality Trend (last hour)").classes("text-sm font-semibold")
                    with ui.row().classes("w-full gap-3 flex-wrap"):
                        outlet_hex_plot = ui.echart(
                            {
                                "title": {"text": "Outlet hexane [ppm]", "textStyle": {"fontSize": 12}},
                                "xAxis": {"type": "value", "name": "sim time [s]", "scale": True},
                                "yAxis": {"type": "value", "name": "ppm", "scale": True},
                                "series": [
                                    {
                                        "name": "Outlet Hexane",
                                        "type": "line",
                                        "showSymbol": False,
                                        "itemStyle": {"color": theme.RED},
                                        "data": [],
                                    }
                                ],
                                "tooltip": {"trigger": "axis"},
                            }
                        ).classes("h-52").style("flex: 1 1 200px;")
                        outlet_moist_plot = ui.echart(
                            {
                                "title": {"text": "Outlet moisture [%]", "textStyle": {"fontSize": 12}},
                                "xAxis": {"type": "value", "name": "sim time [s]", "scale": True},
                                "yAxis": {"type": "value", "name": "%", "scale": True},
                                "series": [
                                    {
                                        "name": "Outlet Moisture",
                                        "type": "line",
                                        "showSymbol": False,
                                        "itemStyle": {"color": theme.TEAL},
                                        "data": [],
                                    }
                                ],
                                "tooltip": {"trigger": "axis"},
                            }
                        ).classes("h-52").style("flex: 1 1 200px;")

            with ui.expansion("Advanced: full MV table & manual drive", value=False).classes(
                "w-full"
            ):
                with ui.column().classes("w-full gap-4"):
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

        # The trend charts now live in the always-visible right column (beside
        # the tower), not in a collapsed expansion, so they draw immediately --
        # no open-to-resize workaround needed anymore.

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
                tower_view.build(snap.stage_order, snap.stage_roles, snap.mvs, snap.dvs, snap.steam)

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

            tower_view.sync(snap, snap.outputs)
            profile_view.sync(snap)

            outputs = snap.outputs
            if outputs is None:
                return

            mi = outputs.mass_inventory
            net = mi.feed_dry_solid_kg_s - mi.product_dry_solid_kg_s
            inventory_label.text = (
                f"Inventory: {mi.total_dry_solid_holdup_kg / 1000.0:.1f} t "
                f"(net {net:+.2f} kg/s)"
            )

            for attr, _title, _unit, fmt, status_fn in _KPI_SPEC:
                value = getattr(outputs, attr)
                kpi_tiles[attr].set(fmt.format(value), status_fn(value) if status_fn else None)

            # All trend charts share one fixed, sliding sim-time window. Pinning
            # the x-axis min/max (not just relying on the data range) is what makes
            # it actually slide -- an unpinned value axis snaps its min back to 0.
            t_now = snap.sim_time
            window_start = t_now - TREND_WINDOW_S
            x_min = round(max(0.0, window_start), 1)
            x_max = round(t_now, 1)

            if snap.stage_order:
                last_sid = snap.stage_order[-1]
                outlet_hex_history.append((t_now, outputs.stage_X_hex_ppm[last_sid]))
                outlet_moisture_history.append((t_now, outputs.stage_X_w_pct[last_sid]))
                for hist in (outlet_hex_history, outlet_moisture_history):
                    while hist and hist[0][0] < window_start:
                        hist.popleft()
                for chart, hist in (
                    (outlet_hex_plot, outlet_hex_history),
                    (outlet_moist_plot, outlet_moisture_history),
                ):
                    chart.options["series"][0]["data"] = list(hist)
                    chart.options["xAxis"]["min"] = x_min
                    chart.options["xAxis"]["max"] = x_max
                    chart.update()

            history_t.append(t_now)
            for sid, t_val in outputs.stage_T.items():
                history_T[sid].append(theme.k_to_c(t_val))
            # Slide the (parallel) stage-temp history to the same window.
            while history_t and history_t[0] < window_start:
                history_t.popleft()
                for dq in history_T.values():
                    if dq:
                        dq.popleft()

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
            trend_plot.options["xAxis"]["min"] = x_min
            trend_plot.options["xAxis"]["max"] = x_max
            trend_plot.update()

        ui.timer(SYNC_INTERVAL_S, sync)
