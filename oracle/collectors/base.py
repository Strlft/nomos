"""Abstract base class and retry/backoff policy for every Oracle collector.

Every concrete collector inherits from :class:`BaseCollector` and implements
three things:

* :attr:`source_id`  — the stable identifier used in configuration tables.
* :meth:`fetch`      — one attempt to retrieve the source-specific payload.
* :meth:`parse`      — extract a :class:`~decimal.Decimal` value from that
  payload.

The :meth:`collect` template method drives the full workflow: retry+backoff
around :meth:`fetch`, call :meth:`parse`, apply the sanity band, and either
return a :class:`NormalizedDatapoint` or emit a :class:`SourceFailure` via the
callback passed to ``__init__``.

Design note — why ``collect`` does not call :func:`oracle.core.normalizer.normalize`:
the Phase 2 normalizer parses a canonical ``{"value":..., "as_of":...}`` JSON
out of ``RawDatapoint.raw_payload``. Real-world collectors (ECB SDMX, BdF
Webstat, FRED) return source-specific payloads that would not satisfy that
contract. Rather than repackage every source into a fake canonical JSON (and
thereby lie about ``source_hash``), the template method uses :meth:`parse`
to extract the typed value and builds the :class:`NormalizedDatapoint`
directly, still routing the value through :func:`check_sanity_band`.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import httpx

from oracle.config import Metric, Unit
from oracle.core.sanity import check_sanity_band
from oracle.errors import (
    CollectorDataError,
    CollectorUnavailableError,
    SanityBandViolation,
)
from oracle.types import NormalizedDatapoint, RawDatapoint, SourceFailure


FailureCallback = Callable[[SourceFailure], None]


# ---------------------------------------------------------------------------
# Retry policy (ARCH §7)
# ---------------------------------------------------------------------------


_MAX_ATTEMPTS: int = 3
_BACKOFF_BASE_SECONDS: float = 1.0
_BACKOFF_FACTOR: float = 2.0
_JITTER_RANGE: tuple[float, float] = (0.75, 1.25)

# Exception types that trigger a retry. httpx.TimeoutException is a parent
# of ReadTimeout/ConnectTimeout so listing it covers both.
_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    CollectorUnavailableError,
)


_FailureKind = Literal[
    "timeout",
    "http_4xx",
    "http_5xx",
    "parse_error",
    "sanity_band_violation",
    "network_error",
    "cross_validation_failure",
]


def _backoff_seconds(attempt_idx: int) -> float:
    """Exponential backoff with ±25% jitter. ``attempt_idx`` is 0-based."""

    base = _BACKOFF_BASE_SECONDS * (_BACKOFF_FACTOR ** attempt_idx)
    return base * random.uniform(*_JITTER_RANGE)


# ---------------------------------------------------------------------------
# BaseCollector
# ---------------------------------------------------------------------------


class BaseCollector(ABC):
    """Shared skeleton for every source-specific collector."""

    # Subclasses set as a class-level attribute or override the property below.
    source_id: str

    def __init__(
        self,
        *,
        failure_callback: FailureCallback | None = None,
        sleep: Callable[[float], "asyncio.Future | None"] | None = None,
    ) -> None:
        """Parameters
        ----------
        failure_callback
            Called with a :class:`SourceFailure` whenever the collector gives
            up on a metric. ``None`` silences failure emission (useful in
            unit tests that only care about the return value).
        sleep
            Hook for tests to replace ``asyncio.sleep`` during retry backoff.
            Must be an async-callable accepting seconds.
        """

        self._failure_callback = failure_callback
        self._sleep = sleep or asyncio.sleep

    # ------------------------------------------------------------------
    # Abstract surface
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch(self, metric: Metric, as_of: date) -> RawDatapoint:
        """Make one attempt to retrieve ``metric`` for ``as_of``.

        Must raise:

        * :class:`httpx.TimeoutException` or :class:`httpx.ConnectError` on
          network failure — these trigger a retry.
        * :class:`CollectorUnavailableError` for HTTP 5xx / 429 — retry.
        * :class:`CollectorDataError` for malformed payloads or HTTP 4xx
          (except 429) — no retry.
        """

    @abstractmethod
    def parse(self, raw: RawDatapoint) -> Decimal:
        """Extract the :class:`~decimal.Decimal` value from ``raw``.

        Raise :class:`CollectorDataError` if the payload cannot be parsed.
        """

    def unit_for(self, metric: Metric) -> Unit:
        """Unit in which :meth:`parse` returns values.

        Override in subclasses whose source reports in a non-default unit.
        The default is :attr:`Unit.DECIMAL_FRACTION` — i.e. 3.75% arrives
        as ``Decimal("0.0375")``.
        """

        return Unit.DECIMAL_FRACTION

    # ------------------------------------------------------------------
    # Template method
    # ------------------------------------------------------------------

    async def collect(
        self, metric: Metric, as_of: date
    ) -> NormalizedDatapoint | None:
        """Fetch, parse, sanity-check, and return a :class:`NormalizedDatapoint`.

        Returns ``None`` and emits a :class:`SourceFailure` on any failure.
        Never raises for fetch/parse/sanity errors — callers get ``None``
        consistently and the failure is captured via the callback.

        Programmer errors (bad config, wrong metric passed, etc.) still raise.
        """

        try:
            raw, attempts = await self._fetch_with_retry(metric, as_of)
        except httpx.TimeoutException as exc:
            self._emit_failure(metric, "timeout", str(exc), "", _MAX_ATTEMPTS)
            return None
        except httpx.ConnectError as exc:
            self._emit_failure(metric, "network_error", str(exc), "", _MAX_ATTEMPTS)
            return None
        except CollectorUnavailableError as exc:
            self._emit_failure(metric, "http_5xx", str(exc), "", _MAX_ATTEMPTS)
            return None
        except CollectorDataError as exc:
            # Raised from fetch() (e.g. HTTP 4xx or unparseable response).
            self._emit_failure(metric, "parse_error", str(exc), "", 1)
            return None

        if raw.metric != metric.value:
            self._emit_failure(
                metric,
                "parse_error",
                f"collector returned metric={raw.metric!r}, expected {metric.value!r}",
                raw.source_url,
                attempts,
            )
            return None

        try:
            value = self.parse(raw)
        except CollectorDataError as exc:
            self._emit_failure(
                metric, "parse_error", str(exc), raw.source_url, attempts
            )
            return None

        unit = self.unit_for(metric)

        try:
            passed = check_sanity_band(metric, value, unit)
        except SanityBandViolation as exc:
            # Defensive — check_sanity_band returns a bool, but future
            # refactors might raise; handle both forms.
            self._emit_failure(
                metric,
                "sanity_band_violation",
                str(exc),
                raw.source_url,
                attempts,
            )
            return None

        if not passed:
            self._emit_failure(
                metric,
                "sanity_band_violation",
                f"value {value} ({unit.value}) outside band for {metric.value}",
                raw.source_url,
                attempts,
            )
            return None

        as_of_parsed = _parse_source_as_of(raw.source_reported_as_of, as_of)
        if as_of_parsed is None:
            self._emit_failure(
                metric,
                "parse_error",
                f"source_reported_as_of={raw.source_reported_as_of!r} is not ISO-8601",
                raw.source_url,
                attempts,
            )
            return None

        return NormalizedDatapoint(
            source_id=self.source_id,
            metric=metric,
            value=value,
            unit=unit,
            as_of=as_of_parsed,
            fetched_at=raw.fetched_at,
            source_hash=raw.source_hash,
            source_url=raw.source_url,
            sanity_band_passed=True,
            cross_validated=False,
            cross_checked_against=None,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_with_retry(
        self, metric: Metric, as_of: date
    ) -> tuple[RawDatapoint, int]:
        last_exc: BaseException | None = None
        for attempt_idx in range(_MAX_ATTEMPTS):
            try:
                raw = await self.fetch(metric, as_of)
                return raw, attempt_idx + 1
            except _RETRY_EXCEPTIONS as exc:
                last_exc = exc
                if attempt_idx < _MAX_ATTEMPTS - 1:
                    await self._sleep(_backoff_seconds(attempt_idx))
        assert last_exc is not None
        raise last_exc

    def _emit_failure(
        self,
        metric: Metric,
        failure_kind: _FailureKind,
        message: str,
        source_url: str,
        attempts: int,
    ) -> None:
        if self._failure_callback is None:
            return
        failure = SourceFailure(
            failure_id=uuid4(),
            source_id=self.source_id,
            metric=metric,
            attempted_at=datetime.now(timezone.utc),
            failure_kind=failure_kind,
            attempts=attempts,
            last_error_message=message[:2000],
            source_url=source_url,
            context={},
        )
        self._failure_callback(failure)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_source_as_of(
    source_reported_as_of: str | None, fallback: date
) -> date | None:
    """Parse the source's as_of string; fall back to the caller's if absent.

    Returns ``None`` if the source reported a non-ISO-8601 value — callers
    treat that as a parse error.
    """

    if source_reported_as_of is None:
        return fallback
    try:
        return date.fromisoformat(source_reported_as_of)
    except ValueError:
        return None
