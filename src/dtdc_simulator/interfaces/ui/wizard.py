"""Equipment-configuration wizard (Phase 4).

A clean 3-step guided flow that replaces "type a scenario YAML path":

    1. Equipment       — material + tray counts per zone (capped by the envelope),
                         with a live stage-stack preview.
    2. Operating & geometry — sensible defaults, pre-filled; adjust only if needed.
    3. Review & build  — live design check (config/design_rules.validate_design);
                         "Build & Assemble" is blocked while there are errors.

The wizard only collects a compact `DesignSpec`; `config.scaffold` turns that into
a full, valid `ScenarioConfig` (canonical ids + auto topology). A small "load an
existing YAML" fallback is kept for power users. Talks only to `RuntimeFacade`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from nicegui import ui

from dtdc_simulator.config.design_rules import Severity, has_errors, validate_design
from dtdc_simulator.config.envelope import load_envelope
from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.scaffold import DEFAULT_TEMPLATE, DesignSpec, scaffold_scenario
from dtdc_simulator.config.schema import ScenarioConfig, StageRole
from dtdc_simulator.engine.facade import RuntimeFacade

logger = logging.getLogger(__name__)

_ZONE_COLOR = {
    StageRole.PREDESOLV: "blue",
    StageRole.MAIN: "orange",
    StageRole.SPARGE: "red",
    StageRole.DRYER: "teal",
    StageRole.COOLER: "blue-grey",
}
_ROLE_PREFIX = {
    StageRole.PREDESOLV: "PD",
    StageRole.MAIN: "MN",
    StageRole.SPARGE: "SP",
    StageRole.DRYER: "DR",
    StageRole.COOLER: "CL",
}


def _num(label: str, value: float, *, width: str = "w-40", **kwargs) -> ui.number:
    return ui.number(label=label, value=value, **kwargs).classes(width)


def create_wizard(
    facade: RuntimeFacade,
    *,
    template_path: str = DEFAULT_TEMPLATE,
    default_material: str = "soybean",
    on_assembled: Callable[[ScenarioConfig], None] | None = None,
) -> None:
    """Build the configuration wizard in the current UI context."""
    envelope = load_envelope()
    caps = {z.role: (z.min_count, z.max_count) for z in envelope.zones}
    materials = sorted(p.stem for p in Path("properties").glob("*.yaml")) or [default_material]
    d = DesignSpec()  # defaults for pre-filling
    el: dict[str, ui.element] = {}

    def count_input(label: str, role: StageRole, value: int) -> ui.number:
        lo, hi = caps[role]
        return ui.number(label=label, value=value, min=lo, max=hi, step=1, format="%d").classes(
            "w-44"
        )

    with ui.card().classes("w-full max-w-3xl"):
        ui.label("Configure your DTDC unit").classes("text-lg font-semibold")

        with ui.stepper().props("flat").classes("w-full") as stepper:
            # ---- Step 1: equipment ----------------------------------------
            with ui.step("Equipment"):
                ui.label("Pick the material and the number of trays in each zone.").classes(
                    "text-sm text-gray-500"
                )
                el["material"] = ui.select(
                    materials, value=default_material, label="Material"
                ).classes("w-64")
                with ui.row().classes("gap-3 flex-wrap items-end"):
                    el["n_predesolv"] = count_input(
                        "Predesolventising", StageRole.PREDESOLV, d.n_predesolv
                    )
                    el["n_main"] = count_input("Countercurrent (main)", StageRole.MAIN, d.n_main)
                    el["n_dryer"] = count_input("Dryer", StageRole.DRYER, d.n_dryer)
                    el["n_cooler"] = count_input("Cooler", StageRole.COOLER, d.n_cooler)
                ui.label("Plus 1 sparge tray (always required).").classes("text-xs text-gray-500")
                ui.label("Resulting unit").classes("text-sm font-medium mt-2")
                preview = ui.row().classes("gap-1 flex-wrap items-center")
                with ui.stepper_navigation():
                    ui.button("Next", on_click=stepper.next)

            # ---- Step 2: operating & geometry -----------------------------
            with ui.step("Operating & geometry"):
                ui.label("Sensible defaults are pre-filled — change only what you need.").classes(
                    "text-sm text-gray-500"
                )
                with ui.expansion("Steam & air", value=True).classes("w-full"):
                    with ui.row().classes("gap-3 flex-wrap"):
                        el["predesolv_indirect_mw"] = _num(
                            "Predesolv indirect [MW]", d.predesolv_indirect_total_w / 1e6, step=0.1
                        )
                        el["main_indirect_mw"] = _num(
                            "Toast indirect [MW]", d.main_indirect_total_w / 1e6, step=0.05
                        )
                        el["direct_steam_kg_s"] = _num(
                            "Sparge steam [kg/s]", d.direct_steam_kg_s, step=0.1
                        )
                        el["heated_air_temp_c"] = _num(
                            "Dryer air [°C]", d.heated_air_temp_c, step=1
                        )
                        el["heated_air_flow"] = _num("Dryer air [kg/s]", d.heated_air_flow, step=5)
                        el["ambient_air_flow"] = _num(
                            "Cooler air [kg/s]", d.ambient_air_flow, step=5
                        )
                with ui.expansion("Geometry", value=False).classes("w-full"):
                    with ui.row().classes("gap-3 flex-wrap"):
                        el["dt_diameter_m"] = _num("DT diameter [m]", d.dt_diameter_m, step=0.5)
                        el["dc_diameter_m"] = _num("DC diameter [m]", d.dc_diameter_m, step=0.5)
                        el["predesolv_bed_m"] = _num(
                            "Predesolv bed [m]", d.predesolv_bed_m, step=0.1
                        )
                        el["main_bed_m"] = _num("Main bed [m]", d.main_bed_m, step=0.1)
                        el["sparge_bed_m"] = _num("Sparge bed [m]", d.sparge_bed_m, step=0.1)
                        el["dryer_bed_m"] = _num("Dryer bed [m]", d.dryer_bed_m, step=0.1)
                        el["cooler_bed_m"] = _num("Cooler bed [m]", d.cooler_bed_m, step=0.1)
                with ui.expansion("Feed & weather", value=False).classes("w-full"):
                    with ui.row().classes("gap-3 flex-wrap"):
                        el["feed_flow_rate"] = _num("Feed [kg/s dry]", d.feed_flow_rate, step=1)
                        el["feed_temperature_c"] = _num(
                            "Feed temp [°C]", d.feed_temperature_c, step=1
                        )
                        el["feed_moisture"] = _num(
                            "Feed moisture [kg/kg]", d.feed_moisture, step=0.01
                        )
                        el["feed_hexane"] = _num("Feed hexane [kg/kg]", d.feed_hexane, step=0.01)
                        el["feed_oil"] = _num("Feed oil [kg/kg]", d.feed_oil, step=0.001)
                        el["ambient_air_temp_c"] = _num(
                            "Ambient temp [°C]", d.ambient_air_temp_c, step=1
                        )
                        el["ambient_rh"] = _num("Ambient RH [0-1]", d.ambient_rh, step=0.05)
                el["start_empty"] = ui.checkbox(
                    "Start empty (watch material propagate through the unit)"
                )
                with ui.stepper_navigation():
                    ui.button("Back", on_click=stepper.previous).props("flat")
                    ui.button("Next", on_click=stepper.next)

            # ---- Step 3: review & build -----------------------------------
            with ui.step("Review & build"):
                summary_label = ui.label().classes("text-sm font-medium")
                check_container = ui.column().classes("w-full gap-1")
                with ui.stepper_navigation():
                    ui.button("Back", on_click=stepper.previous).props("flat")
                    ui.button("Build & Assemble", icon="build", on_click=lambda: _do_build())

        with ui.expansion("Or load an existing scenario YAML").classes("w-full max-w-3xl"):
            yaml_path = ui.input("Scenario YAML path", value=template_path).classes("w-full")
            yaml_error = ui.label().classes("text-red-600 text-sm")

            def _load_yaml() -> None:
                try:
                    cfg = load_scenario(yaml_path.value)
                    _assemble(cfg)
                    yaml_error.text = ""
                except Exception as exc:  # noqa: BLE001
                    yaml_error.text = f"{type(exc).__name__}: {exc}"

            ui.button("Load & Assemble", on_click=_load_yaml).props("outline")

    # ---- behaviour --------------------------------------------------------
    def current_spec() -> DesignSpec:
        return DesignSpec(
            material=el["material"].value,
            n_predesolv=int(el["n_predesolv"].value or 1),
            n_main=int(el["n_main"].value or 1),
            n_sparge=1,
            n_dryer=int(el["n_dryer"].value or 0),
            n_cooler=int(el["n_cooler"].value or 0),
            dt_diameter_m=float(el["dt_diameter_m"].value),
            dc_diameter_m=float(el["dc_diameter_m"].value),
            predesolv_bed_m=float(el["predesolv_bed_m"].value),
            main_bed_m=float(el["main_bed_m"].value),
            sparge_bed_m=float(el["sparge_bed_m"].value),
            dryer_bed_m=float(el["dryer_bed_m"].value),
            cooler_bed_m=float(el["cooler_bed_m"].value),
            feed_flow_rate=float(el["feed_flow_rate"].value),
            predesolv_indirect_total_w=float(el["predesolv_indirect_mw"].value) * 1e6,
            main_indirect_total_w=float(el["main_indirect_mw"].value) * 1e6,
            direct_steam_kg_s=float(el["direct_steam_kg_s"].value),
            heated_air_temp_c=float(el["heated_air_temp_c"].value),
            heated_air_flow=float(el["heated_air_flow"].value),
            ambient_air_flow=float(el["ambient_air_flow"].value),
            feed_temperature_c=float(el["feed_temperature_c"].value),
            feed_moisture=float(el["feed_moisture"].value),
            feed_hexane=float(el["feed_hexane"].value),
            feed_oil=float(el["feed_oil"].value),
            ambient_air_temp_c=float(el["ambient_air_temp_c"].value),
            ambient_rh=float(el["ambient_rh"].value),
            start_empty=bool(el["start_empty"].value),
        )

    def _stage_layout() -> list[tuple[str, StageRole]]:
        counts = [
            (StageRole.PREDESOLV, int(el["n_predesolv"].value or 0)),
            (StageRole.MAIN, int(el["n_main"].value or 0)),
            (StageRole.SPARGE, 1),
            (StageRole.DRYER, int(el["n_dryer"].value or 0)),
            (StageRole.COOLER, int(el["n_cooler"].value or 0)),
        ]
        return [(f"{_ROLE_PREFIX[role]}{i}", role) for role, n in counts for i in range(1, n + 1)]

    def update_preview() -> None:
        preview.clear()
        with preview:
            for sid, role in _stage_layout():
                ui.badge(sid).props(f"color={_ZONE_COLOR[role]}")

    def render_check(issues) -> None:
        errors = [i for i in issues if i.severity is Severity.ERROR]
        warnings = [i for i in issues if i.severity is Severity.WARNING]
        if not issues:
            summary_label.text = "✓ Design valid — ready to build."
        else:
            summary_label.text = f"{len(errors)} error(s), {len(warnings)} warning(s)"
        check_container.clear()
        with check_container:
            for issue in errors + warnings:
                is_err = issue.severity is Severity.ERROR
                with ui.row().classes("items-center gap-2"):
                    ui.icon("error" if is_err else "warning").classes(
                        "text-red-600" if is_err else "text-amber-600"
                    )
                    loc = f" [{issue.location}]" if issue.location else ""
                    ui.label(f"{issue.message}{loc}").classes("text-sm")

    def run_check() -> None:
        try:
            render_check(validate_design(scaffold_scenario(current_spec(), template_path)))
        except Exception as exc:  # noqa: BLE001
            summary_label.text = f"Cannot evaluate design: {exc}"
            check_container.clear()

    def _assemble(cfg: ScenarioConfig) -> None:
        facade.configure(cfg)
        facade.assemble()
        if on_assembled is not None:
            on_assembled(cfg)
        ui.notify("DTDC unit assembled — opening the dashboard.", type="positive")

    def _do_build() -> None:
        try:
            spec = current_spec()
            cfg = scaffold_scenario(spec, template_path)
            issues = validate_design(cfg)
            render_check(issues)
            if has_errors(issues):
                ui.notify("Resolve the design errors before building.", type="negative")
                return
            cfg.sim.dt_start_empty = spec.start_empty
            _assemble(cfg)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Build failed: {exc}", type="negative")

    for key in ("n_predesolv", "n_main", "n_dryer", "n_cooler"):
        el[key].on_value_change(update_preview)
    stepper.on_value_change(lambda: run_check() if stepper.value == "Review & build" else None)
    update_preview()
