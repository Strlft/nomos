"""R-002 — Breach of Agreement; Repudiation (ISDA 2002 §5(a)(ii)).

Two limbs:

1. **Breach of Agreement** (``kind == "non_performance_other"``) — a failure
   to perform any obligation other than those covered by §4(a)(i),
   §4(a)(iii), §2(a)(i) or §2(e). Cured if remedied within **30 calendar
   days** after the notice. Otherwise: ``TRIGGER``.

2. **Repudiation** (``kind == "disaffirmation"``) — disaffirms, disclaims,
   repudiates, or rejects the Agreement or a Confirmation. Per
   ``ORACLE_RULES.md`` §R-002, **always emits POTENTIAL_TRIGGER** — this
   limb never auto-TRIGGERs because characterising disaffirmation as a
   §5(a)(ii) Event of Default is fact-sensitive and requires a human.

Contract shape (duck-typed)::

    contract.contract_id: str
    contract.breach_records: Iterable[BreachRecord]

    BreachRecord has:
        kind:                       "non_performance_other" | "disaffirmation"
        breach_id:                  str | None  (used in evidence keys)
        notice_sent_at:             date | None
        remedied_at:                date | None
        disaffirmation_notice_at:   date | None
        description:                str | None  (free-text, for evidence)

The 30-day grace is **calendar days**, not business days — the ISDA 2002
§5(a)(ii) text says "30 days after notice" without TARGET2 qualification.

If the contract has no ``breach_records`` attribute at all the rule
returns ``indeterminate=True`` (engine emits no event). An empty list is
a legitimate "no trigger".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from oracle.config import Severity
from oracle.rules.registry import register_rule
from oracle.types import Evidence, MarketState, Rule, RuleOutcome


_RULE_ID = "R-002"
_CLAUSE_REF = "ISDA 2002 §5(a)(ii)"
_VERSION = "1.0.0"
_DEFAULT_GRACE_CALENDAR_DAYS = 30
_KIND_NON_PERFORMANCE = "non_performance_other"
_KIND_DISAFFIRMATION = "disaffirmation"


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
    """Read the calendar-day override from the schedule, default 30."""

    if schedule is None:
        return _DEFAULT_GRACE_CALENDAR_DAYS
    override = getattr(schedule, "grace_period_breach_days", None)
    if override is None and isinstance(schedule, dict):
        override = schedule.get("grace_period_breach_days")
    if override is None:
        return _DEFAULT_GRACE_CALENDAR_DAYS
    if not isinstance(override, int) or isinstance(override, bool) or override < 0:
        raise ValueError(
            f"R-002: schedule.grace_period_breach_days must be a "
            f"non-negative int (calendar days); got {override!r}"
        )
    return override


def _identifier(breach: Any, fallback_index: int) -> str:
    """Best-effort identifier for evidence keys."""

    bid = getattr(breach, "breach_id", None)
    if bid is None and isinstance(breach, dict):
        bid = breach.get("breach_id")
    return str(bid) if bid is not None else f"#{fallback_index}"


def _evaluate_non_performance(
    breach: Any,
    grace_days: int,
    as_of: date,
) -> tuple[Severity, str, dict[str, str]]:
    """Limb 1: non_performance_other. Return ``None`` if cured."""

    notice_sent_at = getattr(breach, "notice_sent_at", None)
    remedied_at = getattr(breach, "remedied_at", None)

    if notice_sent_at is None:
        return Severity.WARNING, "breach_no_notice", {}

    if not isinstance(notice_sent_at, date):
        raise ValueError(
            f"R-002: notice_sent_at must be a date; got {notice_sent_at!r}"
        )

    grace_end = notice_sent_at + timedelta(days=grace_days)

    if isinstance(remedied_at, date) and remedied_at <= grace_end:
        # Cured within grace — no trigger; signal cure to the caller.
        return (
            Severity.WARNING,
            "breach_remedied_in_grace",
            {
                "remedied_at": remedied_at.isoformat(),
                "grace_end": grace_end.isoformat(),
                "_cured": "true",
            },
        )

    if as_of > grace_end and not isinstance(remedied_at, date):
        return (
            Severity.TRIGGER,
            "breach_grace_elapsed_unremedied",
            {
                "notice_sent_at": notice_sent_at.isoformat(),
                "grace_end": grace_end.isoformat(),
            },
        )

    return (
        Severity.WARNING,
        "breach_inside_grace",
        {
            "notice_sent_at": notice_sent_at.isoformat(),
            "grace_end": grace_end.isoformat(),
        },
    )


def _evaluate_disaffirmation(breach: Any) -> tuple[Severity, str, dict[str, str]] | None:
    """Limb 2: disaffirmation. Always POTENTIAL_TRIGGER, never TRIGGER."""

    disaffirmation_at = getattr(breach, "disaffirmation_notice_at", None)
    if not isinstance(disaffirmation_at, date):
        return None
    return (
        Severity.POTENTIAL_TRIGGER,
        "disaffirmation_notice_logged",
        {"disaffirmation_notice_at": disaffirmation_at.isoformat()},
    )


def _evaluate_breach(
    breach: Any,
    grace_days: int,
    as_of: date,
    fallback_index: int,
) -> tuple[Severity, Evidence] | None:
    """Assess one breach record. Return ``None`` if it doesn't qualify."""

    kind = getattr(breach, "kind", None)
    if kind is None and isinstance(breach, dict):
        kind = breach.get("kind")

    if kind == _KIND_NON_PERFORMANCE:
        result = _evaluate_non_performance(breach, grace_days, as_of)
    elif kind == _KIND_DISAFFIRMATION:
        result = _evaluate_disaffirmation(breach)
    else:
        return None

    if result is None:
        return None

    severity, reason, parts = result
    if parts.get("_cured") == "true":
        # Cured breach is informational only — no event. The caller drops it.
        return None

    bid = _identifier(breach, fallback_index)
    description = getattr(breach, "description", None)
    if description is None and isinstance(breach, dict):
        description = breach.get("description")

    items = [f"kind={kind}", f"reason={reason}"]
    for key, value in parts.items():
        if key.startswith("_"):
            continue
        items.append(f"{key}={value}")
    if description:
        items.append(f"description={description}")

    evidence = Evidence(
        kind="contract_field",
        key=f"breach_record[{bid}]",
        value=", ".join(items),
        source="irs_engine",
    )
    return severity, evidence


def _predicate(
    market: MarketState,
    contract: Any,
    as_of: date,
) -> RuleOutcome:
    """R-002 predicate. Pure function of its inputs (ARCH §A.2)."""

    if not hasattr(contract, "breach_records") and not (
        isinstance(contract, dict) and "breach_records" in contract
    ):
        return RuleOutcome(
            fired=False,
            indeterminate=True,
            indeterminate_reason="R-002: contract has no breach_records field",
        )

    breaches = getattr(contract, "breach_records", None)
    if breaches is None and isinstance(contract, dict):
        breaches = contract.get("breach_records")
    if breaches is None:
        breaches = ()

    schedule = getattr(contract, "schedule", None)
    if schedule is None and isinstance(contract, dict):
        schedule = contract.get("schedule")
    grace_days = _grace_days(schedule)

    highest: Severity | None = None
    evidence: list[Evidence] = []

    for index, breach in enumerate(breaches):
        assessment = _evaluate_breach(breach, grace_days, as_of, index)
        if assessment is None:
            continue
        severity, breach_evidence = assessment
        highest = _escalate(highest, severity)
        evidence.append(breach_evidence)

    if highest is None:
        return RuleOutcome(fired=False)

    evidence.append(
        Evidence(
            kind="contract_field",
            key="schedule.grace_period_breach_days",
            value=f"{grace_days} calendar day(s)",
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
        required_contract_fields=frozenset({"breach_records", "schedule"}),
        grace_period=timedelta(days=_DEFAULT_GRACE_CALENDAR_DAYS),
        version=_VERSION,
        description=(
            "Breach of Agreement (30-day calendar grace after notice) and "
            "Repudiation/disaffirmation (always POTENTIAL_TRIGGER, never "
            "auto-TRIGGER) under ISDA 2002 §5(a)(ii)."
        ),
    )
)
