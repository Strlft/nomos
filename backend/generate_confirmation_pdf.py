"""
=============================================================================
  CONFIRMATION & NOTICE GENERATOR
  ISDA 2002 · Vanilla IRS · EUR

  Two generators:
  1. generate_confirmation_pdf() — per-trade Confirmation document
  2. generate_notice_pdf() — formal §12 notices

  HIERARCHY CLAUSE (§1(b) ISDA 2002):
  The Confirmation prevails over the Schedule, which prevails over the MA.
  This Confirmation IS the highest-priority document in the stack.
=============================================================================
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from datetime import date, timedelta
from decimal import Decimal
import hashlib, json, os

_OUTPUTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")

# ─── Styles ───────────────────────────────────────────────────────────────
DK = HexColor("#1a1d21")
GR = HexColor("#666666")
AC = HexColor("#7dafc9")
WM = HexColor("#c4956a")
LB = HexColor("#F5F7FA")

styles = getSampleStyleSheet()
sH = ParagraphStyle("H", parent=styles["Title"], fontSize=16, textColor=DK,
    spaceAfter=4, alignment=TA_CENTER)
sSub = ParagraphStyle("Sub", parent=styles["Normal"], fontSize=10,
    textColor=GR, alignment=TA_CENTER, spaceAfter=14)
sB = ParagraphStyle("B", parent=styles["Normal"], fontSize=9.5, leading=13,
    alignment=TA_JUSTIFY, spaceAfter=6)
sBold = ParagraphStyle("BB", parent=sB, fontName="Helvetica-Bold")
sRef = ParagraphStyle("Ref", parent=sB, fontSize=8, textColor=GR,
    leftIndent=10, rightIndent=10)
sLbl = ParagraphStyle("Lbl", parent=styles["Normal"], fontSize=9,
    fontName="Helvetica-Bold", textColor=DK)
sVal = ParagraphStyle("Val", parent=styles["Normal"], fontSize=9)
sFt = ParagraphStyle("Ft", parent=styles["Normal"], fontSize=7,
    textColor=GR, alignment=TA_CENTER)

def _tbl(data, cw=None, hdr=True):
    t = Table(data, colWidths=cw)
    s = [
        ("FONTNAME", (0,0), (-1,-1), "Helvetica"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("GRID", (0,0), (-1,-1), 0.5, HexColor("#CCC")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
    ]
    if hdr:
        s += [
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("BACKGROUND", (0,0), (-1,0), DK),
            ("TEXTCOLOR", (0,0), (-1,0), HexColor("#FFF")),
        ]
    t.setStyle(TableStyle(s))
    return t


# ═══════════════════════════════════════════════════════════════════════════
# 1. CONFIRMATION PDF
# ═══════════════════════════════════════════════════════════════════════════

def generate_confirmation_pdf(params, schedule=None, initiation=None,
                                output_path=None, payment_schedule=None):
    """
    Generate a formal ISDA Confirmation PDF for a vanilla IRS.

    This is the document that CREATES the Transaction.
    It is the highest-priority document in the §1(b) hierarchy.

    Args:
        params: SwapParameters (economic terms)
        schedule: ScheduleElections (optional — referenced by ID)
        initiation: ContractInitiation (optional — tracks who/when)
        output_path: file path for the PDF
        payment_schedule: list of CalculationPeriod (optional — for schedule table)
    """
    if not output_path:
        output_path = os.path.join(_OUTPUTS_DIR, f"{params.contract_id}-Confirmation.pdf")

    doc = SimpleDocTemplate(output_path, pagesize=A4,
        topMargin=2*cm, bottomMargin=2*cm, leftMargin=2.5*cm, rightMargin=2.5*cm)
    story = []

    gov_law = params.governing_law if hasattr(params, 'governing_law') else "English Law"
    is_english = "English" in gov_law

    # ── HEADER ──────────────────────────────────────────────────────────────
    story.append(Spacer(1, 20))
    story.append(Paragraph("CONFIRMATION", sH))
    story.append(Paragraph("Interest Rate Swap Transaction", sSub))
    story.append(Spacer(1, 6))

    # Reference block
    ref_data = [
        ["Date:", str(initiation.initiated_date if initiation and initiation.initiated_date else date.today())],
        ["To:", params.party_b.name],
        ["From:", params.party_a.name],
        ["Re:", f"Interest Rate Swap Transaction — {params.contract_id}"],
    ]
    t = Table(ref_data, colWidths=[60, 400])
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    story.append(t)
    story.append(Spacer(1, 14))

    # ── PREAMBLE ────────────────────────────────────────────────────────────
    schedule_ref = (schedule.schedule_id if schedule else "N/A")
    story.append(Paragraph(
        f"Dear {params.party_b.short_name},", sB))
    story.append(Paragraph(
        f"The purpose of this letter agreement (this <b>\"Confirmation\"</b>) is to confirm "
        f"the terms and conditions of the Transaction entered into between us on the Trade Date "
        f"specified below (the <b>\"Transaction\"</b>). This Confirmation constitutes a "
        f"\"Confirmation\" as referred to in the ISDA 2002 Master Agreement dated as of "
        f"{schedule.date_of_agreement if schedule and schedule.date_of_agreement else '[date]'} "
        f"as amended and supplemented from time to time (the <b>\"Agreement\"</b>), between "
        f"{params.party_a.name} (<b>\"Party A\"</b>) and {params.party_b.name} "
        f"(<b>\"Party B\"</b>).", sB))
    story.append(Paragraph(
        "The definitions and provisions contained in the 2006 ISDA Definitions (as published "
        "by the International Swaps and Derivatives Association, Inc.) are incorporated into "
        "this Confirmation. In the event of any inconsistency between those definitions and "
        "provisions and this Confirmation, this Confirmation will govern.", sB))
    story.append(Paragraph(
        f"<b>Schedule Reference:</b> {schedule_ref}", sRef))
    story.append(Spacer(1, 10))

    # ── GENERAL TERMS ───────────────────────────────────────────────────────
    story.append(Paragraph("<b>1. General Terms</b>", sBold))
    general = [
        ["Trade Date:", str(initiation.initiated_date if initiation and initiation.initiated_date else date.today())],
        ["Effective Date:", str(params.effective_date)],
        ["Termination Date:", str(params.termination_date)],
        ["Notional Amount:", f"{params.currency} {params.notional:,.2f}"],
        ["Calculation Agent:", f"{params.calculation_agent} (§14 ISDA 2002)"],
        ["Business Days:", "TARGET2 and London" if is_english else "New York"],
        ["Business Day Convention:", "Modified Following (ISDA 2006 §4.12(ii))"],
    ]
    for row in general:
        story.append(Paragraph(f"<b>{row[0]}</b> {row[1]}", sB))
    story.append(Spacer(1, 8))

    # ── FIXED AMOUNTS ───────────────────────────────────────────────────────
    story.append(Paragraph("<b>2. Fixed Amounts</b>", sBold))
    fixed = [
        ["Fixed Rate Payer:", f"{params.party_a.name} (\"Party A\")"],
        ["Fixed Rate:", f"{params.fixed_rate * 100:.3f}% per annum"],
        ["Fixed Rate Day Count Fraction:", params.fixed_day_count],
        ["Fixed Rate Payer Payment Dates:", f"Quarterly, commencing {params.effective_date + timedelta(days=90):%d %B %Y}, "
         f"subject to the Business Day Convention, up to and including the Termination Date."],
    ]
    for row in fixed:
        story.append(Paragraph(f"<b>{row[0]}</b> {row[1]}", sB))
    story.append(Spacer(1, 8))

    # ── FLOATING AMOUNTS ────────────────────────────────────────────────────
    story.append(Paragraph("<b>3. Floating Amounts</b>", sBold))
    floating = [
        ["Floating Rate Payer:", f"{params.party_b.name} (\"Party B\")"],
        ["Floating Rate Option:", params.floating_index],
        ["Designated Maturity:", "3 Months"],
        ["Spread:", f"{params.floating_spread * 100 if params.floating_spread else 'None'}"],
        ["Floating Rate Day Count Fraction:", params.floating_day_count],
        ["Reset Dates:", "First day of each Calculation Period"],
        ["Floating Rate Payer Payment Dates:", "Same as Fixed Rate Payer Payment Dates"],
    ]
    for row in floating:
        story.append(Paragraph(f"<b>{row[0]}</b> {row[1]}", sB))
    story.append(Spacer(1, 8))

    # ── PAYMENT SCHEDULE (if available) ─────────────────────────────────────
    if payment_schedule:
        story.append(Paragraph("<b>4. Payment Schedule (Indicative)</b>", sBold))
        story.append(Paragraph(
            "<i>The following schedule is generated by the Execution Engine based on the "
            "terms above. Modified Following Business Day Convention applied (TARGET2 + London). "
            "Actual floating amounts depend on the EURIBOR fixing on each Reset Date.</i>", sRef))
        sched_data = [["#", "Period Start", "Period End", "Payment Date", "Fixed Amount"]]
        for p in payment_schedule:
            sched_data.append([
                str(p.period_number),
                str(p.start_date),
                str(p.end_date),
                str(p.payment_date),
                f"{params.currency} {80000:,.2f}"  # simplified — in production from engine
            ])
        story.append(_tbl(sched_data, cw=[30, 90, 90, 90, 100]))
        story.append(Spacer(1, 8))

    # ── ADDITIONAL PROVISIONS ───────────────────────────────────────────────
    next_section = 5 if payment_schedule else 4
    story.append(Paragraph(f"<b>{next_section}. Additional Provisions</b>", sBold))

    # MTPN
    mtpn = schedule.mtpn_elected if schedule else params.mtpn_elected
    story.append(Paragraph(
        f"<b>Multiple Transaction Payment Netting (§2(c)):</b> "
        f"{'Applicable' if mtpn else 'Not Applicable'} from the Effective Date.", sB))

    # CSA reference
    if schedule and schedule.csa_elected:
        story.append(Paragraph(
            f"<b>Credit Support:</b> This Transaction is subject to the Credit Support Annex "
            f"dated as of {schedule.date_of_agreement or '[date]'} between the parties. "
            f"Threshold (Party A): {params.currency} {schedule.csa_threshold_party_a:,.0f}. "
            f"Threshold (Party B): {params.currency} {schedule.csa_threshold_party_b:,.0f}. "
            f"Minimum Transfer Amount: {params.currency} {schedule.csa_mta:,.0f}.", sB))

    # Oracle
    story.append(Paragraph(
        f"<b>Rate Source:</b> {params.oracle_source}. "
        f"Fallback: ISDA 2021 Benchmark Fallback Rate ({params.oracle_fallback_rate*100:.3f}%). "
        f"Challenge threshold: {params.oracle_challenge_threshold_bps} basis points.", sB))
    story.append(Spacer(1, 8))

    # ── HIERARCHY CLAUSE ────────────────────────────────────────────────────
    next_section += 1
    story.append(Paragraph(f"<b>{next_section}. Hierarchy</b>", sBold))
    story.append(Paragraph(
        "In the event of any inconsistency between this Confirmation and the Schedule to "
        "the Agreement, this Confirmation will prevail. In the event of any inconsistency "
        "between the Schedule and the printed form of the Master Agreement, the Schedule will "
        "prevail. The Execution Engine is subordinate to this Confirmation in all cases.", sB))
    story.append(Spacer(1, 8))

    # ── GOVERNING LAW ───────────────────────────────────────────────────────
    next_section += 1
    story.append(Paragraph(f"<b>{next_section}. Governing Law</b>", sBold))
    story.append(Paragraph(
        f"This Confirmation will be governed by and construed in accordance with "
        f"{'English law' if is_english else 'the laws of the State of New York'}.", sB))
    story.append(Spacer(1, 12))

    # ── SIGNATURE BLOCKS ────────────────────────────────────────────────────
    story.append(Paragraph(
        "Please confirm that the foregoing correctly sets forth the terms of our agreement "
        "by executing this Confirmation and returning it to us.", sB))
    story.append(Spacer(1, 20))

    sig_data = [
        [f"{params.party_a.name}\n(Party A — Fixed Rate Payer)",
         f"{params.party_b.name}\n(Party B — Floating Rate Payer)"],
        ["\n\nBy: ____________________________\nName:\nTitle:\nDate:",
         "\n\nBy: ____________________________\nName:\nTitle:\nDate:"],
    ]
    t = Table(sig_data, colWidths=[230, 230])
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(t)

    # ── FOOTER ──────────────────────────────────────────────────────────────
    story.append(Spacer(1, 30))

    # Confirmation hash
    conf_data = json.dumps({
        "contract_id": params.contract_id,
        "party_a": params.party_a.name,
        "party_b": params.party_b.name,
        "notional": str(params.notional),
        "fixed_rate": str(params.fixed_rate),
        "effective_date": str(params.effective_date),
        "termination_date": str(params.termination_date),
    }, sort_keys=True)
    conf_hash = hashlib.sha256(conf_data.encode()).hexdigest()

    story.append(Paragraph(
        f"Confirmation fingerprint (SHA-256): {conf_hash}", sRef))
    story.append(Paragraph(
        "§1(b) ISDA 2002 — This Confirmation prevails over the Schedule and the Master Agreement.",
        sFt))

    doc.build(story)
    print(f"  [CONFIRMATION] Generated: {output_path}")
    print(f"  [CONFIRMATION] Hash: {conf_hash[:16]}...")
    return output_path, conf_hash


# ═══════════════════════════════════════════════════════════════════════════
# 2. NOTICE PDF
# ═══════════════════════════════════════════════════════════════════════════

NOTICE_TEMPLATES = {
    "FAILURE_TO_PAY": {
        "title": "Notice of Failure to Pay",
        "isda_ref": "§5(a)(i) ISDA 2002",
        "body": (
            "We hereby notify you that an Event of Default has occurred under "
            "Section 5(a)(i) (Failure to Pay or Deliver) of the Agreement. "
            "{party_defaulting} has failed to make a payment of {currency} {amount} "
            "due on {due_date} under Transaction {contract_id}. "
            "The applicable grace period of {grace_period} has expired as of {grace_end}."
        ),
        "consequence": (
            "As a result of the occurrence and continuance of this Event of Default, "
            "and in accordance with Section 2(a)(iii) of the Agreement, our obligation "
            "to make any further payments under the Agreement is suspended. "
            "We reserve all rights under the Agreement, including the right to designate "
            "an Early Termination Date pursuant to Section 6(a)."
        ),
    },
    "BREACH_OF_AGREEMENT": {
        "title": "Notice of Breach of Agreement",
        "isda_ref": "§5(a)(ii) ISDA 2002",
        "body": (
            "We hereby notify you that a breach has occurred under Section 5(a)(ii) "
            "(Breach of Agreement) of the Agreement. {party_defaulting} has failed to "
            "comply with or perform the obligation specified below:\n\n"
            "Obligation: {obligation}\n"
            "Due date: {due_date}\n"
            "Section: {section}"
        ),
        "consequence": (
            "In accordance with Section 5(a)(ii), you have 30 days from the date of "
            "this notice to remedy the breach. If the breach is not remedied within this "
            "period, it will constitute an Event of Default and we may designate an "
            "Early Termination Date pursuant to Section 6(a)."
        ),
    },
    "ETD_DESIGNATION": {
        "title": "Designation of Early Termination Date",
        "isda_ref": "§6(a) ISDA 2002",
        "body": (
            "We refer to the Event of Default notified to you on {eod_notice_date} "
            "(the \"{eod_type}\"). The Event of Default is continuing.\n\n"
            "Pursuant to Section 6(a) of the Agreement, we hereby designate "
            "{etd_date} as the Early Termination Date in respect of all outstanding "
            "Transactions under the Agreement."
        ),
        "consequence": (
            "In accordance with Section 6(e), we will calculate the Early Termination "
            "Amount as the Determining Party. The Early Termination Amount will be "
            "determined as of the Early Termination Date. Payment of the Early "
            "Termination Amount will be made in {currency} within two Local Business "
            "Days of notice of the amount."
        ),
    },
    "DELIVERY_REMINDER": {
        "title": "Reminder — Delivery of Specified Information",
        "isda_ref": "§4(a) ISDA 2002",
        "body": (
            "We write to remind you of your obligation under Section 4(a) of the "
            "Agreement to deliver the following:\n\n"
            "Document: {document}\n"
            "Due date: {due_date}\n\n"
            "As of the date of this letter, we have not received this document. "
            "Please arrange for delivery at your earliest convenience."
        ),
        "consequence": (
            "Please note that failure to deliver Specified Information may affect "
            "the accuracy of representations made under Section 3(d) of the Agreement "
            "and may constitute a breach under Section 5(a)(ii) if not remedied."
        ),
    },
    "TAX_CHANGE": {
        "title": "Notice of Change in Tax Status",
        "isda_ref": "§4(d) ISDA 2002",
        "body": (
            "In accordance with Section 4(d) of the Agreement, we hereby notify you "
            "that a change has occurred which may affect our ability to make payments "
            "under the Agreement free of withholding tax.\n\n"
            "Nature of change: {description}\n"
            "Effective date: {effective_date}"
        ),
        "consequence": (
            "This notification is made in accordance with our obligations under "
            "Section 4(d). We will provide updated tax forms as soon as practicable. "
            "This change may constitute a Tax Event under Section 5(b)(iii) of the "
            "Agreement, in which case either party may designate an Early Termination "
            "Date in respect of the Affected Transactions pursuant to Section 6(b)(ii)."
        ),
    },
}


def generate_notice_pdf(notice_type, from_party, to_party, contract_id,
                         details, output_path=None, governing_law="English Law"):
    """
    Generate a formal §12 ISDA 2002 notice PDF.

    Args:
        notice_type: key from NOTICE_TEMPLATES
        from_party: name of the notifying party
        to_party: name of the receiving party
        contract_id: contract reference
        details: dict with template variables (amount, due_date, etc.)
        output_path: file path
        governing_law: for jurisdiction reference
    """
    template = NOTICE_TEMPLATES.get(notice_type)
    if not template:
        raise ValueError(f"Unknown notice type: {notice_type}")

    if not output_path:
        output_path = os.path.join(_OUTPUTS_DIR, f"{contract_id}-Notice-{notice_type}.pdf")

    doc = SimpleDocTemplate(output_path, pagesize=A4,
        topMargin=2*cm, bottomMargin=2*cm, leftMargin=2.5*cm, rightMargin=2.5*cm)
    story = []

    # Header
    story.append(Spacer(1, 20))
    story.append(Paragraph(template["title"].upper(), sH))
    story.append(Paragraph(f"under the ISDA 2002 Master Agreement — {template['isda_ref']}", sSub))
    story.append(Spacer(1, 10))

    # Reference
    story.append(Paragraph(f"<b>Date:</b> {date.today():%d %B %Y}", sB))
    story.append(Paragraph(f"<b>To:</b> {to_party}", sB))
    story.append(Paragraph(f"<b>From:</b> {from_party}", sB))
    story.append(Paragraph(f"<b>Contract:</b> {contract_id}", sB))
    story.append(Spacer(1, 14))

    # Body
    body_text = template["body"].format(
        contract_id=contract_id,
        **{k: v for k, v in details.items()}
    )
    for para in body_text.split("\n\n"):
        if para.strip():
            story.append(Paragraph(para.strip(), sB))
            story.append(Spacer(1, 4))
    story.append(Spacer(1, 8))

    # Consequence
    consequence = template["consequence"].format(
        **{k: v for k, v in details.items()}
    )
    story.append(Paragraph(consequence, sB))
    story.append(Spacer(1, 14))

    # Delivery method
    story.append(Paragraph(
        "This notice is given in accordance with Section 12 of the Agreement. "
        "Delivery by email is effective on the date of delivery pursuant to "
        "Section 12(a)(vi).", sRef))
    story.append(Spacer(1, 20))

    # Signature
    story.append(Paragraph("Yours faithfully,", sB))
    story.append(Spacer(1, 30))
    story.append(Paragraph(f"____________________________", sB))
    story.append(Paragraph(f"For and on behalf of <b>{from_party}</b>", sB))

    # Footer
    story.append(Spacer(1, 30))
    notice_data = json.dumps({
        "type": notice_type, "from": from_party, "to": to_party,
        "contract": contract_id, "date": str(date.today()),
        "details": {k: str(v) for k, v in details.items()}
    }, sort_keys=True)
    notice_hash = hashlib.sha256(notice_data.encode()).hexdigest()

    story.append(Paragraph(f"Notice fingerprint: {notice_hash}", sRef))
    story.append(Paragraph(
        f"§12 ISDA 2002 · {template['isda_ref']} · "
        f"{'English law' if 'English' in governing_law else 'New York law'}",
        sFt))

    doc.build(story)
    print(f"  [NOTICE] Generated: {output_path}")
    print(f"  [NOTICE] Type: {notice_type} · Hash: {notice_hash[:16]}...")
    return output_path, notice_hash


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from engine import SwapParameters, PartyDetails, ScheduleElections, ContractInitiation

    # --- Confirmation ---
    sched = ScheduleElections(
        schedule_id="MA-ALPHA-BETA-001",
        date_of_agreement=date(2026, 2, 1),
        governing_law="English Law",
        csa_elected=True,
        csa_threshold_party_a=Decimal("500000"),
        csa_threshold_party_b=Decimal("500000"),
        csa_mta=Decimal("50000"),
    )
    init = ContractInitiation(
        initiated_by="CLIENT",
        initiated_date=date(2026, 3, 10),
        schedule_ref="MA-ALPHA-BETA-001",
    )
    params = SwapParameters(
        contract_id="SLC-IRS-EUR-001",
        party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer", jurisdiction_code="GB"),
        party_b=PartyDetails("Beta Fund Ltd", "Beta", "floating_payer", jurisdiction_code="FR"),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03200"),
    )
    generate_confirmation_pdf(params, schedule=sched, initiation=init)

    # --- Notices ---
    generate_notice_pdf("FAILURE_TO_PAY", "Alpha Corp S.A.", "Delta Capital Ltd",
        "SLC-IRS-EUR-003", {
            "party_defaulting": "Delta Capital Ltd",
            "currency": "EUR", "amount": "1,875.00",
            "due_date": "16 March 2026",
            "grace_period": "1 Local Business Day",
            "grace_end": "17 March 2026",
        })

    generate_notice_pdf("BREACH_OF_AGREEMENT", "Alpha Corp S.A.", "Beta Fund Ltd",
        "SLC-IRS-EUR-002", {
            "party_defaulting": "Beta Fund Ltd",
            "obligation": "Annual audited financial statements FY2025",
            "due_date": "30 April 2026",
            "section": "§4(a)(ii)",
        })

    generate_notice_pdf("DELIVERY_REMINDER", "Alpha Corp S.A.", "Beta Fund Ltd",
        "SLC-IRS-EUR-002", {
            "document": "Annual audited financial statements FY2025",
            "due_date": "30 April 2026",
        })

    generate_notice_pdf("ETD_DESIGNATION", "Alpha Corp S.A.", "Delta Capital Ltd",
        "SLC-IRS-EUR-003", {
            "eod_notice_date": "18 March 2026",
            "eod_type": "§5(a)(i) Failure to Pay",
            "etd_date": "25 March 2026",
            "currency": "EUR",
        })

    generate_notice_pdf("TAX_CHANGE", "Beta Fund Ltd", "Alpha Corp S.A.",
        "SLC-IRS-EUR-001", {
            "description": "Change in French withholding tax treatment following legislative amendment",
            "effective_date": "1 January 2027",
        })

    print("\n  All Confirmation + Notices generated.")
