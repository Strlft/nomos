"""
=============================================================================
  DERIVAI — API BRIDGE  v0.3
  Connects the IRS Execution Engine to the Client + Advisor Portals

  Framework : FastAPI (with standalone fallback)
  Auth      : Role passed as query param (prototype — no real auth)

  ENDPOINTS:
  ──────────
  GET  /api/health                              → full module health check
  GET  /api/oracle/latest                       → current EURIBOR rate + status
  GET  /api/contracts                           → list all contracts (role-filtered)
  GET  /api/contracts/{id}                      → contract detail + periods
  POST /api/contracts                           → create contract (PENDING_SIGNATURE)
  POST /api/contracts/{id}/sign                 → client executes → ACTIVE
  POST /api/contracts/{id}/execute              → run calculation cycle
  POST /api/contracts/{id}/approve-pi/{period}  → approve Payment Instruction
  POST /api/contracts/{id}/notice               → generate §12 notice PDF
  POST /api/contracts/{id}/obligation/{s}/deliver → mark obligation delivered
  GET  /api/contracts/{id}/audit                → full audit trail
  GET  /api/contracts/{id}/compliance           → §3/§4 compliance summary

  DUE DILIGENCE (Schedule Part 3 / §4):
  ──────────────
  POST /api/documents/upload?contract_id={id}   → client uploads DD document
  POST /api/documents/{doc_id}/validate         → advisor validates/rejects
  GET  /api/contracts/{id}/due-diligence        → full DD status (RAG + gates)

  ORACLE v3 (market data + events + regulatory):
  ──────────────
  GET  /api/oracle/rates                        → cached readings for all 9 rates
  POST /api/oracle/rates/refresh                → force live ECB fetch (slow)
  GET  /api/oracle/events                       → market events (advisor only)
  GET  /api/oracle/regulatory                   → regulatory alerts by contract type

  CLIENT PROFILE:
  ──────────────
  GET  /api/client/profile                      → get current profile
  POST /api/client/profile                      → update profile (advisor_key required)

  HIERARCHY (§1(b) ISDA 2002):
  ──────────────────────────────
  Confirmation > Schedule > Master Agreement > Code > API Output
  This API is subordinate to all legal documents.

  ERROR FORMAT:
  ─────────────
  All errors return JSON: {"code": "ERROR_CODE", "message": "...", "isda_ref": "..."}
  HTTP 400 — validation / bad input
  HTTP 404 — contract / period not found
  HTTP 409 — conflict (duplicate ID, wrong state)
  HTTP 500 — unexpected internal error (logged with traceback)
=============================================================================
"""

# ── Imports ─────────────────────────────────────────────────────────────────
import sys
import os

# Ensure backend/ is on the path so sibling modules (engine, generate_*) resolve
# regardless of whether this file is run directly or via `uvicorn backend.api:app`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re
import json
import hashlib
import logging
import traceback
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict
from pathlib import Path

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# Structured logging — UTC timestamps, consistent format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("nomos.api")

# ── Optional FastAPI ─────────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, validator, Field
    HAS_FASTAPI = True
    logger.info("FastAPI loaded OK")
except ImportError:
    HAS_FASTAPI = False
    logger.warning("FastAPI not installed — standalone mode only")

# ── Engine imports (with graceful failure) ───────────────────────────────────
_MODULES_OK = False
_MODULE_ERROR: Optional[str] = None

try:
    from engine import (
        SwapParameters, PartyDetails, ScheduleElections, ContractInitiation,
        IRSExecutionEngine, OracleStatus, ContractState,
    )
    from generate_confirmation_pdf import generate_confirmation_pdf, generate_notice_pdf
    _MODULES_OK = True
    logger.info("Engine modules loaded OK")
except Exception as _e:
    _MODULE_ERROR = str(_e)
    logger.critical(f"Engine module load FAILED: {_e}\n{traceback.format_exc()}")

# Due diligence module — optional (graceful degradation if missing)
_DD_OK = False
try:
    from due_diligence import DocumentType, DocumentStatus, CovenantChecker
    _DD_OK = True
    logger.info("Due diligence module loaded OK")
except ImportError as _dd_e:
    logger.warning(f"Due diligence module not loaded: {_dd_e}")

# Oracle v3 — optional (used for all-rates / events / regulatory endpoints)
_ORACLE_V3_API_OK = False
try:
    from engine import get_oracle_v3
    from oracle_v3 import RateID, EventSeverity, RateStatus as OracleRateStatus
    _ORACLE_V3_API_OK = True
    logger.info("OracleV3 API access OK")
except ImportError as _ov3_e:
    logger.warning(f"OracleV3 not available for API: {_ov3_e}")

# ── In-memory client profile store ───────────────────────────────────────────
_client_profile: dict = {
    "company_name": "",
    "jurisdiction": "",
    "lei": "",
    "contact_email": "",
    "advisor_key": "",
}


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS — Validation sets
# ═══════════════════════════════════════════════════════════════════════════

# ISO 3166-1 alpha-2 codes covered by the netting opinion database
_VALID_JURISDICTIONS = {
    "GB", "FR", "DE", "IT", "ES", "NL", "BE", "AT", "CH", "SE", "NO", "DK",
    "FI", "PT", "IE", "LU", "PL", "CZ", "HU", "RO", "GR", "BG", "HR", "SI",
    "SK", "LT", "LV", "EE", "CY", "MT",
    "US", "CA", "MX", "BR", "AR",
    "JP", "CN", "IN", "KR", "AU", "SG", "HK", "MY", "ID", "TH",
    "ZA", "NG", "KE",
    "SA", "AE", "TR", "IL",
    "RU",
}

# Notice types supported by generate_confirmation_pdf.py
_VALID_NOTICE_TYPES = {
    "FAILURE_TO_PAY",
    "BREACH_OF_AGREEMENT",
    "ETD_DESIGNATION",
    "DELIVERY_REMINDER",
    "TAX_CHANGE",
}

# Required template variables per notice type
_NOTICE_REQUIRED_FIELDS: Dict[str, list] = {
    "FAILURE_TO_PAY":     ["party_defaulting", "currency", "amount", "due_date",
                           "grace_period", "grace_end"],
    "BREACH_OF_AGREEMENT":["party_defaulting", "obligation", "due_date", "section"],
    "ETD_DESIGNATION":    ["eod_notice_date", "eod_type", "etd_date", "currency"],
    "DELIVERY_REMINDER":  ["document", "due_date"],
    "TAX_CHANGE":         ["description", "effective_date"],
}

# Contract ID: 3-50 chars, alphanumeric + hyphens, must start with letter/digit
_CONTRACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{2,49}$")

# Max notional: EUR 2 billion (sanity cap for prototype)
_MAX_NOTIONAL = Decimal("2_000_000_000")

# ═══════════════════════════════════════════════════════════════════════════
# IN-MEMORY STORE
# ═══════════════════════════════════════════════════════════════════════════

_engines: Dict[str, "IRSExecutionEngine"] = {}
_schedules: Dict[str, "ScheduleElections"] = {}


# ═══════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_outputs() -> str:
    """Create outputs/ directory at the project root (nomos/outputs/) if it doesn't exist."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")
    os.makedirs(path, exist_ok=True)
    return path


def _get_engine(contract_id: str) -> "IRSExecutionEngine":
    """Look up an engine or raise KeyError (translated to 404 at the route layer)."""
    if contract_id not in _engines:
        raise KeyError(f"Contract '{contract_id}' not found")
    return _engines[contract_id]


def _http_error(status: int, code: str, message: str,
                isda_ref: str = "§1(b) ISDA 2002"):
    """Raise a FastAPI HTTPException with a structured JSON detail."""
    detail = {"code": code, "message": message, "isda_ref": isda_ref}
    if HAS_FASTAPI:
        raise HTTPException(status_code=status, detail=detail)
    # Standalone: raise ValueError so callers can catch it
    raise ValueError(f"[{status}] {code}: {message}")


def _validate_create(data: dict) -> list[str]:
    """
    Validate contract creation request.
    Returns a list of human-readable error strings (empty = valid).
    """
    errors = []

    # Contract ID
    cid = str(data.get("contract_id", "")).strip()
    if not cid:
        errors.append("contract_id is required")
    elif not _CONTRACT_ID_RE.match(cid):
        errors.append(
            "contract_id must be 3–50 characters, alphanumeric and hyphens only, "
            "starting with a letter or digit (e.g. SLC-IRS-EUR-001)"
        )
    elif cid in _engines:
        errors.append(f"contract_id '{cid}' already exists — use a unique ID")

    # Party names
    pa_name = str(data.get("party_a_name", "")).strip()
    pb_name = str(data.get("party_b_name", "")).strip()
    if not pa_name:
        errors.append("party_a_name is required")
    if not pb_name:
        errors.append("party_b_name is required")
    if pa_name and pb_name and pa_name.lower() == pb_name.lower():
        errors.append("party_a_name and party_b_name must be different entities")

    # Jurisdictions
    jA = str(data.get("party_a_jurisdiction", "")).upper()
    jB = str(data.get("party_b_jurisdiction", "")).upper()
    if jA and jA not in _VALID_JURISDICTIONS:
        errors.append(
            f"party_a_jurisdiction '{jA}' not recognised — "
            f"use ISO 3166-1 alpha-2 (e.g. GB, FR, DE, US)"
        )
    if jB and jB not in _VALID_JURISDICTIONS:
        errors.append(
            f"party_b_jurisdiction '{jB}' not recognised — "
            f"use ISO 3166-1 alpha-2 (e.g. GB, FR, DE, US)"
        )

    # Notional
    try:
        notional = Decimal(str(data.get("notional", 0)))
        if notional <= 0:
            errors.append("notional must be a positive number")
        elif notional > _MAX_NOTIONAL:
            errors.append(
                f"notional {notional:,.0f} exceeds prototype maximum "
                f"({_MAX_NOTIONAL:,.0f})"
            )
    except (InvalidOperation, TypeError, ValueError):
        errors.append("notional must be a valid number (e.g. 10000000)")

    # Fixed rate
    try:
        rate = Decimal(str(data.get("fixed_rate", 0)))
        if rate <= 0:
            errors.append("fixed_rate must be positive (e.g. 0.032 for 3.2%)")
        elif rate >= Decimal("0.50"):
            errors.append(
                "fixed_rate must be less than 50% (0.50) — "
                "did you pass a percentage instead of a decimal?"
            )
    except (InvalidOperation, TypeError, ValueError):
        errors.append(
            "fixed_rate must be a valid decimal (e.g. 0.032 for 3.2%, not 3.2)"
        )

    # Dates
    eff_date = term_date = None
    try:
        eff_date = date.fromisoformat(str(data.get("effective_date", "")))
    except (ValueError, TypeError):
        errors.append(
            "effective_date must be ISO format YYYY-MM-DD (e.g. 2026-03-15)"
        )
    try:
        term_date = date.fromisoformat(str(data.get("termination_date", "")))
    except (ValueError, TypeError):
        errors.append(
            "termination_date must be ISO format YYYY-MM-DD (e.g. 2028-03-15)"
        )
    if eff_date and term_date:
        delta = (term_date - eff_date).days
        if delta <= 0:
            errors.append("termination_date must be strictly after effective_date")
        elif delta < 30:
            errors.append(
                f"contract duration ({delta} days) is too short — "
                "minimum 30 days for a vanilla IRS"
            )
        elif delta > 365 * 51:
            errors.append(
                f"contract duration ({delta // 365} years) exceeds 50-year maximum"
            )

    return errors


# ═══════════════════════════════════════════════════════════════════════════
# CORE API FUNCTIONS (framework-agnostic — all return plain dicts/lists)
# ═══════════════════════════════════════════════════════════════════════════

def api_health() -> dict:
    """
    Full system health check.
    Tests: module imports, outputs directory, PDF library, oracle,
    and reports contract counts by state.
    """
    checks: Dict[str, str] = {}

    # Engine module
    checks["engine_module"] = "ok" if _MODULES_OK else f"FAILED — {_MODULE_ERROR}"

    # PDF library
    try:
        import reportlab
        checks["reportlab"] = f"ok (v{reportlab.Version})"
    except ImportError:
        checks["reportlab"] = "NOT_INSTALLED — PDF generation unavailable"

    # Outputs directory
    try:
        out = _ensure_outputs()
        checks["outputs_dir"] = f"ok — {out}"
    except Exception as e:
        checks["outputs_dir"] = f"ERROR — {e}"

    # Oracle (from first loaded contract — no network call)
    if _engines:
        try:
            sample_eng = next(iter(_engines.values()))
            oc_summary = sample_eng.oracle.oracle_summary()
            checks["oracle_last_status"] = oc_summary.get("status", "UNKNOWN")
            checks["oracle_source"] = oc_summary.get("source") or "none yet"
        except Exception as e:
            checks["oracle_last_status"] = f"error — {e}"
    else:
        checks["oracle_last_status"] = "no_contracts_loaded"

    # FastAPI availability
    checks["fastapi"] = "ok" if HAS_FASTAPI else "not_installed (standalone mode)"

    # Contract census
    by_state: Dict[str, int] = {}
    for eng in _engines.values():
        s = eng.state.value
        by_state[s] = by_state.get(s, 0) + 1

    overall = (
        "ok"
        if _MODULES_OK and checks["outputs_dir"].startswith("ok")
        else "degraded"
    )

    return {
        "status": overall,
        "engine_version": "v0.3",
        "isda_version": "ISDA 2002 Master Agreement",
        "checks": checks,
        "contracts": {
            "total": len(_engines),
            "by_state": by_state,
        },
        "schedules_loaded": len(_schedules),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "hierarchy": "§1(b) ISDA 2002 — Confirmation > Schedule > MA > Code > API",
    }


def api_oracle_latest(contract_id: str = None) -> dict:
    if not _MODULES_OK:
        return {"status": "MODULE_ERROR", "error": _MODULE_ERROR}
    if contract_id and contract_id in _engines:
        return _engines[contract_id].oracle.oracle_summary()
    # Return from first available engine (deterministic via insertion order)
    for eng in _engines.values():
        return eng.oracle.oracle_summary()
    return {"status": "NO_CONTRACTS_LOADED", "current_rate": None}


def api_list_contracts(role: str = "client") -> list:
    results = []
    for cid, eng in _engines.items():
        p = eng.params
        # Next pending payment instruction
        np_ = next(
            (per for per in eng.periods
             if not per.payment_confirmed and per.payment_instruction_issued),
            None
        )
        # Compliance: full summary for advisor, lightweight for client
        try:
            if role == "advisor":
                compliance_data = eng.compliance.compliance_summary(date.today())
            else:
                compliance_data = {
                    "overall": not any(
                        o.status == eng.compliance.ObligationStatus.OVERDUE
                        for o in eng.compliance.obligations
                    )
                }
        except Exception:
            compliance_data = {"overall": True}

        results.append({
            "id": cid,
            "type": "Vanilla IRS",
            "party_a": p.party_a.name,
            "party_b": p.party_b.name,
            "notional": float(p.notional),
            "currency": p.currency,
            "fixed_rate": float(p.fixed_rate),
            "effective_date": str(p.effective_date),
            "termination_date": str(p.termination_date),
            "governing_law": p.governing_law,
            "status": eng.state.value,
            "periods_total": len(eng.periods),
            "periods_calculated": sum(1 for per in eng.periods if per.fixed_amount),
            "next_payment": {
                "period": np_.period_number,
                "amount": float(np_.net_amount) if np_.net_amount else None,
                "date": str(np_.payment_date),
            } if np_ else None,
            "compliance": compliance_data,
            "netting_status": (
                eng.netting_assessment.overall_risk_level
                if eng.netting_assessment else "NOT_ASSESSED"
            ),
        })
    return results


def api_contract_detail(contract_id: str, role: str = "client") -> dict:
    eng = _get_engine(contract_id)
    p = eng.params
    detail: dict = {
        "id": contract_id,
        "party_a": {"name": p.party_a.name, "jurisdiction": p.party_a.jurisdiction_code},
        "party_b": {"name": p.party_b.name, "jurisdiction": p.party_b.jurisdiction_code},
        "notional": float(p.notional),
        "currency": p.currency,
        "fixed_rate": float(p.fixed_rate),
        "floating_index": p.floating_index,
        "effective_date": str(p.effective_date),
        "termination_date": str(p.termination_date),
        "governing_law": p.governing_law,
        "status": eng.state.value,
        "periods": [
            {
                "number": per.period_number,
                "start": str(per.start_date),
                "end": str(per.end_date),
                "payment_date": str(per.payment_date),
                "fixed_amount": float(per.fixed_amount) if per.fixed_amount else None,
                "floating_amount": float(per.floating_amount) if per.floating_amount else None,
                "net_amount": float(per.net_amount) if per.net_amount else None,
                "net_payer": per.net_payer.value if per.net_payer else None,
                "oracle_rate": float(per.oracle_reading.rate) if per.oracle_reading else None,
                "oracle_status": per.oracle_reading.status.value if per.oracle_reading else None,
                "payment_confirmed": per.payment_confirmed,
                "suspended": per.suspended,
                "fingerprint": per.calculation_fingerprint,
            }
            for per in eng.periods
        ],
    }
    if role == "advisor":
        detail["schedule"] = eng.schedule.to_dict() if eng.schedule else None
        detail["initiation"] = (
            {
                "initiated_by": eng.initiation.initiated_by,
                "initiated_date": str(eng.initiation.initiated_date),
                "status": eng.initiation.status,
            }
            if eng.initiation else None
        )
        detail["eods"] = [
            {
                "type": eod.eod_type.value,
                "party": eod.affected_party.value,
                "date": str(eod.detected_date),
                "grace_end": str(eod.grace_period_end) if eod.grace_period_end else None,
                "expired": eod.grace_period_expired,
                "reference": eod.isda_reference,
                "is_potential": eod.is_potential_eod,
            }
            for eod in eng.eod_monitor.active_eods
        ]
        detail["oracle_history"] = [
            {
                "rate": float(r.rate),
                "status": r.status.value,
                "source": r.source,
                "time": r.fetch_timestamp,
            }
            for r in eng.oracle.history[-10:]
        ]
    return detail


def api_compliance(contract_id: str) -> dict:
    """§3/§4 compliance snapshot for a contract."""
    eng = _get_engine(contract_id)
    return eng.compliance.compliance_summary(date.today())


def api_create_contract(data: dict) -> dict:
    """
    Create a new IRS contract.

    Steps:
    1. Validate all input fields
    2. Build engine (generates schedule, runs netting check)
    3. Set state → PENDING_SIGNATURE (awaits client execution)
    4. Generate Confirmation PDF with SHA-256 fingerprint
    5. Return contract summary
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE",
                    "Engine modules failed to load — check server logs")

    # Input validation
    errors = _validate_create(data)
    if errors:
        _http_error(
            400, "VALIDATION_ERROR",
            f"Contract creation failed: {'; '.join(errors)}",
            isda_ref="§1(b) ISDA 2002 — Confirmation must be complete and accurate"
        )

    cid = str(data["contract_id"]).strip()

    # Ensure outputs directory exists before PDF generation
    try:
        _ensure_outputs()
    except Exception as e:
        _http_error(500, "FILESYSTEM_ERROR",
                    f"Cannot create outputs directory: {e}")

    # Build SwapParameters
    try:
        params = SwapParameters(
            contract_id=cid,
            party_a=PartyDetails(
                str(data["party_a_name"]).strip(),
                str(data.get("party_a_short") or data["party_a_name"].split()[0]).strip(),
                "fixed_payer",
                jurisdiction_code=str(data.get("party_a_jurisdiction", "GB")).upper(),
            ),
            party_b=PartyDetails(
                str(data["party_b_name"]).strip(),
                str(data.get("party_b_short") or data["party_b_name"].split()[0]).strip(),
                "floating_payer",
                jurisdiction_code=str(data.get("party_b_jurisdiction", "FR")).upper(),
            ),
            notional=Decimal(str(data["notional"])),
            fixed_rate=Decimal(str(data["fixed_rate"])),
            effective_date=date.fromisoformat(str(data["effective_date"])),
            termination_date=date.fromisoformat(str(data["termination_date"])),
        )
    except Exception as e:
        logger.error(f"[{cid}] Parameter build failed: {e}")
        _http_error(400, "PARAMETER_ERROR", f"Could not build contract parameters: {e}")

    # Apply governing law from request if provided
    if "governing_law" in data:
        params.governing_law = str(data["governing_law"])

    # Schedule elections
    schedule = _schedules.get(str(data.get("schedule_ref", ""))) or ScheduleElections(
        governing_law=params.governing_law,
        mtpn_elected=bool(data.get("mtpn", True)),
        csa_elected=bool(data.get("csa", False)),
        csa_threshold_party_a=(
            Decimal(str(data["csa_threshold_a"])) if data.get("csa") and data.get("csa_threshold_a")
            else None
        ),
        csa_threshold_party_b=(
            Decimal(str(data["csa_threshold_b"])) if data.get("csa") and data.get("csa_threshold_b")
            else None
        ),
        csa_mta=(
            Decimal(str(data["csa_mta"])) if data.get("csa") and data.get("csa_mta")
            else None
        ),
    )

    initiation = ContractInitiation(
        initiated_by=str(data.get("initiated_by", "ADVISOR")),
        initiated_date=date.today(),
        schedule_ref=str(data.get("schedule_ref", "")),
        status="INITIATED",
    )

    # Initialise engine (generates schedule + netting opinion)
    try:
        engine = IRSExecutionEngine(params, schedule=schedule, initiation=initiation)
        engine.initialise()
    except Exception as e:
        logger.error(f"[{cid}] Engine initialisation failed: {e}\n{traceback.format_exc()}")
        _http_error(500, "ENGINE_ERROR", f"Engine initialisation failed: {e}")

    # Set PENDING_SIGNATURE — awaits client execution of the Confirmation
    engine.state = ContractState.PENDING_SIGNATURE
    initiation.status = "PENDING_SIGNATURE"

    # Register before PDF generation (so contract exists even if PDF fails)
    _engines[cid] = engine
    logger.info(f"[{cid}] Engine registered — state: PENDING_SIGNATURE")

    # Generate Confirmation PDF
    pdf_path = conf_hash = None
    pdf_error = None
    try:
        pdf_path, conf_hash = generate_confirmation_pdf(
            params, schedule=schedule, initiation=initiation,
            payment_schedule=engine.periods,
        )
        initiation.confirmation_hash = conf_hash
        engine.audit.log("CONFIRMATION_PDF_GENERATED", {
            "path": pdf_path,
            "hash": conf_hash,
        })
        logger.info(f"[{cid}] Confirmation PDF generated: {pdf_path}")
    except Exception as e:
        pdf_error = str(e)
        logger.error(f"[{cid}] PDF generation failed (contract still created): {e}")
        engine.audit.log("CONFIRMATION_PDF_ERROR", {"error": pdf_error})

    return {
        "contract_id": cid,
        "status": "PENDING_SIGNATURE",
        "periods": len(engine.periods),
        "confirmation_pdf": pdf_path,
        "confirmation_hash": conf_hash,
        "pdf_error": pdf_error,   # None on success; non-None if PDF failed
        "netting_status": (
            engine.netting_assessment.overall_risk_level
            if engine.netting_assessment else "NOT_ASSESSED"
        ),
        "governing_law": params.governing_law,
        "note": "Contract awaiting bilateral execution of the Confirmation",
    }


def api_sign_contract(contract_id: str, signed_by: str = "PARTY_B") -> dict:
    """
    Client executes the Confirmation — contract transitions to ACTIVE.

    §1(b) ISDA 2002: the Confirmation is binding upon execution.
    Only PENDING_SIGNATURE contracts can be signed.
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")

    eng = _get_engine(contract_id)

    if eng.state == ContractState.ACTIVE:
        _http_error(
            409, "ALREADY_ACTIVE",
            f"Contract '{contract_id}' is already ACTIVE",
            isda_ref="§1(b) ISDA 2002"
        )
    if eng.state != ContractState.PENDING_SIGNATURE:
        _http_error(
            409, "WRONG_STATE",
            f"Contract '{contract_id}' is in state {eng.state.value} "
            f"and cannot be signed",
            isda_ref="§1(b) ISDA 2002"
        )
    if not signed_by or not str(signed_by).strip():
        _http_error(400, "MISSING_SIGNATORY",
                    "signed_by must identify the executing party")

    # DD gate: block signing until all pre-signing documents are validated
    if _DD_OK and eng.dd_checker:
        readiness = eng.dd_checker.workflow.signing_readiness(eng.dd_checker.documents)
        if not readiness["ready"]:
            _http_error(
                409, "DD_INCOMPLETE",
                f"Contract '{contract_id}' cannot be signed: {readiness['message']}",
                isda_ref="§4 ISDA 2002 — Agreement to Deliver",
            )

    eng.state = ContractState.ACTIVE
    eng.initiation.signed_party_b = True
    eng.initiation.signed_party_b_date = date.today()
    eng.initiation.status = "SIGNED"

    eng.audit.log("CONTRACT_SIGNED", {
        "signed_by": str(signed_by).strip(),
        "party": "PARTY_B",
        "date": str(date.today()),
        "new_state": "ACTIVE",
        "note": "Confirmation executed — contract now ACTIVE per §1(b) ISDA 2002",
        "isda_reference": "§1(b) ISDA 2002 — Confirmation binding upon execution",
    }, actor=str(signed_by).strip())

    logger.info(f"[{contract_id}] CONTRACT_SIGNED by '{signed_by}' → ACTIVE")

    return {
        "contract_id": contract_id,
        "status": "ACTIVE",
        "signed_by": str(signed_by).strip(),
        "signed_date": str(date.today()),
        "isda_ref": "§1(b) ISDA 2002 — Confirmation prevails over Schedule and MA",
    }


def api_execute_period(contract_id: str, period: int = None) -> dict:
    """
    Run a calculation cycle for one period.
    Contract must be ACTIVE (not PENDING_SIGNATURE or SUSPENDED).
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")

    eng = _get_engine(contract_id)

    # Guard: must be ACTIVE
    if eng.state == ContractState.PENDING_SIGNATURE:
        _http_error(
            409, "NOT_ACTIVE",
            f"Contract '{contract_id}' is PENDING_SIGNATURE — "
            "client must sign the Confirmation before calculations can run",
            isda_ref="§1(b) ISDA 2002"
        )
    if eng.state == ContractState.SUSPENDED:
        _http_error(
            409, "SUSPENDED",
            f"Contract '{contract_id}' is SUSPENDED — "
            "§2(a)(iii) condition precedent: no payment while EoD active",
            isda_ref="§2(a)(iii) ISDA 2002"
        )
    if eng.state not in (ContractState.ACTIVE,):
        _http_error(
            409, "WRONG_STATE",
            f"Contract '{contract_id}' is {eng.state.value} — only ACTIVE contracts can be calculated"
        )
    if not eng.periods:
        _http_error(500, "NO_PERIODS",
                    "Contract has no calculation periods — engine may not be initialised")

    # Auto-select next uncalculated period if not specified
    if period is None:
        nxt = next((p.period_number for p in eng.periods if not p.fixed_amount), None)
        if nxt is None:
            return {"status": "ALL_PERIODS_CALCULATED",
                    "periods_total": len(eng.periods),
                    "message": "All periods have been calculated"}
        period = nxt

    # Validate period number
    if not isinstance(period, int) or period < 1 or period > len(eng.periods):
        _http_error(
            400, "INVALID_PERIOD",
            f"Period {period} is out of range — "
            f"contract has {len(eng.periods)} periods (1–{len(eng.periods)})"
        )

    # Run calculation
    try:
        result = eng.run_calculation_cycle(period)
    except Exception as e:
        logger.error(f"[{contract_id}] Period {period} calc error: {e}\n{traceback.format_exc()}")
        _http_error(500, "CALCULATION_ERROR", f"Calculation failed: {e}")

    if result is None:
        # Engine returned None → §2(a)(iii) suspension
        return {
            "status": "SUSPENDED",
            "period": period,
            "message": "§2(a)(iii): payment obligation suspended — EoD or PEoD active",
            "isda_ref": "§2(a)(iii) ISDA 2002",
            "eod_count": len(eng.eod_monitor.active_eods),
        }

    logger.info(f"[{contract_id}] Period {period} calculated — "
                f"net EUR {result.get('net_amount', 'N/A')}")
    return result


def api_approve_pi(contract_id: str, period_number: int, approver: str) -> dict:
    """
    Advisor approves a Payment Instruction.
    HUMAN GATE: no automatic payment — approval required per §2(a)(i).
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")
    if not approver or not str(approver).strip():
        _http_error(400, "MISSING_APPROVER",
                    "approver must identify the Calculation Agent",
                    isda_ref="§14 ISDA 2002 — Calculation Agent")

    eng = _get_engine(contract_id)

    # Validate period bounds
    if not isinstance(period_number, int) or period_number < 1:
        _http_error(400, "INVALID_PERIOD",
                    f"period_number must be a positive integer, got: {period_number}")
    if period_number > len(eng.periods):
        _http_error(
            400, "INVALID_PERIOD",
            f"Period {period_number} does not exist — "
            f"contract has {len(eng.periods)} periods",
            isda_ref="§2(a)(i) ISDA 2002"
        )

    per = eng.periods[period_number - 1]

    if not per.payment_instruction_issued:
        _http_error(
            409, "NO_PI_ISSUED",
            f"No Payment Instruction has been issued for period {period_number} — "
            "run /execute first",
            isda_ref="§2(a)(i) ISDA 2002"
        )
    if per.payment_confirmed:
        _http_error(
            409, "PI_ALREADY_APPROVED",
            f"Period {period_number} Payment Instruction was already approved",
            isda_ref="§2(a)(i) ISDA 2002"
        )
    if per.suspended:
        _http_error(
            409, "PERIOD_SUSPENDED",
            f"Period {period_number} is suspended under §2(a)(iii) — "
            "cannot approve while EoD is active",
            isda_ref="§2(a)(iii) ISDA 2002"
        )

    per.payment_confirmed = True
    approval_hash = hashlib.sha256(
        f"{contract_id}:{period_number}:{approver}:{per.net_amount}:{date.today()}".encode()
    ).hexdigest()

    eng.audit.log("PI_APPROVED", {
        "period": period_number,
        "net_amount": str(per.net_amount),
        "net_payer": per.net_payer.value if per.net_payer else None,
        "approved_by": str(approver).strip(),
        "approval_hash": approval_hash[:16],
        "isda_reference": "§2(a)(i) ISDA 2002 — Payment obligation",
    }, actor=str(approver).strip())

    logger.info(f"[{contract_id}] PI approved — period {period_number}, "
                f"EUR {per.net_amount}, by '{approver}'")

    return {
        "status": "APPROVED",
        "contract_id": contract_id,
        "period": period_number,
        "amount": float(per.net_amount),
        "net_payer": per.net_payer.value if per.net_payer else None,
        "approved_by": str(approver).strip(),
        "approval_hash": approval_hash[:16],
        "isda_ref": "§2(a)(i) ISDA 2002",
    }


def api_generate_notice(contract_id: str, notice_type: str, details: dict) -> dict:
    """
    Generate a §12 ISDA 2002 notice PDF.
    Validates notice type and required template fields before generation.
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")

    # Validate notice type
    if notice_type not in _VALID_NOTICE_TYPES:
        _http_error(
            400, "INVALID_NOTICE_TYPE",
            f"Notice type '{notice_type}' is not supported. "
            f"Valid types: {', '.join(sorted(_VALID_NOTICE_TYPES))}",
            isda_ref="§12 ISDA 2002"
        )

    # Validate required fields
    required = _NOTICE_REQUIRED_FIELDS.get(notice_type, [])
    missing = [f for f in required if f not in details or not details[f]]
    if missing:
        _http_error(
            400, "MISSING_NOTICE_FIELDS",
            f"Notice type '{notice_type}' requires these fields: "
            f"{', '.join(missing)}",
            isda_ref="§12 ISDA 2002"
        )

    eng = _get_engine(contract_id)
    p = eng.params

    try:
        _ensure_outputs()
        pdf_path, notice_hash = generate_notice_pdf(
            notice_type=notice_type,
            from_party=p.party_a.name,
            to_party=p.party_b.name,
            contract_id=contract_id,
            details=details,
            governing_law=p.governing_law,
        )
    except KeyError as e:
        _http_error(
            400, "TEMPLATE_VARIABLE_MISSING",
            f"Notice template is missing a required variable: {e}. "
            f"Check the 'details' payload for notice type '{notice_type}'.",
            isda_ref="§12 ISDA 2002"
        )
    except Exception as e:
        logger.error(f"[{contract_id}] Notice PDF failed: {e}\n{traceback.format_exc()}")
        _http_error(500, "PDF_GENERATION_ERROR", f"Notice PDF generation failed: {e}",
                    isda_ref="§12 ISDA 2002")

    eng.audit.log("NOTICE_GENERATED", {
        "type": notice_type,
        "hash": notice_hash,
        "pdf": pdf_path,
        "isda_reference": "§12 ISDA 2002",
    }, actor="ADVISOR")

    logger.info(f"[{contract_id}] Notice generated — type: {notice_type}, hash: {notice_hash[:16]}")

    return {
        "status": "GENERATED",
        "type": notice_type,
        "pdf": pdf_path,
        "hash": notice_hash,
        "isda_ref": "§12 ISDA 2002",
    }


def api_mark_delivered(contract_id: str, section: str, party: str) -> dict:
    """Mark a §4 obligation as delivered."""
    if not section or not str(section).strip():
        _http_error(400, "MISSING_SECTION", "section is required (e.g. §4(a)(ii))")
    if not party or str(party).upper() not in ("PARTY_A", "PARTY_B"):
        _http_error(400, "INVALID_PARTY",
                    "party must be 'PARTY_A' or 'PARTY_B'",
                    isda_ref="§4(a) ISDA 2002")

    eng = _get_engine(contract_id)
    ob = eng.compliance.mark_delivered(
        str(section).strip(), str(party).upper(), date.today()
    )
    if ob is None:
        _http_error(
            404, "OBLIGATION_NOT_FOUND",
            f"No outstanding obligation found for section '{section}' / party '{party}'. "
            "It may already be DELIVERED or may not exist.",
            isda_ref="§4(a) ISDA 2002"
        )

    eng.audit.log("OBLIGATION_DELIVERED", {
        "section": str(section).strip(),
        "party": str(party).upper(),
        "date": str(date.today()),
        "isda_reference": "§4(a) ISDA 2002 — Agreement to Deliver",
    }, actor="ADVISOR")

    return {
        "status": "DELIVERED",
        "section": str(section).strip(),
        "party": str(party).upper(),
        "delivered_date": str(date.today()),
        "isda_ref": "§4(a) ISDA 2002",
    }


def api_upload_document(contract_id: str, data: dict) -> dict:
    """
    Client uploads a due diligence document.

    Required fields: doc_id, filename, uploaded_by
    Optional:  file_hash (hex), file_content_b64 (base64 of file for server-side
               hash computation and structured data extraction)

    Steps:
    1. Find the DocumentRecord (created at initialise() with status=REQUIRED).
    2. Set status → UPLOADED; compute hash if content provided.
    3. Run AUTO checks: deadline, expiry, financial ratios, cert cross-ref.
    4. Raise HUMAN GATE flags where required.
    5. Log to audit trail.

    Returns document record + list of alerts for the advisor portal.
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")
    if not _DD_OK:
        _http_error(503, "DD_UNAVAILABLE",
                    "Due diligence module not available — check server logs")

    doc_id = str(data.get("doc_id", "")).strip()
    filename = str(data.get("filename", "")).strip()
    uploaded_by = str(data.get("uploaded_by", "")).strip()

    if not doc_id:
        _http_error(400, "MISSING_DOC_ID", "doc_id is required")
    if not filename:
        _http_error(400, "MISSING_FILENAME", "filename is required")
    if not uploaded_by:
        _http_error(400, "MISSING_UPLOADER", "uploaded_by is required")

    eng = _get_engine(contract_id)
    if not eng.dd_checker:
        _http_error(503, "DD_NOT_INITIALISED",
                    "Due diligence checker not initialised for this contract")

    try:
        rec, alerts = eng.dd_checker.upload_document(
            doc_id=doc_id,
            filename=filename,
            uploaded_by=uploaded_by,
            file_hash=data.get("file_hash"),
            file_content_b64=data.get("file_content_b64"),
            today=date.today(),
        )
    except KeyError as e:
        _http_error(404, "DOCUMENT_NOT_FOUND", str(e), isda_ref="§4 ISDA 2002")
    except ValueError as e:
        _http_error(409, "UPLOAD_CONFLICT", str(e), isda_ref="§4 ISDA 2002")

    eng.audit.log("DOCUMENT_UPLOADED", {
        "doc_id": rec.doc_id,
        "doc_type": rec.doc_type.value,
        "party": rec.party,
        "filename": rec.filename,
        "file_hash": rec.file_hash,
        "uploaded_by": uploaded_by,
        "alerts": alerts,
        "human_gates_raised": rec.requires_human_review,
        "isda_reference": f"{rec.linked_obligation} ISDA 2002",
    }, actor=uploaded_by)

    logger.info(
        f"[{contract_id}] DOC UPLOADED: {doc_id} by '{uploaded_by}' "
        f"({'HUMAN GATE' if rec.requires_human_review else 'auto-ok'})"
    )

    return {
        "status": "UPLOADED",
        "document": rec.to_dict(),
        "alerts": alerts,
        "requires_advisor_review": rec.requires_human_review,
        "advisor_action": (
            rec.human_gate_reason if rec.requires_human_review
            else "No immediate action — document queued for validation."
        ),
        "isda_ref": f"{rec.linked_obligation} ISDA 2002",
    }


def api_validate_document(doc_id: str, data: dict) -> dict:
    """
    Advisor validates (or rejects) an uploaded document.

    HUMAN GATE — must be called explicitly by an advisor.

    Required fields: contract_id, advisor
    Optional: notes (str), accepted (bool, default True)

    When accepted=True:
      - Document status → VALIDATED
      - Corresponding §4 obligation marked DELIVERED in ComplianceMonitor
      - All HUMAN GATE flags for this doc_id resolved

    When accepted=False:
      - Document status → REJECTED
      - Client must re-upload
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")
    if not _DD_OK:
        _http_error(503, "DD_UNAVAILABLE",
                    "Due diligence module not available")

    contract_id = str(data.get("contract_id", "")).strip()
    advisor = str(data.get("advisor", "")).strip()
    notes = str(data.get("notes", "")).strip()
    accepted = bool(data.get("accepted", True))

    if not contract_id:
        _http_error(400, "MISSING_CONTRACT_ID", "contract_id is required")
    if not advisor:
        _http_error(400, "MISSING_ADVISOR",
                    "advisor must identify the validating Calculation Agent",
                    isda_ref="§14 ISDA 2002")

    eng = _get_engine(contract_id)
    if not eng.dd_checker:
        _http_error(503, "DD_NOT_INITIALISED",
                    "Due diligence checker not initialised for this contract")

    try:
        rec = eng.dd_checker.validate_document(
            doc_id=doc_id,
            advisor=advisor,
            notes=notes,
            accepted=accepted,
            today=date.today(),
        )
    except KeyError as e:
        _http_error(404, "DOCUMENT_NOT_FOUND", str(e), isda_ref="§4 ISDA 2002")
    except ValueError as e:
        _http_error(409, "VALIDATION_CONFLICT", str(e), isda_ref="§4 ISDA 2002")

    action = "VALIDATED" if accepted else "REJECTED"
    eng.audit.log(f"DOCUMENT_{action}", {
        "doc_id": rec.doc_id,
        "doc_type": rec.doc_type.value,
        "party": rec.party,
        "advisor": advisor,
        "notes": notes,
        "obligation_satisfied": accepted,
        "isda_reference": f"{rec.linked_obligation} ISDA 2002",
    }, actor=advisor)

    logger.info(
        f"[{contract_id}] DOC {action}: {doc_id} by '{advisor}'"
    )

    return {
        "status": action,
        "document": rec.to_dict(),
        "obligation_satisfied": accepted,
        "isda_ref": f"{rec.linked_obligation} ISDA 2002",
        "note": (
            "§4 obligation marked DELIVERED in ComplianceMonitor."
            if accepted
            else "Document rejected — client must re-upload."
        ),
    }


def api_due_diligence(contract_id: str) -> dict:
    """
    Full due diligence status for a contract.

    Returns:
      - RAG status (GREEN / AMBER / RED)
      - All documents grouped by status
      - Overdue / expiring soon lists
      - Pending and resolved HUMAN GATE flags
      - Auto vs human breakdown
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")
    if not _DD_OK:
        _http_error(503, "DD_UNAVAILABLE",
                    "Due diligence module not available")

    eng = _get_engine(contract_id)
    if not eng.dd_checker:
        _http_error(503, "DD_NOT_INITIALISED",
                    "Due diligence checker not initialised for this contract")

    return eng.dd_checker.due_diligence_summary(date.today())


def api_signing_readiness(contract_id: str) -> dict:
    """
    Return the signing gate status for a contract.
    Checks whether all pre-signing DD documents are VALIDATED and the
    DDWorkflow has reached READY_TO_SIGN before the sign endpoint is called.

    Used by both the sign endpoint (server-side gate) and the client portal
    sign button (client-side readiness check).
    """
    if not _MODULES_OK:
        _http_error(503, "MODULE_UNAVAILABLE", "Engine modules unavailable")

    eng = _get_engine(contract_id)

    if not _DD_OK or not eng.dd_checker:
        # Graceful degradation: if DD module is absent, don't block signing
        return {
            "ready": True,
            "workflow_state": "DD_NOT_INITIALISED",
            "pre_signing_total": 0,
            "pre_signing_validated": 0,
            "blocking_count": 0,
            "blocking_documents": [],
            "missing": [],
            "message": "DD checker not initialised — signing not blocked.",
        }

    return eng.dd_checker.workflow.signing_readiness(eng.dd_checker.documents)


def api_upload_dd(contract_id: str, data: dict) -> dict:
    """
    Contract-scoped upload wrapper.
    POST /api/contracts/{id}/due-diligence/upload
    Maps DDUploadRequest fields to the existing api_upload_document logic.
    """
    mapped = {
        "doc_id":           data.get("doc_id", ""),
        "filename":         data.get("filename", ""),
        "uploaded_by":      data.get("uploaded_by", ""),
        "file_hash":        data.get("file_hash"),
        "file_content_b64": data.get("file_content_b64"),
    }
    return api_upload_document(contract_id, mapped)


def api_validate_dd_doc(contract_id: str, doc_id: str, data: dict) -> dict:
    """
    Contract-scoped validate wrapper.
    POST /api/contracts/{id}/due-diligence/{doc_id}/validate
    Injects contract_id and doc_id from the path into the existing validate logic.
    """
    enriched = dict(data)
    enriched["contract_id"] = contract_id
    return api_validate_document(doc_id, enriched)


# ─── Oracle v3 endpoints ──────────────────────────────────────────────────────

def api_oracle_all_rates() -> dict:
    """
    Return latest cached reading for all 9 rates in RateRegistry.
    Uses `registry.latest()` (no live fetch) — safe for 10-second polling.
    If a rate has never been fetched, returns its static fallback value.
    """
    if not _ORACLE_V3_API_OK:
        return {"status": "UNAVAILABLE", "rates": {}, "message": "OracleV3 module not loaded"}

    oracle = get_oracle_v3()
    if oracle is None:
        return {"status": "UNAVAILABLE", "rates": {}, "message": "OracleV3 singleton unavailable"}

    rates_out: dict = {}
    for rid in RateID:
        cached = oracle.registry.latest(rid)
        if cached:
            rates_out[rid.value] = cached.as_dict()
        else:
            # Never fetched — return static fallback
            from oracle_v3 import _STATIC_FALLBACKS
            fb = _STATIC_FALLBACKS.get(rid, None)
            rates_out[rid.value] = {
                "rate_id":         rid.value,
                "rate":            str(fb) if fb is not None else "0",
                "status":          "STATIC_FALLBACK",
                "source":          "STATIC_FALLBACK",
                "fetch_timestamp": None,
                "publication_date": None,
            }

    return {
        "status":    "ok",
        "rates":     rates_out,
        "rate_count": len(rates_out),
        "as_of":     _utcnow_iso(),
    }


def api_oracle_fetch_rates() -> dict:
    """
    Force a live fetch for all 9 rates from ECB (slow — only call on explicit Refresh).
    Returns fresh RateReading objects after network calls.
    """
    if not _ORACLE_V3_API_OK:
        return {"status": "UNAVAILABLE", "rates": {}, "message": "OracleV3 module not loaded"}

    oracle = get_oracle_v3()
    if oracle is None:
        return {"status": "UNAVAILABLE", "rates": {}, "message": "OracleV3 singleton unavailable"}

    readings = oracle.registry.fetch_many(list(RateID))
    rates_out = {rid.value: r.as_dict() for rid, r in readings.items()}

    return {
        "status":    "ok",
        "rates":     rates_out,
        "rate_count": len(rates_out),
        "as_of":     _utcnow_iso(),
    }


def api_oracle_events(contract_id: Optional[str] = None, min_severity: str = "LOW") -> dict:
    """
    Return stored market events from EventMonitor.
    Advisor-only endpoint.  Optionally filter by contract_id and min severity.
    """
    if not _ORACLE_V3_API_OK:
        return {"status": "UNAVAILABLE", "events": [], "message": "OracleV3 module not loaded"}

    oracle = get_oracle_v3()
    if oracle is None:
        return {"status": "UNAVAILABLE", "events": [], "message": "OracleV3 singleton unavailable"}

    sev_map = {
        "LOW":    EventSeverity.LOW,
        "MEDIUM": EventSeverity.MEDIUM,
        "HIGH":   EventSeverity.HIGH,
    }
    sev = sev_map.get(min_severity.upper(), EventSeverity.LOW)

    events = oracle.get_events(contract_id=contract_id, min_severity=sev, since_hours=48)
    return {
        "status":      "ok",
        "event_count": len(events),
        "events":      [e.as_dict() for e in events],
        "as_of":       _utcnow_iso(),
    }


def api_oracle_regulatory(contract_type: str = "IRS", jurisdiction: str = "") -> dict:
    """
    Return pre-loaded regulatory alerts from RegulatoryWatch.
    Filtered by contract_type ('IRS' default) and optional jurisdiction.
    """
    if not _ORACLE_V3_API_OK:
        return {"status": "UNAVAILABLE", "alerts": [], "message": "OracleV3 module not loaded"}

    oracle = get_oracle_v3()
    if oracle is None:
        return {"status": "UNAVAILABLE", "alerts": [], "message": "OracleV3 singleton unavailable"}

    alerts = oracle.get_regulatory_alerts(
        contract_type=contract_type,
        jurisdiction=jurisdiction,
    )

    def _alert_dict(a) -> dict:
        return {
            "alert_id":               a.alert_id,
            "regulation_name":        a.regulation_name,
            "jurisdiction":           a.jurisdiction,
            "impact_description":     a.impact_description,
            "affected_contract_types": a.affected_contract_types,
            "effective_date":         a.effective_date,
            "source_url":             a.source_url,
            "severity":               a.severity.value,
        }

    return {
        "status":      "ok",
        "alert_count": len(alerts),
        "alerts":      [_alert_dict(a) for a in alerts],
        "as_of":       _utcnow_iso(),
    }


# ─── Client profile endpoints ─────────────────────────────────────────────────

def api_get_client_profile() -> dict:
    """Return the current in-memory client profile."""
    return {"status": "ok", "profile": dict(_client_profile)}


def api_set_client_profile(data: dict) -> dict:
    """
    Save client profile fields.  Validates advisor_key is non-empty
    so anonymous profile updates are blocked.
    """
    global _client_profile

    advisor_key = (data.get("advisor_key") or "").strip()
    if not advisor_key:
        _http_error(403, "ADVISOR_KEY_REQUIRED",
                    "An advisor_key is required to update the client profile")

    allowed = {"company_name", "jurisdiction", "lei", "contact_email", "advisor_key"}
    updated = {k: str(v).strip() for k, v in data.items() if k in allowed}
    _client_profile.update(updated)

    return {"status": "ok", "profile": dict(_client_profile)}


# ─── Helper: UTC ISO timestamp ────────────────────────────────────────────────

def _utcnow_iso() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).isoformat()


def api_audit_trail(contract_id: str) -> list:
    """Return a copy of the full audit trail (append-only chain)."""
    eng = _get_engine(contract_id)
    # Return a shallow copy — never expose the live list directly
    return list(eng.audit._entries)


# ═══════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════════════

if HAS_FASTAPI:

    # ── Pydantic Models ──────────────────────────────────────────────────────

    class NewContractRequest(BaseModel):
        contract_id: str
        party_a_name: str
        party_a_short: str = ""
        party_a_jurisdiction: str = "GB"
        party_b_name: str
        party_b_short: str = ""
        party_b_jurisdiction: str = "FR"
        notional: float
        fixed_rate: float
        effective_date: str
        termination_date: str
        schedule_ref: str = ""
        initiated_by: str = "ADVISOR"
        governing_law: str = "English Law"
        mtpn: bool = True
        aet: bool = False
        csa: bool = False
        csa_threshold_a: Optional[float] = None
        csa_threshold_b: Optional[float] = None
        csa_mta: Optional[float] = None

    class NoticeRequest(BaseModel):
        notice_type: str
        details: dict

    class EoDRequest(BaseModel):
        eod_type: str
        defaulting_party: str
        description: str

    class SignRequest(BaseModel):
        signed_by: str = "PARTY_B"

    class DocumentUploadRequest(BaseModel):
        doc_id: str
        filename: str
        uploaded_by: str
        file_hash: Optional[str] = None          # SHA-256 hex, client-computed
        file_content_b64: Optional[str] = None   # base64 content (for server hash + ratio extraction)

    class DocumentValidateRequest(BaseModel):
        contract_id: str
        advisor: str
        notes: str = ""
        accepted: bool = True

    class DDUploadRequest(BaseModel):
        """Body for POST /api/contracts/{id}/due-diligence/upload"""
        doc_id: str
        document_type: str = ""
        filename: str
        uploaded_by: str
        file_hash: Optional[str] = None
        file_content_b64: Optional[str] = None

    class DDValidateRequest(BaseModel):
        """Body for POST /api/contracts/{id}/due-diligence/{doc_id}/validate"""
        advisor: str
        notes: str = ""
        accepted: bool = True

    # ── App ──────────────────────────────────────────────────────────────────

    app = FastAPI(
        title="DerivAI API",
        description="Smart Legal Contract Engine — ISDA 2002 Master Agreement",
        version="0.3",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — permissive for local development
    # In production: restrict allow_origins to the specific portal domains
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],          # TODO: restrict in production
        allow_credentials=False,      # Cannot be True with allow_origins=["*"]
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "X-Request-ID"],
        expose_headers=["X-Contract-ID", "X-Audit-Hash"],
        max_age=600,                  # Cache preflight 10 minutes
    )

    # ── Frontend static files ─────────────────────────────────────────────────
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def serve_login():
        return FileResponse(str(_FRONTEND_DIR / "login.html"))

    @app.get("/client", include_in_schema=False)
    def serve_client():
        return FileResponse(str(_FRONTEND_DIR / "client_portal.html"))

    @app.get("/advisor", include_in_schema=False)
    def serve_advisor():
        return FileResponse(str(_FRONTEND_DIR / "advisor_portal.html"))

    # ── Global exception handler — catches anything not caught by routes ──────

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(
            f"Unhandled exception on {request.method} {request.url.path}: "
            f"{exc}\n{traceback.format_exc()}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred. Check server logs.",
                "isda_ref": "§1(b) ISDA 2002",
            },
        )

    # ── Routes ───────────────────────────────────────────────────────────────

    @app.get("/api/health", tags=["System"])
    def health():
        return api_health()

    @app.get("/api/oracle/latest", tags=["Oracle"])
    def oracle_latest(contract_id: Optional[str] = None):
        try:
            return api_oracle_latest(contract_id)
        except Exception as e:
            logger.error(f"Oracle fetch error: {e}")
            raise HTTPException(500, detail={
                "code": "ORACLE_ERROR", "message": str(e)})

    @app.get("/api/contracts", tags=["Contracts"])
    def list_contracts(role: str = "client"):
        try:
            return api_list_contracts(role)
        except Exception as e:
            logger.error(f"list_contracts error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.get("/api/contracts/{contract_id}", tags=["Contracts"])
    def contract_detail(contract_id: str, role: str = "client"):
        try:
            return api_contract_detail(contract_id, role)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except Exception as e:
            logger.error(f"[{contract_id}] detail error: {e}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.get("/api/contracts/{contract_id}/compliance", tags=["Contracts"])
    def contract_compliance(contract_id: str):
        try:
            return api_compliance(contract_id)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except Exception as e:
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts", tags=["Contracts"])
    def create_contract(req: NewContractRequest):
        try:
            return api_create_contract(req.dict())
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"create_contract error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts/{contract_id}/sign", tags=["Contracts"])
    def sign_contract(contract_id: str, signed_by: str = "PARTY_B"):
        try:
            return api_sign_contract(contract_id, signed_by)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{contract_id}] sign error: {e}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts/{contract_id}/execute", tags=["Contracts"])
    def execute_period(contract_id: str, period: Optional[int] = None):
        try:
            return api_execute_period(contract_id, period)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{contract_id}] execute error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts/{contract_id}/approve-pi/{period}", tags=["Contracts"])
    def approve_pi(contract_id: str, period: int, approver: str = "Advisor"):
        try:
            return api_approve_pi(contract_id, period, approver)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{contract_id}] approve-pi error: {e}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts/{contract_id}/notice", tags=["Contracts"])
    def generate_notice(contract_id: str, req: NoticeRequest):
        try:
            return api_generate_notice(contract_id, req.notice_type, req.details)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{contract_id}] notice error: {e}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts/{contract_id}/obligation/{section}/deliver",
              tags=["Contracts"])
    def mark_delivered(contract_id: str, section: str, party: str):
        try:
            return api_mark_delivered(contract_id, section, party)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.get("/api/contracts/{contract_id}/audit", tags=["Contracts"])
    def audit_trail(contract_id: str):
        try:
            return api_audit_trail(contract_id)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except Exception as e:
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    # ── Due Diligence endpoints ───────────────────────────────────────────────

    @app.get("/api/contracts/{contract_id}/due-diligence", tags=["Due Diligence"])
    def due_diligence(contract_id: str):
        """Full DD status: RAG, documents, human gates, auto-checks."""
        try:
            return api_due_diligence(contract_id)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{contract_id}] DD summary error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/documents/upload", tags=["Due Diligence"])
    def upload_document(contract_id: str, req: DocumentUploadRequest):
        """
        Client uploads a due diligence document.
        Pass contract_id as a query parameter.
        """
        try:
            return api_upload_document(contract_id, req.dict())
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{contract_id}] doc upload error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/documents/{doc_id}/validate", tags=["Due Diligence"])
    def validate_document(doc_id: str, req: DocumentValidateRequest):
        """
        Advisor validates or rejects an uploaded document.
        HUMAN GATE — explicit advisor action required.
        """
        try:
            return api_validate_document(doc_id, req.dict())
        except KeyError as e:
            raise HTTPException(404, detail={
                "code": "NOT_FOUND",
                "message": str(e)})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{doc_id}] doc validate error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.get("/api/contracts/{contract_id}/signing-readiness",
             tags=["Due Diligence"])
    def signing_readiness(contract_id: str):
        """
        Check whether all pre-signing DD documents are validated and
        the DDWorkflow has reached READY_TO_SIGN.
        Returns {ready, workflow_state, blocking_count, missing, message}.
        """
        try:
            return api_signing_readiness(contract_id)
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[{contract_id}] signing-readiness error: {e}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts/{contract_id}/due-diligence/upload",
              tags=["Due Diligence"])
    def dd_upload(contract_id: str, req: DDUploadRequest):
        """
        Client uploads a DD document using the contract-scoped URL.
        Alternative to /api/documents/upload?contract_id=... (RESTful path-based form).
        """
        try:
            return api_upload_dd(contract_id, req.dict())
        except KeyError:
            raise HTTPException(404, detail={
                "code": "CONTRACT_NOT_FOUND",
                "message": f"Contract '{contract_id}' not found"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"[{contract_id}] dd upload error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    @app.post("/api/contracts/{contract_id}/due-diligence/{doc_id}/validate",
              tags=["Due Diligence"])
    def dd_validate(contract_id: str, doc_id: str, req: DDValidateRequest):
        """
        Advisor validates or rejects a DD document via the contract-scoped URL.
        HUMAN GATE — explicit advisor action required.
        Advances the DDWorkflow state if conditions are met.
        """
        try:
            return api_validate_dd_doc(contract_id, doc_id, req.dict())
        except KeyError as e:
            raise HTTPException(404, detail={
                "code": "NOT_FOUND", "message": str(e)})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"[{contract_id}/{doc_id}] dd validate error: "
                f"{e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={
                "code": "INTERNAL_ERROR", "message": str(e)})

    # ── Oracle v3 routes ────────────────────────────────────────────────────

    @app.get("/api/oracle/rates", tags=["Oracle"])
    def oracle_all_rates():
        """All 9 cached rate readings (no live fetch — safe for 10s polling)."""
        return api_oracle_all_rates()

    @app.post("/api/oracle/rates/refresh", tags=["Oracle"])
    def oracle_refresh_rates():
        """Force a live ECB fetch for all 9 rates (slow — call on Refresh button only)."""
        try:
            return api_oracle_fetch_rates()
        except Exception as e:
            logger.error(f"oracle refresh error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={"code": "INTERNAL_ERROR", "message": str(e)})

    @app.get("/api/oracle/events", tags=["Oracle"])
    def oracle_events(
        contract_id: Optional[str] = None,
        min_severity: str = "LOW",
    ):
        """Stored market events from EventMonitor. Advisor-only."""
        return api_oracle_events(contract_id=contract_id, min_severity=min_severity)

    @app.get("/api/oracle/regulatory", tags=["Oracle"])
    def oracle_regulatory(
        contract_type: str = "IRS",
        jurisdiction:  str = "",
    ):
        """Pre-loaded regulatory alerts from RegulatoryWatch."""
        return api_oracle_regulatory(contract_type=contract_type, jurisdiction=jurisdiction)

    # ── Client profile routes ───────────────────────────────────────────────

    class ClientProfileRequest(BaseModel):
        company_name:  str = ""
        jurisdiction:  str = ""
        lei:           str = ""
        contact_email: str = ""
        advisor_key:   str = ""

    @app.get("/api/client/profile", tags=["Client"])
    def get_client_profile():
        """Return current client profile."""
        return api_get_client_profile()

    @app.post("/api/client/profile", tags=["Client"])
    def set_client_profile(req: ClientProfileRequest):
        """Save client profile. Requires advisor_key."""
        try:
            return api_set_client_profile(req.dict())
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"client profile error: {e}\n{traceback.format_exc()}")
            raise HTTPException(500, detail={"code": "INTERNAL_ERROR", "message": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# STANDALONE TEST — full flow: create → sign → execute → approve → notice
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # Default: start the web server.  Pass --test to run the internal test suite instead.
    if "--test" not in sys.argv:
        if not HAS_FASTAPI:
            print("ERROR: FastAPI/uvicorn not installed. Run: pip install fastapi uvicorn")
            sys.exit(1)
        import uvicorn
        # Change working directory to project root so relative paths (outputs/, etc.) resolve correctly
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        print("DerivAI API starting on http://localhost:8000")
        print("  Login    → http://localhost:8000/")
        print("  Client   → http://localhost:8000/client")
        print("  Advisor  → http://localhost:8000/advisor")
        print("  API docs → http://localhost:8000/docs")
        uvicorn.run(app, host="0.0.0.0", port=8000)
        sys.exit(0)

    PASS = "✓"
    FAIL = "✗"
    WARN = "⚠"

    results: list[tuple[str, bool, str]] = []   # (name, passed, detail)

    def check(name: str, passed: bool, detail: str = ""):
        mark = PASS if passed else FAIL
        print(f"  {mark}  {name}" + (f"  —  {detail}" if detail else ""))
        results.append((name, passed, detail))

    def section(title: str):
        print(f"\n  {'─'*56}")
        print(f"  {title}")
        print(f"  {'─'*56}")

    print("\n" + "═" * 60)
    print("  DerivAI API — Full Flow Test")
    print("  create → sign → execute → approve PI → notice")
    print("═" * 60)

    # ── 0. Health (before any contracts) ────────────────────────────────────
    section("0. Health check")
    h = api_health()
    check("modules ok", h["checks"]["engine_module"] == "ok",
          h["checks"]["engine_module"])
    check("reportlab available", "NOT_INSTALLED" not in h["checks"].get("reportlab",""),
          h["checks"].get("reportlab",""))
    check("outputs dir ok", h["checks"].get("outputs_dir","").startswith("ok"),
          h["checks"].get("outputs_dir",""))
    check("overall status", h["status"] == "ok", h["status"])

    # ── 1. Input validation ──────────────────────────────────────────────────
    section("1. Input validation")

    bad_cases = [
        ("empty contract_id",   {"contract_id": "", "party_a_name": "A", "party_b_name": "B",
                                  "notional": 1e7, "fixed_rate": 0.032,
                                  "effective_date": "2026-04-01", "termination_date": "2028-04-01"}),
        ("duplicate id (pre)",  None),   # Filled after first create
        ("negative notional",   {"contract_id": "SLC-TEST-BAD-001", "party_a_name": "A", "party_b_name": "B",
                                  "notional": -1, "fixed_rate": 0.032,
                                  "effective_date": "2026-04-01", "termination_date": "2028-04-01"}),
        ("rate as pct (32)",    {"contract_id": "SLC-TEST-BAD-002", "party_a_name": "A", "party_b_name": "B",
                                  "notional": 1e7, "fixed_rate": 32,
                                  "effective_date": "2026-04-01", "termination_date": "2028-04-01"}),
        ("term before eff",     {"contract_id": "SLC-TEST-BAD-003", "party_a_name": "A", "party_b_name": "B",
                                  "notional": 1e7, "fixed_rate": 0.032,
                                  "effective_date": "2028-04-01", "termination_date": "2026-04-01"}),
        ("same party names",    {"contract_id": "SLC-TEST-BAD-004", "party_a_name": "Same", "party_b_name": "Same",
                                  "notional": 1e7, "fixed_rate": 0.032,
                                  "effective_date": "2026-04-01", "termination_date": "2028-04-01"}),
    ]

    for label, bad in bad_cases:
        if bad is None:
            continue
        errs = _validate_create(bad)
        check(f"rejects: {label}", len(errs) > 0, errs[0] if errs else "NO ERROR — BUG")

    # ── 2. Create contract ───────────────────────────────────────────────────
    section("2. Create contract")
    CID = "SLC-IRS-EUR-TEST-001"
    create_ok = False
    conf_hash = None
    try:
        r = api_create_contract({
            "contract_id": CID,
            "party_a_name": "Alpha Corp S.A.", "party_a_short": "Alpha",
            "party_a_jurisdiction": "GB",
            "party_b_name": "Beta Fund Ltd",   "party_b_short": "Beta",
            "party_b_jurisdiction": "FR",
            "notional": 10_000_000, "fixed_rate": 0.032,
            "effective_date": "2026-03-15", "termination_date": "2028-03-15",
            "initiated_by": "ADVISOR",
        })
        create_ok = True
        conf_hash = r.get("confirmation_hash")
        check("contract created",       r.get("status") == "PENDING_SIGNATURE",
              r.get("status"))
        check("periods generated",      (r.get("periods") or 0) > 0,
              f"{r.get('periods')} periods")
        check("confirmation hash set",  bool(conf_hash),
              (conf_hash or "")[:16] + "…")
        check("netting assessed",       r.get("netting_status") not in (None, ""),
              r.get("netting_status"))
        check("pdf generated",          r.get("pdf_error") is None,
              r.get("pdf_error") or "ok")
    except Exception as e:
        check("contract created", False, str(e))

    # Duplicate ID rejection (needs the contract to exist first)
    if create_ok:
        dup_errs = _validate_create({
            "contract_id": CID, "party_a_name": "X", "party_b_name": "Y",
            "notional": 1e7, "fixed_rate": 0.032,
            "effective_date": "2026-04-01", "termination_date": "2028-04-01"
        })
        check("rejects: duplicate contract_id", any("already exists" in e for e in dup_errs),
              dup_errs[0] if dup_errs else "NO ERROR — BUG")

    # ── 3. Execute before signing (must be blocked) ──────────────────────────
    section("3. Execute before signing (should be blocked)")
    if create_ok:
        try:
            api_execute_period(CID, 1)
            check("execute blocked pre-sign", False, "Execution was NOT blocked — bug")
        except (ValueError, Exception) as e:
            # Should fail with NOT_ACTIVE / WRONG_STATE
            blocked = "PENDING_SIGNATURE" in str(e) or "NOT_ACTIVE" in str(e) or "409" in str(e)
            check("execute blocked pre-sign", blocked, str(e)[:80])

    # ── 4. Sign contract ─────────────────────────────────────────────────────
    section("4. Sign contract (client executes Confirmation)")
    sign_ok = False
    if create_ok:
        try:
            s = api_sign_contract(CID, "Beta Fund Ltd (Compliance Team)")
            sign_ok = s.get("status") == "ACTIVE"
            check("status → ACTIVE",   sign_ok, s.get("status"))
            check("signed_by recorded", bool(s.get("signed_by")), s.get("signed_by",""))
        except Exception as e:
            check("sign contract", False, str(e))

        # Double-sign should be rejected
        if sign_ok:
            try:
                api_sign_contract(CID, "Someone")
                check("double-sign rejected", False, "Was NOT rejected — bug")
            except (ValueError, Exception) as e:
                check("double-sign rejected",
                      "ALREADY_ACTIVE" in str(e) or "409" in str(e), str(e)[:60])

    # ── 5. Execute period 1 ──────────────────────────────────────────────────
    # Stub: pre-seed period 1 with synthetic oracle data so no ECB network call is made.
    section("5. Execute period 1 (oracle stubbed — no network)")
    exec_ok = False
    if sign_ok:
        try:
            from engine import OracleReading, OracleStatus
            from decimal import Decimal as _D
            eng_ref = _engines[CID]
            p1 = eng_ref.periods[0]
            p1.oracle_reading = OracleReading(
                rate=_D("0.02850"),
                status=OracleStatus.FALLBACK,
                source="TEST_STUB",
                fetch_timestamp="2026-01-01T00:00:00Z",
            )
            calc = api_execute_period(CID, 1)
            exec_ok = bool(calc and "net_amount" in calc)
            check("calculation returned", exec_ok, str(calc.get("net_amount", "?")))
            if exec_ok:
                rate = calc.get("euribor") or calc.get("oracle_rate")
                src  = calc.get("oracle_status") or calc.get("oracle_source")
                check("oracle rate present", rate is not None, f"{rate} [{src}]")
                check("fingerprint set",
                      bool(calc.get("calculation_fingerprint") or calc.get("fingerprint")),
                      (calc.get("calculation_fingerprint") or calc.get("fingerprint",""))[:16])
        except Exception as e:
            check("execute period 1", False, str(e))

    # ── 6. Execute period 1 again (already calculated) ───────────────────────
    section("6. Re-execute period 1 (idempotency note)")
    if exec_ok:
        try:
            calc2 = api_execute_period(CID, 1)
            check("re-execute does not crash", True,
                  "Note: no idempotency guard — period re-calculated")
        except Exception as e:
            check("re-execute does not crash", False, str(e))

    # ── 7. Approve PI ────────────────────────────────────────────────────────
    section("7. Approve Payment Instruction")
    approve_ok = False
    if exec_ok:
        try:
            ap = api_approve_pi(CID, 1, "J. Smith (Linklaters)")
            approve_ok = ap.get("status") == "APPROVED"
            check("PI approved",       approve_ok, ap.get("status"))
            check("amount returned",   ap.get("amount") is not None,
                  f"EUR {ap.get('amount'):,.2f}" if ap.get("amount") else "—")
            check("approval hash set", bool(ap.get("approval_hash")),
                  ap.get("approval_hash",""))
        except Exception as e:
            check("approve PI", False, str(e))

        # Double-approve should fail
        if approve_ok:
            try:
                api_approve_pi(CID, 1, "J. Smith (Linklaters)")
                check("double-approve rejected", False, "Was NOT rejected — bug")
            except (ValueError, Exception) as e:
                check("double-approve rejected",
                      "ALREADY_APPROVED" in str(e) or "409" in str(e), str(e)[:60])

        # Invalid period number
        try:
            api_approve_pi(CID, 999, "Advisor")
            check("bad period rejected", False, "Was NOT rejected — bug")
        except (ValueError, Exception) as e:
            check("bad period rejected",
                  "INVALID_PERIOD" in str(e) or "400" in str(e) or "999" in str(e),
                  str(e)[:60])

    # ── 8. Generate §12 notice ───────────────────────────────────────────────
    section("8. Generate §12 notice (DELIVERY_REMINDER)")
    if create_ok:
        # Test invalid notice type
        try:
            api_generate_notice(CID, "MADE_UP_TYPE", {})
            check("invalid notice type rejected", False, "Was NOT rejected — bug")
        except (ValueError, Exception) as e:
            check("invalid notice type rejected",
                  "INVALID_NOTICE_TYPE" in str(e) or "400" in str(e), str(e)[:60])

        # Test missing required fields
        try:
            api_generate_notice(CID, "DELIVERY_REMINDER", {})
            check("missing fields rejected", False, "Was NOT rejected — bug")
        except (ValueError, Exception) as e:
            check("missing fields rejected",
                  "MISSING_NOTICE_FIELDS" in str(e) or "400" in str(e), str(e)[:60])

        # Valid notice
        try:
            n = api_generate_notice(CID, "DELIVERY_REMINDER", {
                "document": "Annual audited financial statements FY2025",
                "due_date": "30 April 2026",
            })
            check("notice generated",    n.get("status") == "GENERATED", n.get("status"))
            check("notice hash present", bool(n.get("hash")), (n.get("hash",""))[:16])
            check("notice pdf present",  bool(n.get("pdf")), n.get("pdf",""))
        except Exception as e:
            check("generate notice", False, str(e))

    # ── 9. Oracle ────────────────────────────────────────────────────────────
    # Uses cached oracle reading injected in step 5 — no live ECB call.
    section("9. Oracle summary (uses stubbed data from step 5)")
    if create_ok:
        try:
            oc = api_oracle_latest(CID)
            check("oracle summary returned",     "status" in oc, str(oc.get("status","—")))
            check("oracle rate or fallback",
                  oc.get("status") in ("CONFIRMED", "FALLBACK", "CHALLENGED", "NO_CONTRACTS_LOADED"),
                  oc.get("status","—"))
            check("no ERROR status",
                  oc.get("status") != "ERROR",
                  "Oracle fallback handled gracefully")
        except Exception as e:
            check("oracle summary", False, str(e))

    # ── 10. Audit trail ──────────────────────────────────────────────────────
    section("10. Audit trail integrity")
    if create_ok:
        try:
            trail = api_audit_trail(CID)
            check("audit trail non-empty",  len(trail) > 0,
                  f"{len(trail)} entries")
            check("chain starts at GENESIS",
                  trail[0].get("prev_hash") == "GENESIS",
                  trail[0].get("prev_hash",""))
            check("all entries have hash",
                  all("entry_hash" in e for e in trail), "")
            check("sequential seq numbers",
                  all(trail[i]["seq"] == i+1 for i in range(len(trail))), "")

            # Verify CONTRACT_SIGNED is in trail (if signed)
            event_types = [e["event_type"] for e in trail]
            if sign_ok:
                check("CONTRACT_SIGNED logged",
                      "CONTRACT_SIGNED" in event_types, "")
            if approve_ok:
                check("PI_APPROVED logged",
                      "PI_APPROVED" in event_types, "")
        except Exception as e:
            check("audit trail", False, str(e))

    # ── 11. Health (with contracts loaded) ──────────────────────────────────
    section("11. Health check (post-test)")
    h2 = api_health()
    check("total contracts",    h2["contracts"]["total"] >= 1,
          str(h2["contracts"]["total"]))
    check("ACTIVE contracts",   h2["contracts"]["by_state"].get("ACTIVE", 0) >= (1 if sign_ok else 0),
          str(h2["contracts"]["by_state"]))

    # ── Summary ──────────────────────────────────────────────────────────────
    total   = len(results)
    passed  = sum(1 for _, p, _ in results if p)
    failed  = total - passed

    print(f"\n{'═'*60}")
    print(f"  Results: {passed}/{total} passed  |  {failed} failed")
    if failed:
        print(f"\n  Failed checks:")
        for name, ok, detail in results:
            if not ok:
                print(f"    {FAIL}  {name}  —  {detail}")
    print(f"{'═'*60}\n")

    if HAS_FASTAPI:
        print("  FastAPI ready. Run: uvicorn api:app --reload --port 8000")
        print("  Docs:            http://localhost:8000/docs\n")
    else:
        print("  Install FastAPI:  pip install fastapi uvicorn\n")

    sys.exit(0 if failed == 0 else 1)
