"""Tests for backend/routers/oracle_v2_router.py.

Each test gets a fresh, empty SQLite DB via the ORACLE_DB_PATH env var.
The router is mounted on a minimal FastAPI app so these tests are
independent of backend/api.py.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from oracle.config import Metric, Severity, Unit
from oracle.core.attestation import build_attestation
from oracle.core.store import AttestationStore
from oracle.types import Evidence, NormalizedDatapoint, OracleAttestation, TriggerEvent


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


def _seed_chain(db_path: Path) -> tuple[OracleAttestation, OracleAttestation]:
    store = AttestationStore(db_path)
    g = build_attestation(
        datapoints=(_datapoint("0.0375"),),
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
    store.append(g)
    store.append(s)
    return g, s


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "oracle.db"
    monkeypatch.setenv("ORACLE_DB_PATH", str(p))
    return p


@pytest.fixture
def client(db_path: Path) -> TestClient:
    from backend.routers.oracle_v2_router import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /attestations/latest
# ---------------------------------------------------------------------------


def test_latest_returns_503_when_store_empty(client: TestClient) -> None:
    r = client.get("/api/v2/oracle/attestations/latest")
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "NO_ATTESTATION_YET"
    assert "message" in body


def test_latest_returns_most_recent_with_datapoints(
    client: TestClient, db_path: Path
) -> None:
    _, s = _seed_chain(db_path)
    r = client.get("/api/v2/oracle/attestations/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["attestation"]["attestation_id"] == str(s.attestation_id)
    assert body["attestation"]["sequence_number"] == 1
    assert body["attestation"]["is_genesis"] is False
    assert len(body["datapoints"]) == 1
    dp = body["datapoints"][0]
    assert dp["metric"] == "ESTR"
    assert dp["value"] == "0.0380"
    assert dp["unit"] == "decimal_fraction"
    assert dp["as_of"] == "2026-04-23"


# ---------------------------------------------------------------------------
# /attestations
# ---------------------------------------------------------------------------


def test_attestations_limit_returns_both_most_recent_first(
    client: TestClient, db_path: Path
) -> None:
    _seed_chain(db_path)
    r = client.get("/api/v2/oracle/attestations?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["attestation"]["sequence_number"] == 1
    assert body[1]["attestation"]["sequence_number"] == 0


# ---------------------------------------------------------------------------
# /triggers
# ---------------------------------------------------------------------------


def test_triggers_empty_store_returns_empty_list(client: TestClient) -> None:
    r = client.get("/api/v2/oracle/triggers")
    assert r.status_code == 200
    assert r.json() == []


def test_triggers_filter_by_severity(
    client: TestClient, db_path: Path
) -> None:
    g, _ = _seed_chain(db_path)
    store = AttestationStore(db_path)

    warning = TriggerEvent(
        event_id=UUID(int=100),
        rule_id="R-001",
        rule_version="1.0.0",
        clause_ref="ISDA 2002 §5(a)(i)",
        severity=Severity.WARNING,
        contract_id="IRS-0001",
        evaluated_at=datetime(2026, 4, 23, 18, 10, 0, tzinfo=UTC),
        as_of=date(2026, 4, 23),
        attestation_ref=g.attestation_id,
        evidence=(
            Evidence(kind="market_datum", key="ESTR", value="0.0375", source="oracle"),
        ),
        rules_version="1.0.0",
    )
    trigger = TriggerEvent(
        event_id=UUID(int=101),
        rule_id="R-002",
        rule_version="1.0.0",
        clause_ref="ISDA 2002 §5(a)(ii)",
        severity=Severity.TRIGGER,
        contract_id="IRS-0002",
        evaluated_at=datetime(2026, 4, 24, 18, 10, 0, tzinfo=UTC),
        as_of=date(2026, 4, 24),
        attestation_ref=g.attestation_id,
        evidence=(
            Evidence(kind="market_datum", key="ESTR", value="0.0380", source="oracle"),
        ),
        rules_version="1.0.0",
    )
    store.record_trigger(warning)
    store.record_trigger(trigger)

    r = client.get("/api/v2/oracle/triggers?severity=trigger")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["severity"] == "trigger"
    assert body[0]["rule_id"] == "R-002"
    assert body[0]["event_id"] == str(trigger.event_id)
    assert body[0]["evidence"][0]["key"] == "ESTR"


# ---------------------------------------------------------------------------
# /chain/verify
# ---------------------------------------------------------------------------


def test_chain_verify_ok_on_valid_chain(
    client: TestClient, db_path: Path
) -> None:
    _seed_chain(db_path)
    r = client.get("/api/v2/oracle/chain/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["attestations_count"] == 2
    assert isinstance(body["checked_at"], str)


def test_chain_verify_detects_payload_corruption(
    client: TestClient, db_path: Path
) -> None:
    _seed_chain(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE attestations SET payload_json = ? WHERE sequence_number = 0",
            (
                '{"datapoints":[],"oracle_version":"0.1.0","rules_version":"1.0.0",'
                '"signed_at":"2026-04-23T18:05:00+00:00"}',
            ),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/v2/oracle/chain/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] is not None


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_200_with_all_fields(
    client: TestClient, db_path: Path
) -> None:
    _seed_chain(db_path)
    r = client.get("/api/v2/oracle/health")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "db_reachable",
        "db_path",
        "last_attestation_at",
        "source_failures_24h",
        "trigger_events_24h",
        "error",
    ):
        assert key in body
    assert body["db_reachable"] is True
    assert body["last_attestation_at"] is not None
    assert body["source_failures_24h"] == 0
    assert body["trigger_events_24h"] == 0
