"""R-002 — Breach of Agreement: every scenario in the test matrix.

The 30-day grace is **calendar days**, not business days, so there's no
TARGET2 holiday case for R-002. The hard rule the spec calls out — and
that this module pins explicitly — is that disaffirmation never escalates
to TRIGGER, regardless of how stale the notice is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pytest

from oracle.config import Severity
from oracle.rules.impl.r002_breach_of_agreement import rule as r002_rule
from oracle.types import MarketState


# ---------------------------------------------------------------------------
# Duck-typed contract objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreachRecord:
    kind: str  # "non_performance_other" | "disaffirmation"
    breach_id: str | None = None
    notice_sent_at: date | None = None
    remedied_at: date | None = None
    disaffirmation_notice_at: date | None = None
    description: str | None = None


@dataclass(frozen=True)
class Schedule:
    grace_period_breach_days: int | None = None


@dataclass(frozen=True)
class Contract:
    contract_id: str = "C-0002"
    breach_records: tuple[BreachRecord, ...] = ()
    schedule: Schedule | None = field(default_factory=Schedule)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_market() -> MarketState:
    """R-002 needs no market data."""

    return MarketState(
        built_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        latest={},
        attestation_refs={},
        missing=frozenset(),
        missing_consecutive_days={},
    )


def _assert_evidence_cites(outcome, breach_id: str) -> None:
    assert any(breach_id in e.key for e in outcome.evidence), (
        f"expected evidence citing breach {breach_id}; "
        f"got keys {[e.key for e in outcome.evidence]}"
    )


# ---------------------------------------------------------------------------
# 1. No breach records → no trigger
# ---------------------------------------------------------------------------


class TestScenario01_NoBreachRecords:
    def test_empty_tuple_does_not_fire(self, empty_market: MarketState) -> None:
        contract = Contract(breach_records=())
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# 2. Breach, no notice → WARNING
# ---------------------------------------------------------------------------


class TestScenario02_BreachNoNotice:
    def test_emits_warning(self, empty_market: MarketState) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=None,
                    description="late delivery of §4(a)(ii) tax form",
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        _assert_evidence_cites(outcome, "B1")
        assert any("breach_no_notice" in e.value for e in outcome.evidence)


# ---------------------------------------------------------------------------
# 3. Breach + notice, inside 30-day grace → WARNING
# ---------------------------------------------------------------------------


class TestScenario03_InsideGrace:
    def test_one_day_after_notice_is_warning(
        self, empty_market: MarketState
    ) -> None:
        # Notice 2026-04-22, grace_end 2026-05-22, as_of 2026-04-23 → inside.
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 22),
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert any("grace_end=2026-05-22" in e.value for e in outcome.evidence)

    def test_asof_equals_grace_end_is_still_warning(
        self, empty_market: MarketState
    ) -> None:
        # Notice 2026-04-22, grace_end 2026-05-22, as_of 2026-05-22 → last day.
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 22),
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 5, 22))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING


# ---------------------------------------------------------------------------
# 4. Breach + notice, remedied before grace end → no trigger
# ---------------------------------------------------------------------------


class TestScenario04_RemediedInGrace:
    def test_remedied_inside_grace_does_not_fire(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 22),
                    remedied_at=date(2026, 5, 1),
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 6, 1))
        assert outcome.fired is False, (
            "a breach remedied within the 30-day grace must not fire, even if "
            "as_of is well past grace_end"
        )

    def test_remedied_on_grace_end_does_not_fire(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 22),
                    remedied_at=date(2026, 5, 22),  # exactly grace_end
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 6, 1))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 5. Breach + notice, grace elapsed, unremedied → TRIGGER
# ---------------------------------------------------------------------------


class TestScenario05_GraceElapsedUnremedied:
    def test_day_after_grace_is_trigger(self, empty_market: MarketState) -> None:
        # Notice 2026-03-22, grace_end 2026-04-21, as_of 2026-04-22 → elapsed.
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 3, 22),
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 22))
        assert outcome.fired is True
        assert outcome.severity is Severity.TRIGGER
        assert any(
            "breach_grace_elapsed_unremedied" in e.value for e in outcome.evidence
        )


# ---------------------------------------------------------------------------
# 6. Disaffirmation notice logged → POTENTIAL_TRIGGER
# ---------------------------------------------------------------------------


class TestScenario06_Disaffirmation:
    def test_disaffirmation_emits_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="disaffirmation",
                    breach_id="D1",
                    disaffirmation_notice_at=date(2026, 4, 20),
                    description="counterparty letter purporting to repudiate",
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        _assert_evidence_cites(outcome, "D1")
        assert any(
            "disaffirmation_notice_logged" in e.value for e in outcome.evidence
        )

    def test_disaffirmation_without_notice_is_silent(
        self, empty_market: MarketState
    ) -> None:
        # disaffirmation_notice_at is the trigger; without it the record is
        # not actionable.
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="disaffirmation",
                    breach_id="D2",
                    disaffirmation_notice_at=None,
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False

    def test_disaffirmation_never_auto_triggers(
        self, empty_market: MarketState
    ) -> None:
        """The hard rule from ORACLE_RULES.md: disaffirmation NEVER → TRIGGER.

        Try every adversarial knob — ancient notice, future as_of, schedule
        override pretending to elapse — and verify the severity stays at
        POTENTIAL_TRIGGER.
        """

        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="disaffirmation",
                    breach_id="D-ANCIENT",
                    disaffirmation_notice_at=date(2010, 1, 1),
                ),
            ),
            schedule=Schedule(grace_period_breach_days=0),  # try to force escalation
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2099, 12, 31))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER, (
            "disaffirmation must NEVER escalate to TRIGGER — it requires a "
            "human characterisation per ISDA 2002 §5(a)(ii) limb 2"
        )
        assert outcome.severity is not Severity.TRIGGER


# ---------------------------------------------------------------------------
# 7. Concurrent breaches, one elapsed → TRIGGER (rule emits one event,
#    severity escalates to the highest fire across all records)
# ---------------------------------------------------------------------------


class TestScenario07_ConcurrentBreaches:
    def test_one_elapsed_drives_severity_to_trigger(
        self, empty_market: MarketState
    ) -> None:
        # B1 inside grace → WARNING; B2 grace elapsed unremedied → TRIGGER.
        # The rule emits ONE outcome with severity TRIGGER, evidence for both.
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 22),  # inside grace
                ),
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B2",
                    notice_sent_at=date(2026, 3, 1),  # grace elapsed
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.TRIGGER
        _assert_evidence_cites(outcome, "B1")
        _assert_evidence_cites(outcome, "B2")

    def test_disaffirmation_plus_warning_breach_is_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        # WARNING (no notice) + POTENTIAL_TRIGGER (disaffirmation) →
        # POTENTIAL_TRIGGER (the higher of the two).
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=None,
                ),
                BreachRecord(
                    kind="disaffirmation",
                    breach_id="D1",
                    disaffirmation_notice_at=date(2026, 4, 20),
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER

    def test_disaffirmation_plus_elapsed_breach_yields_trigger(
        self, empty_market: MarketState
    ) -> None:
        # The non_performance_other limb CAN reach TRIGGER, and that limb's
        # TRIGGER is the highest severity across the whole record set.
        # The disaffirmation record stays POTENTIAL_TRIGGER on its own row;
        # the *aggregate* outcome is TRIGGER, driven by the elapsed breach.
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="disaffirmation",
                    breach_id="D1",
                    disaffirmation_notice_at=date(2026, 4, 20),
                ),
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B-ELAPSED",
                    notice_sent_at=date(2026, 1, 1),  # well past grace
                ),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.TRIGGER


# ---------------------------------------------------------------------------
# Schedule override — calendar-day grace can be tightened or relaxed
# ---------------------------------------------------------------------------


class TestScheduleOverride:
    def test_seven_day_override_inside_grace(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 20),
                ),
            ),
            schedule=Schedule(grace_period_breach_days=7),
        )
        # grace_end = 2026-04-27. as_of = 2026-04-25 → inside.
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 25))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert any(
            "7 calendar day" in e.value and "grace_period" in e.key
            for e in outcome.evidence
        )

    def test_seven_day_override_grace_elapsed_is_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 20),
                ),
            ),
            schedule=Schedule(grace_period_breach_days=7),
        )
        # grace_end = 2026-04-27. as_of = 2026-04-28 → elapsed.
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 28))
        assert outcome.fired is True
        assert outcome.severity is Severity.TRIGGER

    def test_override_must_be_non_negative_int(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at=date(2026, 4, 20),
                ),
            ),
            schedule=Schedule(grace_period_breach_days=-1),
        )
        with pytest.raises(ValueError, match="non-negative int"):
            r002_rule.predicate(empty_market, contract, date(2026, 4, 25))


# ---------------------------------------------------------------------------
# Indeterminate: no breach_records attribute at all
# ---------------------------------------------------------------------------


class TestIndeterminate:
    def test_contract_without_breach_records_is_indeterminate(
        self, empty_market: MarketState
    ) -> None:
        class BareContract:
            contract_id = "C-bare"

        outcome = r002_rule.predicate(empty_market, BareContract(), date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert outcome.indeterminate_reason is not None
        assert "breach_records" in outcome.indeterminate_reason

    def test_unknown_breach_kind_is_silently_ignored(
        self, empty_market: MarketState
    ) -> None:
        # Non-V1 kinds are not yet handled; treating them as no-ops is safer
        # than fabricating a severity. R-002 stays silent.
        contract = Contract(
            breach_records=(
                BreachRecord(kind="some_future_kind", breach_id="X1"),
            ),
        )
        outcome = r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# Defensive: notice_sent_at must be a date if present
# ---------------------------------------------------------------------------


class TestMalformedRecord:
    def test_string_notice_raises_value_error(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            breach_records=(
                BreachRecord(
                    kind="non_performance_other",
                    breach_id="B1",
                    notice_sent_at="2026-04-22",  # type: ignore[arg-type]
                ),
            ),
        )
        with pytest.raises(ValueError, match="notice_sent_at must be a date"):
            r002_rule.predicate(empty_market, contract, date(2026, 4, 23))
