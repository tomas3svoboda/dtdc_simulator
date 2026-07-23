"""The DT/DC vessel schematic -- the tower's own visual centerpiece.

The whole unit is drawn as ONE stacked column (feed at the top, product at the
bottom): FEED -> PREDESOLV/MAIN/SPARGE (DT) -> DRYER/COOLER (DC) -> PRODUCT,
with downward solid-flow arrows between trays and an upward vapor arrow off the
DT top. Each DT tray is an actual vessel (a bordered box whose fill height
tracks `stage_level_pct` and whose color tracks temperature) laid out WIDE and
SHORT. Indirect steam is exposed as two physically meaningful zone totals
(PREDESOLV and TOAST), not as a fictitious valve on every tray; per-tray duty
MVs remain available through OPC UA for detailed actuator integration.

The redesign also folds the GLOBAL operator sliders onto the tray they act on
(BuildSpec §10, "controls at the area of relevance"): feed flow / arm speed /
feed-composition disturbances sit on the FEED card, the dryer-air sliders on the
DRYER card, and the cooler-air + ambient-weather sliders on the COOLER card --
so there's no separate control panel to hunt through. Slider builders live in
`controls.py`; this module just places them.

Talks only to `RuntimeFacade` + `Snapshot` (BuildSpec §3): never imports `core/`.
"""

from __future__ import annotations

from nicegui import ui

from dtdc_simulator.engine.facade import (
    MVSnapshot,
    RuntimeFacade,
    SteamInfo,
    TransferBoundaryInfo,
)
from dtdc_simulator.interfaces.ui import controls, theme

DT_ROLES = ("PREDESOLV", "MAIN", "SPARGE")
DC_ROLES = ("DRYER", "COOLER")

# Model caps holdup at tray capacity, so a backed-up tray reads ~100% (not >100).
_FLOOD_LEVEL_PCT = 99.5

_VESSEL_HEIGHT_PX = 44
_VESSEL_WIDTH_PX = 34


def _vessel(container: ui.element) -> tuple[ui.element, ui.element]:
    """A bordered vessel box + its bottom-anchored fill div. Returns
    `(outer, fill)` so `sync()` can restyle `fill`'s height/color each tick."""
    with container:
        outer = (
            ui.element("div")
            .classes("relative overflow-hidden")
            .style(
                f"height:{_VESSEL_HEIGHT_PX}px; width:{_VESSEL_WIDTH_PX}px; border-radius:6px; "
                f"border:1px solid {theme.BORDER}; background:#ffffff;"
            )
        )
        with outer:
            fill = ui.element("div").style(
                f"position:absolute; left:0; right:0; bottom:0; height:0%; "
                f"background:{theme.TEAL}; transition:height 0.4s ease, background 0.4s ease;"
            )
    return outer, fill


def _tray_mv_slider(
    container: ui.element,
    facade: RuntimeFacade,
    key: str,
    mv: MVSnapshot,
    label: str,
    color: str,
    scale: float = 1.0,
    step: float = 1.0,
) -> ui.slider:
    """One inline per-tray MV slider. `scale` converts the MV's SI storage unit
    to a nicer display unit (e.g. W -> kg/s of condensing steam); the facade
    always gets SI back. `step` snaps the display value so the label pill reads
    cleanly (e.g. 1.11, not 1.1061946…)."""
    with container:
        ui.label(label).classes("text-[10px] whitespace-nowrap").style(f"color:{theme.MUTED};")
        slider = (
            ui.slider(
                min=round(mv.min * scale, 3),
                max=round(mv.max * scale, 3),
                value=round(mv.effective_value * scale, 3),
                step=step,
                on_change=lambda e, k=key, s=scale: facade.set_mv_manual_setpoint(k, e.value / s),
            )
            .props(f"label-always color={color} dense")
            .classes("w-full")
        )
    return slider


def _cell(width: str = "flex:1 1 150px;"):
    """A gap-0 slider cell (label + slider stack) so embedded sliders pack tight."""
    return ui.column().classes("gap-0").style(width)


class TowerView:
    """Owns the tower's NiceGUI widgets and their live-sync state. `build()`
    (re)constructs the DOM when the stage set changes (rare); `sync()` runs
    every tick from `app.py`'s own timer."""

    def __init__(self, facade: RuntimeFacade, column: ui.column) -> None:
        self._facade = facade
        self._column = column
        self._stage_order: list[str] = []
        self._feed_card: ui.card | None = None
        self._product_card: ui.card | None = None
        self._feed_widgets: dict[str, ui.label] = {}
        self._product_widgets: dict[str, ui.label] = {}
        self._cards: dict[str, ui.card] = {}
        self._fills: dict[str, ui.element] = {}
        self._widgets: dict[str, dict[str, object]] = {}
        # Inter-tray solid-flow arrow captions, keyed by the stage whose OUTFLOW
        # they show ("FEED" -> first tray); plus the DT vapor-outlet up-arrow.
        self._flow_arrows: dict[str, ui.label] = {}
        # Countercurrent vapor captions keyed by the LOWER tray whose top-face
        # vapor rises across that connector. The bool marks the MN1 bypass.
        self._vapor_arrows: dict[str, tuple[ui.label, bool]] = {}
        self._vapor_caption: ui.label | None = None
        # Shared "Ambient Air" box arrows -> live air mass flow into DRYER/COOLER.
        self._ambient_arrows: dict[str, ui.label] = {}
        # Steam utility-box readouts, updated from the effective MVs each tick.
        self._steam_widgets: dict[str, ui.label] = {}
        self._steam_mv_keys: dict[str, tuple[str, ...]] = {}
        # Per-DC-stage inlet air flow, captured on the "air in" pass and echoed
        # on the "air out" line so the (conserved) dry-air flow reads on both.
        self._air_flow_by_sid: dict[str, float] = {}
        self._transfer_by_stage: dict[str, TransferBoundaryInfo] = {}

    # ------------------------------------------------------------------ build
    def build(
        self,
        stage_order: list[str],
        stage_roles: dict[str, str],
        mvs: dict[str, MVSnapshot],
        dvs: dict[str, float],
        steam: SteamInfo,
        transfer_boundaries: tuple[TransferBoundaryInfo, ...],
    ) -> None:
        self._stage_order = list(stage_order)
        self._column.clear()
        self._feed_widgets.clear()
        self._product_widgets.clear()
        self._cards.clear()
        self._fills.clear()
        self._widgets.clear()
        self._flow_arrows.clear()
        self._vapor_arrows.clear()
        self._ambient_arrows.clear()
        self._steam_widgets.clear()
        self._steam_mv_keys.clear()
        self._vapor_caption = None
        self._transfer_by_stage = {
            boundary.from_stage: boundary for boundary in transfer_boundaries
        }

        dt_ids = [sid for sid in stage_order if stage_roles.get(sid) in DT_ROLES]
        pred_ids = [sid for sid in dt_ids if stage_roles.get(sid) == "PREDESOLV"]
        main_ids = [sid for sid in dt_ids if stage_roles.get(sid) == "MAIN"]
        sparge_ids = [sid for sid in dt_ids if stage_roles.get(sid) == "SPARGE"]
        dc_ids = [sid for sid in stage_order if stage_roles.get(sid) in DC_ROLES]

        with self._column:
            # Vapor leaves the DT top -> condenser: an up-arrow above the feed.
            with ui.row().classes("w-full items-center justify-center gap-1"):
                ui.label("▲ vapor to condenser").style(
                    f"color:{theme.TEAL}; font-size:11px; font-weight:600;"
                )
                self._vapor_caption = ui.label("").classes("font-mono").style(
                    f"color:{theme.MUTED}; font-size:11px;"
                )
            self._build_feed_card(mvs, dvs)

            if dt_ids:
                self._add_flow_arrow("FEED")
                ui.label("Desolventizer / Toaster").classes("hmi-section-title mt-1")
                # Three physical groups keep each utility beside the equipment
                # it serves and consume the right-hand column all the way down:
                # PD trays + PD jacket, MAIN trays + toast jacket, SPARGE +
                # direct steam. Inter-group connectors stay full-width.
                if pred_ids:
                    with ui.row().classes("w-full gap-3 items-stretch no-wrap"):
                        with ui.column().classes("gap-1 flex-1"):
                            for pos, sid in enumerate(pred_ids):
                                self._build_dt_tray(sid, stage_roles[sid], mvs)
                                if pos < len(pred_ids) - 1:
                                    self._add_dt_flow_connector(
                                        sid, vapor_key=None
                                    )
                        self._build_indirect_steam_zone_box(
                            pred_ids,
                            mvs,
                            steam,
                            title="PREDESOLV INDIRECT STEAM",
                            subtitle="PD1–PD3 jacket circuit",
                            slider_label="Predesolv steam [kg/s total]",
                            flow_widget_key="pred_indirect_flow",
                        )

                if pred_ids and (main_ids or sparge_ids):
                    next_id = (main_ids or sparge_ids)[0]
                    self._add_dt_flow_connector(
                        pred_ids[-1], vapor_key=next_id, bypass=True
                    )

                if main_ids:
                    with ui.row().classes("w-full gap-3 items-stretch no-wrap"):
                        with ui.column().classes("gap-1 flex-1"):
                            for pos, sid in enumerate(main_ids):
                                self._build_dt_tray(sid, stage_roles[sid], mvs)
                                if pos < len(main_ids) - 1:
                                    self._add_dt_flow_connector(
                                        sid,
                                        vapor_key=main_ids[pos + 1],
                                    )
                        self._build_indirect_steam_zone_box(
                            main_ids + sparge_ids,
                            mvs,
                            steam,
                            title="MAIN-TRAY INDIRECT STEAM",
                            subtitle="MN1–SP1 jacket circuit",
                            slider_label="Main/toast steam [kg/s total]",
                            flow_widget_key="toast_indirect_flow",
                        )

                if main_ids and sparge_ids:
                    self._add_dt_flow_connector(
                        main_ids[-1], vapor_key=sparge_ids[0]
                    )

                if sparge_ids:
                    with ui.row().classes("w-full gap-3 items-start no-wrap"):
                        with ui.column().classes("gap-1 flex-1"):
                            for pos, sid in enumerate(sparge_ids):
                                self._build_dt_tray(sid, stage_roles[sid], mvs)
                                if pos < len(sparge_ids) - 1:
                                    self._add_dt_flow_connector(
                                        sid,
                                        vapor_key=sparge_ids[pos + 1],
                                    )
                        self._build_direct_steam_box(sparge_ids, mvs, steam)

            # DC section: the DRYER/COOLER trays in a sub-column, with a shared
            # AMBIENT AIR box beside them (its weather feeds BOTH contactors).
            if dc_ids:
                if dt_ids:
                    self._add_flow_arrow(dt_ids[-1])
                ui.label("Dryer / Cooler").classes("hmi-section-title mt-1")
                with ui.row().classes("w-full gap-3 items-start no-wrap"):
                    with ui.column().classes("gap-1 flex-1"):
                        for pos, sid in enumerate(dc_ids):
                            self._build_dc_tray(sid, stage_roles[sid], mvs)
                            if pos < len(dc_ids) - 1:
                                self._add_flow_arrow(sid)
                    self._build_ambient_box(dvs)

            if stage_order:
                self._add_flow_arrow(stage_order[-1])
            self._build_product_card()

    def _add_flow_arrow(self, key: str) -> None:
        # Builds into the current layout context (the main column, or a DC
        # sub-column), so callers control placement via their own `with` block.
        self._flow_arrows[key] = theme.flow_arrow(down=True)

    def _add_dt_flow_connector(
        self,
        solid_key: str,
        *,
        vapor_key: str | None,
        bypass: bool = False,
    ) -> None:
        """Paired DT connector: falling meal and countercurrent rising vapor."""
        with ui.row().classes("w-full items-center justify-center gap-6").style(
            "margin:-2px 0;"
        ):
            with ui.row().classes("items-center gap-1"):
                ui.label("▼").style(
                    f"color:{theme.TEAL}; font-size:11px; line-height:1;"
                )
                self._flow_arrows[solid_key] = ui.label("").classes("font-mono").style(
                    f"color:{theme.MUTED}; font-size:10px; line-height:1;"
                )
            if vapor_key is not None:
                with ui.row().classes("items-center gap-1"):
                    ui.label("▲").style(
                        f"color:{theme.STEAM_BORDER}; font-size:11px; line-height:1;"
                    )
                    caption = ui.label("").classes("font-mono").style(
                        f"color:{theme.MUTED}; font-size:10px; line-height:1;"
                    )
                    self._vapor_arrows[vapor_key] = (caption, bypass)

    def _build_feed_card(self, mvs: dict[str, MVSnapshot], dvs: dict[str, float]) -> None:
        with (
            ui.card()
            .classes(f"w-full border-l-4 {theme.FEED_BORDER}")
            .style("padding: 8px 10px;") as feed_card
        ):
            self._feed_card = feed_card
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("FEED").classes("font-bold text-xs")
                ui.badge("IN").props("color=grey")
            with ui.row().classes("w-full gap-4 items-start no-wrap mt-1"):
                with ui.column().classes("gap-1").style("min-width:150px;"):
                    self._feed_widgets["flow"] = theme.compact_metric("kg/s")
                    self._feed_widgets["T"] = theme.compact_metric("°C")
                    self._feed_widgets["hex"] = theme.compact_metric("% hex")
                    self._feed_widgets["water"] = theme.compact_metric("% H2O")
                # ◀ feed-stream operator inputs, folded onto the feed itself.
                with ui.column().classes("gap-0 flex-1"):
                    ui.label("◀ feed & throughput").classes("text-[10px]").style(
                        f"color:{theme.TEAL};"
                    )
                    with ui.row().classes("w-full gap-4 items-end flex-wrap"):
                        with _cell():
                            controls.mv_slider(
                                self._facade, "feed_flow_rate", "Feed flow [kg/s]",
                                value=mvs["feed_flow_rate"].effective_value,
                            )
                        with _cell():
                            controls.arm_speed_slider(self._facade, value=_mean_arm_rpm(mvs))
                    ui.label("◀ feed disturbances").classes("text-[10px] mt-1").style(
                        f"color:{theme.AMBER};"
                    )
                    with ui.row().classes("w-full gap-4 items-end flex-wrap"):
                        with _cell():
                            controls.dv_slider(
                                self._facade, "feed_temperature", "Feed temp [°C]", 40, 80,
                                value=dvs["feed_temperature"], to_si=theme.c_to_k, from_si=theme.k_to_c,
                            )
                        with _cell():
                            controls.wet_moisture_slider(
                                self._facade,
                                x1=dvs["feed_moisture"],
                                x2=dvs["feed_hexane"],
                                x3=dvs["feed_oil"],
                            )
                        with _cell():
                            controls.wet_hexane_slider(
                                self._facade,
                                x2=dvs["feed_hexane"],
                                x1=dvs["feed_moisture"],
                                x3=dvs["feed_oil"],
                            )
                        with _cell():
                            controls.dv_slider(
                                self._facade, "feed_oil", "Feed oil [% dry basis]", 0, 5,
                                value=dvs["feed_oil"], scale=100.0,
                            )

    def _build_indirect_steam_zone_box(
        self,
        stage_ids: list[str],
        mvs: dict[str, MVSnapshot],
        steam: SteamInfo,
        *,
        title: str,
        subtitle: str,
        slider_label: str,
        flow_widget_key: str,
    ) -> None:
        keys = [
            f"indirect_steam/{sid}"
            for sid in stage_ids
            if f"indirect_steam/{sid}" in mvs
        ]
        self._steam_mv_keys[flow_widget_key] = tuple(keys)
        with ui.card().classes("border-l-4 self-stretch").style(
            f"padding:8px 10px; width:220px; min-height:100%; "
            f"background:{theme.STEAM_SURFACE}; "
            f"border-left-color:{theme.STEAM_BORDER};"
        ):
            ui.label(title).classes("font-bold text-[11px]")
            ui.label(subtitle).classes("text-[10px]").style(
                f"color:{theme.MUTED};"
            )
            with ui.column().classes("gap-0"):
                ui.label(
                    f"{steam.supply_barg:.1f} barg · "
                    f"{theme.k_to_c(steam.supply_T_K):.0f} °C sat."
                ).classes("text-[11px] font-mono")
                self._steam_widgets[flow_widget_key] = ui.label("").classes(
                    "text-[11px] font-mono"
                )
            with _cell("width:100%;"):
                controls.indirect_steam_zone_slider(
                    self._facade,
                    keys=keys,
                    mvs=mvs,
                    label=slider_label,
                    dH_vap_water=steam.dH_vap_water,
                )
            ui.label("fixed configured tray allocation").classes("text-[10px]").style(
                f"color:{theme.MUTED};"
            )

    def _build_direct_steam_box(
        self,
        sparge_ids: list[str],
        mvs: dict[str, MVSnapshot],
        steam: SteamInfo,
    ) -> None:
        direct_keys = [
            f"direct_steam/{sid}"
            for sid in sparge_ids
            if f"direct_steam/{sid}" in mvs
        ]
        with ui.card().classes("border-l-4").style(
            f"padding:6px 8px; width:220px; flex:0 0 220px; "
            f"background:{theme.STEAM_SURFACE}; "
            f"border-left-color:{theme.STEAM_BORDER};"
        ):
            with ui.column().classes("gap-1 w-full"):
                ui.label("DIRECT STEAM").classes("font-bold text-[11px]")
                ui.label("sparge steam at meal contact").classes("text-[10px]").style(
                    f"color:{theme.MUTED};"
                )
                ui.label(
                    f"{steam.direct_contact_barg:.1f} barg · "
                    f"{theme.k_to_c(steam.direct_contact_T_K):.0f} °C sat."
                ).classes("text-[11px] font-mono")
                self._steam_widgets["direct_flow"] = ui.label("").classes(
                    "text-[11px] font-mono"
                )
                if direct_keys:
                    key = direct_keys[0]
                    _tray_mv_slider(
                        _cell("width:100%;"),
                        self._facade,
                        key,
                        mvs[key],
                        "Direct steam [kg/s total]",
                        theme.TEAL,
                        step=0.05,
                    )
                ui.label(f"from {steam.supply_barg:.1f} barg header").classes(
                    "text-[10px]"
                ).style(f"color:{theme.MUTED};")

    def _build_product_card(self) -> None:
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

    def _build_ambient_box(self, dvs: dict[str, float]) -> None:
        """Shared AMBIENT-AIR box beside the DRYER + COOLER: the weather (temp,
        RH) that feeds BOTH contactors -- the dryer's air is this same ambient
        parcel HEATED (so ambient temp sets the drying-air heating duty and the
        absolute humidity carried in), the cooler's air is it directly. The two
        arrows show the live air mass flow into each (filled by `sync()`)."""
        with ui.card().classes("border-l-4 border-cyan-500").style(
            "padding:6px 8px; width:220px; flex:0 0 220px;"
        ):
            with ui.column().classes("gap-1 w-full"):
                ui.label("AMBIENT AIR").classes("font-bold text-[11px]")
                ui.label("weather → both contactors").classes("text-[10px]").style(
                    f"color:{theme.MUTED};"
                )
                with _cell("width:100%;"):
                    controls.dv_slider(
                        self._facade, "ambient_air_temp", "Ambient temp [°C]", -20, 45,
                        value=dvs["ambient_air_temp"],
                        to_si=theme.c_to_k,
                        from_si=theme.k_to_c,
                    )
                with _cell("width:100%;"):
                    controls.dv_slider(
                        self._facade,
                        "ambient_relative_humidity",
                        "Ambient RH [%]",
                        0,
                        100,
                        value=dvs["ambient_relative_humidity"],
                        scale=100.0,
                    )
                with ui.column().classes("gap-0"):
                    self._ambient_arrows["DRYER"] = ui.label("→ Dryer").classes(
                        "text-[11px] font-mono"
                    ).style(f"color:{theme.TEAL};")
                    self._ambient_arrows["COOLER"] = ui.label("→ Cooler").classes(
                        "text-[11px] font-mono"
                    ).style(f"color:{theme.TEAL};")

    def _build_dt_tray(
        self, sid: str, role: str, mvs: dict[str, MVSnapshot]
    ) -> None:
        style = theme.ROLE_STYLE.get(role, {"border": "border-gray-400", "badge": "grey"})
        widgets: dict[str, object] = {}
        with ui.card().classes(f"w-full border-l-4 {style['border']}").style(
            "padding:8px; flex:0 0 auto;"
        ) as card:
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(sid).classes("font-bold text-xs")
                    ui.badge(role).props(f"color={style['badge']}").classes("text-[10px]")
                flood_badge = ui.badge("FLOOD").props("color=negative")
                flood_badge.visible = False
                widgets["flood"] = flood_badge

            # Wide + short: vessel | readouts | inline MV sliders, side by side.
            with ui.row().classes("w-full gap-3 mt-1 items-start no-wrap"):
                vessel_col = ui.column().classes("gap-0")
                _outer, fill = _vessel(vessel_col)
                self._fills[sid] = fill

                with ui.column().classes("gap-1").style("min-width:150px;"):
                    with ui.row().classes("gap-3 flex-wrap"):
                        widgets["T"] = theme.compact_metric("°C")
                        widgets["hex"] = theme.compact_metric("ppm")
                        widgets["water"] = theme.compact_metric("% H2O")
                    widgets["level_label"] = ui.label("- %").classes(
                        "text-[10px] font-mono text-gray-500"
                    )

                with ui.row().classes("gap-3 items-end flex-1 no-wrap"):
                    boundary = self._transfer_by_stage.get(sid)
                    position_key = (
                        f"transfer_device_position/{boundary.id}" if boundary else ""
                    )
                    if position_key in mvs:
                        device = (
                            "Rotary airlock"
                            if boundary.device_type == "ROTARY_AIRLOCK"
                            else "Discharge gate"
                        )
                        widgets["gate_slider"] = _tray_mv_slider(
                            _cell(),
                            self._facade,
                            position_key,
                            mvs[position_key],
                            f"{device} [%]",
                            theme.TEAL,
                            step=1.0,
                        )

        self._cards[sid] = card
        self._widgets[sid] = widgets

    def _build_dc_tray(self, sid: str, role: str, mvs: dict[str, MVSnapshot]) -> None:
        style = theme.ROLE_STYLE.get(role, {"border": "border-gray-400", "badge": "grey"})
        widgets: dict[str, object] = {}
        with ui.card().classes(f"w-full border-l-4 {style['border']}").style(
            "padding:8px 10px; flex:0 0 auto;"
        ) as card:
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(sid).classes("font-bold text-xs")
                    ui.badge(role).props(f"color={style['badge']}").classes("text-[10px]")
                flood_badge = ui.badge("FLOOD").props("color=negative")
                flood_badge.visible = False
                widgets["flood"] = flood_badge

            # Wide + short: readouts on the left, air controls on the right.
            with ui.row().classes("w-full gap-4 items-start no-wrap mt-1"):
                with ui.column().classes("gap-1").style("min-width:230px;"):
                    with ui.row().classes("gap-3 flex-wrap"):
                        widgets["T"] = theme.compact_metric("°C")
                        widgets["hex"] = theme.compact_metric("ppm")
                        widgets["water"] = theme.compact_metric("% H2O")
                    with ui.row().classes("gap-3 flex-wrap"):
                        ui.label("→ Air in").classes("text-[10px] text-gray-500")
                        widgets["air"] = theme.compact_metric("°C / kg/s dry")
                    with ui.row().classes("gap-3 flex-wrap"):
                        ui.label("Air out →").classes("text-[10px] text-gray-500")
                        widgets["air_out"] = theme.compact_metric("°C / kg/s dry / g H2O·kg⁻¹")
                    with ui.row().classes("gap-3 flex-wrap"):
                        ui.label("Air hexane:").classes("text-[10px] text-gray-500")
                        widgets["air_hex"] = theme.compact_metric("ppm (limit 1100)")
                    with ui.row().classes("items-center gap-2 w-full"):
                        bar_bg = (
                            ui.element("div").classes("flex-1").style(
                                f"height:6px; border-radius:3px; background:{theme.BORDER}; overflow:hidden;"
                            )
                        )
                        with bar_bg:
                            fill = ui.element("div").style(
                                f"height:100%; width:0%; background:{theme.TEAL};"
                            )
                        widgets["level_fill"] = fill
                        widgets["level_label"] = ui.label("- %").classes(
                            "text-[10px] font-mono text-gray-500 whitespace-nowrap"
                        )

                # ◀ air-side operator inputs, folded onto the contactor.
                with ui.column().classes("gap-0 flex-1"):
                    boundary = self._transfer_by_stage.get(sid)
                    position_key = (
                        f"transfer_device_position/{boundary.id}" if boundary else ""
                    )
                    device = (
                        "Rotary airlock"
                        if boundary and boundary.device_type == "ROTARY_AIRLOCK"
                        else "Discharge gate"
                    )
                    if role == "DRYER":
                        ui.label("◀ dryer air").classes("text-[10px]").style(f"color:{theme.TEAL};")
                        with ui.row().classes("w-full gap-4 items-end flex-wrap"):
                            with _cell():
                                controls.mv_slider(
                                    self._facade, "heated_air_temp", "Air temp [°C]",
                                    value=mvs["heated_air_temp"].effective_value,
                                    to_display=theme.k_to_c, from_display=theme.c_to_k,
                                )
                            with _cell():
                                controls.mv_slider(
                                    self._facade, "heated_air_flow", "Air flow [kg/s]",
                                    value=mvs["heated_air_flow"].effective_value,
                                )
                            if position_key in mvs:
                                widgets["gate_slider"] = _tray_mv_slider(
                                    _cell(),
                                    self._facade,
                                    position_key,
                                    mvs[position_key],
                                    f"{device} [%]",
                                    theme.TEAL,
                                    step=1.0,
                                )
                    else:  # COOLER: just its own air flow (ambient weather is the shared box)
                        ui.label("◀ cooler air").classes("text-[10px]").style(f"color:{theme.TEAL};")
                        with ui.row().classes("w-full gap-4 items-end flex-wrap"):
                            with _cell():
                                controls.mv_slider(
                                    self._facade, "ambient_air_flow", "Air flow [kg/s]",
                                    value=mvs["ambient_air_flow"].effective_value,
                                )
                            if position_key in mvs:
                                widgets["gate_slider"] = _tray_mv_slider(
                                    _cell(),
                                    self._facade,
                                    position_key,
                                    mvs[position_key],
                                    f"{device} [%]",
                                    theme.TEAL,
                                    step=1.0,
                                )
        self._cards[sid] = card
        self._widgets[sid] = widgets

    # ------------------------------------------------------------------- sync
    def sync(self, snap, outputs) -> None:  # snap: engine.facade.Snapshot, avoids a core/ import
        feed_flow = snap.mvs["feed_flow_rate"].effective_value
        feed_temperature_k = snap.dvs["feed_temperature"]
        # The model stores feed_hexane/feed_moisture DRY-basis (kg/kg dry solid), but the feed
        # card shows them as TOTAL mass fraction of the wet meal (dry solid + moisture + hexane).
        x1 = snap.dvs["feed_moisture"]
        x2 = snap.dvs["feed_hexane"]
        x3 = snap.dvs["feed_oil"]
        wet_denom = 1.0 + x1 + x2 + x3
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

        # Ambient-air box: live air mass flow into each contactor (dryer air is
        # this ambient parcel heated; cooler air is it directly).
        if self._ambient_arrows:
            self._ambient_arrows["DRYER"].text = (
                f"→ Dryer  {snap.mvs['heated_air_flow'].effective_value:.0f} kg/s (heated)"
            )
            self._ambient_arrows["COOLER"].text = (
                f"→ Cooler  {snap.mvs['ambient_air_flow'].effective_value:.0f} kg/s"
            )
        if self._steam_widgets and snap.steam is not None:
            direct_kg_s = sum(
                mv.effective_value
                for key, mv in snap.mvs.items()
                if key.startswith("direct_steam/")
            )
            for widget_key, keys in self._steam_mv_keys.items():
                flow = sum(snap.mvs[key].effective_value for key in keys)
                flow /= snap.steam.dH_vap_water
                widget = self._steam_widgets.get(widget_key)
                if widget is not None:
                    widget.text = f"Absolute flow  {flow:.2f} kg/s"
            direct_flow = self._steam_widgets.get("direct_flow")
            if direct_flow is not None:
                direct_flow.text = f"Absolute flow  {direct_kg_s:.2f} kg/s"

        if outputs is None:
            return

        # Annotate the stream-flow arrows. "FEED" -> incoming feed rate; each
        # stage key -> its NET solid discharge; vapor arrow -> DT top outlet.
        if "FEED" in self._flow_arrows:
            self._flow_arrows["FEED"].text = f"{feed_flow:.1f} kg/s"
        if self._vapor_caption is not None:
            self._vapor_caption.text = f"{outputs.kpi_outlet_vapor_kg_s:.2f} kg/s"
        for sid, caption in self._flow_arrows.items():
            if sid == "FEED":
                continue
            caption.text = f"{outputs.stage_solid_out_kg_s.get(sid, 0.0):.1f} kg/s"
        for sid, (caption, bypass) in self._vapor_arrows.items():
            flow = outputs.stage_vapor_flow_kg_s.get(sid, 0.0)
            qualifier = " bypass → condenser" if bypass else " vapor"
            caption.text = f"{flow:.2f} kg/s{qualifier}"

        for sid in self._stage_order:
            widgets = self._widgets.get(sid)
            if not widgets:
                continue
            t_val = outputs.stage_T[sid]
            widgets["T"].text = f"{theme.k_to_c(t_val):.1f} °C"
            widgets["hex"].text = f"{outputs.stage_X_hex_ppm[sid]:.0f} ppm"
            widgets["water"].text = f"{outputs.stage_X_w_pct[sid]:.2f} % H2O"

            level_pct = outputs.stage_level_pct[sid]
            # Holdup is capacity-capped in the model now, so a backed-up tray
            # sits AT ~100% (rejecting inflow upstream) rather than climbing past
            # it -- FLOOD triggers at capacity, not at a strict >100%.
            overfilled = level_pct >= _FLOOD_LEVEL_PCT
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
            air_hex_w = widgets.get("air_hex")
            if air_hex_w is not None and sid in outputs.stage_air_hexane_ppm:
                hex_ppm = outputs.stage_air_hexane_ppm[sid]
                over = hex_ppm > 1100.0
                air_hex_w.text = f"{hex_ppm:.0f} ppm" + (" ⚠ OVER LEL" if over else "")
                air_hex_w.style(replace=f"color: {theme.RED if over else theme.DARK};")

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


def _mean_arm_rpm(mvs: dict[str, MVSnapshot]) -> float:
    """Mean sweep-arm rpm across stages, to seed the single global arm slider."""
    vals = [mv.effective_value for k, mv in mvs.items() if k.split("/", 1)[0] == "sweep_arm_speed"]
    return sum(vals) / len(vals) if vals else 3.0
