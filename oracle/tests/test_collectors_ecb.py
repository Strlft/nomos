"""Phase 7a tests for :class:`ECBCollector`.

Covers the four scenarios pinned in the Phase 7 prompt:

* Happy path against a captured real ECB SDMX-JSON payload (mocked with
  ``respx``) — the parsed value must be a :class:`~decimal.Decimal`,
  not a ``float``, and equal to ``raw_percent / 100``.
* HTTP 503 retried up to three times then surfaced as
  ``failure_kind="http_5xx"`` via the failure callback.
* HTTP 400 fails after exactly one attempt — no retry observed.
* Malformed JSON body → ``failure_kind="parse_error"`` after exactly one
  attempt.

Plus an ``@pytest.mark.integration`` test that hits the real ECB SDW
endpoint and asserts the value is inside the ESTR sanity band. Skipped
in CI by default; opt in with ``pytest -m integration``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from oracle.collectors.ecb import ECBCollector
from oracle.config import Metric, Unit
from oracle.errors import CollectorDataError
from oracle.types import SourceFailure


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ecb"
_FIXTURE_PATH = _FIXTURE_DIR / "estr_sample.json"

_ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT"
)
_ECB_EURIBOR_URLS: dict[Metric, str] = {
    Metric.EURIBOR_3M: (
        "https://data-api.ecb.europa.eu/service/data/FM/"
        "M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA"
    ),
    Metric.EURIBOR_6M: (
        "https://data-api.ecb.europa.eu/service/data/FM/"
        "M.U2.EUR.RT.MM.EURIBOR6MD_.HSTA"
    ),
    Metric.EURIBOR_12M: (
        "https://data-api.ecb.europa.eu/service/data/FM/"
        "M.U2.EUR.RT.MM.EURIBOR1YD_.HSTA"
    ),
}
_EURIBOR_FIXTURES: dict[Metric, Path] = {
    Metric.EURIBOR_3M: _FIXTURE_DIR / "euribor_3m_sample.json",
    Metric.EURIBOR_6M: _FIXTURE_DIR / "euribor_6m_sample.json",
    Metric.EURIBOR_12M: _FIXTURE_DIR / "euribor_12m_sample.json",
}


@pytest.fixture
def captured_payload() -> str:
    """Real ECB SDW response captured live on 2026-04-26."""

    return _FIXTURE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def zero_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip retry backoff in unit tests so the suite stays fast."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    # The collector is constructed with the default asyncio.sleep; replace
    # the module-level reference used inside BaseCollector._fetch_with_retry.
    import oracle.collectors.base as base

    monkeypatch.setattr(base.asyncio, "sleep", _no_sleep)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @respx.mock
    async def test_returns_decimal_fraction_from_real_payload(
        self, captured_payload: str
    ) -> None:
        respx.get(_ECB_URL).mock(
            return_value=httpx.Response(200, text=captured_payload)
        )

        collector = ECBCollector()
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is not None
        assert result.metric is Metric.ESTR
        assert result.unit is Unit.DECIMAL_FRACTION
        assert result.source_id == "ecb_sdw_v1"
        assert result.sanity_band_passed is True
        assert result.cross_validated is False

        # The fixture observation is 1.933 (a percentage). The collector
        # must divide by 100 and return a Decimal — not a float.
        assert isinstance(result.value, Decimal)
        assert not isinstance(result.value, float)
        assert result.value == Decimal("1.933") / Decimal("100")

    @respx.mock
    async def test_source_reported_as_of_extracted_from_payload(
        self, captured_payload: str
    ) -> None:
        respx.get(_ECB_URL).mock(
            return_value=httpx.Response(200, text=captured_payload)
        )

        collector = ECBCollector()
        result = await collector.collect(Metric.ESTR, date(2026, 4, 25))

        assert result is not None
        # The fixture's last observation is 2026-04-23. The collector
        # must surface that, not silently fall back to the caller's as_of.
        assert result.as_of == date(2026, 4, 23)

    @respx.mock
    async def test_parse_directly_returns_decimal(
        self, captured_payload: str
    ) -> None:
        # Bypass collect() entirely so we exercise parse() in isolation —
        # this is the assertion the spec asks us to pin explicitly.
        respx.get(_ECB_URL).mock(
            return_value=httpx.Response(200, text=captured_payload)
        )
        collector = ECBCollector()
        raw = await collector.fetch(Metric.ESTR, date(2026, 4, 23))
        value = collector.parse(raw)

        assert isinstance(value, Decimal)
        assert not isinstance(value, float)
        assert value == Decimal("0.01933")


# ---------------------------------------------------------------------------
# HTTP 503 → retried 3 times → http_5xx failure
# ---------------------------------------------------------------------------


class TestServerError:
    @respx.mock
    async def test_503_is_retried_then_emits_http_5xx_failure(
        self, zero_sleep: None
    ) -> None:
        route = respx.get(_ECB_URL).mock(
            return_value=httpx.Response(503, text="busy")
        )

        captured: list[SourceFailure] = []
        collector = ECBCollector(failure_callback=captured.append)
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is None
        assert route.call_count == 3, (
            f"expected 3 attempts on 503, got {route.call_count}"
        )
        assert len(captured) == 1
        failure = captured[0]
        assert failure.failure_kind == "http_5xx"
        assert failure.attempts == 3
        assert failure.source_id == "ecb_sdw_v1"
        assert failure.metric is Metric.ESTR

    @respx.mock
    async def test_429_is_also_retried_three_times(
        self, zero_sleep: None
    ) -> None:
        # Per ARCH §7, 429 is treated as transient and retried.
        route = respx.get(_ECB_URL).mock(
            return_value=httpx.Response(429, text="too many requests")
        )
        captured: list[SourceFailure] = []
        collector = ECBCollector(failure_callback=captured.append)
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is None
        assert route.call_count == 3
        assert captured and captured[0].failure_kind == "http_5xx"


# ---------------------------------------------------------------------------
# HTTP 400 → one attempt only, parse_error failure
# ---------------------------------------------------------------------------


class TestClientError:
    @respx.mock
    async def test_400_is_not_retried(self, zero_sleep: None) -> None:
        route = respx.get(_ECB_URL).mock(
            return_value=httpx.Response(400, text="bad request")
        )
        captured: list[SourceFailure] = []
        collector = ECBCollector(failure_callback=captured.append)
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is None
        assert route.call_count == 1, (
            f"4xx must not retry; saw {route.call_count} attempts"
        )
        assert len(captured) == 1
        failure = captured[0]
        assert failure.failure_kind == "parse_error"
        assert failure.attempts == 1

    @respx.mock
    async def test_404_is_not_retried(self, zero_sleep: None) -> None:
        route = respx.get(_ECB_URL).mock(
            return_value=httpx.Response(404, text="not found")
        )
        collector = ECBCollector()
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is None
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# Malformed JSON → CollectorDataError → parse_error
# ---------------------------------------------------------------------------


class TestMalformedPayload:
    @respx.mock
    async def test_invalid_json_via_collect_records_parse_error(
        self, zero_sleep: None
    ) -> None:
        route = respx.get(_ECB_URL).mock(
            return_value=httpx.Response(200, text="{ not valid json")
        )
        captured: list[SourceFailure] = []
        collector = ECBCollector(failure_callback=captured.append)
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is None
        assert route.call_count == 1, "parse failures must not retry"
        assert len(captured) == 1
        assert captured[0].failure_kind == "parse_error"

    @respx.mock
    async def test_invalid_json_via_fetch_raises_collector_data_error(
        self,
    ) -> None:
        respx.get(_ECB_URL).mock(
            return_value=httpx.Response(200, text="{ not valid json")
        )
        collector = ECBCollector()
        with pytest.raises(CollectorDataError):
            await collector.fetch(Metric.ESTR, date(2026, 4, 23))

    @respx.mock
    async def test_payload_with_no_datasets_raises_in_parse(self) -> None:
        # JSON-valid but missing the structure the parser expects.
        respx.get(_ECB_URL).mock(
            return_value=httpx.Response(200, text='{"header": {}}')
        )
        collector = ECBCollector()
        with pytest.raises(CollectorDataError, match="dataSets"):
            raw = await collector.fetch(Metric.ESTR, date(2026, 4, 23))
            collector.parse(raw)


# ---------------------------------------------------------------------------
# Wrong metric — programmer error, not a SourceFailure
# ---------------------------------------------------------------------------


class TestUnmappedMetric:
    """ECBCollector now serves all four metrics; unmapped metrics still raise.

    We can't easily express an unmapped metric without a runtime hack —
    Metric is a str-enum and every variant is in the mapping. The check
    is structural, so adding a new Metric without updating the mapping
    will be caught here once such a metric is introduced.
    """

    async def test_all_declared_metrics_are_mapped(self) -> None:
        from oracle.collectors.ecb import _METRIC_SERIES

        for m in Metric:
            assert m in _METRIC_SERIES, (
                f"ECBCollector mapping is incomplete: {m} has no series key"
            )


# ---------------------------------------------------------------------------
# EURIBOR — all three tenors
# ---------------------------------------------------------------------------


class TestEuribor:
    @pytest.mark.parametrize(
        "metric",
        [Metric.EURIBOR_3M, Metric.EURIBOR_6M, Metric.EURIBOR_12M],
    )
    @respx.mock
    async def test_each_tenor_returns_decimal_fraction(
        self, metric: Metric
    ) -> None:
        payload = _EURIBOR_FIXTURES[metric].read_text(encoding="utf-8")
        respx.get(_ECB_EURIBOR_URLS[metric]).mock(
            return_value=httpx.Response(200, text=payload)
        )

        collector = ECBCollector()
        result = await collector.collect(metric, date(2026, 4, 23))

        assert result is not None, f"{metric} returned no result"
        assert result.metric is metric
        assert result.unit is Unit.DECIMAL_FRACTION
        assert result.source_id == "ecb_sdw_v1"
        assert result.sanity_band_passed is True

        # ECB returns percentages; collector divides by 100. Value must
        # be a Decimal in the EURIBOR sanity band [-0.02, 0.20].
        assert isinstance(result.value, Decimal)
        assert not isinstance(result.value, float)
        assert Decimal("-0.02") <= result.value <= Decimal("0.20")

    @respx.mock
    async def test_year_month_as_of_normalized_to_first_of_month(self) -> None:
        # EURIBOR observation dimension uses YYYY-MM (e.g. "2026-03").
        # The collector promotes this to YYYY-MM-01 so date.fromisoformat
        # accepts it. Without that handling the collect() call would fail
        # the source_reported_as_of parse step.
        payload = _EURIBOR_FIXTURES[Metric.EURIBOR_3M].read_text(
            encoding="utf-8"
        )
        respx.get(_ECB_EURIBOR_URLS[Metric.EURIBOR_3M]).mock(
            return_value=httpx.Response(200, text=payload)
        )

        collector = ECBCollector()
        result = await collector.collect(
            Metric.EURIBOR_3M, date(2026, 4, 23)
        )

        assert result is not None
        # First-of-month invariant: day must be 1 if the source reported
        # year-month, otherwise the captured fixture's full date.
        assert result.as_of.day == 1
        # Sanity: the year-month is in the past relative to now.
        assert result.as_of.year >= 2020


# ---------------------------------------------------------------------------
# Live integration — all four ECB endpoints
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLiveECBEuribor:
    @pytest.mark.parametrize(
        "metric,band_max",
        [
            (Metric.EURIBOR_3M, Decimal("0.20")),
            (Metric.EURIBOR_6M, Decimal("0.20")),
            (Metric.EURIBOR_12M, Decimal("0.20")),
        ],
    )
    async def test_real_euribor_value_is_within_sanity_band(
        self, metric: Metric, band_max: Decimal
    ) -> None:
        collector = ECBCollector()
        result = await collector.collect(metric, date.today())

        assert result is not None, (
            f"real ECB FM endpoint returned no result for {metric} — "
            f"check connectivity or whether the dataflow has been renamed"
        )
        assert result.metric is metric
        assert result.unit is Unit.DECIMAL_FRACTION
        assert isinstance(result.value, Decimal)
        assert Decimal("-0.02") <= result.value <= band_max
        assert result.sanity_band_passed is True


# ---------------------------------------------------------------------------
# Integration — hits the real ECB endpoint. Skipped in CI by default.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLiveECB:
    async def test_real_estr_value_is_within_sanity_band(self) -> None:
        # Opt in with: pytest -m integration
        collector = ECBCollector()
        result = await collector.collect(Metric.ESTR, date.today())

        assert result is not None, (
            "real ECB SDW endpoint returned no result — "
            "check connectivity or whether the dataflow has been renamed"
        )
        assert result.metric is Metric.ESTR
        assert result.unit is Unit.DECIMAL_FRACTION
        assert isinstance(result.value, Decimal)
        # ESTR sanity band is [-0.02, 0.15] (DECIMAL_FRACTION).
        assert Decimal("-0.02") <= result.value <= Decimal("0.15")
        assert result.sanity_band_passed is True
