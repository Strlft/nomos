"""
Regression test for P1-1: party identifier resolution in api_generate_notice().

Before fix: passing party_defaulting="B" caused the literal string "B" to appear
in the notice body (e.g. "B has failed to make a payment...").

After fix: api_generate_notice() resolves "A"/"B" to the full party name before
template substitution. "Beta Fund Ltd" appears; bare "B" does not appear in
narrative sentences.
"""

import sys, os, re
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _build_notice(tmp_path=None):
    os.environ["NOMOS_MODE"] = "demo"
    from api import (
        api_create_contract, api_sign_contract, api_demo_auto_validate,
        api_set_demo_mode, api_execute_period, api_approve_pi, api_generate_notice,
    )

    cid = "P1-1-RTEST"
    api_create_contract({
        "contract_id": cid,
        "party_a_name": "Alpha Corp S.A.", "party_a_jurisdiction": "FR",
        "party_b_name": "Beta Fund Ltd",   "party_b_jurisdiction": "GB",
        "notional": 10_000_000, "fixed_rate": 0.04,
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
        "amount": 11100.00,
        "currency": "EUR",
    })
    return nr


# ── Template / dict-level checks (no PDF needed) ────────────────────────────

def test_regression_P1_1_notice_dict_contains_full_name():
    """
    api_generate_notice() must return a dict whose 'body' (or 'content') key
    contains 'Beta Fund Ltd', not the bare identifier 'B'.
    """
    nr = _build_notice()
    body = nr.get("body", nr.get("content", ""))
    assert "Beta Fund Ltd" in body, (
        f"P1-1: 'Beta Fund Ltd' not found in notice body. "
        f"Party identifier was not resolved. body='{body[:300]}'"
    )


def test_regression_P1_1_notice_dict_no_bare_b_in_narrative():
    """
    The notice body must not contain ' B has ', ' B's ', or start a sentence
    with 'B has' — these are signs that the raw identifier leaked through.
    """
    nr = _build_notice()
    body = nr.get("body", nr.get("content", ""))

    assert " B has " not in body, (
        f"P1-1 regressed: ' B has ' found in notice body. "
        f"Raw party identifier leaked. body='{body[:300]}'"
    )
    assert not body.startswith("B has "), (
        "P1-1 regressed: notice body starts with 'B has '."
    )
    # "B's" as a possessive of the bare identifier
    assert " B's " not in body, (
        "P1-1 regressed: ' B\\'s ' found in notice body (bare identifier possessive)."
    )


# ── Full-pipeline PDF checks ─────────────────────────────────────────────────

def test_regression_P1_1_pdf_contains_full_name(tmp_path):
    """
    Full pipeline: generated FAILURE_TO_PAY notice PDF must contain 'Beta Fund Ltd'.
    """
    pytest.importorskip("pdfminer")
    from pdfminer.high_level import extract_text

    nr = _build_notice(tmp_path)
    npath = nr.get("pdf", nr.get("file_path", ""))
    assert npath and os.path.exists(npath), f"Notice PDF not found: {npath}"

    raw = extract_text(npath)
    text = re.sub(r" {2,}", " ", raw)

    assert "Beta Fund Ltd" in text, (
        "P1-1: 'Beta Fund Ltd' not found in generated notice PDF. "
        "Party identifier was not resolved before PDF generation."
    )


def test_regression_P1_1_pdf_no_bare_identifier(tmp_path):
    """
    Full pipeline: the notice PDF must not contain ' B has ' or ' B has failed'
    anywhere in the narrative text.
    """
    pytest.importorskip("pdfminer")
    from pdfminer.high_level import extract_text

    nr = _build_notice(tmp_path)
    npath = nr.get("pdf", nr.get("file_path", ""))
    assert npath and os.path.exists(npath), f"Notice PDF not found: {npath}"

    raw = extract_text(npath)
    text = re.sub(r" {2,}", " ", raw)

    assert " B has " not in text, (
        "P1-1 regressed: ' B has ' found in notice PDF. "
        "Raw party identifier leaked into generated document."
    )
    assert " B has failed" not in text, (
        "P1-1 regressed: ' B has failed' found in notice PDF."
    )
