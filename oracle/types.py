"""Oracle typed objects — the contract between layers.

Every class is a Pydantic v2 model configured with ``frozen=True, strict=True``.
That gives us three properties the Oracle depends on:

* Immutability (I1) — published values never mutate.
* Strict type validation — no implicit ``float → Decimal`` coercion slips through.
* Hashability where relevant — so chain integrity checks can use models as keys.

Monetary and rate values are :class:`~decimal.Decimal`. Lists of immutable
children are declared as tuples so the model stays structurally hashable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from oracle.config import Metric, Severity, Unit


# ---------------------------------------------------------------------------
# Layer 1 — Collector output
# ---------------------------------------------------------------------------


class RawDatapoint(BaseModel):
    """A verbatim response from a single collector for a single metric.

    ``raw_payload`` is the UTF-8-decoded response body; ``source_hash`` is the
    SHA-256 hex digest of that payload so the Oracle can prove — years later —
    what bytes the source actually returned.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    source_id: str
    metric: str
    raw_payload: str
    source_hash: str
    fetched_at: datetime
    source_url: str
    source_reported_as_of: str | None = None


# ---------------------------------------------------------------------------
# Layer 2 — Oracle Core inputs/outputs
# ---------------------------------------------------------------------------


class NormalizedDatapoint(BaseModel):
    """A parsed, sanity-checked, cross-validated value ready to be signed."""

    model_config = ConfigDict(frozen=True, strict=True)

    source_id: str
    metric: Metric
    value: Decimal
    unit: Unit
    as_of: date
    fetched_at: datetime
    source_hash: str
    source_url: str
    sanity_band_passed: bool
    cross_validated: bool
    cross_checked_against: str | None = None


class OracleAttestation(BaseModel):
    """A signed, chained bundle of normalized datapoints (Invariants I1, I2).

    Genesis invariant (enforced by :meth:`_check_genesis_invariant`):
    * ``sequence_number == 0`` ⇔ ``is_genesis is True`` ⇔ ``previous_hash is None``.
    * ``sequence_number > 0``  ⇔ ``is_genesis is False`` and ``previous_hash`` set.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    attestation_id: UUID
    sequence_number: int
    datapoints: tuple[NormalizedDatapoint, ...]
    signed_at: datetime
    rules_version: str
    oracle_version: str

    # Chain fields
    payload_hash: str
    previous_hash: str | None
    current_hash: str
    is_genesis: bool

    # Supersession
    supersedes: UUID | None = None
    supersession_reason: str | None = None

    @model_validator(mode="after")
    def _check_genesis_invariant(self) -> "OracleAttestation":
        if self.sequence_number < 0:
            raise ValueError("sequence_number must be non-negative")

        if self.sequence_number == 0:
            if not self.is_genesis:
                raise ValueError(
                    "sequence_number=0 requires is_genesis=True"
                )
            if self.previous_hash is not None:
                raise ValueError(
                    "sequence_number=0 requires previous_hash=None"
                )
        else:
            if self.is_genesis:
                raise ValueError(
                    "sequence_number>0 requires is_genesis=False"
                )
            if self.previous_hash is None:
                raise ValueError(
                    "sequence_number>0 requires non-null previous_hash"
                )
        return self


# ---------------------------------------------------------------------------
# Layer 3 — Rules engine inputs/outputs
# ---------------------------------------------------------------------------


class MarketState(BaseModel):
    """Latest valid datapoint per metric, as seen by the Rules Engine."""

    model_config = ConfigDict(frozen=True, strict=True)

    built_at: datetime
    latest: Mapping[Metric, NormalizedDatapoint]
    attestation_refs: Mapping[Metric, UUID]
    missing: frozenset[Metric]
    missing_consecutive_days: Mapping[Metric, int]


class Evidence(BaseModel):
    """A single datum cited in a :class:`TriggerEvent`'s evidence tuple."""

    model_config = ConfigDict(frozen=True, strict=True)

    kind: Literal["market_datum", "contract_field", "external_default", "mac_indicator"]
    key: str
    value: str
    source: str


class RuleOutcome(BaseModel):
    """What a rule predicate returns.

    ``fired`` and ``indeterminate`` are mutually exclusive in practice but the
    model does not enforce it; the engine layer does.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    fired: bool
    severity: Severity | None = None
    evidence: tuple[Evidence, ...] = ()
    indeterminate: bool = False
    indeterminate_reason: str | None = None


class Rule(BaseModel):
    """A declarative rule — a pinned predicate plus its required-input surface."""

    # ``arbitrary_types_allowed`` is needed because ``predicate`` is a Callable
    # referencing ContractState which is defined by ``irs_engine_v2``.
    model_config = ConfigDict(frozen=True, strict=True, arbitrary_types_allowed=True)

    rule_id: str
    clause_ref: str
    severity: Severity
    predicate: Callable[..., RuleOutcome]
    required_metrics: frozenset[Metric]
    required_contract_fields: frozenset[str]
    grace_period: timedelta
    version: str
    description: str


class TriggerEvent(BaseModel):
    """The object emitted when a rule fires — consumed by the IRS engine."""

    model_config = ConfigDict(frozen=True, strict=True)

    event_id: UUID
    rule_id: str
    rule_version: str
    clause_ref: str
    severity: Severity
    contract_id: str
    evaluated_at: datetime
    as_of: date
    attestation_ref: UUID
    evidence: tuple[Evidence, ...]
    rules_version: str


# ---------------------------------------------------------------------------
# Failure record
# ---------------------------------------------------------------------------


class SourceFailure(BaseModel):
    """Audit record persisted whenever the Oracle refuses to publish (I5)."""

    model_config = ConfigDict(frozen=True, strict=True)

    failure_id: UUID
    source_id: str
    metric: Metric
    attempted_at: datetime
    failure_kind: Literal[
        "timeout",
        "http_4xx",
        "http_5xx",
        "parse_error",
        "sanity_band_violation",
        "network_error",
        "cross_validation_failure",
    ]
    attempts: int
    last_error_message: str
    source_url: str
    context: Mapping[str, str] = {}
