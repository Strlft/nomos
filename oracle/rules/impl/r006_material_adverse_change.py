"""R-006 — Material Adverse Change (Schedule ATE / bespoke MAC clause).

V1 monitors three structured indicators (per ``ORACLE_RULES.md`` §R-006):

A. **Credit rating downgrade** ≥ 2 notches from the contract-inception
   baseline, on the S&P or Moody's scale.
B. **External payment default** in a 90-calendar-day window (shares
   ``contract.external_defaults`` with R-003).
C. **Sanctions designation** active as of ``as_of`` (effective and not
   yet delisted).

Per-party severity ladder::

    0 indicators triggered → no event
    1 indicator triggered  → WARNING
    ≥2 indicators          → POTENTIAL_TRIGGER

When both parties are in scope the rule emits **one** outcome with the
maximum per-party severity. The Evidence tuple enumerates every
indicator that fired, on every party — auditors see the full dossier.

R-006 **never auto-TRIGGERs**. MAC characterisation is a legal judgment;
the rule's job is to surface the dossier, not issue a verdict.

Contract shape (duck-typed)::

    contract.contract_id: str
    contract.schedule:
        mac_applies:               Mapping[str, bool]
        credit_rating_baseline:    Mapping[str, str]   (e.g. "A+", "Baa2")
    contract.credit_rating_actions:   Mapping[str, Iterable[RatingAction]]
    contract.external_defaults:       Mapping[str, Iterable[ExternalDefault]]
    contract.sanctions_designations:  Mapping[str, Iterable[SanctionsDesignation]]

If the contract has no ``schedule`` attribute at all the rule returns
``indeterminate=True``. An empty ``mac_applies`` mapping (or all-False)
is a legitimate "no trigger".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from oracle.config import Severity
from oracle.rules.registry import register_rule
from oracle.types import Evidence, MarketState, Rule, RuleOutcome


_RULE_ID = "R-006"
_CLAUSE_REF = "Schedule ATE / bespoke MAC clause"
_VERSION = "1.0.0"

#: Calendar-day window for indicator B (external payment default).
EXTERNAL_DEFAULT_WINDOW_DAYS: int = 90

#: Notch count that escalates a rating downgrade into indicator A.
RATING_DOWNGRADE_NOTCHES: int = 2

_INDICATOR_RATING = "rating_downgrade"
_INDICATOR_PAYMENT_DEFAULT = "external_payment_default"
_INDICATOR_SANCTIONS = "sanctions_designation"


# Unified rating scale: S&P/Fitch and Moody's mapped to a single
# 0-indexed ordering (0 = highest investment grade, larger = worse).
# When baseline and current are on different scales the notch count is
# still well-defined because both map into this single table.
_RATING_SCALE: dict[str, int] = {
    # AAA / Aaa
    "AAA": 0, "AAA+": 0, "AAA-": 0, "AAA": 0,
    "AAA_SP": 0, "AAA_FITCH": 0, "AAA_MOODY": 0,
    "Aaa": 0,
    # AA+ / Aa1
    "AA+": 1, "Aa1": 1,
    # AA / Aa2
    "AA": 2, "Aa2": 2,
    # AA- / Aa3
    "AA-": 3, "Aa3": 3,
    # A+ / A1
    "A+": 4, "A1": 4,
    # A / A2
    "A": 5, "A2": 5,
    # A- / A3
    "A-": 6, "A3": 6,
    # BBB+ / Baa1
    "BBB+": 7, "Baa1": 7,
    # BBB / Baa2
    "BBB": 8, "Baa2": 8,
    # BBB- / Baa3
    "BBB-": 9, "Baa3": 9,
    # BB+ / Ba1
    "BB+": 10, "Ba1": 10,
    # BB / Ba2
    "BB": 11, "Ba2": 11,
    # BB- / Ba3
    "BB-": 12, "Ba3": 12,
    # B+ / B1
    "B+": 13, "B1": 13,
    # B / B2
    "B": 14, "B2": 14,
    # B- / B3
    "B-": 15, "B3": 15,
    # CCC+ / Caa1
    "CCC+": 16, "Caa1": 16,
    # CCC / Caa2
    "CCC": 17, "Caa2": 17,
    # CCC- / Caa3
    "CCC-": 18, "Caa3": 18,
    # CC / Ca
    "CC": 19, "Ca": 19,
    # C
    "C": 20,
    # D — selective default / default (S&P only)
    "D": 21, "SD": 21,
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
    """Read ``name`` from a dataclass-like object or a dict."""

    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name, default)
    return default if value is None else value


def _rating_index(rating: Any) -> int | None:
    """Map a rating string to its position in the unified scale, or ``None``."""

    if not isinstance(rating, str):
        return None
    return _RATING_SCALE.get(rating.strip())


# ---------------------------------------------------------------------------
# Indicator A — credit rating downgrade ≥ 2 notches
# ---------------------------------------------------------------------------


def _latest_rating_action(actions: Any, as_of: date) -> Any | None:
    """Return the most recent rating action with effective_date ≤ as_of."""

    eligible = []
    for action in actions or ():
        eff = _get_field(action, "effective_date")
        if not isinstance(eff, date) or eff > as_of:
            continue
        eligible.append(action)
    if not eligible:
        return None
    return max(eligible, key=lambda a: _get_field(a, "effective_date"))


def _check_rating_downgrade(
    party: str,
    baseline: Any,
    actions: Any,
    as_of: date,
) -> tuple[bool, Evidence | None, str | None]:
    """Return ``(triggered, evidence, indeterminate_reason)``.

    A non-None ``indeterminate_reason`` is propagated upstream — the rule
    cannot evaluate this party without a parseable baseline.
    """

    baseline_index = _rating_index(baseline)
    if baseline_index is None:
        return False, None, (
            f"R-006: party {party!r} has no parseable credit_rating_baseline "
            f"(got {baseline!r})"
        )

    latest = _latest_rating_action(actions, as_of)
    if latest is None:
        return False, None, None

    new_rating = _get_field(latest, "new_rating")
    new_index = _rating_index(new_rating)
    if new_index is None:
        # Malformed action — surface as ValueError; engine isolates.
        raise ValueError(
            f"R-006: party {party!r} latest rating action has unparseable "
            f"new_rating={new_rating!r}"
        )

    notches = new_index - baseline_index
    if notches < RATING_DOWNGRADE_NOTCHES:
        return False, None, None

    evidence = Evidence(
        kind="contract_field",
        key=f"credit_rating_actions[{party}].latest",
        value=(
            f"baseline={baseline}, new_rating={new_rating}, "
            f"notches={notches}, threshold={RATING_DOWNGRADE_NOTCHES}, "
            f"agency={_get_field(latest, 'agency') or 'unspecified'}, "
            f"effective_date={_get_field(latest, 'effective_date')}"
        ),
        source=str(_get_field(latest, "source_reference") or "manual"),
    )
    return True, evidence, None


# ---------------------------------------------------------------------------
# Indicator B — external payment default in 90-day window
# ---------------------------------------------------------------------------


def _check_payment_default(
    party: str,
    party_defaults: Any,
    as_of: date,
) -> tuple[bool, list[Evidence]]:
    window_start = as_of - timedelta(days=EXTERNAL_DEFAULT_WINDOW_DAYS)
    hits: list[Any] = []
    for d in party_defaults or ():
        status = _get_field(d, "status")
        reported_at = _get_field(d, "reported_at")
        if status != "payment_default":
            continue
        if not isinstance(reported_at, date):
            continue
        if reported_at < window_start or reported_at > as_of:
            continue
        hits.append(d)

    if not hits:
        return False, []

    rows = [
        Evidence(
            kind="external_default",
            key=(
                f"external_defaults[{party}]"
                f"[{_get_field(d, 'default_id') or '<anon>'}]"
            ),
            value=(
                f"status=payment_default, "
                f"amount_due={_get_field(d, 'amount_due')}, "
                f"currency={_get_field(d, 'currency')}, "
                f"reported_at={_get_field(d, 'reported_at')}, "
                f"window_start={window_start.isoformat()}, "
                f"window_days={EXTERNAL_DEFAULT_WINDOW_DAYS}"
            ),
            source=str(_get_field(d, "source_reference") or "manual"),
        )
        for d in hits
    ]
    return True, rows


# ---------------------------------------------------------------------------
# Indicator C — active sanctions designation
# ---------------------------------------------------------------------------


def _check_sanctions(
    party: str,
    designations: Any,
    as_of: date,
) -> tuple[bool, list[Evidence]]:
    active: list[Any] = []
    for s in designations or ():
        eff = _get_field(s, "effective_date")
        delisted = _get_field(s, "delisted_date")
        if not isinstance(eff, date) or eff > as_of:
            continue
        if delisted is not None:
            if not isinstance(delisted, date):
                raise ValueError(
                    f"R-006: party {party!r} sanctions designation "
                    f"delisted_date must be a date or None; got {delisted!r}"
                )
            if delisted <= as_of:
                continue
        active.append(s)

    if not active:
        return False, []

    rows = [
        Evidence(
            kind="contract_field",
            key=(
                f"sanctions_designations[{party}]"
                f"[{_get_field(s, 'entity_id') or '<anon>'}]"
            ),
            value=(
                f"list_name={_get_field(s, 'list_name') or 'unspecified'}, "
                f"effective_date={_get_field(s, 'effective_date')}, "
                f"delisted_date={_get_field(s, 'delisted_date')}"
            ),
            source=str(_get_field(s, "source_reference") or "manual"),
        )
        for s in active
    ]
    return True, rows


# ---------------------------------------------------------------------------
# Per-party aggregation
# ---------------------------------------------------------------------------


def _evaluate_party(
    party: str,
    contract: Any,
    schedule: Any,
    as_of: date,
) -> tuple[Severity | None, list[Evidence], str | None]:
    """Run all three indicators for one party.

    Returns ``(severity_or_None, evidence, indeterminate_reason_or_None)``.
    Severity is None when no indicators fired. When ``indeterminate_reason``
    is non-None the caller short-circuits the whole evaluation.
    """

    baseline_map = _get_field(schedule, "credit_rating_baseline", {})
    baseline = (
        baseline_map.get(party) if isinstance(baseline_map, dict) else None
    )
    actions_map = _get_field(contract, "credit_rating_actions", {})
    party_actions = (
        actions_map.get(party, ()) if isinstance(actions_map, dict) else ()
    )
    rating_triggered, rating_ev, indeterminate = _check_rating_downgrade(
        party, baseline, party_actions, as_of
    )
    if indeterminate is not None:
        return None, [], indeterminate

    defaults_map = _get_field(contract, "external_defaults", {})
    party_defaults = (
        defaults_map.get(party, ()) if isinstance(defaults_map, dict) else ()
    )
    default_triggered, default_evs = _check_payment_default(
        party, party_defaults, as_of
    )

    sanctions_map = _get_field(contract, "sanctions_designations", {})
    party_sanctions = (
        sanctions_map.get(party, ())
        if isinstance(sanctions_map, dict)
        else ()
    )
    sanctions_triggered, sanctions_evs = _check_sanctions(
        party, party_sanctions, as_of
    )

    triggered_names: list[str] = []
    evidence: list[Evidence] = []
    if rating_triggered and rating_ev is not None:
        triggered_names.append(_INDICATOR_RATING)
        evidence.append(rating_ev)
    if default_triggered:
        triggered_names.append(_INDICATOR_PAYMENT_DEFAULT)
        evidence.extend(default_evs)
    if sanctions_triggered:
        triggered_names.append(_INDICATOR_SANCTIONS)
        evidence.extend(sanctions_evs)

    count = len(triggered_names)
    if count == 0:
        return None, [], None

    severity = (
        Severity.POTENTIAL_TRIGGER if count >= 2 else Severity.WARNING
    )
    evidence.append(
        Evidence(
            kind="contract_field",
            key=f"mac[{party}].indicator_set",
            value=(
                f"indicators_triggered={triggered_names}, "
                f"count={count}, severity={severity.value}"
            ),
            source="oracle",
        )
    )
    return severity, evidence, None


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def _predicate(
    market: MarketState,
    contract: Any,
    as_of: date,
) -> RuleOutcome:
    """R-006 predicate. Pure function of its inputs (ARCH §A.2)."""

    schedule = _get_field(contract, "schedule")
    if schedule is None:
        return RuleOutcome(
            fired=False,
            indeterminate=True,
            indeterminate_reason="R-006: contract has no schedule field",
        )

    applies_map = _get_field(schedule, "mac_applies", {})
    if not isinstance(applies_map, dict):
        raise ValueError(
            "R-006: schedule.mac_applies must be a mapping; "
            f"got {type(applies_map).__name__}"
        )

    in_scope_parties = [p for p, applies in applies_map.items() if bool(applies)]
    if not in_scope_parties:
        return RuleOutcome(fired=False)

    highest: Severity | None = None
    evidence: list[Evidence] = []

    for party in in_scope_parties:
        severity, party_evidence, indeterminate = _evaluate_party(
            party, contract, schedule, as_of
        )
        if indeterminate is not None:
            return RuleOutcome(
                fired=False,
                indeterminate=True,
                indeterminate_reason=indeterminate,
            )
        if severity is None:
            continue
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
        required_contract_fields=frozenset({"schedule"}),
        grace_period=timedelta(0),
        version=_VERSION,
        description=(
            "Material Adverse Change (Schedule ATE). V1 monitors three "
            "structured indicators per party: credit rating downgrade "
            "≥2 notches from contract-inception baseline, external "
            "payment default within a 90-calendar-day window, and active "
            "sanctions designation. Single indicator → WARNING; ≥2 "
            "indicators simultaneously → POTENTIAL_TRIGGER. Never "
            "auto-TRIGGERs — MAC characterisation is a legal judgment "
            "and the rule's job is to surface the dossier, not the "
            "verdict."
        ),
    )
)
