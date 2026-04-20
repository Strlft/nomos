"""
Regression test for P1-3: no false §5(a)(ii) escalations from pre-signing
§4(a)/(b) obligations.

Root cause: schedule_standard_obligations() created an "Authorisations
Confirmation {year}" obligation with due_date = effective_date (i.e. it was a
pre-signing obligation).  schedule_from_part3() similarly used "upon execution"
obligations.  Neither was marked DELIVERED at initialisation, so they became
OVERDUE on the first calculation period and wrongly triggered §5(a)(ii)
escalation recommendations.

Fix: obligations with due_date == effective_date (Authorisations Confirmation)
and all "upon execution" Part-3 obligations are auto-delivered at initialisation,
because reaching ACTIVE state implies all pre-signing checks already passed.
"""

import sys, os
import pytest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from engine import (
    SwapParameters, PartyDetails, ScheduleElections, ContractInitiation,
    IRSExecutionEngine,
)


def _make_engine(effective: date = date(2026, 5, 1),
                 termination: date = date(2028, 5, 1)) -> IRSExecutionEngine:
    params = SwapParameters(
        contract_id="P1-3-RTEST",
        party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer", "FR"),
        party_b=PartyDetails("Beta Fund Ltd",   "Beta",  "floating_payer", "GB"),
        notional=Decimal("15000000"),
        fixed_rate=Decimal("0.035"),
        effective_date=effective,
        termination_date=termination,
        governing_law="English Law",
    )
    schedule = ScheduleElections(schedule_id="MA-P1-3-RTEST")
    initiation = ContractInitiation(initiated_by="test@nomos")
    eng = IRSExecutionEngine(params, schedule, initiation)
    eng.initialise()
    return eng


class TestP1_3NoFalseEscalations:

    def test_no_escalation_immediately_after_initialise(self):
        """
        Immediately after initialise(), check_escalation_to_eod(effective_date)
        must return zero escalations.

        Before fix: 'Authorisations Confirmation 2026' was due on effective_date
        and was NOT marked DELIVERED, so it appeared OVERDUE on the effective date
        itself (after the 5-LBD grace) and triggered a false §5(a)(ii) escalation.
        """
        eng = _make_engine()
        today = date(2026, 5, 1)
        eng.compliance.check_obligations(today)
        escalations = eng.compliance.check_escalation_to_eod(today)

        assert escalations == [], (
            f"P1-3 regressed: false §5(a)(ii) escalations on effective_date.\n"
            f"Escalations: {escalations}"
        )

    def test_no_escalation_after_first_period(self):
        """
        After the first quarter (end date ≈ 2026-08-01), check_escalation_to_eod
        must return zero escalations when no real breach has been declared.

        Before fix: 'Authorisations Confirmation 2026' (due 2026-05-01) was
        92 days overdue by period 1 end and produced a false HIGH-severity
        §5(a)(ii) escalation.
        """
        eng = _make_engine()
        today = date(2026, 8, 3)   # MODFOL-adjusted P1 payment date
        eng.compliance.check_obligations(today)
        escalations = eng.compliance.check_escalation_to_eod(today)

        presigning_names = [e["obligation"] for e in escalations
                            if "Authorisations" in e["obligation"]
                            or "upon execution" in e.get("obligation", "").lower()]

        assert presigning_names == [], (
            f"P1-3 regressed: pre-signing obligations still generating escalations.\n"
            f"False escalations: {presigning_names}\n"
            f"All escalations: {escalations}"
        )

    def test_authorisations_confirmation_auto_delivered(self):
        """
        After initialise(), the 'Authorisations Confirmation {year}' obligation
        for the effective year must have status DELIVERED.
        """
        eng = _make_engine()
        ObligationStatus = eng.compliance.ObligationStatus

        auth_obs = [
            o for o in eng.compliance.obligations
            if "Authorisations Confirmation" in o.name
            and o.due_date == date(2026, 5, 1)
        ]
        assert auth_obs, (
            "No 'Authorisations Confirmation 2026' obligation found — "
            "schedule_standard_obligations() may have changed."
        )
        for ob in auth_obs:
            assert ob.status == ObligationStatus.DELIVERED, (
                f"P1-3 regressed: {ob.name} ({ob.party}) has status "
                f"'{ob.status}' instead of DELIVERED. "
                f"Pre-signing auto-delivery not working."
            )

    def test_post_signing_obligations_still_tracked(self):
        """
        Post-signing obligations (e.g. Annual Financial Statements) must NOT
        be auto-delivered — they remain active obligations to be fulfilled later.
        """
        eng = _make_engine()
        ObligationStatus = eng.compliance.ObligationStatus

        fs_obs = [
            o for o in eng.compliance.obligations
            if "Financial Statements" in o.name
        ]
        assert fs_obs, (
            "No 'Annual Financial Statements' obligations found — "
            "schedule_standard_obligations() may have changed."
        )
        for ob in fs_obs:
            assert ob.status != ObligationStatus.DELIVERED, (
                f"Post-signing obligation '{ob.name}' ({ob.party}) was "
                f"incorrectly auto-delivered. Only pre-signing obligations "
                f"should be auto-delivered."
            )

    def test_no_escalation_with_part3_upon_execution_docs(self):
        """
        When Part 3 'upon execution' documents are present, those obligations
        must be auto-delivered and must not generate §5(a)(ii) escalations.
        """
        from engine import ScheduleElections

        # Build a schedule with Part 3 'upon execution' documents
        schedule = ScheduleElections(schedule_id="MA-P1-3-PT3")
        schedule.documents_to_deliver = [
            {"party": "PARTY_A", "document": "Legal Capacity Opinion",
             "deadline": "Upon Execution", "s3d": True},
            {"party": "PARTY_B", "document": "Authorising Resolutions",
             "deadline": "Upon Execution", "s3d": False},
        ]

        params = SwapParameters(
            contract_id="P1-3-PT3",
            party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer", "FR"),
            party_b=PartyDetails("Beta Fund Ltd",   "Beta",  "floating_payer", "GB"),
            notional=Decimal("10000000"),
            fixed_rate=Decimal("0.04"),
            effective_date=date(2026, 5, 1),
            termination_date=date(2028, 5, 1),
            governing_law="English Law",
        )
        initiation = ContractInitiation(initiated_by="test@nomos")
        eng = IRSExecutionEngine(params, schedule, initiation)
        eng.initialise()

        ObligationStatus = eng.compliance.ObligationStatus

        upon_exec_obs = [
            o for o in eng.compliance.obligations
            if o.due_date == date(2026, 5, 1)
        ]
        for ob in upon_exec_obs:
            assert ob.status == ObligationStatus.DELIVERED, (
                f"P1-3: Part-3 'upon execution' obligation '{ob.name}' "
                f"has status '{ob.status}' — expected DELIVERED."
            )

        # No false escalations 90 days later
        today = date(2026, 8, 3)
        eng.compliance.check_obligations(today)
        escalations = eng.compliance.check_escalation_to_eod(today)
        assert escalations == [], (
            f"P1-3: Part-3 'upon execution' docs still escalating: {escalations}"
        )
