"""Daily Oracle cycle — the CLI that runs the full pipeline end-to-end.

Responsibilities (in order):

1. Discover which metrics the configured collector should pull and collect
   each one. Two source modes are supported:

   * ``--fixture PATH``  — :class:`FakeCollector` reads from a YAML
     fixture (offline, deterministic, used in tests and demos).
   * ``--live-ecb``      — :class:`ECBCollector` fetches real €STR from
     the ECB Statistical Data Warehouse. V1 publishes only ESTR live;
     EURIBOR live fetches are deferred to a later phase.

   The two modes are mutually exclusive at the CLI; exactly one must be
   provided.
2. Build a :class:`MarketState` keyed by the successfully collected metrics.
3. Sign the collected datapoints into an :class:`OracleAttestation` chained
   on top of the previous attestation (or genesis if none) and persist it
   via :class:`AttestationStore`.
4. Fetch the contract snapshot via :class:`IRSBridge`.
5. Run :class:`RuleEngine.evaluate`.
6. For each :class:`TriggerEvent`: submit it through the bridge **and**
   persist it to ``trigger_events`` for the Oracle's own audit.
7. Emit a JSON summary line to stdout (the "daily log record" the scheduler
   would ship to a SIEM / observability pipeline).

The top-level coroutine :func:`run_daily_cycle` is async so it composes
cleanly with :meth:`BaseCollector.collect`. The CLI in :func:`main` drives
it via :func:`asyncio.run`.

I5 (no fallback) — in ``--live-ecb`` mode, if the upstream fetch fails the
SourceFailure is persisted by the pipeline and the CLI exits non-zero
**without** publishing a partial or substituted attestation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

from oracle.collectors.base import BaseCollector
from oracle.collectors.ecb import ECBCollector
from oracle.collectors.fake import FakeCollector
from oracle.config import (
    ORACLE_VERSION,
    RULES_VERSION,
    Metric,
)
from oracle.core.attestation import build_attestation
from oracle.core.store import AttestationStore
from oracle.integration.irs_bridge import IRSBridge
from oracle.logging_config import (
    bind_trace_id,
    clear_trace_id,
    configure_logging,
    get_logger,
)
from oracle.rules.engine import RuleEngine
from oracle.types import (
    MarketState,
    NormalizedDatapoint,
    OracleAttestation,
    Rule,
    SourceFailure,
)


_log = get_logger("scheduler")


# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyCycleResult:
    attestations_created: int
    triggers_emitted: int
    source_failures: int
    indeterminate_rules: int
    attestation_id: UUID | None
    trigger_event_ids: tuple[UUID, ...]
    collected_metrics: tuple[Metric, ...]
    missing_metrics: tuple[Metric, ...]
    as_of: date

    def to_json(self) -> str:
        return json.dumps(
            {
                "attestations_created": self.attestations_created,
                "triggers_emitted": self.triggers_emitted,
                "source_failures": self.source_failures,
                "indeterminate_rules": self.indeterminate_rules,
                "attestation_id": (
                    str(self.attestation_id) if self.attestation_id else None
                ),
                "trigger_event_ids": [str(eid) for eid in self.trigger_event_ids],
                "collected_metrics": [m.value for m in self.collected_metrics],
                "missing_metrics": [m.value for m in self.missing_metrics],
                "as_of": self.as_of.isoformat(),
            },
            sort_keys=True,
        )


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


async def run_daily_cycle(
    *,
    collector: BaseCollector,
    store: AttestationStore,
    bridge: IRSBridge,
    rules: Sequence[Rule],
    contract_id: str,
    metrics: Iterable[Metric],
    as_of: date,
) -> DailyCycleResult:
    """One end-to-end Oracle cycle. Pure of the clock only via ``as_of``."""

    metrics_tuple = tuple(metrics)
    failures: list[SourceFailure] = []

    # Re-bind the collector's failure sink so this cycle captures only its own
    # failures (not whatever the collector had wired before it was passed in).
    collector._failure_callback = failures.append  # type: ignore[attr-defined]

    collected: dict[Metric, NormalizedDatapoint] = {}
    for metric in metrics_tuple:
        datapoint = await collector.collect(metric, as_of)
        if datapoint is not None:
            collected[metric] = datapoint

    # Attestation — sign only non-empty batches. Zero datapoints means the
    # whole fixture failed; publishing an empty attestation would be a lie.
    attestation: OracleAttestation | None = None
    if collected:
        previous = store.get_latest_attestation()
        attestation = build_attestation(
            datapoints=tuple(
                collected[m] for m in sorted(collected, key=lambda x: x.value)
            ),
            signed_at=datetime.now(timezone.utc),
            rules_version=RULES_VERSION,
            oracle_version=ORACLE_VERSION,
            previous_attestation=previous,
        )
        store.append(attestation)

    market = _build_market_state(collected, attestation)
    snapshot = bridge.fetch_contract_state(contract_id)

    engine = RuleEngine(rules)
    events = engine.evaluate(market, snapshot, as_of)

    for event in events:
        bridge.submit_trigger_event(event)
        store.record_trigger(event)

    indeterminate = sum(
        1
        for rule in rules
        if rule.required_metrics - frozenset(collected.keys())
    )

    for failure in failures:
        store.record_failure(failure)

    return DailyCycleResult(
        attestations_created=(1 if attestation is not None else 0),
        triggers_emitted=len(events),
        source_failures=len(failures),
        indeterminate_rules=indeterminate,
        attestation_id=attestation.attestation_id if attestation else None,
        trigger_event_ids=tuple(e.event_id for e in events),
        collected_metrics=tuple(sorted(collected.keys(), key=lambda m: m.value)),
        missing_metrics=tuple(
            sorted(
                frozenset(metrics_tuple) - frozenset(collected.keys()),
                key=lambda m: m.value,
            )
        ),
        as_of=as_of,
    )


def _build_market_state(
    collected: dict[Metric, NormalizedDatapoint],
    attestation: OracleAttestation | None,
) -> MarketState:
    attestation_refs: dict[Metric, UUID] = {}
    if attestation is not None:
        for dp in attestation.datapoints:
            attestation_refs[dp.metric] = attestation.attestation_id

    return MarketState(
        built_at=datetime.now(timezone.utc),
        latest=dict(collected),
        attestation_refs=attestation_refs,
        missing=frozenset(),
        missing_consecutive_days={},
    )


# ---------------------------------------------------------------------------
# Fixture inspection
# ---------------------------------------------------------------------------


def discover_metrics_in_fixture(fixture_path: Path) -> frozenset[Metric]:
    """Peek at the fixture to find the metrics it declares.

    Avoids spamming ``parse_error`` SourceFailures for metrics the fixture
    was never meant to provide.
    """

    with fixture_path.open("r", encoding="utf-8") as fp:
        loaded = yaml.safe_load(fp)

    if not isinstance(loaded, dict) or "datapoints" not in loaded:
        raise ValueError(
            f"fixture {fixture_path} must be a mapping with a 'datapoints' key"
        )

    metrics: set[Metric] = set()
    for row in loaded["datapoints"]:
        if not isinstance(row, dict):
            continue
        raw = row.get("metric")
        try:
            metrics.add(Metric(str(raw)))
        except ValueError:
            _log.warning(
                "fixture_unknown_metric",
                action="discover_metrics",
                outcome="skipped",
                fixture_path=str(fixture_path),
                raw_metric=str(raw),
            )
    return frozenset(metrics)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _ensure_project_root_on_syspath() -> None:
    """Add project root to ``sys.path`` if invoked as a bare script.

    ``from backend.engine import ...`` needs the project root on the path
    (``backend/`` is a namespace package; its parent must be importable).
    """

    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="oracle-daily-run",
        description=(
            "Run one end-to-end Oracle cycle. Source mode is mutually "
            "exclusive: either --fixture (offline, FakeCollector) or "
            "--live-ecb (real ECB SDW fetch, V1 = €STR only)."
        ),
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--fixture", type=Path, default=None,
        help="Path to a YAML fixture (FakeCollector format). Offline mode.",
    )
    source_group.add_argument(
        "--live-ecb", action="store_true", default=False,
        help=(
            "Fetch real €STR from the ECB Statistical Data Warehouse. "
            "EURIBOR live fetch is deferred to a later phase."
        ),
    )
    parser.add_argument(
        "--contract-id", required=True, type=str,
        help="Contract identifier to evaluate.",
    )
    parser.add_argument(
        "--db-path", required=True, type=Path,
        help="Path to the SQLite attestation store (created if missing).",
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, default=None,
        help="Evaluation date (ISO 8601). Defaults to today (UTC).",
    )
    return parser.parse_args(argv)


def _build_demo_engine(contract_id: str) -> Any:
    """Minimal in-memory engine for the CLI demo path.

    Constructs a default ``SwapParameters`` and generates the period schedule
    directly, skipping ``IRSExecutionEngine.initialise()`` whose netting
    check and banner prints aren't useful in a scheduler cycle.
    """

    _ensure_project_root_on_syspath()
    from backend.engine import IRSExecutionEngine, SwapParameters

    params = SwapParameters(contract_id=contract_id)
    engine = IRSExecutionEngine(params)
    engine.periods = engine.schedule_gen.generate()
    return engine


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = _parse_args(argv)
    as_of = args.as_of or datetime.now(timezone.utc).date()
    live_mode = bool(args.live_ecb)

    trace_id = str(uuid4())
    bind_trace_id(trace_id)
    started = time.perf_counter()
    _log.info(
        "daily_cycle_start",
        action="run_daily_cycle",
        outcome="started",
        mode="live_ecb" if live_mode else "fixture",
        as_of=as_of.isoformat(),
        contract_id=args.contract_id,
        fixture_path=str(args.fixture) if args.fixture else None,
        db_path=str(args.db_path),
    )

    try:
        engine = _build_demo_engine(args.contract_id)
        engines_by_id: dict[str, Any] = {args.contract_id: engine}

        bridge = IRSBridge(resolver=lambda cid: engines_by_id[cid])

        import oracle.rules.impl.r001_failure_to_pay  # noqa: F401 — registers R-001
        from oracle.rules.registry import get_all_rules

        rules = get_all_rules()

        collector: BaseCollector
        metrics: frozenset[Metric]
        if live_mode:
            # V1: ESTR only. EURIBOR live fetch is deferred (Phase 7b).
            collector = ECBCollector()
            metrics = frozenset({Metric.ESTR})
        else:
            metrics = discover_metrics_in_fixture(args.fixture)
            collector = FakeCollector(args.fixture)
        store = AttestationStore(args.db_path)

        result = asyncio.run(
            run_daily_cycle(
                collector=collector,
                store=store,
                bridge=bridge,
                rules=rules,
                contract_id=args.contract_id,
                metrics=metrics,
                as_of=as_of,
            )
        )

        duration_ms = int((time.perf_counter() - started) * 1000)
        _log.info(
            "daily_cycle_complete",
            action="run_daily_cycle",
            outcome="ok" if result.attestations_created > 0 else "no_attestation",
            duration_ms=duration_ms,
            attestations_created=result.attestations_created,
            triggers_emitted=result.triggers_emitted,
            source_failures=result.source_failures,
            indeterminate_rules=result.indeterminate_rules,
        )

        # The single user-facing summary line — see Phase 8 §1: this
        # ``print`` is the only one allowed in the entire codebase.
        # Operators tail this on stdout; structured logs go to stderr.
        print(result.to_json())

        # I5 — in live mode, the upstream feed failing means we publish
        # nothing and exit non-zero. The fixture path keeps its prior exit
        # code (always 0) for backward compatibility.
        if live_mode and result.attestations_created == 0:
            _log.error(
                "daily_cycle_no_attestation",
                action="run_daily_cycle",
                outcome="failure",
                source_failures=result.source_failures,
                missing_metrics=[m.value for m in result.missing_metrics],
            )
            return 1
        return 0
    finally:
        clear_trace_id()


if __name__ == "__main__":
    raise SystemExit(main())
