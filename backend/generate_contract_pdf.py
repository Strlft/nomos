"""
=============================================================================
  SMART LEGAL CONTRACT — PDF GENERATOR
  ISDA 2002 Master Agreement · Vanilla IRS

  Generates a complete 14-section ISDA 2002 contract PDF from SwapParameters.
  §13 Governing Law and §14 Definitions are DYNAMIC based on the governing
  law election in Schedule Part 4(h).

  HIERARCHY CLAUSE (§1(b) ISDA 2002):
  This PDF IS the governing legal instrument (Layer 1).
  The Execution Engine is subordinate to this document.
  Confirmation > Schedule > Master Agreement > Code.

  Dependencies: reportlab
=============================================================================
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from datetime import date
from decimal import Decimal
import hashlib, json, os

_OUTPUTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")

# ─── Color palette ────────────────────────────────────────────────────────
DARK = HexColor("#1a2332")
AMBER = HexColor("#E8A020")
GREEN = HexColor("#00B86A")
RED = HexColor("#FF4444")
GREY = HexColor("#666666")
LIGHT_BG = HexColor("#F5F7FA")

# ─── Styles ───────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()

style_title = ParagraphStyle("Title", parent=styles["Title"],
    fontSize=24, textColor=DARK, spaceAfter=6, alignment=TA_CENTER)
style_subtitle = ParagraphStyle("Subtitle", parent=styles["Normal"],
    fontSize=12, textColor=GREY, alignment=TA_CENTER, spaceAfter=20)
style_h1 = ParagraphStyle("H1", parent=styles["Heading1"],
    fontSize=14, textColor=DARK, spaceBefore=18, spaceAfter=8,
    borderWidth=0, borderPadding=0)
style_h2 = ParagraphStyle("H2", parent=styles["Heading2"],
    fontSize=11, textColor=HexColor("#333333"), spaceBefore=12, spaceAfter=6)
style_body = ParagraphStyle("Body", parent=styles["Normal"],
    fontSize=9.5, leading=13, alignment=TA_JUSTIFY, spaceAfter=6)
style_auto = ParagraphStyle("Auto", parent=style_body,
    textColor=GREEN, fontSize=8.5, leftIndent=12)
style_human = ParagraphStyle("Human", parent=style_body,
    textColor=RED, fontSize=8.5, leftIndent=12)
style_note = ParagraphStyle("Note", parent=style_body,
    fontSize=8, textColor=GREY, leftIndent=12, rightIndent=12)
style_footer = ParagraphStyle("Footer", parent=styles["Normal"],
    fontSize=7, textColor=GREY, alignment=TA_CENTER)


def generate_contract_pdf(params, output_path, netting_assessment=None):
    """
    Generate a full ISDA 2002 Smart Legal Contract PDF.

    Args:
        params: SwapParameters from irs_engine_v2.py
        output_path: path for the output PDF
        netting_assessment: optional NettingAssessment from netting_opinion_module
    """

    doc = SimpleDocTemplate(output_path, pagesize=A4,
        topMargin=2*cm, bottomMargin=2*cm, leftMargin=2.5*cm, rightMargin=2.5*cm)

    story = []

    # ── Derived values ────────────────────────────────────────────────────
    is_english = "English" in params.governing_law
    gov_law_full = "English Law" if is_english else "the laws of the State of New York"
    jurisdiction_text = (
        "the non-exclusive jurisdiction of the English courts"
        if is_english else
        "the non-exclusive jurisdiction of the courts of the State of New York "
        "and the United States District Court located in the Borough of Manhattan "
        "in New York City"
    )
    section_2aiii_note = (
        "Post-insolvency: English law position per <i>Lomas v JFB Firth Rixson</i> "
        "[2012] EWCA Civ 419 — perpetual suspension permitted. The non-defaulting party "
        "may suspend payments indefinitely without triggering close-out."
        if is_english else
        "Post-insolvency: New York law position — suspension right may be limited by "
        "local insolvency law. The non-defaulting party may be required to close out "
        "within a reasonable period."
    )

    # ── TITLE PAGE ─────────────────────────────────────────────────────────
    story.append(Spacer(1, 60))
    story.append(Paragraph("SMART LEGAL CONTRACT", style_title))
    story.append(Paragraph("Interest Rate Swap — EUR Vanilla", style_subtitle))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"<b>{params.contract_id}</b>", ParagraphStyle(
        "CID", parent=style_subtitle, fontSize=14, textColor=AMBER)))
    story.append(Spacer(1, 20))

    # Party table
    party_data = [
        ["Party A (Fixed Rate Payer)", params.party_a.name],
        ["Party B (Floating Rate Payer)", params.party_b.name],
        ["ISDA Master Agreement", params.isda_version],
        ["Governing Law (§13 / Part 4(h))", params.governing_law],
        ["Effective Date", str(params.effective_date)],
        ["Termination Date", str(params.termination_date)],
        ["Notional Amount", f"EUR {params.notional:,.0f}"],
        ["Fixed Rate", f"{params.fixed_rate * 100:.3f}% p.a."],
        ["Floating Rate", f"{params.floating_index} + {params.floating_spread * 100:.1f} bps"],
        ["Day Count (Fixed / Float)", f"{params.fixed_day_count} / {params.floating_day_count}"],
        ["Payment Frequency", "Quarterly"],
        ["Termination Currency (§8/Part 1(f))", params.termination_currency],
        ["MTPN (Part 4(i))", "Applicable" if params.mtpn_elected else "Not Applicable"],
        ["AET (Part 1(e))", "Applicable" if params.automatic_early_termination else "Not Applicable"],
        ["Calculation Agent (§14)", params.calculation_agent],
    ]
    t = Table(party_data, colWidths=[180, 280])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), DARK),
        ("BACKGROUND", (0, 0), (0, -1), LIGHT_BG),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t)
    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "<b>HIERARCHY CLAUSE — §1(b) ISDA 2002:</b> In the event of any inconsistency, "
        "this Confirmation prevails over the Schedule, which prevails over the Master Agreement. "
        "The Execution Engine is subordinate to this legal text in all cases.",
        style_note))

    story.append(PageBreak())

    # ── SECTION 1 — INTERPRETATION ─────────────────────────────────────────
    story.append(Paragraph("SECTION 1 — INTERPRETATION", style_h1))

    story.append(Paragraph("<b>1(a) — Defined Terms</b>", style_h2))
    story.append(Paragraph(
        "Capitalised terms used in this Smart Legal Contract and not otherwise defined have "
        "the meanings given to them in the ISDA 2002 Master Agreement and the ISDA 2006 "
        "Definitions. References to Sections are to sections of the ISDA 2002 Master Agreement "
        "unless otherwise specified.", style_body))

    story.append(Paragraph("<b>1(b) — Hierarchy Clause (Priority of Documents)</b>", style_h2))
    story.append(Paragraph(
        "In the event of any inconsistency between the provisions of this Confirmation and "
        "the Schedule to the Master Agreement, this Confirmation will prevail. In the event "
        "of any inconsistency between the provisions of the Schedule and the printed form of "
        "the Master Agreement, the Schedule will prevail. The Execution Engine and any Payment "
        "Instructions produced thereby are subordinate to the legal text of this Contract in "
        "all cases.", style_body))
    story.append(Paragraph("■ AUTOMATED: Priority logic encoded in engine — Confirmation parameters override defaults.", style_auto))

    story.append(Paragraph("<b>1(c) — Single Agreement</b>", style_h2))
    story.append(Paragraph(
        "The parties acknowledge and agree that this Contract and all Confirmations form a single "
        "agreement between them (as contemplated by Section 1(c) of the Master Agreement) and that, "
        "but for this acknowledgement and agreement, they would not enter into any Transactions. "
        "This provision is the legal foundation for close-out netting under Section 6.", style_body))

    # ── SECTION 2 — OBLIGATIONS ────────────────────────────────────────────
    story.append(Paragraph("SECTION 2 — OBLIGATIONS", style_h1))

    story.append(Paragraph("<b>2(a)(i) — Payment Obligations</b>", style_h2))
    story.append(Paragraph(
        "Each party will make each payment specified in this Contract, subject to the other "
        "provisions of the Master Agreement. Payments will be made on the due date in freely "
        "transferable funds in a manner customary for payments in EUR.", style_body))

    story.append(Paragraph("<b>2(a)(iii) — Conditions Precedent (Circuit Breaker)</b>", style_h2))
    story.append(Paragraph(
        "The obligation of each party to make any payment is subject to: (1) no Event of Default "
        "or Potential Event of Default with respect to the other party has occurred and is continuing; "
        "(2) no Early Termination Date has occurred or been effectively designated. The Execution "
        "Engine monitors these conditions at every calculation cycle and suspends Payment Instructions "
        "automatically if any condition is breached.", style_body))
    story.append(Paragraph("■ AUTOMATED: §2(a)(iii) circuit breaker — engine halts all payment instructions on EoD/PEoD.", style_auto))
    story.append(Paragraph(f"<i>{section_2aiii_note}</i>", style_note))

    story.append(Paragraph("<b>2(c) — Netting of Payments</b>", style_h2))
    story.append(Paragraph(
        "Payments in EUR due on the same date in respect of the same Transaction are automatically "
        "netted to a single net payment. Multiple Transaction Payment Netting is elected as "
        f"{'Applicable' if params.mtpn_elected else 'Not Applicable'} from the Effective Date "
        "(Schedule Part 4(i)).", style_body))
    story.append(Paragraph("■ AUTOMATED: §2(c) netting calculated by engine — single net payment instruction issued per period.", style_auto))

    # ── SECTION 3 — REPRESENTATIONS ────────────────────────────────────────
    story.append(Paragraph("SECTION 3 — REPRESENTATIONS", style_h1))
    story.append(Paragraph(
        "Each party represents to the other on each date on which a Transaction is entered into "
        "that: (a) it has the power and authority to enter into this Agreement; (b) no Event of "
        "Default or Potential Event of Default has occurred and is continuing; (c) there is no "
        "pending or threatened litigation or proceeding; (d) all specified information is accurate "
        "in all material respects; (e) Payer Tax Representations as specified in the Schedule; "
        "(f) Payee Tax Representations as specified in the Schedule; (g) No Agency — each party "
        "enters as principal, not as agent.", style_body))

    # ── SECTION 4 — AGREEMENTS ─────────────────────────────────────────────
    story.append(Paragraph("SECTION 4 — AGREEMENTS", style_h1))
    story.append(Paragraph(
        "Each party agrees: (a) to furnish specified information to the other party; "
        "(b) to maintain all authorisations necessary; (c) to comply with all applicable laws; "
        "(d) tax agreement — to give notice of any failure to make payments free of withholding "
        "tax and to provide the relevant tax forms.", style_body))

    # ── SECTION 5 — EVENTS OF DEFAULT & TERMINATION EVENTS ─────────────────
    story.append(Paragraph("SECTION 5 — EVENTS OF DEFAULT AND TERMINATION EVENTS", style_h1))

    eods = [
        ("§5(a)(i) Failure to Pay", "1 Local Business Day", "AUTO-MONITOR",
         "Engine detects unpaid PI after grace period. TARGET2+London calendar for LBD."),
        ("§5(a)(ii) Breach of Agreement", "30 calendar days", "HUMAN GATE",
         "Requires notice and factual assessment. Repudiation: immediate."),
        ("§5(a)(iii) Credit Support Default", "N/A", "SKIPPED" if not params.csa_elected else "HUMAN GATE",
         "Only relevant if CSA elected." if not params.csa_elected else "CSA elected — monitor compliance."),
        ("§5(a)(iv) Misrepresentation", "Immediate", "HUMAN GATE",
         "Requires Calculation Agent assessment: was the rep 'material'?"),
        ("§5(a)(v) Default Under Specified Tx", "1 LBD (payment)", "HUMAN GATE",
         "Monitoring of external GMRA/GMSLA agreements."),
        ("§5(a)(vi) Cross-Default", "N/A", "SKIPPED" if not params.cross_default_elected else "HUMAN GATE",
         "Not elected." if not params.cross_default_elected else f"Threshold: EUR {params.cross_default_threshold:,.0f}"),
        ("§5(a)(vii) Bankruptcy", "15 days (bona fide dispute)", "HUMAN GATE",
         "Requires external confirmation (court filing, insolvency proceeding)."),
        ("§5(a)(viii) Merger Without Assumption", "Immediate", "HUMAN GATE",
         "Surviving entity must assume obligations — legal verification required."),
    ]

    eod_data = [["Event of Default", "Grace Period", "Status", "Engine Role"]]
    for e in eods:
        eod_data.append(list(e))

    t_eod = Table(eod_data, colWidths=[120, 80, 75, 185])
    t_eod.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t_eod)

    # ── SECTION 6 — EARLY TERMINATION ──────────────────────────────────────
    story.append(Paragraph("SECTION 6 — EARLY TERMINATION; CLOSE-OUT NETTING", style_h1))
    story.append(Paragraph(
        "<b>6(a)</b> — If an Event of Default has occurred and is continuing, the Non-defaulting Party "
        "may designate an Early Termination Date by not more than 20 days notice. "
        f"Automatic Early Termination: {'Applicable' if params.automatic_early_termination else 'Not Applicable'} "
        "(Part 1(e) Schedule).", style_body))
    story.append(Paragraph("✗ HUMAN GATE: ETD designation requires written notice by Non-defaulting Party.", style_human))

    story.append(Paragraph(
        "<b>6(e)</b> — Early Termination Amount = Close-out Amount (net) + Unpaid Amounts (net). "
        "EoD path: Non-defaulting Party determines; all Transactions terminated. "
        "TE path: Both parties determine; result averaged; only Affected Transactions. "
        "Close-out Amount determined on replacement cost basis per §6(e)(i).", style_body))
    story.append(Paragraph("■ AUTOMATED: Waterfall arithmetic (indicative). Close-out Amount quantum = HUMAN GATE.", style_auto))

    # ── SECTIONS 7-12 ──────────────────────────────────────────────────────
    story.append(Paragraph("SECTION 7 — TRANSFER", style_h1))
    story.append(Paragraph(
        "Neither party may transfer any rights or obligations without prior written consent, "
        "except: (a) pursuant to a consolidation, merger or amalgamation; (b) to an Affiliate; "
        "(c) pursuant to §6(b)(ii) to avoid a Tax Event.", style_body))

    story.append(Paragraph("SECTION 8 — CONTRACTUAL CURRENCY", style_h1))
    story.append(Paragraph(
        f"The Contractual Currency is EUR. Termination Currency: {params.termination_currency} "
        "(Part 1(f) Schedule). Conversion at the rate prevailing on the relevant date.", style_body))

    story.append(Paragraph("SECTION 9 — MISCELLANEOUS", style_h1))
    story.append(Paragraph(
        "§9(e): This Contract may be executed in counterparts. §9(h)(i)(1): Default Rate = "
        "payee's cost of funding + 1% p.a., accruing from original due date.", style_body))
    story.append(Paragraph("■ AUTOMATED: Default interest calculated by engine on overdue amounts.", style_auto))

    story.append(Paragraph("SECTIONS 10-12 — OFFICES, EXPENSES, NOTICES", style_h1))
    story.append(Paragraph(
        "§10: Payments through Offices specified in Confirmation. §11: Defaulting Party "
        "indemnifies enforcement costs. §12: Notices in writing, including electronic form, "
        "to addresses in the Schedule.", style_body))

    # ── SECTION 13 — GOVERNING LAW (DYNAMIC) ──────────────────────────────
    story.append(Paragraph("SECTION 13 — GOVERNING LAW AND JURISDICTION", style_h1))

    story.append(Paragraph("<b>13(a) — Governing Law</b>", style_h2))
    story.append(Paragraph(
        f"This Agreement and each Transaction will be governed by and construed in accordance "
        f"with <b>{gov_law_full}</b>"
        f"{'.' if is_english else ' (without reference to choice of law doctrine).'}",
        style_body))

    story.append(Paragraph("<b>13(b) — Jurisdiction</b>", style_h2))
    if is_english:
        story.append(Paragraph(
            "Each party irrevocably submits to (A) the <b>non-exclusive jurisdiction of the "
            "English courts</b> if the Proceedings do not involve a Convention Court and "
            "(B) the <b>exclusive jurisdiction of the English courts</b> if the Proceedings "
            "do involve a Convention Court.", style_body))
    else:
        story.append(Paragraph(
            "Each party irrevocably submits to the <b>non-exclusive jurisdiction of the courts "
            "of the State of New York and the United States District Court</b> located in the "
            "Borough of Manhattan in New York City.", style_body))

    story.append(Paragraph("<b>13(c) — Service of Process</b>", style_h2))
    story.append(Paragraph(
        "Each party irrevocably appoints the Process Agent specified in Part 4(b) of the Schedule. "
        "Service of process may be given in the manner provided for notices in §12(a).", style_body))

    story.append(Paragraph("<b>13(d) — Waiver of Immunities</b>", style_h2))
    story.append(Paragraph(
        "Each party waives, to the extent permitted by applicable law, all immunity from "
        "jurisdiction, attachment or execution.", style_body))

    # ── SECTION 14 — DEFINITIONS (DYNAMIC) ─────────────────────────────────
    story.append(Paragraph("SECTION 14 — DEFINITIONS", style_h1))

    definitions = [
        ("Calculation Agent", params.calculation_agent),
        ("Close-out Amount", "§6(e)(i) — replacement cost basis, commercially reasonable procedures"),
        ("Contractual Currency", "EUR (§8(a))"),
        ("Convention Court", "Brussels Convention Art. 17 / Lugano Convention Art. 17"
         if is_english else "N/A (New York law)"),
        ("Default Rate", "Payee's cost of funding + 1% p.a. (§9(h)(i)(1))"),
        ("Defaulting Party", "§6(a) — party with respect to which an EoD has occurred"),
        ("Determining Party", "Non-defaulting Party (EoD) / Both parties averaged (TE)"),
        ("Early Termination Amount", "§6(e) — Close-out Amount + Unpaid Amounts (net)"),
        ("Early Termination Date", "§6(a) or §6(b)(iv)"),
        ("English law" if is_english else "New York law",
         "The law of England and Wales" if is_english else
         "The laws of the State of New York, without reference to choice of law doctrine"),
        ("Event of Default", "§5(a) — any of the 8 enumerated events"),
        ("Local Business Day",
         "TARGET2 open + London open (for EUR payments)" if is_english else
         "A day on which commercial banks are open in New York City"),
        ("Multiple Transaction Payment Netting",
         "Applicable" if params.mtpn_elected else "Not Applicable"),
        ("Proceedings",
         "Any suit, action or proceeding relating to any dispute under this Agreement"),
        ("Termination Currency", f"{params.termination_currency} (Part 1(f))"),
        ("Terminated Transactions",
         "All Transactions (EoD path) / Affected Transactions only (TE path)"),
    ]

    def_data = [["Term", "Definition"]]
    for term, defn in definitions:
        def_data.append([term, defn])
    t_def = Table(def_data, colWidths=[130, 330])
    t_def.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_def)

    # ── NETTING OPINION (if available) ─────────────────────────────────────
    if netting_assessment:
        story.append(PageBreak())
        story.append(Paragraph("ANNEX — NETTING OPINION ASSESSMENT", style_h1))
        na = netting_assessment
        risk_sym = {"GREEN": "●", "AMBER": "◐", "RED": "○"}
        na_data = [
            ["Party A Jurisdiction", f"{na.party_a_jurisdiction} — {na.party_a_risk_level}"],
            ["Party B Jurisdiction", f"{na.party_b_jurisdiction} — {na.party_b_risk_level}"],
            ["Overall Risk", f"{risk_sym.get(na.overall_risk_level, '?')} {na.overall_risk_level}"],
            ["Netting Enforceable", "YES" if na.netting_enforceable else "NO — REVIEW REQUIRED"],
            ["Governing Law", na.governing_law.value],
            ["Assessment Fingerprint", na.assessment_fingerprint],
        ]
        t_na = Table(na_data, colWidths=[140, 320])
        t_na.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(t_na)
        story.append(Paragraph(
            "<i>This assessment is based on publicly available ISDA netting opinion status data. "
            "It does NOT constitute legal advice. Parties must obtain independent legal advice "
            "on netting enforceability for their specific circumstances.</i>", style_note))

    # ── FOOTER ─────────────────────────────────────────────────────────────
    story.append(Spacer(1, 30))
    story.append(Paragraph(
        "§1(b) ISDA 2002 — This legal text always prevails over the Execution Engine. "
        "Confirmation > Schedule > Master Agreement > Code.",
        style_footer))
    story.append(Paragraph(
        "Prototype for academic demonstration only. Not for production use. Not legal or financial advice.",
        style_footer))

    # ── BUILD PDF ──────────────────────────────────────────────────────────
    doc.build(story)
    print(f"  [PDF] Generated: {output_path}")
    print(f"  [PDF] Governing Law: {params.governing_law}")
    print(f"  [PDF] Jurisdiction: {'English Courts' if is_english else 'NY Courts'}")
    return output_path


# ─── CLI / standalone usage ────────────────────────────────────────────────
if __name__ == "__main__":
    from engine import SwapParameters, PartyDetails

    # ── English Law contract ───────────────────────────────────────────────
    params_eng = SwapParameters(
        contract_id="SLC-IRS-EUR-001",
        party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer",
                             jurisdiction_code="GB"),
        party_b=PartyDetails("Beta Fund Ltd", "Beta", "floating_payer",
                             jurisdiction_code="FR"),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03200"),
        governing_law="English Law",
    )
    generate_contract_pdf(params_eng,
        os.path.join(_OUTPUTS_DIR, "SLC-IRS-EUR-001-EnglishLaw.pdf"))

    # ── New York Law contract ──────────────────────────────────────────────
    params_ny = SwapParameters(
        contract_id="SLC-IRS-USD-002",
        party_a=PartyDetails("Gamma Holdings Inc.", "Gamma", "fixed_payer",
                             jurisdiction_code="US"),
        party_b=PartyDetails("Delta Partners LLC", "Delta", "floating_payer",
                             jurisdiction_code="US"),
        notional=Decimal("25000000"),
        fixed_rate=Decimal("0.04100"),
        governing_law="New York Law",
        termination_currency="USD",
    )
    generate_contract_pdf(params_ny,
        os.path.join(_OUTPUTS_DIR, "SLC-IRS-USD-002-NewYorkLaw.pdf"))

    print("\n  Both PDFs generated. Compare §13 and §14 between the two.")
