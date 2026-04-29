# ORACLE_ARCHITECTURE.md — Technical Architecture (V1)

**Version** : 1.1
**Reads alongside**: `ORACLE_SPEC.md` (functional), `ORACLE_RULES.md` (rules)
**Audience**: the developer implementing, the reviewer auditing, Claude Code generating code
**Status**: Draft for review

---

## 1. Data flow

```
┌────────────────────────────────────────────────────────────────────┐
│                       EXTERNAL SOURCES                             │
│   ECB SDW  │  BdF Webstat  │  FRED                                 │
└────────────┬──────────────┬─────────────┬──────────────────────────┘
             │              │             │
             ▼              ▼             ▼
┌────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — COLLECTORS                       oracle/collectors/     │
│  BaseCollector (abstract)                                          │
│  ├─ FakeCollector     (YAML fixtures — for tests)                  │
│  ├─ ECBCollector      (httpx async, retry, timeout)                │
│  ├─ BdFCollector      (httpx async, retry, timeout)                │
│  └─ FREDCollector     (httpx async, retry, timeout, API key)       │
│                                                                    │
│  Output type: RawDatapoint                                         │
└──────────────────────────┬─────────────────────────────────────────┘
                           │  RawDatapoint (frozen)
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  LAYER 2 — ORACLE CORE                      oracle/core/           │
│  ├─ Normalizer           (RawDatapoint → NormalizedDatapoint)      │
│  ├─ SanityBands          (reject obviously wrong values)           │
│  ├─ CrossValidator       (compare primary vs secondary)            │
│  ├─ AttestationBuilder   (hash + chain)                            │
│  └─ AttestationStore     (SQLite append-only)                      │
│                                                                    │
│  Output type: OracleAttestation (frozen, signed, chained)          │
└──────────────────────────┬─────────────────────────────────────────┘
                           │  OracleAttestation
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — RULES ENGINE                     oracle/rules/          │
│  ├─ MarketStateBuilder   (latest valid attestation per metric)     │
│  ├─ RuleRegistry         (loaded via decorator)                    │
│  ├─ RuleEngine.evaluate()                                          │
│  └─ rules/impl/r001.py … r006.py                                   │
│                                                                    │
│  Output type: list[TriggerEvent]                                   │
└──────────────────────────┬─────────────────────────────────────────┘
                           │  TriggerEvent
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  BRIDGE                                     oracle/integration/    │
│  IRSBridge.fetch_contract_state()   (read)                         │
│  IRSBridge.submit_trigger_event()   (write-only, narrow)           │
└──────────────────────────┬─────────────────────────────────────────┘
                           ▼
                    irs_engine_v2.py
                    (consumes, decides, never modified by Oracle)
```

**Golden rule**: data flows downward. No layer calls back upward. No shared globals.

---

## 2. Directory layout

```
oracle/
├── __init__.py
├── types.py                      # All pydantic models (§3)
├── config.py                     # Metric enum, sanity bands, source/metric mapping
├── errors.py                     # Custom exceptions
│
├── collectors/
│   ├── __init__.py
│   ├── base.py                   # BaseCollector (ABC)
│   ├── fake.py                   # FakeCollector (YAML fixtures)
│   ├── ecb.py                    # ECBCollector
│   ├── bdf.py                    # BdFCollector
│   └── fred.py                   # FREDCollector
│
├── core/
│   ├── __init__.py
│   ├── normalizer.py             # RawDatapoint → NormalizedDatapoint
│   ├── sanity.py                 # Sanity band checks
│   ├── cross_validator.py        # Primary vs secondary comparison
│   ├── attestation.py            # Build + sign + verify chain
│   └── store.py                  # SQLite persistence
│
├── rules/
│   ├── __init__.py
│   ├── engine.py                 # RuleEngine orchestrator
│   ├── registry.py               # @register_rule decorator
│   ├── calendar.py               # TARGET2 business-day calendar
│   └── impl/
│       ├── __init__.py
│       ├── r001_failure_to_pay.py
│       ├── r002_breach_of_agreement.py
│       ├── r003_cross_default.py
│       ├── r004_illegality.py
│       ├── r005_tax_event.py
│       └── r006_mac.py
│
├── integration/
│   ├── __init__.py
│   └── irs_bridge.py             # Narrow read/write interface to irs_engine_v2
│
├── scheduler/
│   ├── __init__.py
│   └── daily_run.py              # CLI entry
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── fixtures/                 # Captured payloads, YAML datasets
│   ├── test_types.py
│   ├── test_attestation.py
│   ├── test_store.py
│   ├── test_collectors_fake.py
│   ├── test_collectors_ecb.py
│   ├── test_collectors_bdf.py
│   ├── test_collectors_fred.py
│   ├── test_cross_validator.py
│   ├── test_rules_engine.py
│   ├── test_rules_r001.py
│   ├── test_rules_r002.py
│   ├── test_rules_r003.py
│   ├── test_rules_r004.py
│   ├── test_rules_r005.py
│   ├── test_rules_r006.py
│   ├── test_pipeline_e2e.py
│   └── test_chain_property.py    # hypothesis
│
└── README.md
```

---

## 3. Type schemas — the contract of interfaces

All models use **Pydantic v2** with `model_config = ConfigDict(frozen=True, strict=True)`. Numeric values representing money or rates are `Decimal`, never `float`.

### 3.1 `RawDatapoint`

```python
class RawDatapoint(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    source_id: str                      # e.g. "ecb_sdw_v1"
    metric: str                         # Canonical metric name, see config.py
    raw_payload: str                    # Raw bytes decoded as UTF-8
    source_hash: str                    # SHA-256 hex digest of raw_payload
    fetched_at: datetime                # UTC, when the collector received the response
    source_url: str                     # URL fetched
    source_reported_as_of: str | None   # Date string in the source's original format (parsed later)
```

### 3.2 `NormalizedDatapoint`

```python
class NormalizedDatapoint(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    source_id: str
    metric: Metric                       # Enum, see config.py
    value: Decimal                       # Never float
    unit: Unit                           # PERCENT_PER_ANNUM | DECIMAL_FRACTION | BASIS_POINTS
    as_of: date                          # Business date source assigns to this value
    fetched_at: datetime                 # UTC, from RawDatapoint
    source_hash: str                     # Propagated
    source_url: str                      # Propagated
    sanity_band_passed: bool             # Must be True to be included in an attestation
    cross_validated: bool                # True if a secondary source agreed within tolerance
    cross_checked_against: str | None    # source_id of the secondary, None if no cross-validation
```

### 3.3 `OracleAttestation`

```python
class OracleAttestation(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    attestation_id: UUID
    sequence_number: int                 # Monotonic, gap-free
    datapoints: tuple[NormalizedDatapoint, ...]
    signed_at: datetime                  # UTC
    rules_version: str                   # Semver of the rule set at signing time
    oracle_version: str                  # Semver of oracle code at signing time

    # Chain fields
    payload_hash: str
    previous_hash: str | None
    current_hash: str
    is_genesis: bool

    # Optional supersession
    supersedes: UUID | None
    supersession_reason: str | None
```

### 3.4 `MarketState`

```python
class MarketState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    built_at: datetime
    latest: Mapping[Metric, NormalizedDatapoint]
    attestation_refs: Mapping[Metric, UUID]
    missing: frozenset[Metric]
    missing_consecutive_days: Mapping[Metric, int]   # For R-004 Illegality
```

### 3.5 `Rule`

```python
class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, arbitrary_types_allowed=True)

    rule_id: str
    clause_ref: str
    severity: Severity                   # Highest severity the rule can emit
    predicate: Callable[[MarketState, ContractState], RuleOutcome]
    required_metrics: frozenset[Metric]
    required_contract_fields: frozenset[str]
    grace_period: timedelta
    version: str
    description: str
```

### 3.6 `RuleOutcome` (new in V1.1)

```python
class RuleOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    fired: bool                          # True if rule produced an event
    severity: Severity | None            # Non-None if fired
    evidence: tuple[Evidence, ...]
    indeterminate: bool                  # True if required data unavailable
    indeterminate_reason: str | None
```

Rules return `RuleOutcome` rather than `bool` so that the engine can distinguish "no trigger" from "could not evaluate".

### 3.7 `TriggerEvent`

```python
class TriggerEvent(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    event_id: UUID
    rule_id: str
    rule_version: str
    clause_ref: str
    severity: Severity
    contract_id: str
    evaluated_at: datetime
    as_of: date
    attestation_ref: UUID
    evidence: tuple[Evidence, ...]
    rules_version: str
```

### 3.8 `Evidence`

```python
class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    kind: Literal["market_datum", "contract_field", "external_default", "mac_indicator"]
    key: str
    value: str
    source: str                          # "oracle" | "irs_engine" | "user_input"
```

### 3.9 `SourceFailure`

```python
class SourceFailure(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    failure_id: UUID
    source_id: str
    metric: Metric
    attempted_at: datetime
    failure_kind: Literal[
        "timeout", "http_4xx", "http_5xx",
        "parse_error", "sanity_band_violation",
        "network_error", "cross_validation_failure"
    ]
    attempts: int
    last_error_message: str              # Truncated to 2000 chars
    source_url: str
    context: dict[str, str]              # Extra: for cross_validation_failure, includes both values
```

---

## 4. Enums and controlled vocabulary (`oracle/config.py`)

```python
class Metric(str, Enum):
    # Overnight rates
    ESTR            = "ESTR"
    # EURIBOR tenors
    EURIBOR_3M      = "EURIBOR_3M"
    EURIBOR_6M      = "EURIBOR_6M"
    EURIBOR_12M     = "EURIBOR_12M"

class Unit(str, Enum):
    PERCENT_PER_ANNUM = "percent_per_annum"
    DECIMAL_FRACTION  = "decimal_fraction"
    BASIS_POINTS      = "basis_points"

class Severity(str, Enum):
    WARNING           = "warning"
    POTENTIAL_TRIGGER = "potential_trigger"
    TRIGGER           = "trigger"
```

### 4.1 Source → metric mapping

```python
# Declared in oracle/config.py
SOURCE_METRICS: dict[str, frozenset[Metric]] = {
    "ecb_sdw_v1":     frozenset({Metric.ESTR}),
    "bdf_webstat_v1": frozenset({Metric.EURIBOR_3M, Metric.EURIBOR_6M, Metric.EURIBOR_12M}),
    "fred_v1":        frozenset({Metric.EURIBOR_3M}),
    "fake_v1":        frozenset(Metric),      # Fake can fixture anything
}
```

### 4.2 Primary / secondary mapping for cross-validation

```python
PRIMARY_SOURCE: dict[Metric, str] = {
    Metric.ESTR:        "ecb_sdw_v1",
    Metric.EURIBOR_3M:  "bdf_webstat_v1",
    Metric.EURIBOR_6M:  "bdf_webstat_v1",
    Metric.EURIBOR_12M: "bdf_webstat_v1",
}

SECONDARY_SOURCE: dict[Metric, str | None] = {
    Metric.ESTR:        None,               # No secondary for ESTR in V1
    Metric.EURIBOR_3M:  "fred_v1",          # Cross-validated
    Metric.EURIBOR_6M:  None,
    Metric.EURIBOR_12M: None,
}

CROSS_VALIDATION_TOLERANCE: dict[Metric, Decimal] = {
    Metric.EURIBOR_3M: Decimal("0.0002"),   # 2 bps
}
```

### 4.3 Sanity bands

Each metric has min/max plausible values expressed in `DECIMAL_FRACTION`.

| Metric        | Min       | Max        | Rationale                              |
|---------------|-----------|------------|----------------------------------------|
| `ESTR`        | `-0.02`   | `0.15`     | -2% floor, 15% ceiling                 |
| `EURIBOR_3M`  | `-0.02`   | `0.20`     | Same philosophy                        |
| `EURIBOR_6M`  | `-0.02`   | `0.20`     | Same                                   |
| `EURIBOR_12M` | `-0.02`   | `0.20`     | Same                                   |

Wide bands catch parsing errors (wrong scale, wrong currency, empty string → zero), not market moves.

---

## 5. Source endpoint specifications (V1)

Verified at implementation time against official docs.

### 5.1 ECB SDW — €STR

- Base: `https://data-api.ecb.europa.eu/service/data`
- Dataset: `EST` (€STR)
- Series: `EST.B.EU000A2X2A25.WT` (Business day, EU area, ISIN, Wholesale rate)
- Query: `?lastNObservations=1&format=jsondata`
- Response: SDMX-JSON 2.0
- Auth: none
- Rate limit: informal, ~10 req/sec is safe

### 5.2 Banque de France Webstat — EURIBOR

- Base: `https://api.webstat.banque-france.fr/webstat-fr/v1/data`
- Dataset: `IR` (Interest rates)
- Series codes (to be verified at implementation time against the BdF Webstat catalogue — exact codes can change, but the principle is stable):
  - EURIBOR 3M: typically under code path `IR.M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA` for monthly averages; daily under a separate `D` frequency code
  - EURIBOR 6M, 12M: analogous
- Format: JSON
- Auth: API key (free, register at https://webstat.banque-france.fr)
- Implementation note: at build time, the developer must fetch the BdF catalogue, identify the exact daily EURIBOR series codes, and record them in `oracle/config.py` with verification date. **Do not hardcode codes based on guesses.**

### 5.3 FRED — EURIBOR cross-validation

- Base: `https://api.stlouisfed.org/fred/series/observations`
- Series IDs:
  - EURIBOR 3M: `EUR3MTD156N` (Euro Interbank Offered Rate, 3-Month, Daily)
- Query: `?series_id={id}&api_key={key}&file_type=json&sort_order=desc&limit=1`
- Auth: free API key from https://fred.stlouisfed.org/docs/api/api_key.html
- Rate limit: 120 requests/minute (generous)

**Configuration:** API key stored in env var `FRED_API_KEY`. If not set, FRED collector is disabled and EURIBOR_3M proceeds without cross-validation (flagged `cross_validated=False` on the attestation).

---

## 6. Storage schema (SQLite)

Append-only. No `UPDATE`, no `DELETE` in production paths.

### 6.1 `attestations`

```sql
CREATE TABLE attestations (
    attestation_id       TEXT PRIMARY KEY,
    sequence_number      INTEGER NOT NULL UNIQUE,
    payload_json         TEXT NOT NULL,
    payload_hash         TEXT NOT NULL,
    previous_hash        TEXT,
    current_hash         TEXT NOT NULL UNIQUE,
    signed_at            TEXT NOT NULL,
    rules_version        TEXT NOT NULL,
    oracle_version       TEXT NOT NULL,
    is_genesis           INTEGER NOT NULL,
    supersedes           TEXT,
    supersession_reason  TEXT
);

CREATE INDEX idx_attestations_signed_at ON attestations (signed_at);
CREATE INDEX idx_attestations_sequence ON attestations (sequence_number);
```

### 6.2 `datapoints`

```sql
CREATE TABLE datapoints (
    datapoint_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    attestation_id       TEXT NOT NULL REFERENCES attestations(attestation_id),
    source_id            TEXT NOT NULL,
    metric               TEXT NOT NULL,
    value                TEXT NOT NULL,           -- Decimal as string, never REAL
    unit                 TEXT NOT NULL,
    as_of                TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    source_hash          TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    cross_validated      INTEGER NOT NULL,
    cross_checked_against TEXT
);

CREATE INDEX idx_datapoints_metric_asof ON datapoints (metric, as_of);
CREATE INDEX idx_datapoints_attestation ON datapoints (attestation_id);
```

### 6.3 `source_failures`

```sql
CREATE TABLE source_failures (
    failure_id           TEXT PRIMARY KEY,
    source_id            TEXT NOT NULL,
    metric               TEXT NOT NULL,
    attempted_at         TEXT NOT NULL,
    failure_kind         TEXT NOT NULL,
    attempts             INTEGER NOT NULL,
    last_error_message   TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    context_json         TEXT NOT NULL
);
```

### 6.4 `trigger_events`

```sql
CREATE TABLE trigger_events (
    event_id             TEXT PRIMARY KEY,
    rule_id              TEXT NOT NULL,
    rule_version         TEXT NOT NULL,
    clause_ref           TEXT NOT NULL,
    severity             TEXT NOT NULL,
    contract_id          TEXT NOT NULL,
    evaluated_at         TEXT NOT NULL,
    as_of                TEXT NOT NULL,
    attestation_ref      TEXT NOT NULL REFERENCES attestations(attestation_id),
    evidence_json        TEXT NOT NULL,
    rules_version        TEXT NOT NULL
);
```

### 6.5 Integrity guarantees

- `sequence_number` gap-free
- `previous_hash` of attestation `N+1` equals `current_hash` of attestation `N`
- `is_genesis = 1` iff `sequence_number = 0` iff `previous_hash IS NULL`
- `verify_chain()` re-derives every hash, refuses on any mismatch

---

## 7. Retry and timeout policy

Identical for every real-network collector:

- Connect timeout: 5 seconds
- Read timeout: 10 seconds
- Total attempts: 3
- Backoff: exponential, base 1 second, factor 2, jitter ±25% (~1s, ~2s, ~4s)
- Retry triggers: `httpx.TimeoutException`, `httpx.ConnectError`, HTTP 5xx, HTTP 429
- Never retry: HTTP 4xx (except 429), JSON parse errors, sanity violations
- On final failure: emit `SourceFailure`, return `None` from `fetch()`, caller never substitutes

---

## 8. Logging

- `structlog`, JSON renderer
- Output: stderr in development; stderr + rotating file in scheduler
- Required fields: `timestamp`, `component`, `action`, `outcome`, `duration_ms`, `trace_id`
- `print()` forbidden outside `scheduler/daily_run.py` user-facing summary

---

## 9. Test strategy

- Unit tests per module, `pytest` + `pytest-asyncio`
- Integration tests marked `@pytest.mark.integration`, skipped in CI by default, hit real endpoints
- Property tests via `hypothesis` for chain integrity
- Replay fixtures under `tests/fixtures/{ecb,bdf,fred}/`, used with `respx` to mock `httpx`
- Coverage: 90% line on `core/`, 100% branch on `attestation.py` and `cross_validator.py`

---

## 10. Bridge to `irs_engine_v2`

```python
def get_contract_state(contract_id: str) -> ContractState: ...
def submit_trigger_event(event: TriggerEvent) -> TriggerReceipt: ...
```

Oracle never calls `close_out()`, `mark_default()`, or any mutating method. If the methods above are missing from `irs_engine_v2`, they are added minimally without refactoring the engine.

---

## 11. Change management

- Rule version bumps on any predicate, threshold, or required-input change
- Aggregate `rules_version` bumps with any rule version change
- `oracle_version` bumps on any change to `core/` or `types.py`
- Schema changes require `oracle/migrations/NNN_description.sql` + upgrade test

End of architecture.
