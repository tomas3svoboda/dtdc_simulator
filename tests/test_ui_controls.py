from __future__ import annotations

import pytest

from dtdc_simulator.interfaces.ui.app import (
    TREND_HEIGHT_PX,
    _temperature_trend_value,
    _whole_trend_point,
)
from dtdc_simulator.interfaces.ui.controls import (
    _dry_basis_from_wet_pct,
    _wet_pct_from_dry_basis,
)


def test_trend_height_and_numeric_precision() -> None:
    assert TREND_HEIGHT_PX == 420
    assert _whole_trend_point(123.6, 284.4) == (124, 284)
    assert _temperature_trend_value(330.399) == pytest.approx(57.2)


def test_coamo_feed_wet_basis_includes_all_components() -> None:
    x1, x2, x3 = 0.124, 0.388, 0.0137
    denominator = 1.0 + x1 + x2 + x3

    assert _wet_pct_from_dry_basis(x1, x2, x3) == pytest.approx(
        100.0 * x1 / denominator
    )
    assert _wet_pct_from_dry_basis(x2, x1, x3) == pytest.approx(
        100.0 * x2 / denominator
    )


@pytest.mark.parametrize(
    ("dry_value", "others"),
    [
        (0.124, (0.388, 0.0137)),
        (0.388, (0.124, 0.0137)),
    ],
)
def test_wet_dry_basis_conversion_round_trips(
    dry_value: float,
    others: tuple[float, ...],
) -> None:
    wet_pct = _wet_pct_from_dry_basis(dry_value, *others)
    assert _dry_basis_from_wet_pct(wet_pct, *others) == pytest.approx(dry_value)
