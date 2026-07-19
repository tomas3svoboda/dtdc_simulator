"""The DT/DC vessel schematic -- the tower's own visual centerpiece.

Each DT (PREDESOLV/MAIN/SPARGE) tray is drawn as an actual vessel: a bordered
column whose fill height tracks `stage_level_pct` and whose fill color tracks
temperature (the same heat gradient `theme.heat_color` already uses for the
role-card tinting), plus one inline slider for that tray's own most
process-relevant MV (`indirect_steam`/`direct_steam`) and its `gate_opening`
(the rotary-valve "bed level" control) -- so an operator can nudge a tray's
own duty/level right where that tray is drawn. `sweep_arm_speed` is
deliberately NOT duplicated here (would crowd every tray with a 4th control);
it stays reachable from the Advanced drawer's generic MV table/drive control.

DC (DRYER/COOLER) trays keep a simpler card (no vessel, no inline slider --
`core/dc.py` is a 0-D well-mixed contactor, there's no axial profile to draw,
and its own MVs are GLOBAL, not per-tray, so they belong in `controls.py`).

Talks only to `RuntimeFacade` + `Snapshot` (BuildSpec §3): never imports `core/`.
"""

from __future__ import annotations

from nicegui import ui

from dtdc_simulator.engine.facade import MVSnapshot, RuntimeFacade
from dtdc_simulator.interfaces.ui import theme

DT_ROLES = ("PREDESOLV", "MAIN", "SPARGE")
DC_ROLES = ("DRYER", "COOLER")

_VESSEL_HEIGHT_PX = 72


def _vessel(container: ui.element) -> tuple[ui.element, ui.element]:
    """A bordered vessel column + its bottom-anchored fill div. Returns
    `(outer, fill)` so `sync()` can restyle `fill`'s height/color each tick --
    same low-level `ui.element('div').style(...)` pattern the original level
    bar already used, just shaped like a vessel instead of a thin strip."""
    with container:
        outer = (
            ui.element("div")
            .classes("w-full relative overflow-hidden")
            .style(
                f"height:{_VESSEL_HEIGHT_PX}px; border-radius:6px; "
                f"border:1px solid {theme.BORDER}; background:#ffffff;"
            )
        )
        with outer:
            fill = ui.element("div").style(
                f"position:absolute; left:0; right:0; bottom:0; height:0%; "
                f"background:{theme.TEAL}; transition:height 0.4s ease, background 0.4s ease;"
            )
    return outer, fill


def _mv_slider(
    container: ui.element,
    facade: RuntimeFacade,
    key: str,
    mv: MVSnapshot,
    label: str,
    color: str,
    scale: float = 1.0,
) -> ui.slider:
    """One inline per-tray MV slider. `scale` converts the MV's own SI storage
    unit to a nicer display unit (e.g. W -> kW) -- purely a display transform,
    `facade.set_mv_manual_setpoint` always receives the SI value back."""
    with container:
        ui.label(label).classes("text-[10px] text-gray-500")
        slider = (
            ui.slider(
                min=mv.min * scale,
                max=mv.max * scale,
                value=mv.effective_value * scale,
                on_change=lambda e, k=key, s=scale: facade.set_mv_manual_setpoint(k, e.value / s),
            )
            .props(f"label-always color={color} dense")
            .classes("w-full")
        )
    return slider


class TowerView:
    """Owns the tower's NiceGUI widgets and their live-sync state. `build()`
    (re)constructs the DOM when the stage set changes (rare); `sync()` runs
    every tick from `app.py`'s own timer, same split as the pre-redesign
    `build_tower`/`sync` closures it replaces."""

    def __init__(self, facade: RuntimeFacade, dt_column: ui.column, dc_column: ui.column) -> None:
        self._facade = facade
        self._dt_column = dt_column
        self._dc_column = dc_column
        self._stage_order: list[str] = []
        self._feed_card: ui.card | None = None
        self._product_card: ui.card | None = None
        self._feed_widgets: dict[str, ui.label] = {}
        self._product_widgets: dict[str, ui.label] = {}
        self._cards: dict[str, ui.card] = {}
        self._fills: dict[str, ui.element] = {}
        self._widgets: dict[str, dict[str, object]] = {}
        # Per-DC-stage inlet air flow, captured on the "Air in:" pass and
        # echoed on the "Air out:" line so the (conserved) dry-air flow is
        # visible on both -- see `_build_dc_tray`'s own comment.
        self._air_flow_by_sid: dict[str, float] = {}

    # ------------------------------------------------------------------ build
    def build(
        self, stage_order: list[str], stage_roles: dict[str, str], mvs: dict[str, MVSnapshot]
    ) -> None:
        self._stage_order = list(stage_order)
        self._dt_column.clear()
        self._dc_column.clear()
        self._feed_widgets.clear()
        self._product_widgets.clear()
        self._cards.clear()
        self._fills.clear()
        self._widgets.clear()

        dc_stage_ids = [sid for sid in stage_order if stage_roles.get(sid) in DC_ROLES]
        product_column = self._dc_column if dc_stage_ids else self._dt_column

        with self._dt_column:
            with (
                ui.card()
                .classes(f"w-full border-l-4 {theme.FEED_BORDER}")
                .style("padding: 6px 10px;") as feed_card
            ):
                self._feed_card = feed_card
                with ui.row().classes("items-center justify-between"):
                    ui.label("FEED").classes("font-bold text-xs")
                    ui.badge("IN").props("color=grey")
                with ui.row().classes("gap-3 flex-wrap mt-1"):
                    self._feed_widgets["flow"] = theme.compact_metric("kg/s")
                    self._feed_widgets["T"] = theme.compact_metric("°C")
                    self._feed_widgets["hex"] = theme.compact_metric("% hex")
                    self._feed_widgets["water"] = theme.compact_metric("% H2O")

        for sid in stage_order:
            role = stage_roles.get(sid, "")
            target = self._dc_column if role in DC_ROLES else self._dt_column
            if role in DT_ROLES:
                self._build_dt_tray(target, sid, role, mvs)
            else:
                self._build_dc_tray(target, sid, role)

        with product_column:
            with (
                ui.card()
                .classes(f"w-full border-l-4 {theme.FEED_BORDER}")
                .style("padding: 6px 10px;") as product_card
            ):
                self._product_card = product_card
                with ui.row().classes("items-center justify-between"):
                    ui.label("PRODUCT").classes("font-bold text-xs")
                    ui.badge("OUT").props("color=grey")
                with ui.row().classes("gap-3 flex-wrap mt-1"):
                    self._product_widgets["T"] = theme.compact_metric("°C")
                    self._product_widgets["hex"] = theme.compact_metric("ppm")
                    self._product_widgets["water"] = theme.compact_metric("% H2O")

    def _build_dt_tray(
        self, container: ui.column, sid: str, role: str, mvs: dict[str, MVSnapshot]
    ) -> None:
        style = theme.ROLE_STYLE.get(role, {"border": "border-gray-400", "badge": "grey"})
        widgets: dict[str, object] = {}
        with (
            container,
            ui.card().classes(f"w-full border-l-4 {style['border']}").style("padding: 8px;") as card,
        ):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(sid).classes("font-bold text-xs")
                    ui.badge(role).props(f"color={style['badge']}").classes("text-[10px]")
                flood_badge = ui.badge("FLOOD").props("color=negative")
                flood_badge.visible = False
                widgets["flood"] = flood_badge

            with ui.row().classes("w-full gap-2 mt-1 items-stretch no-wrap"):
                vessel_col = ui.column().classes("gap-0").style("width:28px;")
                outer, fill = _vessel(vessel_col)
                self._fills[sid] = fill

                with ui.column().classes("gap-1 flex-1"):
                    with ui.row().classes("gap-3 flex-wrap"):
                        widgets["T"] = theme.compact_metric("°C")
                        widgets["hex"] = theme.compact_metric("ppm")
                        widgets["water"] = theme.compact_metric("% H2O")
                    widgets["level_label"] = ui.label("- %").classes(
                        "text-[10px] font-mono text-gray-500"
                    )

                    if role == "SPARGE":
                        key = f"direct_steam/{sid}"
                        if key in mvs:
                            widgets["duty_slider"] = _mv_slider(
                                ui.column().classes("gap-0 w-full"),
                                self._facade,
                                key,
                                mvs[key],
                                "Direct steam [kg/s]",
                                theme.TEAL,
                            )
                    else:
                        key = f"indirect_steam/{sid}"
                        if key in mvs:
                            widgets["duty_slider"] = _mv_slider(
                                ui.column().classes("gap-0 w-full"),
                                self._facade,
                                key,
                                mvs[key],
                                "Indirect steam [kW]",
                                theme.TEAL,
                                scale=1.0e-3,
                            )

                    gate_key = f"gate_opening/{sid}"
                    if gate_key in mvs:
                        widgets["gate_slider"] = _mv_slider(
                            ui.column().classes("gap-0 w-full"),
                            self._facade,
                            gate_key,
                            mvs[gate_key],
                            "Gate opening [%] (bed level)",
                            theme.TEAL,
                        )

        self._cards[sid] = card
        self._widgets[sid] = widgets

    def _build_dc_tray(self, container: ui.column, sid: str, role: str) -> None:
        style = theme.ROLE_STYLE.get(role, {"border": "border-gray-400", "badge": "grey"})
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
                widgets["T"] = theme.compact_metric("°C")
                widgets["hex"] = theme.compact_metric("ppm")
                widgets["water"] = theme.compact_metric("% H2O")
            with ui.row().classes("gap-3 flex-wrap mt-1"):
                ui.label("Air in:").classes("text-[10px] text-gray-500")
                widgets["air"] = theme.compact_metric("°C / kg/s dry")
            with ui.row().classes("gap-3 flex-wrap mt-1"):
                ui.label("Air out:").classes("text-[10px] text-gray-500")
                # Dry-air flow is repeated here (unchanged from inlet) so the readout
                # can't be misread as "the air shrank" -- the air CONSERVES dry mass and
                # only GAINS humidity (g H2O per kg dry air) as it dries the meal.
                widgets["air_out"] = theme.compact_metric("°C / kg/s dry / g H2O·kg⁻¹")
            with ui.row().classes("items-center gap-2 w-full mt-1"):
                bar_bg = (
                    ui.element("div")
                    .classes("flex-1")
                    .style(
                        f"height:6px; border-radius:3px; background:{theme.BORDER}; "
                        "overflow:hidden;"
                    )
                )
                with bar_bg:
                    fill = ui.element("div").style(f"height:100%; width:0%; background:{theme.TEAL};")
                widgets["level_fill"] = fill
                widgets["level_label"] = ui.label("- %").classes(
                    "text-[10px] font-mono text-gray-500 whitespace-nowrap"
                )
        self._cards[sid] = card
        self._widgets[sid] = widgets

    # ------------------------------------------------------------------- sync
    def sync(self, snap, outputs) -> None:  # snap: engine.facade.Snapshot, avoids a core/ import
        feed_flow = snap.mvs["feed_flow_rate"].effective_value
        feed_temperature_k = snap.dvs["feed_temperature"]
        # The model stores feed_hexane/feed_moisture DRY-basis (kg/kg dry solid), but the feed
        # card shows them as TOTAL mass fraction of the wet meal (dry solid + moisture + hexane),
        # since that's how desolventizer feed is spec'd -- e.g. dry-basis 0.4743 hexane reads as
        # ~30% of wet mass, not "47%". (Oil ~1% is omitted from the denominator, negligible.)
        x1 = snap.dvs["feed_moisture"]
        x2 = snap.dvs["feed_hexane"]
        wet_denom = 1.0 + x1 + x2
        feed_hex_pct = x2 / wet_denom * 100.0
        feed_water_pct = x1 / wet_denom * 100.0

        if self._feed_widgets:
            self._feed_widgets["flow"].text = f"{feed_flow:.2f} kg/s"
            self._feed_widgets["T"].text = f"{theme.k_to_c(feed_temperature_k):.1f} °C"
            self._feed_widgets["hex"].text = f"{feed_hex_pct:.1f}% hex (wet)"
            self._feed_widgets["water"].text = f"{feed_water_pct:.1f}% H2O (wet)"
        if self._feed_card is not None:
            self._feed_card.style(
                replace=f"background-color: {theme.heat_color(feed_temperature_k, 0.12)}"
            )

        if outputs is None:
            return

        for sid in self._stage_order:
            widgets = self._widgets.get(sid)
            if not widgets:
                continue
            t_val = outputs.stage_T[sid]
            widgets["T"].text = f"{theme.k_to_c(t_val):.1f} °C"
            widgets["hex"].text = f"{outputs.stage_X_hex_ppm[sid]:.0f} ppm"
            widgets["water"].text = f"{outputs.stage_X_w_pct[sid]:.2f} % H2O"

            level_pct = outputs.stage_level_pct[sid]
            overfilled = level_pct > 100.0
            level_color = theme.RED if overfilled else theme.heat_color(t_val)
            widgets["level_label"].text = f"{level_pct:.0f} %"
            widgets["flood"].visible = overfilled

            fill = self._fills.get(sid)
            if fill is not None:
                fill.style(
                    replace=(
                        f"position:absolute; left:0; right:0; bottom:0; "
                        f"height:{min(max(level_pct, 0.0), 100.0):.0f}%; background:{level_color}; "
                        "transition:height 0.4s ease, background 0.4s ease;"
                    )
                )
            level_fill = widgets.get("level_fill")
            if level_fill is not None:
                level_fill.style(
                    replace=(
                        f"height:100%; width:{min(max(level_pct, 0.0), 100.0):.0f}%; "
                        f"background:{theme.RED if overfilled else theme.TEAL};"
                    )
                )

            card = self._cards.get(sid)
            if card is not None:
                card.style(replace=f"background-color: {theme.heat_color(t_val, 0.12)}")

            role = snap.stage_roles.get(sid, "")
            if "air" in widgets:
                if role == "DRYER":
                    t = snap.mvs["heated_air_temp"].effective_value
                    f = snap.mvs["heated_air_flow"].effective_value
                else:
                    t = snap.dvs["ambient_air_temp"]
                    f = snap.mvs["ambient_air_flow"].effective_value
                widgets["air"].text = f"{theme.k_to_c(t):.0f} °C / {f:.1f} kg/s"
                self._air_flow_by_sid[sid] = f
            if "air_out" in widgets and sid in outputs.stage_air_T_out:
                t_out = outputs.stage_air_T_out[sid]
                humidity_out_g_kg = outputs.stage_air_humidity_out[sid] * 1000.0
                flow = self._air_flow_by_sid.get(sid, 0.0)
                widgets["air_out"].text = (
                    f"{theme.k_to_c(t_out):.0f} °C / {flow:.1f} kg/s / {humidity_out_g_kg:.1f} g/kg"
                )

        if self._stage_order and self._product_widgets:
            last_sid = self._stage_order[-1]
            self._product_widgets["T"].text = f"{theme.k_to_c(outputs.stage_T[last_sid]):.1f} °C"
            self._product_widgets["hex"].text = f"{outputs.stage_X_hex_ppm[last_sid]:.0f} ppm"
            self._product_widgets["water"].text = f"{outputs.stage_X_w_pct[last_sid]:.2f} % H2O"
            if self._product_card is not None:
                self._product_card.style(
                    replace=(
                        f"background-color: {theme.heat_color(outputs.stage_T[last_sid], 0.12)}"
                    )
                )
