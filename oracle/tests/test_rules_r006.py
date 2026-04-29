"""R-006 — Material Adverse Change: every scenario in the spec's matrix.

The rule monitors three structured indicators per in-scope party. The
hard rule pinned across the suite is that severity caps at
``POTENTIAL_TRIGGER`` regardless of how many indicators fire — MAC
characterisation requires human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from oracle.config import Severity
from oracle.rules.impl.r006_material_adverse_change import (
    EXTERNAL_DEFAULT_WINDOW_DAYS,
    RATING_DOWNGRADE_NOTCHES,
    rule as r006_rule,
)
from oracle.types import MarketState


# ---------------------------------------------------------------------------
# Duck-typed contract objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RatingAction:
    agency: str
    old_rating: str
    new_rating: str
    effective_date: date
    source_reference: str | None = None


@dataclass(frozen=True)
class ExternalDefault:
    default_id: str
    instrument_type: str
    status: str
    amount_due: Decimal
    currency: str
    reported_at: date
    source_reference: str | None = None


@dataclass(frozen=True)
class SanctionsDesignation:
    list_name: str
    entity_id: str
    effective_date: date
    delisted_date: date | None = None
    source_reference: str | None = None


@dataclass(frozen=True)
class Schedule:
    mac_applies: dict[str, bool] = field(default_factory=dict)
    credit_rating_baseline: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Contract:
    contract_id: str = "C-0006"
    schedule: Schedule = field(default_factory=Schedule)
    credit_rating_actions: dict[str, tuple[RatingAction, ...]] = field(
        default_factory=dict
    )
    external_defaults: dict[str, tuple[ExternalDefault, ...]] = field(
        default_factory=dict
    )
    sanctions_designations: dict[str, tuple[SanctionsDesignation, ...]] = field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


_AS_OF = date(2026, 4, 23)


@pytest.fixture
def empty_market() -> MarketState:
    return MarketState(
        built_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        latest={},
        attestation_refs={},
        missing=frozenset(),
        missing_consecutive_days={},
    )


def _bilateral_schedule(
    *,
    mac_a: bool = True,
    mac_b: bool = True,
    baseline_a: str = "A",
    baseline_b: str = "A",
) -> Schedule:
    return Schedule(
        mac_applies={"party_a": mac_a, "party_b": mac_b},
        credit_rating_baseline={"party_a": baseline_a, "party_b": baseline_b},
    )


def _has_indicator(outcome, party: str, indicator_set_substr: str) -> bool:
    """True if any evidence row contains the per-party indicator-set summary."""

    needle = f"mac[{party}].indicator_set"
    return any(
        e.key == needle and indicator_set_substr in e.value
        for e in outcome.evidence
    )


# ---------------------------------------------------------------------------
# 1. MAC not applicable to either party → no trigger
# ---------------------------------------------------------------------------


class TestScenario01_NotApplicable:
    def test_neither_party_in_scope(self, empty_market: MarketState) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(mac_a=False, mac_b=False),
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False
        assert outcome.indeterminate is False

    def test_empty_mac_applies_map(self, empty_market: MarketState) -> None:
        contract = Contract(schedule=Schedule(mac_applies={}))
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# 2. Applicable but zero indicators → no trigger
# ---------------------------------------------------------------------------


class TestScenario02_ZeroIndicators:
    def test_in_scope_but_no_signals(self, empty_market: MarketState) -> None:
        contract = Contract(schedule=_bilateral_schedule())
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# 3. Single indicator: rating downgrade -2 notches → WARNING
# ---------------------------------------------------------------------------


class TestScenario03_RatingDowngrade2:
    def test_two_notch_downgrade_is_warning(
        self, empty_market: MarketState
    ) -> None:
        # Baseline A (index 5) → A- (index 6) is 1 notch; A → BBB+ (index 7)
        # is 2 notches. Use 2 to land on the threshold exactly.
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="BBB+",
                        effective_date=date(2026, 2, 1),
                        source_reference="SP-2026-02-01",
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert _has_indicator(outcome, "party_a", "rating_downgrade")
        assert _has_indicator(outcome, "party_a", "count=1")
        assert any(
            "notches=2" in e.value and "agency=S&P" in e.value
            for e in outcome.evidence
        )


# ---------------------------------------------------------------------------
# 4. Single indicator: rating downgrade -1 notch → no trigger
# ---------------------------------------------------------------------------


class TestScenario04_RatingDowngrade1:
    def test_one_notch_downgrade_is_silent(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="A-",
                        effective_date=date(2026, 2, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False
        assert outcome.indeterminate is False

    def test_upgrade_does_not_fire(self, empty_market: MarketState) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="BBB"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="BBB",
                        new_rating="A",
                        effective_date=date(2026, 2, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 5. Single indicator: payment default in window → WARNING
# ---------------------------------------------------------------------------


class TestScenario05_PaymentDefaultInWindow:
    def test_default_30_days_ago_is_warning(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D-1",
                        instrument_type="bond",
                        status="payment_default",
                        amount_due=Decimal("5000000"),
                        currency="EUR",
                        reported_at=date(2026, 3, 24),  # 30d before as_of
                        source_reference="EBA-DB",
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert _has_indicator(outcome, "party_a", "external_payment_default")
        assert any(
            "status=payment_default" in e.value
            and f"window_days={EXTERNAL_DEFAULT_WINDOW_DAYS}" in e.value
            for e in outcome.evidence
        )

    def test_default_status_other_than_payment_default_does_not_fire(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D-ACC",
                        instrument_type="bond",
                        status="accelerated",
                        amount_due=Decimal("9000000"),
                        currency="EUR",
                        reported_at=date(2026, 3, 24),
                    ),
                    ExternalDefault(
                        default_id="D-REM",
                        instrument_type="bond",
                        status="remediated",
                        amount_due=Decimal("9000000"),
                        currency="EUR",
                        reported_at=date(2026, 3, 24),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 6. Single indicator: payment default outside window → no trigger
# ---------------------------------------------------------------------------


class TestScenario06_PaymentDefaultOutsideWindow:
    def test_default_91_days_ago_does_not_fire(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D-OLD",
                        instrument_type="bond",
                        status="payment_default",
                        amount_due=Decimal("5000000"),
                        currency="EUR",
                        reported_at=_AS_OF
                        - __import__("datetime").timedelta(days=91),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False

    def test_default_in_future_does_not_fire(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D-FUT",
                        instrument_type="bond",
                        status="payment_default",
                        amount_due=Decimal("5000000"),
                        currency="EUR",
                        reported_at=date(2026, 5, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False

    def test_default_exactly_90_days_ago_qualifies(
        self, empty_market: MarketState
    ) -> None:
        # Boundary: ``window_start = as_of - 90 days``; reported_at ==
        # window_start should still count.
        from datetime import timedelta

        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D-EDGE",
                        instrument_type="bond",
                        status="payment_default",
                        amount_due=Decimal("1"),
                        currency="EUR",
                        reported_at=_AS_OF - timedelta(days=90),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING


# ---------------------------------------------------------------------------
# 7. Single indicator: active sanctions designation → WARNING
# ---------------------------------------------------------------------------


class TestScenario07_ActiveSanctions:
    def test_active_designation_is_warning(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="OFAC SDN",
                        entity_id="ENT-42",
                        effective_date=date(2026, 1, 5),
                        delisted_date=None,
                        source_reference="OFAC-2026-01-05",
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert _has_indicator(outcome, "party_a", "sanctions_designation")
        assert any(
            "list_name=OFAC SDN" in e.value for e in outcome.evidence
        )

    def test_future_effective_date_does_not_fire(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="EU consolidated",
                        entity_id="ENT-99",
                        effective_date=date(2027, 6, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 8. Two indicators: downgrade + sanctions → POTENTIAL_TRIGGER
# ---------------------------------------------------------------------------


class TestScenario08_TwoIndicators:
    def test_downgrade_plus_sanctions_is_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="Moody's",
                        old_rating="A2",
                        new_rating="Baa1",
                        effective_date=date(2026, 3, 1),
                    ),
                ),
            },
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="UK consolidated",
                        entity_id="ENT-7",
                        effective_date=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        assert _has_indicator(outcome, "party_a", "count=2")
        assert _has_indicator(outcome, "party_a", "rating_downgrade")
        assert _has_indicator(outcome, "party_a", "sanctions_designation")


# ---------------------------------------------------------------------------
# 9. Three indicators simultaneously → POTENTIAL_TRIGGER (NEVER auto-TRIGGER)
# ---------------------------------------------------------------------------


class TestScenario09_ThreeIndicators:
    def test_all_three_indicators_caps_at_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="AA"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="AA",
                        new_rating="BBB",  # 6 notches down
                        effective_date=date(2026, 2, 14),
                    ),
                ),
            },
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D-1",
                        instrument_type="bond",
                        status="payment_default",
                        amount_due=Decimal("100000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="OFAC SDN",
                        entity_id="ENT-42",
                        effective_date=date(2026, 3, 15),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER, (
            "R-006 must NEVER auto-TRIGGER per ORACLE_RULES.md §R-006"
        )
        assert outcome.severity is not Severity.TRIGGER
        assert _has_indicator(outcome, "party_a", "count=3")
        # All three indicator names appear in the indicator-set summary.
        for name in (
            "rating_downgrade",
            "external_payment_default",
            "sanctions_designation",
        ):
            assert _has_indicator(outcome, "party_a", name)


# ---------------------------------------------------------------------------
# 10. Sanctions delisted before as_of → indicator not triggered
# ---------------------------------------------------------------------------


class TestScenario10_SanctionsDelisted:
    def test_delisted_designation_is_not_active(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="OFAC SDN",
                        entity_id="ENT-42",
                        effective_date=date(2025, 6, 1),
                        delisted_date=date(2026, 2, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False

    def test_delisting_today_means_not_active(
        self, empty_market: MarketState
    ) -> None:
        # Boundary: delisted_date <= as_of → not active.
        contract = Contract(
            schedule=_bilateral_schedule(),
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="OFAC SDN",
                        entity_id="ENT-X",
                        effective_date=date(2025, 6, 1),
                        delisted_date=_AS_OF,
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False

    def test_delisting_tomorrow_still_active(
        self, empty_market: MarketState
    ) -> None:
        from datetime import timedelta

        contract = Contract(
            schedule=_bilateral_schedule(),
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="OFAC SDN",
                        entity_id="ENT-Y",
                        effective_date=date(2025, 6, 1),
                        delisted_date=_AS_OF + timedelta(days=1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING


# ---------------------------------------------------------------------------
# 11. Rating downgrade -3 notches alone → still WARNING
# ---------------------------------------------------------------------------


class TestScenario11_DeepDowngradeAlone:
    def test_three_notch_downgrade_is_still_one_indicator(
        self, empty_market: MarketState
    ) -> None:
        # A → BBB is 3 notches; with no other indicator, severity stays
        # WARNING because that's still a single indicator.
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="BBB",
                        effective_date=date(2026, 1, 10),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert _has_indicator(outcome, "party_a", "count=1")
        assert any("notches=3" in e.value for e in outcome.evidence)


# ---------------------------------------------------------------------------
# 12. Missing baseline rating → indeterminate
# ---------------------------------------------------------------------------


class TestScenario12_MissingBaseline:
    def test_party_in_scope_without_baseline_is_indeterminate(
        self, empty_market: MarketState
    ) -> None:
        # party_a is in scope and has a rating action, but no baseline.
        schedule = Schedule(
            mac_applies={"party_a": True},
            credit_rating_baseline={},  # no baseline for party_a
        )
        contract = Contract(
            schedule=schedule,
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="BBB",
                        effective_date=date(2026, 2, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert "credit_rating_baseline" in (
            outcome.indeterminate_reason or ""
        )

    def test_unparseable_baseline_is_indeterminate(
        self, empty_market: MarketState
    ) -> None:
        schedule = Schedule(
            mac_applies={"party_a": True},
            credit_rating_baseline={"party_a": "ZZZ-not-a-rating"},
        )
        contract = Contract(schedule=schedule)
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False
        assert outcome.indeterminate is True


# ---------------------------------------------------------------------------
# Cross-party aggregation
# ---------------------------------------------------------------------------


class TestCrossPartyAggregation:
    def test_warning_one_party_potential_trigger_other(
        self, empty_market: MarketState
    ) -> None:
        # party_a: 1 indicator (sanctions) → WARNING
        # party_b: 2 indicators (downgrade + payment default) → POTENTIAL_TRIGGER
        # Aggregate: POTENTIAL_TRIGGER (max).
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A", baseline_b="A"),
            credit_rating_actions={
                "party_b": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="BBB+",
                        effective_date=date(2026, 2, 1),
                    ),
                ),
            },
            external_defaults={
                "party_b": (
                    ExternalDefault(
                        default_id="D-B",
                        instrument_type="bond",
                        status="payment_default",
                        amount_due=Decimal("1000000"),
                        currency="EUR",
                        reported_at=date(2026, 3, 1),
                    ),
                ),
            },
            sanctions_designations={
                "party_a": (
                    SanctionsDesignation(
                        list_name="OFAC SDN",
                        entity_id="ENT-A",
                        effective_date=date(2026, 1, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        assert _has_indicator(outcome, "party_a", "count=1")
        assert _has_indicator(outcome, "party_b", "count=2")

    def test_only_party_in_scope_is_evaluated(
        self, empty_market: MarketState
    ) -> None:
        # party_b has all three indicators; mac_applies is False for B,
        # so they are silent. Result should be no trigger.
        contract = Contract(
            schedule=Schedule(
                mac_applies={"party_a": True, "party_b": False},
                credit_rating_baseline={"party_a": "A", "party_b": "A"},
            ),
            credit_rating_actions={
                "party_b": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="CCC",
                        effective_date=date(2026, 2, 1),
                    ),
                ),
            },
            external_defaults={
                "party_b": (
                    ExternalDefault(
                        default_id="D-B",
                        instrument_type="bond",
                        status="payment_default",
                        amount_due=Decimal("9999999"),
                        currency="EUR",
                        reported_at=date(2026, 3, 1),
                    ),
                ),
            },
            sanctions_designations={
                "party_b": (
                    SanctionsDesignation(
                        list_name="OFAC SDN",
                        entity_id="ENT-B",
                        effective_date=date(2026, 1, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# Latest-action selection: only the most recent action drives notches
# ---------------------------------------------------------------------------


class TestRatingActionSelection:
    def test_latest_action_supersedes_older(
        self, empty_market: MarketState
    ) -> None:
        # An older 3-notch downgrade is overridden by a newer 1-notch
        # action; the rule must use only the latest, so no indicator fires.
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="BBB-",
                        effective_date=date(2025, 6, 1),
                    ),
                    RatingAction(
                        agency="S&P",
                        old_rating="BBB-",
                        new_rating="A-",
                        effective_date=date(2026, 3, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False

    def test_future_actions_ignored(self, empty_market: MarketState) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="CCC",
                        effective_date=date(2027, 1, 1),
                    ),
                ),
            },
        )
        outcome = r006_rule.predicate(empty_market, contract, _AS_OF)
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# Defensive: malformed action / contract
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_unparseable_new_rating_raises(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(baseline_a="A"),
            credit_rating_actions={
                "party_a": (
                    RatingAction(
                        agency="S&P",
                        old_rating="A",
                        new_rating="not-a-rating",
                        effective_date=date(2026, 2, 1),
                    ),
                ),
            },
        )
        with pytest.raises(ValueError, match="unparseable new_rating"):
            r006_rule.predicate(empty_market, contract, _AS_OF)

    def test_no_schedule_is_indeterminate(
        self, empty_market: MarketState
    ) -> None:
        class Bare:
            contract_id = "C-bare"

        outcome = r006_rule.predicate(empty_market, Bare(), _AS_OF)
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert "schedule" in (outcome.indeterminate_reason or "")

    def test_mac_applies_must_be_a_mapping(
        self, empty_market: MarketState
    ) -> None:
        @dataclass(frozen=True)
        class BadSchedule:
            mac_applies: tuple = ()  # type: ignore[type-arg]
            credit_rating_baseline: dict = field(default_factory=dict)

        contract = Contract(schedule=BadSchedule())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="mac_applies must be a mapping"):
            r006_rule.predicate(empty_market, contract, _AS_OF)

    def test_threshold_constants_exposed(self) -> None:
        # Document the heuristics so reviewers see them in test code too.
        assert RATING_DOWNGRADE_NOTCHES == 2
        assert EXTERNAL_DEFAULT_WINDOW_DAYS == 90
