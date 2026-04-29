"""Phase 3 tests for :class:`FakeCollector`.

Covers the five scenarios pinned by the Phase 3 prompt plus a handful of
adjacent cases that would trip up a future refactor (missing metric row,
wrong metric on re-read, callback silence when no callback provided).
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from oracle.collectors.fake import FakeCollector
from oracle.config import Metric, Unit
from oracle.errors import CollectorDataError
from oracle.types import SourceFailure


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body))
    return path


@pytest.fixture
def valid_fixture(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "valid.yaml",
        """\
        datapoints:
          - metric: ESTR
            value: "0.0375"
            unit: decimal_fraction
            as_of: "2026-04-23"
            source_reported_as_of: "2026-04-23"
          - metric: EURIBOR_3M
            value: "0.028"
            unit: decimal_fraction
            as_of: "2026-04-23"
            source_reported_as_of: "2026-04-23"
        """,
    )


@pytest.fixture
def out_of_band_fixture(tmp_path: Path) -> Path:
    # ESTR band is [-0.02, 0.15] — 0.99 is well above.
    return _write(
        tmp_path / "out_of_band.yaml",
        """\
        datapoints:
          - metric: ESTR
            value: "0.99"
            unit: decimal_fraction
            as_of: "2026-04-23"
        """,
    )


# ---------------------------------------------------------------------------
# Valid fixture → NormalizedDatapoint
# ---------------------------------------------------------------------------


class TestValidFixture:
    async def test_returns_normalized_with_correct_decimal(
        self, valid_fixture: Path
    ) -> None:
        collector = FakeCollector(valid_fixture)
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is not None
        assert result.value == Decimal("0.0375")
        assert result.metric is Metric.ESTR
        assert result.unit is Unit.DECIMAL_FRACTION
        assert result.source_id == "fake_v1"
        assert result.as_of == date(2026, 4, 23)
        assert result.sanity_band_passed is True
        assert result.cross_validated is False
        assert result.cross_checked_against is None

    async def test_multiple_metrics_each_retrievable(
        self, valid_fixture: Path
    ) -> None:
        collector = FakeCollector(valid_fixture)
        estr = await collector.collect(Metric.ESTR, date(2026, 4, 23))
        euri = await collector.collect(Metric.EURIBOR_3M, date(2026, 4, 23))

        assert estr is not None and estr.value == Decimal("0.0375")
        assert euri is not None and euri.value == Decimal("0.028")

    async def test_missing_metric_records_parse_error(
        self, valid_fixture: Path
    ) -> None:
        captured: list[SourceFailure] = []
        collector = FakeCollector(valid_fixture, failure_callback=captured.append)
        result = await collector.collect(Metric.EURIBOR_12M, date(2026, 4, 23))

        assert result is None
        assert len(captured) == 1
        assert captured[0].failure_kind == "parse_error"


# ---------------------------------------------------------------------------
# Missing file → clear error
# ---------------------------------------------------------------------------


class TestMissingFile:
    async def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.yaml"
        collector = FakeCollector(missing)

        with pytest.raises(FileNotFoundError) as exc_info:
            await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert str(missing) in str(exc_info.value)


# ---------------------------------------------------------------------------
# Malformed YAML → CollectorDataError
# ---------------------------------------------------------------------------


class TestMalformedYAML:
    """Asserted against ``fetch`` directly; ``collect`` would convert the
    same error into a SourceFailure and return None (tested separately)."""

    async def test_unclosed_flow_mapping_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "malformed.yaml", "{ this is not valid")
        collector = FakeCollector(path)

        with pytest.raises(CollectorDataError):
            await collector.fetch(Metric.ESTR, date(2026, 4, 23))

    async def test_yaml_not_a_mapping_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "list_root.yaml", "- just a list\n- of scalars\n")
        collector = FakeCollector(path)

        with pytest.raises(CollectorDataError):
            await collector.fetch(Metric.ESTR, date(2026, 4, 23))

    async def test_yaml_missing_datapoints_key_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "no_dp.yaml", "other_key: value\n")
        collector = FakeCollector(path)

        with pytest.raises(CollectorDataError):
            await collector.fetch(Metric.ESTR, date(2026, 4, 23))

    async def test_malformed_yaml_via_collect_records_parse_error(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path / "malformed.yaml", "{ this is not valid")
        captured: list[SourceFailure] = []
        collector = FakeCollector(path, failure_callback=captured.append)
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is None
        assert len(captured) == 1
        assert captured[0].failure_kind == "parse_error"


# ---------------------------------------------------------------------------
# Deterministic source_hash
# ---------------------------------------------------------------------------


class TestDeterministicHash:
    async def test_same_fixture_twice_produces_identical_source_hash(
        self, valid_fixture: Path
    ) -> None:
        c1 = FakeCollector(valid_fixture)
        c2 = FakeCollector(valid_fixture)
        r1 = await c1.collect(Metric.ESTR, date(2026, 4, 23))
        r2 = await c2.collect(Metric.ESTR, date(2026, 4, 23))

        assert r1 is not None and r2 is not None
        assert r1.source_hash == r2.source_hash

    async def test_same_collector_twice_produces_identical_source_hash(
        self, valid_fixture: Path
    ) -> None:
        collector = FakeCollector(valid_fixture)
        r1 = await collector.collect(Metric.ESTR, date(2026, 4, 23))
        r2 = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert r1 is not None and r2 is not None
        assert r1.source_hash == r2.source_hash
        # fetched_at will differ (datetime.now) but source_hash must not.
        # Sanity-check that fetched_at is indeed recorded.
        assert r1.fetched_at is not None

    async def test_different_values_give_different_source_hash(
        self, tmp_path: Path
    ) -> None:
        a = _write(
            tmp_path / "a.yaml",
            """\
            datapoints:
              - metric: ESTR
                value: "0.0375"
                unit: decimal_fraction
                as_of: "2026-04-23"
            """,
        )
        b = _write(
            tmp_path / "b.yaml",
            """\
            datapoints:
              - metric: ESTR
                value: "0.0380"
                unit: decimal_fraction
                as_of: "2026-04-23"
            """,
        )
        ra = await FakeCollector(a).collect(Metric.ESTR, date(2026, 4, 23))
        rb = await FakeCollector(b).collect(Metric.ESTR, date(2026, 4, 23))

        assert ra is not None and rb is not None
        assert ra.source_hash != rb.source_hash


# ---------------------------------------------------------------------------
# Sanity band violation
# ---------------------------------------------------------------------------


class TestSanityBandViolation:
    async def test_out_of_band_returns_none_and_records_failure(
        self, out_of_band_fixture: Path
    ) -> None:
        captured: list[SourceFailure] = []
        collector = FakeCollector(
            out_of_band_fixture, failure_callback=captured.append
        )
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))

        assert result is None
        assert len(captured) == 1
        failure = captured[0]
        assert failure.failure_kind == "sanity_band_violation"
        assert failure.source_id == "fake_v1"
        assert failure.metric is Metric.ESTR

    async def test_out_of_band_with_no_callback_still_returns_none(
        self, out_of_band_fixture: Path
    ) -> None:
        collector = FakeCollector(out_of_band_fixture)  # no callback
        result = await collector.collect(Metric.ESTR, date(2026, 4, 23))
        assert result is None
