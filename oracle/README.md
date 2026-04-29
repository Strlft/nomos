# Nomos Oracle

Signed, chained attestations of market and legal data for the DerivAI IRS engine.

The Oracle is the trusted-data plane between external sources (ECB, future
EMMI/Refinitiv) and the smart-legal-contract execution engine. Every value it
publishes is:

- **Sourced** — captured verbatim with `source_hash` (SHA-256 of bytes) and
  `source_url`.
- **Sanity-checked** — refused if outside a per-metric plausibility band.
- **Signed and chained** — each attestation hashes the previous one, so
  tampering with history is detectable in O(n) chain walk.
- **Audit-grade** — every refusal is recorded as a `SourceFailure`; every
  rule trigger is persisted alongside the attestation it cites.

---

## Quickstart

Requires Python ≥ 3.11. From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e oracle/[dev]
```

Run the test suite:

```bash
make test                 # unit + property tests, ~1.5s, no network
make test-integration     # also hits real ECB SDW endpoints
```

Or, equivalently:

```bash
cd oracle && pytest
cd oracle && pytest --run-integration
```

---

## Running the daily pipeline

### With `FakeCollector` (offline, deterministic)

The `FakeCollector` reads a YAML fixture instead of calling the network.
It is the path used in tests and demos:

```bash
make run-daily
# or, with explicit args:
python -m oracle.scheduler.daily_run \
    --fixture oracle/tests/fixtures/minimal_estr.yaml \
    --contract-id IRS-DEMO-0001 \
    --db-path /tmp/oracle.db \
    --as-of 2026-04-23
```

Output: one JSON line on **stdout** with the cycle summary (attestations
created, triggers emitted, source failures, indeterminate rules). Structured
logs are written to **stderr** as one JSON object per event — see
`logging_config.py` for the schema.

### With real collectors (ECB SDW)

`oracle.collectors.ecb.ECBCollector` serves all four metrics (ESTR daily,
EURIBOR_3M/6M/12M monthly) from `https://data-api.ecb.europa.eu`. It is
not yet wired into `daily_run.py` — that path still uses `FakeCollector`
for the demo. To run the real collector ad hoc:

```python
import asyncio
from datetime import date
from oracle.collectors.ecb import ECBCollector
from oracle.config import Metric

async def main() -> None:
    c = ECBCollector()
    for m in (Metric.ESTR, Metric.EURIBOR_3M, Metric.EURIBOR_6M, Metric.EURIBOR_12M):
        dp = await c.collect(m, date.today())
        print(m.value, dp.value if dp else "FAILED")

asyncio.run(main())
```

For the production scheduler, replace `FakeCollector(args.fixture)` in
`daily_run.main()` with `ECBCollector()`.

---

## Verifying chain integrity

`AttestationStore.verify_integrity()` walks every stored row, recomputes
the SHA-256 of `payload_json` byte-for-byte, and re-runs `verify_chain`
across the linked-list of `previous_hash → current_hash`. A single byte
mutation in any column is detected.

```bash
make verify-chain DB=/tmp/oracle.db
# or:
python -m oracle.scheduler.verify_chain --db-path /tmp/oracle.db
```

Exit code: `0` ok, `1` corrupt, `2` DB missing. Log line carries
`action="verify_integrity"` and `outcome="ok|corrupt"`.

---

## Dashboard

A minimal Streamlit dashboard surfaces the four operational signals an
on-call operator needs:

| Panel | Source query |
|---|---|
| Latest 10 attestations | `attestations` ORDER BY `sequence_number` DESC |
| Chain integrity | `verify_integrity()` traffic light |
| Recent trigger events | `trigger_events` aggregated by `(rule_id, severity)` + last 50 rows |
| Collector health | per `source_id`: last successful `fetched_at`, 24h failure count, latest attested value |

Run it:

```bash
make dashboard DB=/tmp/oracle.db
# or:
streamlit run oracle/dashboard/app.py -- --db-path /tmp/oracle.db
```

The dashboard is read-only and never writes to the chain.

---

## Adding a new collector

1. Subclass `oracle.collectors.base.BaseCollector` and implement:

   - `source_id: str` (class attribute) — registered in `oracle/config.py`
     `SOURCE_METRICS` and `PRIMARY_SOURCE`.
   - `async fetch(metric, as_of) -> RawDatapoint` — one HTTP attempt.
     Raise `CollectorUnavailableError` on 5xx/429 (retried), `CollectorDataError`
     on 4xx and parse failures (not retried).
   - `parse(raw) -> Decimal` — extract the typed value. Use `Decimal`,
     never `float`.
   - `unit_for(metric) -> Unit` — defaults to `DECIMAL_FRACTION`.

2. Add the metric → series mapping inside the collector. `ECBCollector`
   uses a `_METRIC_SERIES: dict[Metric, tuple[str, str]]` for this; copy
   the pattern.

3. Update `oracle/config.py`:
   - `SOURCE_ID_<NAME>` constant.
   - `SOURCE_METRICS[<source>]` — which metrics this source can publish.
   - `PRIMARY_SOURCE[metric] = <source>` if it should serve a metric by default.
   - Update / extend `SANITY_BANDS` if a new metric is introduced.

4. Capture a real fixture in `tests/fixtures/<source>/...` and write
   tests in `tests/test_collectors_<source>.py` covering: happy path
   (respx-mocked), 5xx retried 3×, 4xx not retried, malformed payload,
   `Decimal` (not `float`) assertion, and an `@pytest.mark.integration`
   live test.

5. Verify: `make test && make test-integration`.

---

## Adding a new rule

1. Create `oracle/rules/impl/r0NN_<short_name>.py`.
2. Define a pure predicate `(market, contract, as_of) -> RuleOutcome`. No
   `datetime.now()` inside the predicate — `as_of` is the only source of
   time. No IO, no globals, no randomness.
3. Build the `Rule` and register it at module load:

   ```python
   from oracle.rules.registry import register_rule
   from oracle.types import Rule
   from oracle.config import Severity

   rule = register_rule(Rule(
       rule_id="R-007",
       clause_ref="ISDA 2002 §X(y)(z)",
       severity=Severity.TRIGGER,
       predicate=_predicate,
       required_metrics=frozenset({Metric.ESTR}),
       required_contract_fields=frozenset({"some_field"}),
       grace_period=timedelta(days=1),
       version="1.0.0",
       description="One-line summary",
   ))
   ```

4. Import the module from `daily_run.main()` (or a registry-loader hook)
   so `register_rule` runs at startup. If a rule isn't imported it isn't
   evaluated.

5. Add `tests/test_rules_r0NN.py` covering every scenario from the spec.
   Use duck-typed dataclasses for the contract — see `test_rules_r006.py`
   for the canonical pattern. The engine's exception isolation means a
   crashing predicate is silently skipped, so cover the failure paths
   explicitly.

---

## Logging schema

Every log line is one JSON object on stderr with these canonical keys
(missing values render as `null`):

| Key | Meaning |
|---|---|
| `timestamp` | ISO-8601 UTC, added automatically. |
| `component` | Subsystem (e.g. `"scheduler"`, `"rules.engine"`). Bound at logger creation. |
| `action` | Verb describing what was attempted (`"collect"`, `"evaluate_rule"`, `"verify_integrity"`). |
| `outcome` | `"ok"`, `"failure"`, `"indeterminate"`, `"skipped"`, `"corrupt"`. |
| `duration_ms` | Wall-clock duration of the action. |
| `trace_id` | Cycle correlation id. Bound by the scheduler at the start of each daily run. |
| `level` | `"info"`, `"warning"`, `"error"`. |
| `message` | Human-readable event name. |

There is **exactly one `print()`** in the codebase: the JSON cycle summary
emitted at the end of `daily_run.main()` on stdout. Everything else
goes through `oracle.logging_config.get_logger()`.

---

## Troubleshooting

**`pytest` says `module not found: oracle`**
You're running from the wrong directory. The `pyproject.toml` `rootdir`
is `oracle/` — run `cd oracle && pytest`, or use `make test`.

**Hypothesis property tests aren't running**
The system `pytest` doesn't have `hypothesis` installed. Use the venv:
`./.venv/bin/pytest`. `make test` selects this automatically.

**`ChainIntegrityError: expected sequence_number=N, got M`**
You're trying to append an attestation that doesn't follow the chain
head. This usually means two writers raced. The store is single-writer
by design; serialise via the scheduler.

**`verify_integrity` returns `False` for a known-good DB**
Check that you're pointing at the right file. Both the byte-level SHA-256
and the parse-and-re-canonicalise checks must pass; a partial migration
or a mid-write crash can leave the chain in an inconsistent state. The
fix is to investigate, not to delete the offending row.

**Live ECB test fails with HTTP 429**
The ECB SDW endpoint rate-limits unauthenticated traffic. Wait a few
minutes. The collector treats 429 as transient and retries; persistent
429s mean you've been throttled.

**Streamlit dashboard renders empty panels**
The DB exists but has no data yet. Run `make run-daily` first to create
at least one attestation.

---

## File layout

```
oracle/
├── collectors/
│   ├── base.py        retry/backoff template + abstract BaseCollector
│   ├── ecb.py         ECB SDW (ESTR daily + EURIBOR monthly)
│   └── fake.py        deterministic YAML fixture collector
├── core/
│   ├── attestation.py canonical_json + payload_dict + verify_chain
│   ├── normalizer.py  raw → NormalizedDatapoint
│   ├── sanity.py      sanity-band check
│   └── store.py       SQLite append-only + verify_integrity
├── rules/
│   ├── engine.py      pure rule orchestrator
│   ├── registry.py    register_rule + get_all_rules
│   └── impl/          R-001 through R-006
├── integration/
│   └── irs_bridge.py  bridge to backend.engine
├── scheduler/
│   ├── daily_run.py   end-to-end daily cycle CLI
│   └── verify_chain.py integrity check CLI
├── dashboard/
│   └── app.py         Streamlit operational dashboard
├── tests/             unit + integration + property tests
├── config.py          metrics, sources, sanity bands
├── errors.py          CollectorDataError, CollectorUnavailableError, …
├── logging_config.py  structlog JSON config
└── types.py           Pydantic v2 typed models
```

---

## Phase history

- **Phase 1** — Pydantic types and config.
- **Phase 2** — normalizer + sanity bands.
- **Phase 3** — `FakeCollector` and retry policy.
- **Phase 4** — `AttestationStore` + chain integrity.
- **Phase 5** — `RuleEngine` + R-001.
- **Phase 6** — R-002 through R-006.
- **Phase 7** — `ECBCollector` for ESTR + EURIBOR (monthly). Single-source
  per metric; FRED/BdF dropped because no free public daily EURIBOR API
  exists.
- **Phase 8** — observability (this doc, structlog, dashboard,
  `verify-chain` CLI, `Makefile`).
