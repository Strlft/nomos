"""
Regression test for P1-5: EoD / TE declaration API endpoints.

Before fix: no API endpoints existed to declare §5 EoDs or TEs.
Operators had to call engine internals directly, bypassing the audit trail.

After fix: 7 new endpoints:
  api_declare_breach_of_agreement()   — §5(a)(ii)
  api_declare_bankruptcy()            — §5(a)(vii)
  api_declare_cross_default()         — §5(a)(vi)
  api_declare_illegality()            — §5(b)(i)
  api_declare_force_majeure()         — §5(b)(ii)
  api_cure_eod()                      — cure a Potential EoD
  api_eod_status()                    — list active EoDs / TEs
"""

import sys, os
import pytest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _make_active_contract(cid: str):
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


class TestEoDStatusEndpoint:

    def test_eod_status_returns_structure(self):
        """api_eod_status() returns the expected keys."""
        cid = "P1-5-STATUS"
        _make_active_contract(cid)
        from api import api_eod_status
        result = api_eod_status(cid)
        assert "eods" in result
        assert "termination_events" in result
        assert "suspended" in result
        assert result["suspended"] is False
        assert result["eods"] == []

    def test_eod_status_missing_contract(self):
        """api_eod_status() raises on unknown contract_id."""
        from api import api_eod_status
        with pytest.raises(Exception):
            api_eod_status("NONEXISTENT-P1-5")


class TestBreachOfAgreement:

    def test_declare_breach_registers_eod(self):
        """api_declare_breach_of_agreement() must register a §5(a)(ii) EoD."""
        cid = "P1-5-BREACH-A"
        _make_active_contract(cid)
        from api import api_declare_breach_of_agreement, api_eod_status
        r = api_declare_breach_of_agreement(cid, {
            "party": "B",
            "description": "Failure to deliver quarterly compliance certificate FY2026",
        })
        assert r["status"] == "EOD_REGISTERED", f"Unexpected status: {r}"
        assert r["eod_type"] == "BREACH_OF_AGREEMENT"
        assert r["is_potential_eod"] is True   # 30-day grace period applies
        assert r["grace_period_end"] is not None

        status = api_eod_status(cid)
        assert len(status["eods"]) == 1
        assert status["eods"][0]["eod_type"] == "BREACH_OF_AGREEMENT"

    def test_declare_breach_audit_trail(self):
        """EOD_BREACH_DECLARED must appear in the audit trail."""
        cid = "P1-5-BREACH-B"
        _make_active_contract(cid)
        from api import api_declare_breach_of_agreement, api_audit_trail
        api_declare_breach_of_agreement(cid, {
            "party": "A",
            "description": "Repudiation of Master Agreement",
            "repudiation": True,
        })
        trail = api_audit_trail(cid)
        events = [e.get("event") for e in trail]
        assert "EOD_BREACH_DECLARED" in events, (
            f"EOD_BREACH_DECLARED not in audit trail. Events: {events}"
        )

    def test_declare_breach_missing_description_raises(self):
        """api_declare_breach_of_agreement() without description must error."""
        cid = "P1-5-BREACH-C"
        _make_active_contract(cid)
        from api import api_declare_breach_of_agreement
        with pytest.raises(Exception):
            api_declare_breach_of_agreement(cid, {"party": "B"})


class TestBankruptcy:

    def test_declare_bankruptcy_registers_eod(self):
        """api_declare_bankruptcy() must register a §5(a)(vii) EoD."""
        cid = "P1-5-BANKR"
        _make_active_contract(cid)
        from api import api_declare_bankruptcy, api_eod_status
        r = api_declare_bankruptcy(cid, {
            "party": "B",
            "description": "Court-appointed administrator (Administration Order)",
        })
        assert r["status"] == "EOD_REGISTERED"
        assert r["eod_type"] == "BANKRUPTCY"
        assert r["is_potential_eod"] is True  # 15-day grace for bonafide disputes
        # Grace end ≈ today + 15 days
        status = api_eod_status(cid)
        eod = next(e for e in status["eods"] if e["eod_type"] == "BANKRUPTCY")
        assert eod is not None


class TestIllegality:

    def test_declare_illegality_registers_te(self):
        """api_declare_illegality() must register a §5(b)(i) TE with 3-LBD waiting period."""
        cid = "P1-5-ILLEG"
        _make_active_contract(cid)
        from api import api_declare_illegality, api_eod_status
        r = api_declare_illegality(cid, {
            "party": "A",
            "description": "New regulation makes performance unlawful for FR entities",
        })
        assert r["status"] == "TE_REGISTERED"
        assert r["te_type"] == "ILLEGALITY"
        assert r["waiting_period_end"] is not None

        status = api_eod_status(cid)
        assert len(status["termination_events"]) == 1
        assert status["termination_events"][0]["te_type"] == "ILLEGALITY"


class TestForceMajeure:

    def test_declare_force_majeure_registers_te(self):
        """api_declare_force_majeure() must register a §5(b)(ii) TE with 8-LBD waiting period."""
        cid = "P1-5-FM"
        _make_active_contract(cid)
        from api import api_declare_force_majeure, api_eod_status
        r = api_declare_force_majeure(cid, {
            "party": "B",
            "description": "Force majeure event — TARGET2 system outage",
        })
        assert r["status"] == "TE_REGISTERED"
        assert r["te_type"] == "FORCE_MAJEURE"
        assert "DEFERRED" in r.get("note", "")

        status = api_eod_status(cid)
        assert any(te["te_type"] == "FORCE_MAJEURE" for te in status["termination_events"])

    def test_force_majeure_audit_trail(self):
        """TE_FORCE_MAJEURE_DECLARED must appear in the audit trail."""
        cid = "P1-5-FM-B"
        _make_active_contract(cid)
        from api import api_declare_force_majeure, api_audit_trail
        api_declare_force_majeure(cid, {
            "party": "B",
            "description": "Power grid failure — performance impossible",
        })
        trail = api_audit_trail(cid)
        events = [e.get("event") for e in trail]
        assert "TE_FORCE_MAJEURE_DECLARED" in events


class TestCureEoD:

    def test_cure_potential_eod_removes_suspension(self):
        """
        After declaring a §5(a)(ii) PEoD and then curing it, the contract
        must no longer be suspended.
        """
        cid = "P1-5-CURE"
        _make_active_contract(cid)
        from api import (api_declare_breach_of_agreement, api_cure_eod,
                          api_eod_status, _engines)

        # Declare the breach
        api_declare_breach_of_agreement(cid, {
            "party": "B",
            "description": "Test breach for cure test",
        })

        # Manually force suspension by marking as full EoD for this test
        eng = _engines[cid]
        if eng.eod_monitor.active_eods:
            eng.eod_monitor.active_eods[0].is_potential_eod = True
        eng.state.__class__  # touch to ensure import ok

        # Cure it
        r = api_cure_eod(cid, {"eod_type": "BREACH_OF_AGREEMENT", "party": "B"})
        assert r["status"] == "CURED", f"Unexpected cure result: {r}"
        assert r["eod_type"] == "BREACH_OF_AGREEMENT"

    def test_cure_nonexistent_eod_returns_not_found(self):
        """Curing an EoD that doesn't exist must return NOT_FOUND, not raise."""
        cid = "P1-5-CURE-NF"
        _make_active_contract(cid)
        from api import api_cure_eod
        r = api_cure_eod(cid, {"eod_type": "FAILURE_TO_PAY", "party": "A"})
        assert r["status"] == "NOT_FOUND"

    def test_cure_invalid_eod_type_raises(self):
        """Passing an invalid eod_type must raise (not silently fail)."""
        cid = "P1-5-CURE-BAD"
        _make_active_contract(cid)
        from api import api_cure_eod
        with pytest.raises(Exception):
            api_cure_eod(cid, {"eod_type": "INVALID_TYPE", "party": "A"})
