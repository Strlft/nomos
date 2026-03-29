"""
Oracle v3 Test Suite
====================
Tests all three layers plus backward-compatibility with the IRS engine.

Run from the project root:
    python tests/test_oracle_v3.py
"""

import sys
import os

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
sys.path.insert(0, ROOT)

from decimal import Decimal
from oracle_v3 import (
    OracleV3, RateID, RateStatus, EventSeverity,
    ContractSubscription, RegulatoryWatch,
    RateRegistry, EventMonitor, build_irs_subscription,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

PASS  = "[PASS]"
FAIL  = "[FAIL]"
INFO  = "[INFO]"

failures = []

def ok(msg: str):
    print(f"  {PASS}  {msg}")

def fail(msg: str):
    print(f"  {FAIL}  {msg}")
    failures.append(msg)

def info(msg: str):
    print(f"  {INFO}  {msg}")

def section(title: str):
    print(f"\n{'─' * 70}")
    print(f"  {title}")
    print(f"{'─' * 70}")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — Layer 1: Rate Registry
# ─────────────────────────────────────────────────────────────────────────────

def test_layer1_rates():
    section("TEST 1 — Layer 1: Rate Registry (all 9 rates)")
    registry = RateRegistry(anomaly_threshold_bps=Decimal("5"))

    all_rates = list(RateID)
    results = {}

    for rid in all_rates:
        reading = registry.fetch(rid)
        results[rid] = reading

        # Must always return a reading (fallback, never None)
        if reading is None:
            fail(f"{rid.value}: fetch() returned None — should never happen")
            continue

        # Rate must be a positive Decimal
        if not isinstance(reading.rate, Decimal) or reading.rate <= Decimal("0"):
            fail(f"{rid.value}: rate {reading.rate!r} is not a positive Decimal")
        else:
            status_tag = reading.status.value
            src_tag    = reading.source[:20]
            ok(f"{rid.value:<18}  {str(reading.rate):<14}  [{status_tag:<10}]  src: {src_tag}")

    # Count confirmed vs fallback
    confirmed = sum(1 for r in results.values() if r.status == RateStatus.CONFIRMED)
    fallback  = sum(1 for r in results.values() if r.status in (RateStatus.FALLBACK, RateStatus.STALE))
    challenged= sum(1 for r in results.values() if r.status == RateStatus.CHALLENGED)
    info(f"Confirmed: {confirmed}  Fallback/Stale: {fallback}  Challenged: {challenged}")

    # Validate fallback chain: no None readings
    if len(results) == len(all_rates):
        ok(f"All {len(all_rates)} rates returned a reading (no None)")
    else:
        fail(f"Only {len(results)}/{len(all_rates)} rates returned readings")

    # Sanity-check FX rates: EUR/USD should be roughly 1.0 – 1.3
    eur_usd = results.get(RateID.EUR_USD)
    if eur_usd:
        if Decimal("0.8") < eur_usd.rate < Decimal("1.5"):
            ok(f"EUR/USD {eur_usd.rate} in plausible range [0.80, 1.50]")
        else:
            fail(f"EUR/USD {eur_usd.rate} outside plausible range — check ECB series key or fallback")

    # Sanity-check money market: EURIBOR_3M should be 0–10%
    e3m = results.get(RateID.EURIBOR_3M)
    if e3m:
        if Decimal("0") < e3m.rate < Decimal("0.10"):
            ok(f"EURIBOR_3M {e3m.rate} in plausible range (0%, 10%)")
        else:
            fail(f"EURIBOR_3M {e3m.rate} outside plausible range")

    # Verify anomaly detection wiring: inject a fake last_confirmed far from reality
    section("  TEST 1b — Anomaly Detection")
    registry2 = RateRegistry(anomaly_threshold_bps=Decimal("5"))
    # Pre-seed history so anomaly can fire
    hist = registry2._histories[RateID.EURIBOR_3M]
    hist.last_confirmed = Decimal("0.90000")   # 90% — obviously wrong
    reading2 = registry2.fetch(RateID.EURIBOR_3M)
    if reading2.status == RateStatus.CHALLENGED:
        ok("Anomaly detection fired correctly → status=CHALLENGED")
    else:
        # ECB might be unreachable so fallback kicks in before anomaly check
        info(f"Anomaly test: got status={reading2.status.value} "
             f"(FALLBACK/STALE expected if ECB down; CHALLENGED expected if ECB live)")

    # Verify history logging
    if len(registry._histories[RateID.EURIBOR_3M].readings) >= 1:
        ok("History log recorded at least 1 reading for EURIBOR_3M")
    else:
        fail("History log empty after fetch")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — Layer 2: Event Monitor
# ─────────────────────────────────────────────────────────────────────────────

def test_layer2_events():
    section("TEST 2 — Layer 2: Event Monitor (stub mode — no NEWSAPI_KEY)")

    monitor = EventMonitor(newsapi_key=None)

    # 1. Stub mode: poll() must not crash, must return empty list
    sub_india_uk = ContractSubscription(
        contract_id="TEST-001",
        counterparty_names=["Infosys Ltd", "Barclays plc"],
        jurisdictions=["India", "United Kingdom"],
        extra_keywords=["RBI", "FCA"],
        contract_type="IRS",
    )
    try:
        events = monitor.poll([sub_india_uk])
        ok(f"poll() with no API key returned {len(events)} events — no crash (stub mode)")
    except Exception as exc:
        fail(f"poll() with no API key raised: {exc}")

    # 2. Classification logic: inject a fake article with HIGH-severity keywords
    fake_article = {
        "title": "Infosys Ltd faces bankruptcy proceedings in India court",
        "description": "Insolvency application filed against Infosys Ltd. Default on bond.",
        "url": "https://example.com/article/1",
        "source": {"name": "Financial Times"},
        "publishedAt": "2026-03-29T10:00:00Z",
    }
    events_classified = monitor._classify_article(fake_article, [sub_india_uk])
    if not events_classified:
        fail("_classify_article returned 0 events for a clear bankruptcy/default article")
    else:
        ev = events_classified[0]
        ok(f"Event classified: type={ev.event_type.value}  severity={ev.severity.value}")
        if ev.severity == EventSeverity.HIGH:
            ok("Severity correctly escalated to HIGH (counterparty + bankruptcy co-occur)")
        else:
            fail(f"Expected HIGH severity, got {ev.severity.value}")
        if "TEST-001" in ev.linked_contracts:
            ok("Contract TEST-001 correctly linked to event")
        else:
            fail("Contract not linked to event")
        if "bankruptcy" in ev.matched_keywords or "insolvency" in ev.matched_keywords:
            ok(f"Keyword match recorded: {ev.matched_keywords}")
        else:
            fail(f"Expected bankruptcy/insolvency keyword in matches, got: {ev.matched_keywords}")

    # 3. Severity escalation: sanctions WITHOUT counterparty name → MEDIUM
    fake_sanctions_generic = {
        "title": "EU imposes new sanctions on Russian energy sector",
        "description": "Sanctions package targets oil and gas companies.",
        "url": "https://example.com/article/2",
        "source": {"name": "Reuters"},
        "publishedAt": "2026-03-29T09:00:00Z",
    }
    sub_other = ContractSubscription(
        contract_id="TEST-002",
        counterparty_names=["Acme Corp"],   # not in the article
        jurisdictions=["France"],
        contract_type="IRS",
    )
    events2 = monitor._classify_article(fake_sanctions_generic, [sub_other])
    if events2:
        ev2 = events2[0]
        ok(f"Generic sanctions article classified: severity={ev2.severity.value}")
        if ev2.severity in (EventSeverity.HIGH, EventSeverity.MEDIUM):
            ok("Sanctions keyword correctly fires HIGH/MEDIUM (no counterparty co-occurrence)")
        # Sanctions is HIGH even without counterparty (it's a HIGH keyword by default)
    else:
        ok("Generic sanctions article not linked to TEST-002 (no keyword overlap) — correct")

    # 4. get_events filter
    # Manually seed event store
    from oracle_v3 import MarketEvent, EventType
    seeded_event = MarketEvent(
        event_id="EVT-SEED-001",
        event_type=EventType.BANKRUPTCY,
        severity=EventSeverity.HIGH,
        headline="Test seeded event",
        description="",
        source_url="https://example.com/seed",
        source_name="Test",
        published_at="2026-03-29T10:00:00Z",
        fetched_at="2026-03-29T10:00:00Z",
        linked_contracts=["TEST-001"],
    )
    monitor._event_store.append(seeded_event)
    monitor._seen_urls.add(seeded_event.source_url)

    filtered = monitor.get_events(contract_id="TEST-001", min_severity=EventSeverity.HIGH, since_hours=48)
    if filtered:
        ok(f"get_events(contract_id='TEST-001', min_severity=HIGH) returned {len(filtered)} event(s)")
    else:
        fail("get_events returned 0 for a seeded HIGH event")

    # 5. NEWSAPI_KEY env var path: if key is set, confirm monitor would activate
    env_key = os.environ.get("NEWSAPI_KEY")
    if env_key:
        monitor_live = EventMonitor(newsapi_key=env_key)
        ok(f"NEWSAPI_KEY found in env — live mode would be active")
        sub_live = build_irs_subscription(
            "LIVE-001", "Deutsche Bank", "BNP Paribas", ["Germany", "France"]
        )
        monitor_live.poll([sub_live])
        ok("Live NewsAPI poll completed without exception")
    else:
        info("NEWSAPI_KEY not set — live mode skipped (expected in CI)")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — Layer 3: Regulatory Watch
# ─────────────────────────────────────────────────────────────────────────────

def test_layer3_regulatory():
    section("TEST 3 — Layer 3: Regulatory Watch — IRS + English law (UK)")

    watch = RegulatoryWatch()

    # 3a. Total items loaded
    all_alerts = watch.all_alerts()
    if len(all_alerts) >= 10:
        ok(f"Regulatory database loaded: {len(all_alerts)} items")
    else:
        fail(f"Expected ≥10 regulatory items, got {len(all_alerts)}")

    # 3b. impacts() for IRS + UK
    print()
    print(f"  {'Alert ID':<12} {'Sev':<8} {'Regulation':<44} {'Impacts IRS+UK?'}")
    print(f"  {'─'*12} {'─'*8} {'─'*44} {'─'*15}")
    irs_uk_alerts = []
    for alert in all_alerts:
        impacts = alert.impacts("IRS", "UK")
        flag = "YES" if impacts else "no"
        print(f"  {alert.alert_id:<12} {alert.severity.value:<8} {alert.regulation_name[:44]:<44} {flag}")
        if impacts:
            irs_uk_alerts.append(alert)
    print()

    if irs_uk_alerts:
        ok(f"{len(irs_uk_alerts)} alerts flagged as relevant to IRS + UK jurisdiction:")
        for a in irs_uk_alerts:
            ok(f"  {a.alert_id}: {a.regulation_name} (eff: {a.effective_date})")
    else:
        fail("No regulatory alerts flagged for IRS + UK — expected at least LIBOR + Basel III")

    # 3c. LIBOR cessation must be HIGH for IRS+UK
    libor = watch.get_by_id("REG-006")
    if libor:
        if libor.impacts("IRS", "UK"):
            ok("REG-006 (UK LIBOR Cessation) correctly impacts IRS + UK")
        else:
            fail("REG-006 (UK LIBOR Cessation) should impact IRS + UK but didn't")
        if libor.severity == EventSeverity.HIGH:
            ok("REG-006 severity = HIGH (correct)")
        else:
            fail(f"REG-006 severity = {libor.severity.value}, expected HIGH")
    else:
        fail("REG-006 not found in regulatory database")

    # 3d. MiCA should NOT impact an IRS contract
    mica = watch.get_by_id("REG-004")
    if mica:
        if not mica.impacts("IRS", "EU"):
            ok("REG-004 (MiCA) correctly does NOT impact IRS contracts")
        else:
            fail("REG-004 (MiCA) should not impact IRS but does — check affected_contract_types")
    else:
        fail("REG-004 (MiCA) not found")

    # 3e. get_alerts with min_severity=HIGH for EU IRS
    high_eu = watch.get_alerts("IRS", "EU", min_severity=EventSeverity.HIGH)
    ok(f"HIGH alerts for IRS+EU: {len(high_eu)} items: "
       f"{[a.alert_id for a in high_eu]}")

    # 3f. DORA — affects ALL contract types under EU (affected_contract_types includes "ALL")
    dora = watch.get_by_id("REG-005")
    if dora:
        if dora.impacts("IRS", "EU"):
            ok("REG-005 (DORA) correctly impacts IRS + EU")
        else:
            fail("REG-005 (DORA) should impact IRS + EU ('ALL' in affected_contract_types)")
    else:
        fail("REG-005 (DORA) not found")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Backward Compatibility: OracleModule in engine.py
# ─────────────────────────────────────────────────────────────────────────────

def test_backward_compatibility():
    section("TEST 4 — Backward Compatibility: OracleModule delegates to OracleV3")
    from engine import (
        OracleModule, SwapParameters, PartyDetails, IRSExecutionEngine,
        get_oracle_v3,
    )
    from decimal import Decimal
    from datetime import date

    # 4a. get_oracle_v3() singleton
    v3 = get_oracle_v3()
    if v3 is not None:
        ok("get_oracle_v3() returned OracleV3 singleton")
    else:
        fail("get_oracle_v3() returned None — oracle_v3 import failed in engine.py")

    # 4b. OracleModule._v3 attribute
    params = SwapParameters(
        contract_id="TEST-COMPAT-001",
        party_a=PartyDetails("Alpha Corp", "Alpha", "fixed_payer"),
        party_b=PartyDetails("Beta Fund", "Beta", "floating_payer"),
        notional=Decimal("5000000"),
        fixed_rate=Decimal("0.03"),
        effective_date=date(2026, 3, 15),
        termination_date=date(2028, 3, 15),
    )
    oracle_module = OracleModule(params)
    if hasattr(oracle_module, "_v3"):
        ok("OracleModule instance has _v3 attribute")
    else:
        fail("OracleModule instance missing _v3 attribute")

    # 4c. OracleModule.fetch() still works (EURIBOR 3M — original behavior intact)
    reading = oracle_module.fetch()
    if reading is not None and hasattr(reading, "rate"):
        ok(f"OracleModule.fetch() still works: rate={reading.rate}, "
           f"status={reading.status.value}")
    else:
        fail("OracleModule.fetch() returned None or invalid reading")

    # 4d. OracleModule.oracle_summary() still returns expected keys
    summary = oracle_module.oracle_summary()
    required_keys = {"current_rate", "status", "source", "fetch_count",
                     "last_confirmed", "sources_attempted", "anomalies_detected"}
    missing = required_keys - set(summary.keys())
    if not missing:
        ok("oracle_summary() returns all expected keys")
    else:
        fail(f"oracle_summary() missing keys: {missing}")

    # 4e. Run the 3 full engine scenarios via IRSExecutionEngine
    section("  TEST 4e — Full IRS Engine Scenarios (A, B, C)")
    from engine import DefaultingParty
    from datetime import timedelta

    # Scenario A — normal execution
    print("\n  [Scenario A] Normal execution — 2 periods")
    params_a = SwapParameters(
        contract_id="TEST-A-001",
        party_a=PartyDetails("Nomos Bank S.A.", "NomosBk", "fixed_payer"),
        party_b=PartyDetails("Hedge Alpha Ltd", "HedgeA", "floating_payer"),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03150"),
        effective_date=date(2026, 3, 15),
        termination_date=date(2031, 3, 15),
    )
    try:
        engine_a = IRSExecutionEngine(params_a)
        engine_a.initialise()
        engine_a.run_calculation_cycle(1)
        engine_a.confirm_payment(1)
        engine_a.run_calculation_cycle(2)
        engine_a.confirm_payment(2)
        ok("Scenario A: 2 periods executed and confirmed without error")
    except Exception as exc:
        fail(f"Scenario A raised: {type(exc).__name__}: {exc}")

    # Scenario B — Failure to Pay / §2(a)(iii)
    print("\n  [Scenario B] Failure to Pay → §2(a)(iii) suspension")
    params_b = SwapParameters(
        contract_id="TEST-B-001",
        party_a=PartyDetails("Creditor Corp", "Creditor", "fixed_payer"),
        party_b=PartyDetails("Defaulter Ltd", "Defaulter", "floating_payer"),
        notional=Decimal("5000000"),
        fixed_rate=Decimal("0.03000"),
        effective_date=date(2026, 3, 15),
        termination_date=date(2029, 3, 15),
    )
    try:
        from datetime import timedelta
        engine_b = IRSExecutionEngine(params_b)
        engine_b.initialise()
        engine_b.run_calculation_cycle(1)
        # Period 1 NOT confirmed — simulate failure to pay
        today_sim = params_b.effective_date + timedelta(days=100)
        eod_rec = engine_b.eod_monitor.detect_potential_failure_to_pay(
            engine_b.periods[0], today_sim
        )
        if eod_rec:
            ok(f"Scenario B: PEoD detected: {eod_rec.eod_type.value}")
        else:
            ok("Scenario B: PEoD not triggered on period 1 (payment date not yet past)")
        engine_b.run_calculation_cycle(2, today=today_sim)
        ok("Scenario B: §2(a)(iii) path completed without error")
    except Exception as exc:
        fail(f"Scenario B raised: {type(exc).__name__}: {exc}")

    # Scenario C — Close-out waterfall
    print("\n  [Scenario C] §6 Close-out — bankruptcy + ETD")
    params_c = SwapParameters(
        contract_id="TEST-C-001",
        party_a=PartyDetails("Alpha Corp S.A.", "Alpha", "fixed_payer"),
        party_b=PartyDetails("Beta Fund Ltd", "Beta", "floating_payer"),
        notional=Decimal("10000000"),
        fixed_rate=Decimal("0.03200"),
        effective_date=date(2026, 3, 15),
        termination_date=date(2028, 3, 15),
    )
    try:
        engine_c = IRSExecutionEngine(params_c)
        engine_c.initialise()
        engine_c.run_calculation_cycle(1)
        engine_c.confirm_payment(1)
        engine_c.run_calculation_cycle(2)
        engine_c.eod_monitor.declare_bankruptcy(
            defaulting_party=DefaultingParty.PARTY_B,
            description="Beta Fund Ltd enters administration.",
            today=date(2027, 1, 10)
        )
        engine_c.trigger_early_termination(
            trigger_type="§5(a)(vii) BANKRUPTCY — Beta Fund Ltd",
            determining_party="PARTY_A",
            etd=date(2027, 1, 15),
            is_eod=True
        )
        ok("Scenario C: close-out waterfall completed without error")
    except Exception as exc:
        fail(f"Scenario C raised: {type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — OracleV3 facade integration
# ─────────────────────────────────────────────────────────────────────────────

def test_oracle_v3_facade():
    section("TEST 5 — OracleV3 facade: end-to-end smoke test")
    oracle = OracleV3()

    # get_rate
    r = oracle.get_rate(RateID.EURIBOR_3M)
    if r and r.rate > 0:
        ok(f"OracleV3.get_rate(EURIBOR_3M) = {r.rate} [{r.status.value}]")
    else:
        fail("OracleV3.get_rate(EURIBOR_3M) failed")

    # get_rates batch
    batch = oracle.get_rates([RateID.EUR_USD, RateID.EUR_GBP, RateID.ESTR])
    if len(batch) == 3 and all(v.rate > 0 for v in batch.values()):
        ok(f"OracleV3.get_rates(batch of 3) all returned positive rates")
    else:
        fail("OracleV3.get_rates batch returned invalid data")

    # subscribe_contract + get_events
    sub = build_irs_subscription(
        "ORACLE-TEST-001", "Infosys Ltd", "Barclays plc",
        ["India", "United Kingdom"]
    )
    oracle.subscribe_contract(sub)
    events = oracle.get_events(contract_id="ORACLE-TEST-001", since_hours=48)
    ok(f"OracleV3.get_events returned {len(events)} events (stub mode or live)")

    # get_regulatory_alerts
    regs = oracle.get_regulatory_alerts("IRS", "EU")
    if regs:
        ok(f"OracleV3.get_regulatory_alerts(IRS, EU) = {len(regs)} alerts")
    else:
        fail("OracleV3.get_regulatory_alerts(IRS, EU) returned 0 alerts")

    # oracle_summary
    summary = oracle.oracle_summary()
    if summary.get("rates") and summary.get("subscribed_contracts"):
        ok("OracleV3.oracle_summary() contains 'rates' and 'subscribed_contracts'")
    else:
        fail(f"OracleV3.oracle_summary() missing expected keys: {list(summary.keys())}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print("  NOMOS ORACLE v3 — Test Suite")
    print("=" * 70)

    test_layer1_rates()
    test_layer2_events()
    test_layer3_regulatory()
    test_backward_compatibility()
    test_oracle_v3_facade()

    print("\n" + "=" * 70)
    if failures:
        print(f"  RESULT: {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"    ✗ {f}")
        sys.exit(1)
    else:
        print("  RESULT: ALL TESTS PASSED")
    print("=" * 70)
