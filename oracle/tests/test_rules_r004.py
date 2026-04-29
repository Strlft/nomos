"""R-004 — Illegality (Rate Unavailability): every scenario in the matrix.

R-004 is the only V1 rule that consults ``MarketState.missing`` instead
of ``MarketState.latest``. The tests below pin both branches of the
ladder (transient vs sustained), the "rate returns" recovery path, and
the indeterminate outcomes for missing / untracked reference rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from oracle.config import Metric, Severity, Unit
from oracle.rules.impl.r004_illegality import (
    SUSTAINED_THRESHOLD_DAYS,
    rule as r004_rule,
)
from oracle.types import MarketState, NormalizedDatapoint


# ---------------------------------------------------------------------------
# Duck-typed contract objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FloatingLeg:
    reference_rate: str


@dataclass(frozen=True)
class Contract:
    contract_id: str = "C-0004"
    floating_leg: FloatingLeg = FloatingLeg(reference_rate="EURIBOR 3M")


# ---------------------------------------------------------------------------
# MarketState helpers
# ---------------------------------------------------------------------------


def _datapoint(metric: Metric) -> NormalizedDatapoint:
    return NormalizedDatapoint(
        source_id="fake_v1",
        metric=metric,
        value=Decimal("0.0375"),
        unit=Unit.DECIMAL_FRACTION,
        as_of=date(2026, 4, 23),
        fetched_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        source_hash="deadbeef",
        source_url="file://fake",
        sanity_band_passed=True,
        cross_validated=False,
    )


def _market(
    *,
    latest: dict[Metric, NormalizedDatapoint] | None = None,
    missing: frozenset[Metric] = frozenset(),
    missing_days: dict[Metric, int] | None = None,
) -> MarketState:
    return MarketState(
        built_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        latest=latest or {},
        attestation_refs={},
        missing=missing,
        missing_consecutive_days=missing_days or {},
    )


# ---------------------------------------------------------------------------
# Reference rate present → no trigger
# ---------------------------------------------------------------------------


class TestScenario01_RatePresent:
    def test_rate_in_latest_does_not_fire(self) -> None:
        market = _market(latest={Metric.EURIBOR_3M: _datapoint(Metric.EURIBOR_3M)})
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False

    def test_rate_present_even_with_other_metrics_missing(self) -> None:
        # Only the contract's reference rate matters — other missing rates
        # must not fire R-004.
        market = _market(
            latest={Metric.EURIBOR_3M: _datapoint(Metric.EURIBOR_3M)},
            missing=frozenset({Metric.ESTR}),
            missing_days={Metric.ESTR: 30},
        )
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# Rate missing 1 day → WARNING
# ---------------------------------------------------------------------------


class TestScenario02_TransientUnavailability:
    def test_one_day_missing_is_warning(self) -> None:
        market = _market(
            missing=frozenset({Metric.EURIBOR_3M}),
            missing_days={Metric.EURIBOR_3M: 1},
        )
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert any(
            "missing_consecutive_days=1" in e.value
            and "regime=transient_unavailability" in e.value
            for e in outcome.evidence
        )

    @pytest.mark.parametrize("days", [1, 2, 3, 4])
    def test_below_threshold_stays_warning(self, days: int) -> None:
        market = _market(
            missing=frozenset({Metric.EURIBOR_3M}),
            missing_days={Metric.EURIBOR_3M: days},
        )
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.severity is Severity.WARNING

    def test_missing_days_default_zero_still_fires_warning(self) -> None:
        # Defensive: rate is in `missing` but no entry in
        # `missing_consecutive_days`. Treat as 0 → still WARNING (rate is
        # currently unavailable today, even if no streak yet).
        market = _market(missing=frozenset({Metric.EURIBOR_3M}), missing_days={})
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING


# ---------------------------------------------------------------------------
# Rate missing ≥ 5 consecutive business days → POTENTIAL_TRIGGER
# ---------------------------------------------------------------------------


class TestScenario03_SustainedUnavailability:
    def test_threshold_day_is_potential_trigger(self) -> None:
        market = _market(
            missing=frozenset({Metric.EURIBOR_3M}),
            missing_days={Metric.EURIBOR_3M: SUSTAINED_THRESHOLD_DAYS},
        )
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        assert any(
            "regime=sustained_unavailability" in e.value
            for e in outcome.evidence
        )

    @pytest.mark.parametrize("days", [5, 6, 10, 30])
    def test_at_or_above_threshold_is_potential_trigger(self, days: int) -> None:
        market = _market(
            missing=frozenset({Metric.EURIBOR_3M}),
            missing_days={Metric.EURIBOR_3M: days},
        )
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.severity is Severity.POTENTIAL_TRIGGER

    def test_never_auto_triggers_even_at_extreme_streaks(self) -> None:
        market = _market(
            missing=frozenset({Metric.EURIBOR_3M}),
            missing_days={Metric.EURIBOR_3M: 365},
        )
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.severity is Severity.POTENTIAL_TRIGGER, (
            "R-004 must NEVER auto-TRIGGER per ORACLE_RULES.md §R-004"
        )
        assert outcome.severity is not Severity.TRIGGER


# ---------------------------------------------------------------------------
# Rate missing 10 days, then returns → no trigger
# ---------------------------------------------------------------------------


class TestScenario04_RecoveryAfterOutage:
    def test_rate_returns_to_latest_clears_the_event(self) -> None:
        # Rate is back in `latest` and removed from `missing` — no event,
        # regardless of how long the prior outage was.
        market = _market(
            latest={Metric.EURIBOR_3M: _datapoint(Metric.EURIBOR_3M)},
            missing=frozenset(),
            missing_days={},
        )
        outcome = r004_rule.predicate(market, Contract(), date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# Untracked rate → indeterminate (not "no trigger")
# ---------------------------------------------------------------------------


class TestScenario05_UntrackedRate:
    @pytest.mark.parametrize(
        "rate", ["SOFR", "TONAR", "BOK 3M", "Custom Internal Index"]
    )
    def test_untracked_rate_is_indeterminate(self, rate: str) -> None:
        contract = Contract(floating_leg=FloatingLeg(reference_rate=rate))
        market = _market(missing=frozenset(), missing_days={})
        outcome = r004_rule.predicate(market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert "does not map to a tracked Metric" in (
            outcome.indeterminate_reason or ""
        )


# ---------------------------------------------------------------------------
# String normalisation accepts ISO and human-readable spellings
# ---------------------------------------------------------------------------


class TestRateStringNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("EURIBOR 3M", Metric.EURIBOR_3M),
            ("euribor 3m", Metric.EURIBOR_3M),
            ("EURIBOR_3M", Metric.EURIBOR_3M),
            ("EURIBOR-3M", Metric.EURIBOR_3M),
            ("  ESTR  ", Metric.ESTR),
            ("EURIBOR 6M", Metric.EURIBOR_6M),
            ("EURIBOR 12M", Metric.EURIBOR_12M),
        ],
    )
    def test_recognised_spellings(self, raw: str, expected: Metric) -> None:
        contract = Contract(floating_leg=FloatingLeg(reference_rate=raw))
        market = _market(
            missing=frozenset({expected}),
            missing_days={expected: 7},
        )
        outcome = r004_rule.predicate(market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        assert any(f"normalised={expected.value}" in e.value for e in outcome.evidence)


# ---------------------------------------------------------------------------
# Missing reference_rate field → indeterminate (distinct from untracked)
# ---------------------------------------------------------------------------


class TestIndeterminateNoField:
    def test_no_floating_leg_field_at_all(self) -> None:
        class Bare:
            contract_id = "C-bare"

        market = _market(missing=frozenset({Metric.EURIBOR_3M}), missing_days={Metric.EURIBOR_3M: 5})
        outcome = r004_rule.predicate(market, Bare(), date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert "floating_leg.reference_rate" in (outcome.indeterminate_reason or "")

    def test_falls_back_to_floating_index_attribute(self) -> None:
        # SwapParameters exposes `floating_index` (a flat string), not
        # `floating_leg.reference_rate`. R-004 should accept either.
        @dataclass(frozen=True)
        class FlatContract:
            contract_id: str = "C-flat"
            floating_index: str = "EURIBOR 3M"

        market = _market(
            missing=frozenset({Metric.EURIBOR_3M}),
            missing_days={Metric.EURIBOR_3M: 7},
        )
        outcome = r004_rule.predicate(market, FlatContract(), date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER

    def test_non_string_reference_rate_is_indeterminate(self) -> None:
        contract = Contract(floating_leg=FloatingLeg(reference_rate=42))  # type: ignore[arg-type]
        market = _market(missing=frozenset({Metric.EURIBOR_3M}), missing_days={Metric.EURIBOR_3M: 5})
        outcome = r004_rule.predicate(market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is True
