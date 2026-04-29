"""R-003 — Cross Default: every scenario in the spec's test matrix.

Two non-obvious invariants pinned here:

* Severity ceiling is ``POTENTIAL_TRIGGER`` — even with defaults far in
  excess of the threshold, the rule never escalates to ``TRIGGER``.
* Mixed-currency qualifying defaults raise :class:`DataInconsistentError`
  rather than attempting conversion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from oracle.config import Severity
from oracle.errors import DataInconsistentError
from oracle.rules.impl.r003_cross_default import rule as r003_rule
from oracle.types import MarketState


# ---------------------------------------------------------------------------
# Duck-typed contract objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalDefault:
    default_id: str
    instrument_type: str
    status: str
    amount_due: Decimal
    currency: str
    reported_at: date
    source_reference: str | None = None


@dataclass(frozen=True)
class Schedule:
    cross_default_applies: dict
    cross_default_threshold_amount: dict
    cross_default_threshold_currency: dict
    specified_indebtedness_definition: frozenset


@dataclass(frozen=True)
class Contract:
    contract_id: str = "C-0003"
    external_defaults: dict = field(default_factory=dict)
    schedule: Schedule | None = None


# ---------------------------------------------------------------------------
# Schedule factories — typical Schedule Part 1(c)(d) elections
# ---------------------------------------------------------------------------


def _bilateral_schedule(
    *,
    threshold_a: Decimal = Decimal("10000000"),
    threshold_b: Decimal = Decimal("10000000"),
    applies_a: bool = True,
    applies_b: bool = True,
    currency: str = "EUR",
) -> Schedule:
    return Schedule(
        cross_default_applies={"party_a": applies_a, "party_b": applies_b},
        cross_default_threshold_amount={"party_a": threshold_a, "party_b": threshold_b},
        cross_default_threshold_currency={"party_a": currency, "party_b": currency},
        specified_indebtedness_definition=frozenset({"loan", "bond"}),
    )


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_market() -> MarketState:
    return MarketState(
        built_at=datetime(2026, 4, 23, 8, 0, tzinfo=timezone.utc),
        latest={},
        attestation_refs={},
        missing=frozenset(),
        missing_consecutive_days={},
    )


def _evidence_keys(outcome) -> list[str]:
    return [e.key for e in outcome.evidence]


# ---------------------------------------------------------------------------
# 1. Cross default not applicable to either party
# ---------------------------------------------------------------------------


class TestScenario01_NotApplicable:
    def test_neither_party_elected_does_not_fire(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(applies_a=False, applies_b=False),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("50000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is False


# ---------------------------------------------------------------------------
# 2. Applicable, zero defaults → no trigger
# ---------------------------------------------------------------------------


class TestScenario02_ZeroDefaults:
    def test_empty_defaults_does_not_fire(self, empty_market: MarketState) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={"party_a": (), "party_b": ()},
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 3. Defaults below threshold → WARNING
# ---------------------------------------------------------------------------


class TestScenario03_BelowThreshold:
    def test_emits_warning_with_aggregate_evidence(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(
                threshold_a=Decimal("10000000"),  # 10m
            ),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("3000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.WARNING
        assert any(
            "aggregate=3000000 EUR" in e.value and "threshold=10000000 EUR" in e.value
            for e in outcome.evidence
        )


# ---------------------------------------------------------------------------
# 4. Defaults at threshold → POTENTIAL_TRIGGER
# ---------------------------------------------------------------------------


class TestScenario04_AtThreshold:
    def test_aggregate_equal_threshold_is_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(threshold_a=Decimal("10000000")),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="bond",
                        status="accelerated",
                        amount_due=Decimal("10000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER


# ---------------------------------------------------------------------------
# 5. Defaults well above threshold → POTENTIAL_TRIGGER (never TRIGGER)
# ---------------------------------------------------------------------------


class TestScenario05_AboveThreshold:
    def test_aggregate_far_above_threshold_caps_at_potential_trigger(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(threshold_a=Decimal("10000000")),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("250000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER, (
            "R-003 must NEVER auto-TRIGGER per ORACLE_RULES.md §R-003 — even "
            "with aggregate 25× the threshold"
        )
        assert outcome.severity is not Severity.TRIGGER


# ---------------------------------------------------------------------------
# 6. All defaults remediated → no trigger
# ---------------------------------------------------------------------------


class TestScenario06_AllRemediated:
    def test_remediated_defaults_do_not_qualify(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="loan",
                        status="remediated",
                        amount_due=Decimal("100000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                    ExternalDefault(
                        default_id="D2",
                        instrument_type="bond",
                        status="remediated",
                        amount_due=Decimal("50000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 7. Mixed currencies → DataInconsistentError, no event
# ---------------------------------------------------------------------------


class TestScenario07_MixedCurrencies:
    def test_mixed_currencies_raises_data_inconsistent(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(currency="EUR"),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="EUR-LOAN",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("5000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                    ExternalDefault(
                        default_id="USD-BOND",
                        instrument_type="bond",
                        status="accelerated",
                        amount_due=Decimal("6000000"),
                        currency="USD",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        with pytest.raises(DataInconsistentError) as excinfo:
            r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        msg = str(excinfo.value)
        assert "USD-BOND" in msg
        assert "'EUR'" in msg
        assert "conversion is not attempted" in msg

    def test_remediated_in_foreign_currency_does_not_raise(
        self, empty_market: MarketState
    ) -> None:
        # Remediated defaults are filtered before the currency check, so a
        # foreign currency here must not raise DataInconsistent.
        contract = Contract(
            schedule=_bilateral_schedule(currency="EUR"),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="USD-OLD",
                        instrument_type="loan",
                        status="remediated",
                        amount_due=Decimal("9999999"),
                        currency="USD",
                        reported_at=date(2026, 1, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# 8. Both parties cross, one below threshold → POTENTIAL_TRIGGER (max)
#    with evidence for both parties.
# ---------------------------------------------------------------------------


class TestScenario08_BothPartiesMixedSeverity:
    def test_one_event_with_both_parties_in_evidence(
        self, empty_market: MarketState
    ) -> None:
        # party_a: aggregate 5m vs threshold 10m → WARNING
        # party_b: aggregate 12m vs threshold 10m → POTENTIAL_TRIGGER
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="A-D1",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("5000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
                "party_b": (
                    ExternalDefault(
                        default_id="B-D1",
                        instrument_type="bond",
                        status="accelerated",
                        amount_due=Decimal("12000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is True
        assert outcome.severity is Severity.POTENTIAL_TRIGGER
        keys = _evidence_keys(outcome)
        # Per-default evidence and per-party aggregate evidence both present.
        assert any("[party_a][A-D1]" in k for k in keys)
        assert any("[party_b][B-D1]" in k for k in keys)
        assert any("cross_default[party_a].aggregate" in k for k in keys)
        assert any("cross_default[party_b].aggregate" in k for k in keys)


# ---------------------------------------------------------------------------
# Filtering edge cases
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_instrument_outside_si_definition_is_ignored(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),  # SI = {loan, bond}
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="trade_finance",  # not in SI
                        status="payment_default",
                        amount_due=Decimal("100000000"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False

    def test_future_reported_at_is_ignored(self, empty_market: MarketState) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("100000000"),
                        currency="EUR",
                        reported_at=date(2099, 1, 1),
                    ),
                ),
            },
        )
        outcome = r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
        assert outcome.fired is False


# ---------------------------------------------------------------------------
# Indeterminate + malformed schedule
# ---------------------------------------------------------------------------


class TestIndeterminateAndMalformed:
    def test_contract_without_external_defaults_is_indeterminate(
        self, empty_market: MarketState
    ) -> None:
        class BareContract:
            contract_id = "C-bare"

        outcome = r003_rule.predicate(empty_market, BareContract(), date(2026, 4, 23))
        assert outcome.fired is False
        assert outcome.indeterminate is True
        assert "external_defaults" in (outcome.indeterminate_reason or "")

    def test_negative_threshold_amount_raises(
        self, empty_market: MarketState
    ) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(threshold_a=Decimal("-1")),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D1",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("100"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        with pytest.raises(ValueError, match="non-negative Decimal"):
            r003_rule.predicate(empty_market, contract, date(2026, 4, 23))

    def test_negative_amount_due_raises(self, empty_market: MarketState) -> None:
        contract = Contract(
            schedule=_bilateral_schedule(),
            external_defaults={
                "party_a": (
                    ExternalDefault(
                        default_id="D-NEG",
                        instrument_type="loan",
                        status="payment_default",
                        amount_due=Decimal("-1"),
                        currency="EUR",
                        reported_at=date(2026, 4, 1),
                    ),
                ),
            },
        )
        with pytest.raises(ValueError, match="amount_due"):
            r003_rule.predicate(empty_market, contract, date(2026, 4, 23))

    def test_applies_map_must_be_a_mapping(self, empty_market: MarketState) -> None:
        contract = Contract(
            schedule=Schedule(
                cross_default_applies="yes",  # type: ignore[arg-type]
                cross_default_threshold_amount={"party_a": Decimal("1")},
                cross_default_threshold_currency={"party_a": "EUR"},
                specified_indebtedness_definition=frozenset({"loan"}),
            ),
            external_defaults={"party_a": ()},
        )
        with pytest.raises(ValueError, match="cross_default_applies"):
            r003_rule.predicate(empty_market, contract, date(2026, 4, 23))
