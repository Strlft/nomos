"""Append-only SQLite persistence for Oracle attestations, failures, triggers.

Append-only means: no ``UPDATE``, no ``DELETE`` on production paths.
Corrections are recorded as new attestations whose ``supersedes`` field points
at the record being replaced — both remain visible forever.

Integrity notes:

* :meth:`AttestationStore.append` is the only entry point for writing
  attestations. It refuses any write whose ``previous_hash`` or
  ``sequence_number`` don't match the head of the chain, raising
  :class:`ChainIntegrityError`.
* ``payload_json`` is the canonical JSON that was hashed at signing time. It
  is the source of truth for reconstruction and verification; the ``datapoints``
  table is a denormalised view for queries.
* :meth:`verify_integrity` reloads every attestation and re-runs
  :func:`verify_chain`, catching any byte-level tampering with ``payload_json``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from oracle.core.attestation import (
    canonical_json,
    payload_dict,
    payload_from_dict,
    verify_chain,
)
from oracle.errors import ChainIntegrityError
from oracle.types import (
    NormalizedDatapoint,
    OracleAttestation,
    SourceFailure,
    TriggerEvent,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS attestations (
    attestation_id       TEXT PRIMARY KEY,
    sequence_number      INTEGER NOT NULL UNIQUE,
    payload_json         TEXT NOT NULL,
    payload_hash         TEXT NOT NULL,
    previous_hash        TEXT,
    current_hash         TEXT NOT NULL UNIQUE,
    signed_at            TEXT NOT NULL,
    rules_version        TEXT NOT NULL,
    oracle_version       TEXT NOT NULL,
    is_genesis           INTEGER NOT NULL,
    supersedes           TEXT,
    supersession_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_attestations_signed_at
    ON attestations (signed_at);
CREATE INDEX IF NOT EXISTS idx_attestations_sequence
    ON attestations (sequence_number);

CREATE TABLE IF NOT EXISTS datapoints (
    datapoint_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    attestation_id       TEXT NOT NULL REFERENCES attestations(attestation_id),
    source_id            TEXT NOT NULL,
    metric               TEXT NOT NULL,
    value                TEXT NOT NULL,
    unit                 TEXT NOT NULL,
    as_of                TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    source_hash          TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    cross_validated      INTEGER NOT NULL,
    cross_checked_against TEXT
);

CREATE INDEX IF NOT EXISTS idx_datapoints_metric_asof
    ON datapoints (metric, as_of);
CREATE INDEX IF NOT EXISTS idx_datapoints_attestation
    ON datapoints (attestation_id);

CREATE TABLE IF NOT EXISTS source_failures (
    failure_id           TEXT PRIMARY KEY,
    source_id            TEXT NOT NULL,
    metric               TEXT NOT NULL,
    attempted_at         TEXT NOT NULL,
    failure_kind         TEXT NOT NULL,
    attempts             INTEGER NOT NULL,
    last_error_message   TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    context_json         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trigger_events (
    event_id             TEXT PRIMARY KEY,
    rule_id              TEXT NOT NULL,
    rule_version         TEXT NOT NULL,
    clause_ref           TEXT NOT NULL,
    severity             TEXT NOT NULL,
    contract_id          TEXT NOT NULL,
    evaluated_at         TEXT NOT NULL,
    as_of                TEXT NOT NULL,
    attestation_ref      TEXT NOT NULL REFERENCES attestations(attestation_id),
    evidence_json        TEXT NOT NULL,
    rules_version        TEXT NOT NULL
);
"""


_LAST_ERROR_MAX_LEN = 2000


class AttestationStore:
    """SQLite-backed, append-only store for the Oracle."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def append(self, attestation: OracleAttestation) -> None:
        """Append a new attestation. Raises on any chain-integrity violation.

        The append is atomic across ``attestations`` and ``datapoints``.
        """

        with self._connect() as conn:
            cur = conn.execute(
                "SELECT sequence_number, current_hash "
                "FROM attestations ORDER BY sequence_number DESC LIMIT 1"
            )
            row = cur.fetchone()

            if row is None:
                expected_seq = 0
                expected_prev: str | None = None
            else:
                expected_seq = row[0] + 1
                expected_prev = row[1]

            if attestation.sequence_number != expected_seq:
                raise ChainIntegrityError(
                    f"expected sequence_number={expected_seq}, "
                    f"got {attestation.sequence_number}"
                )
            if attestation.previous_hash != expected_prev:
                raise ChainIntegrityError(
                    f"expected previous_hash={expected_prev!r}, "
                    f"got {attestation.previous_hash!r}"
                )

            payload = payload_dict(
                attestation.datapoints,
                attestation.signed_at,
                attestation.rules_version,
                attestation.oracle_version,
            )
            payload_json_bytes = canonical_json(payload)

            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO attestations (
                        attestation_id, sequence_number, payload_json,
                        payload_hash, previous_hash, current_hash,
                        signed_at, rules_version, oracle_version,
                        is_genesis, supersedes, supersession_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(attestation.attestation_id),
                        attestation.sequence_number,
                        payload_json_bytes.decode("utf-8"),
                        attestation.payload_hash,
                        attestation.previous_hash,
                        attestation.current_hash,
                        attestation.signed_at.isoformat(),
                        attestation.rules_version,
                        attestation.oracle_version,
                        1 if attestation.is_genesis else 0,
                        str(attestation.supersedes) if attestation.supersedes else None,
                        attestation.supersession_reason,
                    ),
                )
                for dp in attestation.datapoints:
                    conn.execute(
                        """
                        INSERT INTO datapoints (
                            attestation_id, source_id, metric, value, unit,
                            as_of, fetched_at, source_hash, source_url,
                            cross_validated, cross_checked_against
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(attestation.attestation_id),
                            dp.source_id,
                            dp.metric.value,
                            str(dp.value),
                            dp.unit.value,
                            dp.as_of.isoformat(),
                            dp.fetched_at.isoformat(),
                            dp.source_hash,
                            dp.source_url,
                            1 if dp.cross_validated else 0,
                            dp.cross_checked_against,
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def record_failure(self, failure: SourceFailure) -> None:
        """Insert a :class:`SourceFailure`. Independent from the chain."""

        with self._connect() as conn:
            truncated = failure.last_error_message[:_LAST_ERROR_MAX_LEN]
            conn.execute(
                """
                INSERT INTO source_failures (
                    failure_id, source_id, metric, attempted_at,
                    failure_kind, attempts, last_error_message,
                    source_url, context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(failure.failure_id),
                    failure.source_id,
                    failure.metric.value,
                    failure.attempted_at.isoformat(),
                    failure.failure_kind,
                    failure.attempts,
                    truncated,
                    failure.source_url,
                    json.dumps(dict(failure.context), sort_keys=True),
                ),
            )
            conn.commit()

    def record_trigger(self, event: TriggerEvent) -> None:
        """Insert a :class:`TriggerEvent` referencing its source attestation."""

        with self._connect() as conn:
            evidence_payload = [
                {
                    "kind": ev.kind,
                    "key": ev.key,
                    "value": ev.value,
                    "source": ev.source,
                }
                for ev in event.evidence
            ]
            conn.execute(
                """
                INSERT INTO trigger_events (
                    event_id, rule_id, rule_version, clause_ref, severity,
                    contract_id, evaluated_at, as_of, attestation_ref,
                    evidence_json, rules_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    event.rule_id,
                    event.rule_version,
                    event.clause_ref,
                    event.severity.value,
                    event.contract_id,
                    event.evaluated_at.isoformat(),
                    event.as_of.isoformat(),
                    str(event.attestation_ref),
                    json.dumps(evidence_payload, sort_keys=True),
                    event.rules_version,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_all_attestations(self) -> list[OracleAttestation]:
        """Return every attestation in ascending sequence order."""

        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT attestation_id, sequence_number, payload_json,
                       payload_hash, previous_hash, current_hash,
                       is_genesis, supersedes, supersession_reason
                FROM attestations
                ORDER BY sequence_number ASC
                """
            )
            rows = cur.fetchall()

        return [_row_to_attestation(row) for row in rows]

    def get_latest_attestation(self) -> OracleAttestation | None:
        """Return the attestation with the highest sequence number, or None."""

        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT attestation_id, sequence_number, payload_json,
                       payload_hash, previous_hash, current_hash,
                       is_genesis, supersedes, supersession_reason
                FROM attestations
                ORDER BY sequence_number DESC LIMIT 1
                """
            )
            row = cur.fetchone()

        return _row_to_attestation(row) if row else None

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def verify_integrity(self) -> tuple[bool, str | None]:
        """Re-run :func:`verify_chain` over every stored attestation.

        Catches tampering with ``payload_json`` even if the hash columns
        were left alone (the recomputed hash won't match), as well as
        broken previous→current hash links.
        """

        # Byte-level check: payload_hash is defined as the SHA-256 of the
        # canonical JSON bytes that were stored. Recompute directly over the
        # stored bytes rather than parse-and-re-canonicalise, since
        # ``datetime.fromisoformat`` (3.11+) and similar parsers can absorb
        # certain byte mutations and reproduce the original representation.
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT sequence_number, attestation_id, payload_json, "
                "payload_hash FROM attestations ORDER BY sequence_number ASC"
            )
            rows = cur.fetchall()

        for seq, att_id, payload_json, stored_hash in rows:
            recomputed = hashlib.sha256(
                payload_json.encode("utf-8")
            ).hexdigest()
            if recomputed != stored_hash:
                return False, (
                    f"payload_json byte-level mismatch at "
                    f"sequence_number={seq}, attestation_id={att_id}: "
                    f"stored payload_hash does not match SHA-256 of stored bytes"
                )

        try:
            attestations = self.get_all_attestations()
        except Exception as exc:  # noqa: BLE001 — surface any reload failure
            return False, f"failed to reload attestations: {exc}"
        return verify_chain(attestations)


# ---------------------------------------------------------------------------
# Row → OracleAttestation
# ---------------------------------------------------------------------------


def _row_to_attestation(row: Iterable) -> OracleAttestation:
    (
        attestation_id,
        sequence_number,
        payload_json,
        payload_hash,
        previous_hash,
        current_hash,
        is_genesis,
        supersedes,
        supersession_reason,
    ) = row

    payload = payload_from_dict(json.loads(payload_json))

    return OracleAttestation(
        attestation_id=UUID(attestation_id),
        sequence_number=sequence_number,
        datapoints=payload["datapoints"],
        signed_at=payload["signed_at"],
        rules_version=payload["rules_version"],
        oracle_version=payload["oracle_version"],
        payload_hash=payload_hash,
        previous_hash=previous_hash,
        current_hash=current_hash,
        is_genesis=bool(is_genesis),
        supersedes=UUID(supersedes) if supersedes else None,
        supersession_reason=supersession_reason,
    )
