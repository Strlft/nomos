"""ECB SDW collector for €STR (Euro Short-Term Rate) and EURIBOR fixings.

The ECB SDMX-JSON 2.0 API returns a percentage value (e.g. ``1.933``)
which the collector divides by 100 to produce a ``DECIMAL_FRACTION``
ready for the sanity band check.

Series mapping
--------------

* :attr:`Metric.ESTR` → ``EST/B.EU000A2X2A25.WT`` (daily, business week,
  volume-weighted trimmed mean).
* :attr:`Metric.EURIBOR_3M` → ``FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA``
  (monthly, Euro area, EURIBOR 3-month).
* :attr:`Metric.EURIBOR_6M` → ``FM/M.U2.EUR.RT.MM.EURIBOR6MD_.HSTA``.
* :attr:`Metric.EURIBOR_12M` → ``FM/M.U2.EUR.RT.MM.EURIBOR1YD_.HSTA``.

Why ECB for EURIBOR rather than BdF or FRED: BdF stopped publishing
EURIBOR on 2024-07-10 and FRED's daily IBA series were discontinued on
2022-01-31. ECB SDW publishes EURIBOR at monthly frequency only — that
is the only free public source that still works. See
``oracle/config.py`` docstring for the full audit trail.

All four endpoints were verified live against:

    https://data-api.ecb.europa.eu/service/data/<DATAFLOW>/<KEY>
    ?lastNObservations=1&format=jsondata

on 2026-04-26 (HTTP 200).

Retry/timeout policy (ARCH §7)
------------------------------
Connect timeout 5s, read timeout 10s, three attempts with exponential
backoff (1s → 2s → 4s) and ±25% jitter. The retry loop lives in
:class:`BaseCollector`; this class just maps HTTP errors to the right
exception types so the loop does the right thing:

* HTTP 5xx / 429        → :class:`CollectorUnavailableError` (retried)
* ``httpx.TimeoutException`` / ``httpx.ConnectError`` propagate (retried)
* HTTP 4xx (≠ 429)      → :class:`CollectorDataError` (no retry)
* JSON parse failure    → :class:`CollectorDataError` (no retry)
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from oracle.collectors.base import BaseCollector, FailureCallback
from oracle.config import SOURCE_ID_ECB, Metric, Unit
from oracle.errors import CollectorDataError, CollectorUnavailableError
from oracle.types import RawDatapoint


_BASE_URL: str = "https://data-api.ecb.europa.eu/service/data"


# Per-metric (dataflow, series_key) mapping. Adding a new metric is a
# one-line change here plus a ``SOURCE_METRICS`` entry in config.py.
_METRIC_SERIES: dict[Metric, tuple[str, str]] = {
    Metric.ESTR: ("EST", "B.EU000A2X2A25.WT"),
    Metric.EURIBOR_3M: ("FM", "M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA"),
    Metric.EURIBOR_6M: ("FM", "M.U2.EUR.RT.MM.EURIBOR6MD_.HSTA"),
    Metric.EURIBOR_12M: ("FM", "M.U2.EUR.RT.MM.EURIBOR1YD_.HSTA"),
}


# Connect timeout 5s, read timeout 10s — ARCH §7.
_CONNECT_TIMEOUT_SECONDS: float = 5.0
_READ_TIMEOUT_SECONDS: float = 10.0


def _build_url(metric: Metric) -> str:
    dataflow, series_key = _METRIC_SERIES[metric]
    return f"{_BASE_URL}/{dataflow}/{series_key}"


class ECBCollector(BaseCollector):
    """Fetch ESTR and EURIBOR fixings from the ECB SDW SDMX-JSON 2.0 API."""

    source_id: str = SOURCE_ID_ECB

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        failure_callback: FailureCallback | None = None,
    ) -> None:
        super().__init__(failure_callback=failure_callback)
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------
    # Abstract overrides
    # ------------------------------------------------------------------

    async def fetch(self, metric: Metric, as_of: date) -> RawDatapoint:
        if metric not in _METRIC_SERIES:
            raise CollectorDataError(
                f"ECBCollector has no series mapping for metric={metric.value}"
            )

        url = _build_url(metric)
        params = {"lastNObservations": "1", "format": "jsondata"}
        headers = {"Accept": "application/json"}

        client = self._client
        owns = False
        if client is None:
            timeout = httpx.Timeout(
                connect=_CONNECT_TIMEOUT_SECONDS,
                read=_READ_TIMEOUT_SECONDS,
                write=_READ_TIMEOUT_SECONDS,
                pool=_CONNECT_TIMEOUT_SECONDS,
            )
            client = httpx.AsyncClient(timeout=timeout)
            owns = True

        try:
            response = await client.get(url, params=params, headers=headers)
        finally:
            if owns:
                await client.aclose()

        # Map HTTP status to retry / no-retry exceptions per ARCH §7.
        status = response.status_code
        if status >= 500 or status == 429:
            raise CollectorUnavailableError(
                f"ECB SDW returned HTTP {status} for {response.url}"
            )
        if status >= 400:
            raise CollectorDataError(
                f"ECB SDW returned HTTP {status} for {response.url} — "
                f"will not retry"
            )

        body_bytes = response.content
        body_text = body_bytes.decode("utf-8")
        source_hash = hashlib.sha256(body_bytes).hexdigest()

        # Pre-parse to extract the source-reported as-of date and to fail
        # fast on malformed payloads (matches the spec: parse errors are
        # not retried).
        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise CollectorDataError(
                f"ECB SDW response is not valid JSON: {exc}"
            ) from exc

        source_reported = _extract_observation_date(parsed)

        return RawDatapoint(
            source_id=self.source_id,
            metric=metric.value,
            raw_payload=body_text,
            source_hash=source_hash,
            fetched_at=datetime.now(timezone.utc),
            source_url=str(response.url),
            source_reported_as_of=source_reported,
        )

    def parse(self, raw: RawDatapoint) -> Decimal:
        try:
            payload: Any = json.loads(raw.raw_payload)
        except json.JSONDecodeError as exc:
            raise CollectorDataError(
                f"ECB SDW raw_payload is not valid JSON: {exc}"
            ) from exc

        raw_value = _extract_observation_value(payload)
        try:
            percent = Decimal(str(raw_value))
        except InvalidOperation as exc:
            raise CollectorDataError(
                f"ECB SDW observation value {raw_value!r} is not a Decimal"
            ) from exc

        # ECB publishes both €STR and EURIBOR as percentages; the Oracle
        # stores DECIMAL_FRACTION. Decimal division so we never round-trip
        # through float.
        return percent / Decimal("100")

    def unit_for(self, metric: Metric) -> Unit:
        return Unit.DECIMAL_FRACTION


# ---------------------------------------------------------------------------
# SDMX-JSON helpers
# ---------------------------------------------------------------------------


def _extract_observation_value(payload: Any) -> Any:
    """Pull the latest observation value out of an SDMX-JSON document.

    ECB returns ``dataSets[0].series[<series-key>].observations[<obs-idx>]``
    as a list ``[value, status, ...]``. With ``lastNObservations=1`` there
    is exactly one series and one observation; we still iterate
    defensively in case the API ever returns multiple. The series-key
    arity differs between dataflows (EST uses 3 dimensions, FM uses 7),
    but ``next(iter(...))`` is arity-agnostic.
    """

    if not isinstance(payload, dict):
        raise CollectorDataError(
            "ECB SDW payload is not a JSON object at the top level"
        )

    datasets = payload.get("dataSets")
    if not isinstance(datasets, list) or not datasets:
        raise CollectorDataError(
            "ECB SDW payload has no 'dataSets' array"
        )

    series_map = datasets[0].get("series") if isinstance(datasets[0], dict) else None
    if not isinstance(series_map, dict) or not series_map:
        raise CollectorDataError(
            "ECB SDW payload has no series in dataSets[0]"
        )

    # Pick the only (or first) series — with one series key in the URL
    # there's at most one entry.
    _, series = next(iter(series_map.items()))
    if not isinstance(series, dict):
        raise CollectorDataError(
            "ECB SDW series entry is not a JSON object"
        )

    observations = series.get("observations")
    if not isinstance(observations, dict) or not observations:
        raise CollectorDataError(
            "ECB SDW series has no 'observations'"
        )

    # Observation keys are stringified ints; pick the largest as the most
    # recent observation in the series.
    last_key = max(observations.keys(), key=lambda k: int(k))
    obs = observations[last_key]
    if not isinstance(obs, list) or not obs:
        raise CollectorDataError(
            f"ECB SDW observation {last_key!r} is empty"
        )
    return obs[0]


def _extract_observation_date(payload: Any) -> str | None:
    """Return ``structure.dimensions.observation[0].values[<last>].id``.

    For ESTR (daily) this is an ISO-8601 calendar date like ``2026-04-23``.
    For EURIBOR (monthly) it's an ISO year-month like ``2026-03``, which
    ``BaseCollector._parse_source_as_of`` will treat as a parse error
    because ``date.fromisoformat`` rejects ``YYYY-MM``. We patch that up
    here by promoting bare year-month strings to the first-of-month so
    the as_of is a valid date.
    """

    if not isinstance(payload, dict):
        return None
    structure = payload.get("structure")
    if not isinstance(structure, dict):
        return None
    dimensions = structure.get("dimensions")
    if not isinstance(dimensions, dict):
        return None
    obs_dims = dimensions.get("observation")
    if not isinstance(obs_dims, list) or not obs_dims:
        return None
    first_obs_dim = obs_dims[0]
    if not isinstance(first_obs_dim, dict):
        return None
    values = first_obs_dim.get("values")
    if not isinstance(values, list) or not values:
        return None
    last = values[-1]
    if not isinstance(last, dict):
        return None
    obs_id = last.get("id")
    if obs_id is None:
        return None
    obs_id_str = str(obs_id)
    # Year-month → first-of-month so date.fromisoformat() accepts it.
    if len(obs_id_str) == 7 and obs_id_str[4] == "-":
        return f"{obs_id_str}-01"
    return obs_id_str
