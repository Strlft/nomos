"""R-003 — Cross Default (ISDA 2002 §5(a)(vi)).

V1 limitations (per ``ORACLE_RULES.md`` §R-003):

* External credit-event data is **not** auto-detected. The rule reads
  manually recorded entries in ``contract.external_defaults[party]``.
* Severity is bounded at ``POTENTIAL_TRIGGER`` — Cross Default
  characterisation is fact-sensitive and the Oracle never auto-TRIGGERs
  this clause in V1.
* All qualifying defaults must be denominated in the Threshold currency.
  Currency conversion is **not** attempted; mixed currencies raise
  :class:`DataInconsistentError` (the engine isolates the exception and
  emits no event).

Contract shape (duck-typed)::

    contract.contract_id: str
    contract.external_defaults: Mapping[str, Iterable[ExternalDefault]]
    contract.schedule:
        cross_default_applies:                  Mapping[str, bool]
        cross_default_threshold_amount:         Mapping[str, Decimal]
        cross_default_threshold_currency:       Mapping[str, str]
        specified_indebtedness_definition:      Iterable[str]

    ExternalDefault has:
        default_id        str | None
        instrument_type   str          (matched against the SI definition)
        status            str          ("accelerated" | "payment_default" |
                                        "remediated")
        amount_due        Decimal
        currency          str
        reported_at       date
        source_reference  str | None

The rule emits **one** outcome per evaluation. When both parties qualify
the severity is the maximum across parties and the evidence enumerates
each contributing party + default.

If the contract has no ``external_defaults`` attribute at all the rule
returns ``indeterminate=True`` (engine emits no event). An empty mapping
is a legitimate "no trigger".
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from oracle.config import Severity
from oracle.errors import DataInconsistentError
from oracle.rules.registry import register_rule
from oracle.types import Evidence, MarketState, Rule, RuleOutcome


_RULE_ID = "R-003"
_CLAUSE_REF = "ISDA 2002 §5(a)(vi)"
_VERSION = "1.0.0"

_QUALIFYING_STATUSES: frozenset[str] = frozenset({"accelerated", "payment_default"})

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.WARNING: 0,
    Severity.POTENTIAL_TRIGGER: 1,
}


def _escalate(current: Severity | None, candidate: Severity) -> Severity:
    if current is None:
        return candidate
    return candidate if _SEVERITY_RANK[candidate] > _SEVERITY_RANK[current] else current


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dataclass-like object or a dict."""

    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name, default)
    return default if value is None else value


def _qualifying_defaults_for_party(
    defaults: Any,
    si_definition: frozenset[str],
    as_of: date,
) -> list[Any]:
    qualifying: list[Any] = []
    for d in defaults or ():
        instrument = _get_field(d, "instrument_type")
        status = _get_field(d, "status")
        reported_at = _get_field(d, "reported_at")

        if instrument not in si_definition:
            continue
        if status not in _QUALIFYING_STATUSES:
            continue
        if not isinstance(reported_at, date) or reported_at > as_of:
            continue
        qualifying.append(d)
    return qualifying


def _check_currency_homogeneity(
    qualifying: list[Any],
    threshold_currency: str,
    party: str,
) -> None:
    """Raise ``DataInconsistentError`` if any default is in a foreign currency."""

    foreign: list[tuple[str, str]] = []
    for d in qualifying:
        currency = _get_field(d, "currency")
        if currency != threshold_currency:
            default_id = _get_field(d, "default_id") or "<unknown>"
            foreign.append((str(default_id), str(currency)))
    if foreign:
        joined = ", ".join(f"{did}={cur!r}" for did, cur in foreign)
        raise DataInconsistentError(
            f"R-003: party {party!r} threshold currency is "
            f"{threshold_currency!r} but qualifying defaults use other "
            f"currencies: {joined}. Cross-currency conversion is not "
            f"attempted in V1."
        )


def _aggregate_amount(qualifying: list[Any], party: str) -> Decimal:
    total = Decimal("0")
    for d in qualifying:
        amount = _get_field(d, "amount_due")
        if not isinstance(amount, Decimal) or amount < 0:
            default_id = _get_field(d, "default_id") or "<unknown>"
            raise ValueError(
                f"R-003: party {party!r} default {default_id!r} amount_due "
                f"must be a non-negative Decimal; got {amount!r}"
            )
        total += amount
    return total


def _build_default_evidence(
    party: str,
    qualifying: list[Any],
) -> list[Evidence]:
    rows: list[Evidence] = []
    for d in qualifying:
        default_id = _get_field(d, "default_id") or "<anon>"
        rows.append(
            Evidence(
                kind="external_default",
                key=f"external_defaults[{party}][{default_id}]",
                value=", ".join(
                    [
                        f"instrument_type={_get_field(d, 'instrument_type')}",
                        f"status={_get_field(d, 'status')}",
                        f"amount_due={_get_field(d, 'amount_due')}",
                        f"currency={_get_field(d, 'currency')}",
                        f"reported_at={_get_field(d, 'reported_at')}",
                    ]
                ),
                source=str(_get_field(d, "source_reference") or "manual"),
            )
        )
    return rows


def _evaluate_party(
    party: str,
    party_defaults: Any,
    schedule: Any,
    as_of: date,
) -> tuple[Severity, list[Evidence]] | None:
    """Assess one party. Return (severity, evidence) or ``None`` if silent."""

    threshold_amount = _get_field(
        _get_field(schedule, "cross_default_threshold_amount", {}),
        party,
    )
    threshold_currency = _get_field(
        _get_field(schedule, "cross_default_threshold_currency", {}),
        party,
    )
    if not isinstance(threshold_amount, Decimal) or threshold_amount < 0:
        raise ValueError(
            f"R-003: party {party!r} cross_default_threshold_amount must be a "
            f"non-negative Decimal; got {threshold_amount!r}"
        )
    if not isinstance(threshold_currency, str) or not threshold_currency:
        raise ValueError(
            f"R-003: party {party!r} cross_default_threshold_currency must be "
            f"a non-empty string; got {threshold_currency!r}"
        )

    si_raw = _get_field(schedule, "specified_indebtedness_definition", ())
    si_definition: frozenset[str] = frozenset(
        str(item) for item in si_raw
    )

    qualifying = _qualifying_defaults_for_party(party_defaults, si_definition, as_of)
    _check_currency_homogeneity(qualifying, threshold_currency, party)
    aggregate = _aggregate_amount(qualifying, party)

    if aggregate == 0:
        return None

    severity = (
        Severity.POTENTIAL_TRIGGER
        if aggregate >= threshold_amount
        else Severity.WARNING
    )

    evidence = _build_default_evidence(party, qualifying)
    evidence.append(
        Evidence(
            kind="contract_field",
            key=f"cross_default[{party}].aggregate",
            value=(
                f"aggregate={aggregate} {threshold_currency}, "
                f"threshold={threshold_amount} {threshold_currency}, "
                f"comparison={'>=' if aggregate >= threshold_amount else '<'}, "
                f"severity={severity.value}"
            ),
            source="oracle",
        )
    )
    return severity, evidence


def _predicate(
    market: MarketState,
    contract: Any,
    as_of: date,
) -> RuleOutcome:
    """R-003 predicate. Pure function of its inputs (ARCH §A.2)."""

    if not hasattr(contract, "external_defaults") and not (
        isinstance(contract, dict) and "external_defaults" in contract
    ):
        return RuleOutcome(
            fired=False,
            indeterminate=True,
            indeterminate_reason="R-003: contract has no external_defaults field",
        )

    external_defaults = _get_field(contract, "external_defaults", {})
    schedule = _get_field(contract, "schedule")
    applies_map = _get_field(schedule, "cross_default_applies", {})

    if not isinstance(applies_map, dict):
        raise ValueError(
            "R-003: schedule.cross_default_applies must be a mapping; "
            f"got {type(applies_map).__name__}"
        )

    highest: Severity | None = None
    evidence: list[Evidence] = []

    for party, applies in applies_map.items():
        if not bool(applies):
            continue
        party_defaults = (
            external_defaults.get(party, ())
            if isinstance(external_defaults, dict)
            else ()
        )
        outcome = _evaluate_party(party, party_defaults, schedule, as_of)
        if outcome is None:
            continue
        severity, party_evidence = outcome
        highest = _escalate(highest, severity)
        evidence.extend(party_evidence)

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
        required_contract_fields=frozenset({"external_defaults", "schedule"}),
        grace_period=timedelta(0),
        version=_VERSION,
        description=(
            "Cross Default per ISDA 2002 §5(a)(vi): aggregate qualifying "
            "external defaults against the per-party Threshold Amount. V1 "
            "never auto-TRIGGERs (max severity POTENTIAL_TRIGGER) and refuses "
            "currency conversion (mixed currencies raise DataInconsistent)."
        ),
    )
)
