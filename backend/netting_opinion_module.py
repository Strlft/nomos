"""
=============================================================================
  DERIVAI — MODULE 8: NETTING OPINION CHECK
  Execution Engine v0.2 — Extension Module

  WHAT THIS MODULE DOES
  ─────────────────────
  Pre-trade counterparty risk assessment based on ISDA netting opinions.
  At contract creation, the engine checks whether close-out netting under
  §6(e) ISDA 2002 is enforceable against each party given their jurisdiction
  of incorporation and the governing law of the contract.

  This module answers three questions:
    1. Does ISDA publish a netting opinion for this jurisdiction?
    2. Is the opinion CLEAN or QUALIFIED (with reservations)?
    3. Are there specific risks the Calculation Agent should be aware of?

  LEGAL CONTEXT
  ─────────────
  Close-out netting enforceability has two components (ISDA 2002 User Guide):
    (a) Contractual enforceability under the GOVERNING LAW (§13)
        — typically English law or New York law, both well-established
    (b) Consistency with INSOLVENCY LAW of the counterparty's jurisdiction
        — this is what the netting opinions address
        — local insolvency law ALWAYS overrides the governing law choice

  The §1(c) single agreement clause is the legal foundation for close-out
  netting. Without it, a liquidator could cherry-pick profitable trades.
  But §1(c) is only effective if the counterparty's local insolvency law
  respects it — which is what the netting opinion confirms.

  ISDA NETTING OPINIONS — KEY FACTS (as of 2025)
  ────────────────────────────────────────────────
  — ISDA publishes netting opinions for 90 jurisdictions
  — All G-20 jurisdictions now recognise close-out netting enforceability
  — Saudi Arabia was the last G-20 nation (SAMA regulations, February 2025)
  — China and India recognised netting in recent years
  — Some jurisdictions have QUALIFIED opinions (reservations / conditions)
  — ISDA is not aware of any instance where netting was found unenforceable
    in a jurisdiction where ISDA published a clean opinion

  HIERARCHY CLAUSE (§1(b) ISDA 2002)
  ────────────────────────────────────
  This module produces ADVISORY WARNINGS only. It does not constitute legal
  advice. The legal text of the Smart Legal Contract always prevails.
  Parties must obtain independent legal advice on netting enforceability
  in their specific circumstances.

  DATA SOURCE
  ───────────
  The jurisdiction database below is compiled from publicly available ISDA
  data (Status of Netting Legislation page, ISDA opinion updates, ISDA
  Quarterly publications). The actual ISDA netting opinions are proprietary
  and available only to ISDA members. This module uses STATUS classifications
  only, not the opinion text itself.

  PRODUCTION NOTE
  ───────────────
  In production, this database would be replaced by an API connection to
  netalytics (aosphere/ISDA joint venture) which provides machine-readable
  netting opinion analysis for 90+ jurisdictions with counterparty-level
  granularity. See: https://www.aosphere.com/products/derivatives/netalytics/
=============================================================================
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Dict, Tuple
from datetime import date
import hashlib
import json
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# ENUMERATIONS
# ─────────────────────────────────────────────────────────────────────────────

class NettingOpinionStatus(Enum):
    """
    Classification of ISDA netting opinion status for a jurisdiction.

    CLEAN:      ISDA opinion confirms enforceability without material reservations.
                Close-out netting under §6(e) should be enforceable in insolvency.

    QUALIFIED:  ISDA opinion confirms enforceability BUT with specific reservations
                or conditions (e.g., counterparty type restrictions, specific entity
                requirements, recent legislative changes not yet tested in courts).
                Calculation Agent / legal counsel MUST review the qualifications.

    NO_OPINION: ISDA does not publish a netting opinion for this jurisdiction.
                Close-out netting enforceability is UNCERTAIN. Parties should obtain
                a bespoke legal opinion before entering into transactions.
                ⚠ This is the highest risk category.

    NEGATIVE:   ISDA opinion indicates netting may NOT be enforceable (very rare).
                Do NOT rely on close-out netting in this jurisdiction.

    INFORMAL:   ISDA has published an informal country update (not a full opinion).
                Provides some guidance but not the same level of assurance.
    """
    CLEAN       = "CLEAN"
    QUALIFIED   = "QUALIFIED"
    NO_OPINION  = "NO_OPINION"
    NEGATIVE    = "NEGATIVE"
    INFORMAL    = "INFORMAL"


class NettingLegislationType(Enum):
    """
    How the jurisdiction recognises close-out netting.

    SPECIFIC_STATUTE:   Specific netting legislation (e.g., US, France, Germany,
                        Japan). Provides the strongest legal certainty.

    GENERAL_PRINCIPLES: Enforceability based on established general legal principles
                        without specific netting statute (e.g., England, common law
                        jurisdictions). England is the gold standard here — close-out
                        netting is widely accepted without specific statutory recognition.

    MODEL_NETTING_ACT:  Jurisdiction adopted legislation based on ISDA's 2018 Model
                        Netting Act (e.g., Saudi Arabia 2025).

    MIXED:              Combination of statute and general principles.

    NONE:               No specific or general recognition. Highest risk.
    """
    SPECIFIC_STATUTE    = "SPECIFIC_STATUTE"
    GENERAL_PRINCIPLES  = "GENERAL_PRINCIPLES"
    MODEL_NETTING_ACT   = "MODEL_NETTING_ACT"
    MIXED               = "MIXED"
    NONE                = "NONE"


class GoverningLaw(Enum):
    """
    Supported governing law options for the ISDA Master Agreement.
    §13 ISDA 2002 — printed form offers English law or New York law.
    """
    ENGLISH_LAW = "English Law"
    NEW_YORK_LAW = "New York Law"


class Section2aiii_Behavior(Enum):
    """
    How §2(a)(iii) condition precedent behaves post-insolvency.

    This is one of the most contested issues in derivatives law.

    PERPETUAL_SUSPENSION (English law position post-Lomas v JFB Firth Rixson [2012]):
        The non-defaulting party can suspend payments indefinitely without
        triggering close-out. The condition precedent in §2(a)(iii) survives
        the counterparty's insolvency. This can leave the insolvent estate
        in limbo — the swap is neither terminated nor performing.
        Flawed asset argument was rejected by the English courts.

    LIMITED_SUSPENSION (New York law / other jurisdictions):
        The suspension right may be limited by local insolvency law.
        Some jurisdictions treat indefinite suspension as inconsistent
        with the policy of orderly administration of insolvent estates.
        The non-defaulting party may be required to close out within a
        reasonable period or lose the right to do so.

    UNTESTED:
        No clear judicial authority in this jurisdiction. The behavior
        of §2(a)(iii) in insolvency is uncertain.
    """
    PERPETUAL_SUSPENSION = "PERPETUAL_SUSPENSION"
    LIMITED_SUSPENSION   = "LIMITED_SUSPENSION"
    UNTESTED             = "UNTESTED"


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class JurisdictionProfile:
    """
    Netting opinion profile for a single jurisdiction.

    This encodes the key findings from the ISDA netting opinion
    (or lack thereof) for the jurisdiction where a counterparty
    is incorporated.
    """
    jurisdiction_code: str          # ISO 3166-1 alpha-2 (e.g., "GB", "FR", "DE")
    jurisdiction_name: str          # Full name (e.g., "England & Wales")
    opinion_status: NettingOpinionStatus
    legislation_type: NettingLegislationType
    section_2aiii_behavior: Section2aiii_Behavior

    # Opinion details
    opinion_last_updated: Optional[str] = None   # Year of last ISDA opinion update
    opinion_law_firm: Optional[str] = None        # Firm that drafted the opinion
    g20_member: bool = False

    # Qualifications and risks (populated for QUALIFIED opinions)
    qualifications: List[str] = field(default_factory=list)
    # e.g., ["Opinion limited to banks regulated by central bank",
    #        "Untested for investment funds"]

    # Specific insolvency risks
    insolvency_risks: List[str] = field(default_factory=list)
    # e.g., ["Moratorium may delay close-out for 30 days",
    #        "Court approval required for set-off"]

    # Whether AET (Automatic Early Termination) is recommended
    aet_recommended: bool = False
    # Some jurisdictions recommend electing AET to avoid the risk
    # that a moratorium prevents ETD designation after insolvency

    # Special resolution regime (for bank counterparties)
    special_resolution_regime: bool = False
    # Post-2008 bank resolution frameworks (e.g., UK Banking Act 2009,
    # EU BRRD) may override standard netting analysis for bank CPs


@dataclass
class NettingAssessment:
    """
    Result of a pre-trade netting opinion check for a specific
    counterparty pair under a specific governing law.
    """
    # Identification
    contract_id: str
    assessment_date: str
    governing_law: GoverningLaw

    # Party A assessment
    party_a_jurisdiction: str
    party_a_profile: JurisdictionProfile
    party_a_risk_level: str          # "GREEN" / "AMBER" / "RED"

    # Party B assessment
    party_b_jurisdiction: str
    party_b_profile: JurisdictionProfile
    party_b_risk_level: str

    # Overall assessment
    overall_risk_level: str          # Worst of the two
    netting_enforceable: bool        # True only if both are CLEAN
    warnings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    # Audit
    assessment_fingerprint: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 8: NETTING OPINION CHECK
# ─────────────────────────────────────────────────────────────────────────────

class NettingOpinionCheck:
    """
    Pre-trade netting enforceability assessment.

    Called at contract creation (Step 2 of the New Contract flow) when
    the parties' jurisdictions are entered. Produces a NettingAssessment
    with risk classification and warnings.

    USAGE:
        checker = NettingOpinionCheck()
        assessment = checker.assess(
            contract_id="SLC-IRS-EUR-001",
            party_a_jurisdiction="GB",
            party_b_jurisdiction="FR",
            governing_law=GoverningLaw.ENGLISH_LAW
        )
        checker.print_assessment(assessment)

    INTEGRATION WITH ENGINE:
        This module is called BEFORE the contract is activated.
        If overall_risk_level == "RED", the engine should require
        explicit acknowledgment from the Calculation Agent before
        proceeding (HUMAN GATE).
    """

    def __init__(self):
        self.jurisdictions: Dict[str, JurisdictionProfile] = {}
        self._load_jurisdiction_database()

    # ── Jurisdiction Database ────────────────────────────────────────────
    # Compiled from ISDA publicly available data as of 2025.
    # Status classifications based on:
    #   - ISDA "Status of Netting Legislation" page
    #   - ISDA 2025 opinion update summaries
    #   - ISDA Quarterly December 2025 (90 jurisdictions confirmed)
    #   - Saudi Arabia netting opinions (June/November 2025)
    #
    # ⚠ THIS IS A PROTOTYPE DATABASE. Production would use netalytics API.
    # ⚠ NOT LEGAL ADVICE. Always obtain independent legal opinion.
    # ─────────────────────────────────────────────────────────────────────

    def _load_jurisdiction_database(self):
        """Load the jurisdiction netting opinion database."""

        jurisdictions_data = [
            # ═══════════════════════════════════════════════════════════════
            # G-20 JURISDICTIONS — All now recognise close-out netting
            # ═══════════════════════════════════════════════════════════════

            # ── EUROPE ────────────────────────────────────────────────────
            JurisdictionProfile(
                jurisdiction_code="GB", jurisdiction_name="England & Wales",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.GENERAL_PRINCIPLES,
                section_2aiii_behavior=Section2aiii_Behavior.PERPETUAL_SUSPENSION,
                opinion_last_updated="2024", opinion_law_firm="Linklaters",
                g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "UK Banking Act 2009: special resolution regime for banks — "
                    "temporary stay on close-out rights (max 2 business days) for "
                    "bank counterparties under Bank of England resolution"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="FR", jurisdiction_name="France",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", opinion_law_firm="Gide Loyrette Nouel",
                g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Code monétaire et financier Art. L.211-36 et seq. — provides "
                    "specific protection for close-out netting of financial contracts",
                    "Sauvegarde / redressement judiciaire: netting protected by "
                    "financial collateral directive transposition"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="DE", jurisdiction_name="Germany",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", opinion_law_firm="Hengeler Mueller",
                g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Insolvenzordnung §104: specific provision for close-out netting "
                    "of financial contracts — administrator cannot cherry-pick",
                    "EU BRRD transposition: temporary stay for bank CPs (max 48h)"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="IT", jurisdiction_name="Italy",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Decreto Legislativo 170/2004 transposing Financial Collateral "
                    "Directive — protects close-out netting arrangements"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),

            # ── NORTH AMERICA ─────────────────────────────────────────────
            JurisdictionProfile(
                jurisdiction_code="US", jurisdiction_name="United States",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", opinion_law_firm="Cravath / Davis Polk",
                g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "US Bankruptcy Code §§362(b)(17), 546(g), 560: safe harbor for "
                    "swap agreements — exempts from automatic stay and avoidance",
                    "Dodd-Frank Title II (OLA): FDIC may impose 1-business-day stay "
                    "on close-out for systemically important financial institutions"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="CA", jurisdiction_name="Canada",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Payment Clearing and Settlement Act: protects netting agreements",
                    "CDIC Act: resolution stay for bank counterparties"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),

            # ── ASIA-PACIFIC ──────────────────────────────────────────────
            JurisdictionProfile(
                jurisdiction_code="JP", jurisdiction_name="Japan",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Act on Close-out Netting of Specified Financial Transactions: "
                    "comprehensive statutory protection for close-out netting"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="AU", jurisdiction_name="Australia",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Payment Systems and Netting Act 1998: protects close-out netting"
                ],
                special_resolution_regime=True,
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="CN", jurisdiction_name="China (PRC)",
                opinion_status=NettingOpinionStatus.QUALIFIED,
                legislation_type=NettingLegislationType.MIXED,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[
                    "Netting enforceability recognised through Futures and Derivatives "
                    "Law (2022) — relatively new, limited judicial precedent",
                    "Opinion may be limited to certain categories of regulated entities",
                    "Cross-border enforcement remains untested in practice"
                ],
                insolvency_risks=[
                    "Enterprise Bankruptcy Law: interaction with netting provisions "
                    "not yet tested in reported court decisions",
                    "Administrator powers may be broader than in common law jurisdictions"
                ],
                aet_recommended=True
            ),
            JurisdictionProfile(
                jurisdiction_code="IN", jurisdiction_name="India",
                opinion_status=NettingOpinionStatus.QUALIFIED,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[
                    "Bilateral Netting of Qualified Financial Contracts Act 2020 — "
                    "provides statutory recognition but implementing regulations "
                    "are still being finalised",
                    "Scope limited to 'qualified financial contracts' as designated "
                    "by the relevant regulatory authority (RBI/SEBI)"
                ],
                insolvency_risks=[
                    "Insolvency and Bankruptcy Code 2016: interaction with netting "
                    "statute not yet tested in NCLT proceedings",
                    "Moratorium under IBC §14 — scope of carve-out for financial "
                    "contracts still evolving"
                ],
                aet_recommended=True
            ),
            JurisdictionProfile(
                jurisdiction_code="KR", jurisdiction_name="South Korea",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="SG", jurisdiction_name="Singapore",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[
                    "Payment and Settlement Systems (Finality and Netting) Act: "
                    "comprehensive netting protection"
                ],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="HK", jurisdiction_name="Hong Kong",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.GENERAL_PRINCIPLES,
                section_2aiii_behavior=Section2aiii_Behavior.PERPETUAL_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[
                    "Common law jurisdiction — netting enforceability based on "
                    "general contractual principles (similar to English law)"
                ],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="ID", jurisdiction_name="Indonesia",
                opinion_status=NettingOpinionStatus.QUALIFIED,
                legislation_type=NettingLegislationType.MIXED,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2023", g20_member=True,
                qualifications=[
                    "Netting recognition through OJK regulations — scope may be "
                    "limited to certain regulated entities",
                    "Judicial precedent on close-out netting is limited"
                ],
                insolvency_risks=[
                    "Bankruptcy Law No. 37/2004: administrator may have broad "
                    "powers to avoid pre-bankruptcy transactions"
                ],
                aet_recommended=True
            ),

            # ── MIDDLE EAST ───────────────────────────────────────────────
            JurisdictionProfile(
                jurisdiction_code="SA", jurisdiction_name="Saudi Arabia",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.MODEL_NETTING_ACT,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2025", opinion_law_firm="STAT",
                g20_member=True,
                qualifications=[
                    "SAMA regulations (February 2025) — based on ISDA 2018 Model "
                    "Netting Act. CMA regulations (July 2025) extend coverage.",
                    "Legislation is very new — no judicial precedent yet",
                    "At least one party must be supervised by SAMA or CMA"
                ],
                insolvency_risks=[
                    "Interaction between netting regulations and Saudi bankruptcy "
                    "law (2018) untested in practice"
                ],
                aet_recommended=True
            ),
            JurisdictionProfile(
                jurisdiction_code="AE", jurisdiction_name="United Arab Emirates",
                opinion_status=NettingOpinionStatus.QUALIFIED,
                legislation_type=NettingLegislationType.MIXED,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[
                    "DIFC and ADGM (free zones) have specific netting legislation — "
                    "opinions are CLEAN for DIFC/ADGM-incorporated entities",
                    "For onshore UAE entities — opinion is more QUALIFIED",
                    "Federal Decree-Law No. 51/2023: provides some netting recognition "
                    "but scope and application remain to be tested"
                ],
                insolvency_risks=[
                    "Onshore UAE insolvency law: interaction with netting provisions "
                    "not yet tested in court proceedings"
                ],
                aet_recommended=True
            ),
            JurisdictionProfile(
                jurisdiction_code="TR", jurisdiction_name="Turkey",
                opinion_status=NettingOpinionStatus.QUALIFIED,
                legislation_type=NettingLegislationType.GENERAL_PRINCIPLES,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2023", g20_member=True,
                qualifications=[
                    "No specific netting statute — enforceability based on general "
                    "principles of Turkish law of obligations",
                    "Limited judicial precedent on close-out netting in insolvency"
                ],
                insolvency_risks=[
                    "Enforcement and Bankruptcy Law: administrator powers may "
                    "interfere with close-out netting mechanics"
                ],
                aet_recommended=True
            ),

            # ── LATIN AMERICA ─────────────────────────────────────────────
            JurisdictionProfile(
                jurisdiction_code="BR", jurisdiction_name="Brazil",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Law 10,214/2001 and CMN Resolution: comprehensive netting "
                    "protection for financial market participants"
                ],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="MX", jurisdiction_name="Mexico",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="AR", jurisdiction_name="Argentina",
                opinion_status=NettingOpinionStatus.QUALIFIED,
                legislation_type=NettingLegislationType.MIXED,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2023", g20_member=True,
                qualifications=[
                    "Netting framework exists but judicial precedent is limited",
                    "Capital controls may affect payment obligations under the swap"
                ],
                insolvency_risks=[
                    "Argentine insolvency law: cram-down provisions may affect "
                    "close-out netting enforcement"
                ],
                aet_recommended=True
            ),

            # ── AFRICA ────────────────────────────────────────────────────
            JurisdictionProfile(
                jurisdiction_code="ZA", jurisdiction_name="South Africa",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=True,
                qualifications=[],
                insolvency_risks=[
                    "Financial Markets Act 2012: provides netting protection"
                ],
                aet_recommended=False
            ),

            # ── OTHER KEY EUROPEAN JURISDICTIONS ──────────────────────────
            JurisdictionProfile(
                jurisdiction_code="LU", jurisdiction_name="Luxembourg",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="NL", jurisdiction_name="Netherlands",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.GENERAL_PRINCIPLES,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="IE", jurisdiction_name="Ireland",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.PERPETUAL_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="CH", jurisdiction_name="Switzerland",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.GENERAL_PRINCIPLES,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="ES", jurisdiction_name="Spain",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="SE", jurisdiction_name="Sweden",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="BE", jurisdiction_name="Belgium",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.LIMITED_SUSPENSION,
                opinion_last_updated="2024", g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="SC",
                jurisdiction_name="Scotland",
                opinion_status=NettingOpinionStatus.CLEAN,
                legislation_type=NettingLegislationType.GENERAL_PRINCIPLES,
                section_2aiii_behavior=Section2aiii_Behavior.PERPETUAL_SUSPENSION,
                opinion_last_updated="2024", opinion_law_firm="Linklaters",
                g20_member=False,
                qualifications=[],
                insolvency_risks=[],
                aet_recommended=False
            ),
            JurisdictionProfile(
                jurisdiction_code="RU", jurisdiction_name="Russia",
                opinion_status=NettingOpinionStatus.QUALIFIED,
                legislation_type=NettingLegislationType.SPECIFIC_STATUTE,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated="2022", g20_member=True,
                qualifications=[
                    "Federal Law on Clearing: provides netting framework",
                    "⚠ SANCTIONS: EU/UK/US sanctions severely restrict transactions "
                    "with Russian counterparties — legal advice essential",
                    "Close-out netting opinion may not be practically relevant "
                    "given current sanctions regime"
                ],
                insolvency_risks=[
                    "Russian insolvency law: administrator clawback powers",
                    "Sanctions may override contractual netting provisions"
                ],
                aet_recommended=True
            ),

            # ── JURISDICTIONS WITH NO OPINION (examples) ──────────────────
            JurisdictionProfile(
                jurisdiction_code="NG", jurisdiction_name="Nigeria",
                opinion_status=NettingOpinionStatus.INFORMAL,
                legislation_type=NettingLegislationType.NONE,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED,
                opinion_last_updated=None, g20_member=False,
                qualifications=[
                    "ISDA informal country update only — not a full netting opinion",
                    "No specific netting legislation"
                ],
                insolvency_risks=[
                    "Close-out netting enforceability uncertain in insolvency"
                ],
                aet_recommended=True
            ),
        ]

        for jp in jurisdictions_data:
            self.jurisdictions[jp.jurisdiction_code] = jp

    # ── Core Assessment Logic ────────────────────────────────────────────

    def get_jurisdiction(self, code: str) -> Optional[JurisdictionProfile]:
        """Look up a jurisdiction profile by ISO 3166-1 alpha-2 code."""
        return self.jurisdictions.get(code.upper())

    def _classify_risk(self, profile: JurisdictionProfile) -> str:
        """
        Classify counterparty risk based on netting opinion status.

        GREEN:  CLEAN opinion, no material qualifications.
                Close-out netting should be enforceable.

        AMBER:  QUALIFIED opinion or recent untested legislation.
                Close-out netting may be enforceable but with caveats.
                Calculation Agent / legal counsel MUST review.

        RED:    NO_OPINION, NEGATIVE, or INFORMAL only.
                Close-out netting enforceability UNCERTAIN.
                ⚠ Do NOT rely on netting without bespoke legal opinion.
        """
        if profile.opinion_status == NettingOpinionStatus.CLEAN:
            return "GREEN"
        elif profile.opinion_status == NettingOpinionStatus.QUALIFIED:
            return "AMBER"
        elif profile.opinion_status == NettingOpinionStatus.INFORMAL:
            return "AMBER"
        else:  # NO_OPINION or NEGATIVE
            return "RED"

    def assess(
        self,
        contract_id: str,
        party_a_jurisdiction: str,
        party_b_jurisdiction: str,
        governing_law: GoverningLaw = GoverningLaw.ENGLISH_LAW
    ) -> NettingAssessment:
        """
        Perform a pre-trade netting opinion assessment.

        Args:
            contract_id: The SLC contract identifier
            party_a_jurisdiction: ISO 3166-1 alpha-2 code for Party A
            party_b_jurisdiction: ISO 3166-1 alpha-2 code for Party B
            governing_law: English Law or New York Law (§13 ISDA 2002)

        Returns:
            NettingAssessment with risk classification and warnings
        """
        assessment_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        warnings = []
        recommendations = []

        # ── Look up jurisdictions ────────────────────────────────────────
        profile_a = self.get_jurisdiction(party_a_jurisdiction)
        profile_b = self.get_jurisdiction(party_b_jurisdiction)

        # Handle unknown jurisdictions
        if profile_a is None:
            profile_a = JurisdictionProfile(
                jurisdiction_code=party_a_jurisdiction.upper(),
                jurisdiction_name=f"Unknown ({party_a_jurisdiction.upper()})",
                opinion_status=NettingOpinionStatus.NO_OPINION,
                legislation_type=NettingLegislationType.NONE,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED
            )
            warnings.append(
                f"⚠ CRITICAL: No ISDA netting opinion data for Party A jurisdiction "
                f"'{party_a_jurisdiction.upper()}'. Close-out netting enforceability "
                f"is UNKNOWN. Bespoke legal opinion REQUIRED before execution."
            )

        if profile_b is None:
            profile_b = JurisdictionProfile(
                jurisdiction_code=party_b_jurisdiction.upper(),
                jurisdiction_name=f"Unknown ({party_b_jurisdiction.upper()})",
                opinion_status=NettingOpinionStatus.NO_OPINION,
                legislation_type=NettingLegislationType.NONE,
                section_2aiii_behavior=Section2aiii_Behavior.UNTESTED
            )
            warnings.append(
                f"⚠ CRITICAL: No ISDA netting opinion data for Party B jurisdiction "
                f"'{party_b_jurisdiction.upper()}'. Close-out netting enforceability "
                f"is UNKNOWN. Bespoke legal opinion REQUIRED before execution."
            )

        # ── Classify risk ────────────────────────────────────────────────
        risk_a = self._classify_risk(profile_a)
        risk_b = self._classify_risk(profile_b)

        # Overall = worst of the two
        risk_priority = {"GREEN": 0, "AMBER": 1, "RED": 2}
        overall = max(risk_a, risk_b, key=lambda r: risk_priority[r])

        # ── Generate warnings ────────────────────────────────────────────
        for label, profile, risk in [
            ("Party A", profile_a, risk_a),
            ("Party B", profile_b, risk_b)
        ]:
            if profile.qualifications:
                for q in profile.qualifications:
                    warnings.append(f"{label} ({profile.jurisdiction_name}): {q}")

            if profile.insolvency_risks:
                for r in profile.insolvency_risks:
                    warnings.append(
                        f"{label} insolvency risk ({profile.jurisdiction_name}): {r}"
                    )

            if profile.aet_recommended:
                recommendations.append(
                    f"Consider electing Automatic Early Termination (AET) for "
                    f"{label} ({profile.jurisdiction_name}) — recommended where "
                    f"moratorium risk exists per ISDA opinion qualifications."
                )

            if profile.special_resolution_regime:
                warnings.append(
                    f"{label} ({profile.jurisdiction_name}): Special resolution "
                    f"regime applies to bank counterparties — temporary stay on "
                    f"close-out rights may apply. Verify counterparty type."
                )

        # ── §2(a)(iii) behavior check ────────────────────────────────────
        if (profile_a.section_2aiii_behavior !=
                profile_b.section_2aiii_behavior):
            warnings.append(
                f"§2(a)(iii) ASYMMETRY: Party A ({profile_a.jurisdiction_name}) = "
                f"{profile_a.section_2aiii_behavior.value}, Party B "
                f"({profile_b.jurisdiction_name}) = "
                f"{profile_b.section_2aiii_behavior.value}. "
                f"The non-defaulting party's ability to suspend payments indefinitely "
                f"may differ depending on which party defaults. Legal advice required."
            )

        # ── Governing law compatibility check ────────────────────────────
        if governing_law == GoverningLaw.ENGLISH_LAW:
            recommendations.append(
                "Governing law: English Law — contractual enforceability of close-out "
                "netting well-established. §2(a)(iii) perpetual suspension doctrine "
                "applies (Lomas v JFB Firth Rixson [2012] EWCA Civ 419)."
            )
        elif governing_law == GoverningLaw.NEW_YORK_LAW:
            recommendations.append(
                "Governing law: New York Law — contractual enforceability of close-out "
                "netting well-established under US Bankruptcy Code safe harbor. "
                "§2(a)(iii) perpetual suspension may be limited."
            )

        # ── Overall enforceability conclusion ────────────────────────────
        netting_enforceable = (
            risk_a == "GREEN" and risk_b == "GREEN"
        )

        if not netting_enforceable:
            recommendations.append(
                "⚠ HUMAN GATE REQUIRED: Netting enforceability is not confirmed as "
                "CLEAN for both jurisdictions. Calculation Agent must acknowledge "
                "netting risk before contract activation. Independent legal advice "
                "on netting enforceability is STRONGLY recommended."
            )

        # ── Fingerprint ──────────────────────────────────────────────────
        fingerprint_data = json.dumps({
            "contract_id": contract_id,
            "party_a": party_a_jurisdiction,
            "party_b": party_b_jurisdiction,
            "governing_law": governing_law.value,
            "risk_a": risk_a,
            "risk_b": risk_b,
            "overall": overall,
            "ts": assessment_ts
        }, sort_keys=True)
        fingerprint = hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]

        return NettingAssessment(
            contract_id=contract_id,
            assessment_date=assessment_ts,
            governing_law=governing_law,
            party_a_jurisdiction=party_a_jurisdiction.upper(),
            party_a_profile=profile_a,
            party_a_risk_level=risk_a,
            party_b_jurisdiction=party_b_jurisdiction.upper(),
            party_b_profile=profile_b,
            party_b_risk_level=risk_b,
            overall_risk_level=overall,
            netting_enforceable=netting_enforceable,
            warnings=warnings,
            recommendations=recommendations,
            assessment_fingerprint=fingerprint
        )

    # ── Display ──────────────────────────────────────────────────────────

    def print_assessment(self, a: NettingAssessment):
        """Print a formatted netting assessment report."""
        risk_symbols = {"GREEN": "●", "AMBER": "◐", "RED": "○"}

        print(f"\n{'='*70}")
        print(f"  NETTING OPINION CHECK — PRE-TRADE ASSESSMENT")
        print(f"  Contract: {a.contract_id}")
        print(f"  Date: {a.assessment_date}")
        print(f"  Fingerprint: {a.assessment_fingerprint}")
        print(f"{'='*70}")

        print(f"\n  Governing Law (§13 ISDA 2002): {a.governing_law.value}")

        for label, code, profile, risk in [
            ("PARTY A", a.party_a_jurisdiction, a.party_a_profile, a.party_a_risk_level),
            ("PARTY B", a.party_b_jurisdiction, a.party_b_profile, a.party_b_risk_level),
        ]:
            symbol = risk_symbols.get(risk, "?")
            print(f"\n  ── {label}: {profile.jurisdiction_name} ({code}) ──")
            print(f"     Netting Opinion Status:  {symbol} {profile.opinion_status.value}")
            print(f"     Legislation Type:        {profile.legislation_type.value}")
            print(f"     §2(a)(iii) Behavior:     {profile.section_2aiii_behavior.value}")
            print(f"     Risk Classification:     {risk}")
            if profile.opinion_last_updated:
                print(f"     Opinion Last Updated:    {profile.opinion_last_updated}")
            if profile.opinion_law_firm:
                print(f"     Opinion By:              {profile.opinion_law_firm}")
            print(f"     G-20 Member:             {'Yes' if profile.g20_member else 'No'}")
            print(f"     AET Recommended:          {'Yes' if profile.aet_recommended else 'No'}")
            print(f"     Special Resolution Regime: {'Yes' if profile.special_resolution_regime else 'No'}")

        print(f"\n  {'='*66}")
        print(f"  OVERALL RISK: {risk_symbols[a.overall_risk_level]} "
              f"{a.overall_risk_level}")
        print(f"  NETTING ENFORCEABLE (both CLEAN): "
              f"{'YES' if a.netting_enforceable else 'NO — REVIEW REQUIRED'}")
        print(f"  {'='*66}")

        if a.warnings:
            print(f"\n  WARNINGS ({len(a.warnings)}):")
            for i, w in enumerate(a.warnings, 1):
                print(f"    [{i}] {w}")

        if a.recommendations:
            print(f"\n  RECOMMENDATIONS ({len(a.recommendations)}):")
            for i, r in enumerate(a.recommendations, 1):
                print(f"    [{i}] {r}")

        print(f"\n  ⚠ DISCLAIMER: This assessment is based on publicly available ISDA")
        print(f"     netting opinion status data. It does NOT constitute legal advice.")
        print(f"     Parties must obtain independent legal advice on netting")
        print(f"     enforceability for their specific circumstances.")
        print(f"     HIERARCHY CLAUSE: Legal text always prevails. §1(b) ISDA 2002.")
        print(f"{'='*70}\n")

    # ── Utility ──────────────────────────────────────────────────────────

    def list_jurisdictions(self, status_filter: Optional[NettingOpinionStatus] = None):
        """List all jurisdictions in the database, optionally filtered by status."""
        for code, profile in sorted(self.jurisdictions.items(),
                                     key=lambda x: x[1].jurisdiction_name):
            if status_filter and profile.opinion_status != status_filter:
                continue
            symbol = {"GREEN": "●", "AMBER": "◐", "RED": "○"}.get(
                self._classify_risk(profile), "?"
            )
            print(f"  {symbol} {code}  {profile.jurisdiction_name:<25} "
                  f"{profile.opinion_status.value:<12} "
                  f"{profile.legislation_type.value}")


# ─────────────────────────────────────────────────────────────────────────────
# DEMO
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    checker = NettingOpinionCheck()

    print("\n" + "="*70)
    print("  DERIVAI — NETTING OPINION CHECK MODULE v0.1")
    print("  ISDA 2002 Master Agreement · Pre-Trade Assessment")
    print("="*70)

    # ── List all jurisdictions ───────────────────────────────────────────
    print("\n  JURISDICTION DATABASE (prototype — 25 jurisdictions):\n")
    checker.list_jurisdictions()

    # ── Scenario 1: GB ↔ FR — both CLEAN ────────────────────────────────
    print("\n\n── SCENARIO 1: England ↔ France (both CLEAN) ──────────────────")
    a1 = checker.assess("SLC-IRS-EUR-001", "GB", "FR", GoverningLaw.ENGLISH_LAW)
    checker.print_assessment(a1)

    # ── Scenario 2: GB ↔ CN — one QUALIFIED ──────────────────────────────
    print("\n── SCENARIO 2: England ↔ China (QUALIFIED) ─────────────────────")
    a2 = checker.assess("SLC-IRS-EUR-002", "GB", "CN", GoverningLaw.ENGLISH_LAW)
    checker.print_assessment(a2)

    # ── Scenario 3: US ↔ Unknown — RED ───────────────────────────────────
    print("\n── SCENARIO 3: United States ↔ Unknown jurisdiction ────────────")
    a3 = checker.assess("SLC-IRS-EUR-003", "US", "XX", GoverningLaw.NEW_YORK_LAW)
    checker.print_assessment(a3)

    # ── Scenario 4: SA ↔ IN — both QUALIFIED (new legislation) ───────────
    print("\n── SCENARIO 4: Saudi Arabia ↔ India (both recent legislation) ──")
    a4 = checker.assess("SLC-IRS-EUR-004", "SA", "IN", GoverningLaw.ENGLISH_LAW)
    checker.print_assessment(a4)
