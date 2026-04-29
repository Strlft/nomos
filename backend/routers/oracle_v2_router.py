"""FastAPI router exposing the new oracle/ package under /api/v2/oracle/*.

This router is additive — it does not touch the legacy oracle_v3 endpoints
under /api/oracle/*. All reads route through the public AttestationStore API
and read the SQLite database at the path given by the ORACLE_DB_PATH env var
(default: ``oracle.db`` at the working directory).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from oracle.config import Severity
from oracle.core.store import AttestationStore
from oracle.types import NormalizedDatapoint, OracleAttestation


router = APIRouter(prefix="/api/v2/oracle", tags=["Oracle V2"])


def _db_path() -> Path:
    return Path(os.environ.get("ORACLE_DB_PATH", "oracle.db"))


def _open_store() -> AttestationStore:
    return AttestationStore(_db_path())


def _serialize_datapoint(dp: NormalizedDatapoint) -> dict:
    return dp.model_dump(mode="json")


def _serialize_attestation(att: OracleAttestation) -> dict:
    return att.model_dump(mode="json")


def _attestation_envelope(att: OracleAttestation) -> dict:
    return {
        "attestation": _serialize_attestation(att),
        "datapoints": [_serialize_datapoint(dp) for dp in att.datapoints],
    }


@router.get("/attestations/latest")
def attestations_latest():
    store = _open_store()
    latest = store.get_latest_attestation()
    if latest is None:
        return JSONResponse(
            status_code=503,
            content={
                "code": "NO_ATTESTATION_YET",
                "message": "Oracle store contains no attestations.",
            },
        )
    return _attestation_envelope(latest)


@router.get("/attestations")
def attestations_list(limit: int = Query(10, ge=1, le=100)):
    store = _open_store()
    all_attestations = store.get_all_attestations()
    all_attestations.sort(key=lambda a: a.sequence_number, reverse=True)
    return [_attestation_envelope(a) for a in all_attestations[:limit]]


@router.get("/triggers")
def triggers_list(
    limit: int = Query(10, ge=1, le=100),
    severity: Optional[str] = Query(None),
):
    if severity is not None:
        try:
            Severity(severity)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_SEVERITY",
                    "message": (
                        "severity must be one of: "
                        + ", ".join(s.value for s in Severity)
                    ),
                },
            )

    db_path = _db_path()
    _open_store()  # ensure schema exists

    sql = (
        "SELECT event_id, rule_id, rule_version, clause_ref, severity, "
        "contract_id, evaluated_at, as_of, attestation_ref, "
        "evidence_json, rules_version FROM trigger_events"
    )
    params: tuple = ()
    if severity is not None:
        sql += " WHERE severity = ?"
        params = (severity,)
    sql += " ORDER BY evaluated_at DESC LIMIT ?"
    params = params + (limit,)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [
        {
            "event_id": event_id,
            "rule_id": rule_id,
            "rule_version": rule_version,
            "clause_ref": clause_ref,
            "severity": sev,
            "contract_id": contract_id,
            "evaluated_at": evaluated_at,
            "as_of": as_of,
            "attestation_ref": attestation_ref,
            "evidence": json.loads(evidence_json),
            "rules_version": rules_version,
        }
        for (
            event_id, rule_id, rule_version, clause_ref, sev,
            contract_id, evaluated_at, as_of, attestation_ref,
            evidence_json, rules_version,
        ) in rows
    ]


@router.get("/chain/verify")
def chain_verify():
    store = _open_store()
    attestations = store.get_all_attestations()
    ok, err = store.verify_integrity()
    return {
        "ok": ok,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "attestations_count": len(attestations),
        "error": err,
    }


@router.get("/health")
def health():
    db_path = _db_path()
    db_reachable = True
    last_attestation_at: Optional[str] = None
    failures_24h = 0
    triggers_24h = 0
    error: Optional[str] = None

    try:
        store = _open_store()
        latest = store.get_latest_attestation()
        if latest is not None:
            last_attestation_at = latest.signed_at.isoformat()

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        conn = sqlite3.connect(str(db_path))
        try:
            (failures_24h,) = conn.execute(
                "SELECT COUNT(*) FROM source_failures WHERE attempted_at >= ?",
                (cutoff,),
            ).fetchone()
            (triggers_24h,) = conn.execute(
                "SELECT COUNT(*) FROM trigger_events WHERE evaluated_at >= ?",
                (cutoff,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        db_reachable = False
        error = str(exc)

    return {
        "db_reachable": db_reachable,
        "db_path": str(db_path),
        "last_attestation_at": last_attestation_at,
        "source_failures_24h": failures_24h,
        "trigger_events_24h": triggers_24h,
        "error": error,
    }
