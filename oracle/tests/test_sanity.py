"""Tests for :func:`oracle.core.sanity.check_sanity_band`.

For each V1 metric we pin the four boundary cases: exactly at min, exactly at
max, just below min, just above max. We also cover the three unit conversions
so a rate quoted in percent or basis points is compared against the correct
band.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from oracle.config import Metric, SANITY_BANDS, Unit
from oracle.core.sanity import check_sanity_band, to_decimal_fraction


ONE_TICK = Decimal("0.0000001")  # well below any per-metric tolerance


@pytest.mark.parametrize("metric", list(Metric))
class TestBoundaryPerMetric:
    def test_exactly_at_min_passes(self, metric: Metric) -> None:
        band = SANITY_BANDS[metric]
        assert check_sanity_band(metric, band.min_value, Unit.DECIMAL_FRACTION) is True

    def test_exactly_at_max_passes(self, metric: Metric) -> None:
        band = SANITY_BANDS[metric]
        assert check_sanity_band(metric, band.max_value, Unit.DECIMAL_FRACTION) is True

    def test_just_below_min_fails(self, metric: Metric) -> None:
        band = SANITY_BANDS[metric]
        assert (
            check_sanity_band(metric, band.min_value - ONE_TICK, Unit.DECIMAL_FRACTION)
            is False
        )

    def test_just_above_max_fails(self, metric: Metric) -> None:
        band = SANITY_BANDS[metric]
        assert (
            check_sanity_band(metric, band.max_value + ONE_TICK, Unit.DECIMAL_FRACTION)
            is False
        )


class TestUnitConversion:
    """A value in PERCENT or BASIS_POINTS must be compared in DECIMAL_FRACTION."""

    def test_percent_per_annum_scales_by_hundred(self) -> None:
        assert to_decimal_fraction(Decimal("3.75"), Unit.PERCENT_PER_ANNUM) == Decimal("0.0375")

    def test_basis_points_scales_by_ten_thousand(self) -> None:
        assert to_decimal_fraction(Decimal("375"), Unit.BASIS_POINTS) == Decimal("0.0375")

    def test_decimal_fraction_is_passthrough(self) -> None:
        assert to_decimal_fraction(Decimal("0.0375"), Unit.DECIMAL_FRACTION) == Decimal("0.0375")

    def test_percent_within_band(self) -> None:
        # 3.75% = 0.0375, inside every EURIBOR/ESTR band.
        assert (
            check_sanity_band(Metric.EURIBOR_3M, Decimal("3.75"), Unit.PERCENT_PER_ANNUM)
            is True
        )

    def test_percent_outside_band(self) -> None:
        # 25% = 0.25, above the 0.20 cap.
        assert (
            check_sanity_band(Metric.EURIBOR_3M, Decimal("25"), Unit.PERCENT_PER_ANNUM)
            is False
        )

    def test_basis_points_within_band(self) -> None:
        # 375 bps = 0.0375, inside every band.
        assert (
            check_sanity_band(Metric.EURIBOR_12M, Decimal("375"), Unit.BASIS_POINTS)
            is True
        )


class TestNegativeRatesRespectFloor:
    """ESTR has been negative before — the floor is -2%."""

    def test_minus_one_percent_within_estr_band(self) -> None:
        assert (
            check_sanity_band(Metric.ESTR, Decimal("-0.01"), Unit.DECIMAL_FRACTION)
            is True
        )

    def test_minus_five_percent_outside_estr_band(self) -> None:
        assert (
            check_sanity_band(Metric.ESTR, Decimal("-0.05"), Unit.DECIMAL_FRACTION)
            is False
        )
