"""NiceGUI adapter — setup screen + runtime dashboard (BuildSpec §10).

Talks only to `RuntimeFacade`; must not import `core/` (BuildSpec §3, §15).
The OPC UA server is started as an asyncio task inside NiceGUI's own event
loop (`app.on_startup`) so the whole process needs only one asyncio loop;
the tick loop itself runs in its own worker thread (BuildSpec §8.3).
"""

from __future__ import annotations

import asyncio
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

_KPI_TILES = (
    ("kpi_residual_hexane_ppm", "Residual hexane [ppm]", "{:.0f}"),
    ("kpi_meal_moisture_pct", "Meal moisture [%]", "{:.2f}"),
    ("kpi_urease_proxy", "Urease/TIA proxy [%]", "{:.1f}"),
    ("kpi_protein_solubility_pct", "Protein solubility [%]", "{:.1f}"),
    ("kpi_steam_consumption_kg_per_t", "Steam [kg/t]", "{:.2f}"),
    ("kpi_throughput_t_per_day", "Throughput [t/day]", "{:.1f}"),
)


def create_app(
    facade: RuntimeFacade, default_scenario: str, opcua_endpoint: str | None = None
) -> None:
    """Register the OPC UA startup task (if any) and the single dashboard page.
    Caller is responsible for calling `ui.run(...)` afterward."""

    if opcua_endpoint:
        from dtdc_simulator.interfaces.opcua.server import serve as opcua_serve

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

        ui.label("DTDC Real-Time Simulator").classes("text-2xl font-bold")

        setup_container = ui.column().classes("w-full gap-4")
        dashboard_container = ui.column().classes("w-full gap-4")
        dashboard_container.visible = False

        with setup_container, ui.card().classes("w-full max-w-2xl"):
            ui.label("Setup").classes("text-lg font-semibold")
            path_input = ui.input("Scenario YAML path", value=default_scenario).classes("w-full")
            error_label = ui.label("").classes("text-red-600")

            def do_load() -> None:
                try:
                    cfg = load_scenario(path_input.value)
                    facade.configure(cfg)
                    facade.assemble()
                    error_label.text = ""
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
                undersample_badge = ui.badge("UNDERSAMPLE").props("color=red outline")
                undersample_badge.visible = False
                solver_badge = ui.badge("SOLVER STRESS").props("color=orange outline")
                solver_badge.visible = False

            with ui.row().classes("w-full items-center gap-4"):
                ui.button("Run", on_click=facade.run)
                ui.button("Pause", on_click=facade.pause)
                ui.button("Stop", on_click=facade.stop)
                ui.button("Reset", on_click=facade.reset)
                ui.button(
                    "Reconfigure",
                    on_click=lambda: facade.reconfigure(),
                ).props("outline")
                ui.label("Speed factor")
                ui.slider(
                    min=0,
                    max=10,
                    step=0.1,
                    value=1.0,
                    on_change=lambda e: facade.set_speed_factor(float(e.value)),
                ).classes("w-48")
                ui.label("Global mode")
                ui.toggle(
                    {Mode.MANUAL.value: "MANUAL", Mode.AUTO.value: "AUTO"},
                    value=Mode.MANUAL.value,
                    on_change=lambda e: facade.set_global_mode(Mode(e.value)),
                )

            with ui.row().classes("w-full gap-4"):
                kpi_labels: dict[str, ui.label] = {}
                for key, title, _fmt in _KPI_TILES:
                    with ui.card().classes("w-48"):
                        ui.label(title).classes("text-xs text-gray-500")
                        kpi_labels[key] = ui.label("-").classes("text-xl font-mono")

            plot = ui.echart(
                {
                    "xAxis": {"type": "value", "name": "sim time [s]"},
                    "yAxis": {"type": "value", "name": "T [K]"},
                    "series": [],
                    "legend": {"data": []},
                    "tooltip": {"trigger": "axis"},
                }
            ).classes("w-full h-80")

            ui.label("Drive a manipulated variable").classes("text-lg font-semibold mt-4")
            with ui.row().classes("w-full items-center gap-4"):
                mv_select = ui.select(options=[], label="MV key").classes("w-64")
                mv_mode_toggle = ui.toggle(
                    {Mode.MANUAL.value: "MANUAL", Mode.AUTO.value: "AUTO"}, value=Mode.MANUAL.value
                )
                mv_setpoint_input = ui.number(label="Manual setpoint", value=0.0).classes("w-40")

                def apply_mv() -> None:
                    key = mv_select.value
                    if not key:
                        return
                    facade.set_mv_mode(key, Mode(mv_mode_toggle.value))
                    facade.set_mv_manual_setpoint(key, float(mv_setpoint_input.value or 0.0))

                ui.button("Apply", on_click=apply_mv)

            ui.label("Manipulated Variables").classes("text-lg font-semibold mt-4")
            mv_table = ui.table(
                columns=[
                    {"name": "key", "label": "MV", "field": "key", "align": "left"},
                    {"name": "mode", "label": "Mode", "field": "mode"},
                    {"name": "manual", "label": "Manual SP", "field": "manual"},
                    {"name": "auto", "label": "Auto SP", "field": "auto"},
                    {"name": "effective", "label": "Effective", "field": "effective"},
                    {"name": "limits", "label": "Limits", "field": "limits"},
                ],
                rows=[],
                row_key="key",
                pagination=10,
            ).classes("w-full")

        known_mv_keys: list[str] = []

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

            nonlocal known_mv_keys
            mv_keys = list(snap.mvs.keys())
            if mv_keys != known_mv_keys:
                known_mv_keys = mv_keys
                mv_select.set_options(mv_keys, value=mv_keys[0] if mv_keys else None)

            mv_table.rows = [
                {
                    "key": k,
                    "mode": mv.mode.value,
                    "manual": round(mv.manual_setpoint, 4),
                    "auto": round(mv.auto_setpoint, 4),
                    "effective": round(mv.effective_value, 4),
                    "limits": f"[{mv.min:g}, {mv.max:g}]",
                }
                for k, mv in snap.mvs.items()
            ]
            mv_table.update()

            outputs = snap.outputs
            if outputs is None:
                return

            for key, _title, fmt in _KPI_TILES:
                kpi_labels[key].text = fmt.format(getattr(outputs, key))

            history_t.append(snap.sim_time)
            for sid, t_val in outputs.stage_T.items():
                history_T[sid].append(t_val)

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
