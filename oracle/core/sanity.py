"""Sanity-band check for normalized market values.

A sanity band is a wide plausibility window — wide enough to let real market
moves through, narrow enough to catch parsing disasters (wrong scale, wrong
currency, empty string becoming 0.0). Bands live in
:data:`oracle.config.SANITY_BANDS`, always expressed in
:attr:`~oracle.config.Unit.DECIMAL_FRACTION`.
"""

from __future__ import annotations

from decimal import Decimal

from oracle.config import Metric, SANITY_BANDS, Unit


# Conversion factors: multiply a value in ``unit`` by the factor to get
# the equivalent value in DECIMAL_FRACTION.
_TO_DECIMAL_FRACTION: dict[Unit, Decimal] = {
    Unit.DECIMAL_FRACTION: Decimal("1"),
    Unit.PERCENT_PER_ANNUM: Decimal("0.01"),
    Unit.BASIS_POINTS: Decimal("0.0001"),
}


def to_decimal_fraction(value: Decimal, unit: Unit) -> Decimal:
    """Convert ``value`` expressed in ``unit`` to DECIMAL_FRACTION."""

    return value * _TO_DECIMAL_FRACTION[unit]


def check_sanity_band(metric: Metric, value: Decimal, unit: Unit) -> bool:
    """Return True iff ``value`` lies within the band configured for ``metric``.

    The band is inclusive at both ends. Raises :class:`KeyError` if the metric
    has no configured band — this is a programmer error, not user input, so
    failing loudly is the right behavior.
    """

    band = SANITY_BANDS[metric]
    normalized = to_decimal_fraction(value, unit)
    return band.min_value <= normalized <= band.max_value
