"""
=============================================================================
  SMART LEGAL CONTRACT — INTEREST RATE SWAP (EUR VANILLA)
  Execution Engine v0.2

  WHAT THIS FILE DOES
  ────────────────────
  This is the "brain" behind the Smart Legal Contract. It reads the contract
  parameters (who, what notional, what rates, what dates), then:
    1. Fetches EURIBOR 3M from the ECB every quarter             [AUTOMATED]
    2. Calculates fixed and floating payment amounts             [AUTOMATED]
    3. Nets the two legs per §2(c) ISDA 2002                     [AUTOMATED]
    4. Monitors all 8 Events of Default and 5 Termination Events [AUTOMATED]
    5. Triggers circuit breaker if any EoD fires                 [AUTOMATED]
    6. Calculates Close-out Amount waterfall per §6 ISDA 2002    [AUTOMATED]
    7. Generates a cryptographic audit trail for every action    [AUTOMATED]
    8. Produces a Payment Instruction requiring human approval   [HUMAN GATE]

  HIERARCHY CLAUSE (§1(b) ISDA 2002)
  ────────────────────────────────────
  The legal text of the Smart Legal Contract (SLC-IRS-EUR-001) ALWAYS prevails
  over this code. Priority: Confirmation > Schedule > Master Agreement > Code.
  This engine produces Payment Instructions only. No payment is ever executed
  automatically. Human approval by the Calculation Agent is mandatory.

  LEGAL NOTICE
  ─────────────
  Prototype for academic demonstration only. Not for production use.
  Not legal or financial advice.

  ARCHITECTURE
  ─────────────
  Layer 1 — Legal Prose     : SLC-IRS-EUR-001 (Word document)
  Layer 2 — Structured Data : SwapParameters dataclass (below)
  Layer 3 — Execution Engine: this file
  Layer 4 — Audit Trail     : audit_trail_*.json (output)
=============================================================================
"""

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, getcontext
from enum import Enum, auto
from typing import Optional, List, Dict, Tuple
import json
import urllib.request
import urllib.error
import hashlib
import sys
from datetime import datetime, timezone

# High-precision arithmetic - CRITICAL for financial calculations
# Decimal avoids float rounding errors: float(0.1)+float(0.2)=0.30000000000000004
getcontext().prec = 28


# ─────────────────────────────────────────────────────────────────────────────
# ENUMERATIONS — Controlled vocabularies, anchored to ISDA 2002 sections
# ─────────────────────────────────────────────────────────────────────────────

class ContractState(Enum):
    """
    Lifecycle states of the swap.
    Mirrors the ISDA 2002 lifecycle: pre-trade → active → terminated.
    """
    PRE_EXECUTION      = "PRE_EXECUTION"      # Before bilateral signatures
    PENDING_SIGNATURE  = "PENDING_SIGNATURE"  # Created, awaiting client signature
    ACTIVE             = "ACTIVE"             # Normal operation, payments flowing
    SUSPENDED       = "SUSPENDED"        # §2(a)(iii): EoD detected, payments withheld
    EARLY_TERM_NOTIFIED = "EARLY_TERM_NOTIFIED"  # §6(a): ETD notice given, pending
    TERMINATED      = "TERMINATED"       # §6: Close-out complete, swap wound down


class OracleStatus(Enum):
    """Status of the ECB oracle rate fetch."""
    PENDING   = "PENDING"
    CONFIRMED = "CONFIRMED"    # Live rate from ECB SDW
    FALLBACK  = "FALLBACK"     # ECB unreachable — ISDA 2021 fallback applied
    CHALLENGED = "CHALLENGED"  # Rate challenged by a party (>5bps threshold)


class NetPayer(Enum):
    """Which party owes the net payment after §2(c) netting."""
    PARTY_A    = "PARTY_A"     # Fixed rate payer owes net
    PARTY_B    = "PARTY_B"     # Floating rate payer owes net
    ZERO_NET   = "ZERO_NET"    # Exact offset — no payment (rare)


class DefaultingParty(Enum):
    """Which party triggered an Event of Default."""
    PARTY_A    = "PARTY_A"
    PARTY_B    = "PARTY_B"
    NONE       = "NONE"


# ─────────────────────────────────────────────────────────────────────────────
# EVENTS OF DEFAULT — §5(a) ISDA 2002
# ─────────────────────────────────────────────────────────────────────────────

class EventOfDefault(Enum):
    """
    Complete enumeration of the 8 Events of Default under §5(a) ISDA 2002.

    Each EoD gives the Non-defaulting Party the right to designate an Early
    Termination Date and trigger the §6 close-out waterfall.

    Key differences from Termination Events:
    - EoDs are fault-based (one party is the Defaulting Party)
    - ALL Transactions are Terminated Transactions (not just Affected Transactions)
    - The Non-defaulting Party determines Close-out Amount
    """

    FAILURE_TO_PAY = "FAILURE_TO_PAY"
    # §5(a)(i): Party fails to make payment/delivery when due.
    # Grace period: 1 Local Business Day after notice.
    # Most common EoD in practice. Automatically monitored by this engine.
    # Engine monitors: scheduled payment missed + 1 LBD grace → fires EoD.

    BREACH_OF_AGREEMENT = "BREACH_OF_AGREEMENT"
    # §5(a)(ii)(1): Failure to comply with any agreement/obligation.
    # Grace period: 30 days after notice.
    # NEW in 2002: §5(a)(ii)(2): Repudiation/challenge of the Agreement itself.
    # Engine monitors: repudiation_flag set by Calculation Agent.

    CREDIT_SUPPORT_DEFAULT = "CREDIT_SUPPORT_DEFAULT"
    # §5(a)(iii): Three sub-triggers:
    #   (1) Failure to comply with Credit Support Document
    #   (2) Expiration/termination of Credit Support Document
    #   (3) Repudiation of Credit Support Document
    # Only relevant if CSA clause is elected (not in Vanilla IRS template).
    # Engine: skipped if csa_elected = False.

    MISREPRESENTATION = "MISREPRESENTATION"
    # §5(a)(iv): Breach of representations under §3 (not tax reps).
    # Unchanged from 1992 Agreement.
    # Engine monitors: misrep_flag set by Calculation Agent.

    DEFAULT_UNDER_SPECIFIED_TRANSACTION = "DEFAULT_UNDER_SPECIFIED_TRANSACTION"
    # §5(a)(v): Limited cross-default for "Specified Transactions"
    # (other derivative master agreements, e.g. GMRA for repos).
    # Grace period: 1 LBD for payment failures (reduced from 3 in 1992 MA).
    # New in 2002: delivery failures in clause (3).
    # Engine: monitors specified_transaction_default flag.

    CROSS_DEFAULT = "CROSS_DEFAULT"
    # §5(a)(vi): ELECTIVE — only applies if specified in Schedule Part 1(c).
    # Triggers if party defaults on "Specified Indebtedness" above Threshold Amount.
    # 2002 change: limbs (1) and (2) are aggregated to reach the Threshold.
    # Engine: only active if cross_default_elected = True.

    BANKRUPTCY = "BANKRUPTCY"
    # §5(a)(vii): Insolvency events — dissolution, inability to pay debts,
    # administration, winding-up, appointment of receiver, etc.
    # 15-day grace period for bonafide disputes over certain insolvency proceedings.
    # Engine monitors: bankruptcy_flag set by Calculation Agent.

    MERGER_WITHOUT_ASSUMPTION = "MERGER_WITHOUT_ASSUMPTION"
    # §5(a)(viii): Party or Credit Support Provider merges/transfers assets
    # to an entity that does NOT assume the ISDA obligations.
    # Engine monitors: merger_without_assumption_flag set by Calculation Agent.


# ─────────────────────────────────────────────────────────────────────────────
# TERMINATION EVENTS — §5(b) ISDA 2002
# ─────────────────────────────────────────────────────────────────────────────

class TerminationEvent(Enum):
    """
    Complete enumeration of the 5 Termination Events under §5(b) ISDA 2002.

    Key differences from Events of Default:
    - Non-fault based (except Credit Event Upon Merger)
    - Only Affected Transactions are terminated (not ALL Transactions)
    - The Affected Party or both parties may determine Close-out Amount
    - Waiting Period applies before Early Termination Date can be designated
      (Illegality: 3 LBDs; Force Majeure: 8 LBDs)
    """

    ILLEGALITY = "ILLEGALITY"
    # §5(b)(i): Performance becomes unlawful due to change in law/regulation.
    # Anticipatory: triggers even if performance not yet due.
    # Waiting Period: 3 Local Business Days.
    # §5(c)(i): Cannot simultaneously be an EoD under §5(a)(i)/(ii)(1)/(iii)(1).
    # Engine monitors: illegality_flag + jurisdiction check.

    FORCE_MAJEURE = "FORCE_MAJEURE"
    # §5(b)(ii): Natural disasters, acts of terrorism, acts of state, etc.
    # Performance becomes impossible/impracticable.
    # Waiting Period: 8 Local Business Days (longer than Illegality).
    # §5(d): Payments are DEFERRED (not cancelled) during Waiting Period.
    # Engine: defers payment instructions; marks as DEFERRED.

    CREDIT_EVENT_UPON_MERGER = "CREDIT_EVENT_UPON_MERGER"
    # §5(b)(v): Party or Credit Support Provider merges and creditworthiness
    # of surviving entity is "materially weaker" immediately after the event.
    # Distinguished from §5(a)(viii): no assumption failure, just weaker credit.
    # 2002 clarification: comparison is immediately before vs. immediately after.
    # Engine monitors: credit_event_merger_flag.

    TAX_EVENT = "TAX_EVENT"
    # §5(b)(iii): A party would be required to gross-up or deduct withholding
    # tax that was not anticipated at execution. Complex — involves tax counsel.
    # §6(b)(ii): Affected Party must first attempt to transfer Affected Transactions.
    # Engine: monitors tax_event_flag; triggers transfer attempt before ETD.

    TAX_EVENT_UPON_MERGER = "TAX_EVENT_UPON_MERGER"
    # §5(b)(iv): Tax Event arising from a merger event.
    # Same transfer obligation as Tax Event under §6(b)(ii).
    # Engine monitors: tax_event_merger_flag.


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PartyDetails:
    """
    Identifies a party to the ISDA Master Agreement.
    Maps to the preamble and Schedule Part 1 of the 2002 MA.
    """
    name: str                          # Legal entity name
    short_name: str                    # Used in Payment Instructions
    role: str                          # "fixed_payer" or "floating_payer"
    lei: Optional[str] = None          # Legal Entity Identifier (20-char ISO 17442)
    jurisdiction: str = "England"      # Relevant for Illegality §5(b)(i) analysis
    jurisdiction_code: str = "GB"      # ISO 3166-1 alpha-2 — used by NettingOpinionCheck
    credit_support_provider: Optional[str] = None  # §5(a)(iii) Credit Support Provider


@dataclass
class SwapParameters:
    """
    The complete structured data representation of a Vanilla IRS.

    This is Layer 2 of the architecture. It encodes all economic terms
    that would appear in the Confirmation (which overrides the Schedule
    under the §1(b) ISDA 2002 hierarchy).

    Every field maps to a specific section of the ISDA documentation.
    """
    # ── Identification ──────────────────────────────────────────────────────
    contract_id: str                    # e.g. "SLC-IRS-EUR-001"
    isda_version: str = "ISDA 2002 Master Agreement"
    schedule_id: Optional[str] = None  # References ScheduleElections.schedule_id — links this Confirmation to its Schedule

    # ── Parties ─────────────────────────────────────────────────────────────
    party_a: PartyDetails = field(default_factory=lambda: PartyDetails(
        name="Alpha Corp S.A.", short_name="Alpha", role="fixed_payer",
        jurisdiction="England"
    ))
    party_b: PartyDetails = field(default_factory=lambda: PartyDetails(
        name="Beta Fund Ltd", short_name="Beta", role="floating_payer",
        jurisdiction="England"
    ))

    # ── Economic Terms — §2 ISDA / §2-4 Confirmation ────────────────────────
    notional: Decimal = Decimal("10000000")     # EUR 10,000,000
    currency: str = "EUR"
    effective_date: date = date(2026, 3, 15)
    termination_date: date = date(2028, 3, 15)

    # ── Fixed Leg — §3 SLC ───────────────────────────────────────────────────
    fixed_rate: Decimal = Decimal("0.03200")    # 3.200% p.a.
    fixed_day_count: str = "30/360"             # Bond Basis
    fixed_frequency_months: int = 3             # Quarterly

    # ── Floating Leg — §4 SLC ───────────────────────────────────────────────
    floating_index: str = "EURIBOR 3M"
    floating_spread: Decimal = Decimal("0")     # bps → converted below
    floating_day_count: str = "ACT/360"         # Market standard for EURIBOR
    floating_frequency_months: int = 3

    # ── Oracle Configuration ─────────────────────────────────────────────────
    oracle_source: str = "ECB_SDW"
    oracle_fallback_rate: Decimal = Decimal("0.02850")  # ISDA 2021 Protocol
    oracle_challenge_threshold_bps: Decimal = Decimal("5")  # 5 basis points

    # ── Schedule Elections ────────────────────────────────────────────────────
    # These map to specific Parts of the ISDA Schedule
    mtpn_elected: bool = True           # Multiple Transaction Payment Netting §2(c) / Part 4(i)
    governing_law: str = "English Law"  # Schedule Part 4(h) → §13
    termination_currency: str = "EUR"   # Schedule Part 1(f) → §6(e)(ii)
    automatic_early_termination: bool = False  # Part 1(e) → §6(a)

    # Elective EoD provisions
    cross_default_elected: bool = False          # Part 1(c) — §5(a)(vi)
    cross_default_threshold: Optional[Decimal] = None  # If elected
    csa_elected: bool = False                    # Credit Support Annex

    # ── Grace Periods (§5 ISDA 2002) ─────────────────────────────────────────
    # These are ISDA standard defaults — do not modify without legal advice
    grace_period_failure_to_pay_days: int = 1    # §5(a)(i): 1 Local Business Day
    grace_period_breach_days: int = 30           # §5(a)(ii): 30 calendar days
    waiting_period_illegality_days: int = 3      # §5(b)(i): 3 Local Business Days
    waiting_period_force_majeure_days: int = 8   # §5(b)(ii): 8 Local Business Days

    # ── Default Rate §9(h)(i)(1) ISDA 2002 ──────────────────────────────────
    # = payee's cost of funding + 1% p.a. (§14 definition).
    # Default proxy: 5% funding cost + 1% = 6%. Override per Schedule Part 6.
    default_rate: Decimal = Decimal("0.06")

    # ── Calculation Agent ────────────────────────────────────────────────────
    calculation_agent: str = "Party A"           # §14 Definition


@dataclass
class ScheduleElections:
    """
    The ISDA Schedule — negotiated ONCE between two counterparties.

    WHAT THIS IS:
    ─────────────
    The Schedule modifies and supplements the printed form ISDA 2002 MA.
    It is negotiated by the LAWYERS, not the trading desk. Once signed,
    it covers ALL future Transactions between these two parties.

    The SwapParameters (above) is the CONFIRMATION — the per-trade
    economic terms. Confirmation > Schedule > MA per §1(b).

    ISDA SCHEDULE STRUCTURE:
    ────────────────────────
    Part 1: Termination Provisions
      (a) Specified Entities — §5(a)(v)/(vi)/(vii), §5(b)(v)
      (b) Specified Transaction — §5(a)(v)
      (c) Cross-Default — §5(a)(vi) elected? threshold?
      (d) Cross-Default Threshold — amount
      (e) Automatic Early Termination — §6(a)
      (f) Termination Currency — §6(e)
      (g) Additional Termination Events — Part 5

    Part 2: Tax Representations
      (a) Payer Tax Representations — §3(e)
      (b) Payee Tax Representations — §3(f)

    Part 3: Agreement to Deliver (§4(a) documents)
      Tax forms, financial statements, legal opinions, etc.
      Each with: Party, Form/Document, Date, Covered by §3(d)?

    Part 4: Miscellaneous
      (a) Addresses for Notices — §12
      (b) Process Agent — §13(c)
      (c) Offices — §10
      (d) Multibranch Party — §10
      (e) Calculation Agent — §14
      (f) Credit Support Document — §3(a)(iii)
      (g) Credit Support Provider — §3(a)(iii)
      (h) Governing Law — §13
      (i) Netting of Payments — §2(c) MTPN
      (j) Affiliate — §14
      (k) Absence of Litigation — §3(c) (Specified Entities?)
      (l) No Agency — §3(g)
      (m) Additional Representation — "Relationship Between Parties"

    Part 5: Other Provisions
      Non-reliance, waiver of jury trial, recording of conversations,
      additional definitions, etc.

    CSA (Credit Support Annex) — separate document but referenced here.
    """

    # ── Identification ──────────────────────────────────────────────────────
    schedule_id: str = ""                # auto-generated from party names
    date_of_agreement: Optional[date] = None   # Date Schedule was executed
    master_agreement_date: Optional[date] = None  # Date MA was executed (often same as date_of_agreement; MA is the printed form)
    status: str = "DRAFT"               # DRAFT → NEGOTIATING → AGREED → SIGNED

    # ── Part 1: Termination Provisions ────────────────────────────────────
    # (a) Specified Entities
    specified_entities_party_a: List[str] = field(default_factory=list)  # for §5(a)(v)/(vi)/(vii)
    specified_entities_party_b: List[str] = field(default_factory=list)

    # (c)-(d) Cross-Default §5(a)(vi)
    cross_default_party_a: bool = False
    cross_default_party_b: bool = False
    cross_default_threshold_a: Optional[Decimal] = None  # e.g. USD 1,000,000
    cross_default_threshold_b: Optional[Decimal] = None

    # (e) Automatic Early Termination §6(a)
    aet_party_a: bool = False
    aet_party_b: bool = False

    # (f) Termination Currency
    termination_currency: str = "EUR"

    # ── Part 2: Tax Representations ───────────────────────────────────────
    payer_tax_rep_party_a: str = "Standard"      # "Standard" / "None" / custom text
    payer_tax_rep_party_b: str = "Standard"
    payee_tax_rep_party_a: str = "None"          # Treaty / Effectively Connected / US Person / None
    payee_tax_rep_party_b: str = "None"

    # ── Part 3: Agreement to Deliver ──────────────────────────────────────
    # Each item: (party, document, deadline_rule, covered_by_3d)
    # Auto-populated but editable by lawyer
    documents_to_deliver: List[dict] = field(default_factory=lambda: [
        {"party": "BOTH", "document": "Tax forms (W-8BEN/W-8BEN-E or equivalent)",
         "deadline": "Upon execution and annually thereafter", "s3d": True},
        {"party": "BOTH", "document": "Annual audited financial statements",
         "deadline": "120 days after fiscal year end", "s3d": True},
        {"party": "BOTH", "document": "Compliance certificate",
         "deadline": "With each set of financial statements", "s3d": True},
        {"party": "BOTH", "document": "Authorising resolutions / board minutes",
         "deadline": "Upon execution", "s3d": False},
        {"party": "BOTH", "document": "Legal opinion (capacity and enforceability)",
         "deadline": "Upon execution", "s3d": False},
    ])

    # ── Part 4: Miscellaneous ─────────────────────────────────────────────
    governing_law: str = "English Law"           # Part 4(h) → §13
    mtpn_elected: bool = True                    # Part 4(i) → §2(c)
    mtpn_effective_date: Optional[date] = None   # From when MTPN applies
    calculation_agent: str = "Party A"           # Part 4(e) → §14
    process_agent_party_a: str = ""              # Part 4(b) → §13(c)
    process_agent_party_b: str = ""
    no_agency_elected: bool = True               # Part 4(l) → §3(g)
    relationship_between_parties: bool = True    # Part 4(m)

    # ── Addresses for Notices — §12 ───────────────────────────────────────
    notice_address_party_a: str = ""
    notice_email_party_a: str = ""
    notice_address_party_b: str = ""
    notice_email_party_b: str = ""

    # ── Part 5: Other Provisions ──────────────────────────────────────────
    non_reliance: bool = True
    waiver_of_jury_trial: bool = True            # NY law only
    recording_of_conversations: bool = True
    additional_provisions: List[str] = field(default_factory=list)

    # ── CSA Parameters (if applicable) ────────────────────────────────────
    csa_elected: bool = False
    csa_type: str = "VM_ONLY"                    # VM_ONLY / VM_AND_IM / NONE
    csa_threshold_party_a: Optional[Decimal] = None    # e.g. EUR 500,000
    csa_threshold_party_b: Optional[Decimal] = None
    csa_mta: Optional[Decimal] = None                  # Minimum Transfer Amount
    csa_rounding: Optional[Decimal] = None             # e.g. EUR 10,000
    csa_eligible_collateral: List[str] = field(default_factory=lambda: ["Cash (EUR)"])
    csa_haircuts: Dict[str, Decimal] = field(default_factory=dict)  # e.g. {"G7 Govt Bonds": Decimal("0.02")}
    csa_valuation_frequency: str = "Daily"             # Daily / Weekly
    csa_notification_time: str = "13:00 London time"

    def to_dict(self) -> dict:
        """Serialize for audit trail / PDF generation."""
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, Decimal):
                d[k] = str(v)
            elif isinstance(v, date):
                d[k] = str(v)
            elif isinstance(v, list):
                d[k] = v
            elif isinstance(v, dict):
                d[k] = {kk: str(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()}
            else:
                d[k] = v
        return d


@dataclass
class ContractInitiation:
    """
    Tracks who initiated what and when in the contract lifecycle.

    FLOW:
    ─────
    1. LAWYER configures the Schedule (ScheduleElections)
       → clause builder in advisor portal
       → Status: DRAFT → NEGOTIATING → AGREED → SIGNED

    2. CLIENT or LAWYER initiates a new Transaction (SwapParameters)
       → "New Swap" in client portal or advisor portal
       → Populates the Confirmation economic terms

    3. LAWYER reviews and validates the Confirmation
       → Checks consistency with Schedule
       → Runs netting opinion check
       → Generates Confirmation PDF

    4. BOTH PARTIES sign the Confirmation
       → eIDAS QES (production) / SHA-256 hash (prototype)
       → Confirmation hash recorded in audit trail

    5. ENGINE initialises and begins lifecycle management
       → Schedule obligations auto-scheduled
       → First oracle fetch
       → Contract state → ACTIVE
    """
    initiated_by: str = ""              # "CLIENT" or "ADVISOR"
    initiated_date: Optional[date] = None
    schedule_ref: str = ""              # Reference to the MA+Schedule between the parties
    validated_by: str = ""              # Lawyer who validated
    validated_date: Optional[date] = None
    signed_party_a: bool = False
    signed_party_a_date: Optional[date] = None
    signed_party_a_hash: str = ""       # SHA-256 of signature
    signed_party_b: bool = False
    signed_party_b_date: Optional[date] = None
    signed_party_b_hash: str = ""
    confirmation_hash: str = ""         # SHA-256 of the Confirmation PDF
    status: str = "INITIATED"           # INITIATED → VALIDATED → SIGNED → ACTIVE


@dataclass
class OracleReading:
    """
    A single oracle rate fetch result.
    Every reading is recorded in the audit trail with its source and timestamp.
    """
    rate: Decimal
    status: OracleStatus
    source: str
    fetch_timestamp: str
    publication_date: Optional[str] = None
    raw_response_hash: Optional[str] = None     # SHA-256 of raw API response


@dataclass
class CalculationPeriod:
    """
    One quarterly calculation period of the swap.
    Maps to the payment schedule in the Confirmation.
    """
    period_number: int
    start_date: date
    end_date: date
    payment_date: date                          # Usually = end_date (Modified Following)

    # Oracle
    oracle_reading: Optional[OracleReading] = None

    # Calculated amounts
    fixed_amount: Optional[Decimal] = None      # Party A pays this
    floating_amount: Optional[Decimal] = None   # Party B pays this
    net_amount: Optional[Decimal] = None        # After §2(c) netting
    net_payer: Optional[NetPayer] = None

    # Execution state
    payment_instruction_issued: bool = False
    payment_confirmed: bool = False             # Human approval required
    suspended: bool = False                     # §2(a)(iii): withheld due to EoD

    # Audit
    calculation_fingerprint: Optional[str] = None
    default_interest_accrued: Optional[Decimal] = None  # §9(h)(i)(1) Default Rate


@dataclass
class EventOfDefaultRecord:
    """
    A logged instance of an Event of Default or Potential Event of Default.
    Every EoD detection is written to the audit trail immediately.
    """
    eod_type: EventOfDefault
    detecting_party: str                        # "ENGINE" or "CALCULATION_AGENT"
    detected_date: date
    affected_party: DefaultingParty
    description: str                            # Human-readable description
    isda_reference: str                         # e.g. "§5(a)(i) ISDA 2002"
    grace_period_end: Optional[date] = None     # If grace period applies
    grace_period_expired: bool = False
    notice_given: bool = False                  # Has notice been given? §5(a)(i)
    early_termination_designated: bool = False  # §6(a) notice issued
    is_potential_eod: bool = True               # Becomes False after grace expires
    # §2(a)(iii): cured=True when the underlying condition is remedied.
    # Only PEoDs can be cured this way; full EoDs require §6 close-out.
    cured: bool = False


@dataclass
class TerminationEventRecord:
    """
    A logged instance of a Termination Event.
    Distinct from EoD: affects only Affected Transactions, not fault-based.
    """
    te_type: TerminationEvent
    affected_party: str                         # "PARTY_A", "PARTY_B", or "BOTH"
    detected_date: date
    description: str
    isda_reference: str
    waiting_period_end: Optional[date] = None   # §5(d) deferral during waiting period
    waiting_period_expired: bool = False
    transfer_attempted: bool = False            # §6(b)(ii) for Tax Events
    early_termination_designated: bool = False


@dataclass
class CloseOutCalculation:
    """
    The §6 ISDA 2002 close-out waterfall.

    Three components per §37 User Guide:
    (i)  Unpaid Amounts: obligations that became due but were not paid
    (ii) Unpaid Amounts: obligations that would have been due but for EoD/ETD
    (iii) Close-out Amount: future value of Terminated Transactions

    Formula (§6(e)(ii)):
      Early Termination Amount = Close-out Amount (net) + Unpaid Amounts (net)
    """
    trigger: str                                # EoD or TE type
    determining_party: str                      # "PARTY_A" or "PARTY_B"
    early_termination_date: date
    terminated_transactions: List[str]

    # Close-out Amount §6(e)(i) — future value
    close_out_amount_party_a: Optional[Decimal] = None   # A's determination
    close_out_amount_party_b: Optional[Decimal] = None   # B's determination

    # Unpaid Amounts §9(h)(ii) — past obligations
    unpaid_amounts_owed_to_a: Decimal = Decimal("0")
    unpaid_amounts_owed_to_b: Decimal = Decimal("0")

    # Default interest on unpaid amounts §9(h)(i)(1)
    # Default Rate = Payee's cost of funding + 1% p.a. (§14)
    default_interest_rate: Decimal = Decimal("0.06")     # Proxy: 5% funding + 1%

    # Final waterfall (User Guide p.38 Example)
    early_termination_amount: Optional[Decimal] = None
    payable_by: Optional[str] = None            # "PARTY_A" or "PARTY_B"

    # Indicative MTM for Close-out Amount (simplified)
    mtm_rate_used: Optional[Decimal] = None
    calculation_method: str = "REPLACEMENT_COST"  # §6(e)(i): replacement cost basis

    calculation_timestamp: str = ""
    calculation_fingerprint: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1: ORACLE — fetches EURIBOR 3M from ECB SDW
#
# BACKWARD-COMPATIBILITY SHIM
# ───────────────────────────
# OracleModule below is kept intact for existing IRS engine code.
# New code should use oracle_v3.OracleV3 directly, which covers
# EURIBOR 3M/6M/12M, €STR, EUR swap rates, FX rates, event monitoring,
# and regulatory watch.
#
# The _v3 attribute on each OracleModule instance gives callers access to
# the shared OracleV3 singleton when they need multi-rate or event data.
# ─────────────────────────────────────────────────────────────────────────────

# Lazy import — oracle_v3 is optional (graceful degradation if absent)
try:
    from oracle_v3 import OracleV3 as _OracleV3, RateID as _RateID
    _ORACLE_V3_AVAILABLE = True
except ImportError:
    _ORACLE_V3_AVAILABLE = False

# Shared singleton — one OracleV3 per process, lazy-initialised
_oracle_v3_singleton: "Optional[_OracleV3]" = None  # type: ignore[name-defined]

def get_oracle_v3() -> "Optional[_OracleV3]":  # type: ignore[name-defined]
    """Return (or create) the shared OracleV3 singleton."""
    global _oracle_v3_singleton
    if not _ORACLE_V3_AVAILABLE:
        return None
    if _oracle_v3_singleton is None:
        import os
        _oracle_v3_singleton = _OracleV3(newsapi_key=os.environ.get("NEWSAPI_KEY"))
    return _oracle_v3_singleton


class OracleModule:
    """
    ORACLE v2 — Multi-source EURIBOR 3M with ISDA 2021 Fallback Waterfall.

    SOURCES (priority order):
    ─────────────────────────
    1. ECB SDW — Primary source (FM/B.U2.EUR.RT0.MM.EURIBOR3MD_.HSTA)
    2. EMMI (EURIBOR administrator) — Secondary source (future: API integration)
    3. ISDA 2021 Benchmark Fallback — €STR + Adjustment Spread

    ANOMALY DETECTION:
    ──────────────────
    If the fetched rate deviates by more than the configured threshold from
    the last confirmed rate, the oracle flags ANOMALY and requires Calculation
    Agent review (HUMAN GATE). This prevents erroneous rates from propagating.

    ISDA 2021 FALLBACK WATERFALL (simplified):
    ───────────────────────────────────────────
    Step 1: Try primary source (ECB SDW)
    Step 2: Try secondary source (EMMI — stubbed for now)
    Step 3: Calculate from €STR + adjustment spread (ISDA 2021 Protocol)
    Step 4: Use last confirmed rate + 1 day interpolation
    Step 5: Static fallback rate (configured)

    Each step is logged in the oracle history with its source and status.
    """

    ECB_URL = (
        "https://data-api.ecb.europa.eu/service/data/"
        "FM/B.U2.EUR.RT0.MM.EURIBOR3MD_.HSTA"
        "?lastNObservations=1&format=jsondata"
    )

    def __init__(self, params: SwapParameters):
        self.params = params
        self.history: List[OracleReading] = []
        self.last_confirmed_rate: Optional[Decimal] = None
        self._anomaly_threshold_bps = params.oracle_challenge_threshold_bps  # 5 bps default
        # ISDA 2021 Fallback: €STR + spread adjustment
        # Spread = 0.0959% (ISDA IBOR Fallbacks, EURIBOR 3M median)
        self._estr_fallback_spread = Decimal("0.000959")
        # v3 delegate — access via self._v3 for multi-rate / event / regulatory data
        self._v3 = get_oracle_v3()

    def fetch(self) -> OracleReading:
        """
        Multi-source fetch with ISDA 2021 fallback waterfall.
        Logs every attempt in oracle history.
        """
        print(f"  [ORACLE] Fetching EURIBOR 3M...")

        # ── Step 1: ECB SDW (primary) ─────────────────────────────────────
        reading = self._fetch_ecb()
        if reading and reading.status == OracleStatus.CONFIRMED:
            reading = self._check_anomaly(reading)
            self._record(reading)
            return reading

        # ── Step 2: EMMI (secondary — stubbed) ────────────────────────────
        print(f"  [ORACLE] ECB unavailable → trying EMMI (secondary)...")
        reading_emmi = self._fetch_emmi()
        if reading_emmi and reading_emmi.status == OracleStatus.CONFIRMED:
            reading_emmi = self._check_anomaly(reading_emmi)
            self._record(reading_emmi)
            return reading_emmi

        # ── Step 3: €STR + Adjustment Spread (ISDA 2021) ──────────────────
        print(f"  [ORACLE] EMMI unavailable → applying ISDA 2021 fallback (€STR + spread)...")
        reading_estr = self._fallback_estr()
        if reading_estr:
            self._record(reading_estr)
            return reading_estr

        # ── Step 4: Last confirmed + interpolation ─────────────────────────
        if self.last_confirmed_rate:
            print(f"  [ORACLE] Using last confirmed rate: {self.last_confirmed_rate*100:.3f}%")
            reading = OracleReading(
                rate=self.last_confirmed_rate,
                status=OracleStatus.FALLBACK,
                source="LAST_CONFIRMED_INTERPOLATION",
                fetch_timestamp=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            )
            self._record(reading)
            return reading

        # ── Step 5: Static fallback ────────────────────────────────────────
        fb_rate = self.params.oracle_fallback_rate
        print(f"  [ORACLE] All sources exhausted → static fallback: {fb_rate*100:.3f}%")
        reading = OracleReading(
            rate=fb_rate,
            status=OracleStatus.FALLBACK,
            source="ISDA_2021_STATIC_FALLBACK",
            fetch_timestamp=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        )
        self._record(reading)
        return reading

    def _fetch_ecb(self) -> Optional[OracleReading]:
        """Primary: ECB Statistical Data Warehouse."""
        try:
            req = urllib.request.Request(
                self.ECB_URL, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                raw_bytes = response.read()
                raw_str = raw_bytes.decode("utf-8")
                data = json.loads(raw_str)

            series = data["dataSets"][0]["series"]["0:0:0:0:0:0:0"]["observations"]
            last_obs = list(series.values())[-1]
            rate_pct = Decimal(str(last_obs[0]))
            rate_decimal = rate_pct / Decimal("100")

            time_dims = data["structure"]["dimensions"]["observation"]
            pub_date = list(time_dims[0]["values"])[-1].get("id", "unknown")
            raw_hash = hashlib.sha256(raw_bytes).hexdigest()

            reading = OracleReading(
                rate=rate_decimal,
                status=OracleStatus.CONFIRMED,
                source="ECB_SDW",
                fetch_timestamp=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                publication_date=pub_date,
                raw_response_hash=raw_hash
            )
            print(f"  [ORACLE] ✓ ECB CONFIRMED: {rate_pct:.3f}% (pub: {pub_date})")
            return reading
        except Exception as e:
            print(f"  [ORACLE] ✗ ECB failed: {type(e).__name__}")
            return None

    def _fetch_emmi(self) -> Optional[OracleReading]:
        """Secondary: EMMI (European Money Markets Institute) — EURIBOR administrator.
        STUBBED: In production, this would call the EMMI API."""
        # Future: https://www.emmi-benchmarks.eu/euribor-org/euribor-rates.html
        return None

    def _fallback_estr(self) -> Optional[OracleReading]:
        """ISDA 2021 Fallback: €STR compounded in arrears + adjustment spread.
        SIMPLIFIED: uses the static fallback rate + spread as proxy.
        In production: fetch €STR from ECB, compound over the period, add spread."""
        estr_base = Decimal("0.02891")  # €STR rate (proxy — in production: fetched)
        rate = estr_base + self._estr_fallback_spread
        reading = OracleReading(
            rate=rate,
            status=OracleStatus.FALLBACK,
            source="ISDA_2021_ESTR_PLUS_SPREAD",
            fetch_timestamp=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        )
        print(f"  [ORACLE] ✓ FALLBACK: €STR {estr_base*100:.3f}% + "
              f"spread {self._estr_fallback_spread*100:.4f}% = {rate*100:.3f}%")
        return reading

    def _check_anomaly(self, reading: OracleReading) -> OracleReading:
        """Check if the rate is anomalous vs last confirmed rate."""
        if self.last_confirmed_rate is not None:
            diff_bps = abs(reading.rate - self.last_confirmed_rate) * Decimal("10000")
            if diff_bps > self._anomaly_threshold_bps * Decimal("10"):
                # More than 10x the challenge threshold = serious anomaly
                print(f"  [ORACLE] 🚨 ANOMALY: {diff_bps:.1f} bps deviation from last rate. "
                      f"HUMAN GATE required.")
                reading.status = OracleStatus.CHALLENGED
                return reading
            elif diff_bps > self._anomaly_threshold_bps:
                print(f"  [ORACLE] ⚠ Rate deviation: {diff_bps:.1f} bps (threshold: "
                      f"{self._anomaly_threshold_bps} bps). Within tolerance.")
        return reading

    def _record(self, reading: OracleReading):
        """Record reading in history and update last confirmed."""
        self.history.append(reading)
        if reading.status == OracleStatus.CONFIRMED:
            self.last_confirmed_rate = reading.rate

    def challenge_rate(self, submitted_rate: Decimal, challenged_rate: Decimal) -> bool:
        """Validates whether a rate challenge is within threshold."""
        diff_bps = abs(submitted_rate - challenged_rate) * Decimal("10000")
        return diff_bps > self.params.oracle_challenge_threshold_bps

    def oracle_summary(self) -> dict:
        """Summary for audit trail and portals."""
        last = self.history[-1] if self.history else None
        return {
            "current_rate": str(last.rate) if last else None,
            "status": last.status.value if last else "NOT_FETCHED",
            "source": last.source if last else None,
            "fetch_count": len(self.history),
            "last_confirmed": str(self.last_confirmed_rate) if self.last_confirmed_rate else None,
            "sources_attempted": list(set(r.source for r in self.history)),
            "anomalies_detected": sum(1 for r in self.history if r.status == OracleStatus.CHALLENGED),
        }


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 2: DAY COUNT CONVENTIONS
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1B: BUSINESS DAY CALENDAR — TARGET2 + London
# ─────────────────────────────────────────────────────────────────────────────

class BusinessDayCalendar:
    """
    Implements the Business Day Convention per ISDA 2006 Definitions §4.12.

    WHAT THIS CLASS DOES:
    ─────────────────────
    1. Determines whether a date is a Business Day for EUR payments (TARGET2)
       and/or GBP payments (London)
    2. Adjusts dates per Modified Following Business Day Convention (§4.12(ii)):
       → move to next Business Day, UNLESS that crosses a month-end,
         in which case move to the preceding Business Day
    3. Computes "n Local Business Days" for grace periods (§5(a)(i): 1 LBD,
       §5(b)(i): 3 LBDs, §5(b)(ii): 8 LBDs)

    LEGAL BASIS:
    ────────────
    - TARGET2: ECB operating rules. Closed: weekends, 1 Jan, Good Friday,
      Easter Monday, 1 May, 25 Dec, 26 Dec.
    - London: Bank of England settlement calendar. Closed: weekends,
      1 Jan, Good Friday, Easter Monday, first Monday May, last Monday May,
      last Monday Aug, 25 Dec, 26 Dec (+ substitute rules).
    - LMA User Guide: "Business Day" definition requires both London
      and TARGET2 to be open for EUR payments.

    SOURCES:
    ────────
    - ECB TARGET2 closing days: https://www.ecb.europa.eu/paym/target/target2/profuse/calendar/html/index.en.html
    - Bank of England: https://www.bankofengland.co.uk/boeapps/database/BankHolidays.asp
    """

    # ── Easter algorithm (Anonymous Gregorian) ────────────────────────────
    @staticmethod
    def _easter_sunday(year: int) -> date:
        """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
        a = year % 19
        b, c = divmod(year, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month, day = divmod(h + l - 7 * m + 114, 31)
        return date(year, month, day + 1)

    # ── TARGET2 holidays ──────────────────────────────────────────────────
    @classmethod
    def target2_holidays(cls, year: int) -> set:
        """
        TARGET2 closing days per ECB rules.
        Fixed: 1 Jan, 1 May, 25 Dec, 26 Dec.
        Moveable: Good Friday, Easter Monday.
        """
        easter = cls._easter_sunday(year)
        good_friday = easter - timedelta(days=2)
        easter_monday = easter + timedelta(days=1)
        return {
            date(year, 1, 1),     # New Year's Day
            good_friday,           # Good Friday
            easter_monday,         # Easter Monday
            date(year, 5, 1),     # Labour Day
            date(year, 12, 25),   # Christmas Day
            date(year, 12, 26),   # 26 December
        }

    # ── London (England & Wales) bank holidays ────────────────────────────
    @classmethod
    def london_holidays(cls, year: int) -> set:
        """
        England & Wales bank holidays per Bank of England calendar.
        Includes substitute day rule: if 25/26 Dec or 1 Jan falls on
        weekend, the next Monday (and Tuesday if needed) is the holiday.
        """
        easter = cls._easter_sunday(year)
        good_friday = easter - timedelta(days=2)
        easter_monday = easter + timedelta(days=1)

        holidays = {
            good_friday,
            easter_monday,
        }

        # ── First Monday in May ──
        d = date(year, 5, 1)
        while d.weekday() != 0:  # 0 = Monday
            d += timedelta(days=1)
        holidays.add(d)

        # ── Last Monday in May (Spring Bank Holiday) ──
        d = date(year, 5, 31)
        while d.weekday() != 0:
            d -= timedelta(days=1)
        holidays.add(d)

        # ── Last Monday in August (Summer Bank Holiday) ──
        d = date(year, 8, 31)
        while d.weekday() != 0:
            d -= timedelta(days=1)
        holidays.add(d)

        # ── Christmas / Boxing Day / New Year with substitute rules ──
        # 25 Dec
        xmas = date(year, 12, 25)
        boxing = date(year, 12, 26)
        if xmas.weekday() == 5:      # Saturday → Mon 27 + Tue 28
            holidays.add(date(year, 12, 27))
            holidays.add(date(year, 12, 28))
        elif xmas.weekday() == 6:    # Sunday → Mon 26 is Boxing, Tue 27 substitute
            holidays.add(date(year, 12, 26))
            holidays.add(date(year, 12, 27))
        else:
            holidays.add(xmas)
            if boxing.weekday() == 5:     # Boxing on Saturday → Mon 28
                holidays.add(date(year, 12, 28))
            elif boxing.weekday() == 6:   # Boxing on Sunday → Mon 28
                holidays.add(date(year, 12, 28))
            else:
                holidays.add(boxing)

        # 1 Jan (next year's holiday affects this year's schedule)
        nyd = date(year, 1, 1)
        if nyd.weekday() == 5:       # Saturday → Mon 3
            holidays.add(date(year, 1, 3))
        elif nyd.weekday() == 6:     # Sunday → Mon 2
            holidays.add(date(year, 1, 2))
        else:
            holidays.add(nyd)

        return holidays

    # ── Combined calendar ──────────────────────────────────────────────────
    def __init__(self, calendars: List[str] = None):
        """
        Args:
            calendars: list of calendar codes. Default: ["TARGET2", "LONDON"]
                       For EUR IRS, both must be open (LMA User Guide definition).
        """
        self.calendars = calendars or ["TARGET2", "LONDON"]
        self._cache: Dict[int, set] = {}  # year → combined holidays

    def _holidays_for_year(self, year: int) -> set:
        if year not in self._cache:
            combined = set()
            for cal in self.calendars:
                if cal == "TARGET2":
                    combined |= self.target2_holidays(year)
                elif cal == "LONDON":
                    combined |= self.london_holidays(year)
            self._cache[year] = combined
        return self._cache[year]

    def is_business_day(self, d: date) -> bool:
        """True if d is a Business Day (not weekend, not holiday in any calendar)."""
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        return d not in self._holidays_for_year(d.year)

    def modified_following(self, d: date) -> date:
        """
        Modified Following Business Day Convention (ISDA 2006 Definitions §4.12(ii)).

        Rule: if the date is not a Business Day, move to the NEXT Business Day.
        UNLESS that next Business Day is in a different month, in which case
        move to the PRECEDING Business Day.

        This is the standard convention for EUR IRS (TARGET2 + London).
        """
        original_month = d.month
        # Try following first
        adjusted = d
        while not self.is_business_day(adjusted):
            adjusted += timedelta(days=1)
        # If crossed month boundary, go preceding instead
        if adjusted.month != original_month:
            adjusted = d
            while not self.is_business_day(adjusted):
                adjusted -= timedelta(days=1)
        return adjusted

    def add_business_days(self, d: date, n: int) -> date:
        """
        Add n Local Business Days to a date.
        Used for grace periods: §5(a)(i) = 1 LBD, §5(b)(i) = 3 LBDs,
        §5(b)(ii) = 8 LBDs.
        """
        result = d
        added = 0
        while added < n:
            result += timedelta(days=1)
            if self.is_business_day(result):
                added += 1
        return result


class DayCountModule:
    """
    Implements the two day count conventions used in a vanilla EUR IRS.

    30/360 (Bond Basis) — Fixed Leg
    ────────────────────────────────
    Each month treated as 30 days. Year = 360 days.
    Formula: [360(Y2-Y1) + 30(M2-M1) + min(D2,30) - min(D1,30)] / 360
    Used for: fixed leg, most EUR bond markets.

    Actual/360 — Floating Leg
    ──────────────────────────
    Count actual calendar days. Year = 360 days.
    Formula: actual_days / 360
    Used for: EURIBOR money market convention.

    Note: EURIBOR rates are quoted on an ACT/360 basis per §4 SLC.
    The mismatch between fixed (30/360) and floating (ACT/360) is standard
    and economically neutral on average over full years.
    """

    @staticmethod
    def dcf_30_360(start: date, end: date) -> Decimal:
        """
        30/360 Bond Basis day count fraction.
        Maps to §3 SLC (Fixed Leg day count convention).
        """
        y1, m1, d1 = start.year, start.month, min(start.day, 30)
        y2, m2, d2 = end.year, end.month, min(end.day, 30)
        days = 360*(y2-y1) + 30*(m2-m1) + (d2-d1)
        return Decimal(str(days)) / Decimal("360")

    @staticmethod
    def dcf_act_360(start: date, end: date) -> Decimal:
        """
        Actual/360 day count fraction.
        Maps to §4 SLC (Floating Leg day count convention).
        """
        actual_days = (end - start).days
        return Decimal(str(actual_days)) / Decimal("360")

    @staticmethod
    def add_months(d: date, months: int) -> date:
        """
        Adds months to a date. Handles month-end conventions
        (e.g. 31 Jan + 1 month = 28/29 Feb, not overflow to March).
        Modified Following convention applied at schedule generation.
        """
        month = d.month - 1 + months
        year = d.year + month // 12
        month = month % 12 + 1
        # Handle end-of-month: don't exceed last day of target month
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        day = min(d.day, last_day)
        return date(year, month, day)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 3: CALCULATION ENGINE — §2(c) Netting
# ─────────────────────────────────────────────────────────────────────────────

class CalculationEngine:
    """
    Calculates fixed and floating amounts and applies §2(c) netting.

    §2(c) ISDA 2002 — Netting of Payments (automated):
    ─────────────────────────────────────────────────────
    Payments due on the SAME DATE in the SAME CURRENCY under the SAME
    Transaction are automatically netted to a single net payment.

    Multiple Transaction Payment Netting (MTPN) — if elected in Schedule Part 4(i):
    Netting extends across multiple Transactions on the same date in the same currency.
    We elect MTPN by default (mtpn_elected=True) — the most common market practice.

    This means: instead of Party A paying EUR 80k AND Party B paying EUR 61k,
    there is ONE payment: Party A pays EUR 19k to Party B.
    Reduces settlement exposure and operational risk significantly.
    """

    ROUNDING = Decimal("0.01")   # Round to EUR cents

    def __init__(self, params: SwapParameters):
        self.params = params
        self.dc = DayCountModule()

    def calculate_fixed_amount(self, period: CalculationPeriod) -> Decimal:
        """
        Fixed leg payment by Party A.
        Formula: Notional × Fixed Rate × Day Count Fraction (30/360)
        §3 SLC / §2(a)(i) ISDA 2002
        """
        dcf = self.dc.dcf_30_360(period.start_date, period.end_date)
        amount = self.params.notional * self.params.fixed_rate * dcf
        return amount.quantize(self.ROUNDING, rounding=ROUND_HALF_UP)

    def calculate_floating_amount(self, period: CalculationPeriod,
                                   oracle: OracleReading) -> Decimal:
        """
        Floating leg payment by Party B.
        Formula: Notional × (EURIBOR 3M + Spread) × Day Count Fraction (ACT/360)
        §4 SLC / §2(a)(i) ISDA 2002
        """
        dcf = self.dc.dcf_act_360(period.start_date, period.end_date)
        floating_rate = oracle.rate + (self.params.floating_spread / Decimal("10000"))
        amount = self.params.notional * floating_rate * dcf
        return amount.quantize(self.ROUNDING, rounding=ROUND_HALF_UP)

    def apply_netting(self, fixed_amount: Decimal,
                      floating_amount: Decimal) -> Tuple[Decimal, NetPayer]:
        """
        §2(c) ISDA 2002 — Payment Netting (AUTOMATED)

        Basic §2(c) netting applies to all payments due on the SAME DATE under
        the SAME TRANSACTION in the SAME CURRENCY.  It is unconditional — no
        Schedule election is needed to activate same-transaction netting.

        Multiple Transaction Payment Netting (MTPN) — Schedule Part 4(i):
        When params.mtpn_elected=True, netting extends across ALL Transactions
        between the parties on the same date in the same currency.  For a
        single-Transaction engine this has no additional effect; a multi-
        Transaction caller must aggregate fixed/floating amounts per
        payment-date/currency pair BEFORE calling this method.

        NOTE: this engine is single-Transaction — the MTPN flag is recorded in
        the Schedule but has no effect here.  See params.mtpn_elected.
        """
        # §2(c) same-transaction netting — always applies
        net = (fixed_amount - floating_amount).quantize(self.ROUNDING, rounding=ROUND_HALF_UP)

        if net > Decimal("0"):
            # Party A's fixed obligation > Party B's floating obligation
            # Party A pays the difference
            return net, NetPayer.PARTY_A
        elif net < Decimal("0"):
            # Party B's floating obligation > Party A's fixed obligation
            # Party B pays the difference
            return abs(net), NetPayer.PARTY_B
        else:
            return Decimal("0"), NetPayer.ZERO_NET

    def calculate_default_interest(self, principal: Decimal, days: int,
                                    default_rate: Decimal) -> Decimal:
        """
        §9(h)(i)(1) ISDA 2002 — Default interest on late payments.
        Default Rate = payee's cost of funding + 1% p.a. (§14 ISDA 2002).
        Accrues from the originally scheduled payment date.
        """
        dcf = Decimal(str(days)) / Decimal("360")
        interest = principal * default_rate * dcf
        return interest.quantize(self.ROUNDING, rounding=ROUND_HALF_UP)

    def fingerprint(self, data: dict) -> str:
        """
        SHA-256 fingerprint of a calculation step.
        Ensures audit trail integrity — any tampering changes the fingerprint.
        Both parties can independently verify every calculation.
        """
        canonical = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 4: EVENT OF DEFAULT MONITOR — §5(a) ISDA 2002
# ─────────────────────────────────────────────────────────────────────────────

class EoDMonitor:
    """
    Monitors all 8 Events of Default under §5(a) ISDA 2002.

    WHAT THIS CLASS DOES (AUTOMATED):
    ──────────────────────────────────
    1. Detects Potential Events of Default (PEoDs) as they arise
    2. Tracks grace period expiry for each EoD type
    3. Upgrades PEoDs to full EoDs once grace periods expire
    4. Logs every detection to the audit trail with ISDA reference
    5. Triggers §2(a)(iii) suspension of payment obligations
    6. Notifies the IRSExecutionEngine to halt further calculations

    WHAT REQUIRES HUMAN INPUT (HUMAN GATE):
    ─────────────────────────────────────────
    - Bankruptcy §5(a)(vii): requires external confirmation (court filing, etc.)
    - Merger Without Assumption §5(a)(viii): requires legal verification
    - Misrepresentation §5(a)(iv): requires Calculation Agent assessment
    - Breach of Agreement §5(a)(ii): requires notice to be given by a party
    - Cross-Default §5(a)(vi): requires monitoring of external debt obligations
    - Notice of Early Termination §6(a): Non-defaulting Party must actively designate
    """

    def __init__(self, params: SwapParameters):
        self.params = params
        self.active_eods: List[EventOfDefaultRecord] = []
        self.active_tes: List[TerminationEventRecord] = []
        # §2(a)(iii): suspension is now computed dynamically from active_eods
        # so that cured PEoDs lift the suspension automatically.
        # (No longer a one-way persistent flag.)
        self.cal = BusinessDayCalendar(["TARGET2", "LONDON"])

    # ── §5(a)(i): Failure to Pay ─────────────────────────────────────────────

    def check_failure_to_pay(self, period: CalculationPeriod,
                              today: date) -> Optional[EventOfDefaultRecord]:
        """
        §5(a)(i) — Failure to Pay or Deliver

        Automated check: if a payment_instruction was issued but not confirmed
        by the grace period end (payment_date + 1 LBD), fire this EoD.

        Grace period: 1 Local Business Day after notice.
        Uses BusinessDayCalendar (TARGET2 + London) for LBD computation.

        Note: §5(c)(i) prevents this from being simultaneously an Illegality.
        """
        if not period.payment_instruction_issued:
            return None  # No payment due yet

        # 1 Local Business Day grace period — real calendar
        grace_end = self.cal.add_business_days(
            period.payment_date, self.params.grace_period_failure_to_pay_days)

        # §5(a)(i): EoD fires ON grace_end (≥), not the day after (>).
        # Grace period expires "by the end of the first Local Business Day after
        # notice" — if today IS that day and payment is still unconfirmed, it is
        # a full EoD. BUG WAS: `today > grace_end` fired one day too late.
        if today >= grace_end and not period.payment_confirmed:
            # Determine which party failed based on who the net_payer is
            defaulting = (DefaultingParty.PARTY_A
                          if period.net_payer == NetPayer.PARTY_A
                          else DefaultingParty.PARTY_B)

            rec = EventOfDefaultRecord(
                eod_type=EventOfDefault.FAILURE_TO_PAY,
                detecting_party="ENGINE",
                detected_date=today,
                affected_party=defaulting,
                description=(
                    f"Period {period.period_number}: Net payment of EUR "
                    f"{period.net_amount:,.2f} due {period.payment_date} "
                    f"not confirmed. Grace period (1 LBD) expired {grace_end}."
                ),
                isda_reference="§5(a)(i) ISDA 2002",
                grace_period_end=grace_end,
                grace_period_expired=True,
                is_potential_eod=False  # Grace expired — full EoD
            )
            self._register_eod(rec)
            return rec
        return None

    # ── §5(a)(i) potential: detect before grace expires ──────────────────────

    def detect_potential_failure_to_pay(self, period: CalculationPeriod,
                                         today: date) -> Optional[EventOfDefaultRecord]:
        """
        Potential Event of Default — §5(a)(i) before grace period expires.
        §2(a)(iii): Payment obligations are SUSPENDED even for PEoDs.
        This is the key §2(a)(iii) condition precedent: no EoD OR PEoD.
        """
        if not period.payment_instruction_issued:
            return None
        if today > period.payment_date and not period.payment_confirmed:
            grace_end = self.cal.add_business_days(
                period.payment_date, self.params.grace_period_failure_to_pay_days)
            rec = EventOfDefaultRecord(
                eod_type=EventOfDefault.FAILURE_TO_PAY,
                detecting_party="ENGINE",
                detected_date=today,
                affected_party=(DefaultingParty.PARTY_A
                                if period.net_payer == NetPayer.PARTY_A
                                else DefaultingParty.PARTY_B),
                description=(
                    f"POTENTIAL: Period {period.period_number}: payment overdue. "
                    f"Grace period running until {grace_end}."
                ),
                isda_reference="§5(a)(i) ISDA 2002 (Potential)",
                grace_period_end=grace_end,
                grace_period_expired=False,
                is_potential_eod=True
            )
            self._register_eod(rec)
            return rec
        return None

    # ── §5(a)(ii): Breach of Agreement ───────────────────────────────────────

    def declare_breach_of_agreement(self, defaulting_party: DefaultingParty,
                                     description: str,
                                     today: date,
                                     repudiation: bool = False) -> EventOfDefaultRecord:
        """
        §5(a)(ii)(1): Breach of agreement after 30-day grace period.
        §5(a)(ii)(2): Repudiation of Agreement (NEW in 2002 MA) — no grace period.

        HUMAN INPUT REQUIRED: This must be declared by the Calculation Agent
        or a party, not automatically by the engine (except repudiation).
        """
        if repudiation:
            desc_full = (f"§5(a)(ii)(2) REPUDIATION: {description} "
                         f"[No grace period — immediate EoD]")
            isda_ref = "§5(a)(ii)(2) ISDA 2002 — Repudiation of Agreement"
            grace_end = today
            expired = True
        else:
            desc_full = (f"§5(a)(ii)(1) BREACH: {description} "
                         f"[30-day grace period from notice date]")
            isda_ref = "§5(a)(ii)(1) ISDA 2002 — Breach of Agreement"
            grace_end = today + timedelta(days=self.params.grace_period_breach_days)
            expired = False

        rec = EventOfDefaultRecord(
            eod_type=EventOfDefault.BREACH_OF_AGREEMENT,
            detecting_party="CALCULATION_AGENT",
            detected_date=today,
            affected_party=defaulting_party,
            description=desc_full,
            isda_reference=isda_ref,
            grace_period_end=grace_end,
            grace_period_expired=expired,
            is_potential_eod=not expired
        )
        self._register_eod(rec)
        return rec

    # ── §5(a)(iii): Credit Support Default ───────────────────────────────────

    def declare_credit_support_default(self, defaulting_party: DefaultingParty,
                                        sub_clause: int,
                                        description: str,
                                        today: date) -> Optional[EventOfDefaultRecord]:
        """
        §5(a)(iii): Credit Support Default — 3 sub-triggers.
        Only applies if csa_elected = True in Schedule.

        Sub-clauses:
        (1) Failure to comply with Credit Support Document
        (2) Expiration/termination/cessation of Credit Support Document (NEW 2002)
        (3) Repudiation of Credit Support Document (NEW 2002)
        """
        if not self.params.csa_elected:
            print("  [EoD_MONITOR] §5(a)(iii) Credit Support Default: CSA not elected. Skipped.")
            return None

        rec = EventOfDefaultRecord(
            eod_type=EventOfDefault.CREDIT_SUPPORT_DEFAULT,
            detecting_party="CALCULATION_AGENT",
            detected_date=today,
            affected_party=defaulting_party,
            description=f"§5(a)(iii)({sub_clause}): {description}",
            isda_reference=f"§5(a)(iii)({sub_clause}) ISDA 2002",
            grace_period_end=None,
            grace_period_expired=True,
            is_potential_eod=False
        )
        self._register_eod(rec)
        return rec

    # ── §5(a)(iv): Misrepresentation ─────────────────────────────────────────

    def declare_misrepresentation(self, defaulting_party: DefaultingParty,
                                   representation_section: str,
                                   today: date) -> EventOfDefaultRecord:
        """
        §5(a)(iv): Breach of §3 representations (not tax reps).
        HUMAN INPUT REQUIRED: Calculation Agent must identify the breach.
        Unchanged from 1992 Agreement.
        """
        rec = EventOfDefaultRecord(
            eod_type=EventOfDefault.MISREPRESENTATION,
            detecting_party="CALCULATION_AGENT",
            detected_date=today,
            affected_party=defaulting_party,
            description=(f"§5(a)(iv): Representation under {representation_section} "
                         f"found to be materially inaccurate or misleading."),
            isda_reference="§5(a)(iv) ISDA 2002",
            grace_period_end=None,
            grace_period_expired=True,
            is_potential_eod=False
        )
        self._register_eod(rec)
        return rec

    # ── §5(a)(v): Default Under Specified Transaction ─────────────────────────

    def declare_specified_transaction_default(self, defaulting_party: DefaultingParty,
                                               description: str,
                                               today: date,
                                               delivery_failure: bool = False
                                               ) -> EventOfDefaultRecord:
        """
        §5(a)(v): Default under a Specified Transaction (cross-default lite).

        2002 changes from 1992:
        - Grace period for payment failures: 1 LBD (was 3 LBDs)
        - New clause (3): delivery failures — must result in ALL transactions
          under that master being accelerated/terminated before EoD fires here.

        HUMAN INPUT REQUIRED: Monitoring of external GMRA/GMSLA/etc. agreements.
        """
        clause = "(3) Delivery Failure" if delivery_failure else "(2) Payment Failure"
        grace = self.cal.add_business_days(today, 1)  # 1 LBD (TARGET2 + London)

        rec = EventOfDefaultRecord(
            eod_type=EventOfDefault.DEFAULT_UNDER_SPECIFIED_TRANSACTION,
            detecting_party="CALCULATION_AGENT",
            detected_date=today,
            affected_party=defaulting_party,
            description=f"§5(a)(v){clause}: {description}",
            isda_reference=f"§5(a)(v) ISDA 2002 — Default Under Specified Transaction",
            grace_period_end=grace,
            grace_period_expired=False,
            is_potential_eod=True
        )
        self._register_eod(rec)
        return rec

    # ── §5(a)(vi): Cross-Default ──────────────────────────────────────────────

    def check_cross_default(self, defaulting_party: DefaultingParty,
                             indebtedness_amount: Decimal,
                             today: date) -> Optional[EventOfDefaultRecord]:
        """
        §5(a)(vi): Cross-Default — ELECTIVE, only if elected in Schedule Part 1(c).

        2002 change: Two limbs are AGGREGATED to reach the Threshold Amount.
        Limb (1): Acceleration of Specified Indebtedness
        Limb (2): Payment default on Specified Indebtedness

        HUMAN INPUT REQUIRED: Monitoring of external debt obligations.
        """
        if not self.params.cross_default_elected:
            return None
        if self.params.cross_default_threshold is None:
            return None
        if indebtedness_amount < self.params.cross_default_threshold:
            return None

        rec = EventOfDefaultRecord(
            eod_type=EventOfDefault.CROSS_DEFAULT,
            detecting_party="CALCULATION_AGENT",
            detected_date=today,
            affected_party=defaulting_party,
            description=(
                f"§5(a)(vi): Specified Indebtedness of EUR "
                f"{indebtedness_amount:,.2f} meets or exceeds Threshold Amount "
                f"EUR {self.params.cross_default_threshold:,.2f} (limbs (1)+(2) aggregated)."
            ),
            isda_reference="§5(a)(vi) ISDA 2002 — Cross-Default",
            grace_period_end=None,
            grace_period_expired=True,
            is_potential_eod=False
        )
        self._register_eod(rec)
        return rec

    # ── §5(a)(vii): Bankruptcy ───────────────────────────────────────────────

    def declare_bankruptcy(self, defaulting_party: DefaultingParty,
                            description: str,
                            today: date) -> EventOfDefaultRecord:
        """
        §5(a)(vii): Insolvency events — dissolution, inability to pay debts,
        winding-up, administration, appointment of receiver/liquidator, etc.

        15-day grace period for bonafide disputes over insolvency proceedings.
        HUMAN INPUT REQUIRED: Requires external verification (court filings, etc.)

        Automatic Early Termination (AET) is particularly relevant here:
        AET = Applicable → ETD occurs automatically (no notice needed).
        AET = Not Applicable (our default) → Non-defaulting Party must act.
        """
        grace_end = today + timedelta(days=15)  # 15-day dispute grace period

        rec = EventOfDefaultRecord(
            eod_type=EventOfDefault.BANKRUPTCY,
            detecting_party="CALCULATION_AGENT",
            detected_date=today,
            affected_party=defaulting_party,
            description=f"§5(a)(vii) BANKRUPTCY: {description}",
            isda_reference="§5(a)(vii) ISDA 2002 — Bankruptcy",
            grace_period_end=grace_end,
            grace_period_expired=False,
            is_potential_eod=True
        )
        self._register_eod(rec)
        return rec

    # ── §5(a)(viii): Merger Without Assumption ───────────────────────────────

    def declare_merger_without_assumption(self, defaulting_party: DefaultingParty,
                                           description: str,
                                           today: date) -> EventOfDefaultRecord:
        """
        §5(a)(viii): Merger/consolidation where surviving entity FAILS to assume
        ISDA obligations or Credit Support Document benefits are lost.

        HUMAN INPUT REQUIRED: Legal verification of merger terms.
        """
        rec = EventOfDefaultRecord(
            eod_type=EventOfDefault.MERGER_WITHOUT_ASSUMPTION,
            detecting_party="CALCULATION_AGENT",
            detected_date=today,
            affected_party=defaulting_party,
            description=f"§5(a)(viii) MERGER WITHOUT ASSUMPTION: {description}",
            isda_reference="§5(a)(viii) ISDA 2002",
            grace_period_end=None,
            grace_period_expired=True,
            is_potential_eod=False
        )
        self._register_eod(rec)
        return rec

    # ── §5(b): Termination Events ────────────────────────────────────────────

    def declare_illegality(self, affected_party: str,
                            description: str,
                            today: date) -> TerminationEventRecord:
        """
        §5(b)(i): Illegality — performance becomes unlawful.
        Waiting Period: 3 LBDs before ETD can be designated.
        §5(d): Payments DEFERRED during waiting period.
        §5(c)(i): Excludes simultaneous EoD under §5(a)(i)/(ii)(1)/(iii)(1).
        ISDA 2002 §5(b)(i): waiting period = 3 LOCAL BUSINESS DAYS (not calendar).
        BUG WAS: used timedelta (calendar days). Fix: BusinessDayCalendar.add_business_days.
        """
        waiting_end = self.cal.add_business_days(
            today, self.params.waiting_period_illegality_days)
        rec = TerminationEventRecord(
            te_type=TerminationEvent.ILLEGALITY,
            affected_party=affected_party,
            detected_date=today,
            description=f"§5(b)(i) ILLEGALITY: {description}",
            isda_reference="§5(b)(i) ISDA 2002",
            waiting_period_end=waiting_end,
            waiting_period_expired=False
        )
        self.active_tes.append(rec)
        print(f"  [TE_MONITOR] §5(b)(i) ILLEGALITY declared. Waiting period ends: {waiting_end}")
        return rec

    def declare_force_majeure(self, affected_party: str,
                               description: str,
                               today: date) -> TerminationEventRecord:
        """
        §5(b)(ii): Force Majeure — performance impossible/impracticable.
        Waiting Period: 8 LBDs before ETD can be designated.
        §5(d): Payments DEFERRED — not cancelled — during waiting period.
        ISDA 2002 §5(b)(ii): waiting period = 8 LOCAL BUSINESS DAYS (not calendar).
        BUG WAS: used timedelta (calendar days). Fix: BusinessDayCalendar.add_business_days.
        """
        waiting_end = self.cal.add_business_days(
            today, self.params.waiting_period_force_majeure_days)
        rec = TerminationEventRecord(
            te_type=TerminationEvent.FORCE_MAJEURE,
            affected_party=affected_party,
            detected_date=today,
            description=f"§5(b)(ii) FORCE MAJEURE: {description}",
            isda_reference="§5(b)(ii) ISDA 2002",
            waiting_period_end=waiting_end,
            waiting_period_expired=False
        )
        self.active_tes.append(rec)
        print(f"  [TE_MONITOR] §5(b)(ii) FORCE MAJEURE declared. "
              f"Payments deferred. Waiting period ends: {waiting_end}")
        return rec

    def declare_credit_event_upon_merger(self, affected_party: str,
                                          description: str,
                                          today: date) -> TerminationEventRecord:
        """
        §5(b)(v): Credit Event Upon Merger — materially weaker creditworthiness
        AFTER merger event (immediately before vs. immediately after).
        """
        rec = TerminationEventRecord(
            te_type=TerminationEvent.CREDIT_EVENT_UPON_MERGER,
            affected_party=affected_party,
            detected_date=today,
            description=f"§5(b)(v) CREDIT EVENT UPON MERGER: {description}",
            isda_reference="§5(b)(v) ISDA 2002",
        )
        self.active_tes.append(rec)
        return rec

    def declare_tax_event(self, affected_party: str,
                           description: str,
                           today: date) -> TerminationEventRecord:
        """
        §5(b)(iii): Tax Event — unexpected withholding tax or gross-up obligation.
        §6(b)(ii): Must attempt transfer of Affected Transactions first.
        """
        rec = TerminationEventRecord(
            te_type=TerminationEvent.TAX_EVENT,
            affected_party=affected_party,
            detected_date=today,
            description=f"§5(b)(iii) TAX EVENT: {description}",
            isda_reference="§5(b)(iii) ISDA 2002",
            transfer_attempted=False  # Must be set True after transfer attempt
        )
        self.active_tes.append(rec)
        print(f"  [TE_MONITOR] §5(b)(iii) TAX EVENT. Party must attempt "
              f"transfer of Affected Transactions per §6(b)(ii) before ETD.")
        return rec

    # ── State management ─────────────────────────────────────────────────────

    def _register_eod(self, rec: EventOfDefaultRecord):
        """
        Registers an EoD or PEoD.  Suspension is NOT stored as a persistent flag —
        `is_suspended` is computed dynamically so that cured PEoDs automatically
        lift the §2(a)(iii) condition without any separate 'unsuspend' call.
        §2(a)(iii): "…unless an Event of Default or Potential Event of Default
        with respect to the other party has occurred and is continuing."
        """
        self.active_eods.append(rec)
        status = "POTENTIAL EoD" if rec.is_potential_eod else "EVENT OF DEFAULT"
        print(f"\n  {'⚠' if rec.is_potential_eod else '🚨'} [{status}] {rec.eod_type.value}")
        print(f"     Party: {rec.affected_party.value}")
        print(f"     Ref:   {rec.isda_reference}")
        print(f"     §2(a)(iii): Payment obligations SUSPENDED")
        print(f"     Desc:  {rec.description}")

    @property
    def is_suspended(self) -> bool:
        """
        §2(a)(iii) ISDA 2002 — "conditions precedent" check.

        Returns True only while at least one EoD or PEoD is CONTINUING
        (i.e. not yet cured).  This replaces the original one-way bool flag,
        which incorrectly kept the contract suspended even after a PEoD was
        remedied within the grace period.

        Rules:
        - A Potential EoD (cured=False) → suspended.
        - A Potential EoD (cured=True)  → no longer continuing → not suspended.
        - A full EoD (cured=False)      → suspended (only lifted via §6 close-out).
        """
        return any(not r.cured for r in self.active_eods)

    def cure_potential_eod(
        self,
        eod_type: EventOfDefault,
        affected_party: DefaultingParty,
    ) -> bool:
        """
        Mark the earliest uncured Potential EoD of the given type/party as cured.

        §2(a)(iii): When the underlying condition giving rise to a PEoD is
        remedied (e.g. the overdue payment is received within the grace period),
        the PEoD is no longer 'continuing' and the §2(a)(iii) suspension MUST
        be lifted.  Full EoDs are NOT cured by this method — they require §6
        close-out (designated Early Termination Date).

        Returns True if a PEoD was found and cured, False otherwise.
        """
        for rec in self.active_eods:
            if (rec.is_potential_eod
                    and not rec.cured
                    and rec.eod_type == eod_type
                    and rec.affected_party == affected_party):
                rec.cured = True
                print(
                    f"  [§2(a)(iii)] PEoD {eod_type.value} "
                    f"({affected_party.value}) CURED — §2(a)(iii) suspension lifted"
                    if not self.is_suspended else
                    f"  [§2(a)(iii)] PEoD {eod_type.value} "
                    f"({affected_party.value}) CURED — other EoDs still active"
                )
                return True
        return False

    def has_active_eod(self) -> bool:
        """Returns True if any non-potential (full) EoD is recorded."""
        return any(not r.is_potential_eod for r in self.active_eods)

    def summary(self) -> dict:
        """Summary of all active EoDs and TEs for the audit trail."""
        return {
            "total_eods": len(self.active_eods),
            "full_eods": sum(1 for r in self.active_eods if not r.is_potential_eod),
            "potential_eods": sum(1 for r in self.active_eods if r.is_potential_eod),
            "cured_eods": sum(1 for r in self.active_eods if r.cured),
            "termination_events": len(self.active_tes),
            "suspended": self.is_suspended,   # computed — reflects cured PEoDs
            "eod_types": [r.eod_type.value for r in self.active_eods],
            "te_types": [r.te_type.value for r in self.active_tes],
        }


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 4B: COMPLIANCE MONITOR — §3 Representations + §4 Agreements
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceMonitor:
    """
    Monitors ongoing compliance with §3 Representations and §4 Agreements.

    WHAT THIS MODULE DOES (CONTRACT MONITORING — core Nomos value):
    ──────────────────────────────────────────────────────────────────
    §3 Representations are deemed REPEATED at each payment date (per ISDA 2002
    Section 3 preamble). The engine checks at each cycle whether the conditions
    underlying these representations still hold.

    §4 Agreements impose ongoing obligations (furnish information, maintain
    authorisations, comply with laws). The engine tracks delivery deadlines
    for compliance certificates and specified information.

    If a representation is breached → potential §5(a)(iv) Misrepresentation.
    If an agreement is breached → potential §5(a)(ii) Breach of Agreement.
    Both are HUMAN GATES for the actual EoD declaration, but the engine
    provides the detection, tracking, and evidence for the decision.

    ISDA 2002 REFERENCES:
    ─────────────────────
    §3(a): Basic Representations (status, powers, no violation, consents, binding)
    §3(b): Absence of Certain Events (no EoD/PEoD continuing)
    §3(c): Absence of Litigation
    §3(d): Accuracy of Specified Information
    §3(e): Payer Tax Representations
    §3(f): Payee Tax Representations
    §3(g): No Agency (if elected)
    §4(a): Furnish Specified Information (tax forms, financial statements)
    §4(b): Maintain Authorisations
    §4(c): Comply with Laws
    §4(d): Tax Agreement (notify if withholding applies)
    """

    class RepStatus:
        """Status of a single representation check."""
        SATISFIED = "SATISFIED"           # Rep is true as of check date
        BREACHED = "BREACHED"             # Rep found to be untrue
        UNVERIFIABLE = "UNVERIFIABLE"     # Cannot be checked automatically
        PENDING_REVIEW = "PENDING_REVIEW" # Flagged for human review

    class ObligationStatus:
        """Status of a §4 Agreement obligation."""
        NOT_DUE = "NOT_DUE"              # Deadline not yet reached
        DUE = "DUE"                       # Deadline reached, not yet delivered
        DELIVERED = "DELIVERED"           # Document/info received on time
        OVERDUE = "OVERDUE"              # Deadline passed without delivery
        WAIVED = "WAIVED"                # Obligation waived by counterparty

    @dataclass
    class RepCheck:
        """Result of checking one representation at a given date."""
        section: str          # e.g. "§3(b)"
        name: str             # e.g. "Absence of Certain Events"
        status: str           # RepStatus value
        check_date: date
        auto_checked: bool    # True if engine verified automatically
        detail: str           # Human-readable explanation
        requires_human: bool  # True if HUMAN GATE needed

    @dataclass
    class ObligationTracker:
        """Tracks a single §4 obligation delivery."""
        section: str           # e.g. "§4(a)(i)"
        name: str              # e.g. "Tax Forms"
        party: str             # "PARTY_A" or "PARTY_B"
        due_date: date
        status: str            # ObligationStatus value
        delivered_date: Optional[date] = None
        document_hash: Optional[str] = None  # SHA-256 of delivered document

    def __init__(self, params, eod_monitor):
        """
        Args:
            params: SwapParameters
            eod_monitor: EoDMonitor instance (to check §3(b) — no active EoDs)
        """
        self.params = params
        self.eod_monitor = eod_monitor
        self.rep_history: List = []           # All RepCheck results
        self.obligations: List = []            # All ObligationTracker entries
        self.cal = BusinessDayCalendar(["TARGET2", "LONDON"])

    # ── §3 Representation Checks ──────────────────────────────────────────

    def check_all_reps(self, check_date: date) -> List:
        """
        Run all §3 representation checks for a given date.
        Called at each payment date during the calculation cycle.
        Returns a list of RepCheck results.
        """
        results = []

        # §3(a) Basic Representations — UNVERIFIABLE by engine
        # Status, Powers, No Violation, Consents, Binding obligations
        # These are legal/factual — only the party itself can confirm
        results.append(self.RepCheck(
            section="§3(a)", name="Basic Representations",
            status=self.RepStatus.UNVERIFIABLE, check_date=check_date,
            auto_checked=False,
            detail="Status, Powers, No Violation, Consents, Binding — "
                   "requires party self-certification or legal review.",
            requires_human=True
        ))

        # §3(b) Absence of Certain Events — AUTO-CHECKABLE
        # ISDA 2002 §3(b): "No Event of Default or Potential Event of Default
        # has occurred and is CONTINUING WITH RESPECT TO IT."
        # Each party represents this FOR ITSELF — the rep is breached only for
        # the party that has a continuing EoD/PEoD.
        # BUG WAS: both parties were marked BREACHED when only one had an EoD.
        active_eods = [r for r in self.eod_monitor.active_eods if not r.cured]
        active_tes = self.eod_monitor.active_tes
        party_a_eods = [r for r in active_eods
                        if r.affected_party == DefaultingParty.PARTY_A]
        party_b_eods = [r for r in active_eods
                        if r.affected_party == DefaultingParty.PARTY_B]
        if active_eods or active_tes:
            detail_parts = []
            for eod in party_a_eods:
                detail_parts.append(
                    f"Party A — {eod.eod_type.value} "
                    f"({'PEoD' if eod.is_potential_eod else 'EoD'}) "
                    f"({eod.isda_reference})"
                )
            for eod in party_b_eods:
                detail_parts.append(
                    f"Party B — {eod.eod_type.value} "
                    f"({'PEoD' if eod.is_potential_eod else 'EoD'}) "
                    f"({eod.isda_reference})"
                )
            for te in active_tes:
                detail_parts.append(
                    f"{te.affected_party} — {te.te_type.value} ({te.isda_reference})"
                )
            results.append(self.RepCheck(
                section="§3(b)", name="Absence of Certain Events",
                status=self.RepStatus.BREACHED, check_date=check_date,
                auto_checked=True,
                detail=(
                    f"BREACHED for: {'; '.join(detail_parts)}. "
                    f"§2(a)(iii) circuit breaker is {'active' if self.eod_monitor.is_suspended else 'inactive (events cured)'}."
                ),
                requires_human=False
            ))
        else:
            results.append(self.RepCheck(
                section="§3(b)", name="Absence of Certain Events",
                status=self.RepStatus.SATISFIED, check_date=check_date,
                auto_checked=True,
                detail="No Event of Default or Potential Event of Default is continuing.",
                requires_human=False
            ))

        # §3(c) Absence of Litigation — UNVERIFIABLE by engine
        results.append(self.RepCheck(
            section="§3(c)", name="Absence of Litigation",
            status=self.RepStatus.UNVERIFIABLE, check_date=check_date,
            auto_checked=False,
            detail="No pending/threatened litigation — requires party confirmation "
                   "or public registry check (Companies House, PACER).",
            requires_human=True
        ))

        # §3(d) Accuracy of Specified Information — TRACKABLE
        # Check if all required documents have been delivered
        overdue = [o for o in self.obligations
                   if o.status == self.ObligationStatus.OVERDUE
                   and o.due_date <= check_date]
        if overdue:
            detail = "; ".join([f"{o.name} (due {o.due_date})" for o in overdue])
            results.append(self.RepCheck(
                section="§3(d)", name="Accuracy of Specified Information",
                status=self.RepStatus.PENDING_REVIEW, check_date=check_date,
                auto_checked=True,
                detail=f"OVERDUE documents: {detail}. "
                       f"Specified Information may no longer be accurate. "
                       f"Potential §5(a)(iv) Misrepresentation if material.",
                requires_human=True
            ))
        else:
            results.append(self.RepCheck(
                section="§3(d)", name="Accuracy of Specified Information",
                status=self.RepStatus.SATISFIED, check_date=check_date,
                auto_checked=True,
                detail="All Specified Information delivered within deadline.",
                requires_human=False
            ))

        # §3(e) Payer Tax Representations — UNVERIFIABLE by engine
        results.append(self.RepCheck(
            section="§3(e)", name="Payer Tax Representations",
            status=self.RepStatus.UNVERIFIABLE, check_date=check_date,
            auto_checked=False,
            detail="No withholding required — requires tax counsel verification. "
                   "Repeated per Transaction.",
            requires_human=True
        ))

        # §3(f) Payee Tax Representations — UNVERIFIABLE by engine
        results.append(self.RepCheck(
            section="§3(f)", name="Payee Tax Representations",
            status=self.RepStatus.UNVERIFIABLE, check_date=check_date,
            auto_checked=False,
            detail="Payee tax status — made at all times until termination.",
            requires_human=True
        ))

        # §3(g) No Agency — UNVERIFIABLE by engine (if elected)
        results.append(self.RepCheck(
            section="§3(g)", name="No Agency",
            status=self.RepStatus.UNVERIFIABLE, check_date=check_date,
            auto_checked=False,
            detail="Each party enters as principal — assumed true unless flagged.",
            requires_human=False
        ))

        self.rep_history.extend(results)
        return results

    # ── §4 Obligation Tracking ────────────────────────────────────────────

    def schedule_obligation(self, section: str, name: str, party: str,
                             due_date: date) -> 'ComplianceMonitor.ObligationTracker':
        """
        Schedule a §4 obligation for tracking.
        Called at contract initialisation for recurring obligations,
        or ad-hoc for one-off deliverables.
        """
        ob = self.ObligationTracker(
            section=section, name=name, party=party,
            due_date=due_date, status=self.ObligationStatus.NOT_DUE
        )
        self.obligations.append(ob)
        return ob

    def mark_delivered(self, section: str, party: str,
                        delivered_date: date, document_hash: str = None):
        """Mark an obligation as delivered. Records date and optional doc hash."""
        for ob in self.obligations:
            if ob.section == section and ob.party == party and \
               ob.status in (self.ObligationStatus.NOT_DUE,
                             self.ObligationStatus.DUE,
                             self.ObligationStatus.OVERDUE):
                ob.status = self.ObligationStatus.DELIVERED
                ob.delivered_date = delivered_date
                ob.document_hash = document_hash
                return ob
        return None

    def check_obligations(self, today: date) -> List:
        """
        Update status of all obligations based on today's date.
        Returns list of newly OVERDUE obligations.
        """
        newly_overdue = []
        for ob in self.obligations:
            if ob.status == self.ObligationStatus.NOT_DUE and today >= ob.due_date:
                ob.status = self.ObligationStatus.DUE
            if ob.status == self.ObligationStatus.DUE:
                # Grace: 5 LBDs after due date before flagging OVERDUE
                overdue_date = self.cal.add_business_days(ob.due_date, 5)
                if today > overdue_date:
                    ob.status = self.ObligationStatus.OVERDUE
                    newly_overdue.append(ob)
        return newly_overdue

    # ── Summary ───────────────────────────────────────────────────────────

    def compliance_summary(self, check_date: date) -> dict:
        """
        Produce a compliance summary for audit trail logging.
        """
        reps = self.check_all_reps(check_date)
        overdue = self.check_obligations(check_date)

        satisfied = sum(1 for r in reps if r.status == self.RepStatus.SATISFIED)
        breached = sum(1 for r in reps if r.status == self.RepStatus.BREACHED)
        unverifiable = sum(1 for r in reps if r.status == self.RepStatus.UNVERIFIABLE)
        pending = sum(1 for r in reps if r.status == self.RepStatus.PENDING_REVIEW)

        return {
            "check_date": str(check_date),
            "representations": {
                "total": len(reps),
                "satisfied": satisfied,
                "breached": breached,
                "unverifiable": unverifiable,
                "pending_review": pending,
                "details": [
                    {"section": r.section, "name": r.name,
                     "status": r.status, "auto": r.auto_checked,
                     "detail": r.detail}
                    for r in reps
                ]
            },
            "obligations": {
                "total": len(self.obligations),
                "delivered": sum(1 for o in self.obligations
                                if o.status == self.ObligationStatus.DELIVERED),
                "overdue": len(overdue),
                "overdue_details": [
                    {"section": o.section, "name": o.name,
                     "party": o.party, "due_date": str(o.due_date)}
                    for o in overdue
                ]
            },
            "overall_compliant": breached == 0 and len(overdue) == 0,
            "isda_reference": "§3 Representations (repeated per §3 preamble) + §4 Agreements"
        }

    def print_compliance(self, check_date: date):
        """Print human-readable compliance status."""
        # Use pre-computed summary to avoid double-checking
        self.check_obligations(check_date)
        reps = self.check_all_reps(check_date)

        satisfied = sum(1 for r in reps if r.status == self.RepStatus.SATISFIED)
        breached = sum(1 for r in reps if r.status == self.RepStatus.BREACHED)
        unverifiable = sum(1 for r in reps if r.status == self.RepStatus.UNVERIFIABLE)
        pending = sum(1 for r in reps if r.status == self.RepStatus.PENDING_REVIEW)
        overdue = [o for o in self.obligations
                   if o.status == self.ObligationStatus.OVERDUE]
        delivered = sum(1 for o in self.obligations
                        if o.status == self.ObligationStatus.DELIVERED)
        total_obs = len(self.obligations)

        print(f"\n  ── §3/§4 COMPLIANCE CHECK — {check_date} ──")
        print(f"     Representations: {satisfied} satisfied, "
              f"{breached} breached, "
              f"{unverifiable} unverifiable, "
              f"{pending} pending review")
        print(f"     Obligations:     {delivered} delivered, "
              f"{len(overdue)} overdue out of {total_obs} total")

        if breached > 0:
            for r in reps:
                if r.status == self.RepStatus.BREACHED:
                    print(f"     🚨 {r.section} {r.name}: {r.detail}")

        if len(overdue) > 0:
            for o in overdue:
                print(f"     ⚠ OVERDUE: {o.section} {o.name} "
                      f"({o.party}) due {o.due_date}")

        compliant = breached == 0 and len(overdue) == 0
        status = "● COMPLIANT" if compliant else "○ NON-COMPLIANT — review required"
        print(f"     Overall: {status}")

    # ── Auto-scheduling of recurring §4 obligations ────────────────────────

    def schedule_standard_obligations(self, effective_date: date,
                                        termination_date: date):
        """
        Auto-schedule the standard recurring §4 obligations for an IRS lifecycle.

        Called by IRSExecutionEngine.initialise() — no manual scheduling needed.

        Standard obligations per ISDA 2002 §4 + market practice:
        - §4(a)(i): Tax forms (W-8BEN/W-8BEN-E or equivalent) — annually
        - §4(a)(ii): Financial statements — annually (audited) + semi-annually
        - §4(a)(ii): Compliance certificate — with each set of accounts
        - §4(b): Maintain Authorisations — annual confirmation
        - §4(d): Tax status notification — on change (tracked as annual check)
        """
        from datetime import date as dt

        year = effective_date.year
        end_year = termination_date.year

        for y in range(year, end_year + 1):
            # Annual audited financial statements — due 120 days after FY end
            # Market practice: 4 months after 31 Dec (= 30 April)
            fy_end = date(y, 12, 31)
            if fy_end >= effective_date and fy_end <= termination_date:
                fs_due = date(y + 1, 4, 30) if y + 1 <= end_year + 1 else None
                if fs_due and fs_due <= termination_date + timedelta(days=180):
                    for party in ["PARTY_A", "PARTY_B"]:
                        self.schedule_obligation(
                            "§4(a)(ii)", f"Annual Financial Statements FY{y}",
                            party, fs_due)

            # Compliance certificate — delivered with financial statements
            if fs_due and fs_due <= termination_date + timedelta(days=180):
                for party in ["PARTY_A", "PARTY_B"]:
                    self.schedule_obligation(
                        "§4(a)(ii)", f"Compliance Certificate FY{y}",
                        party, fs_due)

            # Tax forms — annually, due 30 days after effective date anniversary
            tax_due = date(y, effective_date.month, min(effective_date.day, 28))
            if tax_due >= effective_date and tax_due <= termination_date:
                tax_due_adj = tax_due + timedelta(days=30)
                for party in ["PARTY_A", "PARTY_B"]:
                    self.schedule_obligation(
                        "§4(a)(i)", f"Tax Forms / W-8 / Treaty Cert {y}",
                        party, tax_due_adj)

            # §4(b) Maintain Authorisations — annual self-certification
            auth_due = date(y, effective_date.month, min(effective_date.day, 28))
            if auth_due >= effective_date and auth_due <= termination_date:
                for party in ["PARTY_A", "PARTY_B"]:
                    self.schedule_obligation(
                        "§4(b)", f"Authorisations Confirmation {y}",
                        party, auth_due)

        print(f"  [COMPLIANCE] Scheduled {len(self.obligations)} recurring obligations "
              f"({effective_date} → {termination_date})")

    def schedule_from_part3(self, documents: List[dict],
                             effective_date: date, termination_date: date):
        """
        Schedule §4 obligations from Schedule Part 3 (Agreement to Deliver).

        Each document in the list has:
          party: "PARTY_A", "PARTY_B", or "BOTH"
          document: name of the document
          deadline: human-readable deadline rule
          s3d: bool — covered by §3(d) representation?

        The method interprets the deadline rules and generates concrete
        obligation entries with actual due dates.
        """
        year = effective_date.year
        end_year = termination_date.year

        for doc in documents:
            parties = ["PARTY_A", "PARTY_B"] if doc.get("party") == "BOTH" else [doc["party"]]
            deadline = doc.get("deadline", "")
            name = doc.get("document", "Unknown document")
            section = "§4(a)(i)" if "tax" in name.lower() else "§4(a)(ii)"

            for party in parties:
                if "upon execution" in deadline.lower():
                    # One-time delivery at effective date
                    self.schedule_obligation(section, name, party, effective_date)
                elif "annually" in deadline.lower() or "fiscal year" in deadline.lower() \
                        or "financial statements" in deadline.lower():
                    # Annual recurring — 120 days after FY end (31 Dec)
                    for y in range(year, end_year + 1):
                        fy_end = date(y, 12, 31)
                        if fy_end >= effective_date and fy_end <= termination_date:
                            due = date(y + 1, 4, 30)
                            if due <= termination_date + timedelta(days=180):
                                self.schedule_obligation(
                                    section, f"{name} FY{y}", party, due)
                elif "financial statements" in deadline.lower():
                    # Delivered with financial statements
                    for y in range(year, end_year + 1):
                        due = date(y + 1, 4, 30)
                        if due <= termination_date + timedelta(days=180):
                            self.schedule_obligation(
                                section, f"{name} FY{y}", party, due)

        print(f"  [COMPLIANCE] Scheduled {len(self.obligations)} obligations from Part 3 "
              f"({effective_date} → {termination_date})")

    # ── Breach escalation to EoD ──────────────────────────────────────────

    def check_escalation_to_eod(self, today: date) -> List[dict]:
        """
        Check if any overdue §4 obligation should escalate to a potential
        §5(a)(ii) Breach of Agreement.

        ISDA 2002 §5(a)(ii) rule: Breach of any agreement/obligation under
        the MA triggers a PEoD with a 30 calendar day cure period.

        The engine does NOT auto-declare the EoD — it prepares the case
        and flags it for the Calculation Agent (HUMAN GATE).

        Returns a list of escalation recommendations.
        """
        escalations = []
        # §5(a)(ii): cure period is 30 calendar days from the date of notice.
        # The obligation becoming overdue is the event; the escalation threshold
        # must fire ON day 30 (>=), not the day after (>).
        # BUG WAS: `> 30` caused escalation one day too late vs the cure period.
        overdue_severe = [
            o for o in self.obligations
            if o.status == self.ObligationStatus.OVERDUE
            and (today - o.due_date).days >= 30  # §5(a)(ii): ≥30 calendar days
        ]

        for ob in overdue_severe:
            escalations.append({
                "type": "BREACH_ESCALATION",
                "section": ob.section,
                "obligation": ob.name,
                "party": ob.party,
                "due_date": str(ob.due_date),
                "days_overdue": (today - ob.due_date).days,
                "recommended_action": f"§5(a)(ii) Breach of Agreement — "
                    f"{ob.party} has failed to comply with {ob.section} "
                    f"({ob.name}). 30-day grace period under §5(a)(ii) "
                    f"may apply. Calculation Agent should assess materiality "
                    f"and consider issuing notice.",
                "isda_reference": "§5(a)(ii) ISDA 2002",
                "human_gate": True,
                "severity": "HIGH" if (today - ob.due_date).days > 60 else "MEDIUM"
            })

        if escalations:
            print(f"\n  🚨 §5(a)(ii) ESCALATION: {len(escalations)} obligation(s) "
                  f"overdue >30 days — Breach of Agreement review required")
            for esc in escalations:
                print(f"     → {esc['obligation']} ({esc['party']}): "
                      f"{esc['days_overdue']} days overdue · {esc['severity']}")

        return escalations

    # ── §12 Notice generation ─────────────────────────────────────────────

    def generate_notice(self, notice_type: str, from_party: str,
                         to_party: str, details: dict) -> dict:
        """
        Generate a structured notice per §12 ISDA 2002.

        Notice types:
        - OVERDUE_REMINDER: reminder that §4 obligation is due/overdue
        - BREACH_NOTICE: formal §5(a)(ii) notice of breach
        - TAX_CHANGE: §4(d) notification of change in tax status
        - EOD_NOTICE: §6(a) Event of Default notice
        - ETD_DESIGNATION: §6(a) Early Termination Date designation

        The notice is generated as structured data. In production,
        this would feed into a document generation system (email, letter).
        """
        notice = {
            "notice_type": notice_type,
            "isda_reference": "§12 ISDA 2002",
            "from_party": from_party,
            "to_party": to_party,
            "generated_date": str(date.today()),
            "contract_id": self.params.contract_id,
            "details": details,
            "delivery_method": "Electronic (§12(a)(vi) — email)",
            "status": "GENERATED — PENDING HUMAN REVIEW AND SEND",
            "note": "This notice must be reviewed and sent by the Calculation Agent. "
                    "The engine generates the notice; the human sends it. §12 ISDA 2002."
        }

        # Specific content per notice type
        if notice_type == "OVERDUE_REMINDER":
            notice["subject"] = (
                f"Reminder: Delivery of {details.get('obligation', 'Specified Information')} "
                f"under §4(a) ISDA 2002 — {self.params.contract_id}"
            )
        elif notice_type == "BREACH_NOTICE":
            notice["subject"] = (
                f"Notice of Breach under §5(a)(ii) ISDA 2002 — "
                f"{self.params.contract_id}"
            )
            notice["cure_period"] = "30 calendar days from effective date of this notice"
            notice["consequence"] = (
                "If the breach is not remedied within the cure period, "
                "it will constitute an Event of Default under §5(a)(ii) "
                "and the Non-defaulting Party may designate an Early "
                "Termination Date under §6(a)."
            )
        elif notice_type == "TAX_CHANGE":
            notice["subject"] = (
                f"Notice under §4(d) ISDA 2002 — Change in Tax Status — "
                f"{self.params.contract_id}"
            )

        return notice


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 5: CLOSE-OUT AMOUNT — §6 ISDA 2002
# ─────────────────────────────────────────────────────────────────────────────

class CloseOutModule:
    """
    Calculates the Early Termination Amount under §6 ISDA 2002.

    §6 ISDA 2002 — Early Termination; Close-out Netting
    ─────────────────────────────────────────────────────
    Two paths:

    (A) Event of Default §6(a):
        Non-defaulting Party designates Early Termination Date (ETD).
        ALL Transactions are Terminated Transactions.
        Non-defaulting Party determines Close-out Amount.

    (B) Termination Event §6(b):
        Affected Party (or Non-affected Party) designates ETD.
        Only Affected Transactions are Terminated Transactions.
        Both parties determine Close-out Amount; result averaged.
        (except Credit Event Upon Merger: Non-affected Party determines)

    CLOSE-OUT AMOUNT §6(e)(i) — Three components (User Guide p.37):
    ──────────────────────────────────────────────────────────────────
    (i)  Unpaid Amounts — obligations that became due but were not paid
    (ii) Unpaid Amounts — obligations that would have been due but for EoD
    (iii) Close-out Amount — future value of Terminated Transactions
         (replacement cost: cost to replicate remaining cash flows at market)

    WATERFALL §6(e)(ii) — User Guide p.38 Examples:
    ─────────────────────────────────────────────────
    1. Each Determining Party calculates Close-out Amount from its side
    2. Average the two Close-out Amounts (or use Non-defaulting Party's for EoD)
    3. Add Unpaid Amounts owed to Non-defaulting Party
    4. Subtract Unpaid Amounts owed by Non-defaulting Party
    5. Result = Early Termination Amount — one net payment, one direction

    THIS CLASS AUTOMATES:
    ─────────────────────
    - The arithmetic waterfall (steps 1-5)
    - Indicative MTM calculation for Close-out Amount (simplified replacement cost)
    - Default interest on Unpaid Amounts §9(h)(i)(1)
    - Fingerprint and audit trail entry

    THIS REQUIRES HUMAN INPUT:
    ───────────────────────────
    - Commercial quotations for Close-out Amount from Reference Market-makers
    - Legal review of the Early Termination Amount
    - Notice of Early Termination Date (§6(a): written notice to Defaulting Party)
    - Actual payment of Early Termination Amount
    """

    def __init__(self, params: SwapParameters, calc_engine: CalculationEngine):
        self.params = params
        self.calc = calc_engine
        self.dc = DayCountModule()

    def calculate_indicative_mtm(self, etd: date, remaining_periods: List[CalculationPeriod],
                                  current_oracle: OracleReading,
                                  determining_party: str) -> Decimal:
        """
        Indicative Mark-to-Market value for Close-out Amount calculation.

        SIMPLIFIED METHOD (prototype):
        We approximate the replacement cost as the NPV of remaining net cash flows,
        discounted at a flat rate (current EURIBOR proxy).

        PRODUCTION METHOD:
        Close-out Amount should be based on:
        (i)   Market quotations from dealers/end-users (§6(e)(i) clause (i))
        (ii)  Relevant market data: yield curves, volatilities, correlations
        (iii) Internal pricing models of the Determining Party

        The 2002 MA removed the strict Market Quotation procedure (4 Reference
        Market-makers) in favour of a flexible "commercially reasonable" approach.
        """
        print(f"\n  [CLOSE_OUT] Calculating indicative MTM for {len(remaining_periods)} remaining periods...")
        print(f"  [CLOSE_OUT] ⚠ PROTOTYPE: Using simplified replacement cost. Production requires market quotations.")

        total_pv = Decimal("0")
        discount_rate = current_oracle.rate  # Flat curve proxy

        for i, period in enumerate(remaining_periods):
            fixed_amt = self.calc.calculate_fixed_amount(period)
            float_amt = self.calc.calculate_floating_amount(period, current_oracle)
            net, payer = self.calc.apply_netting(fixed_amt, float_amt)

            # Discount to ETD (simplified: compound at EURIBOR flat)
            days_to_payment = max((period.payment_date - etd).days, 0)
            dcf = Decimal(str(days_to_payment)) / Decimal("365")
            discount_factor = Decimal("1") / (Decimal("1") + discount_rate * dcf)
            pv = net * discount_factor

            # Sign convention: positive = benefit to Party A (fixed payer)
            if payer == NetPayer.PARTY_A:
                pv = -pv  # Cost to Party A
            elif payer == NetPayer.PARTY_B:
                pv = pv   # Benefit to Party A

            total_pv += pv

        print(f"  [CLOSE_OUT] Indicative Close-out Amount (Party A perspective): EUR {total_pv:,.2f}")
        return total_pv.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def calculate_unpaid_amounts(self, periods: List[CalculationPeriod],
                                  etd: date) -> Tuple[Decimal, Decimal]:
        """
        §9(h)(ii): Unpaid Amounts — past obligations not yet settled.
        Includes default interest §9(h)(i)(1) from payment date to ETD.

        Returns: (unpaid_owed_to_A, unpaid_owed_to_B)
        """
        unpaid_a = Decimal("0")  # Owed to Party A (by Party B)
        unpaid_b = Decimal("0")  # Owed to Party B (by Party A)

        for period in periods:
            if (period.payment_instruction_issued
                    and not period.payment_confirmed
                    and period.payment_date <= etd):
                # Calculate default interest to ETD
                days_overdue = (etd - period.payment_date).days
                # §9(h)(i)(1) Default Rate = payee's cost of funding + 1% p.a.
                # Configurable via SwapParameters.default_rate (default 6%).
                # BUG WAS: hardcoded Decimal("0.06") with no override path.
                default_interest = self.calc.calculate_default_interest(
                    period.net_amount or Decimal("0"),
                    days_overdue,
                    self.params.default_rate
                )
                total_owed = (period.net_amount or Decimal("0")) + default_interest

                if period.net_payer == NetPayer.PARTY_A:
                    unpaid_b += total_owed  # Party A owed to Party B
                elif period.net_payer == NetPayer.PARTY_B:
                    unpaid_a += total_owed  # Party B owed to Party A

        return (unpaid_a.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                unpaid_b.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def execute_waterfall(self, trigger: str,
                           determining_party: str,
                           etd: date,
                           all_periods: List[CalculationPeriod],
                           current_oracle: OracleReading,
                           is_eod: bool = True) -> CloseOutCalculation:
        """
        §6 ISDA 2002 — Complete close-out waterfall.

        Implements User Guide p.38 formula:
          Early Termination Amount =
            Close-out Amount (net of both parties' determinations)
            + Unpaid Amounts owed to Non-defaulting Party
            - Unpaid Amounts owed by Non-defaulting Party

        See User Guide p.38 Examples:
          Example 1: Close-out A=90, B=-100, Unpaid=0 → Y pays X: 95
          Example 2: Close-out A=90, B=-100, Unpaid X=50, Y=25 → Y pays X: 120
        """
        print(f"\n{'='*60}")
        print(f"  §6 ISDA 2002 CLOSE-OUT WATERFALL")
        print(f"  Trigger: {trigger}")
        print(f"  ETD: {etd}  |  Determining Party: {determining_party}")
        print(f"{'='*60}")

        # Separate past (unpaid) from future (remaining) periods
        past_periods = [p for p in all_periods if p.payment_date <= etd]
        remaining_periods = [p for p in all_periods if p.payment_date > etd]

        print(f"\n  [CLOSE_OUT] Terminated transactions: ALL ({len(all_periods)} periods)")
        print(f"  [CLOSE_OUT] Past periods (Unpaid Amounts): {len(past_periods)}")
        print(f"  [CLOSE_OUT] Future periods (Close-out Amount): {len(remaining_periods)}")

        # ── Step 1: Unpaid Amounts §9(h)(ii) ────────────────────────────────
        unpaid_a, unpaid_b = self.calculate_unpaid_amounts(past_periods, etd)
        print(f"\n  [STEP 1] Unpaid Amounts:")
        print(f"     Owed to Party A (Alpha): EUR {unpaid_a:,.2f}")
        print(f"     Owed to Party B (Beta):  EUR {unpaid_b:,.2f}")

        # ── Step 2: Close-out Amount — future value ─────────────────────────
        if not remaining_periods:
            close_out_a = Decimal("0")
            close_out_b = Decimal("0")
            print(f"\n  [STEP 2] Close-out Amount: EUR 0 (no remaining periods)")
        else:
            # Party A's determination (from A's side of market)
            close_out_a = self.calculate_indicative_mtm(
                etd, remaining_periods, current_oracle, "PARTY_A")
            # Party B's determination (from B's side: opposite sign, slight spread)
            # In practice: B would independently obtain market quotations
            # Simplified proxy: add a small bid-ask spread (0.05%)
            spread_proxy = self.params.notional * Decimal("0.0005") * Decimal(
                str(len(remaining_periods))) / Decimal("4")
            close_out_b = -(close_out_a + spread_proxy)
            print(f"\n  [STEP 2] Close-out Amounts:")
            print(f"     Party A determination: EUR {close_out_a:,.2f}")
            print(f"     Party B determination: EUR {close_out_b:,.2f}")

        # ── Step 3: Waterfall §6(e)(ii) ─────────────────────────────────────
        if is_eod:
            # EoD §6(e)(i)(3): the Non-defaulting Party is the sole Determining
            # Party.  Only ONE party's determination is used — NOT averaged.
            # (Averaging applies only for bilateral Termination Events §6(e)(i)(4).)
            # BUG WAS: comment wrongly said "result averaged per §6(e)(ii)"
            # — the code was correct but the comment was misleading.
            if determining_party == "PARTY_A":
                effective_coa = close_out_a
                unpaid_to_det = unpaid_a
                unpaid_by_det = unpaid_b
            else:
                effective_coa = close_out_b
                unpaid_to_det = unpaid_b
                unpaid_by_det = unpaid_a
        else:
            # TE: Average of both parties' determinations
            effective_coa = (close_out_a + close_out_b) / Decimal("2")
            # TE: Non-affected Party perspective
            if determining_party == "PARTY_A":
                unpaid_to_det = unpaid_a
                unpaid_by_det = unpaid_b
            else:
                unpaid_to_det = unpaid_b
                unpaid_by_det = unpaid_a

        print(f"\n  [STEP 3] Waterfall:")
        print(f"     Close-out Amount (effective): EUR {effective_coa:,.2f}")
        print(f"     + Unpaid Amounts owed to det. party: EUR {unpaid_to_det:,.2f}")
        print(f"     - Unpaid Amounts owed by det. party: EUR {unpaid_by_det:,.2f}")

        early_termination_amount = (effective_coa + unpaid_to_det - unpaid_by_det
                                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # ── Step 4: Determine direction ─────────────────────────────────────
        if early_termination_amount > Decimal("0"):
            payable_by = ("PARTY_B" if determining_party == "PARTY_A"
                          else "PARTY_A")
        elif early_termination_amount < Decimal("0"):
            payable_by = determining_party
            early_termination_amount = abs(early_termination_amount)
        else:
            payable_by = "NONE (zero net)"

        print(f"\n  [STEP 4] Early Termination Amount: EUR {early_termination_amount:,.2f}")
        print(f"     Payable by: {payable_by}")
        print(f"     ⚠ HUMAN GATE: Calculation Agent must review and approve")
        print(f"     ⚠ HUMAN GATE: Legal counsel required before payment")

        # ── Build result ─────────────────────────────────────────────────────
        calc_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        result = CloseOutCalculation(
            trigger=trigger,
            determining_party=determining_party,
            early_termination_date=etd,
            terminated_transactions=[f"SLC-IRS-EUR-001 Period {p.period_number}"
                                     for p in all_periods],
            close_out_amount_party_a=close_out_a,
            close_out_amount_party_b=close_out_b,
            unpaid_amounts_owed_to_a=unpaid_a,
            unpaid_amounts_owed_to_b=unpaid_b,
            early_termination_amount=early_termination_amount,
            payable_by=payable_by,
            mtm_rate_used=current_oracle.rate,
            calculation_method="REPLACEMENT_COST_SIMPLIFIED",
            calculation_timestamp=calc_ts,
            calculation_fingerprint=hashlib.sha256(
                json.dumps({
                    "coa_a": str(close_out_a),
                    "coa_b": str(close_out_b),
                    "unpaid_a": str(unpaid_a),
                    "unpaid_b": str(unpaid_b),
                    "eta": str(early_termination_amount),
                    "ts": calc_ts
                }, sort_keys=True).encode()
            ).hexdigest()[:16]
        )
        return result


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 6: SCHEDULE GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleGenerator:
    """
    Generates the quarterly payment schedule from contract parameters.

    Uses BusinessDayCalendar for Modified Following adjustment (TARGET2 + London).
    This resolves Risk Registry R01.
    """

    def __init__(self, params: SwapParameters):
        self.params = params
        self.dc = DayCountModule()
        self.cal = BusinessDayCalendar(["TARGET2", "LONDON"])

    def generate(self) -> List[CalculationPeriod]:
        """
        Generates all calculation periods from effective_date to termination_date.
        Quarterly frequency (every 3 months).
        Payment date = end date adjusted per Modified Following (TARGET2 + London).
        """
        periods = []
        current = self.params.effective_date
        n = 1

        while current < self.params.termination_date:
            next_date = self.dc.add_months(current, self.params.fixed_frequency_months)
            if next_date > self.params.termination_date:
                next_date = self.params.termination_date

            # Adjust payment date per Modified Following Business Day Convention
            # ISDA 2006 Definitions §4.12(ii) — TARGET2 + London
            payment_date = self.cal.modified_following(next_date)

            periods.append(CalculationPeriod(
                period_number=n,
                start_date=current,
                end_date=next_date,
                payment_date=payment_date
            ))
            current = next_date
            n += 1

        return periods


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 7: AUDIT TRAIL
# ─────────────────────────────────────────────────────────────────────────────

class AuditTrail:
    """
    Immutable append-only log of every action taken by the engine.
    Every entry includes a SHA-256 fingerprint linking to the previous entry
    (chain of custody). Both parties hold identical audit trails.
    """

    def __init__(self, contract_id: str):
        self.contract_id = contract_id
        self._entries: List[dict] = []
        self._last_hash = "GENESIS"

    def log(self, event_type: str, data: dict, actor: str = "ENGINE"):
        ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        entry = {
            "seq": len(self._entries) + 1,
            "timestamp": ts,
            "contract_id": self.contract_id,
            "event_type": event_type,
            "actor": actor,
            "data": data,
            "prev_hash": self._last_hash
        }
        entry_hash = hashlib.sha256(
            json.dumps(entry, sort_keys=True, default=str).encode()
        ).hexdigest()
        entry["entry_hash"] = entry_hash
        self._last_hash = entry_hash
        self._entries.append(entry)

    def export(self, path: str):
        output = {
            "contract_id": self.contract_id,
            "export_timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "total_entries": len(self._entries),
            "chain_head_hash": self._last_hash,
            "isda_reference": "ISDA 2002 Master Agreement",
            "hierarchy_clause": (
                "Legal text SLC-IRS-EUR-001 prevails over this audit trail. "
                "Confirmation > Schedule > Master Agreement > Code. §1(b) ISDA 2002."
            ),
            "entries": self._entries
        }
        with open(path, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n  [AUDIT] Exported {len(self._entries)} entries → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR: IRSExecutionEngine
# ─────────────────────────────────────────────────────────────────────────────

class IRSExecutionEngine:
    """
    Orchestrates the full lifecycle of a Vanilla IRS Smart Legal Contract.

    AUTOMATED ACTIONS:
    ──────────────────
    ✓ Fetch EURIBOR 3M from ECB (with fallback)
    ✓ Generate quarterly payment schedule
    ✓ Calculate fixed and floating amounts
    ✓ Net per §2(c) ISDA 2002
    ✓ Monitor all 8 EoDs and 5 Termination Events
    ✓ Suspend obligations per §2(a)(iii) on EoD/PEoD
    ✓ Execute §6 close-out waterfall if ETD designated
    ✓ Generate cryptographic audit trail

    HUMAN GATES:
    ─────────────
    ✗ Payment approval (Calculation Agent must confirm each instruction)
    ✗ EoD declarations requiring external evidence (Bankruptcy, Merger, etc.)
    ✗ Early Termination Date designation (Non-defaulting Party must act)
    ✗ Close-out Amount commercial validation (legal + market quotations)
    ✗ Actual settlement via SWIFT/TARGET2
    """

    def __init__(self, params: SwapParameters,
                 schedule: ScheduleElections = None,
                 initiation: ContractInitiation = None):
        self.params = params
        self.schedule = schedule or ScheduleElections()
        self.initiation = initiation or ContractInitiation()

        # Apply Schedule elections as baseline; Confirmation overrides win per §1(b).
        # Strategy: capture any Confirmation values that differ from dataclass defaults
        # (these were explicitly set in the Confirmation), apply Schedule values, then
        # restore those explicit Confirmation overrides.
        if schedule:
            # Step 1 — record explicit Confirmation overrides (non-default values)
            _SW_DEFAULTS = {
                'governing_law':               "English Law",
                'mtpn_elected':                True,
                'termination_currency':        "EUR",
                'automatic_early_termination': False,
                'cross_default_elected':       False,
                'cross_default_threshold':     None,
                'csa_elected':                 False,
                'calculation_agent':           "Party A",
            }
            _conf_overrides = {
                k: getattr(params, k)
                for k in _SW_DEFAULTS
                if getattr(params, k) != _SW_DEFAULTS[k]
            }
            # Step 2 — apply Schedule (layer 3 baseline)
            params.governing_law = schedule.governing_law
            params.mtpn_elected = schedule.mtpn_elected
            params.termination_currency = schedule.termination_currency
            params.automatic_early_termination = schedule.aet_party_a or schedule.aet_party_b
            params.cross_default_elected = (
                schedule.cross_default_party_a or schedule.cross_default_party_b
            )
            params.cross_default_threshold = schedule.cross_default_threshold_a
            params.csa_elected = schedule.csa_elected
            params.calculation_agent = schedule.calculation_agent
            # Step 3 — restore Confirmation overrides (layer 2 wins per §1(b))
            for _k, _v in _conf_overrides.items():
                setattr(params, _k, _v)
            # Step 4 — link schedule_id into Confirmation if not already set
            if not params.schedule_id:
                params.schedule_id = schedule.schedule_id

        self.oracle = OracleModule(params)
        self.calc = CalculationEngine(params)
        self.schedule_gen = ScheduleGenerator(params)
        self.eod_monitor = EoDMonitor(params)
        self.close_out_module = CloseOutModule(params, self.calc)
        self.compliance = ComplianceMonitor(params, self.eod_monitor)
        self.audit = AuditTrail(params.contract_id)
        self.state = ContractState.ACTIVE
        self.periods: List[CalculationPeriod] = []
        self.close_out_result: Optional[CloseOutCalculation] = None

        # ── Module 9: Due Diligence & Covenant Monitoring ─────────────────
        # CovenantChecker tracks required documents and covenant conditions
        # per Schedule Part 3 / §4 ISDA 2002.
        # Imported conditionally to keep engine self-contained.
        try:
            from due_diligence import CovenantChecker
            self.dd_checker = CovenantChecker(params.contract_id, params)
            self.dd_checker.compliance = self.compliance   # back-reference
            self._dd_module_available = True
        except ImportError:
            self.dd_checker = None
            self._dd_module_available = False

        # ── Module 8: Netting Opinion Check ──────────────────────────────
        # Pre-trade assessment of close-out netting enforceability
        # based on ISDA jurisdictional netting opinions.
        # Imported conditionally to keep engine self-contained.
        try:
            from netting_opinion_module import (
                NettingOpinionCheck, GoverningLaw, NettingAssessment
            )
            self._netting_checker = NettingOpinionCheck()
            self._netting_module_available = True
        except ImportError:
            self._netting_checker = None
            self._netting_module_available = False

        self.netting_assessment = None  # Populated by initialise()

    def initialise(self):
        """Generate schedule, run netting check, and log contract creation."""
        print(f"\n{'='*60}")
        print(f"  {self.params.contract_id} — SLC Execution Engine v0.2")
        print(f"  {self.params.party_a.name} / {self.params.party_b.name}")
        print(f"  {self.params.notional:,.0f} {self.params.currency} · "
              f"{self.params.fixed_rate*100:.3f}% fixed · EURIBOR 3M")
        print(f"  {self.params.effective_date} → {self.params.termination_date}")
        print(f"{'='*60}")

        # ── Schedule Generation ──────────────────────────────────────────
        self.periods = self.schedule_gen.generate()

        # ── Module 9: Due Diligence — Required Documents ─────────────────
        # Initialise all required document stubs (status=REQUIRED).
        # Client will upload; advisor will validate. No network call.
        if self._dd_module_available and self.dd_checker:
            self.dd_checker.initialise_required_documents()
        else:
            print(f"  [INIT] Due Diligence module not available.")

        # ── Module 8: Netting Opinion Check ──────────────────────────────
        # Runs BEFORE contract activation — produces advisory warnings.
        # If assessment is RED, engine logs HUMAN GATE requirement.
        netting_data = {}
        if self._netting_module_available and self._netting_checker:
            from netting_opinion_module import GoverningLaw
            gov_law_enum = (
                GoverningLaw.ENGLISH_LAW
                if "English" in self.params.governing_law
                else GoverningLaw.NEW_YORK_LAW
            )
            self.netting_assessment = self._netting_checker.assess(
                contract_id=self.params.contract_id,
                party_a_jurisdiction=self.params.party_a.jurisdiction_code,
                party_b_jurisdiction=self.params.party_b.jurisdiction_code,
                governing_law=gov_law_enum
            )
            na = self.netting_assessment

            # Print assessment
            self._netting_checker.print_assessment(na)

            # Build audit data
            netting_data = {
                "party_a_jurisdiction": na.party_a_jurisdiction,
                "party_a_status": na.party_a_profile.opinion_status.value,
                "party_a_risk": na.party_a_risk_level,
                "party_b_jurisdiction": na.party_b_jurisdiction,
                "party_b_status": na.party_b_profile.opinion_status.value,
                "party_b_risk": na.party_b_risk_level,
                "overall_risk": na.overall_risk_level,
                "netting_enforceable": na.netting_enforceable,
                "governing_law": na.governing_law.value,
                "warnings_count": len(na.warnings),
                "fingerprint": na.assessment_fingerprint
            }

            # Log to audit trail
            self.audit.log("NETTING_OPINION_CHECK", netting_data)

            # HUMAN GATE: if not both CLEAN, require acknowledgment
            if not na.netting_enforceable:
                print(f"\n  ⚠ HUMAN GATE — NETTING RISK DETECTED")
                print(f"     Overall risk: {na.overall_risk_level}")
                print(f"     Calculation Agent must acknowledge netting risk")
                print(f"     before contract activation.")
                print(f"     Independent legal advice on close-out netting")
                print(f"     enforceability is STRONGLY recommended.")
                self.audit.log("NETTING_RISK_HUMAN_GATE", {
                    "overall_risk": na.overall_risk_level,
                    "action_required": "Calculation Agent acknowledgment",
                    "status": "PENDING"
                }, actor="SYSTEM")
        else:
            print(f"\n  [INIT] Netting Opinion Check module not available.")
            print(f"         Netting enforceability not assessed.")
            self.audit.log("NETTING_OPINION_CHECK", {
                "status": "MODULE_NOT_AVAILABLE",
                "note": "netting_opinion_module.py not found in path"
            })

        # ── Log contract creation ────────────────────────────────────────
        self.audit.log("CONTRACT_INITIALISED", {
            "contract_id": self.params.contract_id,
            "isda_version": self.params.isda_version,
            "parties": {
                "party_a": self.params.party_a.name,
                "party_a_jurisdiction": self.params.party_a.jurisdiction_code,
                "party_b": self.params.party_b.name,
                "party_b_jurisdiction": self.params.party_b.jurisdiction_code,
            },
            "economic_terms": {
                "notional": str(self.params.notional),
                "currency": self.params.currency,
                "fixed_rate": str(self.params.fixed_rate),
                "floating_index": self.params.floating_index,
                "effective_date": str(self.params.effective_date),
                "termination_date": str(self.params.termination_date),
            },
            "schedule_elections": self.schedule.to_dict() if self.schedule else {
                "mtpn_elected": self.params.mtpn_elected,
                "governing_law": self.params.governing_law,
                "automatic_early_termination": self.params.automatic_early_termination,
                "cross_default_elected": self.params.cross_default_elected,
                "csa_elected": self.params.csa_elected,
            },
            "initiation": {
                "initiated_by": self.initiation.initiated_by or "SYSTEM",
                "schedule_ref": self.initiation.schedule_ref,
                "status": self.initiation.status,
            },
            "netting_assessment": netting_data if netting_data else "NOT_ASSESSED",
            "periods_generated": len(self.periods),
            "hierarchy_clause": "§1(b) ISDA 2002 — Confirmation > Schedule > MA > Code"
        })
        print(f"\n  [INIT] Schedule generated: {len(self.periods)} quarterly periods")

        # ── Module 4B: Auto-schedule §4 obligations ──────────────────────
        # Uses Schedule Part 3 (Agreement to Deliver) if available
        if self.schedule and self.schedule.documents_to_deliver:
            self.compliance.schedule_from_part3(
                self.schedule.documents_to_deliver,
                self.params.effective_date, self.params.termination_date)
        else:
            self.compliance.schedule_standard_obligations(
                self.params.effective_date, self.params.termination_date)

    def run_calculation_cycle(self, period_number: int,
                               today: Optional[date] = None) -> Optional[dict]:
        """
        Run a single calculation cycle for one payment period.

        Steps:
        1. Check §2(a)(iii) conditions precedent
        2. Fetch oracle rate
        3. Calculate fixed and floating amounts
        4. Apply §2(c) netting
        5. Check for Failure to Pay on previous periods
        6. Issue Payment Instruction (pending human approval)
        """
        if today is None:
            today = date.today()

        period = self.periods[period_number - 1]

        print(f"\n{'─'*60}")
        print(f"  PERIOD {period.period_number}: {period.start_date} → {period.end_date}")
        print(f"{'─'*60}")

        # ── §2(a)(iii) Conditions Precedent ─────────────────────────────────
        if self.eod_monitor.is_suspended:
            print(f"  ⛔ §2(a)(iii): Payment obligations SUSPENDED (EoD/PEoD active)")
            period.suspended = True
            self.audit.log("PAYMENT_SUSPENDED", {
                "period": period_number,
                "reason": "§2(a)(iii) condition precedent: EoD or PEoD active",
                "active_eods": self.eod_monitor.summary()
            })
            return None

        if self.state not in [ContractState.ACTIVE]:
            print(f"  ⛔ Contract state: {self.state.value} — no calculations")
            return None

        # ── Oracle Fetch ─────────────────────────────────────────────────────
        oracle = self.oracle.fetch()
        period.oracle_reading = oracle

        # ── Calculations ─────────────────────────────────────────────────────
        fixed_amt = self.calc.calculate_fixed_amount(period)
        floating_amt = self.calc.calculate_floating_amount(period, oracle)
        net_amt, net_payer = self.calc.apply_netting(fixed_amt, floating_amt)

        period.fixed_amount = fixed_amt
        period.floating_amount = floating_amt
        period.net_amount = net_amt
        period.net_payer = net_payer

        # ── Fingerprint ──────────────────────────────────────────────────────
        fp_data = {
            "period": period_number,
            "oracle_rate": str(oracle.rate),
            "oracle_status": oracle.status.value,
            "fixed_amount": str(fixed_amt),
            "floating_amount": str(floating_amt),
            "net_amount": str(net_amt),
            "net_payer": net_payer.value,
            "ts": datetime.now(timezone.utc).isoformat()
        }
        period.calculation_fingerprint = self.calc.fingerprint(fp_data)

        # ── Display ──────────────────────────────────────────────────────────
        payer_name = (self.params.party_a.short_name if net_payer == NetPayer.PARTY_A
                      else self.params.party_b.short_name if net_payer == NetPayer.PARTY_B
                      else "NONE")
        print(f"\n  Oracle:   EURIBOR 3M = {oracle.rate*100:.3f}% [{oracle.status.value}]")
        print(f"  Fixed:    EUR {fixed_amt:>12,.2f}  (Party A · 30/360)")
        print(f"  Float:    EUR {floating_amt:>12,.2f}  (Party B · ACT/360)")
        print(f"  Net [§2(c)]: EUR {net_amt:>12,.2f}  → {payer_name} pays")
        print(f"  Fingerprint: {period.calculation_fingerprint}")

        # ── §3/§4 Compliance Check ────────────────────────────────────────────
        compliance = self.compliance.compliance_summary(period.payment_date)
        self.compliance.print_compliance(period.payment_date)
        self.audit.log("COMPLIANCE_CHECK", compliance)

        # ── §5(a)(ii) Breach Escalation Check ─────────────────────────────────
        escalations = self.compliance.check_escalation_to_eod(period.payment_date)
        if escalations:
            self.audit.log("BREACH_ESCALATION_REVIEW", {
                "escalations": escalations,
                "action_required": "Calculation Agent must assess materiality "
                                   "and consider §5(a)(ii) notice",
                "human_gate": True
            })

        # ── Payment Instruction ──────────────────────────────────────────────
        period.payment_instruction_issued = True
        print(f"\n  💳 PAYMENT INSTRUCTION ISSUED (⚠ Pending human approval)")
        print(f"     Amount:  EUR {net_amt:,.2f}")
        print(f"     Payer:   {payer_name} ({self.params.party_a.name if net_payer==NetPayer.PARTY_A else self.params.party_b.name})")
        print(f"     Due:     {period.payment_date}")

        # ── EoD Check on previous periods ────────────────────────────────────
        for prev in self.periods[:period_number-1]:
            if prev.payment_instruction_issued and not prev.payment_confirmed:
                eod = self.eod_monitor.detect_potential_failure_to_pay(prev, today)
                if eod:
                    self.state = ContractState.SUSPENDED

        # ── Audit ─────────────────────────────────────────────────────────────
        self.audit.log("CALCULATION_COMPLETE", fp_data)
        self.audit.log("PAYMENT_INSTRUCTION_ISSUED", {
            "period": period_number,
            "net_amount": str(net_amt),
            "net_payer": net_payer.value,
            "payment_date": str(period.payment_date),
            "status": "PENDING_HUMAN_APPROVAL",
            "fingerprint": period.calculation_fingerprint
        })

        return {
            "period": period_number,
            "start": str(period.start_date),
            "end": str(period.end_date),
            "euribor": float(oracle.rate),
            "oracle_status": oracle.status.value,
            "fixed_amount": float(fixed_amt),
            "floating_amount": float(floating_amt),
            "net_amount": float(net_amt),
            "net_payer": net_payer.value,
            "fingerprint": period.calculation_fingerprint
        }

    def run_all_periods(self) -> List[dict]:
        """Run calculation cycle for all periods. Stops if EoD fires."""
        results = []
        for p in self.periods:
            result = self.run_calculation_cycle(p.period_number)
            if result:
                results.append(result)
            if self.eod_monitor.is_suspended:
                print(f"\n  ⛔ EoD detected — halting further calculations per §2(a)(iii)")
                break
        return results

    def confirm_payment(self, period_number: int):
        """Human approval of a Payment Instruction.

        §2(a)(iii): If a Potential EoD was registered for this period's failure
        to pay, confirming the payment remedies the underlying condition and the
        PEoD must be marked cured so the §2(a)(iii) suspension is lifted.
        Only PEoDs are cured this way; full EoDs require §6 close-out.
        """
        period = self.periods[period_number - 1]
        period.payment_confirmed = True
        print(f"\n  ✓ PAYMENT CONFIRMED: Period {period_number} — EUR {period.net_amount:,.2f}")

        # §2(a)(iii): cure any PEoD whose condition is now remedied
        if period.net_payer is not None:
            defaulting = (DefaultingParty.PARTY_A
                          if period.net_payer == NetPayer.PARTY_A
                          else DefaultingParty.PARTY_B)
            self.eod_monitor.cure_potential_eod(
                EventOfDefault.FAILURE_TO_PAY, defaulting)

        self.audit.log("PAYMENT_CONFIRMED", {
            "period": period_number,
            "amount": str(period.net_amount),
            "payer": period.net_payer.value if period.net_payer else None,
            "confirmed_by": "CALCULATION_AGENT",
            "suspended_after": self.eod_monitor.is_suspended,
        }, actor="CALCULATION_AGENT")

    def trigger_early_termination(self, trigger_type: str,
                                   determining_party: str = "PARTY_A",
                                   etd: Optional[date] = None,
                                   is_eod: bool = True):
        """
        §6(a)/(b): Designate Early Termination Date and run close-out waterfall.

        HUMAN GATE: This method should only be called after:
        - Written notice has been given to the Defaulting/Affected Party
        - Legal counsel has confirmed the EoD or TE
        - Calculation Agent has authorised the designation
        """
        if etd is None:
            etd = date.today()

        print(f"\n{'='*60}")
        print(f"  §6 EARLY TERMINATION DESIGNATED")
        print(f"  ETD: {etd}  |  Trigger: {trigger_type}")
        print(f"  ⚠ HUMAN GATE: Notice required. Legal review required.")
        print(f"{'='*60}")

        self.state = ContractState.EARLY_TERM_NOTIFIED

        # Fetch current oracle for MTM
        oracle = self.oracle.fetch()

        # Run §6 waterfall
        self.close_out_result = self.close_out_module.execute_waterfall(
            trigger=trigger_type,
            determining_party=determining_party,
            etd=etd,
            all_periods=self.periods,
            current_oracle=oracle,
            is_eod=is_eod
        )

        self.state = ContractState.TERMINATED

        self.audit.log("EARLY_TERMINATION_DESIGNATED", {
            "trigger": trigger_type,
            "etd": str(etd),
            "determining_party": determining_party,
            "is_eod": is_eod,
            "early_termination_amount": str(self.close_out_result.early_termination_amount),
            "payable_by": self.close_out_result.payable_by,
            "fingerprint": self.close_out_result.calculation_fingerprint
        }, actor="CALCULATION_AGENT")

        return self.close_out_result

    def print_summary(self):
        """Print full lifecycle summary."""
        print(f"\n{'='*60}")
        print(f"  LIFECYCLE SUMMARY — {self.params.contract_id}")
        print(f"{'='*60}")
        print(f"  State: {self.state.value}")
        print(f"  Periods calculated: {sum(1 for p in self.periods if p.fixed_amount)}")
        print(f"  Payments confirmed: {sum(1 for p in self.periods if p.payment_confirmed)}")

        total_a = sum(p.net_amount or Decimal("0") for p in self.periods
                      if p.net_payer == NetPayer.PARTY_A)
        total_b = sum(p.net_amount or Decimal("0") for p in self.periods
                      if p.net_payer == NetPayer.PARTY_B)
        print(f"\n  §2(c) Netting Summary (lifecycle):")
        print(f"     {self.params.party_a.short_name} pays net: EUR {total_a:>12,.2f}")
        print(f"     {self.params.party_b.short_name} pays net: EUR {total_b:>12,.2f}")

        if self.eod_monitor.active_eods or self.eod_monitor.active_tes:
            print(f"\n  §5 Events:")
            print(f"  {self.eod_monitor.summary()}")

        if self.close_out_result:
            print(f"\n  §6 Close-out:")
            print(f"     Early Termination Amount: EUR {self.close_out_result.early_termination_amount:,.2f}")
            print(f"     Payable by: {self.close_out_result.payable_by}")

        if self.netting_assessment:
            na = self.netting_assessment
            risk_sym = {"GREEN": "●", "AMBER": "◐", "RED": "○"}
            print(f"\n  §1(c)/§6(e) Netting Opinion Assessment:")
            print(f"     Party A ({na.party_a_jurisdiction}): "
                  f"{risk_sym.get(na.party_a_risk_level,'?')} {na.party_a_risk_level}")
            print(f"     Party B ({na.party_b_jurisdiction}): "
                  f"{risk_sym.get(na.party_b_risk_level,'?')} {na.party_b_risk_level}")
            print(f"     Overall: {risk_sym.get(na.overall_risk_level,'?')} "
                  f"{na.overall_risk_level} — "
                  f"{'Enforceable' if na.netting_enforceable else 'REVIEW REQUIRED'}")
            print(f"     Governing law: {na.governing_law.value}")
            print(f"     Fingerprint: {na.assessment_fingerprint}")

        print(f"\n  ⚠ HIERARCHY CLAUSE (§1(b) ISDA 2002):")
        print(f"     Legal text SLC-IRS-EUR-001 prevails over this output.")
        print(f"     Confirmation > Schedule > Master Agreement > Code.")
        print(f"     No payment occurs without human approval.")


# ─────────────────────────────────────────────────────────────────────────────
# RISK REGISTRY — prototype limitations
# ─────────────────────────────────────────────────────────────────────────────

RISK_REGISTRY = {
    "R01": {
        "title": "Holiday calendar (MITIGATED)",
        "risk": "MITIGATED: BusinessDayCalendar implements TARGET2 + London calendars. "
                "Modified Following Business Day Convention per ISDA 2006 §4.12(ii). "
                "Grace periods use real Local Business Days (not calendar day proxy).",
        "mitigation": "Already implemented. Easter algorithm + TARGET2 fixed holidays + "
                      "London bank holidays with substitute day rules."
    },
    "R02": {
        "title": "Single oracle source",
        "risk": "ECB SDW is sole rate source. Fallback is hardcoded constant, not a "
                "true ISDA 2021 waterfall (Term SOFR → compounded RFR → spread-adjusted).",
        "mitigation": "V2: median of 3 sources. V3: Chainlink EURIBOR feed."
    },
    "R03": {
        "title": "Close-out Amount simplified",
        "risk": "MTM uses flat-curve NPV. Production requires market quotations from "
                "dealers per §6(e)(i) ISDA 2002. No volatility surface.",
        "mitigation": "Integrate Bloomberg B-PIPE or ICE Data for curve data."
    },
    "R04": {
        "title": "No eIDAS signatures",
        "risk": "Signatures are simulated SHA-256 hashes. Not legally binding under "
                "EU Regulation 910/2014. §9(e) ISDA 2002 requires proper execution.",
        "mitigation": "Integrate DocuSign/Qualified Trust Service Provider for eIDAS QES."
    },
    "R05": {
        "title": "EoD declarations partially manual",
        "risk": "Bankruptcy, Merger, Misrepresentation EoDs require external evidence. "
                "Engine cannot automatically detect from public data.",
        "mitigation": "V2: Company events monitoring API (Bloomberg Company Monitor)."
    },
    "R06": {
        "title": "No fiat settlement",
        "risk": "Payment Instructions are produced but no actual EUR transfer occurs. "
                "Requires SWIFT or TARGET2 integration.",
        "mitigation": "V3: Initiate SWIFT MT202 via banking API (Kyriba, Finastra)."
    },
    "R07": {
        "title": "No cross-default monitoring",
        "risk": "§5(a)(vi) Cross-Default requires monitoring external debt obligations. "
                "Not implemented (cross_default_elected=False by default).",
        "mitigation": "V2: Credit monitoring feed (Moody's, Fitch ratings alerts)."
    },
    "R08": {
        "title": "EURIBOR cessation risk",
        "risk": "EURIBOR may be discontinued. Engine has ISDA 2021 fallback but "
                "€STR compounding waterfall is simplified.",
        "mitigation": "Implement full ISDA 2021 Benchmark Fallback Protocol waterfall."
    },
    "R09": {
        "title": "Decimal arithmetic (handled)",
        "risk": "MITIGATED: All calculations use Python Decimal, not float. "
                "Precision set to 28 decimal places.",
        "mitigation": "Already implemented."
    },
    "R10": {
        "title": "Tax Event monitoring",
        "risk": "§5(b)(iii) Tax Events require monitoring of withholding tax regulations. "
                "Highly jurisdiction-specific. Engine only supports manual declaration.",
        "mitigation": "Tax counsel required. No automated solution practical at v0.2."
    },
    "R11": {
        "title": "Netting opinion check (MITIGATED)",
        "risk": "MITIGATED: Module 8 (NettingOpinionCheck) verifies ISDA netting opinion "
                "status for both counterparty jurisdictions at contract creation. "
                "30 jurisdictions covered (all G-20). GREEN/AMBER/RED classification. "
                "§2(a)(iii) behavior flagged. Assessment logged in audit trail.",
        "mitigation": "Already implemented. Production: connect to netalytics API for "
                      "90+ jurisdictions with counterparty-level granularity."
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# DEMO / MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  DERIVAI — SMART LEGAL CONTRACT ENGINE v0.2")
    print("  ISDA 2002 Master Agreement · Vanilla IRS · EUR")
    print("="*60)

    # ── Contract parameters ───────────────────────────────────────────────
    params = SwapParameters(
        contract_id="SLC-IRS-EUR-001",
        party_a=PartyDetails(
            name="Alpha Corp S.A.", short_name="Alpha",
            role="fixed_payer", jurisdiction="England", jurisdiction_code="GB"
        ),
        party_b=PartyDetails(
            name="Beta Fund Ltd", short_name="Beta",
            role="floating_payer", jurisdiction="France", jurisdiction_code="FR"
        ),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03200"),
        effective_date=date(2026, 3, 15),
        termination_date=date(2028, 3, 15),
        mtpn_elected=True,
        automatic_early_termination=False,
        cross_default_elected=False,
        csa_elected=False
    )

    # ── Create and initialise engine ──────────────────────────────────────
    engine = IRSExecutionEngine(params)
    engine.initialise()

    print("\n\n── SCENARIO A: Normal lifecycle (all periods) ──────────────")
    results = engine.run_all_periods()

    # Simulate human approval of first 3 payments
    for i in range(1, min(4, len(engine.periods) + 1)):
        if engine.periods[i-1].payment_instruction_issued:
            engine.confirm_payment(i)

    engine.print_summary()

    # ── SCENARIO B: Event of Default demo ────────────────────────────────
    print("\n\n── SCENARIO B: Event of Default — §5(a)(i) Failure to Pay ──")
    params_b = SwapParameters(
        contract_id="SLC-IRS-EUR-001-EOD-DEMO",
        party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer"),
        party_b=PartyDetails("Beta Fund Ltd", "Beta", "floating_payer"),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03200"),
        effective_date=date(2026, 3, 15),
        termination_date=date(2028, 3, 15),
    )
    engine_b = IRSExecutionEngine(params_b)
    engine_b.initialise()

    # Run period 1 normally
    engine_b.run_calculation_cycle(1)
    # Period 1 payment NOT confirmed (simulates failure to pay)
    # Run period 2 — this will detect PEoD on period 1
    today_sim = params_b.effective_date + timedelta(days=100)
    eod_rec = engine_b.eod_monitor.detect_potential_failure_to_pay(
        engine_b.periods[0], today_sim
    )
    if eod_rec:
        print(f"\n  Engine detects: {eod_rec.eod_type.value}")
        print(f"  §2(a)(iii) activated — attempting period 2...")
        engine_b.run_calculation_cycle(2, today=today_sim)

    # ── SCENARIO C: Close-out Waterfall demo ─────────────────────────────
    print("\n\n── SCENARIO C: §6 Close-out Waterfall — ETD designated ──────")
    params_c = SwapParameters(
        contract_id="SLC-IRS-EUR-001-CLOSEOUT-DEMO",
        party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer"),
        party_b=PartyDetails("Beta Fund Ltd", "Beta", "floating_payer"),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03200"),
        effective_date=date(2026, 3, 15),
        termination_date=date(2028, 3, 15),
    )
    engine_c = IRSExecutionEngine(params_c)
    engine_c.initialise()

    # Run and confirm 2 periods
    engine_c.run_calculation_cycle(1)
    engine_c.confirm_payment(1)
    engine_c.run_calculation_cycle(2)
    # Period 2 NOT confirmed — creates an Unpaid Amount

    # Declare bankruptcy on Party B → triggers §5(a)(vii) → ETD
    engine_c.eod_monitor.declare_bankruptcy(
        defaulting_party=DefaultingParty.PARTY_B,
        description="Beta Fund Ltd enters administration (Company No. 12345678). "
                    "Administrator appointed 2027-01-10.",
        today=date(2027, 1, 10)
    )

    # Non-defaulting Party A designates ETD
    closeout = engine_c.trigger_early_termination(
        trigger_type="§5(a)(vii) BANKRUPTCY — Beta Fund Ltd",
        determining_party="PARTY_A",
        etd=date(2027, 1, 15),
        is_eod=True
    )

    engine_c.print_summary()

    # ── Export audit trails ───────────────────────────────────────────────
    engine.audit.export("./outputs/audit_trail_SLC-IRS-EUR-001-v2.json")
    engine_c.audit.export("./outputs/audit_trail_SLC-IRS-EUR-001-closeout.json")

    # ── SCENARIO D: Netting Opinion Check — QUALIFIED jurisdiction ────────
    print("\n\n── SCENARIO D: Netting Opinion — GB ↔ CN (QUALIFIED) ──────────")
    params_d = SwapParameters(
        contract_id="SLC-IRS-EUR-005-NETTING-DEMO",
        party_a=PartyDetails(
            "Gamma Holdings plc", "Gamma", "fixed_payer",
            jurisdiction="England", jurisdiction_code="GB"
        ),
        party_b=PartyDetails(
            "Dragon Capital Management Co. Ltd", "Dragon", "floating_payer",
            jurisdiction="China", jurisdiction_code="CN"
        ),
        notional=Decimal("25000000"),
        fixed_rate=Decimal("0.03500"),
        effective_date=date(2026, 6, 15),
        termination_date=date(2029, 6, 15),
    )
    engine_d = IRSExecutionEngine(params_d)
    engine_d.initialise()
    engine_d.print_summary()

    print("\n\n── RISK REGISTRY ──────────────────────────────────────────────")
    for rid, risk in RISK_REGISTRY.items():
        print(f"  [{rid}] {risk['title']}")
        print(f"         Risk: {risk['risk'][:80]}...")
        print(f"         Mitigation: {risk['mitigation'][:80]}")

    print(f"\n{'='*60}")
    print(f"  Engine v0.2 complete.")
    print(f"  HIERARCHY CLAUSE: Legal text always prevails. §1(b) ISDA 2002.")
    print(f"{'='*60}\n")
