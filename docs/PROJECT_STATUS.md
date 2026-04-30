# DerivAI / Nomos — Project Status

**Last updated**: 30 April 2026
**Owner**: Esther Lafitte (King's College London, financial law)
**Repo**: `~/Desktop/DerivAI/nomos`
**Stack**: Python 3.14 in `.venv`, FastAPI, vanilla HTML/CSS/JS, SQLite

---

## Context for Claude

You are picking up a project mid-flight. Esther is a law student building **DerivAI / Nomos**, a Smart Legal Contract platform for OTC derivatives, as her end-of-year project for a former Linklaters partner. The professor has rigorous standards. The project must be defendable both technically and legally.

Esther has 1 month full-time on this project. As of this status, she is about 5 days in.

The repo has three pillars:

1. **`oracle/` package** — V1 oracle with signed attestations, ECB SDW live data, 6 ISDA rules (R-001 to R-006). Defended by 248 tests.
2. **`backend/`** — FastAPI app with the IRS execution engine (`backend/engine.py`), a v2 router exposing the new oracle (`backend/routers/oracle_v2_router.py`), and the legacy `oracle_v3.py` still wired into the engine for now.
3. **`frontend/`** — three pages now redesigned with the shared design system: `login.html`, `client_portal.html`, `advisor_portal.html`, plus the standalone Oracle showcase at `oracle.html`. Design system at `frontend/assets/nomos.css` + `nomos-ui.js`.

---

## Working agreements (binding)

These are non-negotiable rules established across all prior sessions:

1. **No invented data, ever.** The Oracle never publishes a value when its source is unreachable. Read invariant I5 in `docs/Oracle/ORACLE_SPEC.md`.
2. **One change at a time.** Don't merge phases. Don't refactor surrounding code "while you're there".
3. **Backwards-compatible migrations.** When migrating from `oracle_v3` to the new package, keep the old running until the new is proven. Never big-bang.
4. **Hard constraints**:
   - Don't modify `oracle_v3.py`, `nomos.css`, `nomos-ui.js` without explicit consent
   - Don't introduce new dependencies without consent
   - Don't add purple/violet/glassmorphism (the "AI-slop" palette is out)
   - All values are `Decimal`, never `float`, for any monetary or rate computation
5. **Editorial design philosophy** — see `docs/design/design_notes.md`. Plain language first, jargon as metadata. Newsreader display + Geist sans + Geist Mono. Single ochre accent.
6. **Diagnostic = the user's terminal. Modification = Claude Code.** When diagnosing, lead Esther through commands she runs herself. When modifying, prepare a Claude Code prompt.
7. **Always activate the venv first**: `source .venv/bin/activate` (Esther forgets this often; remind her gently).

---

## Current state — what works

### Oracle V1 (live and tested)

- Daily €STR fetch from ECB Statistical Data Warehouse, signed SHA-256, chained, persisted in `oracle.db`
- Automated daily run at 18:00 via `launchd` (file: `~/Library/LaunchAgents/com.nomos.oracle.daily.plist`)
- 6 rules implemented and tested:
  - R-001 Failure to Pay (ISDA §5(a)(i)) — can emit TRIGGER
  - R-002 Breach of Agreement (§5(a)(ii)) — never auto-TRIGGER, requires repudiation
  - R-003 Cross Default (§5(a)(vi)) — POTENTIAL_TRIGGER only in V1
  - R-004 Illegality / rate unavailability (§5(b)(i))
  - R-005 Tax Event flags (§5(b)(ii))
  - R-006 MAC indicia (§5(a)(vii)) — human-gated, max POTENTIAL_TRIGGER, requires 2+ indicators
- One demo trigger event in DB: R-001 on contract `DEMO-R001`
- 248 tests passing, 5 skipped (network integration tests)
- Page `/oracle` displays everything: chain integrity, latest attestation, recent triggers, KPI strip

### Backend API

- FastAPI app at `backend/api.py`, served via uvicorn on port 8000
- New v2 endpoints under `/api/v2/oracle/*`:
  - `GET /attestations/latest`
  - `GET /attestations?limit=N`
  - `GET /triggers?limit=N&severity=...`
  - `GET /chain/verify`
  - `GET /health`
- All legacy `/api/oracle/*` and `/api/contracts/*` endpoints still functional
- 19+ backend tests passing across 5 test files

### Frontend (all redesigned)

- `/` → `login.html` — Newsreader hero, two role cards, ochre CTA for Advisor
- `/client` → 4-view portal: Today, Contracts, Documents, Profile
- `/advisor` → 5-view portal: Dashboard, Contracts, Audit, Notices, Library
- `/oracle` → standalone showcase page (currently NOT integrated into portal sidebars — see Pending #1)
- All four pages use `frontend/assets/nomos.css` + `nomos-ui.js`
- All four pages tested for absence of legacy AI-slop palette

---

## Current state — what does NOT yet work

### 1. Oracle nav from portals is broken

When clicking "Open oracle" inside a contract detail or anywhere from `/client` or `/advisor`, the user lands on `/oracle` which has no sidebar — they cannot easily go back to the portal. **Solution to implement**: add Oracle as a sidebar item in both portals OR make `/oracle` referrer-aware (showing the appropriate portal chrome). This is part of post-redesign polish.

### 2. Engine still uses legacy oracle for floating-rate calculations

`backend/engine.py:649` still does `from oracle_v3 import OracleV3, RateID`. Floating rate calculations for IRS payments still go through `oracle_v3.py`. This means:

- The Schedule & Payments tab in contract details shows floating rates computed from `oracle_v3`, NOT from the new package
- `_STATIC_FALLBACKS` in `oracle_v3.py` may still be invoked silently for rates that aren't `ESTR` (e.g., the 2.987% EURIBOR 3M visible in the schedule is likely a `_STATIC_FALLBACK` value)
- This violates invariant I5 (no invented data) but is **deliberately preserved** until Step 4 migration

This is a **planned migration step (Step 4)**, not a bug. We deliberately kept it for last because it's the highest-risk migration.

### 3. EURIBOR not live

The new `oracle/` package only fetches `ESTR` from ECB. EURIBOR 3M/6M/12M from Banque de France Webstat (BdFCollector) and from FRED (cross-validation) were specified in `ORACLE_ARCHITECTURE.md` §5.2 but **not yet implemented as live collectors**. They exist as test fixtures via `FakeCollector`. See `docs/Oracle/CLAUDE_CODE_PROMPTS.md` Phases 7b, 7c, 7d for ready-made prompts.

### 4. Layers 2 and 3 not started

The old `oracle_v3.py` had stubs for event monitoring (Layer 2) and regulatory watch (Layer 3) but with hardcoded fake events and frozen regulatory alerts — explicitly out of scope for V1. These layers are deferred until after Step 4 engine migration.

---

## What to do next, in order of priority

### Immediate polish (1-2 days)

- [ ] **Fix Oracle navigation from portals** — add "Oracle" sidebar item in both `/client` and `/advisor`, or make `/oracle` referrer-aware
- [ ] **Implement BdFCollector** for EURIBOR live (per `ORACLE_ARCHITECTURE.md` §5.2 and `CLAUDE_CODE_PROMPTS.md` Phase 7b). Source codes need verification at `https://webstat.banque-france.fr/`
- [ ] **Implement FREDCollector** for cross-validation (Phase 7c). Series ID `EUR3MTD156N`. Requires `FRED_API_KEY` env var. Token bucket limiter for 120 req/min.
- [ ] **Implement cross-validator** at `oracle/core/cross_validator.py` (Phase 7d). 2 bps tolerance.

### Critical — Step 4: Engine migration (2-3 days)

This is the most delicate task remaining. Goal: replace every use of `oracle_v3` in `backend/engine.py` with the new oracle package. Then delete `oracle_v3.py` permanently.

**Approach**:

1. Inventory all call sites in `backend/engine.py` and `backend/api.py` that import or call into `oracle_v3` (currently: `engine.py:649`, `api.py:141`, `api.py:2240`)
2. For each call site, identify what data it needs (rate value, rate metadata, source, timestamp)
3. Provide an equivalent path through `oracle/integration/irs_bridge.py` or directly through `AttestationStore`
4. Replace call by call, with test runs after each
5. Once `backend/engine.py` no longer imports `oracle_v3`, delete `oracle_v3.py` from the repo and from `backend/api.py` imports
6. Remove the legacy `/api/oracle/*` endpoints if not used by frontend

**Key risk**: floating-rate payment computations need a rate at a **specific reset date**. Ensure the new path correctly resolves a historical attestation by date, not just "latest".

**Open product/technical decision**: convert contracts from "EURIBOR 3M" reference to ESTR (the market trend post-2022, EURIBOR is in transition), OR implement BdFCollector first and migrate engine to support both. Esther needs to decide before Claude Code starts the migration.

### Strategic (1-2 weeks): Layers 2 and 3

Only after engine migration is complete. Same rigor as Layer 1: spec → architecture → collectors with retry/backoff → sign and chain attestations → integrate into UI.

**Layer 2 (Event Monitoring) — free authoritative sources only**:
- OFAC press releases (US Treasury) — RSS
- ESMA news — RSS
- Federal Register API (US) — JSON, official
- AMF actualités — RSS
- Banque de France news — RSS
- **NO NewsAPI free tier** (rate-limited, no pro content), **NO stub events**

**Layer 3 (Regulatory Watch)**:
- EUR-Lex API — free, official EU
- ESMA Register
- FCA Handbook updates
- Federal Register

Each alert must be attested with source URL + LLM impact analysis (with citations, human-reviewable). R-006 MAC detection becomes more meaningful once Layer 2 is live (rating downgrades detected from Layer 2 → MAC indicator).

### Final polish (last days)

- Empty states everywhere
- Error handling
- Dark mode toggle (CSS vars already in `nomos.css`)
- Responsive (mobile breakpoints)
- Accessibility (focus states, keyboard nav, aria-labels)

---

## File map (key files only)

```
nomos/
├── docs/
│   ├── Oracle/
│   │   ├── ORACLE_SPEC.md           # Functional spec, 5 invariants
│   │   ├── ORACLE_ARCHITECTURE.md   # Schemas, modules, endpoints, retry policy
│   │   ├── ORACLE_RULES.md          # The 6 rules clause-by-clause
│   │   └── CLAUDE_CODE_PROMPTS.md   # Phase-by-phase prompts (some used, some pending)
│   └── design/
│       ├── design_notes.md          # Design philosophy
│       └── styles.css               # Reference styles (inspiration)
│
├── oracle/                          # The new oracle package, V1 done
│   ├── types.py                     # Pydantic v2 frozen models
│   ├── config.py                    # Sanity bands, source-metric mapping
│   ├── errors.py
│   ├── collectors/
│   │   ├── base.py                  # BaseCollector ABC
│   │   ├── fake.py                  # YAML fixture collector
│   │   └── ecb.py                   # ECB SDW live collector (only this one is wired)
│   ├── core/
│   │   ├── attestation.py           # Sign/chain/verify
│   │   ├── store.py                 # SQLite append-only
│   │   ├── normalizer.py
│   │   └── sanity.py
│   ├── rules/
│   │   ├── engine.py
│   │   ├── registry.py
│   │   ├── calendar.py              # TARGET2 business day calendar (2024-2027 only)
│   │   └── impl/
│   │       ├── r001_failure_to_pay.py
│   │       ├── r002_breach_of_agreement.py
│   │       ├── r003_cross_default.py
│   │       ├── r004_illegality.py
│   │       ├── r005_tax_event.py
│   │       └── r006_material_adverse_change.py
│   ├── integration/
│   │   └── irs_bridge.py            # Read-only access to engine state
│   ├── scheduler/
│   │   └── daily_run.py             # CLI: --fixture or --live-ecb
│   ├── scripts/
│   │   └── seed_demo_contract.py    # Seeds R-001 trigger on DEMO-R001
│   └── tests/                       # 248 passing, 5 skipped
│
├── backend/
│   ├── api.py                       # FastAPI app — DO NOT refactor
│   ├── engine.py                    # IRS engine — STILL USES oracle_v3, MIGRATION PENDING (Step 4)
│   ├── oracle_v3.py                 # LEGACY — to be deleted at Step 4
│   ├── routers/
│   │   └── oracle_v2_router.py      # New v2 endpoints
│   └── tests/
│       ├── conftest.py
│       ├── test_login_page.py
│       ├── test_oracle_page.py
│       ├── test_oracle_v2_router.py
│       ├── test_client_portal_page.py
│       └── test_advisor_portal_page.py
│
├── frontend/
│   ├── login.html                   # Redesigned ✓
│   ├── client_portal.html           # Redesigned ✓ (4 views)
│   ├── advisor_portal.html          # Redesigned ✓ (5 views)
│   ├── oracle.html                  # Redesigned ✓ (standalone showcase)
│   └── assets/
│       ├── nomos.css                # Design system — DO NOT modify without consent
│       └── nomos-ui.js              # Helpers — DO NOT modify without consent
│
├── oracle.db                        # SQLite, contains 2+ attestations and 1 trigger event
└── .venv/                           # Python 3.14 venv
```

---

## Conventions

- **Always activate venv** before running anything: `source .venv/bin/activate`
- **All financial values are `Decimal`**, never `float`
- **All ISDA references are section-level**, e.g., `"ISDA 2002 §5(a)(i)"`
- **All times are UTC** in storage; rendered relative or in user's locale on display
- **Attestations are immutable** — never UPDATE, only INSERT new with `supersedes` field
- **`§1(b) hierarchy clause`** must appear in every legal output (Confirmation > Schedule > MA > Code)

---

## Test commands

```bash
cd ~/Desktop/DerivAI/nomos
source .venv/bin/activate

# Oracle package
pytest oracle/tests/ -q
# Expected: 248 passed, 5 skipped

# Backend
pytest backend/tests/ -v
# Expected: ~19 passing across login, oracle page, v2 router, client portal, advisor portal

# Live ECB fetch (manual)
python -m oracle.scheduler.daily_run --live-ecb --contract-id DEMO-001 --db-path oracle.db

# Server
uvicorn backend.api:app --reload --port 8000
```

---

## Lessons learned from prior sessions (apply going forward)

- Esther frequently forgets to activate venv → reminder helps
- Esther confused `#` comments in copy-paste (zsh interprets `#`) → strip comments
- Long Claude Code outputs can hit "Stream idle timeout" → for files >1500 lines, prompt should explicitly request chunked output with confirmation between chunks
- Esther appreciates: explicit timeline plans, clear "STOP and ask" rules in prompts, screenshot-based validation, incremental visible wins
- Esther uses French naturally in conversation; technical terms in English are fine

---

## When in doubt

- Read `docs/Oracle/ORACLE_SPEC.md` for invariants
- Read `docs/design/design_notes.md` for design philosophy
- Look at how `oracle.html` is built before building any other view
- Look at how the v2 router is built before adding any new route
- **Always ask Esther before**: adding a new dependency, modifying `oracle_v3.py`, changing `nomos.css`, deleting any file

---

End of status. The project is in a strong state — V1 oracle live, four pages redesigned, 248 tests green. The next sensitive step is Step 4 (engine migration). Treat it carefully.
