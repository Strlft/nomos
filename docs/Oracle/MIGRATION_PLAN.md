# MIGRATION_PLAN.md — Replacing `backend/oracle_v3.py` with the `oracle/` package

**Status**: Diagnostic phase (Step 4a). No code changes. Awaiting Esther's review.
**Date**: 2026-05-01
**Author**: Claude (under Esther's brief)

---

## 0. TL;DR

- **9 distinct call sites** across `backend/engine.py` (3) and `backend/api.py` (6) plus a private-symbol import (`_STATIC_FALLBACKS`).
- The actual financial coupling is narrower than the brief implies: `OracleModule._v3` (engine.py:710) is initialised but **never read** anywhere else — it is a dead reference. The real numerical path goes through `OracleModule._fetch_ecb` / `_fallback_estr`, both of which are independent of `oracle_v3`.
- **The `2.987 %` EURIBOR 3M shown in Schedule & Payments is NOT an `oracle_v3._STATIC_FALLBACK`.** It is `OracleModule._fallback_estr`'s hardcoded `estr_base = 0.02891` + ISDA spread `0.000959` ≈ `0.029869`, computed inside `engine.py` itself. The legacy ECB URL on line 697 (`FM/B.U2.EUR.RT0.MM.EURIBOR3MD_.HSTA`) uses a discontinued `B` frequency code and likely 404s, which routes every call to that hardcoded fallback. This finding shifts the migration emphasis: removing `oracle_v3` is the easy half; replacing the live-rate path that drives floating payments is the harder half.
- The new `oracle/` package's `ECBCollector` already maps `EURIBOR_3M / 6M / 12M` (oracle/collectors/ecb.py:64-69). The blocker isn't the collector — it's the scheduler, which currently restricts the `--live-ecb` metric set to `{Metric.ESTR}` (oracle/scheduler/daily_run.py:363).
- **Recommended option for the EURIBOR question: Option C** (migrate engine, support both ESTR + EURIBOR through the new package, keep EURIBOR live via the existing ECB collector — *no BdF rebuild needed*). Justification in Section 4.
- Estimated effort: **3–4 working days** for engine + api migration, including regression fixture + tests. Rules/regulatory layers (events, RegulatoryWatch) are out of V1 scope per ORACLE_SPEC §2.2 — those endpoints can either be deleted or stubbed; that's a product decision for Esther (Section 7, Q1).

---

## 1. Inventory of `oracle_v3` usage

The table below lists every line outside `backend/oracle_v3.py` itself that references the legacy module, plus the `OracleModule._v3` dead-store and the `OracleModule.fetch()` paths that drive financial values today (these don't import `oracle_v3` symbols but are part of the same migration).

| File | Line | Symbol used | What it does | Caller context | Data needed |
|------|------|-------------|--------------|----------------|-------------|
| `backend/engine.py` | 649 | `OracleV3 as _OracleV3, RateID as _RateID` | Module-level lazy import (try/except ImportError) | top-level | None — reference only |
| `backend/engine.py` | 655 | `_OracleV3` | Singleton type annotation | module global | None |
| `backend/engine.py` | 657–665 | `OracleV3(newsapi_key=...)` | Builds the shared `OracleV3` instance on first access | `get_oracle_v3()` | A constructed `OracleV3` |
| `backend/engine.py` | 710 | `get_oracle_v3()` | **Dead store**: `self._v3 = get_oracle_v3()` — never read in any other `OracleModule` method | `OracleModule.__init__` | None used |
| `backend/engine.py` | 3060 | `self.oracle.fetch()` | Live EURIBOR 3M fetch for floating-leg payment | `IRSExecutionEngine.run_calculation_cycle` | EURIBOR 3M rate (Decimal) + status |
| `backend/engine.py` | 3213 | `self.oracle.fetch()` | Live EURIBOR 3M fetch for indicative MTM in §6 close-out | `IRSExecutionEngine.trigger_early_termination` | EURIBOR 3M rate (Decimal) + status |
| `backend/engine.py` | 1166 | `OracleReading` (param type) | Reads `oracle.rate` to compute floating amount = N × (r + spread) × DCF | `CalculationEngine.calculate_floating_amount` | Decimal rate |
| `backend/engine.py` | 2417 | `OracleReading` (param type) | Reads `oracle.rate` to project floating cash flows for close-out NPV | `CloseOutModule.calculate_indicative_mtm` | Decimal rate |
| `backend/api.py` | 140 | `from engine import get_oracle_v3` | Imports the singleton accessor | top-level | Function reference |
| `backend/api.py` | 141 | `RateID, EventSeverity, RateStatus as OracleRateStatus` | Imports legacy enums (note: `OracleRateStatus` is unused after import) | top-level | Enum types |
| `backend/api.py` | 2231 | `get_oracle_v3()` | Returns cached readings for all 9 RateIDs (no live fetch) | `api_oracle_all_rates` (`GET /api/oracle/rates`) | All 9 rate readings (cached) |
| `backend/api.py` | 2236 | `for rid in RateID` | Iterates the 9 legacy rate IDs to assemble response | same | Enum iteration |
| `backend/api.py` | 2242 | `from oracle_v3 import _STATIC_FALLBACKS` | **Private symbol import** — used to fabricate a "STATIC_FALLBACK" record when a rate has never been fetched | same | Decimal fallback dict |
| `backend/api.py` | 2269 | `get_oracle_v3()` | Forces live ECB fetch for all 9 rates | `api_oracle_fetch_rates` (`POST /api/oracle/rates/refresh`) | Fresh `RateReading` map |
| `backend/api.py` | 2273 | `oracle.registry.fetch_many(list(RateID))` | The actual live-fetch call | same | Network calls × 9 |
| `backend/api.py` | 2292 | `get_oracle_v3()` | Returns stored MarketEvents (Layer 2) | `api_oracle_events` (`GET /api/oracle/events`) | Event list + `has_api_key` flag |
| `backend/api.py` | 2297–2301 | `EventSeverity.{LOW,MEDIUM,HIGH}` | Maps querystring `min_severity` to enum | same | Enum values |
| `backend/api.py` | 2305 | `oracle.poll_events()` | Triggers stub-event seeding on first call | same | Side effect |
| `backend/api.py` | 2308 | `oracle.get_events(...)` | Filters by contract_id / severity / window | same | Event list |
| `backend/api.py` | 2327 | `get_oracle_v3()` | Returns RegulatoryAlert list (Layer 3) | `api_oracle_regulatory` (`GET /api/oracle/regulatory`) | Alert list |
| `backend/api.py` | 2331 | `oracle.get_regulatory_alerts(...)` | Filters by contract_type / jurisdiction | same | Alert list |
| `backend/api.py` | 514 | `eng.oracle.oracle_summary()` | Summarises the engine's own `OracleModule` state — **does not touch `oracle_v3`** but is part of the same `/api/oracle/*` surface and is consumed by `client_portal.html:229` and `advisor_portal.html:332` | `api_oracle_latest` (`GET /api/oracle/latest`) | Last reading + status |
| `tests/test_oracle_v3.py` | whole file | `OracleV3, RateID, MarketEvent, EventType, get_oracle_v3` | Unit tests for the legacy module + `get_oracle_v3` singleton | top-level test runner | Whole `oracle_v3` API |
| `backend/tests/test_oracle_page.py` | 36 | string `"oracle_v3"` | **Negative assertion**: serves as a guard that the new `/oracle` page does not leak legacy paths. Keep this test. | `test_oracle_page_does_not_leak_legacy_oracle` | None |
| `.claude/settings.local.json` | 8 | shell command `tests/test_oracle_v3.py` | Permission entry granting Claude permission to run the legacy test. Cleanup, not blocker. | settings | None |
| `docs/PROJECT_STATUS.md` | several | prose mentions | Documentation only — update at the end of the migration. | docs | None |

**Hidden state to flag:**
- `_oracle_v3_singleton` (engine.py:655) is process-wide. Any code path that imports `engine.get_oracle_v3` shares one cache. After migration, the new attestation store is the equivalent shared state — but it lives on disk, not in memory. Different lifetime semantics.
- `OracleV3.event_monitor._stubs_seeded` (oracle_v3.py:604) is a hidden boolean that flips on first `poll_events()` call. The `/api/oracle/events` endpoint relies on this side effect to seed stub events when no `NEWSAPI_KEY` is set. Anything that replaces this endpoint must either preserve "first-call seeds stubs" semantics or change the contract explicitly.

---

## 2. Mapping legacy patterns → new `oracle/` package

For each unique usage pattern, here is the proposed replacement and a coverage verdict.

### 2.1 EURIBOR 3M live fetch for floating-leg payments

- **Legacy path**: `OracleModule.fetch() → _fetch_ecb()` (custom URL on engine.py:697) → falls through to `_fallback_estr` (hardcoded 2.891% €STR proxy + spread, engine.py:808) when ECB returns nothing.
- **Proposed new path**: query `AttestationStore` (oracle/core/store.py) for the latest published `NormalizedDatapoint` with `metric == Metric.EURIBOR_3M`, return its `value` and `as_of` to the engine.
- **Support status**: **Partially supported.**
  - `Metric.EURIBOR_3M` exists in `oracle/config.py:45`.
  - `ECBCollector` already maps it in `oracle/collectors/ecb.py:66`.
  - `AttestationStore` persists it via `daily_run.py`'s pipeline.
  - **Missing piece 1**: `AttestationStore` exposes `get_latest_attestation()` (returns the whole attestation), but **no per-metric query helper** — the engine would have to walk `attestation.datapoints` to pick out EURIBOR_3M. Trivial to add (~10 lines).
  - **Missing piece 2**: in `--live-ecb` mode, `daily_run.py:363` restricts metrics to `{Metric.ESTR}`. EURIBOR is only published when running with `--fixture`. So even with the helper, a live system has no EURIBOR data unless we either (a) enable EURIBOR in the live-ecb branch (one-line change since the collector already supports it), or (b) accept that EURIBOR comes from a fixture in V1. This is the heart of the EURIBOR question (Section 4).
- **Effort**: 0.5 day for the query helper + tests. The "EURIBOR live" toggle is an additional 0.5 day under Option A or C (Section 4).

### 2.2 EURIBOR 3M for §6 close-out indicative MTM

- **Legacy path**: same as 2.1 — `self.oracle.fetch()` invoked from `IRSExecutionEngine.trigger_early_termination` (engine.py:3213).
- **Proposed new path**: same as 2.1.
- **Support status**: same as 2.1. Note that `calculate_indicative_mtm` (engine.py:2417) reads `current_oracle.rate` and also computes an OIS proxy `max(rate − ISDA_spread, 0)` (engine.py:2450). The new package should also publish `Metric.ESTR` so this proxy can be replaced by a direct `Metric.ESTR` lookup — a quality improvement, not strictly part of removing `oracle_v3`. **Out of scope for this migration**; flag as follow-up.
- **Effort**: shared with 2.1.

### 2.3 `/api/oracle/latest` (frontend portals)

- **Legacy path**: `engine.oracle.oracle_summary()` (api.py:514) returning `{current_rate, status, source, fetch_count, last_confirmed, sources_attempted, anomalies_detected}`.
- **Proposed new path**: serve the equivalent payload from the new attestation store: latest EURIBOR_3M datapoint + recent attestation chain integrity status.
- **Support status**: Behaviorally close — `AttestationStore.get_latest_attestation()` covers the data; `verify_integrity()` covers the status. The exact payload shape will change (no more `last_confirmed_rate`, no anomaly counts), so the frontend portals (`client_portal.html:229`, `advisor_portal.html:332`) will need a minor JS update.
- **Effort**: 0.5 day for endpoint rewrite + frontend touch-up.

### 2.4 `/api/oracle/rates` and `/api/oracle/rates/refresh` (9-rate dashboard)

- **Legacy path**: `oracle.registry.latest(rid)` for each of 9 RateIDs; falls through to `_STATIC_FALLBACKS` when never fetched (api.py:2242).
- **Proposed new path**: For the 4 metrics the new package supports (`ESTR`, `EURIBOR_3M`, `EURIBOR_6M`, `EURIBOR_12M`), serve from `AttestationStore`. For the 5 it doesn't (`EUR_SWAP_2Y/5Y/10Y`, `EUR_USD`, `EUR_GBP`), there are three options: (a) remove them from the response, (b) keep them in a "planned" status, (c) build collectors for them in V2.
- **Support status**: **Partial.**
  - 4 of 9 metrics covered by `oracle/`.
  - The new package by design (I5 — fail loud) does not have static fallbacks. Replicating the legacy "fabricate a STATIC_FALLBACK row when never fetched" behaviour would *violate* I5. Don't replicate it. Either omit the rate or return an explicit `MISSING` status.
  - Live refresh: `AttestationStore` is append-only; "refresh" semantics map to "trigger a `daily_run` cycle" rather than a synchronous HTTP-driven fetch. May want to keep the `POST .../refresh` button but have it kick off the Oracle scheduler.
- **Effort**: 1 day for rewrite + frontend reconciliation. **Decision for Esther**: fate of the 5 non-V1 rates (EUR_SWAP, FX) — see Section 7 Q2.

### 2.5 `/api/oracle/events` (Layer 2 — MarketEvents)

- **Legacy path**: `OracleV3.poll_events()` + `get_events()` returning `MarketEvent` rows from NewsAPI or stubs.
- **Proposed new path**: **Not supported** in the new package. ORACLE_SPEC §2.2 explicitly lists "Event monitoring via news APIs" as out of V1 scope.
- **Support status**: **Unsupported by design.** Three honest options:
  1. **Delete the endpoint entirely** — frontend currently uses it on the legacy advisor dashboard; check whether the redesigned client/advisor portals still call it.
  2. **Keep it serving the curated 7-event stub list** under a new path (e.g. `/api/legacy/events`) until V2's news-monitoring module ships.
  3. **Stub it to return `[]` with a `"status": "DEFERRED"`** flag so the frontend degrades gracefully.
- **Effort**: 0.25 day for option 1 or 3; 0.5 day for option 2.

### 2.6 `/api/oracle/regulatory` (Layer 3 — RegulatoryAlerts)

- **Legacy path**: `OracleV3.get_regulatory_alerts(...)` returning hardcoded 26-alert list from `_build_regulatory_db()`.
- **Proposed new path**: **Not supported** in the new package. ORACLE_SPEC §2.2 lists "Regulatory watch" as out of V1 scope.
- **Support status**: **Unsupported by design.** Same options as 2.5. Note that the data here is hand-curated and useful (and was visible in the Oracle showcase Esther just shipped per commit `7c0dec0`). Killing it abruptly may regress visible UI.
- **Effort**: depends on choice. **Decision for Esther**: Section 7 Q1.

### 2.7 The dead `_v3` reference

- **Legacy path**: `OracleModule.__init__` does `self._v3 = get_oracle_v3()` (engine.py:710) but nothing in `OracleModule` ever reads `_v3`. Confirmed by `grep self._v3` returning only that one line.
- **Proposed new path**: simply delete the line. No replacement needed.
- **Support status**: ✅ trivial.
- **Effort**: 1 minute.

### 2.8 The legacy test file

- **Legacy path**: `tests/test_oracle_v3.py` exercises `OracleV3` and `get_oracle_v3()` end-to-end.
- **Proposed new path**: delete after migration. The new package's tests live in `oracle/tests/` and cover the same surface (collectors, store, rules) far more thoroughly.
- **Support status**: ✅ — but verify that the `engine.get_oracle_v3()` singleton round-trip test (test_oracle_v3.py:332) is not asserting something unique that we'd lose. Quick read confirms it's only a smoke check.
- **Effort**: deletion + remove the permission entry from `.claude/settings.local.json:8`. 5 minutes.

---

## 3. Risk analysis

| Call site | Risk | Why |
|-----------|------|-----|
| engine.py:710 (`self._v3 = get_oracle_v3()`) | **Low** | Dead store. Removal cannot change behaviour. |
| engine.py:649 (top-level import) | **Low** | Lazy guarded import; removal flips `_ORACLE_V3_AVAILABLE` to False in places that ultimately don't read it. |
| engine.py:3060 (`run_calculation_cycle` → floating payment) | **High** | Drives the rate that goes into N × r × DCF for every quarterly payment. A 0.1 % rate change on €10 M notional is €25 k per quarter. **Numerical regression test required.** |
| engine.py:3213 (`trigger_early_termination` → MTM) | **High** | Drives the indicative Close-out Amount (§6(e)(i)). Same notional × rate × tenor sensitivity, multiplied across all remaining periods. **Numerical regression test required.** |
| api.py:140-141 (imports) | **Low** | Mechanical replacement. |
| api.py:2231-2258 (`/api/oracle/rates`) | **Medium** | UI-visible. Today the frontend renders 9 rows with status badges; after migration it will render fewer (or differently labelled) rows. Not financial. |
| api.py:2242 (`_STATIC_FALLBACKS` import) | **Medium** | Removing this changes what the dashboard shows for "never-fetched" rates from a synthetic fallback to "missing". Visible regression but matches I5 ("fail loud") — desirable. |
| api.py:2261-2281 (`/api/oracle/rates/refresh`) | **Medium** | Same as 2.4. |
| api.py:2284-2316 (`/api/oracle/events`) | **Medium** | Visible UI feature. Behaviour change is product-driven, not financial. |
| api.py:2319-2356 (`/api/oracle/regulatory`) | **Medium** | Visible UI feature. Same as 2.5. |
| api.py:514 (`/api/oracle/latest`) | **Medium** | Hit by client_portal.html:229 and advisor_portal.html:332 every time the portals load. Payload-shape change → minor JS edit. |
| tests/test_oracle_v3.py | **Low** | Testing the module being removed. Delete with confidence after Step 4 final. |

**The two High-risk call sites are engine.py:3060 and engine.py:3213.** Both feed `OracleReading.rate` into Decimal arithmetic that determines payable amounts. The migration must produce numerically identical rates (or differences that are explained by the legacy fallback path having been replaced by a real source). Section 6 specifies the regression strategy.

---

## 4. The EURIBOR question

The new oracle package supports `Metric.EURIBOR_3M` end-to-end *in code* (config, collector, store, types). But:

- The scheduler (`oracle/scheduler/daily_run.py:363`) hardcodes the live-ecb metric set to `{Metric.ESTR}`.
- Comments in `daily_run.py:280-293` say "EURIBOR live fetch is deferred to a later phase".
- ORACLE_SPEC.md §5 (the V1 spec, version 1.1) lists Banque de France Webstat + FRED as the EURIBOR sources. **But oracle/config.py docstring contradicts this**: BdF stopped publishing EURIBOR on 2024-07-10 and FRED's daily series was discontinued 2022-01-31. The new package therefore points EURIBOR at ECB monthly fixings instead — a Phase-7b revision. This is a real spec/code drift Esther should be aware of (Section 7 Q3).

In other words: building a "BdFCollector" (Option A as written in the brief) would not work — the source no longer exists. The brief's Option A should therefore be reframed as "enable EURIBOR live in the existing ECBCollector" rather than "implement BdFCollector".

### Option A — Enable EURIBOR live in the existing ECBCollector

- **What it takes**: change `daily_run.py:363` from `frozenset({Metric.ESTR})` to include `Metric.EURIBOR_3M` (and 6M/12M if desired). No collector work.
- **Pros**: zero new dependencies; live monthly EURIBOR fixings; preserves existing IRS contracts referencing EURIBOR_3M; fully spec-aligned with I5 (single authoritative source).
- **Cons**: EURIBOR fixings on ECB SDW are *monthly*, not daily. For an end-of-quarter floating-rate determination this is fine (the fixing is the official monthly average); for intra-period MTM it means the rate is stale for up to 30 days. That's already the situation today and matches market practice — not a regression.

### Option B — Convert existing contracts from EURIBOR 3M to ESTR

- **What it takes**: add a `Metric.ESTR`-based floating leg path; migrate DEMO contracts; update Confirmation generator and PDFs; update audit narrative.
- **Pros**: Aligns with the post-2022 market shift away from IBOR rates. ESTR is daily, more granular, single authoritative source (ECB administers it). Cleaner story.
- **Cons**: Substantial scope creep — touches Confirmation, audit, generator, frontend labels, demo fixtures, documentation. Risks distracting from the migration's stated goal (replace `oracle_v3`). Existing `DEMO-001` and any signed/active contracts referencing EURIBOR have their economic terms in the audit trail; changing the underlying after-the-fact is dishonest. New contracts referencing ESTR should be a separate roadmap item, not a forced part of this migration.

### Option C — Migrate engine, support both metrics in the new package, keep EURIBOR as monthly-ECB live (and FakeCollector for tests)

- **What it takes**: extend `daily_run.py`'s live mode to include EURIBOR_3M (essentially Option A) **and** add a EURIBOR fixture (e.g. `oracle/fixtures/demo_euribor_estr.yaml`) for tests / offline demos.
- **Pros**: Both rates available; live in production, fixture in tests; smallest possible surface change to `oracle/`; preserves contract integrity; preserves the V1 fail-loud invariant.
- **Cons**: The "B" / "C" distinction is small in practice; both rely on the existing ECBCollector. The added cost over A is ~30 minutes for the fixture file.

### Recommendation: **Option C**

Justification:
1. **The brief's Option A as written presupposes a non-existent BdF feed.** Reframed as "enable EURIBOR via existing ECBCollector" the work shrinks to a one-line scheduler change.
2. **Option B is too big.** It changes contract economics, not just plumbing.
3. **Option C** delivers the same engine migration as A, plus a fixture so tests and demos don't depend on the network. It is the smallest delta that keeps both metrics live, leaves contract economics untouched, and respects I5.

**This is the option I am recommending unless Esther decides otherwise.** Whichever option you pick, the engine migration steps in Section 5 are the same — only Section 5 Step 4 (rate-source enablement) differs.

---

## 5. Step-by-step migration plan

Each step touches at most one file (or a tightly coupled pair). Each is rollback-safe by `git checkout` of that single file. Each has an explicit pass/fail test.

The order is risk-ascending: dead-code first, then wiring, then UI endpoints, then the High-risk financial path, then deletion.

### Step 1 — Remove the dead `_v3` reference in `OracleModule`

- **File**: `backend/engine.py` only (line 710).
- **Action**: Delete `self._v3 = get_oracle_v3()` and the comment on lines 709. Don't yet touch `get_oracle_v3()` itself — `api.py` still imports it.
- **Test**: `pytest backend/tests/` — must pass unchanged. Bonus: `python -c "from engine import OracleModule; m = OracleModule.__init__"` does not raise.
- **Rollback**: `git checkout backend/engine.py`.
- **Risk**: Low.

### Step 2 — Add a per-metric query helper to `AttestationStore`

- **File**: `oracle/core/store.py`.
- **Action**: Add `def get_latest_datapoint(self, metric: Metric) -> NormalizedDatapoint | None`. It walks `get_latest_attestation().datapoints` and returns the matching one. Keep it small; pure function.
- **Test**: Add a unit test in `oracle/tests/` that signs an attestation containing `Metric.EURIBOR_3M` and asserts `store.get_latest_datapoint(Metric.EURIBOR_3M)` returns the right value. `pytest oracle/tests/`.
- **Rollback**: `git checkout oracle/core/store.py oracle/tests/test_store.py`.
- **Risk**: Low.

### Step 3 — Capture the regression fixture **before** any High-risk change

- **File**: new file `tests/fixtures/oracle_migration_baseline.json`.
- **Action**: Run a deterministic calculation cycle on `DEMO-001` (or whichever contract is loaded by default in `_engines`) with `rate_override=Decimal("0.025")` and a hardcoded `as_of` date, capture all eight quarterly periods' `{period_number, oracle_rate, oracle_status, oracle_source, fixed_amount, floating_amount, net_amount, net_payer, calculation_fingerprint}`. Also run `trigger_early_termination` on a copy and capture the close-out result. Write to JSON. **Do not invoke any live oracle** for this baseline — use `rate_override` so the result is purely a function of the engine math, not the rate source. This fixture proves Steps 1–2 didn't change engine arithmetic.
- **Then**: a *second* fixture, `oracle_migration_legacy_live.json`, captured *with* the legacy `OracleModule.fetch()` allowed to run (no rate_override) — this captures whatever the legacy behaviour actually returns today (likely the 2.987% fallback), so we have a "before" snapshot that is honest about the existing buggy state.
- **Test**: a checked-in script `tests/oracle_migration_capture.py` that re-runs the capture and prints a diff. The fixture itself is the test artefact.
- **Rollback**: delete the fixtures.
- **Risk**: Low (read-only).

### Step 4 — Enable EURIBOR_3M live collection in the daily scheduler (Option C)

- **File**: `oracle/scheduler/daily_run.py` (line ~363) — and a new `oracle/fixtures/demo_euribor_estr.yaml`.
- **Action**: Change `metrics = frozenset({Metric.ESTR})` to `frozenset({Metric.ESTR, Metric.EURIBOR_3M})`. Update the docstring on lines 280–293 to reflect the new live set. Add a fixture YAML containing both metrics for offline tests.
- **Test**:
  - Offline: `python -m oracle.scheduler.daily_run --fixture oracle/fixtures/demo_euribor_estr.yaml --contract-id DEMO-001 --db-path /tmp/test.db` exits 0 and the resulting attestation contains both metrics.
  - Live: `python -m oracle.scheduler.daily_run --live-ecb --contract-id DEMO-001 --db-path /tmp/test_live.db` returns an attestation with both metrics. (Will require a working internet connection.)
- **Rollback**: revert the daily_run.py change.
- **Risk**: Medium (network-dependent; ECB monthly EURIBOR series may have its own quirks).
- **Skip if Esther chooses Option A**: same step, just chooses metric set.
- **Skip entirely if Esther chooses Option B**: this step is replaced by an ESTR-only rewrite of the floating leg.

### Step 5 — Replace `OracleModule` internals with a thin facade over `AttestationStore`

- **File**: `backend/engine.py` (the `OracleModule` class, ~150 lines).
- **Action**: Rewrite `OracleModule.fetch()` to read from `AttestationStore.get_latest_datapoint(Metric.EURIBOR_3M)`. Map the result back to an `OracleReading` for type compatibility (do not change `OracleReading` — every caller depends on it). When the store has no datapoint, `OracleModule.fetch()` should raise an explicit error rather than silently returning a fallback (per I5). Remove `_fetch_ecb`, `_fetch_emmi`, `_fallback_estr`, the hardcoded `0.02891` €STR proxy, and the `ECB_URL` constant.
- **Important**: `rate_override` (engine.py:3046) must keep working — that's the testing escape hatch.
- **Test**:
  - `python tests/oracle_migration_capture.py` — compare against `oracle_migration_baseline.json` (Step 3). Numeric values must be **identical** because the override path is unchanged.
  - Fresh capture against the live attestation store, compared against `oracle_migration_legacy_live.json`. Differences are expected — they should reduce to: (a) `oracle.source` value changes from `ECB_SDW`/`ISDA_2021_ESTR_PLUS_SPREAD`/`ISDA_2021_STATIC_FALLBACK` to `ecb_sdw_v1` (new collector source ID); (b) the rate value changes from the hardcoded 2.987% fallback to whatever ECB SDW returns for the latest monthly EURIBOR fixing. **Document this difference in `tests/fixtures/MIGRATION_DIFF_NOTES.md` so future regressions can tell signal from noise.**
  - `pytest backend/tests/ oracle/tests/`.
- **Rollback**: `git checkout backend/engine.py`.
- **Risk**: **High.** Two financial paths converge here: floating-leg payments and §6 close-out MTM. Numerical regression must be controlled.

### Step 6 — Migrate `/api/oracle/latest` to read from `AttestationStore`

- **File**: `backend/api.py` (the `api_oracle_latest` function and its route registration).
- **Action**: Rewrite to call `AttestationStore.get_latest_datapoint(Metric.EURIBOR_3M)` and shape a payload similar enough to the legacy `oracle_summary()` that the frontend doesn't need surgery. Drop `last_confirmed_rate`/`anomalies_detected` — the new chain has different semantics.
- **Test**:
  - `curl localhost:8000/api/oracle/latest` returns 200 with the expected shape.
  - Open `client_portal.html` and `advisor_portal.html` in a browser; verify the rate widget still renders.
  - `pytest backend/tests/test_oracle_page.py` still passes (it should, since this test guards the new `/oracle` page only).
- **Rollback**: `git checkout backend/api.py frontend/client_portal.html frontend/advisor_portal.html`.
- **Risk**: Medium (UI-visible).

### Step 7 — Reroute `/api/oracle/rates` and `/api/oracle/rates/refresh`

- **File**: `backend/api.py`.
- **Action**: Implement `api_oracle_all_rates` against `AttestationStore`, listing only the 4 supported metrics. Drop the `_STATIC_FALLBACKS` import. For `api_oracle_fetch_rates`, decide between (a) deleting it (the daily scheduler is the new refresh mechanism) or (b) having it shell out to `daily_run.py` synchronously. Default to (a) — match V1 semantics.
- **Test**: `curl localhost:8000/api/oracle/rates` returns 4 metrics; `pytest backend/tests/`.
- **Rollback**: revert api.py.
- **Risk**: Medium (UI labels change; old "9 rates" dashboard now shows 4).

### Step 8 — Resolve `/api/oracle/events` and `/api/oracle/regulatory`

- **File**: `backend/api.py`.
- **Action**: **Decision-required.** Implement Esther's choice from Section 7 Q1. Default plan if she doesn't answer: stub both endpoints to `{"status": "DEFERRED", "items": []}`, leaving the 26 hardcoded regulatory alerts and the 7 stub events as "removed in V1; will return in V2". Update the frontend to hide the panels rather than show empty.
- **Test**: pytest + manual smoke.
- **Rollback**: revert api.py + frontend.
- **Risk**: Medium (UI regression; user-facing).

### Step 9 — Remove `get_oracle_v3` and the `oracle_v3` imports from `api.py` and `engine.py`

- **Files**: `backend/api.py` (lines 137–145), `backend/engine.py` (lines 645–665).
- **Action**: After Steps 5–8 complete, nothing references `get_oracle_v3()` or any `oracle_v3` symbol. Delete the lazy imports, the singleton, and the accessor. Update the module docstrings on `engine.py` lines 633–645 to remove the BACKWARD-COMPATIBILITY SHIM section.
- **Test**: `python -c "import backend.api; import backend.engine"` raises no ImportError. `pytest backend/tests/ oracle/tests/`.
- **Rollback**: revert both files.
- **Risk**: Low (mechanical, since callers were rewritten in 5–8).

### Step 10 — Delete `tests/test_oracle_v3.py`

- **File**: `tests/test_oracle_v3.py`.
- **Action**: `git rm tests/test_oracle_v3.py`. Also remove the permission entry on `.claude/settings.local.json:8` (purely cosmetic — won't break anything).
- **Test**: `pytest tests/` — count of tests drops, none fail.
- **Rollback**: `git checkout`.
- **Risk**: Low.

### Step 11 — Delete `backend/oracle_v3.py`

- **File**: `backend/oracle_v3.py`.
- **Action**: `git rm backend/oracle_v3.py`. Verify no remaining references with `grep -rn 'oracle_v3' --include='*.py' --include='*.md' --include='*.html'`. If any remain (likely `docs/PROJECT_STATUS.md` mentions), fix the docs.
- **Test**: `python -c "import backend.api; import backend.engine"`; full app smoke (`uvicorn backend.api:app` + browser); `pytest`.
- **Rollback**: `git checkout`.
- **Risk**: Low (final mechanical step; everything that depended on it is already migrated).

---

## 6. Test strategy

### Reference contract
**`DEMO-001`** — the contract default-seeded by `oracle/scheduler/daily_run.py:_build_demo_engine()`. This is the contract used in the Oracle scheduler's own E2E test. Rationale: it is fully specified by `SwapParameters()` defaults, so the regression fixture is reproducible from a clean checkout.

### Reference computations to capture
For DEMO-001, capture these values both before migration begins (Step 3) and after each High/Medium-risk step:

1. **Per-period values** for all 8 quarterly periods (assuming a 2-year termination):
   - `oracle.rate` (Decimal)
   - `oracle.status` (string)
   - `oracle.source` (string)
   - `period.fixed_amount`
   - `period.floating_amount`
   - `period.net_amount`
   - `period.net_payer.value`
   - `period.calculation_fingerprint` (SHA-256 of the period's fp_data — this is the canonical regression key)

2. **§6 close-out result** at a fixed ETD (e.g. `params.effective_date + timedelta(days=200)`):
   - `early_termination_amount`
   - `payable_by`
   - `mtm_rate_used`
   - `calculation_fingerprint`

### Two baselines, not one
Capture **two** baselines in Step 3:

- **`oracle_migration_baseline_override.json`** — using `rate_override=Decimal("0.025")`. This isolates engine arithmetic from rate source. Steps 1, 5, 9 must produce **byte-identical** results vs this baseline.

- **`oracle_migration_baseline_legacy_live.json`** — letting `OracleModule.fetch()` run normally (probably hits the 2.987 % fallback). This captures the *current observed behaviour* including the bug. Step 5 will diff against this; differences are expected and must be explained in `tests/fixtures/MIGRATION_DIFF_NOTES.md` (the only acceptable explanation: "rate value changes from hardcoded 2.987% legacy fallback to live ECB monthly fixing").

### Acceptance criteria per step

- After Step 1 (dead-store removal): override-baseline diff must be zero.
- After Step 2 (store helper added, no engine change): override-baseline diff must be zero.
- After Step 3 (fixture captured): N/A.
- After Step 4 (scheduler enables EURIBOR): the SQLite store contains EURIBOR_3M datapoints. Engine still uses legacy path, so override-baseline diff is still zero.
- After Step 5 (engine reads from store): override-baseline diff is zero. Live diff vs `oracle_migration_baseline_legacy_live.json` is non-zero — the difference must be exactly the rate-value change documented in `MIGRATION_DIFF_NOTES.md`. Re-derive the floating amounts using the new rate and confirm they match.
- After Steps 6–9 (API rewrites): all engine fingerprints unchanged from Step 5 baseline.
- After Steps 10–11 (deletions): no behavioural change; `pytest` passes.

### CI integration
Add `tests/oracle_migration_capture.py` as a script invokable by `pytest tests/test_oracle_migration.py` so the regression check runs with the rest of the suite.

---

## 7. Open questions for Esther

1. **EURIBOR question (Section 4) — confirm Option C?** I recommend Option C (engine migrates; EURIBOR_3M enabled live in the existing ECBCollector; fixture added for offline tests). Are you on board, or do you prefer A or B? Note: the brief's Option A presupposes a BdFCollector but BdF stopped publishing EURIBOR on 2024-07-10 — so "Option A" effectively becomes "enable the existing ECBCollector for EURIBOR" anyway.

2. **Fate of the 5 non-V1 rates (`EUR_SWAP_2Y/5Y/10Y`, `EUR_USD`, `EUR_GBP`)** in the `/api/oracle/rates` dashboard:
   - (a) Drop them from the response (simplest; UI shows fewer cards).
   - (b) Keep them with a "PLANNED" status badge (preserves UI symmetry; promises future work).
   - (c) Punt to V2 — explicitly mark them as out of scope and remove the rate cards from the redesigned showcase you just shipped.
   I'd recommend (a) for honesty.

3. **ORACLE_SPEC v1.1 vs the actual `oracle/config.py` source mix.** The spec on disk (`docs/Oracle/ORACLE_SPEC.md` §5) lists BdF + FRED for EURIBOR with cross-validation. The code has been silently revised in Phase 7b to read EURIBOR from ECB monthly only (no cross-validation). The spec is now stale by one phase. Should I update it as part of this migration, or is that a separate doc-maintenance task?

4. **The `/api/oracle/events` and `/api/oracle/regulatory` endpoints** (Layer 2/3) are out of V1 scope per the spec. But the redesigned advisor portal and Oracle showcase you shipped in commits `7c0dec0` and `cd46c2a` may still consume them — I haven't verified the new frontend wiring. Three options for handling these endpoints in Step 8:
   - (a) Delete them outright. Cleanest; possibly visible regression on advisor dashboard.
   - (b) Keep them serving the legacy stub data under a `/api/legacy/*` prefix for the V2 transition window.
   - (c) Stub them to return `{"status": "DEFERRED", "items": []}` and update the frontend to hide the panels gracefully.
   Default plan is (c) unless you say otherwise.

5. **`/api/oracle/rates/refresh` semantics.** Today it triggers a synchronous live ECB fetch. In the new package, "refresh" maps to "run a daily scheduler cycle". Synchronous shell-out from a Flask request is ugly. Acceptable to delete the refresh button entirely and document that fresh data lands once a day at 18:00 CET? Or do you want a manual trigger preserved for demos?

6. **External dependencies on `oracle_v3`.** Are there any scripts, cron jobs, CI pipelines, or ad-hoc analytics tools outside the repo that import from `oracle_v3`? Inside the repo I see only the files inventoried in Section 1. If you have any external consumers I haven't seen, the deletion in Step 11 will break them.

7. **Order vs urgency.** This plan is risk-ascending — High-risk financial paths come last. If you'd rather fix the visible "2.987 % fallback" bug first (because it's customer-visible) and accept the integration risk, the plan would reorder Step 5 ahead of Steps 6–8. Confirm preference.

---

## Effort summary

| Step | Effort | Risk | Blocking decision |
|------|--------|------|-------------------|
| 1 — Remove dead `_v3` | 1 min | Low | none |
| 2 — Add store query helper | 0.5 day | Low | none |
| 3 — Capture regression fixtures | 0.5 day | Low | none |
| 4 — Enable EURIBOR live | 0.5 day | Medium | Q1 |
| 5 — Migrate `OracleModule.fetch` | 1 day | **High** | none |
| 6 — Migrate `/api/oracle/latest` | 0.5 day | Medium | none |
| 7 — Migrate `/api/oracle/rates*` | 1 day | Medium | Q2, Q5 |
| 8 — Resolve events/regulatory | 0.25–0.5 day | Medium | Q4 |
| 9 — Remove `get_oracle_v3` shim | 0.25 day | Low | none |
| 10 — Delete legacy test | 5 min | Low | none |
| 11 — Delete `oracle_v3.py` | 0.25 day | Low | Q6 |

**Total: 4–5 working days assuming Esther's decisions in Section 7 land cleanly. Reduce by ~1 day if events/regulatory are simply deleted (Q4 → option (a)).**

End of plan.
