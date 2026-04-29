"""Tests for :class:`oracle.core.store.AttestationStore`.

Covers:

* Append → read round-trip preserves every field.
* Sequence-number gaps and duplicates are rejected.
* Broken chain (wrong ``previous_hash``) is rejected.
* ``verify_integrity()`` detects raw-SQL corruption of ``payload_json``.
* ``source_failures`` are isolated from chain integrity.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from oracle.config import Metric, Severity, Unit
from oracle.core.attestation import build_attestation
from oracle.core.store import AttestationStore
from oracle.errors import ChainIntegrityError
from oracle.types import (
    Evidence,
    NormalizedDatapoint,
    SourceFailure,
    TriggerEvent,
)


UTC = timezone.utc


def _datapoint(value: str = "0.0375") -> NormalizedDatapoint:
    return NormalizedDatapoint(
        source_id="ecb_sdw_v1",
        metric=Metric.ESTR,
        value=Decimal(value),
        unit=Unit.DECIMAL_FRACTION,
        as_of=date(2026, 4, 23),
        fetched_at=datetime(2026, 4, 23, 18, 0, 0, tzinfo=UTC),
        source_hash="a" * 64,
        source_url="https://data-api.ecb.europa.eu/service/data/EST",
        sanity_band_passed=True,
        cross_validated=False,
        cross_checked_against=None,
    )


def _genesis_and_successor():
    g = build_attestation(
        datapoints=(_datapoint(),),
        signed_at=datetime(2026, 4, 23, 18, 5, 0, tzinfo=UTC),
        rules_version="1.0.0",
        oracle_version="0.1.0",
        attestation_id=UUID(int=1),
    )
    s = build_attestation(
        datapoints=(_datapoint("0.0380"),),
        signed_at=datetime(2026, 4, 24, 18, 5, 0, tzinfo=UTC),
        rules_version="1.0.0",
        oracle_version="0.1.0",
        previous_attestation=g,
        attestation_id=UUID(int=2),
    )
    return g, s


@pytest.fixture
def store(tmp_path: Path) -> AttestationStore:
    return AttestationStore(tmp_path / "oracle.db")


# ---------------------------------------------------------------------------
# Append / read
# ---------------------------------------------------------------------------


class TestAppendAndRead:
    def test_fresh_store_is_empty(self, store: AttestationStore) -> None:
        assert store.get_latest_attestation() is None
        assert store.get_all_attestations() == []

    def test_append_then_read_genesis(self, store: AttestationStore) -> None:
        g, _ = _genesis_and_successor()
        store.append(g)

        latest = store.get_latest_attestation()
        assert latest is not None
        assert latest.attestation_id == g.attestation_id
        assert latest.sequence_number == 0
        assert latest.current_hash == g.current_hash
        assert latest == g

    def test_append_two_then_read_in_order(self, store: AttestationStore) -> None:
        g, s = _genesis_and_successor()
        store.append(g)
        store.append(s)

        all_ = store.get_all_attestations()
        assert [a.sequence_number for a in all_] == [0, 1]
        assert all_[0] == g
        assert all_[1] == s

    def test_verify_integrity_passes_after_clean_writes(
        self, store: AttestationStore
    ) -> None:
        g, s = _genesis_and_successor()
        store.append(g)
        store.append(s)
        ok, err = store.verify_integrity()
        assert ok, err


# ---------------------------------------------------------------------------
# Sequence / chain rejection
# ---------------------------------------------------------------------------


class TestAppendRejections:
    def test_out_of_order_sequence_rejected(self, store: AttestationStore) -> None:
        _, s = _genesis_and_successor()
        # Try to append a sequence_number=1 attestation into an empty store.
        with pytest.raises(ChainIntegrityError):
            store.append(s)

    def test_duplicate_sequence_rejected(self, store: AttestationStore) -> None:
        g, _ = _genesis_and_successor()
        store.append(g)

        # Another fresh genesis-looking attestation also claims sequence 0.
        another = build_attestation(
            datapoints=(_datapoint("0.0400"),),
            signed_at=datetime(2026, 4, 23, 19, 0, 0, tzinfo=UTC),
            rules_version="1.0.0",
            oracle_version="0.1.0",
            attestation_id=UUID(int=42),
        )
        with pytest.raises(ChainIntegrityError):
            store.append(another)

    def test_broken_chain_rejected(self, store: AttestationStore) -> None:
        g, _ = _genesis_and_successor()
        store.append(g)

        # Successor whose previous_hash references a different genesis.
        alien_genesis = build_attestation(
            datapoints=(_datapoint("0.0400"),),
            signed_at=datetime(2026, 4, 23, 18, 6, 0, tzinfo=UTC),
            rules_version="1.0.0",
            oracle_version="0.1.0",
            attestation_id=UUID(int=77),
        )
        alien_successor = build_attestation(
            datapoints=(_datapoint("0.0410"),),
            signed_at=datetime(2026, 4, 24, 18, 5, 0, tzinfo=UTC),
            rules_version="1.0.0",
            oracle_version="0.1.0",
            previous_attestation=alien_genesis,
            attestation_id=UUID(int=78),
        )
        with pytest.raises(ChainIntegrityError):
            store.append(alien_successor)


# ---------------------------------------------------------------------------
# verify_integrity detects raw-SQL corruption
# ---------------------------------------------------------------------------


class TestVerifyIntegrityDetectsCorruption:
    def test_payload_json_mutation_caught(
        self, store: AttestationStore, tmp_path: Path
    ) -> None:
        g, s = _genesis_and_successor()
        store.append(g)
        store.append(s)
        assert store.verify_integrity()[0] is True

        # Tamper with payload_json of the genesis record via raw SQL.
        conn = sqlite3.connect(str(tmp_path / "oracle.db"))
        try:
            conn.execute(
                "UPDATE attestations SET payload_json = ? WHERE sequence_number = 0",
                ('{"datapoints":[],"oracle_version":"0.1.0","rules_version":"1.0.0","signed_at":"2026-04-23T18:05:00+00:00"}',),
            )
            conn.commit()
        finally:
            conn.close()

        ok, err = store.verify_integrity()
        assert ok is False
        assert err is not None
        assert "sequence_number=0" in err

    def test_payload_hash_mutation_caught(
        self, store: AttestationStore, tmp_path: Path
    ) -> None:
        g, _ = _genesis_and_successor()
        store.append(g)

        conn = sqlite3.connect(str(tmp_path / "oracle.db"))
        try:
            conn.execute(
                "UPDATE attestations SET payload_hash = ? WHERE sequence_number = 0",
                ("0" * 64,),
            )
            conn.commit()
        finally:
            conn.close()

        ok, err = store.verify_integrity()
        assert ok is False
        assert err is not None

    def test_current_hash_mutation_caught(
        self, store: AttestationStore, tmp_path: Path
    ) -> None:
        g, s = _genesis_and_successor()
        store.append(g)
        store.append(s)

        conn = sqlite3.connect(str(tmp_path / "oracle.db"))
        try:
            conn.execute(
                "UPDATE attestations SET current_hash = ? WHERE sequence_number = 0",
                ("0" * 64,),
            )
            conn.commit()
        finally:
            conn.close()

        ok, err = store.verify_integrity()
        assert ok is False
        assert err is not None


# ---------------------------------------------------------------------------
# Failures and triggers live alongside attestations
# ---------------------------------------------------------------------------


class TestFailuresAndTriggers:
    def test_source_failure_does_not_affect_chain(
        self, store: AttestationStore
    ) -> None:
        g, s = _genesis_and_successor()
        store.append(g)

        failure = SourceFailure(
            failure_id=UUID(int=1001),
            source_id="ecb_sdw_v1",
            metric=Metric.ESTR,
            attempted_at=datetime(2026, 4, 23, 18, 0, 0, tzinfo=UTC),
            failure_kind="http_5xx",
            attempts=3,
            last_error_message="503 Service Unavailable",
            source_url="https://data-api.ecb.europa.eu/service/data/EST",
            context={"retry_after": "30"},
        )
        store.record_failure(failure)

        store.append(s)
        ok, err = store.verify_integrity()
        assert ok, err

    def test_trigger_event_persists(self, store: AttestationStore) -> None:
        g, _ = _genesis_and_successor()
        store.append(g)

        event = TriggerEvent(
            event_id=UUID(int=2001),
            rule_id="R-001",
            rule_version="1.0.0",
            clause_ref="ISDA 2002 §5(a)(i)",
            severity=Severity.TRIGGER,
            contract_id="IRS-0001",
            evaluated_at=datetime(2026, 4, 23, 18, 10, 0, tzinfo=UTC),
            as_of=date(2026, 4, 23),
            attestation_ref=g.attestation_id,
            evidence=(
                Evidence(
                    kind="market_datum",
                    key="ESTR",
                    value="0.0375",
                    source="oracle",
                ),
            ),
            rules_version="1.0.0",
        )
        store.record_trigger(event)
        # Integrity unaffected.
        assert store.verify_integrity()[0] is True

    def test_truncates_long_error_message(self, store: AttestationStore, tmp_path: Path) -> None:
        failure = SourceFailure(
            failure_id=UUID(int=3001),
            source_id="ecb_sdw_v1",
            metric=Metric.ESTR,
            attempted_at=datetime(2026, 4, 23, 18, 0, 0, tzinfo=UTC),
            failure_kind="parse_error",
            attempts=1,
            last_error_message="x" * 5000,
            source_url="https://example.test",
            context={},
        )
        store.record_failure(failure)

        conn = sqlite3.connect(str(tmp_path / "oracle.db"))
        try:
            (stored,) = conn.execute(
                "SELECT last_error_message FROM source_failures WHERE failure_id = ?",
                (str(failure.failure_id),),
            ).fetchone()
        finally:
            conn.close()

        assert len(stored) == 2000
