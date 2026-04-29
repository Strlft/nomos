"""End-to-end tests for ``--live-ecb`` mode of the daily Oracle cycle.

These tests drive :func:`oracle.scheduler.daily_run.main` with the new
``--live-ecb`` flag and assert that:

* The ECB SDW endpoint is hit (mocked via ``respx``) and the resulting
  attestation carries ``source_id="ecb_sdw_v1"``.
* On HTTP 503, the collector retries three times, no attestation is
  persisted, exactly one :class:`SourceFailure` is recorded, and the
  CLI exits non-zero — invariant I5 (no fallback, no partial publish).
* The mutually-exclusive argument group rejects both ``--fixture`` and
  ``--live-ecb`` together as well as neither.
* (Integration, opt-in) The real ECB endpoint returns a value inside
  the ESTR sanity band.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from oracle.config import Metric
from oracle.scheduler.daily_run import main


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ecb"
_ESTR_FIXTURE = _FIXTURE_DIR / "estr_sample.json"

_ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def zero_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip retry backoff so the failure-case test stays fast."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    import oracle.collectors.base as base

    monkeypatch.setattr(base.asyncio, "sleep", _no_sleep)


def _read_attestations(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT a.attestation_id, a.sequence_number, "
            "       d.source_id, d.metric "
            "FROM attestations a "
            "LEFT JOIN datapoints d ON d.attestation_id = a.attestation_id "
            "ORDER BY a.sequence_number ASC"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _read_source_failures(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT failure_id, source_id, metric, failure_kind, attempts "
            "FROM source_failures"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _base_argv(db_path: Path) -> list[str]:
    return [
        "--contract-id", "LIVE-ECB-001",
        "--db-path", str(db_path),
        "--as-of", "2026-04-23",
    ]


# ---------------------------------------------------------------------------
# Happy path — --live-ecb hits ECB and produces an attestation
# ---------------------------------------------------------------------------


@respx.mock
def test_live_ecb_mode_uses_ecb_collector(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _ESTR_FIXTURE.read_text(encoding="utf-8")
    route = respx.get(_ECB_URL).mock(
        return_value=httpx.Response(200, text=payload)
    )

    db_path = tmp_path / "oracle.db"
    rc = main(["--live-ecb", *_base_argv(db_path)])

    assert rc == 0, "live-ecb cycle should exit 0 on success"
    assert route.call_count >= 1, "ECB endpoint must have been called"

    rows = _read_attestations(db_path)
    assert len(rows) == 1, f"expected 1 attestation row, got {rows!r}"
    row = rows[0]
    assert row["sequence_number"] == 0
    assert row["source_id"] == "ecb_sdw_v1"
    assert row["metric"] == Metric.ESTR.value

    failures = _read_source_failures(db_path)
    assert failures == [], f"unexpected source failures: {failures!r}"

    summary_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert '"attestations_created": 1' in summary_line
    assert '"collected_metrics": ["ESTR"]' in summary_line


# ---------------------------------------------------------------------------
# Failure path — 503 three times → no attestation, one SourceFailure, exit 1
# ---------------------------------------------------------------------------


@respx.mock
def test_live_ecb_failure_persists_source_failure(
    tmp_path: Path, zero_sleep: None, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.get(_ECB_URL).mock(
        return_value=httpx.Response(503, text="busy")
    )

    db_path = tmp_path / "oracle.db"
    rc = main(["--live-ecb", *_base_argv(db_path)])

    assert rc == 1, "live-ecb must exit non-zero when no attestation is published"
    assert route.call_count == 3, (
        f"ECB collector must retry three times on 503, saw {route.call_count}"
    )

    rows = _read_attestations(db_path)
    assert rows == [], (
        f"no attestation may be persisted on upstream failure (I5); got {rows!r}"
    )

    failures = _read_source_failures(db_path)
    assert len(failures) == 1, f"expected one SourceFailure row; got {failures!r}"
    f = failures[0]
    assert f["source_id"] == "ecb_sdw_v1"
    assert f["metric"] == Metric.ESTR.value
    assert f["failure_kind"] == "http_5xx"
    assert f["attempts"] == 3

    summary_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert '"attestations_created": 0' in summary_line
    assert '"source_failures": 1' in summary_line


# ---------------------------------------------------------------------------
# Argparse mutual exclusion
# ---------------------------------------------------------------------------


def test_mutually_exclusive_args_both_rejected(tmp_path: Path) -> None:
    fixture = tmp_path / "f.yaml"
    fixture.write_text("datapoints: []\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main([
            "--fixture", str(fixture),
            "--live-ecb",
            *_base_argv(tmp_path / "oracle.db"),
        ])
    assert excinfo.value.code == 2  # argparse error


def test_mutually_exclusive_args_neither_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(_base_argv(tmp_path / "oracle.db"))
    assert excinfo.value.code == 2  # argparse error


# ---------------------------------------------------------------------------
# Integration — hits the real ECB endpoint. Opt in with `pytest -m integration`.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_ecb_endpoint_smoke(tmp_path: Path) -> None:
    db_path = tmp_path / "oracle.db"
    rc = main(["--live-ecb", *_base_argv(db_path)])
    assert rc == 0, "real ECB cycle returned non-zero — check connectivity"

    rows = _read_attestations(db_path)
    assert len(rows) == 1, f"expected 1 attestation row, got {rows!r}"

    # Pull the persisted ESTR datapoint value and assert it lives inside
    # the ESTR sanity band [-0.02, 0.15] (DECIMAL_FRACTION).
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT value FROM datapoints WHERE metric = ? LIMIT 1",
            (Metric.ESTR.value,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None
    value = Decimal(row[0])
    assert Decimal("-0.02") <= value <= Decimal("0.15"), (
        f"real ESTR value {value} outside sanity band"
    )
