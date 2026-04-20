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

class DocumentCategory(Enum):
    """
    Two-tier document model.

    GENERAL          — entity-level documents uploaded ONCE and shared across
                       ALL contracts where this entity is a party.
    CONTRACT_SPECIFIC — documents specific to a single transaction.
    """
    GENERAL           = "GENERAL"
    CONTRACT_SPECIFIC = "CONTRACT_SPECIFIC"


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

    GENERAL (entity-level, upload once per entity):
      CERTIFICATE_OF_INCORPORATION, CONSTITUTIONAL_DOCUMENTS, BOARD_RESOLUTION,
      AUTHORIZED_SIGNATORIES, KYC_PASSPORT, KYC_ADDRESS, KYC_UBO,
      ANNUAL_FINANCIAL_STATEMENTS, COMPLIANCE_CERTIFICATE, TAX_FORM

    CONTRACT_SPECIFIC (one per contract):
      MASTER_AGREEMENT, SCHEDULE_DOCUMENT, CONFIRMATION_DOCUMENT,
      CREDIT_SUPPORT_ANNEX, TRANSACTION_LEGAL_OPINION, TRANSACTION_CONSENT
    """
    # ── GENERAL: entity-level ────────────────────────────────────────────────
    CERTIFICATE_OF_INCORPORATION = "CERTIFICATE_OF_INCORPORATION"
    # §4(b) — Certificate of incorporation / good standing.
    # Delivered upon execution. No expiry — one-time document.

    CONSTITUTIONAL_DOCUMENTS = "CONSTITUTIONAL_DOCUMENTS"
    # §4(b) — Articles of Association / Memorandum / LLC Agreement / Statuts.
    # No expiry — one-time document.

    BOARD_RESOLUTION = "BOARD_RESOLUTION"
    # §4(b) — Board resolution authorising derivatives transactions.
    # No expiry — one-time document.

    AUTHORIZED_SIGNATORIES = "AUTHORIZED_SIGNATORIES"
    # §4(b) — Powers of Attorney / list of authorised signatories.
    # No expiry — updated on personnel change.

    KYC_PASSPORT = "KYC_PASSPORT"
    # AML/KYC — Certified copy of passport or government ID.
    # Expiry: 3-year KYC refresh cycle.

    KYC_ADDRESS = "KYC_ADDRESS"
    # AML/KYC — Proof of registered address (utility bill / bank statement).
    # Expiry: 3 months (must be recent).

    KYC_UBO = "KYC_UBO"
    # AML/KYC — Ultimate Beneficial Owner declaration.
    # Expiry: annually (re-certify each year).

    ANNUAL_FINANCIAL_STATEMENTS = "ANNUAL_FINANCIAL_STATEMENTS"
    # §4(a)(ii) — Latest audited financial statements.
    # Expiry: 12 months (superseded by next year's accounts).

    COMPLIANCE_CERTIFICATE = "COMPLIANCE_CERTIFICATE"
    # §4(a)(ii) — Officer certificate confirming no EoD / no breach.
    # Delivered with each set of annual accounts.

    TAX_FORM = "TAX_FORM"
    # §4(a)(i) — W-8BEN / W-8BEN-E (entities); FATCA.
    # Expiry: 3 years from date of signature per IRS rules.

    # ── CONTRACT_SPECIFIC: per-transaction ──────────────────────────────────
    MASTER_AGREEMENT = "MASTER_AGREEMENT"
    # §1 ISDA 2002 — Signed ISDA Master Agreement (or on-file reference).
    # HUMAN GATE: advisor must confirm correct version and execution.

    SCHEDULE_DOCUMENT = "SCHEDULE_DOCUMENT"
    # §1 ISDA 2002 — Negotiated Schedule (elections and modifications).
    # HUMAN GATE: advisor must review elections.

    CONFIRMATION_DOCUMENT = "CONFIRMATION_DOCUMENT"
    # §1 ISDA 2002 — Trade Confirmation; auto-generated by the engine.

    CREDIT_SUPPORT_ANNEX = "CREDIT_SUPPORT_ANNEX"
    # §4(b) — CSA / ISDA Credit Support Document (if collateral elected).

    TRANSACTION_LEGAL_OPINION = "TRANSACTION_LEGAL_OPINION"
    # §4(a)(ii) — Legal opinion on enforceability of this specific MA +
    # Confirmation + netting in the relevant jurisdiction.
    # HUMAN GATE: must be reviewed by advisor / counsel.

    TRANSACTION_CONSENT = "TRANSACTION_CONSENT"
    # §4(b) — Any regulatory consent or licence required by this counterparty
    # or jurisdiction for this specific transaction.

    # ── Retained for backward compatibility (no longer created by default) ──
    LEGAL_OPINION = "LEGAL_OPINION"
    # Deprecated in favour of TRANSACTION_LEGAL_OPINION.
    # Kept so existing serialised values can still be deserialised.


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

    # Two-tier document model
    category: "DocumentCategory" = None          # set in _require / entity store
    entity_name: Optional[str] = None            # populated for GENERAL docs

    def __post_init__(self):
        if self.category is None:
            self.category = _DOCUMENT_CATEGORY.get(
                self.doc_type, DocumentCategory.CONTRACT_SPECIFIC)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type.value,
            "category": self.category.value if self.category else "CONTRACT_SPECIFIC",
            "entity_name": self.entity_name,
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

# ── Document category mapping ─────────────────────────────────────────────────

_DOCUMENT_CATEGORY: Dict["DocumentType", "DocumentCategory"] = {
    # GENERAL — entity-level (upload once, applies to all contracts)
    DocumentType.CERTIFICATE_OF_INCORPORATION:  DocumentCategory.GENERAL,
    DocumentType.CONSTITUTIONAL_DOCUMENTS:      DocumentCategory.GENERAL,
    DocumentType.BOARD_RESOLUTION:              DocumentCategory.GENERAL,
    DocumentType.AUTHORIZED_SIGNATORIES:        DocumentCategory.GENERAL,
    DocumentType.KYC_PASSPORT:                  DocumentCategory.GENERAL,
    DocumentType.KYC_ADDRESS:                   DocumentCategory.GENERAL,
    DocumentType.KYC_UBO:                       DocumentCategory.GENERAL,
    DocumentType.ANNUAL_FINANCIAL_STATEMENTS:   DocumentCategory.GENERAL,
    DocumentType.COMPLIANCE_CERTIFICATE:        DocumentCategory.GENERAL,
    DocumentType.TAX_FORM:                      DocumentCategory.GENERAL,
    # CONTRACT_SPECIFIC — per-transaction
    DocumentType.MASTER_AGREEMENT:              DocumentCategory.CONTRACT_SPECIFIC,
    DocumentType.SCHEDULE_DOCUMENT:             DocumentCategory.CONTRACT_SPECIFIC,
    DocumentType.CONFIRMATION_DOCUMENT:         DocumentCategory.CONTRACT_SPECIFIC,
    DocumentType.CREDIT_SUPPORT_ANNEX:          DocumentCategory.CONTRACT_SPECIFIC,
    DocumentType.TRANSACTION_LEGAL_OPINION:     DocumentCategory.CONTRACT_SPECIFIC,
    DocumentType.TRANSACTION_CONSENT:           DocumentCategory.CONTRACT_SPECIFIC,
    # Legacy
    DocumentType.LEGAL_OPINION:                 DocumentCategory.CONTRACT_SPECIFIC,
}


# Which document types require a HUMAN GATE on upload (always)
_ALWAYS_HUMAN_GATE = {
    DocumentType.TRANSACTION_LEGAL_OPINION: (
        HumanGateType.LEGAL_OPINION_REVIEW,
        "Legal opinion substance review required — "
        "is the opinion unqualified and covers enforceability of the MA, "
        "Confirmation, and close-out netting in the relevant jurisdiction?",
        "§4(a)(ii) ISDA 2002"
    ),
    DocumentType.MASTER_AGREEMENT: (
        HumanGateType.LEGAL_OPINION_REVIEW,
        "Master Agreement review required — "
        "confirm correct ISDA version, governing law elections, and execution.",
        "§1 ISDA 2002"
    ),
    DocumentType.SCHEDULE_DOCUMENT: (
        HumanGateType.LEGAL_OPINION_REVIEW,
        "Schedule review required — "
        "verify all elections, modifications, and Part 5 additional provisions.",
        "§1 ISDA 2002"
    ),
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
    DocumentType.ANNUAL_FINANCIAL_STATEMENTS:   "§4(a)(ii)",
    DocumentType.COMPLIANCE_CERTIFICATE:        "§4(a)(ii)",
    DocumentType.TAX_FORM:                      "§4(a)(i)",
    DocumentType.BOARD_RESOLUTION:              "§4(b)",
    DocumentType.LEGAL_OPINION:                 "§4(a)(ii)",
    DocumentType.TRANSACTION_LEGAL_OPINION:     "§4(a)(ii)",
    DocumentType.KYC_PASSPORT:                  "§4(a)(ii)",
    DocumentType.KYC_ADDRESS:                   "§4(a)(ii)",
    DocumentType.KYC_UBO:                       "§4(a)(ii)",
    DocumentType.CERTIFICATE_OF_INCORPORATION:  "§4(b)",
    DocumentType.CONSTITUTIONAL_DOCUMENTS:      "§4(b)",
    DocumentType.AUTHORIZED_SIGNATORIES:        "§4(b)",
    DocumentType.MASTER_AGREEMENT:              "§1 ISDA 2002",
    DocumentType.SCHEDULE_DOCUMENT:             "§1 ISDA 2002",
    DocumentType.CONFIRMATION_DOCUMENT:         "§1 ISDA 2002",
    DocumentType.CREDIT_SUPPORT_ANNEX:          "§4(b)",
    DocumentType.TRANSACTION_CONSENT:           "§4(b)",
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
    # General entity docs
    DocumentType.BOARD_RESOLUTION,
    DocumentType.CERTIFICATE_OF_INCORPORATION,
    DocumentType.CONSTITUTIONAL_DOCUMENTS,
    DocumentType.AUTHORIZED_SIGNATORIES,
    DocumentType.LEGAL_OPINION,
    DocumentType.TAX_FORM,
    DocumentType.ANNUAL_FINANCIAL_STATEMENTS,
    # Contract-specific docs
    DocumentType.MASTER_AGREEMENT,
    DocumentType.SCHEDULE_DOCUMENT,
    DocumentType.CONFIRMATION_DOCUMENT,
    DocumentType.CREDIT_SUPPORT_ANNEX,
    DocumentType.TRANSACTION_LEGAL_OPINION,
    DocumentType.TRANSACTION_CONSENT,
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
        Create DocumentRecord stubs for CONTRACT-SPECIFIC documents only.

        General (entity-level) documents are managed by EntityDocumentStore
        and satisfy requirements across ALL contracts for that entity.
        See api._get_or_create_entity_store().

        Called by IRSExecutionEngine.initialise() — idempotent if already done.
        """
        if self.documents:
            return  # Already initialised

        eff = self.params.effective_date

        for party in ["PARTY_A", "PARTY_B"]:
            party_name = (self.params.party_a.short_name if party == "PARTY_A"
                          else self.params.party_b.short_name)

            self._require(
                DocumentType.MASTER_AGREEMENT, party,
                f"ISDA Master Agreement — {party_name} (signed or on-file reference)",
                "§1 ISDA 2002", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.SCHEDULE_DOCUMENT, party,
                f"Schedule to Master Agreement — {party_name}",
                "§1 ISDA 2002", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.CONFIRMATION_DOCUMENT, party,
                f"Trade Confirmation — {party_name} (this transaction)",
                "§1 ISDA 2002", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.CREDIT_SUPPORT_ANNEX, party,
                f"Credit Support Annex / Collateral Document — {party_name}",
                "§4(b)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.TRANSACTION_LEGAL_OPINION, party,
                f"Transaction Legal Opinion — {party_name} "
                f"(MA + netting enforceability in {self.params.governing_law})",
                "§4(a)(ii)", due_date=eff, is_pre_signing=True
            )
            self._require(
                DocumentType.TRANSACTION_CONSENT, party,
                f"Regulatory Consent / Transaction Licence — {party_name}",
                "§4(b)", due_date=eff, is_pre_signing=True
            )

        print(f"  [DD] {len(self.documents)} contract-specific documents initialised "
              f"for contract {self.contract_id}")

    def _require(self, doc_type: DocumentType, party: str, description: str,
                 section: str, due_date: date,
                 is_pre_signing: bool = False) -> DocumentRecord:
        """Create a contract-specific DocumentRecord with status=REQUIRED."""
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
            category=DocumentCategory.CONTRACT_SPECIFIC,
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

    def auto_validate_all(self, advisor: str = "DEMO_SYSTEM", today=None) -> int:
        """
        Demo mode only: mark all pending documents VALIDATED without the
        normal upload → advisor-review flow.
        """
        today = today or date.today()
        count = 0
        for doc in self.documents:
            if doc.status in (DocumentStatus.VALIDATED, DocumentStatus.NOT_REQUIRED):
                continue
            if doc.status in (DocumentStatus.REQUIRED, DocumentStatus.EXPIRED):
                doc.status = DocumentStatus.UPLOADED
                doc.uploaded_by = advisor
                doc.upload_date = today
                doc.file_hash = "DEMO_AUTO"
            try:
                self.validate_document(
                    doc_id=doc.doc_id,
                    advisor=advisor,
                    notes="Auto-validated — demo mode",
                    accepted=True,
                    today=today,
                )
                count += 1
            except (ValueError, KeyError):
                pass
        return count

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

    def due_diligence_summary(
        self,
        today: Optional[date] = None,
        entity_docs_a: Optional[List["DocumentRecord"]] = None,
        entity_docs_b: Optional[List["DocumentRecord"]] = None,
    ) -> dict:
        """
        Full due diligence status report — merges contract-specific docs with
        general (entity-level) docs from EntityDocumentStore instances.

        entity_docs_a: general docs for party_a's EntityDocumentStore
        entity_docs_b: general docs for party_b's EntityDocumentStore

        Overall RAG status:
          GREEN  — all docs VALIDATED, no pending gates, no expirations
          AMBER  — some UPLOADED but awaiting validation, or expiring ≤ 30 days
          RED    — docs missing / overdue, human gates unresolved, or EXPIRED
        """
        today = today or date.today()
        entity_a = list(entity_docs_a or [])
        entity_b = list(entity_docs_b or [])

        # Contract-specific docs + entity-level docs merged for completeness checks
        all_docs: List[DocumentRecord] = self.documents + entity_a + entity_b

        # Run auto-expiry check on contract docs only
        # (entity store manages its own expirations via check_expirations())
        newly_expired = self.check_expirations(today)

        # Group ALL docs by status
        by_status: Dict[str, List[dict]] = {s.value: [] for s in DocumentStatus}
        for doc in all_docs:
            by_status[doc.status.value].append(doc.to_dict())

        # Overdue: REQUIRED docs whose due date has passed
        overdue = [
            d for d in all_docs
            if d.status == DocumentStatus.REQUIRED
            and d.due_date_obligation is not None
            and today > d.due_date_obligation
        ]

        # Expiring soon (within 30 days)
        expiring_soon = [
            d for d in all_docs
            if d.expiry_date
            and d.status == DocumentStatus.VALIDATED
            and 0 <= (d.expiry_date - today).days <= 30
        ]

        # Pending human gates (contract-level only; entity store has its own)
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
            for d in all_docs
        ):
            rag = "GREEN"
        else:
            rag = "AMBER"

        # Workflow uses the full merged doc list so entity docs gate signing
        merged_for_workflow = all_docs

        return {
            "contract_id": self.contract_id,
            "as_of_date": str(today),
            "rag_status": rag,
            "summary": {
                "total_required":   len(all_docs),
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
                    "Master Agreement and Schedule adequacy review",
                    "Transaction legal opinion substance review",
                    "Auditor opinion: qualified vs unqualified",
                    "Financial covenant breach assessment",
                    "MAC clause trigger judgment",
                    "§3(d) material representation accuracy",
                ],
            },
            "isda_reference": "§4 ISDA 2002 — Agreements; Schedule Part 3",
            # Workflow state machine
            "workflow": self.workflow.to_dict(),
            "signing_readiness": self.workflow.signing_readiness(merged_for_workflow),
            # Post-signing docs that are overdue (potential §5(a)(ii) breach)
            "post_signing_overdue": [
                d.to_dict() for d in all_docs
                if not d.is_pre_signing
                and d.status == DocumentStatus.REQUIRED
                and d.due_date_obligation is not None
                and today > d.due_date_obligation
            ],
            # Flat doc list — ALL docs (general + contract-specific)
            "documents": [d.to_dict() for d in all_docs],
            # Frontend-friendly overdue descriptions list
            "overdue_docs": [d.description for d in overdue],
            # Two-tier breakdown for the new frontend layout
            "document_tiers": {
                "general_party_a": [d.to_dict() for d in entity_a],
                "general_party_b": [d.to_dict() for d in entity_b],
                "contract_specific": [d.to_dict() for d in self.documents],
            },
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


# ─────────────────────────────────────────────────────────────────────────────
# EntityDocumentStore
# ─────────────────────────────────────────────────────────────────────────────

class EntityDocumentStore:
    """
    Manages GENERAL (entity-level) documents for a single legal entity.

    Documents uploaded here satisfy requirements across ALL contracts where
    this entity is a party — they are uploaded ONCE and reused.

    General document types managed (10 types):
      CERTIFICATE_OF_INCORPORATION, CONSTITUTIONAL_DOCUMENTS, BOARD_RESOLUTION,
      AUTHORIZED_SIGNATORIES, KYC_PASSPORT, KYC_ADDRESS, KYC_UBO,
      ANNUAL_FINANCIAL_STATEMENTS, COMPLIANCE_CERTIFICATE, TAX_FORM

    IDs use the format GD-{safe_name}-{seq:04d} to distinguish from
    contract-specific DD-{contract_id}-{seq:04d} IDs.
    """

    def __init__(self, entity_name: str):
        self.entity_name = entity_name
        self.documents: List[DocumentRecord] = []
        self.human_gates: List[HumanGateFlag] = []
        self._seq = 0
        # Safe identifier for doc IDs (strip spaces, cap at 20 chars)
        self._safe_name = entity_name.replace(" ", "_")[:20]
        self.initialise_required_documents()

    # ── Initialisation ───────────────────────────────────────────────────────

    def initialise_required_documents(self):
        """
        Create DocumentRecord stubs for all 10 GENERAL document types.
        Idempotent — no-op if already initialised.
        """
        if self.documents:
            return

        self._require(
            DocumentType.CERTIFICATE_OF_INCORPORATION,
            f"Certificate of Incorporation — {self.entity_name}",
            "§4(b)", is_pre_signing=True,
        )
        self._require(
            DocumentType.CONSTITUTIONAL_DOCUMENTS,
            f"Constitutional Documents (Articles / Memorandum) — {self.entity_name}",
            "§4(b)", is_pre_signing=True,
        )
        self._require(
            DocumentType.BOARD_RESOLUTION,
            f"Board Resolution authorising derivatives transactions — {self.entity_name}",
            "§4(b)", is_pre_signing=True,
        )
        self._require(
            DocumentType.AUTHORIZED_SIGNATORIES,
            f"Authorised Signatories / Powers of Attorney — {self.entity_name}",
            "§4(b)", is_pre_signing=True,
        )
        self._require(
            DocumentType.KYC_PASSPORT,
            f"KYC — Passport / Government ID — {self.entity_name}",
            "§4(a)(ii)", is_pre_signing=True,
        )
        self._require(
            DocumentType.KYC_ADDRESS,
            f"KYC — Proof of Registered Address — {self.entity_name}",
            "§4(a)(ii)", is_pre_signing=True,
        )
        self._require(
            DocumentType.KYC_UBO,
            f"KYC — Ultimate Beneficial Owner Declaration — {self.entity_name}",
            "§4(a)(ii)", is_pre_signing=True,
        )
        self._require(
            DocumentType.ANNUAL_FINANCIAL_STATEMENTS,
            f"Annual Financial Statements — {self.entity_name}",
            "§4(a)(ii)", is_pre_signing=True,
        )
        self._require(
            DocumentType.COMPLIANCE_CERTIFICATE,
            f"Compliance Certificate — {self.entity_name}",
            "§4(a)(ii)", is_pre_signing=False,
        )
        self._require(
            DocumentType.TAX_FORM,
            f"Tax Form (W-8BEN / W-8BEN-E / FATCA) — {self.entity_name}",
            "§4(a)(i)", is_pre_signing=True,
        )

        print(f"  [EntityStore] {len(self.documents)} general documents initialised "
              f"for entity '{self.entity_name}'")

    def _require(self, doc_type: DocumentType, description: str,
                 section: str, is_pre_signing: bool = False) -> DocumentRecord:
        """Create a GENERAL DocumentRecord with status=REQUIRED."""
        self._seq += 1
        doc_id = f"GD-{self._safe_name}-{self._seq:04d}"
        rec = DocumentRecord(
            doc_id=doc_id,
            doc_type=doc_type,
            contract_id="ENTITY",
            party="ENTITY",
            status=DocumentStatus.REQUIRED,
            linked_obligation=section,
            description=description,
            due_date_obligation=None,
            is_pre_signing=is_pre_signing,
            category=DocumentCategory.GENERAL,
            entity_name=self.entity_name,
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
        Register a client-uploaded general document.

        Follows the same lifecycle as CovenantChecker.upload_document() but
        without contract-specific checks (no deadline, no financial ratios).
        """
        today = today or date.today()
        rec = self._find(doc_id)

        if rec.status == DocumentStatus.VALIDATED:
            raise ValueError(
                f"Document {doc_id} is already VALIDATED. "
                "To re-upload, the advisor must first reject or expire it."
            )

        if file_content_b64:
            try:
                raw = base64.b64decode(file_content_b64)
                file_hash = hashlib.sha256(raw).hexdigest()
            except Exception:
                pass

        rec.upload_date = today
        rec.filename = filename
        rec.file_hash = file_hash
        rec.uploaded_by = uploaded_by
        rec.status = DocumentStatus.UPLOADED

        alerts: List[str] = []

        # Expiry date
        expiry_days = _EXPIRY_DAYS.get(rec.doc_type)
        if expiry_days:
            rec.expiry_date = today + timedelta(days=expiry_days)

        # Human gate for always-flagged types (e.g. ANNUAL_FINANCIAL_STATEMENTS)
        if rec.doc_type in _ALWAYS_HUMAN_GATE:
            gate_type, gate_desc, gate_ref = _ALWAYS_HUMAN_GATE[rec.doc_type]
            rec.requires_human_review = True
            rec.human_gate_reason = gate_desc
            self._raise_human_gate(gate_type, rec.doc_id, "ENTITY",
                                   today, gate_desc, gate_ref)
            alerts.append(f"HUMAN GATE [{rec.doc_type.value}]: {gate_desc[:80]}...")
        else:
            rec.auto_check_passed = True
            rec.auto_check_detail = "Uploaded."

        print(f"  [EntityStore] UPLOADED: {rec.doc_id} — {rec.description} "
              f"({self.entity_name})")
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
        """Advisor validates (or rejects) a general document."""
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
            for gate in self.human_gates:
                if gate.doc_id == doc_id and not gate.resolved:
                    gate.resolved = True
                    gate.resolved_by = advisor
                    gate.resolution_notes = notes
            print(f"  [EntityStore] VALIDATED: {rec.doc_id} — {rec.description} "
                  f"by {advisor}")
        else:
            rec.status = DocumentStatus.REJECTED
            print(f"  [EntityStore] REJECTED: {rec.doc_id} — {rec.description} "
                  f"by {advisor}")

        return rec

    def auto_validate_all(self, advisor: str = "DEMO_SYSTEM", today=None) -> int:
        """Demo mode only: mark all pending general documents VALIDATED."""
        today = today or date.today()
        count = 0
        for doc in self.documents:
            if doc.status in (DocumentStatus.VALIDATED, DocumentStatus.NOT_REQUIRED):
                continue
            if doc.status in (DocumentStatus.REQUIRED, DocumentStatus.EXPIRED):
                doc.status = DocumentStatus.UPLOADED
                doc.uploaded_by = advisor
                doc.upload_date = today
                doc.file_hash = "DEMO_AUTO"
            try:
                self.validate_document(
                    doc_id=doc.doc_id,
                    advisor=advisor,
                    notes="Auto-validated — demo mode",
                    accepted=True,
                    today=today,
                )
                count += 1
            except (ValueError, KeyError):
                pass
        return count

    # ── Expiry monitoring ────────────────────────────────────────────────────

    def check_expirations(self, today: Optional[date] = None) -> List[DocumentRecord]:
        """AUTO: Mark validated documents as EXPIRED if past their expiry date."""
        today = today or date.today()
        newly_expired = []
        for rec in self.documents:
            if rec.expiry_date and rec.status == DocumentStatus.VALIDATED:
                if today >= rec.expiry_date:
                    rec.status = DocumentStatus.EXPIRED
                    newly_expired.append(rec)
                    print(f"  [EntityStore] EXPIRED: {rec.doc_id} — {rec.description}")
        return newly_expired

    # ── Human gates ──────────────────────────────────────────────────────────

    def pending_human_gates(self) -> List[HumanGateFlag]:
        """Return all unresolved human gate flags."""
        return [g for g in self.human_gates if not g.resolved]

    def _raise_human_gate(
        self,
        gate_type: HumanGateType,
        doc_id: Optional[str],
        party: str,
        today: date,
        description: str,
        isda_ref: str,
    ):
        self.human_gates.append(HumanGateFlag(
            gate_type=gate_type,
            doc_id=doc_id,
            contract_id="ENTITY",
            party=party,
            raised_date=today,
            description=description,
            isda_reference=isda_ref,
        ))

    # ── Lookup ───────────────────────────────────────────────────────────────

    def _find(self, doc_id: str) -> DocumentRecord:
        for doc in self.documents:
            if doc.doc_id == doc_id:
                return doc
        raise KeyError(
            f"Document '{doc_id}' not found in entity store for '{self.entity_name}'"
        )

    # ── Summary ──────────────────────────────────────────────────────────────

    def summary(self, today: Optional[date] = None) -> dict:
        """Return a RAG-status summary for this entity's general documents."""
        today = today or date.today()
        self.check_expirations(today)

        by_status: Dict[str, List[dict]] = {s.value: [] for s in DocumentStatus}
        for doc in self.documents:
            by_status[doc.status.value].append(doc.to_dict())

        pending_gates = self.pending_human_gates()

        expiring_soon = [
            d for d in self.documents
            if d.expiry_date
            and d.status == DocumentStatus.VALIDATED
            and 0 <= (d.expiry_date - today).days <= 30
        ]

        if (by_status[DocumentStatus.EXPIRED.value]
                or by_status[DocumentStatus.REQUIRED.value]
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
            "entity_name": self.entity_name,
            "rag_status": rag,
            "total": len(self.documents),
            "validated": len(by_status[DocumentStatus.VALIDATED.value]),
            "uploaded_pending": len(by_status[DocumentStatus.UPLOADED.value]),
            "required_missing": len(by_status[DocumentStatus.REQUIRED.value]),
            "rejected": len(by_status[DocumentStatus.REJECTED.value]),
            "expired": len(by_status[DocumentStatus.EXPIRED.value]),
            "expiring_soon": len(expiring_soon),
            "pending_gates": len(pending_gates),
            "documents": [d.to_dict() for d in self.documents],
            "human_gates": {
                "pending":  [g.to_dict() for g in pending_gates],
                "resolved": [g.to_dict() for g in self.human_gates if g.resolved],
            },
        }
