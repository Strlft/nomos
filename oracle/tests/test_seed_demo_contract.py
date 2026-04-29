"""Tests for ``oracle.scripts.seed_demo_contract``.

The seed script's contract:

* Running it on a database that already contains at least one attestation
  produces exactly one ``trigger_events`` row for R-001 with severity
  ``trigger`` and clause ref ``ISDA 2002 §5(a)(i)``.
* The chain integrity check still passes after seeding.
* A second invocation on the same database is a no-op — zero new
  ``trigger_events`` rows are inserted.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from oracle.core.store import AttestationStore
from oracle.scheduler import daily_run as scheduler
from oracle.scripts import seed_demo_contract


_FIXTURE = (
    Path(__file__).parent / "fixtures" / "minimal_estr.yaml"
).resolve()

# The minimal ESTR fixture publishes its single observation as_of 2026-04-23.
# Pin the seed run to a Thursday a few days later so add/sub_business_days
# stay inside the TARGET2 calendar's supported window (2024-2027) and
# 5 business days before is well-defined.
_AS_OF = date(2026, 4, 30)


def _seed_attestation_chain(db_path: Path) -> None:
    """Seed the chain via the public daily_run pipeline so the seed script
    finds a real attestation to reference."""

    rc = scheduler.main(
        [
            "--fixture", str(_FIXTURE),
            "--contract-id", "BOOTSTRAP-CHAIN",
            "--db-path", str(db_path),
            "--as-of", _AS_OF.isoformat(),
        ]
    )
    assert rc == 0, "daily_run --fixture should publish an attestation"


def _read_triggers(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT event_id, rule_id, severity, clause_ref, "
            "       contract_id, as_of, attestation_ref "
            "FROM trigger_events ORDER BY evaluated_at ASC"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "oracle.db"
    _seed_attestation_chain(p)
    return p


def test_seed_inserts_one_r001_trigger(db_path: Path) -> None:
    rc = seed_demo_contract.main(
        ["--db-path", str(db_path), "--as-of", _AS_OF.isoformat()]
    )
    assert rc == 0

    triggers = _read_triggers(db_path)
    # Filter out anything BOOTSTRAP-CHAIN may have emitted (it shouldn't,
    # but be defensive).
    seeded = [t for t in triggers if t["contract_id"] == "DEMO-R001"]
    assert len(seeded) == 1, f"expected 1 DEMO-R001 trigger; got {seeded!r}"
    t = seeded[0]
    assert t["rule_id"] == "R-001"
    assert t["severity"] == "trigger"
    assert t["clause_ref"] == "ISDA 2002 §5(a)(i)"
    assert t["as_of"] == _AS_OF.isoformat()


def test_seed_keeps_chain_integrity_ok(db_path: Path) -> None:
    rc = seed_demo_contract.main(
        ["--db-path", str(db_path), "--as-of", _AS_OF.isoformat()]
    )
    assert rc == 0

    store = AttestationStore(db_path)
    ok, err = store.verify_integrity()
    assert ok, f"chain integrity broken after seeding: {err}"


def test_seed_is_idempotent(db_path: Path) -> None:
    argv = ["--db-path", str(db_path), "--as-of", _AS_OF.isoformat()]

    first_rc = seed_demo_contract.main(argv)
    assert first_rc == 0
    first = _read_triggers(db_path)
    seeded_first = [t for t in first if t["contract_id"] == "DEMO-R001"]
    assert len(seeded_first) == 1

    second_rc = seed_demo_contract.main(argv)
    assert second_rc == 0  # no-op succeeds quietly
    second = _read_triggers(db_path)
    seeded_second = [t for t in second if t["contract_id"] == "DEMO-R001"]
    assert seeded_second == seeded_first, (
        "second seed must not insert a duplicate trigger_events row"
    )


def test_seed_refuses_when_no_attestation_exists(tmp_path: Path) -> None:
    # Empty store — the seed script must refuse rather than fabricate an
    # attestation_ref. Foreign-key violations would otherwise be silent.
    empty_db = tmp_path / "empty.db"
    AttestationStore(empty_db)  # creates schema, leaves all tables empty

    rc = seed_demo_contract.main(
        ["--db-path", str(empty_db), "--as-of", _AS_OF.isoformat()]
    )
    assert rc == 2, "expected precondition_failure exit code"

    conn = sqlite3.connect(str(empty_db))
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM trigger_events"
        ).fetchone()
    finally:
        conn.close()
    assert count == 0
