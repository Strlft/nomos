"""YAML-backed fixture collector for tests and local development.

The fixture file is YAML with the shape described in
``docs/oracle/CLAUDE_CODE_PROMPTS.md``:

.. code-block:: yaml

    datapoints:
      - metric: ESTR
        value: "0.019"
        unit: decimal_fraction
        as_of: "2026-04-23"
        source_reported_as_of: "2026-04-23"

Behavior:

* Missing file  → :class:`FileNotFoundError` (propagated verbatim so the
  caller sees exactly which path was wrong).
* Malformed YAML → :class:`CollectorDataError`.
* No matching metric → :class:`CollectorDataError`.
* The :attr:`RawDatapoint.raw_payload` is a **canonical** JSON blob
  (``{"value":..., "as_of":...}``) so that two reads of the same fixture
  produce identical ``source_hash`` — the invariant exercised by the
  Phase 3 determinism test.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from oracle.collectors.base import BaseCollector, FailureCallback
from oracle.config import Metric, Unit
from oracle.errors import CollectorDataError
from oracle.types import RawDatapoint


class FakeCollector(BaseCollector):
    """Read a YAML fixture and serve canonical-JSON RawDatapoints from it."""

    source_id: str = "fake_v1"

    def __init__(
        self,
        fixture_path: Path,
        *,
        failure_callback: FailureCallback | None = None,
    ) -> None:
        super().__init__(failure_callback=failure_callback)
        self._fixture_path = Path(fixture_path)

    # ------------------------------------------------------------------
    # Abstract overrides
    # ------------------------------------------------------------------

    async def fetch(self, metric: Metric, as_of: date) -> RawDatapoint:
        fixture = self._load_fixture()
        datapoint = self._find_datapoint(fixture, metric)

        value_str = str(datapoint["value"])
        fixture_as_of = str(datapoint["as_of"])

        canonical = json.dumps(
            {"value": value_str, "as_of": fixture_as_of},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        source_hash = hashlib.sha256(canonical).hexdigest()

        source_reported = datapoint.get("source_reported_as_of")
        if source_reported is not None:
            source_reported = str(source_reported)

        return RawDatapoint(
            source_id=self.source_id,
            metric=metric.value,
            raw_payload=canonical.decode("utf-8"),
            source_hash=source_hash,
            fetched_at=datetime.now(timezone.utc),
            source_url=f"file://{self._fixture_path}",
            source_reported_as_of=source_reported or fixture_as_of,
        )

    def parse(self, raw: RawDatapoint) -> Decimal:
        try:
            payload: Any = json.loads(raw.raw_payload)
        except json.JSONDecodeError as exc:
            raise CollectorDataError(
                f"raw_payload is not valid JSON: {exc}"
            ) from exc

        if not isinstance(payload, dict) or "value" not in payload:
            raise CollectorDataError(
                "raw_payload is missing the required 'value' field"
            )

        try:
            return Decimal(str(payload["value"]))
        except InvalidOperation as exc:
            raise CollectorDataError(
                f"value={payload['value']!r} is not a valid Decimal"
            ) from exc

    def unit_for(self, metric: Metric) -> Unit:
        """Read the unit out of the fixture row; default to DECIMAL_FRACTION."""

        fixture = self._load_fixture()
        datapoint = self._find_datapoint(fixture, metric)
        unit_raw = datapoint.get("unit", Unit.DECIMAL_FRACTION.value)
        try:
            return Unit(str(unit_raw))
        except ValueError as exc:
            raise CollectorDataError(
                f"unknown unit {unit_raw!r} in fixture for metric {metric.value}"
            ) from exc

    # ------------------------------------------------------------------
    # Fixture loading
    # ------------------------------------------------------------------

    def _load_fixture(self) -> dict[str, Any]:
        # Let FileNotFoundError propagate verbatim — the prompt requires a
        # clear error (not a silent None) when the fixture is missing.
        with self._fixture_path.open("r", encoding="utf-8") as fp:
            try:
                loaded = yaml.safe_load(fp)
            except yaml.YAMLError as exc:
                raise CollectorDataError(
                    f"fixture {self._fixture_path} is not valid YAML: {exc}"
                ) from exc

        if not isinstance(loaded, dict) or "datapoints" not in loaded:
            raise CollectorDataError(
                f"fixture {self._fixture_path} must be a mapping with a "
                f"'datapoints' key"
            )
        if not isinstance(loaded["datapoints"], list):
            raise CollectorDataError(
                f"fixture {self._fixture_path}: 'datapoints' must be a list"
            )
        return loaded

    def _find_datapoint(
        self, fixture: dict[str, Any], metric: Metric
    ) -> dict[str, Any]:
        for entry in fixture["datapoints"]:
            if not isinstance(entry, dict):
                raise CollectorDataError(
                    f"fixture {self._fixture_path}: each datapoint must be a mapping"
                )
            if entry.get("metric") == metric.value:
                for required in ("value", "as_of"):
                    if required not in entry:
                        raise CollectorDataError(
                            f"fixture {self._fixture_path}: datapoint for "
                            f"{metric.value} missing {required!r}"
                        )
                return entry
        raise CollectorDataError(
            f"fixture {self._fixture_path} has no datapoint for {metric.value}"
        )
