"""R-005 — Tax Event (ISDA 2002 §5(b)(ii), §5(b)(iii)).

V1 is **flag-based** (per ``ORACLE_RULES.md`` §R-005). Full Tax Event
detection requires tax-law-change monitoring across multiple
jurisdictions and is deferred to V2+.

Severity ladder (no escalation beyond POTENTIAL_TRIGGER — Tax Events
require human review under §5(b)(ii)/(iii) regardless of the flag)::

    flag.effective_date > as_of                  → ignored (not yet effective)
    flag.kind == "withholding_imposed"           → POTENTIAL_TRIGGER
    flag.kind == "indemnifiable_tax_required"    → POTENTIAL_TRIGGER
    flag.kind == "withholding_removed"           → WARNING

Contract shape (duck-typed)::

    contract.contract_id: str
    contract.tax_event_flags: Iterable[TaxEventFlag]

    TaxEventFlag has:
        kind:              str   ("withholding_imposed" |
                                  "indemnifiable_tax_required" |
                                  "withholding_removed")
        jurisdiction:      str   (e.g. "GB", "FR-CG", "EU")
        effective_date:    date
        description:       str | None
        source_reference:  str | None
        flag_id:           str | None

If the contract has no ``tax_event_flags`` attribute at all the rule
returns ``indeterminate=True``. An empty list is "no trigger".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from oracle.config import Severity
from oracle.rules.registry import register_rule
from oracle.types import Evidence, MarketState, Rule, RuleOutcome


_RULE_ID = "R-005"
_CLAUSE_REF = "ISDA 2002 §5(b)(ii), §5(b)(iii)"
_VERSION = "1.0.0"

_KIND_WITHHOLDING_IMPOSED = "withholding_imposed"
_KIND_INDEMNIFIABLE_TAX = "indemnifiable_tax_required"
_KIND_WITHHOLDING_REMOVED = "withholding_removed"

_KIND_SEVERITIES: dict[str, Severity] = {
    _KIND_WITHHOLDING_IMPOSED: Severity.POTENTIAL_TRIGGER,
    _KIND_INDEMNIFIABLE_TAX: Severity.POTENTIAL_TRIGGER,
    _KIND_WITHHOLDING_REMOVED: Severity.WARNING,
}

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.WARNING: 0,
    Severity.POTENTIAL_TRIGGER: 1,
}


def _escalate(current: Severity | None, candidate: Severity) -> Severity:
    if current is None:
        return candidate
    return candidate if _SEVERITY_RANK[candidate] > _SEVERITY_RANK[current] else current


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name, default)
    return default if value is None else value


def _identifier(flag: Any, fallback_index: int) -> str:
    fid = _get_field(flag, "flag_id")
    return str(fid) if fid else f"#{fallback_index}"


def _evaluate_flag(
    flag: Any,
    as_of: date,
    fallback_index: int,
) -> tuple[Severity, Evidence] | None:
    """Assess one flag. Return ``None`` if it doesn't qualify."""

    effective_date = _get_field(flag, "effective_date")
    if not isinstance(effective_date, date):
        raise ValueError(
            f"R-005: flag effective_date must be a date; got {effective_date!r}"
        )

    if effective_date > as_of:
        return None

    kind = _get_field(flag, "kind")
    severity = _KIND_SEVERITIES.get(str(kind)) if kind is not None else None
    if severity is None:
        # Unknown kind — silently ignore rather than fabricating a severity.
        return None

    flag_id = _identifier(flag, fallback_index)
    jurisdiction = _get_field(flag, "jurisdiction") or "unspecified"
    description = _get_field(flag, "description")

    parts = [
        f"kind={kind}",
        f"jurisdiction={jurisdiction}",
        f"effective_date={effective_date.isoformat()}",
    ]
    if description:
        parts.append(f"description={description}")

    evidence = Evidence(
        kind="contract_field",
        key=f"tax_event_flags[{flag_id}]",
        value=", ".join(parts),
        source=str(_get_field(flag, "source_reference") or "manual"),
    )
    return severity, evidence


def _predicate(
    market: MarketState,
    contract: Any,
    as_of: date,
) -> RuleOutcome:
    """R-005 predicate. Pure function of its inputs (ARCH §A.2)."""

    if not hasattr(contract, "tax_event_flags") and not (
        isinstance(contract, dict) and "tax_event_flags" in contract
    ):
        return RuleOutcome(
            fired=False,
            indeterminate=True,
            indeterminate_reason="R-005: contract has no tax_event_flags field",
        )

    flags = _get_field(contract, "tax_event_flags", ())

    highest: Severity | None = None
    evidence: list[Evidence] = []

    for index, flag in enumerate(flags):
        assessment = _evaluate_flag(flag, as_of, index)
        if assessment is None:
            continue
        severity, flag_evidence = assessment
        highest = _escalate(highest, severity)
        evidence.append(flag_evidence)

    if highest is None:
        return RuleOutcome(fired=False)

    return RuleOutcome(
        fired=True,
        severity=highest,
        evidence=tuple(evidence),
    )


rule: Rule = register_rule(
    Rule(
        rule_id=_RULE_ID,
        clause_ref=_CLAUSE_REF,
        severity=Severity.POTENTIAL_TRIGGER,
        predicate=_predicate,
        required_metrics=frozenset(),
        required_contract_fields=frozenset({"tax_event_flags"}),
        grace_period=timedelta(0),
        version=_VERSION,
        description=(
            "Tax Event under ISDA 2002 §5(b)(ii)/(iii). V1 is flag-based: "
            "fires on human-populated tax_event_flags whose effective_date "
            "has elapsed. Withholding imposition / indemnifiable tax → "
            "POTENTIAL_TRIGGER; withholding removal → WARNING (audit "
            "record). Never auto-TRIGGERs — Tax Event characterisation "
            "requires human legal review."
        ),
    )
)
