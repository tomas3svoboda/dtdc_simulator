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

from dtdc_simulator.engine.facade import MV_LIMITS, RuntimeFacade
from dtdc_simulator.interfaces.ui import theme

# Group prefix an "arm rotation speed" slider broadcasts to (every sweep_arm_speed/<stage>).
_ARM_SPEED_GROUP = "sweep_arm_speed"

_Conv = Callable[[float], float]

# Snap seeds + drags to this step so the label pill reads "56.9", not
# "56.85000000000002" (raw float noise from the K<->°C / fraction<->% converts).
_STEP = 0.1


def _seed(value: float, lo: float, hi: float) -> float:
    return round(min(max(value, lo), hi), 1)


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
    facade: RuntimeFacade, *, x2: float, x1: float, lo: float = 10.0, hi: float = 45.0,
    width: str = "w-full",
) -> ui.slider:
    """Feed hexane as a WET-basis % (mass hexane / wet meal) -- how desolventizer
    feed is actually spec'd -- instead of the model's dry-basis storage. Uses the
    SAME denominator as the FEED card readout (`X2/(1+X1+X2)`, oil omitted as a
    ~1% simplification) so slider and card agree. Conversion needs the moisture
    `X1`: the seed uses the build-time value; on change we read the LIVE moisture
    off the facade, so the wet-basis reading stays honest as moisture is moved.
        w = X2/(1+X1+X2)   ->   X2 = w(1+X1)/(1-w)
    """

    def on_change(e) -> None:
        w = min(max(e.value / 100.0, 0.0), 0.95)
        x1_now = facade.get_snapshot().dvs["feed_moisture"]
        facade.set_dv("feed_hexane", w * (1.0 + x1_now) / (1.0 - w))

    denom = 1.0 + x1 + x2
    seed = x2 / denom * 100.0 if denom > 0.0 else 0.0
    _label("Feed hexane [% wet]", theme.AMBER)
    return (
        ui.slider(min=lo, max=hi, value=_seed(seed, lo, hi), step=_STEP, on_change=on_change)
        .classes(width)
        .props("label-always color=warning dense")
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
