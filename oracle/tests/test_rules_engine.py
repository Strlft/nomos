"""Rule engine orchestration tests.

Three responsibilities exercised here:

* **Multi-rule routing** — the engine emits one TriggerEvent per fired
  rule and leaves silent rules silent.
* **Indeterminate handling** — a rule whose required metrics are missing
  from MarketState is skipped; no event is emitted.
* **Exception isolation** — a rule whose predicate raises does not
  crash the engine; other rules still run.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from oracle.config import Metric, RULES_VERSION, Severity, Unit
from oracle.rules.engine import RuleEngine
from oracle.types import (
    Evidence,
    MarketState,
    NormalizedDatapoint,
    Rule,
    RuleOutcome,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_datapoint(
    metric: Metric = Metric.ESTR, value: str = "0.0375"
) -> NormalizedDatapoint:
    return NormalizedDatapoint(
        source_id="fake_v1",
        metric=metric,
        value=Decimal(value),
        unit=Unit.DECIMAL_FRACTION,
        as_of=date(2026, 4, 23),
        fetched_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        source_hash="deadbeef",
        source_url="file://fake",
        sanity_band_passed=True,
        cross_validated=False,
    )


def _make_rule(
    *,
    rule_id: str,
    fired: bool,
    severity: Severity | None = Severity.WARNING,
    required_metrics: frozenset[Metric] = frozenset(),
    raises: type[BaseException] | None = None,
    indeterminate: bool = False,
) -> Rule:
    def predicate(market: Any, contract: Any, as_of: date) -> RuleOutcome:
        if raises is not None:
            raise raises("boom")
        return RuleOutcome(
            fired=fired,
            severity=severity if fired else None,
            evidence=(
                Evidence(
                    kind="contract_field",
                    key=f"{rule_id}.key",
                    value="v",
                    source="oracle",
                ),
            )
            if fired
            else (),
            indeterminate=indeterminate,
            indeterminate_reason="missing-fact" if indeterminate else None,
        )

    return Rule(
        rule_id=rule_id,
        clause_ref=f"ISDA 2002 §test({rule_id})",
        severity=severity or Severity.WARNING,
        predicate=predicate,
        required_metrics=required_metrics,
        required_contract_fields=frozenset(),
        grace_period=timedelta(0),
        version="1.0.0",
        description=f"test rule {rule_id}",
    )


def _market(
    *,
    latest: dict[Metric, NormalizedDatapoint] | None = None,
    attestation_refs: dict[Metric, UUID] | None = None,
) -> MarketState:
    return MarketState(
        built_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        latest=latest or {},
        attestation_refs=attestation_refs or {},
        missing=frozenset(),
        missing_consecutive_days={},
    )


def _contract(contract_id: str = "C-xyz") -> Any:
    return SimpleNamespace(contract_id=contract_id)


# ---------------------------------------------------------------------------
# Multi-rule routing
# ---------------------------------------------------------------------------


class TestMultiRuleRouting:
    def test_only_fired_rules_produce_events(self) -> None:
        fires_warning = _make_rule(rule_id="T-A", fired=True, severity=Severity.WARNING)
        silent = _make_rule(rule_id="T-B", fired=False)
        fires_trigger = _make_rule(
            rule_id="T-C", fired=True, severity=Severity.TRIGGER
        )

        engine = RuleEngine([fires_warning, silent, fires_trigger])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))

        assert [e.rule_id for e in events] == ["T-A", "T-C"]
        assert {e.severity for e in events} == {Severity.WARNING, Severity.TRIGGER}
        # All events carry the same as_of/contract_id/rules_version.
        for e in events:
            assert e.contract_id == "C-xyz"
            assert e.as_of == date(2026, 4, 23)
            assert e.rules_version == RULES_VERSION
            assert e.evidence  # non-empty

    def test_preserves_rule_order(self) -> None:
        rules = [
            _make_rule(rule_id=f"T-{i}", fired=True, severity=Severity.WARNING)
            for i in range(5)
        ]
        engine = RuleEngine(rules)
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))
        assert [e.rule_id for e in events] == [f"T-{i}" for i in range(5)]

    def test_duplicate_rule_id_rejected_at_construction(self) -> None:
        a = _make_rule(rule_id="DUP", fired=False)
        b = _make_rule(rule_id="DUP", fired=True)
        with pytest.raises(ValueError, match="duplicate rule_id"):
            RuleEngine([a, b])


# ---------------------------------------------------------------------------
# Indeterminate handling
# ---------------------------------------------------------------------------


class TestIndeterminateHandling:
    def test_missing_required_metric_skips_rule(self) -> None:
        needs_estr = _make_rule(
            rule_id="NEEDS-ESTR",
            fired=True,
            severity=Severity.WARNING,
            required_metrics=frozenset({Metric.ESTR}),
        )
        engine = RuleEngine([needs_estr])

        # MarketState has no ESTR datapoint → rule is indeterminate → no event.
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))
        assert events == []

    def test_metric_present_then_rule_fires(self) -> None:
        needs_estr = _make_rule(
            rule_id="NEEDS-ESTR",
            fired=True,
            severity=Severity.WARNING,
            required_metrics=frozenset({Metric.ESTR}),
        )
        engine = RuleEngine([needs_estr])

        attestation_id = uuid4()
        market = _market(
            latest={Metric.ESTR: _make_datapoint(Metric.ESTR)},
            attestation_refs={Metric.ESTR: attestation_id},
        )
        events = engine.evaluate(market, _contract(), date(2026, 4, 23))

        assert len(events) == 1
        assert events[0].attestation_ref == attestation_id

    def test_predicate_returns_indeterminate_emits_no_event(self) -> None:
        rule = _make_rule(
            rule_id="T-IND",
            fired=False,
            indeterminate=True,
        )
        engine = RuleEngine([rule])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))
        assert events == []

    def test_indeterminate_does_not_block_other_rules(self) -> None:
        ind = _make_rule(rule_id="T-IND", fired=False, indeterminate=True)
        fires = _make_rule(rule_id="T-FIRES", fired=True, severity=Severity.WARNING)
        engine = RuleEngine([ind, fires])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))
        assert [e.rule_id for e in events] == ["T-FIRES"]


# ---------------------------------------------------------------------------
# Exception isolation
# ---------------------------------------------------------------------------


class TestExceptionIsolation:
    def test_exploding_rule_does_not_crash_engine(self) -> None:
        boom = _make_rule(rule_id="T-BOOM", fired=False, raises=RuntimeError)
        fires = _make_rule(rule_id="T-OK", fired=True, severity=Severity.TRIGGER)

        engine = RuleEngine([boom, fires])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))

        assert [e.rule_id for e in events] == ["T-OK"]

    def test_exploding_rule_in_middle_still_runs_later_rules(self) -> None:
        first = _make_rule(rule_id="A", fired=True, severity=Severity.WARNING)
        boom = _make_rule(rule_id="B", fired=False, raises=ValueError)
        last = _make_rule(rule_id="C", fired=True, severity=Severity.TRIGGER)

        engine = RuleEngine([first, boom, last])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))

        assert [e.rule_id for e in events] == ["A", "C"]

    def test_two_different_exception_types_both_isolated(self) -> None:
        boom_runtime = _make_rule(
            rule_id="RT", fired=False, raises=RuntimeError
        )
        boom_zero = _make_rule(
            rule_id="ZD", fired=False, raises=ZeroDivisionError
        )
        fires = _make_rule(rule_id="OK", fired=True, severity=Severity.WARNING)

        engine = RuleEngine([boom_runtime, boom_zero, fires])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))
        assert [e.rule_id for e in events] == ["OK"]


# ---------------------------------------------------------------------------
# TriggerEvent fields carry the rule's identity
# ---------------------------------------------------------------------------


class TestTriggerEventShape:
    def test_event_fields_mirror_the_rule(self) -> None:
        rule = _make_rule(
            rule_id="T-FIELDS",
            fired=True,
            severity=Severity.POTENTIAL_TRIGGER,
        )
        engine = RuleEngine([rule])
        events = engine.evaluate(_market(), _contract("C-1"), date(2026, 4, 23))

        assert len(events) == 1
        e = events[0]
        assert e.rule_id == "T-FIELDS"
        assert e.rule_version == "1.0.0"
        assert e.clause_ref == "ISDA 2002 §test(T-FIELDS)"
        assert e.severity is Severity.POTENTIAL_TRIGGER
        assert e.contract_id == "C-1"
        assert e.as_of == date(2026, 4, 23)
        assert e.rules_version == RULES_VERSION
        assert isinstance(e.attestation_ref, UUID)  # nil uuid is fine when no market

    def test_fired_without_severity_is_dropped(self) -> None:
        # Defensive: if a rule returns fired=True, severity=None we should
        # not produce a TriggerEvent (TriggerEvent.severity is non-nullable).
        def bad_predicate(market: Any, contract: Any, as_of: date) -> RuleOutcome:
            return RuleOutcome(fired=True, severity=None, evidence=())

        rule = Rule(
            rule_id="BAD",
            clause_ref="n/a",
            severity=Severity.WARNING,
            predicate=bad_predicate,
            required_metrics=frozenset(),
            required_contract_fields=frozenset(),
            grace_period=timedelta(0),
            version="1.0.0",
            description="malformed",
        )
        engine = RuleEngine([rule])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))
        assert events == []


# ---------------------------------------------------------------------------
# Rules registry is not required by the engine itself
# ---------------------------------------------------------------------------


class TestEngineIgnoresGlobalRegistry:
    """RuleEngine takes rules explicitly; it does not read the global registry.

    This guarantees test isolation — one test module can't pollute another
    through the registry.
    """

    def test_unregistered_rules_still_run(self) -> None:
        rule = _make_rule(rule_id="T-UNREG", fired=True, severity=Severity.WARNING)
        engine = RuleEngine([rule])
        events = engine.evaluate(_market(), _contract(), date(2026, 4, 23))
        assert [e.rule_id for e in events] == ["T-UNREG"]
