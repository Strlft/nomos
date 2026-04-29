# ORACLE_RULES.md — Rule Definitions (V1)

**Version** : 1.1
**Reads alongside**: `ORACLE_SPEC.md` (functional), `ORACLE_ARCHITECTURE.md` (technical)
**Authority**: ISDA 2002 Master Agreement, as modified by the Schedule of the specific contract
**Status**: Draft for review
**Rules version covered**: 1.0.0

---

## 0. How to read this document

Every rule below has the same structure. Claude Code must implement each rule in a file named `oracle/rules/impl/r{NNN}_{slug}.py`, and the module must expose a single `rule: Rule` object matching the schema in `ORACLE_ARCHITECTURE.md` §3.5.

Each rule declares:
- **Clause reference** — the exact ISDA 2002 section
- **Trigger condition** — legal language, then structured pseudocode
- **Required inputs** — market data + contract fields
- **Severity** — `WARNING`, `POTENTIAL_TRIGGER`, or `TRIGGER`
- **Grace period** — per ISDA standard or Schedule override
- **Automation limit** — what the rule cannot decide; what escalates to human
- **Test matrix** — minimum scenarios

### Severity semantics

- `WARNING` — precursor condition exists; no legal effect; informational
- `POTENTIAL_TRIGGER` — data conditions met but confirmation (typically notice delivery or legal characterization) still required
- `TRIGGER` — all ISDA clause conditions satisfied including notice; engine may lawfully rely on this to enter §6 (subject to engine policy)

**No rule in V1 automatically invokes §6 close-out.**

---

## R-001 — Failure to Pay or Deliver

### Clause
**ISDA 2002 §5(a)(i)**

### Plain language
A party fails to make, when due, any payment under the Agreement or delivery under §2(a)(i) or §2(e), if such failure is not remedied on or before the first Local Business Day (payment) or first Local Delivery Day (delivery) after notice of such failure is given.

### Structured pseudocode

```
GIVEN a contract with scheduled payment P
WHERE P.amount > 0 AND P.due_date has passed (as_of > P.due_date)

IF P.status == PAID:
    → no trigger

IF P.status == PENDING AND notice_of_failure is NULL:
    → WARNING (grace period not yet started)

IF P.status == PENDING AND notice_of_failure.sent_at is set:
    grace_end = add_business_days(
        notice_of_failure.sent_at, n=1, calendar=TARGET2
    )
    IF as_of <= grace_end:          → WARNING
    ELSE IF as_of > grace_end:      → TRIGGER
```

### Required inputs
- Market metrics: NONE
- Contract fields: `scheduled_payments[]`, `notices[]`, `schedule`

### Severity
`TRIGGER` when grace period elapsed and still unpaid.
`WARNING` otherwise when overdue.

### Grace period
Default **1 Local Business Day** after notice. Schedule override at `schedule.grace_period_failure_to_pay`. TARGET2 calendar for EUR.

### Automation limit
Does not verify actual notice delivery. Trusts `notices[]` field. If disputed, factual issue for parties.

### Test matrix

| Scenario | Expected |
|----------|----------|
| No payment due | No trigger |
| Payment paid on time | No trigger |
| Overdue, no notice | `WARNING` |
| Overdue + notice, inside grace | `WARNING` |
| Overdue + notice, grace elapsed, unpaid | `TRIGGER` |
| Overdue + notice, grace elapsed, paid during grace | No trigger |
| Grace straddles TARGET2 holiday | Grace extends |
| Schedule overrides grace to 3 days | Rule respects override |

---

## R-002 — Breach of Agreement; Repudiation

### Clause
**ISDA 2002 §5(a)(ii)**

### Plain language
Two limbs:
1. **Breach of Agreement** — failure to perform any other obligation (not covered by §4(a)(i), §4(a)(iii), §2(a)(i)/§2(e)) if not remedied within 30 days after notice.
2. **Repudiation** — disaffirms, disclaims, repudiates, or rejects the Agreement or a Confirmation (or evidences such intention).

### Structured pseudocode

```
FOR EACH breach IN contract.breach_records:
    IF breach.kind == "non_performance_other":
        IF breach.notice_sent_at is NULL:
            → WARNING
        ELSE:
            grace_end = add_calendar_days(breach.notice_sent_at, 30)
            IF breach.remedied_at <= grace_end:      → no trigger
            ELSE IF as_of > grace_end AND NOT remedied:
                → TRIGGER
            ELSE:                                     → WARNING

    IF breach.kind == "disaffirmation":
        IF breach.disaffirmation_notice_at is NOT NULL:
            → POTENTIAL_TRIGGER    # Always requires human confirmation
```

### Required inputs
- Market metrics: NONE
- Contract fields: `breach_records[]`

### Severity
`TRIGGER` — Breach of Agreement with grace elapsed.
`POTENTIAL_TRIGGER` — Repudiation (always needs human review).
`WARNING` otherwise.

### Grace period
**30 calendar days** after notice for §5(a)(ii)(1). Verify against Schedule wording; default to 30 calendar days.

### Automation limit
Does not characterize facts as breach. Takes `breach_records` as given. Disaffirmation never auto-TRIGGERs due to evidentiary complexity.

### Test matrix

| Scenario | Expected |
|----------|----------|
| No breach records | No trigger |
| Breach, no notice | `WARNING` |
| Breach + notice, inside 30-day grace | `WARNING` |
| Breach + notice, remedied before grace end | No trigger |
| Breach + notice, grace elapsed, unremedied | `TRIGGER` |
| Disaffirmation notice logged | `POTENTIAL_TRIGGER` |
| Concurrent breaches, one elapsed | One `TRIGGER` emitted |

---

## R-003 — Cross Default (Threshold Test)

### Clause
**ISDA 2002 §5(a)(vi)**, subject to Schedule Part 1:
- Cross Default applies to each party (yes/no)
- Threshold Amount per party
- Specified Indebtedness definition

### Plain language
Cross Default occurs if:
1. A default in respect of Specified Indebtedness aggregating at least the Threshold Amount has resulted in (or is capable of resulting in) acceleration; **or**
2. A payment default on one or more payments under Specified Indebtedness aggregating at least the Threshold Amount.

### Structured pseudocode

```
# V1: external defaults are NOT auto-detected.
# They must be recorded by a human in contract.external_defaults[]
# with source_reference (rating agency action, counterparty disclosure, etc.)

FOR EACH party IN {party_a, party_b}:
    IF NOT schedule.cross_default_applies[party]:
        CONTINUE

    qualifying_defaults = [
        d for d in contract.external_defaults[party]
        if d.instrument_type IN specified_indebtedness_definition
        AND d.status IN {"accelerated", "payment_default"}
        AND d.reported_at <= as_of
    ]
    aggregate = SUM(d.amount_due for d in qualifying_defaults)
    threshold = schedule.cross_default_threshold_amount[party]

    IF aggregate == 0:                          → no trigger
    ELIF aggregate < threshold:                 → WARNING
    ELSE:                                       → POTENTIAL_TRIGGER
```

### Required inputs
- Market metrics: NONE (V1)
- Contract fields: `schedule.cross_default_applies`, `schedule.cross_default_threshold_amount`, `schedule.specified_indebtedness_definition`, `external_defaults[]`

### Severity
`POTENTIAL_TRIGGER` when aggregate ≥ threshold and at least one default is `accelerated` or `payment_default`.
`WARNING` when defaults exist below threshold.
Never auto-`TRIGGER` in V1 (characterization is fact-sensitive).

### Grace period
None at the clause level.

### Automation limit
**Major limit**: Cross Default needs external credit-event data. V1 reads only manually recorded `external_defaults`. Currency: all defaults in threshold currency; else rule raises `DataInconsistent`.

### Test matrix

| Scenario | Expected |
|----------|----------|
| Cross default not applicable | No trigger |
| Applicable, zero defaults | No trigger |
| Defaults below threshold | `WARNING` |
| Defaults at threshold | `POTENTIAL_TRIGGER` |
| Defaults well above threshold | `POTENTIAL_TRIGGER` |
| All defaults `status == "remediated"` | No trigger |
| Mixed currencies | `DataInconsistent`, no event |
| Both parties cross, one below threshold | One `POTENTIAL_TRIGGER`, one `WARNING` |

---

## R-004 — Illegality (Rate Unavailability)

### Clause
**ISDA 2002 §5(b)(i)**

### V1 scope narrowing
Full Illegality requires regulatory monitoring (V2+). V1 implements the **rate unavailability sub-case**: if the benchmark rate the contract references has been discontinued or is persistently unavailable from its administrator, performance of the rate-setting obligation is impaired.

### Structured pseudocode

```
GIVEN contract.floating_leg.reference_rate

IF reference_rate IN market_state.missing:
    days = market_state.missing_consecutive_days[reference_rate]
    IF days >= 5:
        → POTENTIAL_TRIGGER       # Sustained unavailability
    ELSE:
        → WARNING                  # Transient
ELSE:
    → no trigger
```

### Required inputs
- Market metrics: the rate the contract references
- Contract fields: `floating_leg.reference_rate`, `trade_date`

### Severity
`WARNING` for transient unavailability (1-4 business days).
`POTENTIAL_TRIGGER` for sustained (≥5 business days).
Never auto-`TRIGGER`.

### Grace period
None at the clause level. The 5-day threshold is an Oracle heuristic, not a legal cure period.

### Automation limit
Illegality is a legal judgment. V1 surfaces the factual condition only.

### Test matrix

| Scenario | Expected |
|----------|----------|
| Reference rate present | No trigger |
| Rate missing 1 day | `WARNING` |
| Rate missing 5 consecutive business days | `POTENTIAL_TRIGGER` |
| Rate missing 10 days, then returns | No trigger when present |
| Contract uses untracked rate | `indeterminate`, not a trigger |

---

## R-005 — Tax Event (Flag-Based)

### Clause
**ISDA 2002 §5(b)(ii), §5(b)(iii)**

### V1 scope narrowing
Full Tax Event needs tax-law-change monitoring (V2+). V1 is **flag-based**: triggers on human-populated `tax_event_flags[]`.

### Structured pseudocode

```
FOR EACH flag IN contract.tax_event_flags:
    IF flag.effective_date > as_of:                 → no trigger
    ELIF flag.kind == "withholding_imposed":        → POTENTIAL_TRIGGER
    ELIF flag.kind == "indemnifiable_tax_required": → POTENTIAL_TRIGGER
    ELIF flag.kind == "withholding_removed":        → WARNING   # Audit record
```

### Required inputs
- Market metrics: NONE
- Contract fields: `tax_event_flags[]` with `{kind, jurisdiction, effective_date, description, source_reference}`

### Severity
`POTENTIAL_TRIGGER` for imposed withholding / indemnifiable tax.
`WARNING` for tax changes that are informational.
Never auto-`TRIGGER`.

### Automation limit
Tax Events are among the most complex ISDA clauses. V1 is deliberately minimal scaffold.

### Test matrix

| Scenario | Expected |
|----------|----------|
| No flags | No trigger |
| Flag with future effective_date | No trigger |
| Flag `withholding_imposed` (past effective) | `POTENTIAL_TRIGGER` |
| Flag `indemnifiable_tax_required` (past) | `POTENTIAL_TRIGGER` |
| Flag `withholding_removed` | `WARNING` |
| Mixed past + future flags | Only past ones evaluated |

---

## R-006 — Material Adverse Change (Structured Indicia)

### Clause
**Schedule / Part 1 Additional Termination Event** (or bespoke MAC clause).
MAC is not a standard §5 clause in ISDA 2002 Master; it is typically incorporated via the Schedule as an Additional Termination Event or via the Credit Support Annex. The contract must specify:
- Whether MAC applies
- The party(ies) to whom it applies
- The definition of MAC (varies widely across Schedules)

### Plain language
MAC provisions typically allow one party to terminate if a material adverse change has occurred in the other party's financial condition, operations, or ability to perform. Under English law, MAC must be:
- Material (substantial, not trivial)
- Adverse (negative, not merely disruptive)
- Durable (not transient)
- Not reasonably foreseeable at contract signature

(*Grupo Hotelero Urvasco v. Carey Value Added* [2013] EWHC 1039 (Comm))

### V1 scope: structured indicia only

The Oracle **never concludes** that a MAC has occurred. It monitors three structured indicators; when any cross its threshold, the Oracle emits `POTENTIAL_TRIGGER` with an `indicator_set` as evidence. A human reviewer decides whether the indicators together constitute a MAC under the governing law and the Schedule definition.

V1 monitors:

1. **Credit rating downgrade** — a party's long-term issuer rating is downgraded by ≥2 notches from the baseline recorded at contract inception, on the S&P scale or equivalent. Source: manually recorded in `contract.credit_rating_actions[]`.
2. **External payment default** — a party has recorded at least one `external_defaults` entry with `status == "payment_default"` in the current assessment window (default: 90 days). Source: same `external_defaults` field as R-003.
3. **Sanctions designation** — a party has been added to a sanctions list (OFAC SDN, EU consolidated, UK consolidated). Source: manually recorded in `contract.sanctions_designations[]`.

### Structured pseudocode

```
FOR EACH party IN {party_a, party_b}:
    IF NOT schedule.mac_applies[party]:
        CONTINUE

    indicators_triggered = []

    # Indicator A: rating downgrade
    baseline = schedule.credit_rating_baseline[party]
    latest_action = latest(
        contract.credit_rating_actions[party],
        key="effective_date",
        before_or_equal=as_of,
    )
    IF latest_action IS NOT NULL:
        downgrade_notches = count_downgrade_notches(baseline, latest_action.new_rating)
        IF downgrade_notches >= 2:
            indicators_triggered.append("rating_downgrade")

    # Indicator B: external payment default in window
    window_start = subtract_calendar_days(as_of, 90)
    recent_defaults = [
        d for d in contract.external_defaults[party]
        if d.status == "payment_default"
        AND d.reported_at >= window_start
        AND d.reported_at <= as_of
    ]
    IF len(recent_defaults) >= 1:
        indicators_triggered.append("external_payment_default")

    # Indicator C: sanctions designation
    active_sanctions = [
        s for s in contract.sanctions_designations[party]
        if s.effective_date <= as_of
        AND (s.delisted_date IS NULL OR s.delisted_date > as_of)
    ]
    IF len(active_sanctions) >= 1:
        indicators_triggered.append("sanctions_designation")

    # Emission
    IF len(indicators_triggered) == 0:
        → no trigger
    ELIF len(indicators_triggered) == 1:
        → WARNING with indicator identified
    ELSE:
        → POTENTIAL_TRIGGER with all indicators identified
```

### Required inputs

- Market metrics: NONE (V1 MAC is entirely based on structured contract-side data)
- Contract fields:
  - `schedule.mac_applies: {party_a: bool, party_b: bool}`
  - `schedule.credit_rating_baseline: {party_a: str, party_b: str}` (e.g., `"A+"`, `"Baa2"`)
  - `credit_rating_actions[party]: list[RatingAction]` with `{agency, old_rating, new_rating, effective_date, source_reference}`
  - `external_defaults[party]` (shared with R-003)
  - `sanctions_designations[party]: list[SanctionsDesignation]` with `{list_name, entity_id, effective_date, delisted_date, source_reference}`

### Severity

- `POTENTIAL_TRIGGER` when **two or more** indicators are triggered simultaneously — this is the Oracle's highest MAC severity
- `WARNING` when exactly one indicator is triggered
- Never auto-`TRIGGER` in V1 and never in V2 — MAC determination is inherently a legal judgment requiring human review

### Grace period
None at the rule level. MAC assessment is point-in-time.

### Automation limit

**The rule's job is to surface a dossier, not to issue a verdict.** The Evidence tuple of the `TriggerEvent` must include:
- Which indicators triggered
- The raw data values (ratings, defaults, designations) that caused each to trigger
- The source references the parties provided

A human (counsel, advisor, or the IRS engine's human-gate workflow) then assesses whether the Schedule's MAC definition is met under the governing law.

**Explicitly forbidden in V1 and V2:**
- NLP sentiment analysis of news
- Stock price movements as indicators
- Automated news scraping for MAC trigger detection
- Any non-deterministic or non-auditable signal

These were raised in the original brainstorm and are rejected because they cannot meet the determinism and auditability invariants.

### Test matrix

| Scenario | Expected |
|----------|----------|
| MAC not applicable to either party | No trigger |
| Applicable, zero indicators | No trigger |
| Single indicator: rating downgrade -2 notches | `WARNING` |
| Single indicator: rating downgrade -1 notch | No trigger |
| Single indicator: payment default in window | `WARNING` |
| Single indicator: payment default outside window | No trigger |
| Single indicator: active sanctions designation | `WARNING` |
| Two indicators: downgrade + sanctions | `POTENTIAL_TRIGGER` |
| Three indicators all simultaneous | `POTENTIAL_TRIGGER` (not auto-TRIGGER) |
| Sanctions delisted before as_of | Indicator not triggered |
| Rating downgrade -3 notches + no other | `WARNING` (still one indicator) |
| Missing baseline rating | `indeterminate`, not a trigger |

---

## Appendix A — Common principles across all V1 rules

### A.1 Rules never trigger §6 close-out
None of R-001 through R-006 calls `close_out()` or any mutating engine method. Rules emit `TriggerEvent`. The engine and ultimately a human decide whether to invoke §6.

### A.2 Rules are deterministic
Given identical `MarketState` and `ContractState`, output is identical. No `datetime.now()` inside predicates. No randomness. `as_of` is explicit.

### A.3 Rules never silently skip
Missing inputs produce `indeterminate=True` with a reason. The engine distinguishes "no trigger" from "could not evaluate".

### A.4 Rules log evidence
Every `TriggerEvent.evidence` enumerates the specific datapoints and contract fields that drove the match. Replay-able from evidence alone.

### A.5 Rules are versioned
Any change to predicate, threshold, or required input bumps the rule's semver. Aggregate `rules_version` bumps accordingly.

### A.6 Rules do not communicate with each other
R-003 and R-006 both read `external_defaults`, but they do so independently and do not coordinate. Each rule is a pure function of `(MarketState, ContractState)`.

---

## Appendix B — Excluded from V1

- §5(a)(iii) Credit Support Default
- §5(a)(iv) Misrepresentation
- §5(a)(v) Default under Specified Transaction
- §5(a)(vii) Bankruptcy (top V2 candidate — jurisdiction-specific insolvency integration)
- §5(a)(viii) Merger Without Assumption
- §5(b)(iv) Credit Event Upon Merger
- §5(b)(v) Additional Termination Event (general — R-006 implements one specific variant)

---

End of rules document.
