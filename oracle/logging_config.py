"""Structlog configuration for the Oracle.

Single canonical schema for every log line emitted by the Oracle:

============  ===========================================================
Field         Meaning
============  ===========================================================
timestamp     ISO-8601 UTC timestamp (added automatically).
component     Subsystem name — e.g. ``"scheduler"``, ``"rules.engine"``,
              ``"collectors.ecb"``. Bound once via :func:`get_logger`.
action        What is being attempted — e.g. ``"collect"``,
              ``"evaluate_rule"``, ``"verify_integrity"``. Set per call.
outcome       Result of the action — typically ``"ok"``, ``"failure"``,
              ``"indeterminate"``, ``"skipped"``.
duration_ms   Wall-clock duration of the action in milliseconds.
trace_id      Correlation id for an Oracle cycle. Bound by the scheduler
              at the start of a daily run; absent on standalone events.
============  ===========================================================

Other fields may be present (rule_id, metric, attestation_id, etc.) but
the six above are the contract: every observability consumer can expect
them to be parseable. The renderer is pure JSON — one line per event,
sorted keys, no colours, safe for SIEM / Loki / Cloud Logging ingestion.

Usage
-----

::

    from oracle.logging_config import configure_logging, get_logger

    configure_logging()  # call once at process start
    log = get_logger("scheduler")
    log.info("daily_cycle_complete", action="run_daily_cycle",
             outcome="ok", duration_ms=1234, trace_id=str(uuid4()))
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


_CONFIGURED: bool = False


# ---------------------------------------------------------------------------
# Custom processors
# ---------------------------------------------------------------------------


def _ensure_canonical_keys(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Guarantee the canonical schema fields appear in every event.

    Missing values render as JSON ``null`` so downstream tooling can index
    the column without per-event existence checks. ``component`` is
    expected to be bound on the logger; if a call site forgets, we fall
    back to the literal ``"unknown"`` rather than crashing.
    """

    event_dict.setdefault("component", "unknown")
    event_dict.setdefault("action", None)
    event_dict.setdefault("outcome", None)
    event_dict.setdefault("duration_ms", None)
    event_dict.setdefault("trace_id", None)
    return event_dict


def _rename_event_to_message(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Rename ``event`` (structlog default) to ``message`` for log readers.

    structlog calls the positional first arg ``event``; SIEMs typically
    look for ``message`` or ``msg``. Keep this stable so dashboards don't
    need rewrites.
    """

    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib logging + structlog to render canonical JSON lines.

    Idempotent: safe to call from multiple entry points (CLI, dashboard,
    tests). Subsequent calls are no-ops so test suites don't double-wrap
    the processor chain.
    """

    global _CONFIGURED
    if _CONFIGURED:
        return

    # Stdlib root logger writes plain messages to stderr; structlog adds
    # the structure on top. Going through stdlib means third-party
    # libraries (httpx, asyncio) inherit the same handler.
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(
                fmt="iso", utc=True, key="timestamp"
            ),
            _ensure_canonical_keys,
            _rename_event_to_message,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    """Return a logger pre-bound with ``component=<component>``.

    Calls :func:`configure_logging` (idempotent) so that callers binding
    a logger at module-import time still pick up the canonical JSON
    chain. Without this, ``BoundLoggerLazyProxy.bind()`` materialises
    the bound logger using whichever structlog config is live at import
    time — that is structlog's default console renderer, not ours.
    """

    configure_logging()
    return structlog.get_logger().bind(component=component)


def bind_trace_id(trace_id: str) -> None:
    """Bind ``trace_id`` to the current context so subsequent log calls
    in the same task / coroutine carry it automatically.

    Call once at the start of a unit of work (e.g. a daily Oracle cycle);
    use :func:`clear_trace_id` to release it.
    """

    structlog.contextvars.bind_contextvars(trace_id=trace_id)


def clear_trace_id() -> None:
    """Remove ``trace_id`` from the contextvars binding."""

    structlog.contextvars.unbind_contextvars("trace_id")
