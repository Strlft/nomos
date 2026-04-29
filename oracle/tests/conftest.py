"""Shared pytest fixtures for the Oracle test suite.

Kept intentionally thin — Phase 1 only needs factories that produce valid
instances of each type so tests can poke at invariants without wrestling
with every required field.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

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


# ---------------------------------------------------------------------------
# Integration-test gating
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run tests marked @pytest.mark.integration (real network calls)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-integration"):
        return
    skip_marker = pytest.mark.skip(
        reason="integration test; pass --run-integration (or -m integration) to opt in"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


UTC = timezone.utc


def _fixed_datetime() -> datetime:
    return datetime(2026, 4, 23, 18, 0, 0, tzinfo=UTC)


def _fixed_date() -> date:
    return date(2026, 4, 23)


def _uuid(n: int) -> UUID:
    return UUID(int=n)


@pytest.fixture
def raw_datapoint() -> RawDatapoint:
    return RawDatapoint(
        source_id="ecb_sdw_v1",
        metric="ESTR",
        raw_payload='{"value": "0.0375"}',
        source_hash="a" * 64,
        fetched_at=_fixed_datetime(),
        source_url="https://data-api.ecb.europa.eu/service/data/EST",
        source_reported_as_of="2026-04-23",
    )


@pytest.fixture
def normalized_datapoint() -> NormalizedDatapoint:
    return NormalizedDatapoint(
        source_id="ecb_sdw_v1",
        metric=Metric.ESTR,
        value=Decimal("0.0375"),
        unit=Unit.DECIMAL_FRACTION,
        as_of=_fixed_date(),
        fetched_at=_fixed_datetime(),
        source_hash="a" * 64,
        source_url="https://data-api.ecb.europa.eu/service/data/EST",
        sanity_band_passed=True,
        cross_validated=False,
        cross_checked_against=None,
    )


@pytest.fixture
def genesis_attestation(normalized_datapoint: NormalizedDatapoint) -> OracleAttestation:
    return OracleAttestation(
        attestation_id=_uuid(1),
        sequence_number=0,
        datapoints=(normalized_datapoint,),
        signed_at=_fixed_datetime(),
        rules_version="1.0.0",
        oracle_version="0.1.0",
        payload_hash="b" * 64,
        previous_hash=None,
        current_hash="c" * 64,
        is_genesis=True,
    )


@pytest.fixture
def successor_attestation(
    normalized_datapoint: NormalizedDatapoint,
) -> OracleAttestation:
    return OracleAttestation(
        attestation_id=_uuid(2),
        sequence_number=1,
        datapoints=(normalized_datapoint,),
        signed_at=_fixed_datetime(),
        rules_version="1.0.0",
        oracle_version="0.1.0",
        payload_hash="d" * 64,
        previous_hash="c" * 64,
        current_hash="e" * 64,
        is_genesis=False,
    )


@pytest.fixture
def market_state(normalized_datapoint: NormalizedDatapoint) -> MarketState:
    return MarketState(
        built_at=_fixed_datetime(),
        latest={Metric.ESTR: normalized_datapoint},
        attestation_refs={Metric.ESTR: _uuid(1)},
        missing=frozenset({Metric.EURIBOR_6M}),
        missing_consecutive_days={Metric.EURIBOR_6M: 2},
    )


@pytest.fixture
def evidence() -> Evidence:
    return Evidence(
        kind="market_datum",
        key="ESTR",
        value="0.0375",
        source="oracle",
    )


@pytest.fixture
def rule_outcome(evidence: Evidence) -> RuleOutcome:
    return RuleOutcome(
        fired=True,
        severity=Severity.WARNING,
        evidence=(evidence,),
        indeterminate=False,
        indeterminate_reason=None,
    )


@pytest.fixture
def rule() -> Rule:
    def _predicate(*_args: object, **_kwargs: object) -> RuleOutcome:
        return RuleOutcome(fired=False)

    return Rule(
        rule_id="R-001",
        clause_ref="ISDA 2002 §5(a)(i)",
        severity=Severity.TRIGGER,
        predicate=_predicate,
        required_metrics=frozenset(),
        required_contract_fields=frozenset({"scheduled_payments"}),
        grace_period=timedelta(days=1),
        version="1.0.0",
        description="Failure to Pay or Deliver",
    )


@pytest.fixture
def trigger_event(evidence: Evidence) -> TriggerEvent:
    return TriggerEvent(
        event_id=_uuid(10),
        rule_id="R-001",
        rule_version="1.0.0",
        clause_ref="ISDA 2002 §5(a)(i)",
        severity=Severity.TRIGGER,
        contract_id="IRS-0001",
        evaluated_at=_fixed_datetime(),
        as_of=_fixed_date(),
        attestation_ref=_uuid(1),
        evidence=(evidence,),
        rules_version="1.0.0",
    )


@pytest.fixture
def source_failure() -> SourceFailure:
    return SourceFailure(
        failure_id=_uuid(99),
        source_id="ecb_sdw_v1",
        metric=Metric.ESTR,
        attempted_at=_fixed_datetime(),
        failure_kind="http_5xx",
        attempts=3,
        last_error_message="503 Service Unavailable",
        source_url="https://data-api.ecb.europa.eu/service/data/EST",
        context={"retry_after": "30"},
    )
