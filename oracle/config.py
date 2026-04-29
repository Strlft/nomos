"""Oracle controlled vocabulary, source/metric mapping, and validation bands.

This module is the single source of truth for:

- The :class:`Metric`, :class:`Unit`, and :class:`Severity` enums.
- Which source can publish which metric (:data:`SOURCE_METRICS`).
- Which source is primary per metric (:data:`PRIMARY_SOURCE`).
- Sanity bands per metric (:data:`SANITY_BANDS`).

All numeric values are :class:`~decimal.Decimal`. No ``float`` ever.

Single-source-per-metric (Phase 7 revision)
-------------------------------------------

The original architecture pinned a primary + secondary source per metric so
the Oracle could cross-validate every published rate. As of Phase 7b we
collapsed to a single source per metric because no free public daily
EURIBOR feed exists:

* Banque de France stopped publishing EURIBOR on 2024-07-10.
* FRED's daily EURIBOR series (``EUR3MTD156N`` etc.) was discontinued by
  IBA on 2022-01-31.
* ECB SDW publishes EURIBOR at *monthly* frequency only, in the FM
  (Financial Market) dataflow.

The Oracle now reads ESTR (daily) and EURIBOR-3M/6M/12M (monthly) all from
ECB SDW. ``SECONDARY_SOURCE`` and ``CROSS_VALIDATION_TOLERANCE`` were
deleted along with the BdF and FRED source IDs — they had no remaining
referent.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import NamedTuple


class Metric(str, Enum):
    """Canonical metric identifiers consumed by the Oracle Core and Rules."""

    # Overnight rates
    ESTR = "ESTR"
    # EURIBOR tenors (monthly fixings from ECB FM dataflow)
    EURIBOR_3M = "EURIBOR_3M"
    EURIBOR_6M = "EURIBOR_6M"
    EURIBOR_12M = "EURIBOR_12M"


class Unit(str, Enum):
    """Units a :class:`NormalizedDatapoint` may carry."""

    PERCENT_PER_ANNUM = "percent_per_annum"
    DECIMAL_FRACTION = "decimal_fraction"
    BASIS_POINTS = "basis_points"


class Severity(str, Enum):
    """Escalation levels a rule may emit."""

    WARNING = "warning"
    POTENTIAL_TRIGGER = "potential_trigger"
    TRIGGER = "trigger"


# ---------------------------------------------------------------------------
# Source identifiers
# ---------------------------------------------------------------------------

SOURCE_ID_ECB: str = "ecb_sdw_v1"
SOURCE_ID_FAKE: str = "fake_v1"


# ---------------------------------------------------------------------------
# Source → metric mapping (ARCH §4.1)
# ---------------------------------------------------------------------------

SOURCE_METRICS: dict[str, frozenset[Metric]] = {
    SOURCE_ID_ECB: frozenset(
        {
            Metric.ESTR,
            Metric.EURIBOR_3M,
            Metric.EURIBOR_6M,
            Metric.EURIBOR_12M,
        }
    ),
    SOURCE_ID_FAKE: frozenset(Metric),
}


# ---------------------------------------------------------------------------
# Primary source mapping (ARCH §4.2)
# ---------------------------------------------------------------------------

PRIMARY_SOURCE: dict[Metric, str] = {
    Metric.ESTR: SOURCE_ID_ECB,
    Metric.EURIBOR_3M: SOURCE_ID_ECB,
    Metric.EURIBOR_6M: SOURCE_ID_ECB,
    Metric.EURIBOR_12M: SOURCE_ID_ECB,
}


# ---------------------------------------------------------------------------
# Sanity bands (ARCH §4.3)
# ---------------------------------------------------------------------------


class SanityBand(NamedTuple):
    """Inclusive min/max plausible values for a metric, in DECIMAL_FRACTION."""

    min_value: Decimal
    max_value: Decimal


SANITY_BANDS: dict[Metric, SanityBand] = {
    Metric.ESTR: SanityBand(Decimal("-0.02"), Decimal("0.15")),
    Metric.EURIBOR_3M: SanityBand(Decimal("-0.02"), Decimal("0.20")),
    Metric.EURIBOR_6M: SanityBand(Decimal("-0.02"), Decimal("0.20")),
    Metric.EURIBOR_12M: SanityBand(Decimal("-0.02"), Decimal("0.20")),
}


# ---------------------------------------------------------------------------
# Versioning (ARCH §11)
# ---------------------------------------------------------------------------

ORACLE_VERSION: str = "0.1.0"
RULES_VERSION: str = "1.0.0"
