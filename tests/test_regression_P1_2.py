"""
Regression test for P1-2: §2(a)(iii) notice language.

Per Lomas v JFB Firth Rixson [2012] EWCA Civ 419 at [43]:
  "the condition precedent in Section 2(a)(iii) operates automatically."

The §12 failure-to-pay notice must NOT describe §2(a)(iii) as an affirmative
right to suspend. It MUST use the condition-precedent framing: the obligation
does not arise (it is not actively suspended).
"""

import sys, os, re, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# ── Template-level check (no PDF needed) ─────────────────────────────────────

def _load_template():
    tpl_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "nomos_standard_v1.json"
    )
    with open(tpl_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_regression_P1_2_template_no_exercising_right_language():
    """
    The notice_failure_to_pay_consequence in the template JSON must not contain
    'exercising our right' or 'right under §2(a)(iii) to suspend'.
    These phrases incorrectly frame §2(a)(iii) as an elected right
    (Lomas [2012] EWCA Civ 419 at [43]).
    """
    tpl = _load_template()
    consequence = tpl["notice_failure_to_pay_consequence"]

    assert "exercising our right" not in consequence, (
        "P1-2 regressed: 'exercising our right' found in notice_failure_to_pay_consequence. "
        "§2(a)(iii) is a condition precedent, not an elected right. "
        "Lomas v JFB Firth Rixson [2012] EWCA Civ 419 at [43]."
    )
    assert "right under §2(a)(iii) to suspend" not in consequence, (
        "P1-2 regressed: 'right under §2(a)(iii) to suspend' found."
    )


def test_regression_P1_2_template_has_condition_precedent_language():
    """
    The notice_failure_to_pay_consequence must use condition-precedent framing:
    - 'condition precedent' must appear
    - 'do not arise' (payment obligations do not arise) must appear
    """
    tpl = _load_template()
    consequence = tpl["notice_failure_to_pay_consequence"]

    assert "condition precedent" in consequence, (
        "P1-2: 'condition precedent' not found in notice_failure_to_pay_consequence. "
        "The Lomas-correct framing requires this phrase."
    )
    assert "do not arise" in consequence, (
        "P1-2: 'do not arise' not found. Payment obligations must be described as "
        "not arising (condition precedent not satisfied), not as actively suspended."
    )


def test_regression_P1_2_template_preserves_section_6a_right():
    """
    The notice must preserve the right to designate an ETD under §6(a).
    This is independent of the §2(a)(iii) condition-precedent issue.
    """
    tpl = _load_template()
    consequence = tpl["notice_failure_to_pay_consequence"]

    has_etd = ("Early Termination Date" in consequence or
               "Section 6(a)" in consequence or
               "§6(a)" in consequence)
    assert has_etd, (
        "The notice consequence must preserve the right to designate an "
        "Early Termination Date (§6(a)). This right was lost from the template."
    )


# ── Full-pipeline PDF check ───────────────────────────────────────────────────

def test_regression_P1_2_generated_notice_pdf_language(tmp_path):
    """
    Full pipeline: generate a FAILURE_TO_PAY notice, extract text, verify:
    1. 'exercising our right' NOT present
    2. 'condition precedent' present
    3. 'do not arise' present
    """
    pytest.importorskip("pdfminer")
    from pdfminer.high_level import extract_text

    os.environ["NOMOS_MODE"] = "demo"
    from api import (
        api_create_contract, api_sign_contract, api_demo_auto_validate,
        api_set_demo_mode, api_execute_period, api_approve_pi, api_generate_notice,
    )

    cid = "P1-2-RTEST"
    api_create_contract({
        "contract_id": cid,
        "party_a_name": "Alpha Corp S.A.", "party_a_jurisdiction": "FR",
        "party_b_name": "Beta Fund Ltd",   "party_b_jurisdiction": "GB",
        "notional": 15_000_000, "fixed_rate": 0.035,
        "effective_date": "2026-05-01", "termination_date": "2028-05-01",
        "governing_law": "English Law",
    })
    api_set_demo_mode(True)
    api_demo_auto_validate(cid)
    api_sign_contract(cid, "Beta Fund Ltd — Head of Trading", "B")
    api_execute_period(cid, 1)
    api_approve_pi(cid, 1, "Advisor")

    nr = api_generate_notice(cid, "FAILURE_TO_PAY", {
        "party_defaulting": "B",
        "due_date": "2026-08-03",
        "grace_period": "1 LBD",
        "grace_end": "2026-08-04",
        "amount": 16752.17,
        "currency": "EUR",
    })

    npath = nr.get("pdf", nr.get("file_path", ""))
    assert npath and os.path.exists(npath), f"Notice PDF not found: {npath}"

    raw = extract_text(npath)
    text = re.sub(r" {2,}", " ", raw)

    assert "exercising our right" not in text, (
        "P1-2 regressed: 'exercising our right' still in generated notice PDF."
    )
    assert "condition precedent" in text, (
        "P1-2: 'condition precedent' not found in generated notice PDF."
    )
    assert "do not arise" in text, (
        "P1-2: 'do not arise' not found in generated notice PDF."
    )
