"""R-005 — Tax Event: every scenario in the spec's test matrix.

The whole rule is flag-based: each row of ``contract.tax_event_flags`` is
either past-effective and triggers a known kind, or it is silently
skipped. The hard rule pinned here is that severity caps at
``POTENTIAL_TRIGGER`` regardless of the flag kind or its age.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pytest

from oracle.config import Severity
from oracle.rules.impl.r005_tax_event import rule as r005_rule
from oracle.types import MarketState


# ---------------------------------------------------------------------------
# Duck-typed contract objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaxEventFlag:
    kind: str
    jurisdiction: str
    effective_date: date
    flag_id: str | None = None
    description: str | None = None
    source_reference: str | None = None


@dataclass(frozen=True)
class Contract:
    contract_id: str = "C-0005"
    tax_event_flags: tuple[TaxEventFlag, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_market() -> MarketState:
    return MarketState(
        built_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        latest={},
        attestation_refs={},
        missing=frozenset(),
        missing_consecutive_days={},
    )


def _assert_evidence_cites(outcome, flag_id: str) -> None:
    assert any(flag_id in e.key for e in outcome.evidence), (
        f"expected evidence citing flag {flag_id}; "
        f"got keys {[e.key for e in outcome.evidence]}"
    )


# ---------------------------------------------------------------------------
# 1. No flags → no trigger
# ---------------------------------------------------------------------------


class TestScenario01_NoFlags:
    def test_empty_tuple_does_not_fire(self, empty_market: MarketState) -> None:
        contract = Contract(tax_event_flags=())
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# 2. Flag with future effective_date → no trigger
# ---------------------------------------------------------------------------


class TestScenario02_FutureEffectiveDate:
    def test_future_flag_is_silent(self, empty_market: MarketState) -> None:
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="withholding_imposed",
                    jurisdiction="GB",
                    effective_date=date(2027, 1, 1),
                    flag_id="F-FUTURE",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 3. withholding_imposed (past effective) → POTENTIAL_TRIGGER
# ---------------------------------------------------------------------------


class TestScenario03_WithholdingImposed:
    def test_past_effective_emits_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="withholding_imposed",
                    jurisdiction="GB",
                    effective_date=date(2026, 1, 15),
                    flag_id="F1",
                    description="HMRC bulletin 2026-01-10",
                    source_reference="HMRC-bulletin-2026-01-10",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        _assert_evidence_cites(outcome, "F1")
        assert any("kind=withholding_imposed" in e.value for e in outcome.evidence)
        assert any("jurisdiction=GB" in e.value for e in outcome.evidence)
        assert any(e.source == "HMRC-bulletin-2026-01-10" for e in outcome.evidence)

    def test_effective_date_today_qualifies(
        self, empty_market: MarketState
    ) -> None:
        # Boundary: as_of == effective_date counts as past-effective.
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="withholding_imposed",
                    jurisdiction="FR",
                    effective_date=date(2026, 4, 23),
                    flag_id="F-BORDER",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER


# ---------------------------------------------------------------------------
# 4. indemnifiable_tax_required (past) → POTENTIAL_TRIGGER
# ---------------------------------------------------------------------------


class TestScenario04_IndemnifiableTax:
    def test_past_effective_emits_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="indemnifiable_tax_required",
                    jurisdiction="DE",
                    effective_date=date(2026, 3, 1),
                    flag_id="F-IND",
                    description="BMF circular requires gross-up",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        assert any(
            "kind=indemnifiable_tax_required" in e.value for e in outcome.evidence
        )


# ---------------------------------------------------------------------------
# 5. withholding_removed → WARNING
# ---------------------------------------------------------------------------


class TestScenario05_WithholdingRemoved:
    def test_emits_warning_audit_record(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="withholding_removed",
                    jurisdiction="GB",
                    effective_date=date(2026, 4, 1),
                    flag_id="F-REM",
                    description="HMRC notice removing withholding",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert any("kind=withholding_removed" in e.value for e in outcome.evidence)


# ---------------------------------------------------------------------------
# 6. Mixed past + future flags → only past evaluated
# ---------------------------------------------------------------------------


class TestScenario06_MixedPastAndFuture:
    def test_only_past_flags_drive_severity(
        self, empty_market: MarketState
    ) -> None:
        # Three flags:
        #   F-PAST: withholding_removed past → WARNING
        #   F-FUT-IND: indemnifiable_tax_required FUTURE → ignored
        #   F-FUT-WH: withholding_imposed FUTURE → ignored
        # Aggregate severity should be WARNING (past flag only).
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="withholding_removed",
                    jurisdiction="GB",
                    effective_date=date(2026, 1, 1),
                    flag_id="F-PAST",
                ),
                TaxEventFlag(
                    kind="indemnifiable_tax_required",
                    jurisdiction="FR",
                    effective_date=date(2027, 1, 1),
                    flag_id="F-FUT-IND",
                ),
                TaxEventFlag(
                    kind="withholding_imposed",
                    jurisdiction="DE",
                    effective_date=date(2027, 7, 1),
                    flag_id="F-FUT-WH",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        _assert_evidence_cites(outcome, "F-PAST")
        # No evidence row for the future flags.
        for fid in ("F-FUT-IND", "F-FUT-WH"):
            assert not any(fid in e.key for e in outcome.evidence)


# ---------------------------------------------------------------------------
# Severity escalation across multiple past flags
# ---------------------------------------------------------------------------


class TestSeverityEscalation:
    def test_warning_plus_potential_trigger_yields_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="withholding_removed",
                    jurisdiction="GB",
                    effective_date=date(2026, 1, 1),
                    flag_id="F-W",
                ),
                TaxEventFlag(
                    kind="withholding_imposed",
                    jurisdiction="FR",
                    effective_date=date(2026, 2, 1),
                    flag_id="F-PT",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        # Both contribute evidence — auditors get the full picture.
        _assert_evidence_cites(outcome, "F-W")
        _assert_evidence_cites(outcome, "F-PT")

    def test_never_auto_triggers_regardless_of_age_or_count(
        self, empty_market: MarketState
    ) -> None:
        # Pile on every triggering kind, with ancient effective dates,
        # in many jurisdictions. R-005 must still cap at POTENTIAL_TRIGGER.
        contract = Contract(
            tax_event_flags=tuple(
                TaxEventFlag(
                    kind=kind,
                    jurisdiction=juris,
                    effective_date=date(2010, 1, 1),
                    flag_id=f"F-{kind}-{juris}",
                )
                for kind in ("withholding_imposed", "indemnifiable_tax_required")
                for juris in ("GB", "FR", "DE", "IT", "ES")
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER, (
            "R-005 must NEVER auto-TRIGGER per ORACLE_RULES.md §R-005"
        )
        assert outcome.severity is not Severity.TRIGGER


# ---------------------------------------------------------------------------
# Indeterminate: no tax_event_flags attribute at all
# ---------------------------------------------------------------------------


class TestIndeterminate:
    def test_contract_without_tax_event_flags_is_indeterminate(
        self, empty_market: MarketState
    ) -> None:
        class BareContract:
            contract_id = "C-bare"

        outcome = r005_rule.predicate(empty_market, BareContract(), date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert "tax_event_flags" in (outcome.indeterminate_reason or "")

    def test_unknown_kind_silently_ignored(
        self, empty_market: MarketState
    ) -> None:
        # Future spec might add new kinds. R-005 stays silent rather than
        # fabricating a severity.
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="some_future_kind",
                    jurisdiction="EU",
                    effective_date=date(2026, 1, 1),
                    flag_id="F-X",
                ),
            ),
        )
        outcome = r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# Defensive: effective_date must be a date
# ---------------------------------------------------------------------------


class TestMalformedFlag:
    def test_string_effective_date_raises_value_error(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            tax_event_flags=(
                TaxEventFlag(
                    kind="withholding_imposed",
                    jurisdiction="GB",
                    effective_date="2026-01-15",  # type: ignore[arg-type]
                    flag_id="F-BAD",
                ),
            ),
        )
        with pytest.raises(ValueError, match="effective_date must be a date"):
            r005_rule.predicate(empty_market, contract, date(2026, 4, 23))
