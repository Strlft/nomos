"""Seed a demo contract that fires R-001 (Failure to Pay, ISDA §5(a)(i)).

This script exists so the Oracle page at ``/oracle`` shows a real trigger
event in the "Recent triggers" panel instead of an empty state. It does
not generate market data — it composes:

1. A synthetic ``IRSExecutionEngine`` carrying one PENDING calculation
   period whose ``payment_date`` is 5 TARGET2 business days before
   ``as_of``.
2. A ``failure_to_pay`` :class:`OracleNotice` sent 4 TARGET2 business
   days before ``as_of`` (so 1 business day on the clock — the ISDA
   default grace — has already elapsed by ``as_of``).
3. An :class:`IRSBridge` over the synthetic engine.
4. The actual :class:`RuleEngine` from ``oracle.rules.engine``, run
   exactly as ``oracle.scheduler.daily_run`` runs it.

The TriggerEvent comes from ``RuleEngine.evaluate`` — it is *not*
constructed by hand. That preserves the audit trail.

Idempotency
-----------
The script is idempotent on the ``(rule_id, contract_id, as_of)`` triple:
a second run on the same database does not insert a duplicate
``trigger_events`` row. The check is a direct SELECT against the same
SQLite file the store wrote to.

Prerequisite
------------
The ``trigger_events.attestation_ref`` column is a foreign key into
``attestations(attestation_id)`` (see ``oracle/core/store.py``). The
referenced attestation must therefore exist in the store before this
script runs. Use ``python -m oracle.scheduler.daily_run --fixture …``
or ``--live-ecb …`` to publish at least one attestation first; the
script will refuse to run otherwise rather than fabricate a fallback.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4


# ---------------------------------------------------------------------------
# Path bootstrap — let the script run as `python -m oracle.scripts.…` AND
# as a direct file so the IRSBridge can `from backend.engine import …`.
# ---------------------------------------------------------------------------


def _ensure_project_root_on_syspath() -> None:
    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


_ensure_project_root_on_syspath()


from oracle.config import RULES_VERSION, Severity  # noqa: E402
from oracle.core.store import AttestationStore  # noqa: E402
from oracle.integration.irs_bridge import IRSBridge  # noqa: E402
from oracle.logging_config import (  # noqa: E402
    bind_trace_id,
    clear_trace_id,
    configure_logging,
    get_logger,
)
from oracle.rules.calendar import add_business_days, is_business_day  # noqa: E402
from oracle.rules.engine import RuleEngine  # noqa: E402
from oracle.types import MarketState  # noqa: E402


_log = get_logger("scheduler.seed_demo")


_DEFAULT_CONTRACT_ID = "DEMO-R001"


# ---------------------------------------------------------------------------
# Synthetic engine — duck-typed surface used by get_oracle_contract_snapshot
# ---------------------------------------------------------------------------


@dataclass
class _Period:
    """Subset of CalculationPeriod fields read by ``get_oracle_contract_snapshot``."""

    period_number: int
    payment_date: date
    net_amount: Decimal
    payment_confirmed: bool


@dataclass
class _Params:
    contract_id: str
    effective_date: date
    termination_date: date
    grace_period_failure_to_pay_days: int = 1


@dataclass
class _AuditLog:
    entries: list[tuple[str, dict, str]] = field(default_factory=list)

    def log(self, kind: str, payload: dict, *, actor: str = "SYSTEM") -> None:
        self.entries.append((kind, dict(payload), actor))


@dataclass
class _DemoEngine:
    """Minimal engine surface for :func:`backend.engine.get_oracle_contract_snapshot`.

    Reads: ``params.contract_id``, ``params.effective_date``,
    ``params.termination_date``, ``params.grace_period_failure_to_pay_days``,
    and iterates ``periods``. Writes (only via ``submit_trigger_event``):
    appends to ``oracle_trigger_events`` and to ``audit``.
    """

    params: _Params
    periods: list[_Period]
    audit: _AuditLog = field(default_factory=_AuditLog)
    oracle_trigger_events: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Calendar helper — walk back ``n`` TARGET2 business days from ``start``.
# ---------------------------------------------------------------------------


def _subtract_business_days(start: date, n: int) -> date:
    """Return the date that is ``n`` TARGET2 business days strictly before ``start``.

    Mirrors :func:`oracle.rules.calendar.add_business_days` but in reverse.
    Walking by single days is fine for the small ``n`` values this script
    uses.
    """

    if n < 0:
        raise ValueError(f"n must be non-negative; got {n}")
    if n == 0:
        return start
    from datetime import timedelta

    current = start
    remaining = n
    while remaining > 0:
        current = current - timedelta(days=1)
        if is_business_day(current):
            remaining -= 1
    return current


# ---------------------------------------------------------------------------
# Demo notice — duck-typed for R-001's ``_earliest_notice_for``
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DemoNotice:
    kind: str
    payment_id: str
    sent_at: date


# ---------------------------------------------------------------------------
# Build the synthetic engine
# ---------------------------------------------------------------------------


def _build_demo_engine(
    *,
    contract_id: str,
    as_of: date,
    amount: Decimal,
) -> _DemoEngine:
    due_date = _subtract_business_days(as_of, 5)
    period = _Period(
        period_number=1,
        payment_date=due_date,
        net_amount=amount,
        payment_confirmed=False,
    )
    params = _Params(
        contract_id=contract_id,
        # Effective ~ 90 days before as_of, termination ~ 2y after.
        # Specific dates don't matter to R-001 — it only reads payments + notices.
        effective_date=_subtract_business_days(as_of, 60),
        termination_date=add_business_days(as_of, 252),
    )
    return _DemoEngine(params=params, periods=[period])


def _build_notice(*, payment_id: str, as_of: date) -> _DemoNotice:
    return _DemoNotice(
        kind="failure_to_pay",
        payment_id=payment_id,
        sent_at=_subtract_business_days(as_of, 4),
    )


# ---------------------------------------------------------------------------
# MarketState builder — empty market data, refs from the latest attestation
# ---------------------------------------------------------------------------


def _build_market_state_from_latest(store: AttestationStore) -> MarketState | None:
    """Return a MarketState whose ``attestation_refs`` point at the latest
    persisted attestation.

    Returns ``None`` if the store has no attestations — the caller treats
    that as a precondition failure.
    """

    latest = store.get_latest_attestation()
    if latest is None or not latest.datapoints:
        return None
    refs = {dp.metric: latest.attestation_id for dp in latest.datapoints}
    return MarketState(
        built_at=datetime.now(timezone.utc),
        latest={},
        attestation_refs=refs,
        missing=frozenset(),
        missing_consecutive_days={},
    )


# ---------------------------------------------------------------------------
# Idempotency — direct SELECT, since AttestationStore exposes no read API
# for trigger_events.
# ---------------------------------------------------------------------------


def _existing_trigger(
    db_path: Path, *, rule_id: str, contract_id: str, as_of: date
) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT 1 FROM trigger_events "
            "WHERE rule_id = ? AND contract_id = ? AND as_of = ? LIMIT 1",
            (rule_id, contract_id, as_of.isoformat()),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seed-demo-contract",
        description=(
            "Seed a demo contract that fires R-001 (Failure to Pay) so the "
            "Oracle page shows a real trigger event. Idempotent on "
            "(rule_id, contract_id, as_of)."
        ),
    )
    parser.add_argument(
        "--db-path", required=True, type=Path,
        help="Path to the SQLite attestation store (must already contain "
             "at least one attestation with datapoints).",
    )
    parser.add_argument(
        "--contract-id", default=_DEFAULT_CONTRACT_ID, type=str,
        help=f"Contract identifier for the demo (default: {_DEFAULT_CONTRACT_ID}).",
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, default=None,
        help="Evaluation date (ISO 8601). Defaults to today (UTC).",
    )
    parser.add_argument(
        "--amount", type=Decimal, default=Decimal("50000"),
        help="EUR amount for the synthetic overdue payment (default: 50000).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parse_args(argv)
    as_of: date = args.as_of or datetime.now(timezone.utc).date()
    contract_id: str = args.contract_id

    bind_trace_id(str(uuid4()))
    try:
        _log.info(
            "seed_demo_start",
            action="seed_demo_contract",
            outcome="started",
            db_path=str(args.db_path),
            contract_id=contract_id,
            as_of=as_of.isoformat(),
        )

        store = AttestationStore(args.db_path)
        market = _build_market_state_from_latest(store)
        if market is None:
            _log.error(
                "seed_demo_no_attestation",
                action="seed_demo_contract",
                outcome="precondition_failure",
                reason=(
                    "trigger_events.attestation_ref is a foreign key into "
                    "attestations; run daily_run to publish at least one "
                    "attestation with datapoints first."
                ),
            )
            print(
                '{"status":"precondition_failure",'
                '"message":"no attestation in store; run oracle.scheduler.daily_run first"}'
            )
            return 2

        # Idempotency check. The trigger_events table has no UNIQUE
        # constraint on (rule_id, contract_id, as_of), so we guard on the
        # write side rather than rely on the schema.
        if _existing_trigger(
            args.db_path,
            rule_id="R-001",
            contract_id=contract_id,
            as_of=as_of,
        ):
            _log.info(
                "seed_demo_already_present",
                action="seed_demo_contract",
                outcome="noop",
                rule_id="R-001",
                contract_id=contract_id,
                as_of=as_of.isoformat(),
            )
            print(
                f'{{"status":"already_seeded","rule_id":"R-001",'
                f'"contract_id":"{contract_id}","as_of":"{as_of.isoformat()}",'
                f'"events_inserted":0}}'
            )
            return 0

        engine = _build_demo_engine(
            contract_id=contract_id, as_of=as_of, amount=args.amount
        )
        notice = _build_notice(payment_id="1", as_of=as_of)
        engines_by_id: dict[str, Any] = {contract_id: engine}
        bridge = IRSBridge(
            resolver=lambda cid: engines_by_id[cid],
            notices_provider=lambda _cid: (notice,),
        )

        # Register R-001 (and any other rules wired into the registry) and
        # run the real RuleEngine — the TriggerEvent we persist below comes
        # from the engine, not from this script.
        import oracle.rules.impl.r001_failure_to_pay  # noqa: F401
        from oracle.rules.registry import get_all_rules

        rules = get_all_rules()
        snapshot = bridge.fetch_contract_state(contract_id)
        events = RuleEngine(rules).evaluate(market, snapshot, as_of)

        r001_events = [
            e
            for e in events
            if e.rule_id == "R-001" and e.severity is Severity.TRIGGER
        ]
        if not r001_events:
            _log.error(
                "seed_demo_no_trigger",
                action="seed_demo_contract",
                outcome="failure",
                reason="R-001 did not fire — check overdue/notice/grace inputs",
                events_emitted=[e.rule_id for e in events],
            )
            print(
                '{"status":"failure","message":"R-001 did not fire",'
                f'"events_emitted":{len(events)}}}'
            )
            return 3

        for event in r001_events:
            store.record_trigger(event)
            bridge.submit_trigger_event(event)

        ok, err = store.verify_integrity()
        if not ok:
            _log.error(
                "seed_demo_chain_broken",
                action="seed_demo_contract",
                outcome="failure",
                error=err,
            )
            print(f'{{"status":"chain_broken","error":"{err}"}}')
            return 4

        ev = r001_events[0]
        _log.info(
            "seed_demo_complete",
            action="seed_demo_contract",
            outcome="ok",
            rule_id=ev.rule_id,
            event_id=str(ev.event_id),
            attestation_ref=str(ev.attestation_ref),
            rules_version=RULES_VERSION,
        )
        print(
            f'{{"status":"ok","rule_id":"{ev.rule_id}",'
            f'"event_id":"{ev.event_id}","contract_id":"{contract_id}",'
            f'"as_of":"{as_of.isoformat()}","events_inserted":{len(r001_events)}}}'
        )
        return 0
    finally:
        clear_trace_id()


if __name__ == "__main__":
    raise SystemExit(main())
