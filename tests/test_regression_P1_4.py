"""
Regression test for P1-4: rate_override parameter in api_execute_period().

Before fix: api_execute_period() had no rate_override parameter. The only way
to get a known floating rate in tests was to mock the oracle or rely on network.

After fix: passing rate_override=<float> bypasses the oracle fetch, uses the
supplied rate, and records ORACLE_RATE_OVERRIDE in the audit trail.
"""

import sys, os
import pytest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _setup_contract(cid: str):
    os.environ["NOMOS_MODE"] = "demo"
    from api import (
        api_create_contract, api_sign_contract, api_demo_auto_validate,
        api_set_demo_mode,
    )
    api_create_contract({
        "contract_id": cid,
        "party_a_name": "Alpha Corp S.A.", "party_a_jurisdiction": "FR",
        "party_b_name": "Beta Fund Ltd",   "party_b_jurisdiction": "GB",
        "notional": 10_000_000, "fixed_rate": 0.035,
        "effective_date": "2026-05-01", "termination_date": "2028-05-01",
        "governing_law": "English Law",
    })
    api_set_demo_mode(True)
    api_demo_auto_validate(cid)
    api_sign_contract(cid, "Beta Fund Ltd — Head of Trading", "B")


def test_regression_P1_4_rate_override_accepted():
    """
    api_execute_period() must accept rate_override kwarg without error.
    """
    from api import api_execute_period
    import inspect
    sig = inspect.signature(api_execute_period)
    assert "rate_override" in sig.parameters, (
        "P1-4: api_execute_period() does not have rate_override parameter."
    )


def test_regression_P1_4_engine_accepts_rate_override():
    """
    IRSExecutionEngine.run_calculation_cycle() must accept rate_override kwarg.
    """
    from engine import IRSExecutionEngine
    import inspect
    sig = inspect.signature(IRSExecutionEngine.run_calculation_cycle)
    assert "rate_override" in sig.parameters, (
        "P1-4: run_calculation_cycle() does not have rate_override parameter."
    )


def test_regression_P1_4_rate_override_produces_deterministic_result():
    """
    Calling api_execute_period(cid, rate_override=0.03875) must:
    1. Succeed (return a result dict)
    2. The floating amount reflects exactly 3.875% EURIBOR
    3. The oracle_status is RATE_OVERRIDE
    """
    cid = "P1-4-RTEST-A"
    _setup_contract(cid)

    from api import api_execute_period
    result = api_execute_period(cid, rate_override=0.03875)

    assert result is not None, "P1-4: api_execute_period returned None with rate_override"
    assert result.get("status") != "SUSPENDED", (
        f"P1-4: unexpected SUSPENDED status: {result}"
    )

    # Verify the oracle status field
    assert result.get("oracle_status") == "RATE_OVERRIDE", (
        f"P1-4: expected oracle_status='RATE_OVERRIDE', got '{result.get('oracle_status')}'"
    )

    # Verify deterministic floating amount:
    # EUR 10M × 3.875% × ACT/360 (May-1 → Aug-1 = 92 days)
    # = 10,000,000 × 0.03875 × (92/360) ≈ EUR 9,902.78
    floating = result.get("floating_amount")
    assert floating is not None, "P1-4: floating_amount not in result"

    expected_approx = 10_000_000 * 0.03875 * (92 / 360)
    assert abs(float(floating) - expected_approx) < 5.0, (
        f"P1-4: floating_amount {floating:.2f} deviates too much from "
        f"expected {expected_approx:.2f} for rate_override=0.03875."
    )


def test_regression_P1_4_rate_override_audit_trail():
    """
    After api_execute_period() with rate_override, the contract audit trail
    must contain an ORACLE_RATE_OVERRIDE entry.
    """
    cid = "P1-4-RTEST-B"
    _setup_contract(cid)

    from api import api_execute_period, api_audit_trail
    api_execute_period(cid, rate_override=0.04)

    trail = api_audit_trail(cid)
    event_types = [e.get("event") for e in trail.get("entries", [])]

    assert "ORACLE_RATE_OVERRIDE" in event_types, (
        f"P1-4: ORACLE_RATE_OVERRIDE not found in audit trail.\n"
        f"Events: {event_types}"
    )


def test_regression_P1_4_no_rate_override_uses_oracle():
    """
    Calling api_execute_period() without rate_override must not produce
    a RATE_OVERRIDE oracle_status (it uses the real oracle).
    """
    cid = "P1-4-RTEST-C"
    _setup_contract(cid)

    from api import api_execute_period
    result = api_execute_period(cid)   # no rate_override

    if result and result.get("status") != "SUSPENDED":
        assert result.get("oracle_status") != "RATE_OVERRIDE", (
            "P1-4: oracle_status is RATE_OVERRIDE but no rate_override was passed."
        )
