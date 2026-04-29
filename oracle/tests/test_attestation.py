"""Tests for :mod:`oracle.core.attestation`.

Covers:

* Genesis construction & verification.
* Successor construction & verification.
* Detection of a wrong ``previous_hash``.
* Detection of a mutated payload.
* Round-trip serialize → deserialize preserves every hash.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from oracle.config import Metric, Unit
from oracle.core.attestation import (
    build_attestation,
    canonical_json,
    compute_current_hash,
    compute_payload_hash,
    datapoint_to_dict,
    dict_to_datapoint,
    payload_dict,
    payload_from_dict,
    verify_attestation,
    verify_chain,
)
from oracle.types import NormalizedDatapoint, OracleAttestation


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Small in-file fixtures (distinct from conftest.py to keep this file readable)
# ---------------------------------------------------------------------------


def _datapoint(value: str = "0.0375", metric: Metric = Metric.ESTR) -> NormalizedDatapoint:
    return NormalizedDatapoint(
        source_id="ecb_sdw_v1",
        metric=metric,
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


def _genesis() -> OracleAttestation:
    return build_attestation(
        datapoints=(_datapoint(),),
        signed_at=datetime(2026, 4, 23, 18, 5, 0, tzinfo=UTC),
        rules_version="1.0.0",
        oracle_version="0.1.0",
        attestation_id=UUID(int=1),
    )


def _successor(genesis: OracleAttestation) -> OracleAttestation:
    return build_attestation(
        datapoints=(_datapoint("0.0380"),),
        signed_at=datetime(2026, 4, 24, 18, 5, 0, tzinfo=UTC),
        rules_version="1.0.0",
        oracle_version="0.1.0",
        previous_attestation=genesis,
        attestation_id=UUID(int=2),
    )


# ---------------------------------------------------------------------------
# Genesis / successor happy paths
# ---------------------------------------------------------------------------


class TestGenesis:
    def test_genesis_sequence_and_flags(self) -> None:
        g = _genesis()
        assert g.sequence_number == 0
        assert g.is_genesis is True
        assert g.previous_hash is None

    def test_genesis_verifies(self) -> None:
        g = _genesis()
        assert verify_attestation(g, expected_previous_hash=None) is True

    def test_successor_sequence_and_link(self) -> None:
        g = _genesis()
        s = _successor(g)
        assert s.sequence_number == 1
        assert s.is_genesis is False
        assert s.previous_hash == g.current_hash

    def test_successor_verifies(self) -> None:
        g = _genesis()
        s = _successor(g)
        assert verify_attestation(s, expected_previous_hash=g.current_hash) is True

    def test_chain_of_two_verifies(self) -> None:
        g = _genesis()
        s = _successor(g)
        ok, err = verify_chain([g, s])
        assert ok, err


# ---------------------------------------------------------------------------
# Wrong previous_hash detected
# ---------------------------------------------------------------------------


class TestWrongPreviousHash:
    def test_mismatched_expected_previous_hash_fails(self) -> None:
        g = _genesis()
        s = _successor(g)
        assert verify_attestation(s, expected_previous_hash="f" * 64) is False

    def test_chain_with_broken_link_fails(self) -> None:
        # Construct a well-signed successor but pretend it follows a different
        # genesis. verify_chain must catch the mismatch.
        g = _genesis()
        other_genesis = build_attestation(
            datapoints=(_datapoint("0.0400"),),
            signed_at=datetime(2026, 4, 23, 18, 5, 0, tzinfo=UTC),
            rules_version="1.0.0",
            oracle_version="0.1.0",
            attestation_id=UUID(int=99),
        )
        s = _successor(other_genesis)

        ok, err = verify_chain([g, s])
        assert ok is False
        assert err is not None
        assert "sequence_number=1" in err


# ---------------------------------------------------------------------------
# Mutated payload detected
# ---------------------------------------------------------------------------


class TestMutatedPayload:
    def test_mutated_payload_hash_fails_verification(self) -> None:
        # Build a well-formed attestation, then construct a Pydantic copy
        # whose payload_hash has been overwritten to a false value.
        g = _genesis()
        tampered = g.model_copy(update={"payload_hash": "0" * 64})
        assert verify_attestation(tampered, expected_previous_hash=None) is False

    def test_mutated_current_hash_fails_verification(self) -> None:
        g = _genesis()
        tampered = g.model_copy(update={"current_hash": "0" * 64})
        assert verify_attestation(tampered, expected_previous_hash=None) is False

    def test_mutated_datapoint_value_invalidates_payload_hash(self) -> None:
        g = _genesis()
        tampered_dp = g.datapoints[0].model_copy(update={"value": Decimal("0.9999")})
        tampered = g.model_copy(update={"datapoints": (tampered_dp,)})
        # payload_hash is still the original so verify must fail.
        assert verify_attestation(tampered, expected_previous_hash=None) is False


# ---------------------------------------------------------------------------
# Round-trip serialize/deserialize preserves current_hash
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_datapoint_roundtrips_exactly(self) -> None:
        dp = _datapoint()
        assert dict_to_datapoint(datapoint_to_dict(dp)) == dp

    def test_canonical_json_is_deterministic(self) -> None:
        payload_a = payload_dict(
            (_datapoint(),),
            datetime(2026, 4, 23, 18, 5, 0, tzinfo=UTC),
            "1.0.0",
            "0.1.0",
        )
        payload_b = payload_dict(
            (_datapoint(),),
            datetime(2026, 4, 23, 18, 5, 0, tzinfo=UTC),
            "1.0.0",
            "0.1.0",
        )
        assert canonical_json(payload_a) == canonical_json(payload_b)

    def test_roundtrip_preserves_current_hash(self) -> None:
        g = _genesis()
        # Simulate the store path: canonical bytes → parse → rebuild → re-hash.
        payload = payload_dict(
            g.datapoints, g.signed_at, g.rules_version, g.oracle_version
        )
        canonical = canonical_json(payload)
        reparsed = payload_from_dict(json.loads(canonical.decode("utf-8")))

        recomputed_payload_hash = compute_payload_hash(
            reparsed["datapoints"],
            reparsed["signed_at"],
            reparsed["rules_version"],
            reparsed["oracle_version"],
        )
        assert recomputed_payload_hash == g.payload_hash

        recomputed_current_hash = compute_current_hash(
            recomputed_payload_hash, g.previous_hash
        )
        assert recomputed_current_hash == g.current_hash

    def test_out_of_order_sequence_detected(self) -> None:
        """verify_chain rejects a chain that skips a sequence number."""

        g = _genesis()
        s = _successor(g)
        # Skip ``g`` so the chain starts at sequence_number=1 instead of 0.
        ok, err = verify_chain([s])
        assert ok is False
        assert err is not None


