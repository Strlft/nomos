"""Phase 1 type-layer tests.

Covers the four invariants the scaffold must enforce before any logic is
written on top of these models:

(a) Every model rejects attribute mutation.
(b) Missing required fields raise ValidationError.
(c) Decimal fields reject float inputs in strict mode.
(d,e) OracleAttestation enforces the genesis invariant in both directions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from oracle.config import Metric, Severity, Unit
from oracle.types import (
    Evidence,
    MarketState,
    NormalizedDatapoint,
    OracleAttestation,
    RawDatapoint,
    Rule,
    RuleOutcome,
    SourceFailure,
    TriggerEvent,
)


UTC = timezone.utc


# ---------------------------------------------------------------------------
# (a) Mutation rejected on every model
# ---------------------------------------------------------------------------


class TestImmutability:
    """Every frozen model must refuse attribute assignment."""

    def test_raw_datapoint_is_frozen(self, raw_datapoint: RawDatapoint) -> None:
        with pytest.raises((ValidationError, TypeError)):
            raw_datapoint.metric = "EURIBOR_3M"  # type: ignore[misc]

    def test_normalized_datapoint_is_frozen(
        self, normalized_datapoint: NormalizedDatapoint
    ) -> None:
        with pytest.raises((ValidationError, TypeError)):
            normalized_datapoint.value = Decimal("0.05")  # type: ignore[misc]

    def test_oracle_attestation_is_frozen(
        self, genesis_attestation: OracleAttestation
    ) -> None:
        with pytest.raises((ValidationError, TypeError)):
            genesis_attestation.sequence_number = 42  # type: ignore[misc]

    def test_market_state_is_frozen(self, market_state: MarketState) -> None:
        with pytest.raises((ValidationError, TypeError)):
            market_state.missing = frozenset()  # type: ignore[misc]

    def test_evidence_is_frozen(self, evidence: Evidence) -> None:
        with pytest.raises((ValidationError, TypeError)):
            evidence.value = "tampered"  # type: ignore[misc]

    def test_rule_outcome_is_frozen(self, rule_outcome: RuleOutcome) -> None:
        with pytest.raises((ValidationError, TypeError)):
            rule_outcome.fired = False  # type: ignore[misc]

    def test_rule_is_frozen(self, rule: Rule) -> None:
        with pytest.raises((ValidationError, TypeError)):
            rule.version = "9.9.9"  # type: ignore[misc]

    def test_trigger_event_is_frozen(self, trigger_event: TriggerEvent) -> None:
        with pytest.raises((ValidationError, TypeError)):
            trigger_event.severity = Severity.WARNING  # type: ignore[misc]

    def test_source_failure_is_frozen(self, source_failure: SourceFailure) -> None:
        with pytest.raises((ValidationError, TypeError)):
            source_failure.attempts = 0  # type: ignore[misc]

    def test_tuple_of_datapoints_is_not_a_list(
        self, genesis_attestation: OracleAttestation
    ) -> None:
        # Declared as tuple[...]; if declared as list[...] Pydantic would
        # coerce and this check would silently fail.
        assert isinstance(genesis_attestation.datapoints, tuple)


# ---------------------------------------------------------------------------
# (b) Required fields validated
# ---------------------------------------------------------------------------


class TestRequiredFields:
    """Each model refuses to construct when a required field is omitted."""

    def test_raw_datapoint_requires_source_id(self) -> None:
        with pytest.raises(ValidationError):
            RawDatapoint(  # type: ignore[call-arg]
                metric="ESTR",
                raw_payload="{}",
                source_hash="a" * 64,
                fetched_at=datetime(2026, 4, 23, tzinfo=UTC),
                source_url="https://example.test",
            )

    def test_normalized_datapoint_requires_value(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedDatapoint(  # type: ignore[call-arg]
                source_id="ecb_sdw_v1",
                metric=Metric.ESTR,
                unit=Unit.DECIMAL_FRACTION,
                as_of=date(2026, 4, 23),
                fetched_at=datetime(2026, 4, 23, tzinfo=UTC),
                source_hash="a" * 64,
                source_url="https://example.test",
                sanity_band_passed=True,
                cross_validated=False,
            )

    def test_oracle_attestation_requires_current_hash(
        self, normalized_datapoint: NormalizedDatapoint
    ) -> None:
        with pytest.raises(ValidationError):
            OracleAttestation(  # type: ignore[call-arg]
                attestation_id=UUID(int=1),
                sequence_number=0,
                datapoints=(normalized_datapoint,),
                signed_at=datetime(2026, 4, 23, tzinfo=UTC),
                rules_version="1.0.0",
                oracle_version="0.1.0",
                payload_hash="b" * 64,
                previous_hash=None,
                is_genesis=True,
            )

    def test_trigger_event_requires_contract_id(self, evidence: Evidence) -> None:
        with pytest.raises(ValidationError):
            TriggerEvent(  # type: ignore[call-arg]
                event_id=UUID(int=5),
                rule_id="R-001",
                rule_version="1.0.0",
                clause_ref="§5(a)(i)",
                severity=Severity.TRIGGER,
                evaluated_at=datetime(2026, 4, 23, tzinfo=UTC),
                as_of=date(2026, 4, 23),
                attestation_ref=UUID(int=1),
                evidence=(evidence,),
                rules_version="1.0.0",
            )

    def test_source_failure_requires_failure_kind(self) -> None:
        with pytest.raises(ValidationError):
            SourceFailure(  # type: ignore[call-arg]
                failure_id=UUID(int=99),
                source_id="ecb_sdw_v1",
                metric=Metric.ESTR,
                attempted_at=datetime(2026, 4, 23, tzinfo=UTC),
                attempts=3,
                last_error_message="boom",
                source_url="https://example.test",
            )

    def test_source_failure_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValidationError):
            SourceFailure(
                failure_id=UUID(int=99),
                source_id="ecb_sdw_v1",
                metric=Metric.ESTR,
                attempted_at=datetime(2026, 4, 23, tzinfo=UTC),
                failure_kind="implausible",  # type: ignore[arg-type]
                attempts=3,
                last_error_message="boom",
                source_url="https://example.test",
            )


# ---------------------------------------------------------------------------
# (c) Decimal fields reject float in strict mode
# ---------------------------------------------------------------------------


class TestStrictDecimalRejectsFloat:
    """Strict mode forbids ``float`` → ``Decimal`` coercion."""

    def test_normalized_datapoint_rejects_float_value(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedDatapoint(
                source_id="ecb_sdw_v1",
                metric=Metric.ESTR,
                value=0.0375,  # type: ignore[arg-type]
                unit=Unit.DECIMAL_FRACTION,
                as_of=date(2026, 4, 23),
                fetched_at=datetime(2026, 4, 23, tzinfo=UTC),
                source_hash="a" * 64,
                source_url="https://example.test",
                sanity_band_passed=True,
                cross_validated=False,
            )

    def test_normalized_datapoint_accepts_decimal_value(self) -> None:
        # Sanity: the happy path still works.
        NormalizedDatapoint(
            source_id="ecb_sdw_v1",
            metric=Metric.ESTR,
            value=Decimal("0.0375"),
            unit=Unit.DECIMAL_FRACTION,
            as_of=date(2026, 4, 23),
            fetched_at=datetime(2026, 4, 23, tzinfo=UTC),
            source_hash="a" * 64,
            source_url="https://example.test",
            sanity_band_passed=True,
            cross_validated=False,
        )


# ---------------------------------------------------------------------------
# (d,e) OracleAttestation genesis invariant
# ---------------------------------------------------------------------------


def _base_attestation_kwargs(normalized_datapoint: NormalizedDatapoint) -> dict:
    return {
        "attestation_id": UUID(int=1),
        "datapoints": (normalized_datapoint,),
        "signed_at": datetime(2026, 4, 23, tzinfo=UTC),
        "rules_version": "1.0.0",
        "oracle_version": "0.1.0",
        "payload_hash": "b" * 64,
        "current_hash": "c" * 64,
    }


class TestGenesisInvariant:
    """sequence_number=0 ↔ is_genesis=True ↔ previous_hash=None."""

    def test_valid_genesis(self, normalized_datapoint: NormalizedDatapoint) -> None:
        OracleAttestation(
            **_base_attestation_kwargs(normalized_datapoint),
            sequence_number=0,
            previous_hash=None,
            is_genesis=True,
        )

    def test_valid_successor(self, normalized_datapoint: NormalizedDatapoint) -> None:
        OracleAttestation(
            **_base_attestation_kwargs(normalized_datapoint),
            sequence_number=1,
            previous_hash="c" * 64,
            is_genesis=False,
        )

    def test_genesis_zero_cannot_have_previous_hash(
        self, normalized_datapoint: NormalizedDatapoint
    ) -> None:
        with pytest.raises(ValidationError):
            OracleAttestation(
                **_base_attestation_kwargs(normalized_datapoint),
                sequence_number=0,
                previous_hash="c" * 64,
                is_genesis=True,
            )

    def test_genesis_zero_requires_is_genesis_true(
        self, normalized_datapoint: NormalizedDatapoint
    ) -> None:
        with pytest.raises(ValidationError):
            OracleAttestation(
                **_base_attestation_kwargs(normalized_datapoint),
                sequence_number=0,
                previous_hash=None,
                is_genesis=False,
            )

    def test_successor_requires_is_genesis_false(
        self, normalized_datapoint: NormalizedDatapoint
    ) -> None:
        with pytest.raises(ValidationError):
            OracleAttestation(
                **_base_attestation_kwargs(normalized_datapoint),
                sequence_number=1,
                previous_hash="c" * 64,
                is_genesis=True,
            )

    def test_successor_requires_previous_hash(
        self, normalized_datapoint: NormalizedDatapoint
    ) -> None:
        with pytest.raises(ValidationError):
            OracleAttestation(
                **_base_attestation_kwargs(normalized_datapoint),
                sequence_number=1,
                previous_hash=None,
                is_genesis=False,
            )

    def test_negative_sequence_number_rejected(
        self, normalized_datapoint: NormalizedDatapoint
    ) -> None:
        with pytest.raises(ValidationError):
            OracleAttestation(
                **_base_attestation_kwargs(normalized_datapoint),
                sequence_number=-1,
                previous_hash="c" * 64,
                is_genesis=False,
            )


# ---------------------------------------------------------------------------
# Sanity: Rule carries a predicate callable
# ---------------------------------------------------------------------------


class TestRulePredicate:
    """Rule must carry a callable returning a RuleOutcome."""

    def test_predicate_roundtrips(self, rule: Rule) -> None:
        outcome = rule.predicate(None, None)
        assert isinstance(outcome, RuleOutcome)
        assert outcome.fired is False

    def test_grace_period_is_timedelta(self, rule: Rule) -> None:
        assert isinstance(rule.grace_period, timedelta)
