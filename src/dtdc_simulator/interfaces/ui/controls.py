"""Reusable operator-slider builders, used by `tower.py` to embed each global
MV/DV control directly onto the tray it acts on (feed sliders on the FEED card,
dryer-air sliders on the DRYER card, etc. -- the redesign's "controls at the
area of relevance"). MVs render teal (`theme.TEAL`), disturbances amber
(`theme.AMBER`), so "what I set" reads apart from "what the process throws at me".

Each builder seeds its slider from a value the caller pulls out of the live
`Snapshot` (so a freshly-assembled scenario shows its own defaults) and wires
`on_change` straight to `RuntimeFacade`. Talks only to `RuntimeFacade`
(BuildSpec §3); `MV_LIMITS` is plain range data, not `core/`.
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from dtdc_simulator.engine.facade import MV_LIMITS, MVSnapshot, RuntimeFacade
from dtdc_simulator.interfaces.ui import theme

# Group prefix an "arm rotation speed" slider broadcasts to (every sweep_arm_speed/<stage>).
_ARM_SPEED_GROUP = "sweep_arm_speed"

_Conv = Callable[[float], float]

# Snap seeds + drags to this step so the label pill reads "56.9", not
# "56.85000000000002" (raw float noise from the K<->°C / fraction<->% converts).
_STEP = 0.1


def _seed(value: float, lo: float, hi: float) -> float:
    return round(min(max(value, lo), hi), 1)


def _wet_pct_from_dry_basis(value: float, *other_components: float) -> float:
    denominator = 1.0 + value + sum(other_components)
    return 100.0 * value / denominator if denominator > 0.0 else 0.0


def _dry_basis_from_wet_pct(wet_pct: float, *other_components: float) -> float:
    wet_fraction = min(max(wet_pct / 100.0, 0.0), 0.95)
    return wet_fraction * (1.0 + sum(other_components)) / (1.0 - wet_fraction)


def _label(text: str, accent: str) -> None:
    ui.label(text).classes("text-[11px] whitespace-nowrap").style(f"color:{accent};")


def mv_slider(
    facade: RuntimeFacade,
    key: str,
    label: str,
    *,
    value: float,
    to_display: _Conv | None = None,
    from_display: _Conv | None = None,
    width: str = "w-full",
) -> ui.slider:
    """A teal manipulated-variable slider seeded at `value` (SI). `to_display`/
    `from_display` convert SI<->display (e.g. K<->°C); `on_change` pushes the
    SI value back to the facade."""
    lo, hi, _rate = MV_LIMITS[key]
    to_display = to_display or (lambda v: v)
    from_display = from_display or (lambda v: v)

    def on_change(e) -> None:
        facade.set_mv_manual_setpoint(key, from_display(e.value))

    _label(label, theme.MUTED)
    return (
        ui.slider(
            min=to_display(lo),
            max=to_display(hi),
            value=_seed(to_display(value), to_display(lo), to_display(hi)),
            step=_STEP,
            on_change=on_change,
        )
        .classes(width)
        .props("label-always color=primary dense")  # inject_theme maps primary->TEAL
    )


def dv_slider(
    facade: RuntimeFacade,
    key: str,
    label: str,
    lo: float,
    hi: float,
    *,
    value: float,
    scale: float = 1.0,
    to_si: _Conv | None = None,
    from_si: _Conv | None = None,
    width: str = "w-full",
) -> ui.slider:
    """An amber disturbance slider. `lo`/`hi` are in DISPLAY units; `scale`
    (or `to_si`/`from_si`) converts display<->SI. Seeded at the SI `value`."""
    to_si = to_si or (lambda v: v / scale)
    from_si = from_si or (lambda v: v * scale)

    def on_change(e) -> None:
        facade.set_dv(key, to_si(e.value))

    _label(label, theme.AMBER)
    return (
        ui.slider(min=lo, max=hi, value=_seed(from_si(value), lo, hi), step=_STEP, on_change=on_change)
        .classes(width)
        .props("label-always color=warning dense")  # inject_theme maps warning->AMBER
    )


def wet_hexane_slider(
    facade: RuntimeFacade,
    *,
    x2: float,
    x1: float,
    x3: float,
    lo: float = 10.0,
    hi: float = 45.0,
    width: str = "w-full",
) -> ui.slider:
    """Feed hexane as a WET-basis % (mass hexane / wet meal) -- how desolventizer
    feed is actually spec'd -- instead of the model's dry-basis storage. Uses the
    SAME complete denominator as the FEED card readout. Conversion reads live
    moisture and oil so all composition controls remain mutually consistent.
        w = X2/(1+X1+X2+X3)   ->   X2 = w(1+X1+X3)/(1-w)
    """

    def on_change(e) -> None:
        dvs = facade.get_snapshot().dvs
        facade.set_dv(
            "feed_hexane",
            _dry_basis_from_wet_pct(
                e.value,
                dvs["feed_moisture"],
                dvs["feed_oil"],
            ),
        )

    seed = _wet_pct_from_dry_basis(x2, x1, x3)
    _label("Feed hexane [% wet]", theme.AMBER)
    return (
        ui.slider(min=lo, max=hi, value=_seed(seed, lo, hi), step=_STEP, on_change=on_change)
        .classes(width)
        .props("label-always color=warning dense")
    )


def wet_moisture_slider(
    facade: RuntimeFacade,
    *,
    x1: float,
    x2: float,
    x3: float,
    lo: float = 5.0,
    hi: float = 25.0,
    width: str = "w-full",
) -> ui.slider:
    """Feed moisture in the same total-wet-meal basis as the feed card."""

    def on_change(e) -> None:
        dvs = facade.get_snapshot().dvs
        facade.set_dv(
            "feed_moisture",
            _dry_basis_from_wet_pct(
                e.value,
                dvs["feed_hexane"],
                dvs["feed_oil"],
            ),
        )

    seed = _wet_pct_from_dry_basis(x1, x2, x3)
    _label("Feed moisture [% wet]", theme.AMBER)
    return (
        ui.slider(min=lo, max=hi, value=_seed(seed, lo, hi), step=_STEP, on_change=on_change)
        .classes(width)
        .props("label-always color=warning dense")
    )


def indirect_steam_zone_slider(
    facade: RuntimeFacade,
    *,
    keys: list[str],
    mvs: dict[str, MVSnapshot],
    label: str,
    dH_vap_water: float,
    width: str = "w-full",
) -> ui.slider:
    """One zone-level indirect-steam control backed by per-tray heat-duty MVs."""
    live_keys = [key for key in keys if key in mvs]
    total_w = sum(mvs[key].effective_value for key in live_keys)
    maximum_w = sum(mvs[key].max for key in live_keys)
    scale = 1.0 / dH_vap_water

    def on_change(e) -> None:
        facade.set_mv_weighted_group_manual_total(
            live_keys,
            float(e.value) / scale,
        )

    _label(label, theme.MUTED)
    return (
        ui.slider(
            min=0.0,
            max=round(maximum_w * scale, 3),
            value=round(total_w * scale, 3),
            step=0.01,
            on_change=on_change,
        )
        .classes(width)
        .props("label-always color=primary dense")
    )


def arm_speed_slider(facade: RuntimeFacade, *, value: float, width: str = "w-full") -> ui.slider:
    """One global sweep-arm-speed slider broadcasting to every
    `sweep_arm_speed/<stage>` MV (`facade.set_mv_group_manual_setpoint`)."""
    lo, hi, _rate = MV_LIMITS["sweep_arm_speed"]

    def on_change(e) -> None:
        facade.set_mv_group_manual_setpoint(_ARM_SPEED_GROUP, float(e.value))

    _label("Arm rotation speed [rpm]  ⟳ all trays", theme.MUTED)
    return (
        ui.slider(min=lo, max=hi, value=_seed(value, lo, hi), step=_STEP, on_change=on_change)
        .classes(width)
        .props("label-always color=primary dense")
    )
