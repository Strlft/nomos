"""
Regression test for P1-7: bilateral signing mode.

Before fix: "bilateral" was not a recognised contract_mode value; it defaulted
silently to "advisor_managed", so single-party signing activated the contract
immediately, bypassing the requirement for both parties to sign.

Also: peer_to_peer workflow status labels were asymmetric ("PENDING_INITIATOR"
/ "PENDING_COUNTERPARTY") instead of symmetric per-party labels.

After fix:
- "bilateral" is accepted as contract_mode and normalised to "peer_to_peer"
- Workflow status labels are symmetric: PENDING_BOTH_PARTIES, PENDING_PARTY_A,
  PENDING_PARTY_B
- Contract only reaches ACTIVE after both parties have signed
"""

import sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _base_payload(cid: str, mode: str) -> dict:
    return {
        "contract_id": cid,
        "party_a_name": "Alpha Corp S.A.", "party_a_jurisdiction": "FR",
        "party_b_name": "Beta Fund Ltd",   "party_b_jurisdiction": "GB",
        "notional": 10_000_000, "fixed_rate": 0.035,
        "effective_date": "2026-05-01", "termination_date": "2028-05-01",
        "governing_law": "English Law",
        "contract_mode": mode,
    }


def _setup(cid: str, mode: str):
    os.environ["NOMOS_MODE"] = "demo"
    from api import api_create_contract, api_demo_auto_validate, api_set_demo_mode
    api_set_demo_mode(True)
    api_create_contract(_base_payload(cid, mode))
    api_demo_auto_validate(cid)


class TestBilateralModeAlias:

    def test_bilateral_mode_accepted(self):
        """contract_mode='bilateral' must be accepted and stored as peer_to_peer."""
        cid = "P1-7-ALIAS"
        _setup(cid, "bilateral")
        from api import _contract_meta
        assert _contract_meta[cid]["mode"] == "peer_to_peer", (
            f"P1-7: 'bilateral' mode was not normalised to 'peer_to_peer'. "
            f"Got: {_contract_meta[cid]['mode']}"
        )

    def test_bilateral_mode_not_immediately_active(self):
        """
        After creating with mode='bilateral', the contract must NOT be ACTIVE
        before either party signs.
        """
        cid = "P1-7-NO-ACTIVE"
        _setup(cid, "bilateral")
        from api import _engines
        from engine import ContractState
        eng = _engines[cid]
        assert eng.state == ContractState.PENDING_SIGNATURE, (
            f"P1-7: bilateral contract was ACTIVE before signing. "
            f"State: {eng.state.value}"
        )


class TestBilateralWorkflowStatus:

    def test_initial_status_is_pending_both(self):
        """Before either party signs, workflow status must be PENDING_BOTH_PARTIES."""
        cid = "P1-7-STATUS-A"
        _setup(cid, "bilateral")
        from api import api_contract_detail
        detail = api_contract_detail(cid, role="advisor")
        ws = detail.get("workflow_status")
        assert ws == "PENDING_BOTH_PARTIES", (
            f"P1-7: Expected PENDING_BOTH_PARTIES before any signature, got '{ws}'"
        )

    def test_status_pending_party_a_after_b_signs(self):
        """After B signs, status must be PENDING_PARTY_A."""
        cid = "P1-7-STATUS-B"
        _setup(cid, "bilateral")
        from api import api_sign_contract, api_contract_detail
        api_sign_contract(cid, "Beta Fund Ltd — Trading Desk", "B")
        detail = api_contract_detail(cid, role="advisor")
        ws = detail.get("workflow_status")
        assert ws == "PENDING_PARTY_A", (
            f"P1-7: Expected PENDING_PARTY_A after B signed, got '{ws}'"
        )

    def test_status_pending_party_b_after_a_signs(self):
        """After A signs, status must be PENDING_PARTY_B."""
        cid = "P1-7-STATUS-C"
        _setup(cid, "bilateral")
        from api import api_sign_contract, api_contract_detail
        api_sign_contract(cid, "Alpha Corp — CFO", "A")
        detail = api_contract_detail(cid, role="advisor")
        ws = detail.get("workflow_status")
        assert ws == "PENDING_PARTY_B", (
            f"P1-7: Expected PENDING_PARTY_B after A signed, got '{ws}'"
        )


class TestBilateralActivation:

    def test_single_party_b_sign_does_not_activate_bilateral(self):
        """
        In bilateral mode, signing by Party B alone must NOT activate the contract.
        This was the pre-fix behaviour: the contract became ACTIVE immediately.
        """
        cid = "P1-7-SINGLE-B"
        _setup(cid, "bilateral")
        from api import api_sign_contract, _engines
        from engine import ContractState
        r = api_sign_contract(cid, "Beta Fund Ltd — Trading Desk", "B")
        assert r.get("activated") is False, (
            "P1-7 regressed: single-party B signature activated the bilateral contract."
        )
        assert _engines[cid].state == ContractState.PENDING_SIGNATURE, (
            f"P1-7 regressed: contract state is {_engines[cid].state.value} "
            "after single signature in bilateral mode."
        )

    def test_bilateral_activates_after_both_sign(self):
        """
        In bilateral mode, after both A and B sign, the contract must become ACTIVE.
        """
        cid = "P1-7-BOTH-SIGN"
        _setup(cid, "bilateral")
        from api import api_sign_contract, _engines
        from engine import ContractState

        r_b = api_sign_contract(cid, "Beta Fund Ltd — Trading Desk", "B")
        assert r_b.get("activated") is False

        r_a = api_sign_contract(cid, "Alpha Corp — CFO", "A")
        assert r_a.get("activated") is True, (
            "P1-7: contract not activated after both parties signed in bilateral mode."
        )
        assert _engines[cid].state == ContractState.ACTIVE, (
            f"P1-7: expected ACTIVE, got {_engines[cid].state.value}"
        )

    def test_advisor_managed_mode_still_single_signature(self):
        """
        Regression guard: advisor_managed mode must still activate on a single
        Party B signature (existing behaviour must not regress).
        """
        cid = "P1-7-AM-GUARD"
        _setup(cid, "advisor_managed")
        from api import api_sign_contract, _engines
        from engine import ContractState
        r = api_sign_contract(cid, "Beta Fund Ltd — Head of Trading", "B")
        assert r.get("activated") is True, (
            "P1-7 regression: advisor_managed mode no longer activates on Party B signature."
        )
        assert _engines[cid].state == ContractState.ACTIVE
