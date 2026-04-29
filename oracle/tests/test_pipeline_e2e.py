"""End-to-end pipeline test — Phase 5 stop condition.

One run of :func:`run_daily_cycle` against a fixture that *should* fire
R-001 (overdue payment + a notice older than the grace period). Asserts:

* Exactly one attestation persisted, chained on genesis.
* Exactly one TriggerEvent persisted, ``rule_id == "R-001"`` and severity
  ``TRIGGER``.
* :meth:`AttestationStore.verify_integrity` returns ``(True, None)``.
* The engine's state was **not** mutated except via the bridge's single
  documented write path: ``oracle_trigger_events`` gained one entry, and
  ``audit.log`` recorded one ``ORACLE_TRIGGER_EVENT``. Nothing else
  changed (periods are unmodified, snapshot at start == snapshot at end).
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from oracle.collectors.fake import FakeCollector
from oracle.config import Severity
from oracle.core.store import AttestationStore
from oracle.integration.irs_bridge import IRSBridge
from oracle.scheduler.daily_run import (
    discover_metrics_in_fixture,
    run_daily_cycle,
)


# ---------------------------------------------------------------------------
# Engine test-double — duck-types only what the bridge actually touches.
# ---------------------------------------------------------------------------


@dataclass
class _Period:
    period_number: int
    net_amount: Decimal
    payment_date: date
    payment_confirmed: bool


@dataclass
class _Params:
    contract_id: str
    effective_date: date = date(2025, 1, 15)
    termination_date: date = date(2027, 1, 15)
    grace_period_failure_to_pay_days: int = 1


@dataclass
class _AuditLog:
    entries: list[tuple[str, dict, str]] = field(default_factory=list)

    def log(self, kind: str, payload: dict, *, actor: str = "SYSTEM") -> None:
        self.entries.append((kind, dict(payload), actor))


@dataclass
class _FakeEngine:
    """Minimal engine surface required by ``backend.engine`` integration helpers.

    ``backend.engine.get_oracle_contract_snapshot`` reads ``params.contract_id``,
    ``params.effective_date``, ``params.termination_date``, and iterates
    ``periods`` for ``period_number / net_amount / payment_date / payment_confirmed``.
    ``backend.engine.submit_trigger_event`` appends to ``oracle_trigger_events``
    and writes to ``audit.log``.
    """

    params: _Params
    periods: list[_Period]
    audit: _AuditLog = field(default_factory=_AuditLog)
    oracle_trigger_events: list = field(default_factory=list)


def _build_fake_engine(contract_id: str, *, due_date: date) -> _FakeEngine:
    """Engine with one PENDING period whose ``payment_date`` is ``due_date``."""

    return _FakeEngine(
        params=_Params(contract_id=contract_id),
        periods=[
            _Period(
                period_number=1,
                net_amount=Decimal("125000.00"),
                payment_date=due_date,
                payment_confirmed=False,
            ),
        ],
    )


def _engine_snapshot(engine: _FakeEngine) -> dict[str, Any]:
    """Capture every observable engine attribute except oracle/audit fields."""

    return {
        "params": copy.deepcopy(engine.params),
        "periods": copy.deepcopy(engine.periods),
    }


# ---------------------------------------------------------------------------
# Fixture YAML — a single ESTR row so the cycle has *something* to attest.
# ---------------------------------------------------------------------------


def _write_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "estr.yaml"
    fixture.write_text(
        yaml.safe_dump(
            {
                "datapoints": [
                    {
                        "metric": "ESTR",
                        "value": "0.0375",
                        "unit": "decimal_fraction",
                        "as_of": "2026-04-23",
                        "source_reported_as_of": "2026-04-23",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return fixture


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_e2e_r001_fires_and_persists(tmp_path: Path) -> None:
    """One full cycle: attest, evaluate, submit, persist."""

    # Force a fresh import path: the registry is module-level, but R-001
    # may not be registered yet if no other test has imported it.
    import oracle.rules.impl.r001_failure_to_pay  # noqa: F401
    from oracle.rules.registry import get_all_rules

    rules = tuple(get_all_rules())
    assert any(r.rule_id == "R-001" for r in rules), (
        "R-001 not registered — registry import failed"
    )

    contract_id = "E2E-001"
    as_of = date(2026, 4, 23)  # Thursday

    # Notice sent Tue 2026-04-21 → grace_end Wed 2026-04-22 (+1 biz day).
    # as_of Thu 2026-04-23 > grace_end → R-001 escalates to TRIGGER.
    notice_sent_at = date(2026, 4, 21)
    due_date = date(2026, 4, 17)  # any earlier weekday — overdue on as_of

    # The snapshot's payment_id is str(period.period_number) (period 1 → "1").
    notice = _Notice(kind="failure_to_pay", payment_id="1", sent_at=notice_sent_at)

    engine = _build_fake_engine(contract_id, due_date=due_date)
    before = _engine_snapshot(engine)

    bridge = IRSBridge(
        resolver=lambda cid: engine if cid == contract_id else (_ for _ in ()).throw(
            KeyError(cid)
        ),
        notices_provider=lambda _cid: (notice,),
    )

    fixture_path = _write_fixture(tmp_path)
    metrics = discover_metrics_in_fixture(fixture_path)
    collector = FakeCollector(fixture_path)
    store = AttestationStore(tmp_path / "oracle.db")

    result = asyncio.run(
        run_daily_cycle(
            collector=collector,
            store=store,
            bridge=bridge,
            rules=rules,
            contract_id=contract_id,
            metrics=metrics,
            as_of=as_of,
        )
    )

    # ── DailyCycleResult shape ─────────────────────────────────────────────
    assert result.attestations_created == 1
    assert result.triggers_emitted == 1
    assert result.source_failures == 0
    assert result.indeterminate_rules == 0
    assert result.attestation_id is not None
    assert len(result.trigger_event_ids) == 1
    assert result.as_of == as_of

    # ── Persistence ────────────────────────────────────────────────────────
    attestations = store.get_all_attestations()
    assert len(attestations) == 1, "expected exactly one attestation persisted"
    att = attestations[0]
    assert att.is_genesis is True
    assert att.sequence_number == 0
    assert att.previous_hash is None
    assert att.attestation_id == result.attestation_id

    triggers = _read_triggers(store)
    assert len(triggers) == 1, "expected exactly one TriggerEvent persisted"
    t = triggers[0]
    assert t["rule_id"] == "R-001"
    assert t["severity"] == Severity.TRIGGER.value
    assert t["contract_id"] == contract_id
    assert t["attestation_ref"] == str(att.attestation_id)
    assert t["as_of"] == as_of.isoformat()

    # ── Chain integrity ────────────────────────────────────────────────────
    ok, err = store.verify_integrity()
    assert ok, f"chain integrity failed: {err}"

    # ── Engine state untouched except via the bridge's single write path ───
    after = _engine_snapshot(engine)
    assert after == before, (
        "the Oracle modified engine state outside of submit_trigger_event"
    )
    assert len(engine.oracle_trigger_events) == 1, (
        "the bridge should have appended exactly one TriggerEvent"
    )
    assert engine.oracle_trigger_events[0].rule_id == "R-001"

    audit_kinds = [entry[0] for entry in engine.audit.entries]
    assert audit_kinds == ["ORACLE_TRIGGER_EVENT"], (
        f"expected exactly one ORACLE_TRIGGER_EVENT audit entry; got {audit_kinds}"
    )
    audit_payload = engine.audit.entries[0][1]
    assert audit_payload["rule_id"] == "R-001"
    assert audit_payload["severity"] == Severity.TRIGGER.value
    assert engine.audit.entries[0][2] == "ORACLE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Notice:
    """Duck-type for an OracleNotice used by R-001 + the bridge."""

    kind: str
    payment_id: str
    sent_at: date


def _read_triggers(store: AttestationStore) -> list[dict]:
    """Read trigger_events back via the store's connection helper."""

    import sqlite3

    conn = sqlite3.connect(str(store._db_path))  # noqa: SLF001 — test-only
    try:
        cur = conn.execute(
            "SELECT event_id, rule_id, rule_version, clause_ref, severity, "
            "contract_id, evaluated_at, as_of, attestation_ref, "
            "evidence_json, rules_version FROM trigger_events"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
