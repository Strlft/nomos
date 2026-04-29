"""R-001 — Failure to Pay or Deliver (ISDA 2002 §5(a)(i)).

Legal pseudocode reproduced from ``docs/oracle/ORACLE_RULES.md``::

    GIVEN scheduled payment P, as_of > P.due_date, P.amount > 0
    IF P.status == PAID:                          → no trigger
    IF P.status == PENDING AND no notice:         → WARNING
    IF P.status == PENDING AND notice.sent_at:
        grace_end = add_business_days(notice.sent_at, n, TARGET2)
        IF as_of <= grace_end:                    → WARNING
        ELSE:                                     → TRIGGER

``n`` defaults to **1 TARGET2 business day**. The per-contract override
lives at ``schedule.grace_period_failure_to_pay`` and must be a
non-negative int (business days).

Contract shape (duck-typed — :mod:`irs_engine_v2` owns the authoritative
``ContractState`` and the Oracle must not import it)::

    contract.contract_id: str
    contract.scheduled_payments: Iterable[Payment]
    contract.notices:            Iterable[Notice]
    contract.schedule:           Schedule | None

    Payment has:  payment_id, amount (Decimal), due_date (date), status (str)
    Notice  has:  kind (str), payment_id, sent_at (date)
    Schedule has: grace_period_failure_to_pay: int | None

If the contract has no ``scheduled_payments`` attribute at all the rule
returns ``indeterminate=True`` — the engine then emits **no** event
(ARCH §A.3). This is distinct from having an empty list of payments,
which is a legitimate "no trigger".
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from oracle.config import Severity
from oracle.rules.calendar import add_business_days
from oracle.rules.registry import register_rule
from oracle.types import Evidence, MarketState, Rule, RuleOutcome


_RULE_ID = "R-001"
_CLAUSE_REF = "ISDA 2002 §5(a)(i)"
_VERSION = "1.0.0"
_DEFAULT_GRACE_BUSINESS_DAYS = 1
_NOTICE_KIND = "failure_to_pay"
_STATUS_PAID = "PAID"


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.WARNING: 0,
    Severity.POTENTIAL_TRIGGER: 1,
    Severity.TRIGGER: 2,
}


def _escalate(current: Severity | None, candidate: Severity) -> Severity:
    """Return whichever of ``current`` and ``candidate`` is more severe."""

    if current is None:
        return candidate
    return candidate if _SEVERITY_RANK[candidate] > _SEVERITY_RANK[current] else current


def _grace_days(schedule: Any) -> int:
    """Read ``schedule.grace_period_failure_to_pay`` with the ISDA default."""

    if schedule is None:
        return _DEFAULT_GRACE_BUSINESS_DAYS
    override = getattr(schedule, "grace_period_failure_to_pay", None)
    if override is None and isinstance(schedule, dict):
        override = schedule.get("grace_period_failure_to_pay")
    if override is None:
        return _DEFAULT_GRACE_BUSINESS_DAYS
    if not isinstance(override, int) or isinstance(override, bool) or override < 0:
        raise ValueError(
            f"R-001: schedule.grace_period_failure_to_pay must be a "
            f"non-negative int (business days); got {override!r}"
        )
    return override


def _earliest_notice_for(notices: Any, payment_id: Any) -> Any | None:
    """Return the earliest ``failure_to_pay`` notice for ``payment_id``."""

    if notices is None:
        return None
    best: Any | None = None
    for notice in notices:
        if getattr(notice, "kind", None) != _NOTICE_KIND:
            continue
        if getattr(notice, "payment_id", None) != payment_id:
            continue
        sent_at = getattr(notice, "sent_at", None)
        if not isinstance(sent_at, date):
            continue
        if best is None or sent_at < best.sent_at:
            best = notice
    return best


def _evaluate_payment(
    payment: Any,
    notices: Any,
    grace_days: int,
    as_of: date,
) -> tuple[Severity, Evidence] | None:
    """Assess one payment. Return ``None`` if it doesn't qualify."""

    amount = getattr(payment, "amount", None)
    due_date = getattr(payment, "due_date", None)
    status = getattr(payment, "status", None)
    payment_id = getattr(payment, "payment_id", None)

    if not isinstance(amount, Decimal) or amount <= 0:
        return None
    if not isinstance(due_date, date) or as_of <= due_date:
        return None
    if status == _STATUS_PAID:
        return None

    notice = _earliest_notice_for(notices, payment_id)
    if notice is None:
        severity = Severity.WARNING
        detail = "overdue_no_notice"
        grace_end: date | None = None
    else:
        grace_end = add_business_days(notice.sent_at, grace_days)
        if as_of <= grace_end:
            severity = Severity.WARNING
            detail = "overdue_inside_grace"
        else:
            severity = Severity.TRIGGER
            detail = "overdue_grace_elapsed"

    parts = [
        f"amount={amount}",
        f"due_date={due_date.isoformat()}",
        f"status={status}",
        f"reason={detail}",
    ]
    if notice is not None:
        parts.append(f"notice_sent_at={notice.sent_at.isoformat()}")
    if grace_end is not None:
        parts.append(f"grace_end={grace_end.isoformat()}")

    evidence = Evidence(
        kind="contract_field",
        key=f"payment[{payment_id}]",
        value=", ".join(parts),
        source="irs_engine",
    )
    return severity, evidence


def _predicate(
    market: MarketState,
    contract: Any,
    as_of: date,
) -> RuleOutcome:
    """R-001 predicate. Pure function of its inputs (ARCH §A.2)."""

    if not hasattr(contract, "scheduled_payments") and not (
        isinstance(contract, dict) and "scheduled_payments" in contract
    ):
        return RuleOutcome(
            fired=False,
            indeterminate=True,
            indeterminate_reason=(
                "R-001: contract has no scheduled_payments field"
            ),
        )

    payments = getattr(contract, "scheduled_payments", None)
    if payments is None and isinstance(contract, dict):
        payments = contract.get("scheduled_payments")
    if payments is None:
        payments = ()

    notices = getattr(contract, "notices", None)
    if notices is None and isinstance(contract, dict):
        notices = contract.get("notices")
    if notices is None:
        notices = ()

    schedule = getattr(contract, "schedule", None)
    if schedule is None and isinstance(contract, dict):
        schedule = contract.get("schedule")

    grace_days = _grace_days(schedule)

    highest: Severity | None = None
    evidence: list[Evidence] = []

    for payment in payments:
        assessment = _evaluate_payment(payment, notices, grace_days, as_of)
        if assessment is None:
            continue
        severity, payment_evidence = assessment
        highest = _escalate(highest, severity)
        evidence.append(payment_evidence)

    if highest is None:
        return RuleOutcome(fired=False)

    evidence.append(
        Evidence(
            kind="contract_field",
            key="schedule.grace_period_failure_to_pay",
            value=f"{grace_days} business day(s)",
            source="irs_engine",
        )
    )

    return RuleOutcome(
        fired=True,
        severity=highest,
        evidence=tuple(evidence),
    )


rule: Rule = register_rule(
    Rule(
        rule_id=_RULE_ID,
        clause_ref=_CLAUSE_REF,
        severity=Severity.TRIGGER,
        predicate=_predicate,
        required_metrics=frozenset(),
        required_contract_fields=frozenset(
            {"scheduled_payments", "notices", "schedule"}
        ),
        grace_period=timedelta(days=_DEFAULT_GRACE_BUSINESS_DAYS),
        version=_VERSION,
        description=(
            "Failure to make a payment or delivery when due, unremedied one "
            "Local Business Day after notice (ISDA 2002 §5(a)(i))."
        ),
    )
)
