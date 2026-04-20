"""
=============================================================================
  CONFIRMATION & NOTICE GENERATOR
  ISDA 2002-framework · Vanilla IRS · EUR

  Two generators:
  1. generate_confirmation_pdf() — per-trade Confirmation document
  2. generate_notice_pdf() — formal §12 notices

  HIERARCHY CLAUSE (§1(b)):
  The Confirmation prevails over the Schedule, which prevails over the MA.
  This Confirmation IS the highest-priority document in the stack.

  TEMPLATE SYSTEM:
  Clause wording is loaded from templates/<template_id>.json at runtime.
  Law firms may supply a replacement JSON with the same clause keys to
  substitute their own preferred wording.
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
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

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


def _load_template(template_id):
    """Load clause texts from templates/<template_id>.json."""
    path = os.path.join(_TEMPLATES_DIR, f"{template_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Notice metadata (title and §-reference only; body/consequence in JSON) ──
NOTICE_TEMPLATES = {
    "FAILURE_TO_PAY": {
        "title": "Notice of Failure to Pay",
        "isda_ref": "§5(a)(i)",
        "body_key": "notice_failure_to_pay_body",
        "consequence_key": "notice_failure_to_pay_consequence",
    },
    "BREACH_OF_AGREEMENT": {
        "title": "Notice of Breach of Agreement",
        "isda_ref": "§5(a)(ii)",
        "body_key": "notice_breach_body",
        "consequence_key": "notice_breach_consequence",
    },
    "ETD_DESIGNATION": {
        "title": "Designation of Early Termination Date",
        "isda_ref": "§6(a)",
        "body_key": "notice_etd_body",
        "consequence_key": "notice_etd_consequence",
    },
    "DELIVERY_REMINDER": {
        "title": "Reminder — Delivery of Specified Information",
        "isda_ref": "§4(a)",
        "body_key": "notice_delivery_reminder_body",
        "consequence_key": "notice_delivery_reminder_consequence",
    },
    "TAX_CHANGE": {
        "title": "Notice of Change in Tax Status",
        "isda_ref": "§4(d)",
        "body_key": "notice_tax_change_body",
        "consequence_key": "notice_tax_change_consequence",
    },
}


def _compute_fixed_amount(params, period) -> str:
    """
    Compute indicative fixed amount for the schedule table using the 30/360 day count.
    If the period already has a calculated fixed_amount, use that.
    Otherwise compute from dates using the 30/360 convention.
    """
    if period.fixed_amount is not None:
        return f"{params.currency} {float(period.fixed_amount):,.2f}"
    # 30/360: (Y2-Y1)*360 + (M2-M1)*30 + min(D2,30) - min(D1,30)
    sd, ed = period.start_date, period.end_date
    days_30_360 = ((ed.year - sd.year) * 360
                   + (ed.month - sd.month) * 30
                   + min(ed.day, 30) - min(sd.day, 30))
    dcf = days_30_360 / 360
    fixed = float(params.notional) * float(params.fixed_rate) * dcf
    return f"{params.currency} {fixed:,.2f}"


# ═══════════════════════════════════════════════════════════════════════════
# 1. CONFIRMATION PDF
# ═══════════════════════════════════════════════════════════════════════════

def generate_confirmation_pdf(params, schedule=None, initiation=None,
                               template_id="nomos_standard_v1",
                               output_path=None, payment_schedule=None):
    """
    Generate a formal Confirmation PDF for a vanilla IRS.

    This document creates the Transaction and sits at the top of the §1(b)
    hierarchy. Clause wording is read from templates/<template_id>.json.

    Args:
        params: SwapParameters (economic terms)
        schedule: ScheduleElections (optional — referenced by ID)
        initiation: ContractInitiation (optional — tracks who/when)
        template_id: clause template to use (default: "nomos_standard_v1")
        output_path: file path for the PDF
        payment_schedule: list of CalculationPeriod (optional — for schedule table)
    """
    tpl = _load_template(template_id)

    if not output_path:
        output_path = os.path.join(_OUTPUTS_DIR, f"{params.contract_id}-Confirmation.pdf")

    doc = SimpleDocTemplate(output_path, pagesize=A4,
        topMargin=2*cm, bottomMargin=2*cm, leftMargin=2.5*cm, rightMargin=2.5*cm)
    story = []

    gov_law = params.governing_law if hasattr(params, 'governing_law') else "English Law"
    is_english = "english" in gov_law.lower()

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
    # Use master_agreement_date if set (MA and Schedule are separate docs executed together);
    # fall back to date_of_agreement (Schedule date) per common practice.
    _ma_date = (
        getattr(schedule, 'master_agreement_date', None) or
        (schedule.date_of_agreement if schedule else None)
    )
    date_of_agreement = str(_ma_date) if _ma_date else "[date]"

    story.append(Paragraph(f"Dear {params.party_b.short_name},", sB))
    story.append(Paragraph(
        tpl["confirmation_preamble"].format(
            date_of_agreement=date_of_agreement,
            party_a_name=params.party_a.name,
            party_b_name=params.party_b.name,
        ), sB))
    story.append(Paragraph(tpl["confirmation_definitions_note"], sB))
    story.append(Paragraph(f"<b>Schedule Reference:</b> {schedule_ref}", sRef))
    story.append(Spacer(1, 10))

    # ── GENERAL TERMS ───────────────────────────────────────────────────────
    story.append(Paragraph("<b>1. General Terms</b>", sBold))
    general = [
        ["Trade Date:", str(initiation.initiated_date if initiation and initiation.initiated_date else date.today())],
        ["Effective Date:", str(params.effective_date)],
        ["Termination Date:", str(params.termination_date)],
        ["Notional Amount:", f"{params.currency} {params.notional:,.2f}"],
        ["Calculation Agent:", f"{params.calculation_agent} (§14)"],
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
        ["Fixed Rate Payer Payment Dates:", (
            f"Quarterly, commencing {payment_schedule[0].payment_date:%d %B %Y}, "
            if payment_schedule else
            f"Quarterly, subject to the Business Day Convention, "
        ) + "up to and including the Termination Date."],
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
                _compute_fixed_amount(params, p)
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
        def _csa_amt(v):
            return f"{params.currency} {v:,.0f}" if v is not None else "Not specified"
        story.append(Paragraph(
            f"<b>Credit Support:</b> This Transaction is subject to the Credit Support Annex "
            f"dated as of {schedule.date_of_agreement or '[date]'} between the parties. "
            f"Threshold (Party A): {_csa_amt(schedule.csa_threshold_party_a)}. "
            f"Threshold (Party B): {_csa_amt(schedule.csa_threshold_party_b)}. "
            f"Minimum Transfer Amount: {_csa_amt(schedule.csa_mta)}.", sB))

    # Oracle
    story.append(Paragraph(
        f"<b>Rate Source:</b> {params.oracle_source}. "
        f"Fallback: ISDA 2021 Benchmark Fallback Rate ({params.oracle_fallback_rate*100:.3f}%). "
        f"Challenge threshold: {params.oracle_challenge_threshold_bps} basis points.", sB))
    story.append(Spacer(1, 8))

    # ── HIERARCHY CLAUSE ────────────────────────────────────────────────────
    next_section += 1
    story.append(Paragraph(f"<b>{next_section}. Hierarchy</b>", sBold))
    story.append(Paragraph(tpl["confirmation_hierarchy"], sB))
    story.append(Spacer(1, 8))

    # ── GOVERNING LAW ───────────────────────────────────────────────────────
    next_section += 1
    story.append(Paragraph(f"<b>{next_section}. Governing Law</b>", sBold))
    story.append(Paragraph(
        tpl["clause_13a_english"] if is_english else tpl["clause_13a_newyork"], sB))
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

    story.append(Paragraph(tpl["footer_isda_disclaimer"], sRef))
    story.append(Paragraph(
        f"Confirmation fingerprint (SHA-256): {conf_hash}", sRef))
    story.append(Paragraph(
        "§1(b) — This Confirmation takes precedence over the Schedule and the Master Agreement.",
        sFt))

    doc.build(story)
    print(f"  [CONFIRMATION] Generated: {output_path}")
    print(f"  [CONFIRMATION] Template:  {template_id}")
    print(f"  [CONFIRMATION] Hash: {conf_hash[:16]}...")
    return output_path, conf_hash


# ═══════════════════════════════════════════════════════════════════════════
# 2. NOTICE PDF
# ═══════════════════════════════════════════════════════════════════════════

def generate_notice_pdf(notice_type, from_party, to_party, contract_id,
                         details, output_path=None, governing_law="English Law",
                         template_id="nomos_standard_v1"):
    """
    Generate a formal §12 notice PDF.

    Args:
        notice_type: key from NOTICE_TEMPLATES
        from_party: name of the notifying party
        to_party: name of the receiving party
        contract_id: contract reference
        details: dict with template variables (amount, due_date, etc.)
        output_path: file path
        governing_law: for jurisdiction reference
        template_id: clause template to use (default: "nomos_standard_v1")
    """
    template = NOTICE_TEMPLATES.get(notice_type)
    if not template:
        raise ValueError(f"Unknown notice type: {notice_type}")

    tpl = _load_template(template_id)

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

    # Body — loaded from template JSON
    body_text = tpl[template["body_key"]].format(
        contract_id=contract_id,
        **{k: v for k, v in details.items()}
    )
    for para in body_text.split("\n\n"):
        if para.strip():
            story.append(Paragraph(para.strip(), sB))
            story.append(Spacer(1, 4))
    story.append(Spacer(1, 8))

    # Consequence — loaded from template JSON
    consequence = tpl[template["consequence_key"]].format(
        **{k: v for k, v in details.items()}
    )
    story.append(Paragraph(consequence, sB))
    story.append(Spacer(1, 14))

    # Delivery method
    story.append(Paragraph(
        "This notice is given in accordance with §12 of the Agreement. "
        "Electronic delivery takes effect on the date of receipt at the address specified in the Schedule.",
        sRef))
    story.append(Spacer(1, 20))

    # Signature
    story.append(Paragraph("Yours faithfully,", sB))
    story.append(Spacer(1, 30))
    story.append(Paragraph("____________________________", sB))
    story.append(Paragraph(f"For and on behalf of <b>{from_party}</b>", sB))

    # Footer
    story.append(Spacer(1, 30))
    notice_data = json.dumps({
        "type": notice_type, "from": from_party, "to": to_party,
        "contract": contract_id, "date": str(date.today()),
        "details": {k: str(v) for k, v in details.items()}
    }, sort_keys=True)
    notice_hash = hashlib.sha256(notice_data.encode()).hexdigest()

    story.append(Paragraph(tpl["footer_isda_disclaimer"], sRef))
    story.append(Paragraph(f"Notice fingerprint: {notice_hash}", sRef))
    story.append(Paragraph(
        f"§12 · {template['isda_ref']} · "
        f"{'English law' if 'english' in governing_law.lower() else 'New York law'}",
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
