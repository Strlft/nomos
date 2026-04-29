# CLAUDE_CODE_PROMPTS.md — Step-by-step prompts for implementing the Oracle

**How to use this file.** Give Claude Code access to the three spec documents (`ORACLE_SPEC.md`, `ORACLE_ARCHITECTURE.md`, `ORACLE_RULES.md`). Then, for each phase below, copy the prompt *exactly* as written into a new Claude Code session (or a new message, with prior context). Wait for completion. Review the diff. Only after you are satisfied, move to the next phase.

**Do not merge phases.** The quality of the result depends on Claude Code doing one thing at a time with a clear scope.

**Before every prompt, preamble this in every session:**

> You are implementing the Nomos Oracle module. You must read `/docs/oracle/ORACLE_SPEC.md`, `/docs/oracle/ORACLE_ARCHITECTURE.md`, and `/docs/oracle/ORACLE_RULES.md` in full before writing any code. These three documents are binding. If you believe any requirement in them is unclear or wrong, STOP and ask me — do not silently deviate. You must never modify `irs_engine_v2.py` without my explicit, written consent in chat. You must never invent market data, never hardcode "fallback" rate values, and never publish attestations when a source is unavailable.

---

## Phase 1 — Scaffold and types

**Goal**: directory structure and all Pydantic models. No logic yet.

**Prompt:**

> Phase 1: scaffold and types.
>
> 1. Create the directory structure exactly as described in `ORACLE_ARCHITECTURE.md` §2, with empty `__init__.py` files in every package.
>
> 2. Implement `oracle/types.py` with every Pydantic v2 model defined in `ORACLE_ARCHITECTURE.md` §3 and the Metric/Unit/Severity enums from §4. Every model must use `model_config = ConfigDict(frozen=True, strict=True)`. All monetary or rate values must be `Decimal`, never `float`. Tuples, not lists, for immutable collections inside models.
>
> 3. Implement `oracle/errors.py` with the exception classes: `CollectorUnavailableError`, `CollectorDataError`, `ChainIntegrityError`, `SanityBandViolation`, `CrossValidationFailure`, `DataUnavailableError`, `DataInconsistentError`. Each should inherit from a single `OracleError` base.
>
> 4. Implement `oracle/config.py` with the source-metric mapping tables, sanity bands, and cross-validation tolerances from §4 and §5 of the architecture doc.
>
> 5. Write `oracle/tests/test_types.py` covering: (a) every model rejects mutation with `ValidationError` or `TypeError`, (b) required fields are validated, (c) `Decimal` fields reject `float` inputs in strict mode, (d) `OracleAttestation` with `sequence_number=0` must have `is_genesis=True` and `previous_hash=None`, (e) `OracleAttestation` with `sequence_number>0` must have `is_genesis=False` and a non-null `previous_hash`.
>
> 6. Add a `pyproject.toml` with these dependencies: `pydantic>=2.6`, `httpx>=0.27`, `structlog>=24`, `pytest>=8`, `pytest-asyncio`, `pytest-cov`, `respx`, `hypothesis`, `pyyaml`. Python version >=3.11.
>
> Do not implement any collector, any core logic, any rule, any database access. Do not touch `irs_engine_v2.py`. When done, show me the complete diff and the output of `pytest oracle/tests/test_types.py -v`.

**Stop condition**: tests pass, every field matches the spec exactly, no logic in any file yet.

---

## Phase 2 — Oracle Core: attestation building, signing, chaining, persistence

**Prompt:**

> Phase 2: oracle core.
>
> 1. Implement `oracle/core/normalizer.py`: function `normalize(raw: RawDatapoint, expected_metric: Metric, expected_unit: Unit) -> NormalizedDatapoint`. Parsing logic for the value depends on the source format — for now, write the normalizer generically and let each collector feed it already-parsed values; the normalizer's job is type coercion, unit validation, and sanity-band application.
>
> 2. Implement `oracle/core/sanity.py`: function `check_sanity_band(metric: Metric, value: Decimal, unit: Unit) -> bool`. Reads bands from `oracle/config.py`. Returns True if within band, False otherwise.
>
> 3. Implement `oracle/core/attestation.py` with:
>    - `canonical_json(payload: dict) -> bytes`: JSON-encoded, sorted keys, no whitespace, UTF-8.
>    - `compute_payload_hash(datapoints: tuple[NormalizedDatapoint, ...], signed_at: datetime, rules_version: str, oracle_version: str) -> str`: SHA-256 hex.
>    - `build_attestation(...)`: constructs an `OracleAttestation`, computing `payload_hash`, chaining `previous_hash`, computing `current_hash`.
>    - `verify_attestation(a: OracleAttestation, expected_previous_hash: str | None) -> bool`: recomputes every hash and chain link.
>    - `verify_chain(attestations: list[OracleAttestation]) -> tuple[bool, str | None]`: returns (ok, error_message). Error message must identify the first attestation that failed verification.
>
> 4. Implement `oracle/core/store.py` with the SQLite schemas from §7 of the architecture doc. Use `sqlite3` from the standard library — no ORM. Expose a class `AttestationStore` with methods:
>    - `__init__(db_path: Path)`: creates tables if they don't exist.
>    - `append(attestation: OracleAttestation) -> None`: validates that `previous_hash` equals the last stored `current_hash`, and that `sequence_number` is exactly `last_sequence + 1`. Raises `ChainIntegrityError` otherwise. Atomic transaction covering both `attestations` and `datapoints` tables.
>    - `record_failure(failure: SourceFailure) -> None`: writes to `source_failures`. Non-atomic with attestations (failures can coexist with successful attestations for other metrics).
>    - `record_trigger(event: TriggerEvent) -> None`: writes to `trigger_events`.
>    - `get_all_attestations() -> list[OracleAttestation]`
>    - `get_latest_attestation() -> OracleAttestation | None`
>    - `verify_integrity() -> tuple[bool, str | None]`: runs `verify_chain` over all stored attestations.
>
> 5. Write these test files:
>    - `test_attestation.py`: genesis attestation passes; second attestation well-chained passes; second attestation with wrong `previous_hash` fails; mutation of a stored attestation's payload fails `verify_chain`; round-trip serialize/deserialize preserves `current_hash`.
>    - `test_store.py`: append/read cycle; rejection of out-of-order sequence; rejection of broken chain; `verify_integrity()` on a database corrupted via raw SQL (simulate by writing a wrong `payload_json` directly); isolation of `source_failures` from chain integrity.
>    - `test_sanity.py`: each V1 metric's band is enforced; edge cases (exactly at min, exactly at max, just outside each).
>
> 6. Property-based test `test_chain_property.py` using `hypothesis`: generate 100 sequences of random NormalizedDatapoint tuples, build attestation chains, verify that (a) every valid chain verifies True, (b) any single random byte mutation of a stored `payload_json` breaks verification.
>
> Show me the diff and `pytest oracle/tests/ -v` output. Do not touch collectors, rules, or `irs_engine_v2`.

**Stop condition**: all tests pass; you manually corrupt a byte in the SQLite file via `sqlite3` CLI and `verify_integrity()` detects it.

---

## Phase 3 — FakeCollector and the base collector contract

**Prompt:**

> Phase 3: the base collector contract and a fixture-based fake collector.
>
> 1. Implement `oracle/collectors/base.py` with:
>    - Abstract class `BaseCollector` with method `async def fetch(self, metric: Metric, as_of: date) -> RawDatapoint` and abstract property `source_id: str`.
>    - Abstract method `def parse(self, raw: RawDatapoint) -> Decimal` that each concrete collector implements to extract the value from its source-specific payload.
>    - Concrete template method `async def collect(self, metric: Metric, as_of: date) -> NormalizedDatapoint | None` that calls `fetch`, then `parse`, then feeds the normalizer, then the sanity band, returning the NormalizedDatapoint if all pass, else returning None and emitting a `SourceFailure` via a callback (passed in constructor).
>    - The retry/backoff policy from §8 of the architecture doc lives here as shared logic.
>
> 2. Implement `oracle/collectors/fake.py`: `FakeCollector(BaseCollector)` that reads a YAML file (path given in constructor) and returns fixtures matching the requested metric. YAML structure:
>
> ```yaml
> datapoints:
>   - metric: ESTR
>     value: "0.019"
>     unit: decimal_fraction
>     as_of: "2026-04-23"
>     source_reported_as_of: "2026-04-23"
> ```
>
> 3. Tests in `test_collectors_fake.py`:
>    - Valid fixture returns a NormalizedDatapoint with correct Decimal value.
>    - Missing file raises a clear error (not a silent None).
>    - Malformed YAML raises `CollectorDataError`.
>    - Same fixture read twice produces identical `source_hash` (determinism).
>    - Value outside sanity band produces None and records a `SourceFailure` with kind `sanity_band_violation`.
>
> Do not implement the ECB, BdF, or FRED collector yet. Do not change core/. Do not change types.py. Show me the diff.

**Stop condition**: FakeCollector tests pass; you can run a small script that instantiates a FakeCollector, calls `collect(Metric.ESTR, date.today())`, and gets a NormalizedDatapoint back.

---

## Phase 4 — Rules engine scaffold and R-001 (Failure to Pay)

**Prompt:**

> Phase 4: rules engine and the first rule.
>
> 1. Implement `oracle/rules/calendar.py`: a TARGET2 calendar that can compute `add_business_days(start: date, n: int) -> date`. Use a hardcoded static list of TARGET2 holidays for 2024–2027 (data available from the ECB website). Tests: known holidays (Good Friday, Easter Monday, 1 May, 25 and 26 December, 1 January) are skipped; weekends are skipped; non-holiday weekdays count. Do NOT pull `python-holidays` — too broad and not TARGET2-specific.
>
> 2. Implement `oracle/rules/registry.py`:
>    - Decorator `@register_rule` that collects rule modules.
>    - Function `get_all_rules() -> list[Rule]` returning all registered rules.
>    - Function `get_rule_by_id(rule_id: str) -> Rule | None`.
>
> 3. Implement `oracle/rules/engine.py`:
>    - Class `RuleEngine` with method `evaluate(market: MarketState, contract: ContractState, as_of: date) -> list[TriggerEvent]`.
>    - For each rule: check that `required_metrics` are all present in `market.latest` — if any missing, the rule is **indeterminate**, it does not evaluate, and the engine emits **no event** for it (the indeterminate state is logged but not a TriggerEvent).
>    - For each rule whose inputs are complete: call the predicate. If True, construct an immutable TriggerEvent with all required evidence fields.
>    - The engine is purely a function: no state, no IO.
>
> 4. Implement `oracle/rules/impl/r001_failure_to_pay.py` exactly as specified in `ORACLE_RULES.md` R-001. Predicate takes `(MarketState, ContractState)` and returns True when conditions are met. Grace-period logic uses the TARGET2 calendar from step 1 and respects schedule override per R-001 spec.
>
> 5. Tests in `test_rules_r001.py`: exactly the eight scenarios from the R-001 test matrix in `ORACLE_RULES.md`. Each test must assert on `severity` and on the `evidence` tuple contents, not just on the trigger firing.
>
> 6. Tests in `test_rules_engine.py`: the engine correctly routes multiple rules; missing required metrics leave a rule indeterminate; a rule that raises an unexpected exception does not crash the engine but is logged as an internal error and produces no event.
>
> Do not implement R-002 through R-006 yet. Do not implement real-network collectors. Do not touch `irs_engine_v2.py`. Show me the diff.

**Stop condition**: R-001's eight scenarios pass.

---

## Phase 5 — End-to-end pipeline with the bridge

**Prompt:**

> Phase 5: wire the pipeline end-to-end using FakeCollector.
>
> 1. Before writing any code: read `irs_engine_v2.py` and report back, in chat, which public functions you would call to:
>    - Fetch a read-only snapshot of a contract's state (for `ContractState`).
>    - Submit a `TriggerEvent` and receive a receipt.
>    If no such functions exist, propose the minimum possible additions — **name the functions, describe the signatures, list the side effects, and wait for my approval** before modifying `irs_engine_v2.py`. Do not refactor the engine.
>
> 2. After I approve: implement `oracle/integration/irs_bridge.py` with exactly two public functions:
>    - `fetch_contract_state(contract_id: str) -> ContractState` — read-only.
>    - `submit_trigger_event(event: TriggerEvent) -> TriggerReceipt` — the only mutating call the Oracle ever makes against the engine, and it submits a typed event, not a command.
>    Nothing else. No convenience wrappers.
>
> 3. Implement `oracle/scheduler/daily_run.py` as a CLI entry point:
>    - Accepts `--fixture path/to/data.yaml` (uses FakeCollector), `--contract-id`, `--db-path`.
>    - Fetches datapoints via the specified collector.
>    - Builds a MarketState from the latest valid datapoint per metric.
>    - Calls `fetch_contract_state` on the bridge.
>    - Constructs and signs an `OracleAttestation`, persists it.
>    - Runs `RuleEngine.evaluate`, gets a list of `TriggerEvent`s.
>    - For each event: calls `submit_trigger_event` via the bridge, persists the event in `trigger_events`.
>    - Emits a final JSON log record summarizing: `attestations_created`, `triggers_emitted`, `source_failures`, `indeterminate_rules`.
>
> 4. Write `test_pipeline_e2e.py`: a single test that runs the entire pipeline on a fixture where R-001 should fire. Asserts: one attestation persisted, one TriggerEvent persisted with rule_id R-001, chain integrity holds, `irs_engine_v2`'s state was not directly mutated by the Oracle (only the submit_trigger_event bridge method was called, which the test can observe through a mock or a test-double).
>
> Show me the diff and the pipeline output.

**Stop condition**: the e2e test passes, and running the CLI manually on a fixture produces a sensible log summary.

---

## Phase 6 — Remaining rules R-002 through R-006

Do these **one at a time**, not in parallel. Each rule gets its own prompt.

### Phase 6a — R-002 Breach of Agreement

**Prompt:**

> Implement R-002 Breach of Agreement exactly as specified in `ORACLE_RULES.md`. File: `oracle/rules/impl/r002_breach_of_agreement.py`. Tests: `test_rules_r002.py`, covering every scenario in the R-002 test matrix. Do not touch other rules. Note that disaffirmation must never auto-TRIGGER; enforce this with an explicit test.

### Phase 6b — R-003 Cross Default

**Prompt:**

> Implement R-003 Cross Default per `ORACLE_RULES.md`. Critical: POTENTIAL_TRIGGER only, never TRIGGER, per the spec. If `external_defaults` contains records in currencies other than the Threshold currency, raise `DataInconsistentError` — do not attempt conversion. Tests must cover the currency-mismatch scenario explicitly.

### Phase 6c — R-004 Illegality (Rate Unavailability)

**Prompt:**

> Implement R-004 Illegality per `ORACLE_RULES.md`. This rule depends on `market_state.missing` (not `market_state.latest`). The 5-business-day threshold for POTENTIAL_TRIGGER escalation is an Oracle heuristic, not an ISDA legal period — document this in the rule's `description` field. Tests must cover transient vs sustained unavailability.

### Phase 6d — R-005 Tax Event

**Prompt:**

> Implement R-005 Tax Event per `ORACLE_RULES.md`. Flag-based only; never auto-TRIGGER; POTENTIAL_TRIGGER is the highest severity this rule can emit. Tests must cover each `kind` value from the spec.

### Phase 6e — R-006 Material Adverse Change

**Prompt:**

> Implement R-006 Material Adverse Change per `ORACLE_RULES.md`. This rule has three indicators (rating downgrade, external payment default, severe NPV deterioration). The rule **never auto-TRIGGERS** — maximum severity is POTENTIAL_TRIGGER, and only when at least two indicators are simultaneously triggered. Evidence must enumerate which indicators fired and why. Tests must cover: zero indicators, each single indicator alone (a WARNING event per the spec, not POTENTIAL_TRIGGER), two indicators (POTENTIAL_TRIGGER), three indicators (POTENTIAL_TRIGGER with all evidence enumerated).

### Phase 6 closing

**After all five rules are implemented**, run:

> Re-run the entire `pytest oracle/tests/ -v --cov=oracle` suite. All tests must pass. Report coverage percentages. Identify any rule with less than 100% line coverage and explain why.

---

## Phase 7 — Real collectors: ECB, Banque de France, FRED

### Phase 7a — ECBCollector (€STR)

**Prompt:**

> Implement `oracle/collectors/ecb.py` — `ECBCollector(BaseCollector)` that fetches €STR from the ECB SDW SDMX-JSON 2.0 API.
>
> Endpoint and parsing:
> - Use `httpx.AsyncClient`, never `urllib`.
> - Base URL: `https://data-api.ecb.europa.eu/service/data`.
> - Series key for €STR: `EST.B.EU000A2X2A25.WT` (verify this at implementation time by hitting `https://data-api.ecb.europa.eu/help/`; if the current series key differs, update the code and log the verification date in a comment with a URL).
> - Query params: `?lastNObservations=1&format=jsondata`.
> - Retry/timeout policy: exactly as specified in `ORACLE_ARCHITECTURE.md` §8. No deviation.
>
> Parsing:
> - The response is SDMX-JSON. Extract the value from `dataSets[0].series[<any key>].observations[<last>][0]`.
> - ECB publishes €STR as a percentage (e.g., `1.93`). Divide by 100 to get `DECIMAL_FRACTION` unit before feeding the normalizer.
> - Extract `as_of` date from `structure.dimensions.observation[0].values[<last>].id`.
>
> Failures:
> - On any HTTP 5xx, 429, timeout, or connection error: retry per policy.
> - On HTTP 4xx (except 429): fail immediately without retry.
> - On JSON parse failure: fail immediately; emit `SourceFailure` with kind `parse_error`.
> - On sanity band violation: emit `SourceFailure` with kind `sanity_band_violation`.
>
> Tests in `test_collectors_ecb.py`:
> - Use `respx` to mock `httpx`. Happy path with a captured real ECB payload (save one in `tests/fixtures/ecb/estr_sample.json` — you will need to curl the real endpoint once, manually, and commit the response).
> - 503 retried 3 times → CollectorUnavailableError.
> - 400 fails immediately → no retry observed.
> - Malformed JSON → CollectorDataError.
> - Assert that the parsed value is `Decimal`, not `float`, with an explicit `assert isinstance(value, Decimal)`.
>
> Integration test marked `@pytest.mark.integration` that hits the real ECB endpoint, fetches €STR, asserts the value is in `[-0.02, 0.15]`. Skip in CI by default.
>
> Do not touch FakeCollector. Show me the diff.

### Phase 7b — BdFCollector (EURIBOR)

**Prompt:**

> Implement `oracle/collectors/bdf.py` — `BdFCollector(BaseCollector)` for Banque de France Webstat EURIBOR daily.
>
> Do not hardcode series codes based on guesses. Your first action is to browse the BdF Webstat catalogue (start at `https://webstat.banque-france.fr/`) and identify the exact daily EURIBOR 3M, 6M, and 12M series codes. Report these to me in chat **before writing parsing code**, with the catalogue URLs. Wait for my confirmation of the series codes.
>
> Once confirmed, implement the collector following the same template as `ECBCollector`:
> - `httpx.AsyncClient`, retry policy per spec.
> - Parse the BdF Webstat response (typically SDMX-JSON or a similar format).
> - Divide by 100 to convert from percent to decimal fraction.
> - Same sanity band enforcement as ECB.
>
> Tests same structure as ECB: respx-mocked unit tests with a captured real payload, integration test for real network.
>
> Do not touch other collectors.

### Phase 7c — FREDCollector (EURIBOR cross-validation)

**Prompt:**

> Implement `oracle/collectors/fred.py` — `FREDCollector(BaseCollector)` using the FRED API.
>
> - Endpoint: `https://api.stlouisfed.org/fred/series/observations`
> - Series ID for EURIBOR 3M: `EUR3MTD156N` (Euro Interbank Offered Rate, 3-Month, Daily). Verify at implementation time.
> - Requires API key in env var `FRED_API_KEY`. If not set, the collector's constructor raises a clear error — do not silently degrade.
> - FRED rate limit: 120 requests/minute. V1 polls well below this, but implement a token bucket limiter anyway (simple, 10 lines of code).
> - FRED returns values as strings that can be `"."` for missing observations. Treat `"."` as a source failure of kind `parse_error`, do not substitute.
>
> Tests same structure as previous collectors.

### Phase 7d — Cross-validator

**Prompt:**

> Implement `oracle/core/cross_validator.py`:
>
> - Function `cross_validate(primary: NormalizedDatapoint | None, secondary: NormalizedDatapoint | None, tolerance: Decimal) -> CrossValidationResult`.
> - `CrossValidationResult` is an immutable Pydantic model with fields: `accepted: bool`, `chosen: NormalizedDatapoint | None`, `cross_checked_against: NormalizedDatapoint | None`, `reason: str`.
> - Rules:
>   - Both present, within tolerance → accept primary, record cross-checked against secondary.
>   - Both present, outside tolerance → reject both, reason explains the disagreement with exact values.
>   - Primary present, secondary absent → accept primary with flag `cross_validated=False`.
>   - Primary absent → reject (secondary never promoted).
>
> - Integrate this into the `daily_run.py` pipeline: for any metric configured with a secondary source in `config.py`, cross-validate before attestation.
>
> Tests in `test_cross_validator.py`: all four scenarios, plus edge cases (exactly at tolerance, tolerance=0).

---

## Phase 8 — Polish

**Prompt:**

> Phase 8: observability and documentation.
>
> 1. Configure `structlog` in `oracle/logging_config.py`: JSON renderer, fields `timestamp`, `component`, `action`, `outcome`, `duration_ms`, `trace_id`. No `print()` anywhere outside `daily_run.py`'s final user-facing summary.
>
> 2. Write `oracle/README.md` covering: quickstart, how to run the daily pipeline with FakeCollector, how to run with real collectors, how to add a new collector, how to add a new rule, how to run `verify_chain()` on the live database, troubleshooting.
>
> 3. Build a minimal Streamlit dashboard `oracle/dashboard/app.py` with four panels: latest attestations (10 most recent), chain integrity status (green/red with last check time), recent trigger events (by rule_id, severity), collector health (per source_id: last successful fetch time, 24h failure count, latest attested value).
>
> 4. Add a final repo-level `Makefile` or `justfile` with commands: `test`, `test-integration`, `lint`, `run-daily`, `dashboard`, `verify-chain`.
>
> Do not add new features. Only observability and docs.

---

## General rules of engagement for every phase

1. **One phase at a time.** Do not let Claude Code merge phases even if it offers to.
2. **Review the diff.** Before accepting, read every file changed. If you do not understand a line, ask.
3. **Run the tests yourself.** Do not trust "tests pass" reports without running `pytest` in your own terminal.
4. **Forbidden actions require your written approval in chat**: modifying `irs_engine_v2.py`, adding a new external dependency not listed in `pyproject.toml`, changing any type schema in `types.py`, hardcoding any rate value.
5. **When Claude Code proposes a "simpler" alternative** to something in the spec, the default answer is **no, follow the spec**. The spec's rigor is the point. Shortcuts here mean problems with your professor.
6. **Ask Claude Code to show the diff** at the end of every phase. Review it in your editor's diff viewer before accepting.

---

End of prompts document.
