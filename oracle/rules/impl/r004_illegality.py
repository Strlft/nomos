"""R-004 — Illegality / Rate Unavailability (ISDA 2002 §5(b)(i)).

V1 implements only the **rate-unavailability** sub-case (per
``ORACLE_RULES.md`` §R-004). Full Illegality requires regulatory
monitoring and is deferred to V2+.

The rule is unusual in that it consults ``MarketState.missing`` rather
than ``MarketState.latest`` — its job is to surface the *absence* of a
required rate, not to reason about its value. ``required_metrics`` is
therefore left empty so the engine does not skip R-004 the moment its
reference rate goes missing.

Severity ladder::

    rate present                          → no trigger
    rate missing,  days_consecutive < 5   → WARNING (transient)
    rate missing,  days_consecutive ≥ 5   → POTENTIAL_TRIGGER (sustained)

The 5-business-day threshold is **an Oracle heuristic, not an ISDA legal
period**. ISDA 2002 §5(b)(i) does not specify a window for rate
unavailability; the Waiting Period in §5(d) applies to Termination
Events generally but is independent of this Oracle's escalation
heuristic. The threshold is documented in :data:`SUSTAINED_THRESHOLD_DAYS`
and surfaced in :attr:`Rule.description` so reviewers see the caveat
without having to read the rule body.

Contract shape (duck-typed)::

    contract.floating_leg.reference_rate: str
        e.g. ``"EURIBOR 3M"``, ``"ESTR"``. Normalised to the canonical
        :class:`Metric` value before consulting MarketState.

    contract.floating_index: str  (fallback, supports SwapParameters)

If neither attribute exists → ``indeterminate=True`` with reason. If the
attribute exists but the string can't be mapped to a tracked metric (the
"Contract uses untracked rate" matrix row) → ``indeterminate=True``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from oracle.config import Metric, Severity
from oracle.rules.registry import register_rule
from oracle.types import Evidence, MarketState, Rule, RuleOutcome


_RULE_ID = "R-004"
_CLAUSE_REF = "ISDA 2002 §5(b)(i)"
_VERSION = "1.0.0"

#: Oracle heuristic — see module docstring. Not an ISDA legal period.
SUSTAINED_THRESHOLD_DAYS: int = 5


def _normalise_rate(raw: Any) -> Metric | None:
    """Map a free-text rate identifier to a canonical :class:`Metric`."""

    if not isinstance(raw, str):
        return None
    canonical = raw.strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return Metric(canonical)
    except ValueError:
        return None


def _read_reference_rate(contract: Any) -> tuple[bool, Any]:
    """Return ``(present, raw_value)`` for the contract's reference rate.

    ``present=False`` means *no* such field exists at all (indeterminate).
    ``present=True`` with a non-string value means the field exists but is
    malformed; the caller will turn that into indeterminate.
    """

    floating_leg = getattr(contract, "floating_leg", None)
    if floating_leg is None and isinstance(contract, dict):
        floating_leg = contract.get("floating_leg")

    if floating_leg is not None:
        rate = getattr(floating_leg, "reference_rate", None)
        if rate is None and isinstance(floating_leg, dict):
            rate = floating_leg.get("reference_rate")
        if rate is not None:
            return True, rate

    rate = getattr(contract, "floating_index", None)
    if rate is None and isinstance(contract, dict):
        rate = contract.get("floating_index")
    if rate is not None:
        return True, rate

    return False, None


def _predicate(
    market: MarketState,
    contract: Any,
    as_of: date,
) -> RuleOutcome:
    """R-004 predicate. Pure function of its inputs (ARCH §A.2)."""

    present, raw_rate = _read_reference_rate(contract)
    if not present:
        return RuleOutcome(
            fired=False,
            indeterminate=True,
            indeterminate_reason=(
                "R-004: contract exposes neither floating_leg.reference_rate "
                "nor floating_index"
            ),
        )

    metric = _normalise_rate(raw_rate)
    if metric is None:
        return RuleOutcome(
            fired=False,
            indeterminate=True,
            indeterminate_reason=(
                f"R-004: reference rate {raw_rate!r} does not map to a "
                f"tracked Metric — Oracle cannot assess unavailability"
            ),
        )

    if metric not in market.missing:
        return RuleOutcome(fired=False)

    days = int(market.missing_consecutive_days.get(metric, 0))
    if days >= SUSTAINED_THRESHOLD_DAYS:
        severity = Severity.POTENTIAL_TRIGGER
        regime = "sustained_unavailability"
    else:
        severity = Severity.WARNING
        regime = "transient_unavailability"

    evidence = (
        Evidence(
            kind="market_datum",
            key=f"market_state.missing[{metric.value}]",
            value=(
                f"missing_consecutive_days={days}, "
                f"threshold={SUSTAINED_THRESHOLD_DAYS}, "
                f"regime={regime}"
            ),
            source="oracle",
        ),
        Evidence(
            kind="contract_field",
            key="floating_leg.reference_rate",
            value=f"raw={raw_rate!r}, normalised={metric.value}",
            source="irs_engine",
        ),
    )

    return RuleOutcome(fired=True, severity=severity, evidence=evidence)


rule: Rule = register_rule(
    Rule(
        rule_id=_RULE_ID,
        clause_ref=_CLAUSE_REF,
        severity=Severity.POTENTIAL_TRIGGER,
        predicate=_predicate,
        # Intentionally empty — R-004 reads `market.missing`, not
        # `market.latest`. Declaring the reference rate as required would
        # make the engine skip R-004 the moment the rate disappeared.
        required_metrics=frozenset(),
        required_contract_fields=frozenset({"floating_leg"}),
        grace_period=timedelta(0),
        version=_VERSION,
        description=(
            "Rate unavailability sub-case of ISDA 2002 §5(b)(i) Illegality. "
            "Surfaces the factual condition only — Illegality itself is a "
            "legal judgment. WARNING for transient unavailability "
            "(1-4 business days); POTENTIAL_TRIGGER for sustained "
            "(≥5 business days). Note: the 5-business-day threshold is an "
            "Oracle heuristic, NOT an ISDA legal period — §5(b)(i) does not "
            "specify a window for rate unavailability. Never auto-TRIGGERs."
        ),
    )
)
