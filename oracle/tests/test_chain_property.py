"""Property-based chain integrity tests (hypothesis).

(a) Every chain built from randomly generated NormalizedDatapoint tuples
    must verify True.
(b) A single-byte mutation of any stored ``payload_json`` must break
    :meth:`AttestationStore.verify_integrity`.

Strategy notes
--------------
* Values are drawn from an extreme-but-valid range so they always pass the
  sanity band that the normalizer uses upstream (tests here don't run the
  normalizer — they construct NormalizedDatapoint directly with
  ``sanity_band_passed=True``).
* Sources, hashes, and URLs are fixed ASCII strings so ``payload_json`` is
  pure ASCII and byte-level mutation is safe to represent as a character
  swap in a Python ``str``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from hypothesis import HealthCheck, given, settings, strategies as st

from oracle.config import Metric, Unit
from oracle.core.attestation import build_attestation, verify_chain
from oracle.core.store import AttestationStore
from oracle.types import NormalizedDatapoint, OracleAttestation


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_SAFE_DECIMAL = st.decimals(
    min_value=Decimal("-0.01"),
    max_value=Decimal("0.10"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def normalized_datapoint_strategy(draw: st.DrawFn) -> NormalizedDatapoint:
    metric = draw(st.sampled_from(list(Metric)))
    value = draw(_SAFE_DECIMAL)
    as_of = draw(
        st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
    )
    # fetched_at: deterministic UTC datetime keyed off as_of.
    fetched_at = datetime(as_of.year, as_of.month, as_of.day, 18, 0, 0, tzinfo=UTC)

    return NormalizedDatapoint(
        source_id="test_src_v1",
        metric=metric,
        value=value,
        unit=Unit.DECIMAL_FRACTION,
        as_of=as_of,
        fetched_at=fetched_at,
        source_hash="0" * 64,
        source_url="https://example.test/data",
        sanity_band_passed=True,
        cross_validated=False,
        cross_checked_against=None,
    )


# A chain spec is a non-empty list of non-empty datapoint tuples.
_CHAIN_SPEC = st.lists(
    st.lists(normalized_datapoint_strategy(), min_size=1, max_size=3),
    min_size=1,
    max_size=5,
)


# ---------------------------------------------------------------------------
# Chain builder
# ---------------------------------------------------------------------------


def _build_chain(
    chain_spec: list[list[NormalizedDatapoint]],
) -> list[OracleAttestation]:
    base_signed_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    chain: list[OracleAttestation] = []
    previous: OracleAttestation | None = None
    for i, dps in enumerate(chain_spec):
        att = build_attestation(
            datapoints=tuple(dps),
            signed_at=datetime(2026, 1, 1 + i, 12, 0, 0, tzinfo=UTC)
            if i < 30
            else base_signed_at,
            rules_version="1.0.0",
            oracle_version="0.1.0",
            previous_attestation=previous,
            attestation_id=uuid4(),
        )
        chain.append(att)
        previous = att
    return chain


# ---------------------------------------------------------------------------
# (a) Valid chains always verify
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(chain_spec=_CHAIN_SPEC)
def test_valid_chain_verifies(chain_spec: list[list[NormalizedDatapoint]]) -> None:
    chain = _build_chain(chain_spec)
    ok, err = verify_chain(chain)
    assert ok, err


# ---------------------------------------------------------------------------
# (b) Single-byte (character) mutation breaks verification
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    chain_spec=_CHAIN_SPEC,
    mutation_seed=st.integers(min_value=0, max_value=2**30),
)
def test_byte_mutation_breaks_chain(
    tmp_path_factory: object,  # pytest built-in factory
    chain_spec: list[list[NormalizedDatapoint]],
    mutation_seed: int,
) -> None:
    # Hypothesis reruns this body for many examples; give each its own DB file.
    tmp_dir: Path = tmp_path_factory.mktemp("oracle_chain")  # type: ignore[attr-defined]
    db_path = tmp_dir / "oracle.db"

    store = AttestationStore(db_path)
    for att in _build_chain(chain_spec):
        store.append(att)

    # Pick an attestation and a character to mutate.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT sequence_number, payload_json FROM attestations ORDER BY sequence_number"
        ).fetchall()
        assert rows, "chain was empty"

        target_idx = mutation_seed % len(rows)
        seq, payload_json = rows[target_idx]

        # Flip one character. Canonical JSON is ASCII-only given our strategy,
        # so a simple char swap is equivalent to a single-byte mutation.
        char_idx = (mutation_seed // len(rows)) % len(payload_json)
        original = payload_json[char_idx]
        replacement = "x" if original != "x" else "y"
        mutated = payload_json[:char_idx] + replacement + payload_json[char_idx + 1 :]

        assert mutated != payload_json

        conn.execute(
            "UPDATE attestations SET payload_json = ? WHERE sequence_number = ?",
            (mutated, seq),
        )
        conn.commit()
    finally:
        conn.close()

    ok, err = store.verify_integrity()
    assert ok is False, "byte mutation was not detected"
    assert err is not None


# ---------------------------------------------------------------------------
# (c) Canonical JSON deterministic under reconstruction
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(chain_spec=_CHAIN_SPEC)
def test_roundtrip_preserves_hashes(
    chain_spec: list[list[NormalizedDatapoint]],
) -> None:
    from oracle.core.attestation import payload_dict, canonical_json, compute_payload_hash, payload_from_dict

    chain = _build_chain(chain_spec)
    for att in chain:
        payload = payload_dict(
            att.datapoints, att.signed_at, att.rules_version, att.oracle_version
        )
        canonical = canonical_json(payload)
        reparsed = payload_from_dict(json.loads(canonical.decode("utf-8")))
        recomputed = compute_payload_hash(
            reparsed["datapoints"],
            reparsed["signed_at"],
            reparsed["rules_version"],
            reparsed["oracle_version"],
        )
        assert recomputed == att.payload_hash
