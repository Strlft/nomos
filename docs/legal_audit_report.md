# Nomos IRS Platform — End-to-End Legal Practitioner Audit

**Audit Date:** 2026-04-20 / 2026-04-21  
**Auditor Persona:** 15+ year Magic Circle derivatives lawyer (ISDA expert)  
**Platform Version:** SLC Execution Engine v0.2  
**Contract Tested:** EUR 15M vanilla IRS, 3.50% fixed vs EURIBOR 3M, 2Y, English Law, MTPN, CSA, Cross-default  
**Parties:** Alpha Corp S.A. (FR / Party A — fixed payer) × Beta Fund Ltd (GB / Party B — floating payer)

---

## Executive Summary

The engine is technically sophisticated and legally literate for an early prototype. The ISDA 2002 lifecycle is meaningfully modelled: grace periods are correct, §2(a)(iii) suspension works as a dynamic condition precedent (not a one-way flag), the close-out waterfall follows the three-step §6(e)(ii) structure, and the audit trail has SHA-256 chain integrity. The netting opinion module cites Lomas [2012] EWCA Civ 419 correctly.

However, three P0 bugs were found and fixed during this audit:
1. **Governing law case-sensitivity** — English law contracts were assessed under New York Law netting opinion.
2. **Confirmation PDF: wrong fixed amounts** — all periods showed EUR 80,000 (hardcoded placeholder) instead of the computed EUR 131,250.
3. **Confirmation PDF: wrong first payment date** — showed "30 July 2026" (effective_date + 90 days) instead of the correct MODFOL-adjusted "03 August 2026".

All three P0 issues are fixed in this commit. The platform is not production-ready for a number of P1/P2 reasons documented below, but it is sound enough for a demo with the fixes applied.

---

## 1. Contract Creation

**Flow:** `POST /api/contracts` → schedule generation → netting opinion → DD workflow init → Confirmation PDF

**Result:** PASS (post-fix)

### 1.1 Governing Law — P0 BUG (FIXED)

**File:** `backend/engine.py:2849`, `backend/generate_contract_pdf.py:99`, `backend/generate_confirmation_pdf.py:443`

**Problem:** The governing law is stored as `"English Law"` (from the frontend) or can arrive as `"ENGLISH"` from direct API calls. The check `"English" in "ENGLISH"` returns `False` in Python (case-sensitive). This caused:
- Netting opinion to run under New York Law for English law contracts
- PDF generators to use "New York law" language in hierarchy clauses

**Fix:** Changed all checks to `"english" in governing_law.lower()`.

**Before (wrong netting opinion):**
```
Governing Law (§13 ISDA 2002): New York Law  ← WRONG for English law contract
```

**After (correct):**
```
Governing Law (§13 ISDA 2002): English Law  ✓
Recommendation: §2(a)(iii) perpetual suspension applies (Lomas v JFB Firth Rixson [2012] EWCA Civ 419)
```

### 1.2 Netting Opinion Assessment — PASS

For FR × GB under English Law:
- Party A (France): CLEAN, SPECIFIC_STATUTE, LIMITED_SUSPENSION, GREEN
- Party B (England & Wales): CLEAN, GENERAL_PRINCIPLES, PERPETUAL_SUSPENSION, GREEN
- Overall: GREEN — netting enforceable

Warnings correctly flag:
- Code monétaire et financier sauvegarde / redressement risk
- UK Banking Act 2009 special resolution regime temporary stay
- §2(a)(iii) ASYMMETRY: FR = LIMITED, GB = PERPETUAL — this is a real legal issue and is correctly flagged

The asymmetry warning is legally important: under Lomas, Party B (GB entity) benefits from perpetual suspension while Party A's (FR entity) suspension right may be more limited. The engine flags this correctly.

### 1.3 DD Workflow — PASS

12 contract-specific + 20 entity-level documents (10 per party) initialised. Workflow state machine: `KYC_PENDING → KYC_VALIDATED → DOCS_PENDING → DOCS_VALIDATED → READY_TO_SIGN`. Blocking gate on signing (30 docs must be validated). Demo mode auto-validates all 32 correctly.

---

## 2. Period Schedule

**Result:** PASS

| Period | Start | End | Payment Date | Offset |
|--------|-------|-----|-------------|--------|
| P1 | 2026-05-01 | 2026-08-01 | 2026-08-03 | +2d |
| P2 | 2026-08-01 | 2026-11-01 | 2026-11-02 | +1d |
| P3 | 2026-11-01 | 2027-02-01 | 2027-02-01 | +0d |
| P4 | 2027-02-01 | 2027-05-01 | 2027-05-04 | +3d |
| P5 | 2027-05-01 | 2027-08-01 | 2027-08-02 | +1d |
| P6 | 2027-08-01 | 2027-11-01 | 2027-11-01 | +0d |
| P7 | 2027-11-01 | 2028-02-01 | 2028-02-01 | +0d |
| P8 | 2028-02-01 | 2028-05-01 | 2028-05-02 | +1d |

Modified Following Business Day Adjustment (TARGET2 + London) applied correctly. P1 payment on Aug 3 (Aug 1 falls on Saturday → Monday Aug 3). P4 payment on May 4 (May 1 is a bank holiday). P8 payment on May 2 (May 1 public holiday). All correct.

---

## 3. Confirmation PDF

**Result:** PASS (post-fix)

### 3.1 Fixed Amount — P0 BUG (FIXED)

**File:** `backend/generate_confirmation_pdf.py:257`

**Problem:** Hardcoded `80000` as a placeholder with comment "simplified — in production from engine". The PDF schedule table showed EUR 80,000 for all 8 periods — materially wrong vs the correct EUR 131,250.

**Correct calculation (30/360 quarterly):**
```
EUR 15,000,000 × 3.500% × 90/360 = EUR 131,250.00 per period
```
(30/360 from any month-end to 3 months later = 90 days exactly)

**Fix:** Added `_compute_fixed_amount(params, period)` helper that uses the 30/360 day count fraction from the period's dates, or the pre-calculated `period.fixed_amount` if available.

### 3.2 First Payment Date — P0 BUG (FIXED)

**File:** `backend/generate_confirmation_pdf.py:221`

**Problem:** `effective_date + timedelta(days=90)` = 2026-07-30 ("30 July 2026") instead of the correct MODFOL-adjusted date of 2026-08-03.

**Fix:** Uses `payment_schedule[0].payment_date` when the period schedule is available (it always is at creation time).

### 3.3 Legal Content Review

The Confirmation reads competently. Hierarchy clause at §6 is correct per §1(b) ISDA 2002. The 2006 ISDA Definitions incorporation by reference is correct. The §2(c) MTPN election is correctly stated as "Applicable from the Effective Date."

**Issue (P1 — not fixed):** The text says "Fixed Rate Payer Payment Dates: Quarterly, commencing 03 August 2026, up to and including the Termination Date." This is technically incomplete — a proper ISDA Confirmation should specify the Payment Dates as a list or as "each date falling 3 months after the preceding Payment Date, subject to adjustment" rather than just "quarterly, commencing." However, for a prototype this is acceptable.

**Issue (P2):** The `[date]` placeholder in "ISDA 2002 Master Agreement dated as of [date]" is never populated — the MA date is not captured anywhere in the data model. In production this would need to reference the actual date of the Framework Agreement.

---

## 4. Signing Readiness / Bilateral Execution

**Result:** PASS (with caveat)

The signing gate correctly blocks execution until all 30 pre-signing documents are validated. Demo mode correctly bypasses this gate with an appropriate banner.

**Caveat (P2):** In `advisor_managed` mode, Party B's signature alone activates the contract (state → ACTIVE). There is no bilateral signing mode — Party A's signature is not required. For a real derivatives transaction, bilateral execution of the Confirmation is mandatory. The system should support a `bilateral` mode where both parties must sign before ACTIVE state is reached. The current `advisor_managed` mode is appropriate for demo purposes but should be clearly labelled as such.

---

## 5. §5 Grace Periods

**Result:** PASS — all four grace/waiting periods are correct.

| Event | Period | ISDA 2002 | Engine |
|-------|--------|-----------|--------|
| §5(a)(i) Failure to Pay | 1 LBD | 1 LBD | ✓ |
| §5(a)(ii) Breach of Agreement | 30 calendar days | 30 calendar days | ✓ |
| §5(b)(i) Illegality | 3 LBD | 3 LBD | ✓ |
| §5(b)(ii) Force Majeure | 8 LBD | 8 LBD | ✓ |

Business day calendar uses TARGET2 + London — correct for a EUR IRS between a French and English entity.

---

## 6. Period Calculation — Day Count

**Result:** PASS

Oracle: ECB SDW → EMMI → €STR + ISDA 2021 Benchmark Fallback (2.987% in test environment, ECB unavailable). The fallback waterfall is correctly implemented per ISDA 2021 Protocol.

P1 calculation (EURIBOR = 2.987%, fallback):
```
Fixed leg (30/360): EUR 15M × 3.500% × 90/360 = EUR 131,250.00  ✓
Float leg (ACT/360): EUR 15M × 2.987% × 92/360 = EUR 114,497.83  ✓
Net (§2(c) netting): EUR 16,752.17 payable by Party A  ✓
```

Day count conventions are correct market standard for EUR IRS:
- Fixed leg: **30/360** ✓
- Floating leg (EURIBOR): **ACT/360** ✓

The oracle fallback (2.987%) is fixed-point from the ISDA 2021 €STR + spread. In a real deployment you'd want the live ECB rate — but the fallback is correctly documented and legally appropriate.

**Issue (P1):** There is no way to inject a test rate into `api_execute_period()`. The function always calls `self.oracle.fetch()`. For testing and demo scenarios this is problematic: every test run uses the ISDA 2021 fallback rate (2.987%), which may not match the demo narrative. A `rate_override` parameter on `api_execute_period()` (or a separate demo-mode oracle stub) would improve testability.

---

## 7. §2(a)(iii) Suspension

**Result:** PASS

`EoDMonitor.is_suspended` is a dynamic property computed from `active_eods` — it is not a one-way flag. This is the correct implementation of §2(a)(iii): suspension lifts automatically when all EoDs are cured.

Test: `declare_breach_of_agreement(PARTY_B, today=2026-12-15)` (43 days after breach date, past 30-day grace) → `is_suspended = True`. Executing P2 via `api_execute_period()` returns `status: "SUSPENDED", isda_ref: "§2(a)(iii) ISDA 2002"`.

The §2(a)(iii) implementation is one of the strongest parts of the engine — correctly implements the Lomas doctrine.

**Issue (P1):** No API endpoint to trigger EoD events. `declare_breach_of_agreement()` and similar methods are only accessible on the engine object directly. In production, there would need to be `POST /api/contracts/{id}/eod/breach`, `/eod/bankruptcy` etc., each with appropriate human gates.

**Legal accuracy issue (P1):** The §12 notice generated for Failure to Pay contains the line:
> "we are exercising our right under §2(a)(iii) to suspend all further payments"

This is legally inaccurate. §2(a)(iii) is a **condition precedent**, not an affirmative right to suspend. The non-defaulting party need not elect to suspend; the obligation to pay simply does not arise while an EoD or PEoD exists. Per *Lomas v JFB Firth Rixson* [2012] EWCA Civ 419 at [43]: "the condition precedent in Section 2(a)(iii) operates automatically." The notice template should be corrected to: "By reason of the occurrence and continuation of the Event of Default, the condition precedent in §2(a)(iii) is not satisfied and accordingly payment obligations under the Agreement do not arise. We preserve all rights available to us under the Agreement..."

---

## 8. §6(e) Close-Out Waterfall

**Result:** PASS (arithmetic) / WARNING (methodology)

Test: `trigger_early_termination(BREACH_OF_AGREEMENT, PARTY_A, ETD=2026-12-20)` after P1 executed and P2 suspended.

```
Unpaid Amounts (A): EUR 0.00
Unpaid Amounts (B): EUR 0.00
Close-out Amount (Party A determination): EUR -104,431.86
Close-out Amount (Party B determination): EUR  +93,181.86
Early Termination Amount: EUR 104,431.86  payable by PARTY_A
```

Waterfall structure is correctly three-step:
1. Unpaid Amounts (§6(e)(i))
2. Close-out Amount (§6(e)(ii))
3. Net = Early Termination Amount

The arithmetic checks out: UA(0) + COA(-104,431.86) = ETA(104,431.86) payable by A. Correct sign convention.

**WARNING (P1 — methodology):** The close-out calculation explicitly states: "PROTOTYPE: Using simplified replacement cost. Production requires market quotations." Under ISDA 2002 §6(e)(ii), the Determining Party determines the Close-out Amount by reference to market quotations or, if not available, its reasonable good faith estimate. A flat-curve NPV proxy is not a compliant methodology for production use. The divergence between Party A's determination (-104,431.86) and Party B's counter-determination (+93,181.86) is ~11%, which is an unacceptably large spread — in practice this would trigger a dispute. This must be replaced by a proper mid-market curve before production.

**Issue (P1):** The system exposes both parties' Close-out Amount determinations. Under ISDA 2002, only the **Determining Party's** calculation is used for the ETA. Displaying Party B's counter-determination alongside Party A's without qualification could create legal confusion (suggesting equal weight). The UI should clearly label Party B's figure as "indicative counter-determination (not binding)".

---

## 9. §12 Notice

**Result:** PARTIAL PASS

The notice PDF is generated correctly with:
- Correct §5(a)(i) / §12 reference
- Contract ID, parties, amount, due date, grace period
- Fingerprint (SHA-256) for tamper evidence
- Correct English law footer reference

**Issues:**

**P1 — Party name not resolved:** The `party_defaulting` template variable substitutes the raw identifier "B" into the body text, producing: "B has failed to make a payment." This should resolve "B" to "Beta Fund Ltd". Fix: `api_generate_notice()` should map `"A"` → `params.party_a.name` and `"B"` → `params.party_b.name` in the `details` dict before passing to the PDF generator.

**P1 — Legal accuracy (§2(a)(iii) language):** See Section 7 above. The suspension paragraph incorrectly frames §2(a)(iii) as an exercised right rather than an automatic condition precedent.

**P2 — Notice type case-sensitivity:** The API requires `"FAILURE_TO_PAY"` (ALL_CAPS) but the error message for an invalid type is not immediately clear about this. The API documentation should explicitly list valid types and their casing.

**P2 — Response key inconsistency:** `api_generate_notice()` returns `pdf` (the file path) but other API endpoints use `file_path` or `confirmation_pdf`. This inconsistency will cause integration issues.

---

## 10. Compliance Checker (§3/§4)

**Result:** FALSE POSITIVE — P1 issue

Every period calculation fires this escalation:
```
🚨 §5(a)(ii) ESCALATION: 6 obligation(s) overdue >30 days
  → Tax forms (W-8BEN/W-8BEN-E or equivalent) (PARTY_A): 94 days overdue
  → Authorising resolutions / board minutes (PARTY_A): 94 days overdue
  → Legal opinion (capacity and enforceability) (PARTY_A): 94 days overdue
  [same for PARTY_B]
```

These §4 obligations have a due date of `2026-05-01` (the effective date). Since P1 is calculated on `2026-08-03`, these show as 94 days overdue. This triggers a spurious §5(a)(ii) escalation on every period calculation.

**Root cause:** The compliance checker is treating §4(a)(ii) documents (authorising resolutions, legal opinions) as "delivered" obligations that must be confirmed in the system. In practice, these are pre-signing prerequisites. If they were outstanding at signing, the contract would not have been executable in the first place. The compliance checker should either:
1. Mark these as SATISFIED upon signing (since signing implies their existence), or
2. Set their due date relative to the signing date (not the effective date), or
3. Suppress §5(a)(ii) escalation for these specific obligation types since they're covered by the DD gate

In a demo context this is particularly damaging — every period execution shows "NON-COMPLIANT" and "BREACH escalation", which will alarm a sophisticated audience.

---

## 11. Audit Trail

**Result:** PASS

SHA-256 hash chain is intact across all entries. Each entry contains `hash` (of current entry) and `prev_hash` (linking to previous). Chain verified programmatically — no breaks found.

Sample trail for AUDIT7 lifecycle:
```
CONTRACT_CREATED
DEMO_MODE_ACTIVATED
DOCUMENTS_AUTO_VALIDATED × 32
CONTRACT_SIGNED
PAYMENT_CALCULATED (P1: EUR 16,752.17)
PI_APPROVED (P1)
EOD_DECLARED (BREACH_OF_AGREEMENT)
PAYMENT_SUSPENDED (P2)
EARLY_TERMINATION_DESIGNATED (ETD: 2026-12-20)
```

The chain integrity is a genuine security property. The tamper-evident design is appropriate for an ISDA audit record.

---

## 12. Oracle

**Result:** PASS (with production caveat)

The multi-source EURIBOR 3M fallback waterfall is correctly implemented:
1. ECB SDW (primary)
2. EMMI (secondary — stubbed)
3. €STR + 0.0959% ISDA 2021 Benchmark Fallback Spread
4. Last confirmed rate (interpolated)
5. Static fallback (2.850%)

In the test environment (no network access to ECB), the €STR + spread fallback activates and returns 2.987%. This is legally sound — the ISDA 2021 Protocol provides a compliant fallback for EURIBOR cessation or unavailability.

**Issue (P1):** No rate injection API for testing/demo. Every call to `api_execute_period()` uses the oracle. The fallback rate (2.987%) is not configurable at runtime, making it impossible to demonstrate scenarios with specific rates (e.g., "what if EURIBOR hits 5%?"). A `rate_override` field in the `api_execute_period` payload, or a `/demo/set-oracle-rate` endpoint, would significantly improve demo utility.

---

## Bug List

### P0 (Fixed in this commit)

| ID | File | Line | Description | Fix |
|----|------|------|-------------|-----|
| P0-1 | `engine.py` | 2849 | Governing law case-sensitivity: `"English" in "ENGLISH"` → `False` → netting under NY Law | `"english" in governing_law.lower()` |
| P0-2 | `generate_contract_pdf.py` | 99 | Same case-sensitivity for contract PDF | `"english" in governing_law.lower()` |
| P0-3 | `generate_confirmation_pdf.py` | 443 | Same case-sensitivity for notice jurisdiction text | `"english" in governing_law.lower()` |
| P0-4 | `generate_confirmation_pdf.py` | 257 | Fixed amounts hardcoded as EUR 80,000 in Confirmation schedule table | `_compute_fixed_amount()` using 30/360 |
| P0-5 | `generate_confirmation_pdf.py` | 221 | First payment date = `effective_date + 90 days` = "30 July" instead of MODFOL-adjusted "3 August" | Use `payment_schedule[0].payment_date` |

### P1 (Not fixed — require separate implementation work)

| ID | Component | Description |
|----|-----------|-------------|
| P1-1 | `api.py` / `generate_confirmation_pdf.py` | Notice party name: `party_defaulting="B"` substitutes as "B" not "Beta Fund Ltd" in notice body |
| P1-2 | `generate_confirmation_pdf.py` / template | §12 notice §2(a)(iii) suspension language: "exercising our right to suspend" — legally inaccurate; §2(a)(iii) is a condition precedent, not an elected right |
| P1-3 | `engine.py` / compliance checker | §4(a) compliance obligations show as 94-day overdue at P1 → false §5(a)(ii) escalation fires on every period calculation |
| P1-4 | `api.py` | No rate override for `api_execute_period()` — cannot inject test rates; always uses oracle fallback (2.987%) |
| P1-5 | `api.py` | No API endpoints for EoD declaration (`/eod/breach`, `/eod/bankruptcy` etc.) — engine methods only accessible directly |
| P1-6 | `engine.py` close-out | COA calculation uses simplified flat-curve NPV — not §6(e)(ii) compliant for production. Divergence between two-party determinations is ~11%. |
| P1-7 | Advisor portal | No bilateral signing mode — `advisor_managed` activates on Party B's signature alone. Needs a `bilateral` mode for production. |

### P2 (Enhancements / polish)

| ID | Component | Description |
|----|-----------|-------------|
| P2-1 | `generate_confirmation_pdf.py` | MA date placeholder `[date]` never populated — references unresolved in PDF |
| P2-2 | `api.py` notice | Response key `pdf` should be `file_path` for consistency with other endpoints |
| P2-3 | `api.py` notice | Notice type requires ALL_CAPS — not documented; causes 400 on first use |
| P2-4 | `engine.py` close-out | Party B counter-determination displayed without "not binding" qualifier — could imply equal weight |
| P2-5 | Advisor portal | "Demo Mode" toggle is small and easily missed in presentations |
| P2-6 | `api.py` | No `cure_potential_eod()` API endpoint — curing EoDs only possible via engine directly |
| P2-7 | `generate_confirmation_pdf.py` | Payment Dates description could be more precise: "each date falling 3 months after..." rather than just "quarterly, commencing" |

---

## Legal Accuracy Assessment

| ISDA 2002 Section | Accuracy | Notes |
|-------------------|----------|-------|
| §1(b) hierarchy | ✓ CORRECT | Confirmation > Schedule > MA — correctly implemented and stated |
| §2(a)(i) sequential execution | ✓ CORRECT | PI must be approved before next period calculated |
| §2(a)(iii) suspension | ✓ CORRECT | Dynamic condition precedent, Lomas-compliant |
| §2(c) netting | ✓ CORRECT | MTPN election correctly applied |
| §3/§4 compliance | ⚠ PARTIAL | False §5(a)(ii) escalations for §4(a) obligations (P1-3) |
| §5(a)(i) FTP grace | ✓ CORRECT | 1 LBD (TARGET2 + London) |
| §5(a)(ii) breach grace | ✓ CORRECT | 30 calendar days |
| §5(b)(i) illegality | ✓ CORRECT | 3 LBD |
| §5(b)(ii) FM | ✓ CORRECT | 8 LBD |
| §6(e) waterfall | ✓ CORRECT (arithmetic) | COA methodology not production-ready (P1-6) |
| §12 notice | ⚠ PARTIAL | §2(a)(iii) framing incorrect (P1-2), party name not resolved (P1-1) |
| §13 governing law | ✓ CORRECT (post-fix) | Fixed by P0-1 |

**Overall legal accuracy verdict:** Sound foundations, two material legal errors in notice language (P1-1, P1-2), one methodology gap in close-out (P1-6). Fit for demo with P0 fixes applied; not fit for production without P1 fixes.

---

## UX Friction Points

1. **Oracle fallback noise:** Every period execution prints 4 lines of oracle fallback logging. In a demo, this creates visual noise that distracts from the commercial narrative.

2. **Compliance OVERDUE escalation in demo mode:** The `🚨 §5(a)(ii) ESCALATION` message fires on every period execution even in demo mode. A sophisticated audience (lawyers, traders) will ask about it and the honest answer is "it's a bug." Fix P1-3 before demo.

3. **Demo mode toggle visibility:** The "Demo Mode" button is in the header next to logout — easy to overlook. Consider making it more prominent (coloured button or persistent banner) before presentations.

4. **Notice type discovery:** Calling `api_generate_notice` with `"failure_to_pay"` returns an unhelpful error until you discover ALL_CAPS is required. The error message lists valid types which is helpful, but the case requirement should be in the docstring.

5. **Sequential execution lock:** If a period is in "PI pending" state, executing the next period returns a `409 PREVIOUS_PI_PENDING`. This is legally correct but confusing without context — the error message is good but the UI doesn't make the pending PI visible on the same screen.

---

## Recommendations

1. **Fix P1-2 (§2(a)(iii) notice language) before any demo to lawyers.** The "exercising our right to suspend" language will be immediately spotted by any ISDA-trained lawyer and will undermine confidence in the platform's legal rigour.

2. **Fix P1-3 (compliance false escalations) before any client demo.** The §5(a)(ii) breach escalation firing on every period execution looks like a serious problem to a non-technical audience.

3. **Fix P1-1 (party name in notice)** — "B has failed to make payment" is obviously wrong and embarrassing in a demo.

4. **Add a rate override to `api_execute_period()`** (P1-4) to enable scenario modelling — this is a key demo capability.

5. **For production:** Replace the flat-curve close-out NPV with a proper mid-market curve using discount factors. The simplified methodology is a known limitation that must be resolved before live contracts are processed.

---

*This report was generated by programmatic end-to-end testing of the Python API layer. The Confirmation PDF and §12 Notice were reviewed in full. PDF text was extracted using pdfminer. Hash chain integrity was verified programmatically. All P0 bugs listed above have been fixed in the same commit as this report.*
