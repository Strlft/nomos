"""R-001 — Failure to Pay: the eight scenarios pinned by ORACLE_RULES.md.

Each test mirrors one row of the rule's test matrix. Dates are chosen so
that the TARGET2 holiday case (scenario 7) straddles Good Friday 2025-04-18
and Easter Monday 2025-04-21 — if the calendar ever regresses back to
"weekends only", that test flips to TRIGGER and fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from oracle.config import Severity
from oracle.rules.impl.r001_failure_to_pay import rule as r001_rule
from oracle.types import MarketState


# ---------------------------------------------------------------------------
# Duck-typed contract objects (the Oracle must not import irs_engine_v2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Payment:
    payment_id: str
    amount: Decimal
    due_date: date
    status: str  # "PAID" | "PENDING"


@dataclass(frozen=True)
class Notice:
    kind: str
    payment_id: str
    sent_at: date


@dataclass(frozen=True)
class Schedule:
    grace_period_failure_to_pay: int | None = None


@dataclass(frozen=True)
class Contract:
    contract_id: str = "C-0001"
    scheduled_payments: tuple[Payment, ...] = ()
    notices: tuple[Notice, ...] = ()
    schedule: Schedule | None = field(default_factory=Schedule)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_market() -> MarketState:
    """R-001 needs no market data; build an empty but valid MarketState."""

    return MarketState(
        built_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        latest={},
        attestation_refs={},
        missing=frozenset(),
        missing_consecutive_days={},
    )


def _assert_evidence_cites(outcome, payment_id: str) -> None:
    assert any(payment_id in e.key for e in outcome.evidence), (
        f"expected evidence to cite payment {payment_id}; "
        f"got {[e.key for e in outcome.evidence]}"
    )


# ---------------------------------------------------------------------------
# 1. No payment due → no trigger
# ---------------------------------------------------------------------------


class TestScenario01_NoPaymentDue:
    def test_future_due_date_does_not_fire(self, empty_market: MarketState) -> None:
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 5, 1), "PENDING"),
            ),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False

    def test_no_payments_at_all_does_not_fire(self, empty_market: MarketState) -> None:
        contract = Contract()  # empty tuple of payments
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# 2. Payment paid on time → no trigger
# ---------------------------------------------------------------------------


class TestScenario02_PaidOnTime:
    def test_paid_payment_is_ignored(self, empty_market: MarketState) -> None:
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 22), "PAID"),
            ),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 3. Overdue, no notice → WARNING
# ---------------------------------------------------------------------------


class TestScenario03_OverdueNoNotice:
    def test_emits_warning_with_payment_evidence(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 22), "PENDING"),
            ),
            notices=(),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        _assert_evidence_cites(outcome, "P1")
        assert any("overdue_no_notice" in e.value for e in outcome.evidence)


# ---------------------------------------------------------------------------
# 4. Overdue + notice, inside grace → WARNING
# ---------------------------------------------------------------------------


class TestScenario04_InsideGrace:
    def test_asof_equals_grace_end_is_still_warning(
        self, empty_market: MarketState
    ) -> None:
        # Notice Wed 2026-04-22 → grace_end = Thu 2026-04-23 (1 biz day).
        # as_of Thu 2026-04-23 is the last day of grace → WARNING.
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 21), "PENDING"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2026, 4, 22)),),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert any("grace_end=2026-04-23" in e.value for e in outcome.evidence)


# ---------------------------------------------------------------------------
# 5. Overdue + notice, grace elapsed, unpaid → TRIGGER
# ---------------------------------------------------------------------------


class TestScenario05_GraceElapsedUnpaid:
    def test_day_after_grace_is_trigger(self, empty_market: MarketState) -> None:
        # grace_end Thu 2026-04-23, as_of Fri 2026-04-24 → elapsed.
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 21), "PENDING"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2026, 4, 22)),),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 24))
        assert outcome.fired is True
        assert outcome.severity is Severity.TRIGGER
        assert any("overdue_grace_elapsed" in e.value for e in outcome.evidence)


# ---------------------------------------------------------------------------
# 6. Overdue + notice, grace elapsed, paid during grace → no trigger
# ---------------------------------------------------------------------------


class TestScenario06_PaidDuringGrace:
    def test_paid_payment_does_not_fire_even_after_grace_end(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 21), "PAID"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2026, 4, 22)),),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 24))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 7. Grace straddles TARGET2 holiday → grace extends
# ---------------------------------------------------------------------------


class TestScenario07_GraceStraddlesHoliday:
    def test_good_friday_and_easter_monday_extend_grace(
        self, empty_market: MarketState
    ) -> None:
        # Notice Thu 2025-04-17.
        # 2025-04-18 = Good Friday, 04-19/20 weekend, 04-21 = Easter Monday.
        # First biz day after notice = Tue 2025-04-22 → grace_end.
        # as_of Mon 2025-04-21 → inside grace → WARNING, not TRIGGER.
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2025, 4, 16), "PENDING"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2025, 4, 17)),),
        )
        market = MarketState(
            built_at=datetime(2025, 4, 21, 8, 0, tzinfo=timezone.utc),
            latest={},
            attestation_refs={},
            missing=frozenset(),
            missing_consecutive_days={},
        )

        outcome = r001_rule.predicate(market, contract, date(2025, 4, 21))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING, (
            "Easter Monday 2025-04-21 should still be inside the 1-biz-day "
            "grace period from a Thu 2025-04-17 notice; got TRIGGER instead"
        )
        assert any("grace_end=2025-04-22" in e.value for e in outcome.evidence)

    def test_first_business_day_after_holiday_is_trigger(
        self, empty_market: MarketState
    ) -> None:
        # Same notice as above. Wed 2025-04-23 is the day *after* grace_end
        # (Tue 2025-04-22) → TRIGGER.
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2025, 4, 16), "PENDING"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2025, 4, 17)),),
        )
        market = MarketState(
            built_at=datetime(2025, 4, 23, 8, 0, tzinfo=timezone.utc),
            latest={},
            attestation_refs={},
            missing=frozenset(),
            missing_consecutive_days={},
        )
        outcome = r001_rule.predicate(market, contract, date(2025, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.TRIGGER


# ---------------------------------------------------------------------------
# 8. Schedule overrides grace to 3 days → rule respects override
# ---------------------------------------------------------------------------


class TestScenario08_ScheduleOverride:
    def test_override_three_days_inside_grace_is_warning(
        self, empty_market: MarketState
    ) -> None:
        # Notice Mon 2026-04-20. Grace = 3 biz days → grace_end Thu 2026-04-23.
        # as_of Wed 2026-04-22 → inside grace → WARNING.
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 19), "PENDING"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2026, 4, 20)),),
            schedule=Schedule(grace_period_failure_to_pay=3),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 22))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert any("grace_end=2026-04-23" in e.value for e in outcome.evidence)
        assert any(
            "3 business day" in e.value and "grace_period" in e.key
            for e in outcome.evidence
        )

    def test_override_three_days_grace_elapsed_is_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 19), "PENDING"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2026, 4, 20)),),
            schedule=Schedule(grace_period_failure_to_pay=3),
        )
        outcome = r001_rule.predicate(empty_market, contract, date(2026, 4, 24))
        assert outcome.fired is True
        assert outcome.severity is Severity.TRIGGER

    def test_override_must_be_non_negative_int(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            scheduled_payments=(
                Payment("P1", Decimal("1000"), date(2026, 4, 19), "PENDING"),
            ),
            notices=(Notice("failure_to_pay", "P1", date(2026, 4, 20)),),
            schedule=Schedule(grace_period_failure_to_pay=-1),
        )
        with pytest.raises(ValueError, match="non-negative int"):
            r001_rule.predicate(empty_market, contract, date(2026, 4, 22))


# ---------------------------------------------------------------------------
# Indeterminate: no scheduled_payments attribute at all
# ---------------------------------------------------------------------------


class TestIndeterminate:
    def test_contract_without_scheduled_payments_returns_indeterminate(
        self, empty_market: MarketState
    ) -> None:
        class BareContract:
            contract_id = "C-bare"

        outcome = r001_rule.predicate(empty_market, BareContract(), date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert outcome.indeterminate_reason is not None
        assert "scheduled_payments" in outcome.indeterminate_reason
