"""Rule engine orchestrator.

The engine is a **pure** function of its inputs (ARCH §A.2): given the
same ``(MarketState, contract, as_of)`` it produces the same list of
``TriggerEvent``. It has no persistent state, no IO, and does not call
``datetime.now()`` inside any predicate path.

For each registered rule, the engine:

1. Checks that every metric the rule declares under
   :attr:`Rule.required_metrics` is present in ``market.latest``.
   Missing metrics ⇒ the rule is **indeterminate** and the engine
   emits **no** event (per ARCH §A.3 — rules never silently skip, but
   the engine never synthesizes a TriggerEvent it cannot back with
   data).
2. Invokes the rule's predicate. Any exception is caught and the rule
   is isolated — one crashing rule must not prevent other rules from
   evaluating (ARCH §8).
3. If the ``RuleOutcome`` fired and is not itself indeterminate, the
   engine builds a :class:`TriggerEvent` and appends it.

The ``attestation_ref`` on the TriggerEvent is picked from
``market.attestation_refs``: preferably one of the rule's required
metrics, otherwise any attestation in the state, falling back to the
nil UUID when the rule has no market inputs and the state carries no
refs (R-001 is the canonical example).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from oracle.config import RULES_VERSION, Metric
from oracle.logging_config import get_logger
from oracle.types import MarketState, Rule, TriggerEvent


_log = get_logger("rules.engine")


# Sentinel used when a rule has no required metrics and the MarketState
# carries no refs. The TriggerEvent still needs a valid UUID.
_NIL_ATTESTATION: UUID = UUID(int=0)


class RuleEngine:
    """Evaluate a fixed set of rules against ``(MarketState, contract, as_of)``."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        rules_tuple: tuple[Rule, ...] = tuple(rules)
        seen: set[str] = set()
        for rule in rules_tuple:
            if rule.rule_id in seen:
                raise ValueError(
                    f"RuleEngine: duplicate rule_id {rule.rule_id!r}"
                )
            seen.add(rule.rule_id)
        self._rules: tuple[Rule, ...] = rules_tuple

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    def evaluate(
        self,
        market: MarketState,
        contract: Any,
        as_of: date,
    ) -> list[TriggerEvent]:
        """Run every rule and return the list of fired TriggerEvents."""

        events: list[TriggerEvent] = []
        evaluated_at = datetime.now(timezone.utc)
        contract_id = _contract_id(contract)

        for rule in self._rules:
            available_metrics: frozenset[Metric] = frozenset(market.latest.keys())
            missing = rule.required_metrics - available_metrics
            if missing:
                _log.info(
                    "rule_indeterminate",
                    action="evaluate_rule",
                    outcome="indeterminate",
                    rule_id=rule.rule_id,
                    reason="missing_metrics",
                    missing_metrics=sorted(m.value for m in missing),
                )
                continue

            try:
                outcome = rule.predicate(market, contract, as_of)
            except Exception as exc:  # noqa: BLE001 — isolation is the point
                _log.warning(
                    "rule_predicate_raised",
                    action="evaluate_rule",
                    outcome="failure",
                    rule_id=rule.rule_id,
                    exception_type=type(exc).__name__,
                    exception_message=str(exc),
                )
                continue

            if outcome.indeterminate:
                _log.info(
                    "rule_indeterminate",
                    action="evaluate_rule",
                    outcome="indeterminate",
                    rule_id=rule.rule_id,
                    reason=outcome.indeterminate_reason,
                )
                continue
            if not outcome.fired:
                continue
            if outcome.severity is None:
                _log.error(
                    "rule_severity_missing",
                    action="evaluate_rule",
                    outcome="failure",
                    rule_id=rule.rule_id,
                    reason="fired_true_but_severity_none",
                )
                continue

            events.append(
                TriggerEvent(
                    event_id=uuid4(),
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    clause_ref=rule.clause_ref,
                    severity=outcome.severity,
                    contract_id=contract_id,
                    evaluated_at=evaluated_at,
                    as_of=as_of,
                    attestation_ref=_pick_attestation_ref(rule, market),
                    evidence=outcome.evidence,
                    rules_version=RULES_VERSION,
                )
            )
        return events


def _contract_id(contract: Any) -> str:
    """Best-effort contract id, tolerant of dict-shaped contracts."""

    if contract is None:
        return ""
    cid = getattr(contract, "contract_id", None)
    if cid is None and isinstance(contract, dict):
        cid = contract.get("contract_id")
    return str(cid) if cid is not None else ""


def _pick_attestation_ref(rule: Rule, market: MarketState) -> UUID:
    for metric in sorted(rule.required_metrics, key=lambda m: m.value):
        ref = market.attestation_refs.get(metric)
        if ref is not None:
            return ref
    for metric in sorted(market.attestation_refs.keys(), key=lambda m: m.value):
        return market.attestation_refs[metric]
    return _NIL_ATTESTATION
