"""
Basic tests for the Nomos IRS execution engine.
Run with: python -m pytest tests/
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from decimal import Decimal
import pytest

from engine import (
    SwapParameters, PartyDetails, ScheduleElections, ContractInitiation,
    IRSExecutionEngine, ContractState,
)


def _make_engine(contract_id="TEST-001") -> IRSExecutionEngine:
    params = SwapParameters(
        contract_id=contract_id,
        party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer", jurisdiction_code="GB"),
        party_b=PartyDetails("Beta Fund Ltd", "Beta", "floating_payer", jurisdiction_code="FR"),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03200"),
        governing_law="English Law",
    )
    schedule = ScheduleElections(schedule_id="MA-ALPHA-BETA-001")
    initiation = ContractInitiation(initiated_by="advisor@test.com")
    return IRSExecutionEngine(params, schedule, initiation)


def test_engine_initialises():
    engine = _make_engine()
    assert engine is not None
    assert engine.state == ContractState.PENDING_SIGNATURE


def test_payment_schedule_generated():
    engine = _make_engine()
    assert len(engine.periods) > 0


def test_notional_stored():
    engine = _make_engine()
    assert engine.params.notional == Decimal("10000000")


def test_fixed_rate_stored():
    engine = _make_engine()
    assert engine.params.fixed_rate == Decimal("0.03200")


def test_governing_law():
    engine = _make_engine()
    assert engine.params.governing_law == "English Law"
