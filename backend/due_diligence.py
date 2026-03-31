"""
=============================================================================
  NOMOS — DUE DILIGENCE & COVENANT MONITORING MODULE
  CovenantChecker v1.0

  WHAT THIS MODULE DOES
  ──────────────────────
  Per Schedule Part 3 (Agreement to Deliver) and §4 ISDA 2002, parties to
  a vanilla IRS must provide:
    • Annual audited financial statements          §4(a)(ii)
    • Compliance certificate (with each set of accounts) §4(a)(ii)
    • Tax forms — W-8BEN / W-8BEN-E               §4(a)(i)
    • Board resolutions                            §4(b)
    • Legal opinion                                §4(a)(ii)
    • KYC package: passport, proof of address, UBO §AML/KYC (market practice)

  Each document follows the lifecycle:
    NOT_REQUIRED → REQUIRED → UPLOADED → VALIDATED
                                       → REJECTED
                           → EXPIRED (any stage)

  AUTO checks (no human needed):
    – Upload deadline tracking (§4(a) due dates)
    – KYC / tax form expiry monitoring
    – Financial ratio extraction from structured JSON/CSV upload
    – Cross-reference: compliance cert date vs financial statement date

  HUMAN GATE (advisor must review):
    – Is the legal opinion adequate? (substance — only a lawyer can judge)
    – Are financial statements qualified or unqualified? (auditor's opinion)
    – Financial covenant breach? (leverage ratio, net worth test)
    – MAC clause triggered? (judgment call — §5(b) / bespoke Schedule)
    – §3(d) representation materially inaccurate?

  INTEGRATION
  ───────────
  CovenantChecker is held by IRSExecutionEngine alongside ComplianceMonitor.
  When a document is VALIDATED, CovenantChecker calls back into ComplianceMonitor
  to mark the corresponding §4 obligation as DELIVERED.

  LEGAL NOTICE
  ─────────────
  Prototype for academic / demonstration purposes only.
  Not legal or financial advice. Human review always required.
=============================================================================
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import base64
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# ENUMERATIONS
# ─────────────────────────────────────────────────────────────────────────────

class DocumentStatus(Enum):
    """Lifecycle status of a due diligence document."""
    NOT_REQUIRED = "NOT_REQUIRED"   # Inapplicable to this contract configuration
    REQUIRED     = "REQUIRED"       # Needed, not yet uploaded
    UPLOADED     = "UPLOADED"       # Client uploaded — awaiting advisor validation
    VALIDATED    = "VALIDATED"      # Advisor confirmed document is adequate
    REJECTED     = "REJECTED"       # Advisor rejected — re-submission needed
    EXPIRED      = "EXPIRED"        # Expiry date passed — re-upload required


class DocumentType(Enum):
    """
    Document types required under Schedule Part 3 / §4 ISDA 2002
    and applicable AML/KYC regulation.
    """
    ANNUAL_FINANCIAL_STATEMENTS = "ANNUAL_FINANCIAL_STATEMENTS"
    # §4(a)(ii) — Annual audited accounts, due ~120 days after FY end.
    # Expiry: 12 months (re-upload when new accounts filed).

    COMPLIANCE_CERTIFICATE = "COMPLIANCE_CERTIFICATE"
    # §4(a)(ii) — Officer certificate confirming no EoD / no breach.
    # Delivered with each set of annual accounts. No standalone expiry.

    TAX_FORM = "TAX_FORM"
    # §4(a)(i) — W-8BEN (individuals) or W-8BEN-E (entities); FATCA.
    # Expiry: 3 years from date of signature per IRS rules.

    BOARD_RESOLUTION = "BOARD_RESOLUTION"
    # §4(b) — Resolution authorising execution; delivered upon execution.
    # No expiry — one-time document.

    LEGAL_OPINION = "LEGAL_OPINION"
    # §4(a)(ii) — Opinion on enforceability of the MA + Confirmation.
    # HUMAN GATE: must be reviewed by advisor / counsel.
    # No expiry unless jurisdiction law changes materially.

    KYC_PASSPORT = "KYC_PASSPORT"
    # AML/KYC — Certified copy of passport or government ID.
    # Expiry: tied to document expiry date; market practice re-verify 3 years.

    KYC_ADDRESS = "KYC_ADDRESS"
    # AML/KYC — Proof of registered address (utility bill / bank statement).
    # Expiry: 3 months (must be recent).

    KYC_UBO = "KYC_UBO"
    # AML/KYC — Ultimate Beneficial Owner declaration.
    # Expiry: annually (re-certify each year).

    CERTIFICATE_OF_INCORPORATION = "CERTIFICATE_OF_INCORPORATION"
    # §4(b) — Certificate of incorporation / good standing for entities.
    # Delivered upon execution. No expiry — one-time document.


class HumanGateType(Enum):
    """Categories of issues that require advisor judgment."""
    LEGAL_OPINION_REVIEW     = "LEGAL_OPINION_REVIEW"
    AUDITOR_OPINION_CHECK    = "AUDITOR_OPINION_CHECK"    # Qualified vs unqualified
    COVENANT_BREACH          = "COVENANT_BREACH"           # Financial ratio test
    MAC_CLAUSE               = "MAC_CLAUSE"                # Material Adverse Change
    REPRESENTATION_ACCURACY  = "REPRESENTATION_ACCURACY"   # §3(d) adequacy


# ─────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DocumentRecord:
    """
    Tracks one due diligence document through its full lifecycle.

    Instances are created by CovenantChecker.initialise_required_documents()
    (status=REQUIRED) and updated by upload_document() / validate_document().
    """
    doc_id: str                          # DD-{contract_id}-{seq:04d}
    doc_type: DocumentType
    contract_id: str
    party: str                           # "PARTY_A" or "PARTY_B"
    status: DocumentStatus
    linked_obligation: str               # §4(a)(i), §4(a)(ii), §4(b), etc.
    description: str                     # e.g. "Annual Financial Statements FY2026"

    # Upload details (populated by upload_document)
    upload_date: Optional[date] = None
    filename: Optional[str] = None
    file_hash: Optional[str] = None      # SHA-256 hex of file content
    uploaded_by: Optional[str] = None

    # Validation details — HUMAN GATE
    validation_date: Optional[date] = None
    validated_by: Optional[str] = None
    validation_notes: Optional[str] = None

    # Expiry
    expiry_date: Optional[date] = None   # None = no expiry

    # Auto-checks
    auto_check_passed: Optional[bool] = None
    auto_check_detail: str = ""

    # Human gate flags raised by auto-checks or upload processing
    requires_human_review: bool = False
    human_gate_reason: str = ""          # Surfaced in advisor portal

    # Structured financial data (if §3(d) / covenant monitoring)
    financial_ratios: Optional[Dict[str, float]] = None
    covenant_breaches: List[str] = field(default_factory=list)

    # Internal: obligation due date (set by CovenantChecker._require, not serialised).
    # Named with a trailing underscore to avoid Python name-mangling for dunder names.
    # Used by upload_document() and due_diligence_summary() to check lateness.
    due_date_obligation: Optional[date] = None

    # True = must be validated before the contract can be signed (Phase 1).
    # False = ongoing post-signing obligation (Phase 2).
    is_pre_signing: bool = False

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type.value,
            "contract_id": self.contract_id,
            "party": self.party,
            "status": self.status.value,
            "linked_obligation": self.linked_obligation,
            "description": self.description,
            "due_date_obligation": str(self.due_date_obligation) if self.due_date_obligation else None,
            "upload_date": str(self.upload_date) if self.upload_date else None,
            "filename": self.filename,
            "file_hash": self.file_hash,
            "uploaded_by": self.uploaded_by,
            "validation_date": str(self.validation_date) if self.validation_date else None,
            "validated_by": self.validated_by,
            "validation_notes": self.validation_notes,
            "expiry_date": str(self.expiry_date) if self.expiry_date else None,
            "auto_check_passed": self.auto_check_passed,
            "auto_check_detail": self.auto_check_detail,
            "requires_human_review": self.requires_human_review,
            "human_gate_reason": self.human_gate_reason,
            "financial_ratios": self.financial_ratios,
            "covenant_breaches": self.covenant_breaches,
            "is_pre_signing": self.is_pre_signing,
        }


@dataclass
class HumanGateFlag:
    """
    An issue raised that requires explicit advisor judgment.
    Cannot be resolved by the engine alone.
    """
    gate_type: HumanGateType
    doc_id: Optional[str]
    contract_id: str
    party: str
    raised_date: date
    description: str
    isda_reference: str
    resolved: bool = False
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "gate_type": self.gate_type.value,
            "doc_id": self.doc_id,
            "contract_id": self.contract_id,
            "party": self.party,
            "raised_date": str(self.raised_date),
            "description": self.description,
            "isda_reference": self.isda_reference,
            "resolved": self.resolved,
            "resolved_by": self.resolved_by,
            "resolution_notes": self.resolution_notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# COVENANT THRESHOLDS — override via SwapParameters (future)
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_COVENANT_THRESHOLDS = {
    "leverage_ratio":      {"max": 4.0,  "label": "Max leverage ratio 4.0x"},
    "net_worth":           {"min": 0.0,  "label": "Min net worth > 0"},
    "current_ratio":       {"min": 1.0,  "label": "Min current ratio 1.0x"},
    "interest_coverage":   {"min": 1.5,  "label": "Min interest coverage 1.5x"},
    "debt_to_equity":      {"max": 3.0,  "label": "Max debt/equity 3.0x"},
}

# KYC / tax form expiry periods (days from upload/signing date)
_EXPIRY_DAYS = {
    DocumentType.KYC_PASSPORT: 3 * 365,    # 3-year KYC refresh cycle
    DocumentType.KYC_ADDRESS:  90,          # 3 months — must be recent
    DocumentType.KYC_UBO:      365,         # Annual re-certification
    DocumentType.TAX_FORM:     3 * 365,     # W-8BEN valid 3 years
    DocumentType.ANNUAL_FINANCIAL_STATEMENTS: 365,  # Superseded by next year's
}

# Which document types require a HUMAN GATE on upload (always)
_ALWAYS_HUMAN_GATE = {
    DocumentType.LEGAL_OPINION: (
        HumanGateType.LEGAL_OPINION_REVIEW,
        "Legal opinion substance review required — "
        "is the opinion unqualified and covers enforceability of the MA, "
        "Confirmation, and close-out netting in the relevant jurisdiction?",
        "§4(a)(ii) ISDA 2002"
    ),
    DocumentType.ANNUAL_FINANCIAL_STATEMENTS: (
        HumanGateType.AUDITOR_OPINION_CHECK,
        "Auditor opinion review required — "
        "are the financial statements unqualified? Any going concern language? "
        "Any material restatements? Potential §5(a)(ii) breach if material.",
        "§3(d) ISDA 2002 — Accuracy of Specified Information"
    ),
}

# Which types satisfy which §4 obligation
_OBLIGATION_MAP = {
    DocumentType.ANNUAL_FINANCIAL_STATEMENTS: "§4(a)(ii)",
    DocumentType.COMPLIANCE_CERTIFICATE:      "§4(a)(ii)",
    DocumentType.TAX_FORM:                    "§4(a)(i)",
    DocumentType.BOARD_RESOLUTION:            "§4(b)",
    DocumentType.LEGAL_OPINION:               "§4(a)(ii)",
    DocumentType.KYC_PASSPORT:                "§4(a)(ii)",
    DocumentType.KYC_ADDRESS:                 "§4(a)(ii)",
    DocumentType.KYC_UBO:                     "§4(a)(ii)",
    DocumentType.CERTIFICATE_OF_INCORPORATION: "§4(b)",
}


# ─────────────────────────────────────────────────────────────────────────────
# DD WORKFLOW STATE MACHINE
# ─────────────────────────────────────────────────────────────────────────────

class DDWorkflowState(Enum):
    """
    States of the pre-signing due diligence workflow.
    The contract CANNOT move to ACTIVE until READY_TO_SIGN is reached.
    """
    INITIATED      = "INITIATED"       # Workflow created
    KYC_PENDING    = "KYC_PENDING"     # Awaiting KYC documents
    KYC_VALIDATED  = "KYC_VALIDATED"   # KYC docs validated — legal/financial docs next
    DOCS_PENDING   = "DOCS_PENDING"    # Awaiting legal capacity + financial docs
    DOCS_VALIDATED = "DOCS_VALIDATED"  # All pre-signing docs validated
    READY_TO_SIGN  = "READY_TO_SIGN"   # Contract may be executed


_KYC_TYPES: frozenset = frozenset({
    DocumentType.KYC_PASSPORT,
    DocumentType.KYC_ADDRESS,
    DocumentType.KYC_UBO,
})

_LEGAL_FINANCIAL_TYPES: frozenset = frozenset({
    DocumentType.BOARD_RESOLUTION,
    DocumentType.CERTIFICATE_OF_INCORPORATION,
    DocumentType.LEGAL_OPINION,
    DocumentType.TAX_FORM,
    DocumentType.ANNUAL_FINANCIAL_STATEMENTS,
})


class DDWorkflow:
    """
    Finite-state machine that gates contract execution on DD completion.

    State sequence:
        INITIATED → KYC_PENDING → KYC_VALIDATED → DOCS_PENDING
                  → DOCS_VALIDATED → READY_TO_SIGN

    advance() is called by CovenantChecker.validate_document() and
    upload_document() after every state-changing event.
    """

    def __init__(self, contract_id: str):
        self.contract_id = contract_id
        self.state = DDWorkflowState.INITIATED
        self._history: List[Tuple[str, date, str]] = []
        self._transition(DDWorkflowState.KYC_PENDING,
                         "Workflow initialised — awaiting KYC documents")

    def _transition(self, new_state: DDWorkflowState, reason: str,
                    today: Optional[date] = None) -> None:
        today = today or date.today()
        if new_state != self.state:
            self._history.append((self.state.value, today, reason))
            self.state = new_state
            print(f"  [DD-WF] {self.contract_id}: → {new_state.value} — {reason}")

    def advance(self, documents: List["DocumentRecord"],
                today: Optional[date] = None) -> "DDWorkflowState":
        """
        Re-evaluate workflow state from the full document set and advance if
        conditions are met. Called after every upload or validation event.
        Returns the current (possibly updated) state.
        """
        today = today or date.today()

        pre        = [d for d in documents if d.is_pre_signing]
        kyc_docs   = [d for d in pre if d.doc_type in _KYC_TYPES]
        legal_docs = [d for d in pre if d.doc_type in _LEGAL_FINANCIAL_TYPES]

        kyc_done   = bool(kyc_docs) and all(
            d.status == DocumentStatus.VALIDATED for d in kyc_docs)
        legal_done = bool(legal_docs) and all(
            d.status == DocumentStatus.VALIDATED for d in legal_docs)

        if self.state == DDWorkflowState.KYC_PENDING and kyc_done:
            self._transition(DDWorkflowState.KYC_VALIDATED,
                             "All KYC documents validated", today)
            self._transition(DDWorkflowState.DOCS_PENDING,
                             "Proceeding to legal and financial document review", today)

        if self.state == DDWorkflowState.DOCS_PENDING and legal_done:
            self._transition(DDWorkflowState.DOCS_VALIDATED,
                             "All legal/financial documents validated", today)
            self._transition(DDWorkflowState.READY_TO_SIGN,
                             "All pre-signing due diligence complete", today)

        return self.state

    def signing_readiness(self, documents: List["DocumentRecord"]) -> dict:
        """
        Return signing gate status: is the contract ready to execute?
        Lists all blocking pre-signing documents not yet VALIDATED.
        """
        pre      = [d for d in documents if d.is_pre_signing]
        blocking = [d for d in pre if d.status != DocumentStatus.VALIDATED]
        ready    = (self.state == DDWorkflowState.READY_TO_SIGN)

        n = len(blocking)
        msg = (
            "All pre-signing documents validated — contract may be executed."
            if ready else
            f"{n} pre-signing document{'s' if n != 1 else ''} require validation before signing."
        )
        return {
            "ready":               ready,
            "workflow_state":      self.state.value,
            "pre_signing_total":   len(pre),
            "pre_signing_validated": len(pre) - len(blocking),
            "blocking_count":      n,
            "blocking_documents":  [d.to_dict() for d in blocking],
            "missing": [
                {"doc_id": d.doc_id, "doc_type": d.doc_type.value,
                 "description": d.description, "status": d.status.value,
                 "party": d.party}
                for d in blocking
            ],
            "message": msg,
        }

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "history": [
                {"from_state": s, "on_date": str(d), "reason": r}
                for s, d, r in self._history
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# CovenantChecker
# ─────────────────────────────────────────────────────────────────────────────

class CovenantChecker:
    """
    Due diligence and covenant monitoring for a single ISDA 2002 IRS contract.

    Lifecycle
    ─────────
    1. IRSExecutionEngine.initialise() calls initialise_required_documents()
       → creates DocumentRecord stubs with status=REQUIRED for all IRS docs.
    2. Client uploads via POST /api/documents/upload
       → upload_document() sets status=UPLOADED, runs auto-checks.
    3. Advisor reviews and validates via POST /api/documents/{id}/validate
       → validate_document() sets status=VALIDATED, calls back into
         ComplianceMonitor to mark the §4 obligation as DELIVERED.
    4. Engine checks expirations at each calculation cycle.
    """

    def __init__(self, contract_id: str, params):
        """
        Args:
            contract_id: The contract's unique ID.
            params: SwapParameters (for party names, dates, jurisdiction).
        """
        self.contract_id = contract_id
        self.params = params
        self.documents: List[DocumentRecord] = []
        self.human_gates: List[HumanGateFlag] = []
        self._seq = 0
        self.compliance = None   # Set by IRSExecutionEngine after both are created

        # Covenant thresholds — can be overridden per contract
        self.covenant_thresholds: Dict = dict(_DEFAULT_COVENANT_THRESHOLDS)
        self.workflow = DDWorkflow(contract_id)

    # ── Initialisation ───────────────────────────────────────────────────────

    def initialise_required_documents(self):
        """
        Create DocumentRecord stubs (status=REQUIRED) for every document
        that a vanilla IRS contract requires under Schedule Part 3 / §4 / KYC.

        Called by IRSExecutionEngine.initialise() — idempotent if already done.
        """
        if self.documents:
            return  # Already initialised

        eff = self.params.effective_date
        term = self.params.termination_date

        # ── Phase 1: Pre-signing — one-time documents ────────────────────────
        for party in ["PARTY_A", "PARTY_B"]:
            party_name = (self.params.party_a.short_name if party == "PARTY_A"
                          else self.params.party_b.short_name)

            self._require(
                DocumentType.BOARD_RESOLUTION, party,
                f"Board Resolution — {party_name} authorising execution",
                "§4(b)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.CERTIFICATE_OF_INCORPORATION, party,
                f"Certificate of Incorporation — {party_name}",
                "§4(b)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.LEGAL_OPINION, party,
                f"Legal Opinion — {party_name} MA + netting enforceability",
                "§4(a)(ii)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.ANNUAL_FINANCIAL_STATEMENTS, party,
                f"Audited Financial Statements (recent) — {party_name}",
                "§4(a)(ii)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.TAX_FORM, party,
                f"W-8BEN / Tax Form — {party_name}",
                "§4(a)(i)", due_date=eff, is_pre_signing=True
            )
            # KYC package
            self._require(
                DocumentType.KYC_PASSPORT, party,
                f"KYC — {party_name} Passport / Government ID",
                "§4(a)(ii)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.KYC_ADDRESS, party,
                f"KYC — {party_name} Proof of Registered Address",
                "§4(a)(ii)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.KYC_UBO, party,
                f"KYC — {party_name} Ultimate Beneficial Owner Declaration",
                "§4(a)(ii)", due_date=eff, is_pre_signing=True
            )

        # ── Recurring annual documents ────────────────────────────────────
        year_range = range(eff.year, term.year + 2)
        for y in year_range:
            fy_due = date(y, 4, 30)  # 120 days after Dec 31 → April 30
            if fy_due < eff or fy_due > term + timedelta(days=180):
                continue
            for party in ["PARTY_A", "PARTY_B"]:
                party_name = (self.params.party_a.short_name if party == "PARTY_A"
                              else self.params.party_b.short_name)

                self._require(
                    DocumentType.ANNUAL_FINANCIAL_STATEMENTS, party,
                    f"Annual Financial Statements FY{y - 1} — {party_name}",
                    "§4(a)(ii)", due_date=fy_due
                )
                self._require(
                    DocumentType.COMPLIANCE_CERTIFICATE, party,
                    f"Compliance Certificate FY{y - 1} — {party_name}",
                    "§4(a)(ii)", due_date=fy_due
                )
                # Tax forms — annually, due 30 days after effective date anniversary
                tax_due = eff.replace(year=y) + timedelta(days=30)
                if eff <= tax_due <= term:
                    self._require(
                        DocumentType.TAX_FORM, party,
                        f"W-8BEN / Tax Forms {y} — {party_name}",
                        "§4(a)(i)", due_date=tax_due
                    )
                # KYC annual re-certification (UBO)
                ubo_due = eff.replace(year=y)
                if eff < ubo_due <= term:
                    self._require(
                        DocumentType.KYC_UBO, party,
                        f"KYC UBO Re-certification {y} — {party_name}",
                        "§4(a)(ii)", due_date=ubo_due
                    )

        print(f"  [DD] {len(self.documents)} required documents initialised "
              f"for contract {self.contract_id}")

    def _require(self, doc_type: DocumentType, party: str, description: str,
                 section: str, due_date: date,
                 is_pre_signing: bool = False) -> DocumentRecord:
        """Create a DocumentRecord with status=REQUIRED."""
        self._seq += 1
        doc_id = f"DD-{self.contract_id}-{self._seq:04d}"
        rec = DocumentRecord(
            doc_id=doc_id,
            doc_type=doc_type,
            contract_id=self.contract_id,
            party=party,
            status=DocumentStatus.REQUIRED,
            linked_obligation=section,
            description=description,
            due_date_obligation=due_date,
            is_pre_signing=is_pre_signing,
        )
        self.documents.append(rec)
        return rec

    # ── Document upload ──────────────────────────────────────────────────────

    def upload_document(
        self,
        doc_id: str,
        filename: str,
        uploaded_by: str,
        file_hash: Optional[str] = None,
        file_content_b64: Optional[str] = None,
        today: Optional[date] = None,
    ) -> Tuple[DocumentRecord, List[str]]:
        """
        Register a client-uploaded document.

        Steps:
        1. Find the DocumentRecord by doc_id.
        2. Compute or accept file hash for audit trail.
        3. Set status → UPLOADED.
        4. Run AUTO checks (deadline, expiry, financial ratios).
        5. Raise HUMAN GATE flags where required.
        6. Alert compliance monitor.

        Returns: (DocumentRecord, list_of_alert_messages)
        Raises: KeyError if doc_id not found.
                ValueError if document is already VALIDATED.
        """
        today = today or date.today()
        rec = self._find(doc_id)

        if rec.status == DocumentStatus.VALIDATED:
            raise ValueError(
                f"Document {doc_id} is already VALIDATED. "
                "To re-upload, the advisor must first reject or expire it."
            )

        # Compute hash
        if file_content_b64:
            try:
                raw = base64.b64decode(file_content_b64)
                file_hash = hashlib.sha256(raw).hexdigest()
            except Exception:
                # Base64 decode failed — accept caller-provided hash or leave None
                pass

        rec.upload_date = today
        rec.filename = filename
        rec.file_hash = file_hash
        rec.uploaded_by = uploaded_by
        rec.status = DocumentStatus.UPLOADED

        alerts: List[str] = []

        # ── Compute expiry date ───────────────────────────────────────────
        expiry_days = _EXPIRY_DAYS.get(rec.doc_type)
        if expiry_days:
            rec.expiry_date = today + timedelta(days=expiry_days)

        # ── AUTO check: was it uploaded on time? ──────────────────────────
        due = rec.due_date_obligation
        if due and today > due:
            days_late = (today - due).days
            detail = (
                f"Document uploaded {days_late} day(s) late "
                f"(due {due}, uploaded {today}). "
                "Potential §5(a)(ii) Breach of Agreement."
            )
            rec.auto_check_passed = False
            rec.auto_check_detail = detail
            alerts.append(f"LATE UPLOAD: {rec.description} — {detail}")
        else:
            rec.auto_check_passed = True
            rec.auto_check_detail = "Uploaded within deadline."

        # ── AUTO check: structured financial data ─────────────────────────
        if rec.doc_type == DocumentType.ANNUAL_FINANCIAL_STATEMENTS and file_content_b64:
            ratios, breaches = self._extract_financial_ratios(
                file_content_b64, rec.contract_id, rec.party
            )
            if ratios:
                rec.financial_ratios = ratios
                rec.covenant_breaches = breaches
                if breaches:
                    rec.requires_human_review = True
                    rec.human_gate_reason = (
                        f"COVENANT BREACH DETECTED: {'; '.join(breaches)}. "
                        "Advisor must assess §5(a)(ii) / MAC implications."
                    )
                    alerts.append(
                        f"COVENANT BREACH [{rec.party}]: {'; '.join(breaches)}"
                    )
                    self._raise_human_gate(
                        HumanGateType.COVENANT_BREACH, rec.doc_id, rec.party, today,
                        rec.human_gate_reason,
                        "§5(a)(ii) ISDA 2002 — Breach of Agreement (financial covenants)"
                    )

        # ── AUTO check: compliance cert cross-reference ───────────────────
        if rec.doc_type == DocumentType.COMPLIANCE_CERTIFICATE:
            mismatch = self._check_cert_vs_financials(rec.party, today)
            if mismatch:
                rec.auto_check_detail += f" | Cross-ref: {mismatch}"
                alerts.append(f"CROSS-REF WARNING [{rec.party}]: {mismatch}")

        # ── HUMAN GATE: always-flagged types ─────────────────────────────
        if rec.doc_type in _ALWAYS_HUMAN_GATE:
            gate_type, gate_desc, gate_ref = _ALWAYS_HUMAN_GATE[rec.doc_type]
            rec.requires_human_review = True
            rec.human_gate_reason = gate_desc
            self._raise_human_gate(gate_type, rec.doc_id, rec.party, today,
                                   gate_desc, gate_ref)
            alerts.append(
                f"HUMAN GATE [{rec.doc_type.value}]: {gate_desc[:80]}..."
            )

        print(
            f"  [DD] UPLOADED: {rec.doc_id} — {rec.description} "
            f"(hash: {(file_hash or 'N/A')[:12]})"
        )
        return rec, alerts

    # ── Document validation (advisor) ────────────────────────────────────────

    def validate_document(
        self,
        doc_id: str,
        advisor: str,
        notes: str = "",
        accepted: bool = True,
        today: Optional[date] = None,
    ) -> DocumentRecord:
        """
        Advisor validates (or rejects) an uploaded document.

        HUMAN GATE — no automation here. Advisor must explicitly call this.

        If accepted=True:
          - Sets status → VALIDATED
          - Resolves any HUMAN GATE flags for this doc_id
          - Calls ComplianceMonitor.mark_delivered() to satisfy the §4 obligation

        If accepted=False:
          - Sets status → REJECTED
          - Client must re-upload

        Raises: KeyError if doc_id not found.
                ValueError if document is not UPLOADED.
        """
        today = today or date.today()
        rec = self._find(doc_id)

        if rec.status not in (DocumentStatus.UPLOADED, DocumentStatus.REJECTED):
            raise ValueError(
                f"Document {doc_id} has status {rec.status.value} — "
                "only UPLOADED or REJECTED documents can be validated."
            )

        rec.validation_date = today
        rec.validated_by = advisor
        rec.validation_notes = notes

        if accepted:
            rec.status = DocumentStatus.VALIDATED
            # Resolve human gate flags for this document
            for gate in self.human_gates:
                if gate.doc_id == doc_id and not gate.resolved:
                    gate.resolved = True
                    gate.resolved_by = advisor
                    gate.resolution_notes = notes

            # Callback into ComplianceMonitor
            if self.compliance is not None:
                self.compliance.mark_delivered(
                    section=rec.linked_obligation,
                    party=rec.party,
                    delivered_date=today,
                    document_hash=rec.file_hash,
                )

            print(
                f"  [DD] VALIDATED: {rec.doc_id} — {rec.description} "
                f"by {advisor}"
            )
        else:
            rec.status = DocumentStatus.REJECTED
            print(
                f"  [DD] REJECTED: {rec.doc_id} — {rec.description} "
                f"by {advisor}. Notes: {notes or 'none'}"
            )

        # Advance workflow state machine (pre-signing gate)
        self.workflow.advance(self.documents, today)
        return rec

    # ── Expiry monitoring (AUTO) ──────────────────────────────────────────────

    def check_expirations(self, today: Optional[date] = None) -> List[DocumentRecord]:
        """
        AUTO: Scan all documents for expirations.

        Marks VALIDATED documents as EXPIRED if their expiry_date ≤ today.
        Returns the list of documents newly marked EXPIRED.
        """
        today = today or date.today()
        newly_expired = []
        for rec in self.documents:
            if rec.expiry_date and rec.status == DocumentStatus.VALIDATED:
                if today >= rec.expiry_date:
                    rec.status = DocumentStatus.EXPIRED
                    newly_expired.append(rec)
                    print(
                        f"  [DD] EXPIRED: {rec.doc_id} — {rec.description} "
                        f"(expiry: {rec.expiry_date})"
                    )
        return newly_expired

    # ── AUTO: financial ratio extraction ─────────────────────────────────────

    def _extract_financial_ratios(
        self,
        content_b64: str,
        contract_id: str,
        party: str,
    ) -> Tuple[Optional[Dict[str, float]], List[str]]:
        """
        AUTO: Attempt to extract financial ratios from base64-encoded content.

        Supports two formats:
        1. JSON: {"leverage_ratio": 2.5, "net_worth": 50000000, ...}
        2. CSV:  metric,value\\nleverage_ratio,2.5\\n...

        Returns: (ratios_dict, list_of_covenant_breaches)
        On parse failure returns (None, []).
        """
        try:
            raw = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        except Exception:
            return None, []

        ratios: Optional[Dict[str, float]] = None

        # Try JSON first
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                ratios = {
                    k: float(v)
                    for k, v in data.items()
                    if isinstance(v, (int, float, str))
                    and str(v).replace(".", "", 1).lstrip("-").isdigit()
                }
        except (json.JSONDecodeError, ValueError):
            pass

        # Try CSV
        if ratios is None:
            try:
                reader = csv.DictReader(io.StringIO(raw))
                rows = list(reader)
                if rows and "metric" in rows[0] and "value" in rows[0]:
                    ratios = {}
                    for row in rows:
                        try:
                            ratios[row["metric"].strip()] = float(row["value"])
                        except (ValueError, KeyError):
                            pass
            except Exception:
                pass

        if not ratios:
            return None, []

        # Check against covenant thresholds
        breaches: List[str] = []
        for metric, constraints in self.covenant_thresholds.items():
            if metric not in ratios:
                continue
            val = ratios[metric]
            if "max" in constraints and val > constraints["max"]:
                breaches.append(
                    f"{metric}={val:.2f} exceeds max {constraints['max']} "
                    f"({constraints['label']})"
                )
            if "min" in constraints and val < constraints["min"]:
                breaches.append(
                    f"{metric}={val:.2f} below min {constraints['min']} "
                    f"({constraints['label']})"
                )

        return ratios, breaches

    # ── AUTO: compliance cert cross-reference ─────────────────────────────────

    def _check_cert_vs_financials(self, party: str, today: date) -> Optional[str]:
        """
        AUTO: Check that a compliance certificate has been uploaded in the same
        cycle as the corresponding financial statements.

        Returns a warning string if mismatch detected, else None.
        """
        # Find most recently uploaded financial statements for this party
        fs_docs = [
            d for d in self.documents
            if d.doc_type == DocumentType.ANNUAL_FINANCIAL_STATEMENTS
            and d.party == party
            and d.upload_date is not None
        ]
        if not fs_docs:
            return ("No financial statements uploaded yet — "
                    "compliance cert cannot be cross-referenced.")

        latest_fs = max(fs_docs, key=lambda d: d.upload_date)  # type: ignore[arg-type]

        # Check the cert was uploaded within 14 days of the financials
        if (today - latest_fs.upload_date).days > 14:  # type: ignore[operator]
            return (
                f"Compliance certificate uploaded more than 14 days after "
                f"financial statements ({latest_fs.description}). "
                "Ensure the certificate covers the same period."
            )
        return None

    # ── HUMAN GATE registration ───────────────────────────────────────────────

    def _raise_human_gate(
        self,
        gate_type: HumanGateType,
        doc_id: Optional[str],
        party: str,
        today: date,
        description: str,
        isda_ref: str,
    ):
        gate = HumanGateFlag(
            gate_type=gate_type,
            doc_id=doc_id,
            contract_id=self.contract_id,
            party=party,
            raised_date=today,
            description=description,
            isda_reference=isda_ref,
        )
        self.human_gates.append(gate)

    # ── Resolve human gate ────────────────────────────────────────────────────

    def resolve_human_gate(
        self,
        gate_type: HumanGateType,
        party: str,
        resolved_by: str,
        resolution_notes: str = "",
    ) -> bool:
        """
        Mark a human gate as resolved by the advisor.
        Called when advisor explicitly closes a MAC / §3(d) flag
        (not linked to a specific document).
        Returns True if a gate was found and resolved.
        """
        for gate in self.human_gates:
            if (gate.gate_type == gate_type
                    and gate.party == party
                    and not gate.resolved):
                gate.resolved = True
                gate.resolved_by = resolved_by
                gate.resolution_notes = resolution_notes
                return True
        return False

    # ── Pending human gates ───────────────────────────────────────────────────

    def pending_human_gates(self) -> List[HumanGateFlag]:
        """Return all unresolved human gate flags."""
        return [g for g in self.human_gates if not g.resolved]

    # ── Full DD summary ──────────────────────────────────────────────────────

    def due_diligence_summary(self, today: Optional[date] = None) -> dict:
        """
        Full due diligence status report.

        Returns a dict suitable for JSON serialisation (used by the API).
        Overall RAG status:
          GREEN  — all REQUIRED docs VALIDATED, no pending gates, no expirations
          AMBER  — some UPLOADED but awaiting validation, or expiring ≤ 30 days
          RED    — REQUIRED docs missing / overdue, human gates unresolved,
                   or docs EXPIRED
        """
        today = today or date.today()

        # Run auto-expiry check
        newly_expired = self.check_expirations(today)

        # Group documents by status
        by_status: Dict[str, List[dict]] = {s.value: [] for s in DocumentStatus}
        for doc in self.documents:
            by_status[doc.status.value].append(doc.to_dict())

        # Overdue: REQUIRED docs whose due date has passed
        overdue = [
            d for d in self.documents
            if d.status == DocumentStatus.REQUIRED
            and d.due_date_obligation is not None
            and today > d.due_date_obligation
        ]

        # Expiring soon (within 30 days)
        expiring_soon = [
            d for d in self.documents
            if d.expiry_date
            and d.status == DocumentStatus.VALIDATED
            and 0 <= (d.expiry_date - today).days <= 30
        ]

        # Pending human gates
        pending_gates = self.pending_human_gates()

        # RAG classification
        if (by_status[DocumentStatus.EXPIRED.value]
                or overdue
                or pending_gates):
            rag = "RED"
        elif (by_status[DocumentStatus.UPLOADED.value]
              or expiring_soon
              or by_status[DocumentStatus.REJECTED.value]):
            rag = "AMBER"
        elif all(
            d.status in (DocumentStatus.VALIDATED, DocumentStatus.NOT_REQUIRED)
            for d in self.documents
        ):
            rag = "GREEN"
        else:
            rag = "AMBER"

        return {
            "contract_id": self.contract_id,
            "as_of_date": str(today),
            "rag_status": rag,
            "summary": {
                "total_required":   len(self.documents),
                "validated":        len(by_status[DocumentStatus.VALIDATED.value]),
                "uploaded_pending": len(by_status[DocumentStatus.UPLOADED.value]),
                "required_missing": len(by_status[DocumentStatus.REQUIRED.value]),
                "rejected":         len(by_status[DocumentStatus.REJECTED.value]),
                "expired":          len(by_status[DocumentStatus.EXPIRED.value]),
                "overdue_count":    len(overdue),
                "expiring_soon":    len(expiring_soon),
                "pending_gates":    len(pending_gates),
            },
            "documents_by_status": by_status,
            "overdue_documents": [d.to_dict() for d in overdue],
            "expiring_soon": [d.to_dict() for d in expiring_soon],
            "newly_expired": [d.to_dict() for d in newly_expired],
            "human_gates": {
                "pending":  [g.to_dict() for g in pending_gates],
                "resolved": [g.to_dict() for g in self.human_gates if g.resolved],
            },
            "auto_vs_human_breakdown": {
                "auto_checks": [
                    "Upload deadline tracking",
                    "KYC / tax form expiry monitoring",
                    "Financial ratio extraction (structured CSV / JSON)",
                    "Compliance cert vs financials cross-reference",
                ],
                "human_gates": [
                    "Legal opinion adequacy (substance review)",
                    "Auditor opinion: qualified vs unqualified",
                    "Financial covenant breach assessment",
                    "MAC clause trigger judgment",
                    "§3(d) material representation accuracy",
                ],
            },
            "isda_reference": "§4 ISDA 2002 — Agreements; Schedule Part 3",
            # Workflow state machine
            "workflow": self.workflow.to_dict(),
            "signing_readiness": self.workflow.signing_readiness(self.documents),
            # Post-signing docs that are overdue (potential §5(a)(ii) breach)
            "post_signing_overdue": [
                d.to_dict() for d in self.documents
                if not d.is_pre_signing
                and d.status == DocumentStatus.REQUIRED
                and d.due_date_obligation is not None
                and today > d.due_date_obligation
            ],
            # Flat document list (all docs, for frontend iteration)
            "documents": [d.to_dict() for d in self.documents],
            # Frontend-friendly overdue descriptions list
            "overdue_docs": [d.description for d in overdue],
        }

    # ── Lookup by doc_id ─────────────────────────────────────────────────────

    def _find(self, doc_id: str) -> DocumentRecord:
        for doc in self.documents:
            if doc.doc_id == doc_id:
                return doc
        raise KeyError(f"Document '{doc_id}' not found in contract {self.contract_id}")

    def get_document(self, doc_id: str) -> DocumentRecord:
        """Public lookup — raises KeyError if not found."""
        return self._find(doc_id)

    def get_documents_for_party(self, party: str) -> List[DocumentRecord]:
        """Return all documents for one party."""
        return [d for d in self.documents if d.party == party]

    def get_documents_by_type(self, doc_type: DocumentType) -> List[DocumentRecord]:
        """Return all documents of a given type."""
        return [d for d in self.documents if d.doc_type == doc_type]
