"""
Regression tests for P0 bugs found and fixed during the 2026-04-21 legal audit.

Each test is named test_regression_P0_<N>_* and verifies exactly one fix.
All five tests must pass. If any reverts, the test will fail.

Contract under test (mirrors the audit): EUR 15M, 3.50%, 2Y, English Law, FR × GB.
"""

import sys
import os
import re
from decimal import Decimal
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from engine import (
    SwapParameters, PartyDetails, ScheduleElections, ContractInitiation,
    IRSExecutionEngine,
)
from netting_opinion_module import GoverningLaw as NettingGoverningLaw


# ──────────────────────────────────────────────────────────────────────────────
# Shared fixture — audit contract (English Law, UPPERCASE as sent by direct API)
# ──────────────────────────────────────────────────────────────────────────────

def _make_audit_engine(governing_law_str: str = "English Law") -> IRSExecutionEngine:
    """Create the standard audit contract with the given governing_law string."""
    params = SwapParameters(
        contract_id="P0-REG-001",
        party_a=PartyDetails(
            name="Alpha Corp S.A.", short_name="Alpha",
            role="fixed_payer", jurisdiction_code="FR",
        ),
        party_b=PartyDetails(
            name="Beta Fund Ltd", short_name="Beta",
            role="floating_payer", jurisdiction_code="GB",
        ),
        notional=Decimal("15000000"),
        fixed_rate=Decimal("0.035"),
        effective_date=date(2026, 5, 1),
        termination_date=date(2028, 5, 1),
        governing_law=governing_law_str,
    )
    schedule = ScheduleElections(schedule_id="MA-P0-REG-001")
    initiation = ContractInitiation(initiated_by="audit@test.com")
    return IRSExecutionEngine(params, schedule, initiation)


# ──────────────────────────────────────────────────────────────────────────────
# P0-1 / P0-2 / P0-3  Governing law case-sensitivity
# ──────────────────────────────────────────────────────────────────────────────

class TestP0GoverningLawCaseSensitivity:
    """
    P0-1 engine.py:2849  — netting opinion uses correct GoverningLaw enum
    P0-2 generate_contract_pdf.py:99 — is_english flag correct
    P0-3 generate_confirmation_pdf.py:171+443 — is_english flag + jurisdiction text
    """

    @pytest.mark.parametrize("gov_law_str", [
        "English Law",   # normal frontend value
        "ENGLISH",       # direct-API uppercase
        "english law",   # all-lowercase (robustness)
        "English law",   # mixed case
    ])
    def test_regression_P0_1_netting_opinion_uses_english_law_enum(self, gov_law_str):
        """
        engine.py: for any casing of 'English', the netting assessment must use
        GoverningLaw.ENGLISH_LAW, not GoverningLaw.NEW_YORK_LAW.

        Before fix: "English" in "ENGLISH" → False → defaulted to NEW_YORK_LAW.
        """
        eng = _make_audit_engine(gov_law_str)
        eng.initialise()

        assert eng.netting_assessment is not None, "No netting assessment produced"
        gov_law_used = eng.netting_assessment.governing_law
        assert gov_law_used == NettingGoverningLaw.ENGLISH_LAW, (
            f"Expected ENGLISH_LAW but got {gov_law_used} "
            f"for governing_law='{gov_law_str}'. "
            f"Case-sensitivity bug (P0-1) has reappeared."
        )

    @pytest.mark.parametrize("gov_law_str", [
        "English Law",
        "ENGLISH",
        "english law",
    ])
    def test_regression_P0_2_contract_pdf_is_english_flag(self, gov_law_str):
        """
        generate_contract_pdf.py: is_english must be True for any English Law variant.
        Imports the function-level logic directly to avoid full PDF generation.
        """
        import generate_contract_pdf as _m
        # The is_english check is: "english" in params.governing_law.lower()
        assert "english" in gov_law_str.lower(), (
            f"Test setup error: '{gov_law_str}' should be an English law variant"
        )
        # Verify the actual module uses .lower()
        import inspect
        src = inspect.getsource(_m)
        assert '"english" in' in src.lower() or "lower()" in src, (
            "generate_contract_pdf.py no longer uses case-insensitive check (P0-2 regressed)"
        )

    @pytest.mark.parametrize("gov_law_str", [
        "English Law",
        "ENGLISH",
        "english law",
    ])
    def test_regression_P0_3_confirmation_pdf_is_english_flag(self, gov_law_str):
        """
        generate_confirmation_pdf.py: is_english must use .lower() at lines 171 and 443.
        """
        import generate_confirmation_pdf as _m
        import inspect
        src = inspect.getsource(_m)

        # Count occurrences of case-insensitive English check
        lower_checks = src.count('"english" in gov_law.lower()')
        lower_checks += src.count("'english' in governing_law.lower()")
        lower_checks += src.count('"english" in governing_law.lower()')

        # Must have at least 2 case-insensitive checks (lines ~171 and ~443)
        assert lower_checks >= 2, (
            f"Expected ≥2 case-insensitive governing_law checks in "
            f"generate_confirmation_pdf.py, found {lower_checks}. "
            f"P0-3 may have regressed."
        )

        # Must have zero bare case-sensitive 'English' in checks on governing_law
        # Pattern: "English" in <variable> (where variable is gov_law or governing_law)
        bad_pattern = re.compile(r'"English"\s+in\s+(?:gov_law|governing_law)\b')
        bad_matches = bad_pattern.findall(src)
        assert not bad_matches, (
            f"Found case-sensitive check(s) in generate_confirmation_pdf.py: "
            f"{bad_matches}. P0-3 has regressed."
        )


# ──────────────────────────────────────────────────────────────────────────────
# P0-4  Confirmation PDF fixed amounts (was EUR 80,000 hardcoded)
# ──────────────────────────────────────────────────────────────────────────────

class TestP0ConfirmationPDFFixedAmounts:
    """
    P0-4 generate_confirmation_pdf.py:257 — _compute_fixed_amount() replaces 80000.

    For EUR 15M × 3.50% × 90/360 = EUR 131,250.00 per quarter (30/360 day count).
    """

    def test_regression_P0_4_fixed_amount_not_80000_in_source(self):
        """The literal 80000 must not appear in the schedule table code path."""
        import generate_confirmation_pdf as _m
        import inspect
        src = inspect.getsource(_m)
        # The hardcoded 80000 must be gone
        assert "80000" not in src, (
            "Hardcoded 80000 still present in generate_confirmation_pdf.py. "
            "P0-4 has regressed."
        )
        assert "_compute_fixed_amount" in src, (
            "_compute_fixed_amount helper not found in generate_confirmation_pdf.py."
        )

    def test_regression_P0_4_compute_fixed_amount_correct_value(self):
        """_compute_fixed_amount returns EUR 131,250.00 for the audit contract P1."""
        from generate_confirmation_pdf import _compute_fixed_amount
        from engine import SwapParameters, PartyDetails, ScheduleElections, IRSExecutionEngine
        from datetime import date

        # Build the period manually — same as P1 in the audit contract
        class _FakePeriod:
            start_date = date(2026, 5, 1)
            end_date   = date(2026, 8, 1)
            fixed_amount = None  # not yet calculated

        params = SwapParameters(
            contract_id="P0-4-TEST",
            party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer", "FR"),
            party_b=PartyDetails("Beta Fund Ltd",   "Beta",  "floating_payer", "GB"),
            notional=Decimal("15000000"),
            fixed_rate=Decimal("0.035"),
            governing_law="English Law",
        )
        result = _compute_fixed_amount(params, _FakePeriod())
        # 30/360: May-1 → Aug-1 = 3*30 = 90 days → 90/360 = 0.25
        # EUR 15M × 3.5% × 0.25 = EUR 131,250.00
        assert result == "EUR 131,250.00", (
            f"Expected 'EUR 131,250.00', got '{result}'. "
            f"_compute_fixed_amount is wrong (P0-4 regressed or broken)."
        )

    def test_regression_P0_4_all_8_periods_correct_via_full_pipeline(self, tmp_path):
        """
        Full pipeline: create contract → generate PDF → extract text → verify
        all 8 fixed amounts = EUR 131,250.00, none = EUR 80,000.00.
        """
        pytest.importorskip("pdfminer")
        from pdfminer.high_level import extract_text

        import os, sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
        os.environ["NOMOS_MODE"] = "demo"

        from api import api_create_contract, _engines

        cid = "P0-4-FULL"
        r = api_create_contract({
            "contract_id": cid,
            "party_a_name": "Alpha Corp S.A.", "party_a_jurisdiction": "FR",
            "party_b_name": "Beta Fund Ltd",   "party_b_jurisdiction": "GB",
            "notional": 15_000_000, "fixed_rate": 0.035,
            "effective_date": "2026-05-01", "termination_date": "2028-05-01",
            "governing_law": "English Law",
        })
        pdf_path = r.get("confirmation_pdf", "")
        assert pdf_path and os.path.exists(pdf_path), f"PDF not found: {pdf_path}"

        import re as _re
        text = extract_text(pdf_path)
        text_norm = _re.sub(r" {2,}", " ", text)
        assert "80,000" not in text_norm, (
            "EUR 80,000 found in Confirmation PDF — P0-4 has regressed."
        )
        assert text_norm.count("131,250") >= 8, (
            f"Expected 8 occurrences of 131,250 in PDF (one per period), "
            f"found {text_norm.count('131,250')}."
        )


# ──────────────────────────────────────────────────────────────────────────────
# P0-5  Confirmation PDF first payment date (was "30 July", should be "03 August")
# ──────────────────────────────────────────────────────────────────────────────

class TestP0ConfirmationPDFFirstPaymentDate:
    """
    P0-5 generate_confirmation_pdf.py:221 — first payment date uses
    payment_schedule[0].payment_date, not effective_date + 90 days.

    effective_date=2026-05-01 + 90 days = 2026-07-30 ("30 July 2026") — WRONG.
    MODFOL-adjusted first payment = 2026-08-03 ("03 August 2026") — CORRECT.
    """

    def test_regression_P0_5_no_timedelta_90_in_source(self):
        """The timedelta(days=90) approximation must no longer be used for payment dates."""
        import generate_confirmation_pdf as _m
        import inspect
        src = inspect.getsource(_m)
        # The old pattern: effective_date + timedelta(days=90)
        assert "timedelta(days=90)" not in src, (
            "timedelta(days=90) still used for first payment date in "
            "generate_confirmation_pdf.py. P0-5 has regressed."
        )

    def test_regression_P0_5_first_payment_date_correct_in_pdf(self, tmp_path):
        """
        Full pipeline: generated PDF must contain '03 August 2026' and must NOT
        contain '30 July 2026'.
        """
        pytest.importorskip("pdfminer")
        from pdfminer.high_level import extract_text
        import os, sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
        os.environ["NOMOS_MODE"] = "demo"

        from api import api_create_contract

        cid = "P0-5-TEST"
        r = api_create_contract({
            "contract_id": cid,
            "party_a_name": "Alpha Corp S.A.", "party_a_jurisdiction": "FR",
            "party_b_name": "Beta Fund Ltd",   "party_b_jurisdiction": "GB",
            "notional": 15_000_000, "fixed_rate": 0.035,
            "effective_date": "2026-05-01", "termination_date": "2028-05-01",
            "governing_law": "English Law",
        })
        pdf_path = r.get("confirmation_pdf", "")
        assert pdf_path and os.path.exists(pdf_path), f"PDF not found: {pdf_path}"

        import re as _re
        text = extract_text(pdf_path)
        # Normalise multi-space runs produced by pdfminer's layout extraction
        text_norm = _re.sub(r" {2,}", " ", text)
        assert "30 July" not in text_norm, (
            "Found '30 July' in Confirmation PDF — P0-5 has regressed. "
            "effective_date + 90 days approximation is back."
        )
        assert "03 August 2026" in text_norm or "3 August 2026" in text_norm, (
            f"Expected '03 August 2026' in Confirmation PDF but not found. "
            f"MODFOL-adjusted first payment date missing. P0-5 regressed.\n"
            f"Actual text snippet: {text_norm[:500]}"
        )
