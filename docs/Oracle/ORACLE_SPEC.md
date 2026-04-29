# ORACLE_SPEC.md — Functional Specification (V1)

**Project** : DerivAI — Oracle module
**Version** : 1.1 (specification, not implementation)
**Author** : Esther
**Status** : Draft for review
**Last updated** : 2026-04-23

---

## 1. Purpose

The Oracle is the **sole authoritative source of external market and legal data** for the DerivAI Smart Legal Contract platform. It publishes **signed, immutable, chained attestations** that the IRS execution engine (`irs_engine_v2`) consumes to evaluate contractual triggers under the ISDA 2002 Master Agreement.

The Oracle exists to answer **one question**: *"What is the verified, timestamped, audit-defensible value of market datum X as of date T, and from which source?"*

Nothing more.

---

## 2. What the Oracle does — and does not — do

### 2.1 In scope (V1)

- Collect market data from **identified, authoritative public sources only** — ECB SDW, Banque de France Webstat, and FRED (Federal Reserve Bank of St. Louis)
- **Cross-validate** critical rates across two independent sources when both are configured for the same metric
- Normalize, validate, and **sign** each datapoint with a SHA-256 hash chained to the previous attestation
- Persist all attestations in an append-only SQLite store
- Expose a **read-only interface** for the IRS engine, the Rules Engine, and the future Copilot
- Evaluate a **declarative set of six Rules** mapped to ISDA clauses section-by-section, and emit `TriggerEvent` objects
- Run on a daily schedule (one refresh per business day per metric)
- Fail loudly when a source is unavailable — never substitute, never invent

### 2.2 Explicitly out of scope (V1)

- Event monitoring via news APIs
- Regulatory watch (EUR-Lex, ESMA, FCA monitoring)
- Automated regulatory impact analysis via LLM
- Full Cross Default with live credit-event feeds (V1 accepts manually recorded defaults only)
- NLP-based MAC detection from unstructured news (V1 MAC is structured-indicator based only)
- CDS / credit spread feeds
- Multi-currency beyond EUR
- FX rates
- Automatic close-out triggering (the Oracle **never** calls `close_out()`; it emits `TriggerEvent` objects only)

### 2.3 Non-goals (never in scope, even V2+)

- **The Oracle is not a decision-maker.** It publishes facts and rule-match events. Contract decisions belong to `irs_engine_v2` and, ultimately, to a human.
- **The Oracle does not autonomously conclude on a MAC event.** Under English law (*IBP v. Tyson*, *Grupo Hotelero Urvasco v. Carey Value Added*), MAC is a qualitative judgment. The Oracle surfaces structured indicia; conclusion always requires human review.
- **The Oracle does not give legal advice.**

---

## 3. Five Invariants (non-negotiable)

### I1 — Immutability

A published attestation is **never** modified. Corrections produce new attestations referencing prior ones via a `supersedes` field. All attestations remain in the store forever.

**Implementation:** all models are `frozen=True`. Any mutation raises `FrozenInstanceError`.

### I2 — Signature and chaining

Every attestation carries:
- `payload_hash` — SHA-256 of the canonical JSON payload
- `previous_hash` — the `current_hash` of the prior attestation (`null` for genesis)
- `current_hash` — SHA-256 of `(payload_hash || previous_hash)`

Tampering invalidates every subsequent hash. `verify_chain()` detects this.

### I3 — Strict layer separation

```
Collectors (I/O)  →  Oracle Core (validation + signing)  →  Rules Engine (predicates)
```

Collectors know HTTP and source formats; they do not know contracts. Oracle Core knows attestations; it does not know HTTP and does not know ISDA. Rules Engine knows ISDA clauses; it does not know HTTP and does not know persistence.

No shared global state. No back-references. Communication only through typed immutable objects.

### I4 — Determinism

Same inputs → same outputs, forever. No `datetime.now()` inside predicates. No randomness. Rule versions are pinned so that re-evaluation five years later produces identical output.

### I5 — Fail loud, fail safe

If a source is unreachable, returns malformed data, violates a sanity band, or fails cross-validation, the Oracle:
- **Does not publish an attestation** for the affected datum
- Emits a structured `SourceFailure` record
- Never substitutes a stale, fallback, or estimated value without an explicit, logged, human-authorized override

Consequence: rules that need the missing datum are indeterminate, not silently skipped.

---

## 4. Scope of V1 (strict)

V1 ships with:

- **ONE contract type**: vanilla EUR Interest Rate Swap
- **THREE real sources**:
  - **ECB SDW** — primary for €STR
  - **Banque de France Webstat** — primary for EURIBOR 3M / 6M / 12M
  - **FRED** (Federal Reserve Bank of St. Louis) — cross-validation source for EURIBOR
- **ONE test source**: FakeCollector reading YAML fixtures
- **SIX rules** (see `ORACLE_RULES.md`):
  - R-001 — Failure to Pay (§5(a)(i))
  - R-002 — Breach of Agreement (§5(a)(ii))
  - R-003 — Cross Default (§5(a)(vi))
  - R-004 — Illegality / rate unavailability (§5(b)(i))
  - R-005 — Tax Event (§5(b)(ii))
  - R-006 — Material Adverse Change (Schedule / ATE)
- **Daily scheduled refresh** at 18:00 CET on TARGET2 business days
- **SQLite persistence**, single file, append-only
- **Structured JSON logging** via `structlog`
- **Full test suite**: unit + integration + chain-integrity property tests

---

## 5. Sources — authoritative identifiers

Each source has a stable, versioned `source_id`:

| `source_id`       | Description                                    | Access       | Metrics covered                       |
|-------------------|------------------------------------------------|--------------|---------------------------------------|
| `ecb_sdw_v1`      | ECB Statistical Data Warehouse, SDMX-JSON 2.0  | Public, free | `ESTR`                                |
| `bdf_webstat_v1`  | Banque de France Webstat REST API              | Public, free | `EURIBOR_3M`, `EURIBOR_6M`, `EURIBOR_12M` |
| `fred_v1`         | Federal Reserve Bank of St. Louis FRED API     | Free (API key, 120 req/min) | `EURIBOR_3M` (cross-validation only)  |
| `fake_v1`         | YAML fixture collector                         | Local only   | All metrics, tests only               |

**Rationale for this source mix:**
- ECB SDW is the authoritative publisher for €STR (it administers €STR)
- BdF Webstat republishes daily EURIBOR fixings under French central-bank authority — this is the free, authoritative path without an EMMI licence
- FRED provides an independent cross-check: if BdF and FRED disagree on EURIBOR 3M by more than tolerance, the Oracle refuses to publish

---

## 6. Cross-validation (new in V1.1)

When two sources are configured for the same metric, the Oracle compares their values:

- **Tolerance**: 2 basis points (0.0002 in decimal fraction) for rate metrics
- **Agreement**: both sources within tolerance → primary value attested with `cross_validated=True`, `cross_checked_against` pointing to secondary
- **Disagreement**: outside tolerance → **neither value attested**, `SourceFailure` of kind `cross_validation_failure` recorded with both values for audit
- **Secondary unavailable**: primary attested with `cross_validated=False` (allowed, but flagged)
- **Primary unavailable**: no attestation even if secondary is available — secondaries do not become primaries silently

Cross-validation applies only to metrics explicitly marked for it in `config.py`. In V1, only `EURIBOR_3M` is cross-validated (BdF primary, FRED secondary). `ESTR` has one authoritative source (ECB) and is not cross-validated.

---

## 7. Refresh cadence

- **Scheduled**: one full refresh per metric per business day (TARGET2 calendar), at 18:00 CET
- **On-demand**: the IRS engine may request a refresh before evaluating a specific clause
- **Never**: faster than the source publishes

---

## 8. What failure looks like — the test case

> At 18:00 CET, the scheduled run starts. ECB SDW returns HTTP 503 for 20 minutes. The Oracle retries three times with exponential backoff; each fails. The Oracle logs a `SourceFailure` for `ESTR`, writes it to the store, and **does not publish any attestation** for that metric. BdF returns EURIBOR_3M normally; FRED returns a value 5 bps higher than BdF (outside tolerance). The Oracle refuses both, writes a `cross_validation_failure`, does not publish EURIBOR_3M either. The IRS engine queries `MarketState` at 18:05, finds both metrics missing, and reports: "Rate data for ESTR and EURIBOR_3M as of 2026-04-23 is unavailable. No rate-dependent rule evaluation possible."

Any implementation that publishes a "stale" or "fallback" or "estimated" value in this scenario **fails the spec**.

---

## 9. Success criteria for V1

V1 is done when:

1. All five invariants (I1–I5) are enforced by tests
2. Six V1 rules (R-001 through R-006) are implemented, each with a test file covering: no-trigger, warning, trigger (or potential_trigger), data-unavailable
3. ECB collector fetches €STR successfully in a real-network integration test
4. BdF collector fetches EURIBOR_3M successfully in a real-network integration test
5. FRED collector fetches EURIBOR_3M successfully
6. Cross-validation passes when BdF and FRED agree (real-network test with a tolerance of 2 bps)
7. Cross-validation test with mocked disagreement refuses attestation and logs failure
8. Chain-integrity property test passes: 1000 randomly generated sequences, zero false positives, zero false negatives
9. Corruption test passes: mutation of one byte in stored payload → `verify_chain()` returns `False`
10. End-to-end pipeline test passes: fixture market data → signed attestations → rule evaluation → `TriggerEvent` delivered
11. `oracle/README.md` documents: how to run, how to add a collector, how to add a rule, how to verify chain integrity

---

## 10. Deferred questions

- Multi-source consensus with weighted voting (V1: simple 2-source agreement, hard tolerance)
- Attestation signing with an asymmetric key (V1: hash-chain only; V2: add Ed25519 signature)
- Cross-process publication (V1: single-process SQLite; V2: event bus)
- Regulatory monitoring architecture (separate V2 module)
- Event/news ingestion architecture (separate V2 module)
- Automated credit-event feeds for full §5(a)(vi)
- NLP-based MAC indicator extraction from unstructured news
- Full §5(a)(vii) Bankruptcy rule with jurisdiction-specific insolvency law

---

End of spec.
