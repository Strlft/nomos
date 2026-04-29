"""Attestation construction, hashing, and chain verification.

Every :class:`OracleAttestation` carries three hashes (see SPEC §I2):

* ``payload_hash``  — SHA-256 of the canonical JSON of the payload
  (the datapoints plus the signing metadata).
* ``previous_hash`` — the prior attestation's ``current_hash`` (``None`` for
  the genesis record).
* ``current_hash``  — SHA-256 of the concatenation
  ``payload_hash || previous_hash``.

This module contains everything needed to build those hashes, to verify a
single attestation against its expected predecessor, and to verify a whole
chain end-to-end. No SQLite, no I/O — that lives in :mod:`oracle.core.store`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from oracle.config import Metric, Unit
from oracle.types import NormalizedDatapoint, OracleAttestation


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


def canonical_json(payload: dict) -> bytes:
    """JSON-encoded, sorted keys, no whitespace, UTF-8.

    Input ``payload`` must already contain only primitive types (str, int,
    bool, None, list, dict). Callers are expected to pre-convert Decimal,
    date, datetime, UUID, and Enum instances via :func:`datapoint_to_dict`
    / :func:`payload_dict` below.
    """

    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Datapoint ↔ primitive dict
# ---------------------------------------------------------------------------


def datapoint_to_dict(dp: NormalizedDatapoint) -> dict:
    """Serialize a :class:`NormalizedDatapoint` to primitive types."""

    return {
        "source_id": dp.source_id,
        "metric": dp.metric.value,
        "value": str(dp.value),
        "unit": dp.unit.value,
        "as_of": dp.as_of.isoformat(),
        "fetched_at": dp.fetched_at.isoformat(),
        "source_hash": dp.source_hash,
        "source_url": dp.source_url,
        "sanity_band_passed": dp.sanity_band_passed,
        "cross_validated": dp.cross_validated,
        "cross_checked_against": dp.cross_checked_against,
    }


def dict_to_datapoint(d: dict) -> NormalizedDatapoint:
    """Inverse of :func:`datapoint_to_dict`. Raises on missing/invalid fields."""

    return NormalizedDatapoint(
        source_id=d["source_id"],
        metric=Metric(d["metric"]),
        value=Decimal(d["value"]),
        unit=Unit(d["unit"]),
        as_of=date.fromisoformat(d["as_of"]),
        fetched_at=datetime.fromisoformat(d["fetched_at"]),
        source_hash=d["source_hash"],
        source_url=d["source_url"],
        sanity_band_passed=d["sanity_band_passed"],
        cross_validated=d["cross_validated"],
        cross_checked_against=d["cross_checked_against"],
    )


def payload_dict(
    datapoints: tuple[NormalizedDatapoint, ...],
    signed_at: datetime,
    rules_version: str,
    oracle_version: str,
) -> dict:
    """The dict that gets canonicalised and hashed as the attestation payload."""

    return {
        "datapoints": [datapoint_to_dict(dp) for dp in datapoints],
        "oracle_version": oracle_version,
        "rules_version": rules_version,
        "signed_at": signed_at.isoformat(),
    }


def payload_from_dict(payload: dict) -> dict:
    """Reconstruct the canonical Python objects from a parsed payload dict.

    Returns a new dict with ``datapoints`` reconstructed as a
    ``tuple[NormalizedDatapoint, ...]`` and ``signed_at`` as a ``datetime``.
    """

    return {
        "datapoints": tuple(dict_to_datapoint(d) for d in payload["datapoints"]),
        "signed_at": datetime.fromisoformat(payload["signed_at"]),
        "rules_version": payload["rules_version"],
        "oracle_version": payload["oracle_version"],
    }


# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_payload_hash(
    datapoints: tuple[NormalizedDatapoint, ...],
    signed_at: datetime,
    rules_version: str,
    oracle_version: str,
) -> str:
    """SHA-256 hex of the canonical payload JSON."""

    payload = payload_dict(datapoints, signed_at, rules_version, oracle_version)
    return _sha256_hex(canonical_json(payload))


def compute_current_hash(payload_hash: str, previous_hash: str | None) -> str:
    """SHA-256 hex of ``payload_hash || previous_hash``.

    For the genesis record, ``previous_hash`` is ``None`` and the empty string
    is used as the deterministic placeholder.
    """

    concatenated = (payload_hash + (previous_hash or "")).encode("utf-8")
    return _sha256_hex(concatenated)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_attestation(
    *,
    datapoints: tuple[NormalizedDatapoint, ...],
    signed_at: datetime,
    rules_version: str,
    oracle_version: str,
    previous_attestation: OracleAttestation | None = None,
    attestation_id: UUID | None = None,
    supersedes: UUID | None = None,
    supersession_reason: str | None = None,
) -> OracleAttestation:
    """Construct a fully-hashed, chained :class:`OracleAttestation`."""

    payload_hash = compute_payload_hash(
        datapoints, signed_at, rules_version, oracle_version
    )

    if previous_attestation is None:
        sequence_number = 0
        previous_hash: str | None = None
        is_genesis = True
    else:
        sequence_number = previous_attestation.sequence_number + 1
        previous_hash = previous_attestation.current_hash
        is_genesis = False

    current_hash = compute_current_hash(payload_hash, previous_hash)

    return OracleAttestation(
        attestation_id=attestation_id or uuid4(),
        sequence_number=sequence_number,
        datapoints=datapoints,
        signed_at=signed_at,
        rules_version=rules_version,
        oracle_version=oracle_version,
        payload_hash=payload_hash,
        previous_hash=previous_hash,
        current_hash=current_hash,
        is_genesis=is_genesis,
        supersedes=supersedes,
        supersession_reason=supersession_reason,
    )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_attestation(
    attestation: OracleAttestation,
    expected_previous_hash: str | None,
) -> bool:
    """Recompute every hash on ``attestation`` and confirm the chain link.

    Returns ``True`` iff:

    * ``attestation.previous_hash`` equals ``expected_previous_hash``.
    * ``payload_hash`` matches the recomputed hash of the payload.
    * ``current_hash`` matches ``SHA256(payload_hash || previous_hash)``.
    * The genesis invariant is consistent with ``previous_hash``.
    """

    if attestation.previous_hash != expected_previous_hash:
        return False

    expected_payload_hash = compute_payload_hash(
        attestation.datapoints,
        attestation.signed_at,
        attestation.rules_version,
        attestation.oracle_version,
    )
    if attestation.payload_hash != expected_payload_hash:
        return False

    expected_current_hash = compute_current_hash(
        attestation.payload_hash, attestation.previous_hash
    )
    if attestation.current_hash != expected_current_hash:
        return False

    if attestation.is_genesis != (attestation.previous_hash is None):
        return False

    return True


def verify_chain(
    attestations: list[OracleAttestation],
) -> tuple[bool, str | None]:
    """Verify a list of attestations in ascending sequence order.

    Returns ``(True, None)`` on success. On failure, returns
    ``(False, error)`` where ``error`` identifies the first attestation that
    broke verification (by sequence number and attestation_id).
    """

    if not attestations:
        return True, None

    ordered = sorted(attestations, key=lambda a: a.sequence_number)

    # Enforce gap-free sequence starting at 0 and flag the first offender.
    for index, att in enumerate(ordered):
        if att.sequence_number != index:
            return False, (
                f"sequence gap at position {index}: "
                f"got sequence_number={att.sequence_number}, "
                f"attestation_id={att.attestation_id}"
            )

    expected_previous_hash: str | None = None
    for att in ordered:
        if not verify_attestation(att, expected_previous_hash):
            return False, (
                f"verification failed at sequence_number={att.sequence_number}, "
                f"attestation_id={att.attestation_id}"
            )
        expected_previous_hash = att.current_hash

    return True, None
