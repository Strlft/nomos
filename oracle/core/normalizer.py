"""RawDatapoint → NormalizedDatapoint.

The normalizer is intentionally thin. Each real collector decides what the
wire format looks like; its obligation is to repackage the response as a
canonical mini-JSON inside :attr:`RawDatapoint.raw_payload`:

.. code-block:: json

    {"value": "0.0375", "as_of": "2026-04-23"}

The normalizer then:

1. Confirms the :attr:`RawDatapoint.metric` string matches ``expected_metric``.
2. Parses the JSON and coerces ``value`` to :class:`~decimal.Decimal` and
   ``as_of`` to :class:`~datetime.date`.
3. Applies the sanity band and surfaces the result on
   :attr:`NormalizedDatapoint.sanity_band_passed`.
4. Raises :class:`SanityBandViolation` when the value falls outside the band —
   per Invariant I5, we refuse to publish silently-bad values.

Cross-validation is not the normalizer's concern. The :mod:`cross_validator`
(Phase 3) sets ``cross_validated`` / ``cross_checked_against``.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from oracle.config import Metric, Unit
from oracle.core.sanity import check_sanity_band
from oracle.errors import CollectorDataError, SanityBandViolation
from oracle.types import NormalizedDatapoint, RawDatapoint


def normalize(
    raw: RawDatapoint,
    expected_metric: Metric,
    expected_unit: Unit,
) -> NormalizedDatapoint:
    """Parse, type-coerce, and sanity-check a :class:`RawDatapoint`.

    Raises
    ------
    CollectorDataError
        If the raw payload is unparseable or the metric label disagrees.
    SanityBandViolation
        If the parsed value falls outside the configured sanity band.
    """

    if raw.metric != expected_metric.value:
        raise CollectorDataError(
            f"RawDatapoint.metric={raw.metric!r} does not match expected "
            f"{expected_metric.value!r}"
        )

    try:
        parsed = json.loads(raw.raw_payload)
    except json.JSONDecodeError as exc:
        raise CollectorDataError(f"raw_payload is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise CollectorDataError("raw_payload must decode to a JSON object")

    for required in ("value", "as_of"):
        if required not in parsed:
            raise CollectorDataError(f"raw_payload missing required key {required!r}")

    try:
        value = Decimal(str(parsed["value"]))
    except InvalidOperation as exc:
        raise CollectorDataError(f"value={parsed['value']!r} is not a valid Decimal") from exc

    try:
        as_of = date.fromisoformat(parsed["as_of"])
    except (TypeError, ValueError) as exc:
        raise CollectorDataError(
            f"as_of={parsed['as_of']!r} is not an ISO-8601 date"
        ) from exc

    if not check_sanity_band(expected_metric, value, expected_unit):
        raise SanityBandViolation(
            f"{expected_metric.value}={value} {expected_unit.value} "
            f"is outside the configured sanity band"
        )

    return NormalizedDatapoint(
        source_id=raw.source_id,
        metric=expected_metric,
        value=value,
        unit=expected_unit,
        as_of=as_of,
        fetched_at=raw.fetched_at,
        source_hash=raw.source_hash,
        source_url=raw.source_url,
        sanity_band_passed=True,
        cross_validated=False,
        cross_checked_against=None,
    )
