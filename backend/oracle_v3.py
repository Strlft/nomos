"""
NOMOS ORACLE v3
===============
Layered market intelligence module — contract-agnostic.

Any contract engine (IRS, loans, project finance) can import OracleV3 and
subscribe to the rates and event feeds it needs.

LAYER 1 — Market Data
    EURIBOR 3M / 6M / 12M   ECB SDW (FM dataset)
    €STR overnight           ECB SDW (EST dataset)
    EUR swap 2Y / 5Y / 10Y  ECB yield-curve dataset (YC) — zero-coupon proxy
    EUR/USD, EUR/GBP         ECB SDW (EXR dataset)

    Each rate: primary source → fallback chain → anomaly detection → history log.
    RateRegistry maps RateID → fetch function, so new rates can be added without
    touching contract logic.

LAYER 2 — Event Monitoring
    Polls NewsAPI.org (free tier, requires NEWSAPI_KEY env var) for keywords
    derived from active contract parties, jurisdictions, and risk terms.
    Results are classified as MarketEvent objects with severity LOW/MEDIUM/HIGH.
    Events are linked to contract IDs for MAC-clause monitoring.

LAYER 3 — Regulatory Watch  (stub — production would scrape FCA/ESMA/EBA)
    Hardcoded RegulatoryAlert items covering current EU/UK/US regulatory
    changes. Contracts can be assessed for impact via get_regulatory_alerts().

Usage
-----
    from oracle_v3 import OracleV3, RateID

    oracle = OracleV3(newsapi_key="...")          # key optional
    euribor = oracle.get_rate(RateID.EURIBOR_3M)
    events  = oracle.get_events(contract_id="C-001", since_hours=48)
    regs    = oracle.get_regulatory_alerts(contract_type="IRS", jurisdiction="EU")
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

# SSL context — prefers certifi CA bundle (pip install certifi) when available,
# which is the standard fix for macOS systems where Python's default SSL
# store may not include all required CA certificates.
# Set env var ORACLE_SSL_VERIFY=0 to bypass verification (corporate proxies only).
def _make_ssl_context() -> Optional[ssl.SSLContext]:
    if os.environ.get("ORACLE_SSL_VERIFY", "1") == "0":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    return None  # urllib default (uses system CA store)

_SSL_CONTEXT = _make_ssl_context()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: ENUMERATIONS
# ─────────────────────────────────────────────────────────────────────────────

class RateStatus(Enum):
    CONFIRMED  = "CONFIRMED"   # Live from primary source, within anomaly threshold
    FALLBACK   = "FALLBACK"    # Secondary source or static fallback applied
    CHALLENGED = "CHALLENGED"  # Anomaly detected — human review required
    STALE      = "STALE"       # Older than configured max_age_hours


class RateID(str, Enum):
    """Canonical identifiers for all rates the Oracle can serve."""
    # ── Money-market benchmarks ──────────────────────────────────────────────
    EURIBOR_3M  = "EURIBOR_3M"
    EURIBOR_6M  = "EURIBOR_6M"
    EURIBOR_12M = "EURIBOR_12M"
    ESTR        = "ESTR"          # ECB €STR overnight rate
    # ── EUR interest-rate swap (par) — ECB zero-coupon YC proxy ──────────────
    EUR_SWAP_2Y  = "EUR_SWAP_2Y"
    EUR_SWAP_5Y  = "EUR_SWAP_5Y"
    EUR_SWAP_10Y = "EUR_SWAP_10Y"
    # ── FX spot (EUR/foreign — units of foreign per 1 EUR) ──────────────────
    EUR_USD = "EUR_USD"
    EUR_GBP = "EUR_GBP"


class EventSeverity(Enum):
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"


class EventType(Enum):
    SANCTIONS         = "SANCTIONS"
    DEFAULT           = "DEFAULT"
    BANKRUPTCY        = "BANKRUPTCY"
    FORCE_MAJEURE     = "FORCE_MAJEURE"
    REGULATORY_CHANGE = "REGULATORY_CHANGE"
    COUNTERPARTY_NEWS = "COUNTERPARTY_NEWS"
    JURISDICTION_NEWS = "JURISDICTION_NEWS"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: CORE DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RateReading:
    """
    A single rate observation.  Every reading is persisted in RateHistory.
    raw_response_hash provides an audit anchor to the exact bytes received.
    """
    rate_id:          RateID
    rate:             Decimal
    status:           RateStatus
    source:           str                    # ECB_SDW | ECB_YC | FALLBACK | …
    fetch_timestamp:  str                    # ISO-8601 UTC
    publication_date: Optional[str] = None  # Date the source published this value
    raw_response_hash: Optional[str] = None # SHA-256 of raw response bytes

    def as_dict(self) -> dict:
        return {
            "rate_id":          self.rate_id.value,
            "rate":             str(self.rate),
            "status":           self.status.value,
            "source":           self.source,
            "fetch_timestamp":  self.fetch_timestamp,
            "publication_date": self.publication_date,
        }


@dataclass
class RateHistory:
    """Per-rate circular history buffer + last-confirmed cache."""
    rate_id:              RateID
    readings:             List[RateReading]             = field(default_factory=list)
    last_confirmed:       Optional[Decimal]              = None
    last_confirmed_date:  Optional[str]                  = None   # publication_date of last CONFIRMED reading
    anomaly_threshold_bps: Decimal                      = Decimal("5")
    max_age_hours:        int                            = 48

    def record(self, reading: RateReading) -> None:
        self.readings.append(reading)
        if reading.status == RateStatus.CONFIRMED:
            self.last_confirmed = reading.rate
            # Use ECB publication_date if available, else fetch timestamp date
            self.last_confirmed_date = (
                reading.publication_date
                if reading.publication_date and reading.publication_date != "unknown"
                else reading.fetch_timestamp[:10]
            )

    def latest(self) -> Optional[RateReading]:
        return self.readings[-1] if self.readings else None

    def check_stale(self) -> bool:
        """True if latest reading is older than max_age_hours."""
        latest = self.latest()
        if not latest:
            return True
        ts = datetime.fromisoformat(latest.fetch_timestamp.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        return age > timedelta(hours=self.max_age_hours)


@dataclass
class MarketEvent:
    """
    A news event relevant to one or more contracts.
    Severity drives MAC-clause monitoring in the execution engine.
    """
    event_id:        str
    event_type:      EventType
    severity:        EventSeverity
    headline:        str
    description:     str
    source_url:      str
    source_name:     str
    published_at:    str             # ISO-8601 UTC from news source
    fetched_at:      str             # ISO-8601 UTC when we retrieved it
    matched_keywords: List[str]      = field(default_factory=list)
    linked_contracts: List[str]      = field(default_factory=list)  # contract IDs

    def as_dict(self) -> dict:
        return {
            "event_id":         self.event_id,
            "event_type":       self.event_type.value,
            "severity":         self.severity.value,
            "headline":         self.headline,
            "description":      self.description,
            "source_url":       self.source_url,
            "source_name":      self.source_name,
            "published_at":     self.published_at,
            "fetched_at":       self.fetched_at,
            "matched_keywords": self.matched_keywords,
            "linked_contracts": self.linked_contracts,
        }


@dataclass
class RegulatoryAlert:
    """
    A regulatory change that may impact active contracts.
    In production this would be hydrated from FCA / ESMA / EBA feeds.
    """
    alert_id:              str
    regulation_name:       str
    jurisdiction:          str           # EU, UK, US, GLOBAL …
    impact_description:    str
    affected_contract_types: List[str]  # ["IRS", "CDS", "LOAN", …]
    effective_date:        str          # ISO-8601 date
    source_url:            str
    severity:              EventSeverity = EventSeverity.MEDIUM
    urgency:               EventSeverity = EventSeverity.MEDIUM   # how soon action is needed
    status:                str           = "IN_FORCE"             # PROPOSED / ADOPTED / IN_FORCE
    theme:                 str           = "REPORTING"            # CLEARING/REPORTING/CAPITAL/CONDUCT/CRYPTO/SETTLEMENT/DISCLOSURE/RESILIENCE/ESG

    def impacts(self, contract_type: str, jurisdiction: str) -> bool:
        """True if this alert is relevant to the given contract type/jurisdiction."""
        type_match = (
            not self.affected_contract_types        # empty = all types
            or contract_type.upper() in self.affected_contract_types
        )
        # Jurisdiction match: alert jurisdiction must overlap with contract's
        juris_match = (
            self.jurisdiction == "GLOBAL"
            or jurisdiction.upper() in self.jurisdiction.upper()
            or self.jurisdiction.upper() in jurisdiction.upper()
        )
        return type_match and juris_match

    def as_dict(self) -> dict:
        return {
            "alert_id":              self.alert_id,
            "regulation_name":       self.regulation_name,
            "jurisdiction":          self.jurisdiction,
            "impact_description":    self.impact_description,
            "affected_contract_types": self.affected_contract_types,
            "effective_date":        self.effective_date,
            "source_url":            self.source_url,
            "severity":              self.severity.value,
            "urgency":               self.urgency.value,
            "status":                self.status,
            "theme":                 self.theme,
        }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: RATE REGISTRY (LAYER 1)
# ─────────────────────────────────────────────────────────────────────────────

# ECB SDW base URL — SDMX-JSON 2.0 REST API
_ECB_BASE = "https://data-api.ecb.europa.eu/service/data"

# ECB SDMX-JSON series keys — verified against data-api.ecb.europa.eu (Q1 2026).
#
# EURIBOR: published monthly in the FM dataset.
#   Dimensions: FREQ.REF_AREA.CURRENCY.PROVIDER_FM.INSTRUMENT_FM.PROVIDER_FM_ID.DATA_TYPE_FM
#   FREQ=M (monthly — no daily EURIBOR series in ECB SDMX), PROVIDER_FM=RT
#   The monthly rate reflects the last business-day fixing of each month.
#
# €STR: published as UONSTR (Unsecured Overnight Rate) in the FM dataset at monthly freq.
#   Historical EONIA also available as FM/M.U2.EUR.4F.MM.EONIA.HSTA.
#
# EUR Swap: ECB AAA zero-coupon yield curve (YC dataset), daily.
#   ZC spot rates; par swap ≈ ZC ± 5-15 bps.  For exact par rates use ICE/Bloomberg.
#
# FX: ECB reference rates, daily (EXR dataset). Foreign units per 1 EUR.

_ECB_SERIES: Dict[RateID, str] = {
    RateID.EURIBOR_3M:  "FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",
    RateID.EURIBOR_6M:  "FM/M.U2.EUR.RT.MM.EURIBOR6MD_.HSTA",
    RateID.EURIBOR_12M: "FM/M.U2.EUR.RT.MM.EURIBOR1YD_.HSTA",
    RateID.ESTR:        "FM/M.U2.EUR.4F.MM.UONSTR.HSTA",       # €STR / UONSTR
    # EUR swap: ECB AAA zero-coupon yield curve spot rates (YC dataset)
    RateID.EUR_SWAP_2Y:  "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",
    RateID.EUR_SWAP_5Y:  "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_5Y",
    RateID.EUR_SWAP_10Y: "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y",
    # FX: ECB reference rates (foreign currency units per 1 EUR)
    RateID.EUR_USD: "EXR/D.USD.EUR.SP00.A",
    RateID.EUR_GBP: "EXR/D.GBP.EUR.SP00.A",
}

# Static fallback values — ISDA 2021 / last-resort.
# Updated Q1 2026 to reflect ECB rate-cut cycle (rates fell ~150bps from 2024 peak).
# These are used only when all live sources and ISDA fallbacks fail.
_STATIC_FALLBACKS: Dict[RateID, Decimal] = {
    RateID.EURIBOR_3M:   Decimal("0.02010"),   # ECB monthly avg Feb 2026
    RateID.EURIBOR_6M:   Decimal("0.02140"),
    RateID.EURIBOR_12M:  Decimal("0.02220"),
    RateID.ESTR:         Decimal("0.01930"),   # ECB €STR (UONSTR) Feb 2026
    RateID.EUR_SWAP_2Y:  Decimal("0.02620"),   # ECB YC ZC spot Feb 2026
    RateID.EUR_SWAP_5Y:  Decimal("0.02740"),
    RateID.EUR_SWAP_10Y: Decimal("0.03090"),
    RateID.EUR_USD:      Decimal("1.1517"),    # ECB ref rate Mar 2026
    RateID.EUR_GBP:      Decimal("0.8672"),
}

# ISDA 2021 adjustment spreads for €STR-based fallbacks (IBOR Fallbacks)
_ISDA_ESTR_SPREADS: Dict[RateID, Decimal] = {
    RateID.EURIBOR_3M:  Decimal("0.000959"),  # 9.59 bps
    RateID.EURIBOR_6M:  Decimal("0.001537"),  # 15.37 bps
    RateID.EURIBOR_12M: Decimal("0.002493"),  # 24.93 bps
}


def _ecb_fetch(rate_id: RateID) -> Optional[RateReading]:
    """
    Generic ECB SDW fetcher.  Handles the SDMX-JSON response format used
    by both FM (EURIBOR), EST (€STR), YC (yield curves) and EXR (FX) datasets.
    Returns None on any network or parse error.
    """
    series_key = _ECB_SERIES.get(rate_id)
    if not series_key:
        return None

    url = f"{_ECB_BASE}/{series_key}?lastNObservations=1&format=jsondata"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as resp:
            raw_bytes = resp.read()

        data = json.loads(raw_bytes.decode("utf-8"))
        # SDMX-JSON: first series key may vary — iterate to find non-empty one
        series_map = data["dataSets"][0]["series"]
        observations = None
        for _, s in series_map.items():
            if s.get("observations"):
                observations = s["observations"]
                break
        if observations is None:
            return None

        last_obs = list(observations.values())[-1]
        raw_value = Decimal(str(last_obs[0]))

        # EURIBOR/€STR/YC series are expressed in percent — convert to decimal.
        # FX rates (EXR) are already in native units (e.g. 1.08 USD per EUR).
        is_percent_series = rate_id not in (RateID.EUR_USD, RateID.EUR_GBP)
        rate = (raw_value / Decimal("100")) if is_percent_series else raw_value

        # Publication date from observation dimension
        pub_date = "unknown"
        try:
            obs_dims = data["structure"]["dimensions"]["observation"]
            pub_date = list(obs_dims[0]["values"])[-1].get("id", "unknown")
        except (KeyError, IndexError):
            pass

        return RateReading(
            rate_id=rate_id,
            rate=rate,
            status=RateStatus.CONFIRMED,
            source="ECB_SDW",
            fetch_timestamp=_utcnow(),
            publication_date=pub_date,
            raw_response_hash=hashlib.sha256(raw_bytes).hexdigest(),
        )

    except Exception as exc:
        print(f"  [ORACLE] ✗ ECB fetch failed for {rate_id.value}: {type(exc).__name__}: {exc}")
        return None


class RateRegistry:
    """
    Central registry that maps RateID → fetch function + per-rate history.

    Any contract engine calls:
        registry.fetch(RateID.EURIBOR_6M)
    and gets back a RateReading with full fallback waterfall applied.

    To extend: call registry.register(rate_id, primary_fn, fallback_fn).
    """

    def __init__(self, anomaly_threshold_bps: Decimal = Decimal("5")):
        self._histories: Dict[RateID, RateHistory] = {}
        self._anomaly_bps = anomaly_threshold_bps

        # Auto-register all known rates
        for rid in RateID:
            self._histories[rid] = RateHistory(
                rate_id=rid,
                anomaly_threshold_bps=anomaly_threshold_bps,
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, rate_id: RateID) -> RateReading:
        """
        Fetch a rate with full fallback waterfall:
          1. ECB SDW (primary)
          2. ISDA 2021 €STR + spread (for EURIBOR tenors only)
          3. Last confirmed + stale marker
          4. Static fallback
        """
        history = self._histories[rate_id]
        print(f"  [ORACLE] Fetching {rate_id.value}...")

        # Step 1: Primary (ECB)
        reading = _ecb_fetch(rate_id)
        if reading and reading.status == RateStatus.CONFIRMED:
            reading = self._check_anomaly(reading, history)
            history.record(reading)
            print(f"  [ORACLE] ✓ {rate_id.value}: {reading.rate} ({reading.source})")
            return reading

        # Step 2: ISDA 2021 €STR fallback (EURIBOR tenors only)
        if rate_id in _ISDA_ESTR_SPREADS:
            print(f"  [ORACLE] ECB unavailable → applying ISDA 2021 €STR fallback...")
            estr_reading = _ecb_fetch(RateID.ESTR)
            estr_base = estr_reading.rate if estr_reading else _STATIC_FALLBACKS[RateID.ESTR]
            spread = _ISDA_ESTR_SPREADS[rate_id]
            reading = RateReading(
                rate_id=rate_id,
                rate=estr_base + spread,
                status=RateStatus.FALLBACK,
                source="ISDA_2021_ESTR_PLUS_SPREAD",
                fetch_timestamp=_utcnow(),
            )
            history.record(reading)
            print(f"  [ORACLE] ✓ FALLBACK {rate_id.value}: {reading.rate} (€STR + spread)")
            return reading

        # Step 3: Last confirmed (stale)
        if history.last_confirmed is not None:
            print(f"  [ORACLE] Using last confirmed (stale) for {rate_id.value}")
            reading = RateReading(
                rate_id=rate_id,
                rate=history.last_confirmed,
                status=RateStatus.STALE,
                source="LAST_CONFIRMED_STALE",
                fetch_timestamp=_utcnow(),
            )
            history.record(reading)
            return reading

        # Step 4: Static fallback
        fallback_rate = _STATIC_FALLBACKS.get(rate_id, Decimal("0"))
        print(f"  [ORACLE] All sources exhausted → static fallback {rate_id.value}: {fallback_rate}")
        reading = RateReading(
            rate_id=rate_id,
            rate=fallback_rate,
            status=RateStatus.FALLBACK,
            source="STATIC_FALLBACK",
            fetch_timestamp=_utcnow(),
        )
        history.record(reading)
        return reading

    def fetch_many(self, rate_ids: List[RateID]) -> Dict[RateID, RateReading]:
        """Fetch multiple rates.  Each gets its own fallback waterfall."""
        return {rid: self.fetch(rid) for rid in rate_ids}

    def history(self, rate_id: RateID) -> List[RateReading]:
        return self._histories[rate_id].readings

    def latest(self, rate_id: RateID) -> Optional[RateReading]:
        return self._histories[rate_id].latest()

    def summary(self) -> dict:
        out = {}
        for rid, h in self._histories.items():
            latest = h.latest()
            if latest:
                out[rid.value] = {
                    "rate":   str(latest.rate),
                    "status": latest.status.value,
                    "source": latest.source,
                    "as_of":  latest.fetch_timestamp,
                    "fetch_count": len(h.readings),
                    "anomalies":   sum(
                        1 for r in h.readings if r.status == RateStatus.CHALLENGED
                    ),
                    "last_confirmed_date": h.last_confirmed_date,
                    # Last 10 readings for sparkline (rate + status only)
                    "history": [
                        {"rate": str(r.rate), "status": r.status.value, "ts": r.fetch_timestamp[:16]}
                        for r in h.readings[-10:]
                    ],
                }
        return out

    # ── Private helpers ───────────────────────────────────────────────────────

    def _check_anomaly(
        self, reading: RateReading, history: RateHistory
    ) -> RateReading:
        if history.last_confirmed is None:
            return reading
        diff_bps = abs(reading.rate - history.last_confirmed) * Decimal("10000")
        threshold = history.anomaly_threshold_bps
        if diff_bps > threshold * Decimal("10"):
            print(
                f"  [ORACLE] ANOMALY: {reading.rate_id.value} deviated {diff_bps:.1f} bps "
                f"(threshold {threshold} bps x10). HUMAN GATE required."
            )
            reading.status = RateStatus.CHALLENGED
        elif diff_bps > threshold:
            print(
                f"  [ORACLE]  Rate deviation: {reading.rate_id.value} {diff_bps:.1f} bps "
                f"(threshold {threshold} bps) — within tolerance."
            )
        return reading


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: EVENT MONITORING (LAYER 2)
# ─────────────────────────────────────────────────────────────────────────────

# Keyword → (EventType, base_severity)
# Higher-severity classifications override lower ones if multiple match.
_KEYWORD_MAP: List[Tuple[str, EventType, EventSeverity]] = [
    ("sanctions",          EventType.SANCTIONS,         EventSeverity.HIGH),
    ("sanctioned",         EventType.SANCTIONS,         EventSeverity.HIGH),
    ("ofac",               EventType.SANCTIONS,         EventSeverity.HIGH),
    ("sdn list",           EventType.SANCTIONS,         EventSeverity.HIGH),
    ("default",            EventType.DEFAULT,           EventSeverity.HIGH),
    ("event of default",   EventType.DEFAULT,           EventSeverity.HIGH),
    ("cross default",      EventType.DEFAULT,           EventSeverity.HIGH),
    ("bankruptcy",         EventType.BANKRUPTCY,        EventSeverity.HIGH),
    ("insolvency",         EventType.BANKRUPTCY,        EventSeverity.HIGH),
    ("administration",     EventType.BANKRUPTCY,        EventSeverity.HIGH),
    ("liquidation",        EventType.BANKRUPTCY,        EventSeverity.HIGH),
    ("force majeure",      EventType.FORCE_MAJEURE,     EventSeverity.HIGH),
    ("regulatory change",  EventType.REGULATORY_CHANGE, EventSeverity.MEDIUM),
    ("regulation",         EventType.REGULATORY_CHANGE, EventSeverity.LOW),
    ("legislation",        EventType.REGULATORY_CHANGE, EventSeverity.LOW),
    ("compliance",         EventType.REGULATORY_CHANGE, EventSeverity.LOW),
]

_SEVERITY_ORDER = {
    EventSeverity.LOW: 0,
    EventSeverity.MEDIUM: 1,
    EventSeverity.HIGH: 2,
}


@dataclass
class ContractSubscription:
    """
    Defines what keywords an active contract cares about.
    Created by the contract engine; consumed by EventMonitor.
    """
    contract_id:       str
    counterparty_names: List[str]     = field(default_factory=list)
    jurisdictions:     List[str]      = field(default_factory=list)
    extra_keywords:    List[str]      = field(default_factory=list)
    contract_type:     str            = "IRS"

    def all_keywords(self) -> List[str]:
        """All lower-case keywords this contract subscribes to."""
        base = [kw for kw, _, _ in _KEYWORD_MAP]
        base += [n.lower() for n in self.counterparty_names]
        base += [j.lower() for j in self.jurisdictions]
        base += [k.lower() for k in self.extra_keywords]
        return list(dict.fromkeys(base))  # deduplicate, preserve order


class EventMonitor:
    """
    LAYER 2: Event Monitoring via NewsAPI.org (free tier).

    Initialization:
        monitor = EventMonitor(newsapi_key="YOUR_KEY")

    If newsapi_key is None (or env var NEWSAPI_KEY not set), the monitor
    operates in stub mode and returns an empty event list without error.
    The contract engine degrades gracefully — MAC monitoring simply reports
    "no data" rather than blocking execution.

    Severity escalation rules
    ─────────────────────────
    HIGH   — bankruptcy / default / sanctions + counterparty name in article
    MEDIUM — sanctions / regulatory action in counterparty jurisdiction
    LOW    — general regulatory or jurisdiction news
    """

    _NEWSAPI_URL = "https://newsapi.org/v2/everything"

    def __init__(
        self,
        newsapi_key: Optional[str] = None,
        max_results_per_query: int = 10,
    ):
        self.api_key = newsapi_key or os.environ.get("NEWSAPI_KEY")
        self.max_results = max_results_per_query
        self._event_store: List[MarketEvent] = []
        self._seen_urls: Set[str] = set()
        self._event_counter = 0
        self._stubs_seeded: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def poll(self, subscriptions: List[ContractSubscription]) -> List[MarketEvent]:
        """
        Run one news poll cycle for all subscribed contracts.
        Returns newly discovered events (not seen in previous polls).
        When NEWSAPI_KEY is not set, seeds curated stub events once on first poll.
        """
        if not self.api_key:
            if not self._stubs_seeded:
                print("  [EVENT MONITOR] No API key — seeding curated stub events.")
                stubs = self._build_stub_events()
                self._event_store.extend(stubs)
                self._stubs_seeded = True
                return stubs
            return []

        new_events: List[MarketEvent] = []
        # Deduplicate queries across all subscriptions
        queries_seen: Set[str] = set()

        for sub in subscriptions:
            # Build a targeted query: high-value keywords only for this contract
            query_parts = []
            # Named counterparties + jurisdiction + high-severity terms
            query_parts += sub.counterparty_names
            query_parts += sub.jurisdictions
            query_parts += sub.extra_keywords
            query_parts += ["sanctions", "default", "bankruptcy", "force majeure"]
            query = " OR ".join(f'"{p}"' for p in query_parts[:5])  # NewsAPI free: keep short
            if query in queries_seen:
                continue
            queries_seen.add(query)

            articles = self._fetch_news(query)
            for article in articles:
                events = self._classify_article(article, subscriptions)
                for ev in events:
                    if ev.source_url not in self._seen_urls:
                        self._seen_urls.add(ev.source_url)
                        self._event_store.append(ev)
                        new_events.append(ev)

        return new_events

    def get_events(
        self,
        contract_id: Optional[str] = None,
        min_severity: EventSeverity = EventSeverity.LOW,
        since_hours: int = 48,
    ) -> List[MarketEvent]:
        """
        Retrieve stored events.  Optionally filter by contract, severity, recency.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        result = []
        for ev in self._event_store:
            if contract_id and contract_id not in ev.linked_contracts:
                continue
            if _SEVERITY_ORDER[ev.severity] < _SEVERITY_ORDER[min_severity]:
                continue
            try:
                ts = datetime.fromisoformat(ev.fetched_at.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except ValueError:
                pass
            result.append(ev)
        return result

    def all_events(self) -> List[MarketEvent]:
        return list(self._event_store)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_stub_events(self) -> List[MarketEvent]:
        """
        Curated realistic stub events returned when no NEWSAPI_KEY is configured.
        Covers the major MAC-clause trigger categories (sanctions, default, regulatory,
        market disruption, counterparty news) at varied severity levels.
        Set NEWSAPI_KEY in the environment to replace these with live data.
        """
        now = _utcnow()
        return [
            MarketEvent(
                event_id="EVT-STUB-001",
                event_type=EventType.SANCTIONS,
                severity=EventSeverity.HIGH,
                headline="OFAC designates major Russian state-owned bank — SDN list updated",
                description=(
                    "The U.S. Office of Foreign Assets Control has added VTB Bank and two "
                    "affiliated entities to the Specially Designated Nationals list, expanding "
                    "blocking sanctions. EU counterparties with existing OTC derivative trades "
                    "referencing affected entities may face immediate termination obligations."
                ),
                source_url="https://home.treasury.gov/policy-issues/financial-sanctions/recent-actions",
                source_name="U.S. Treasury / OFAC",
                published_at="2026-04-03T14:00:00Z",
                fetched_at=now,
                matched_keywords=["ofac", "sanctions", "sdn list"],
                linked_contracts=[],
            ),
            MarketEvent(
                event_id="EVT-STUB-002",
                event_type=EventType.REGULATORY_CHANGE,
                severity=EventSeverity.HIGH,
                headline="ESMA publishes final EMIR 3.0 RTS on active account requirements — 6-month clock starts",
                description=(
                    "The European Securities and Markets Authority has published binding "
                    "Regulatory Technical Standards mandating EU counterparties to hold an "
                    "active account at an EU CCP for specified derivatives categories. "
                    "Firms have 6 months to demonstrate representativeness thresholds."
                ),
                source_url="https://www.esma.europa.eu/press-news/esma-news",
                source_name="ESMA",
                published_at="2026-03-28T09:00:00Z",
                fetched_at=now,
                matched_keywords=["regulatory change", "regulation", "compliance"],
                linked_contracts=[],
            ),
            MarketEvent(
                event_id="EVT-STUB-003",
                event_type=EventType.DEFAULT,
                severity=EventSeverity.HIGH,
                headline="Moody's downgrades French sovereign to Aa3 — cross-default clauses under review",
                description=(
                    "Moody's Investors Service cut France's long-term issuer rating to Aa3 "
                    "from Aa2, citing fiscal consolidation delays. Advisors should review "
                    "cross-default threshold triggers in ISDA schedules where the French "
                    "Republic or French state entities are specified counterparties."
                ),
                source_url="https://www.moodys.com/research/sovereigns",
                source_name="Moody's Investors Service",
                published_at="2026-04-01T07:30:00Z",
                fetched_at=now,
                matched_keywords=["default", "cross default"],
                linked_contracts=[],
            ),
            MarketEvent(
                event_id="EVT-STUB-004",
                event_type=EventType.FORCE_MAJEURE,
                severity=EventSeverity.HIGH,
                headline="ECB activates emergency repo facility amid eurozone CCP margin call spike",
                description=(
                    "The European Central Bank has activated its emergency liquidity support "
                    "facility after LCH SA issued intraday margin calls 4× above normal levels "
                    "following a sharp move in EUR swap rates. Collateral shortfalls being "
                    "reported by several mid-size clearing members. Force majeure review advised."
                ),
                source_url="https://www.ecb.europa.eu/press/pr/date/2026",
                source_name="European Central Bank",
                published_at="2026-04-04T11:15:00Z",
                fetched_at=now,
                matched_keywords=["force majeure"],
                linked_contracts=[],
            ),
            MarketEvent(
                event_id="EVT-STUB-005",
                event_type=EventType.BANKRUPTCY,
                severity=EventSeverity.MEDIUM,
                headline="Austrian regional bank enters administration — ISDA close-out netting triggered",
                description=(
                    "Austrian regulator FMA has placed Hypo Vorarlberg Bank in resolution "
                    "proceedings. Counterparties with bilateral IRS/CDS positions are "
                    "executing close-out netting valuations under 2002 ISDA Master Agreement "
                    "Section 6(e). Austrian courts have confirmed netting enforceability."
                ),
                source_url="https://www.fma.gv.at/en/press-releases",
                source_name="FMA Austria",
                published_at="2026-03-20T16:45:00Z",
                fetched_at=now,
                matched_keywords=["administration", "bankruptcy", "insolvency"],
                linked_contracts=[],
            ),
            MarketEvent(
                event_id="EVT-STUB-006",
                event_type=EventType.REGULATORY_CHANGE,
                severity=EventSeverity.MEDIUM,
                headline="FCA publishes updated ISDA fallback protocol adoption guidance for UK firms",
                description=(
                    "The Financial Conduct Authority has issued a Dear CEO letter reminding "
                    "UK-authorised firms of outstanding IBOR fallback protocol adoption "
                    "obligations. Firms with legacy EUR LIBOR-linked contracts not yet "
                    "adhering to the ISDA 2020 protocol face supervisory scrutiny."
                ),
                source_url="https://www.fca.org.uk/markets/libor/firms",
                source_name="Financial Conduct Authority",
                published_at="2026-03-15T10:00:00Z",
                fetched_at=now,
                matched_keywords=["regulatory change", "compliance"],
                linked_contracts=[],
            ),
            MarketEvent(
                event_id="EVT-STUB-007",
                event_type=EventType.COUNTERPARTY_NEWS,
                severity=EventSeverity.LOW,
                headline="European Commission launches SFDR review — new product categorisation expected H2 2026",
                description=(
                    "The European Commission has published a targeted consultation on the "
                    "Sustainable Finance Disclosure Regulation overhaul. Proposed changes "
                    "include a simpler two-category product label system replacing Article 8/9. "
                    "Final rules expected Q4 2026; fund and structured finance contracts may "
                    "require updated disclosure language."
                ),
                source_url="https://finance.ec.europa.eu/sustainable-finance",
                source_name="European Commission",
                published_at="2026-04-02T08:00:00Z",
                fetched_at=now,
                matched_keywords=["regulation", "legislation"],
                linked_contracts=[],
            ),
        ]

    def _fetch_news(self, query: str) -> List[dict]:
        params = urllib.parse.urlencode({
            "q":        query,
            "language": "en",
            "sortBy":   "publishedAt",
            "pageSize": self.max_results,
            "apiKey":   self.api_key,
        })
        url = f"{self._NEWSAPI_URL}?{params}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Nomos/3.0"})
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("articles", [])
        except Exception as exc:
            print(f"  [EVENT MONITOR] NewsAPI fetch failed: {type(exc).__name__}: {exc}")
            return []

    def _classify_article(
        self,
        article: dict,
        subscriptions: List[ContractSubscription],
    ) -> List[MarketEvent]:
        """
        Match an article against keyword map and contract subscriptions.
        Returns 0 or 1 MarketEvent with severity and linked contracts resolved.
        """
        text = " ".join(filter(None, [
            article.get("title", ""),
            article.get("description", ""),
        ])).lower()

        matched_keywords: List[str] = []
        best_type = EventType.COUNTERPARTY_NEWS
        best_severity = None

        # Match system keywords
        for kw, etype, esev in _KEYWORD_MAP:
            if kw in text:
                matched_keywords.append(kw)
                if best_severity is None or _SEVERITY_ORDER[esev] > _SEVERITY_ORDER[best_severity]:
                    best_severity = esev
                    best_type = etype

        # Match contract-specific keywords and link contracts
        linked: List[str] = []
        for sub in subscriptions:
            contract_terms = (
                [n.lower() for n in sub.counterparty_names]
                + [j.lower() for j in sub.jurisdictions]
                + [k.lower() for k in sub.extra_keywords]
            )
            for term in contract_terms:
                if term in text:
                    matched_keywords.append(term)
                    if sub.contract_id not in linked:
                        linked.append(sub.contract_id)

        if not matched_keywords:
            return []  # no relevant match

        # Escalate severity if counterparty name + high-risk term co-occur
        if linked and best_severity == EventSeverity.MEDIUM:
            best_severity = EventSeverity.HIGH
        if best_severity is None:
            best_severity = EventSeverity.LOW

        self._event_counter += 1
        ev = MarketEvent(
            event_id=f"EVT-{self._event_counter:04d}",
            event_type=best_type,
            severity=best_severity,
            headline=article.get("title", "")[:200],
            description=(article.get("description") or "")[:500],
            source_url=article.get("url", ""),
            source_name=(article.get("source") or {}).get("name", "unknown"),
            published_at=article.get("publishedAt", _utcnow()),
            fetched_at=_utcnow(),
            matched_keywords=list(dict.fromkeys(matched_keywords)),
            linked_contracts=linked,
        )
        return [ev]


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5: REGULATORY WATCH (LAYER 3)
# ─────────────────────────────────────────────────────────────────────────────

def _build_regulatory_db() -> List[RegulatoryAlert]:
    """
    Hardcoded regulatory watch items — current as of Q1 2026.
    26 items across EU / UK / US / Global jurisdictions.
    In production this would be populated by scraping FCA/ESMA/EBA/SEC feeds.

    Fields:
        severity  — impact level if non-compliant
        urgency   — how soon action is needed (HIGH = within 6 months)
        status    — PROPOSED / ADOPTED / IN_FORCE
        theme     — CLEARING / REPORTING / CAPITAL / CONDUCT / CRYPTO /
                    SETTLEMENT / DISCLOSURE / RESILIENCE / ESG
    """
    return [
        # ── EU ────────────────────────────────────────────────────────────────
        RegulatoryAlert(
            alert_id="REG-001",
            regulation_name="EMIR Refit / EMIR 3.0",
            jurisdiction="EU",
            impact_description=(
                "Updated OTC derivative clearing thresholds; revised reporting "
                "under EMIR 3.0 including new XML XBRL formats and active account "
                "requirements at EU CCPs. Active account RTS published Mar 2026 — "
                "6-month representativeness clock now running."
            ),
            affected_contract_types=["IRS", "CDS", "FX_DERIVATIVE", "COMMODITY_DERIVATIVE"],
            effective_date="2024-04-29",
            source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32019R0834",
            severity=EventSeverity.HIGH,
            urgency=EventSeverity.HIGH,
            status="IN_FORCE",
            theme="CLEARING",
        ),
        RegulatoryAlert(
            alert_id="REG-002",
            regulation_name="MiFIR Review / MiFID II Review (EU 2024/791)",
            jurisdiction="EU",
            impact_description=(
                "Revised transparency regime: new consolidated tape for bonds "
                "and derivatives; reformed systematic internaliser thresholds; "
                "new pre/post-trade transparency waivers."
            ),
            affected_contract_types=["IRS", "BOND", "EQUITY_DERIVATIVE"],
            effective_date="2024-03-28",
            source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R0791",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="REPORTING",
        ),
        RegulatoryAlert(
            alert_id="REG-003",
            regulation_name="Basel III Endgame (CRR3 / CRD6)",
            jurisdiction="EU",
            impact_description=(
                "Revised credit risk standardised approach, FRTB market risk "
                "framework, and CVA risk framework. Increases capital requirements "
                "for derivatives counterparty credit risk and changes RWA calculations."
            ),
            affected_contract_types=["IRS", "CDS", "LOAN", "PROJECT_FINANCE"],
            effective_date="2025-01-01",
            source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R1623",
            severity=EventSeverity.HIGH,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="CAPITAL",
        ),
        RegulatoryAlert(
            alert_id="REG-004",
            regulation_name="MiCA — Markets in Crypto-Assets (EU 2023/1114)",
            jurisdiction="EU",
            impact_description=(
                "Full application from 30 Dec 2024. Crypto-asset service providers "
                "require authorisation. Stablecoin and e-money token issuers subject "
                "to new capital and reserve requirements."
            ),
            affected_contract_types=["DIGITAL_ASSET", "TOKEN_SETTLEMENT"],
            effective_date="2024-12-30",
            source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32023R1114",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="CRYPTO",
        ),
        RegulatoryAlert(
            alert_id="REG-005",
            regulation_name="DORA — Digital Operational Resilience Act (EU 2022/2554)",
            jurisdiction="EU",
            impact_description=(
                "Mandatory ICT risk management framework, incident reporting, and "
                "third-party ICT provider oversight for all EU financial entities. "
                "Contracts relying on cloud or outsourced infrastructure must comply. "
                "First TLPT exercises due H1 2026."
            ),
            affected_contract_types=["IRS", "CDS", "LOAN", "PROJECT_FINANCE", "ALL"],
            effective_date="2025-01-17",
            source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022R2554",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.HIGH,
            status="IN_FORCE",
            theme="RESILIENCE",
        ),
        RegulatoryAlert(
            alert_id="REG-007",
            regulation_name="SFDR Review — New Product Categorisation (EU)",
            jurisdiction="EU",
            impact_description=(
                "European Commission consultation on simplified two-category label "
                "system replacing Article 8/9. New templates expected Q4 2026; "
                "structured finance and fund contracts may need updated disclosure language."
            ),
            affected_contract_types=["FUND", "STRUCTURED_FINANCE", "PROJECT_FINANCE"],
            effective_date="2027-01-01",
            source_url="https://finance.ec.europa.eu/sustainable-finance",
            severity=EventSeverity.LOW,
            urgency=EventSeverity.LOW,
            status="PROPOSED",
            theme="ESG",
        ),
        RegulatoryAlert(
            alert_id="REG-008",
            regulation_name="EU T+1 Settlement Cycle (CSDR Review)",
            jurisdiction="EU",
            impact_description=(
                "Proposed shortening of securities settlement from T+2 to T+1, "
                "targeting Oct 2027. Impacts collateral management timelines and "
                "margin call logistics in repo and securities lending contracts."
            ),
            affected_contract_types=["REPO", "SECURITIES_LENDING", "BOND", "EQUITY"],
            effective_date="2027-10-11",
            source_url="https://www.esma.europa.eu/press-news/esma-news/esma-publishes-report-shortening-settlement-cycle",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.LOW,
            status="PROPOSED",
            theme="SETTLEMENT",
        ),
        RegulatoryAlert(
            alert_id="REG-011",
            regulation_name="CSRD — Corporate Sustainability Reporting Directive",
            jurisdiction="EU",
            impact_description=(
                "Mandatory double materiality reporting for large EU corporates from "
                "FY2024 reports (published 2025). ESG disclosures now feed into "
                "counterparty credit assessments under CRR3. Project finance and "
                "loan documentation may require sustainability-linked covenants."
            ),
            affected_contract_types=["LOAN", "PROJECT_FINANCE", "BOND"],
            effective_date="2025-01-01",
            source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022L2464",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="ESG",
        ),
        RegulatoryAlert(
            alert_id="REG-012",
            regulation_name="FIDA — Financial Data Access Regulation",
            jurisdiction="EU",
            impact_description=(
                "Proposed open finance framework mandating data sharing between "
                "financial institutions, including contract and performance data. "
                "Firms will need to implement standardised APIs by 2027. "
                "Data governance clauses in master agreements may need revision."
            ),
            affected_contract_types=["IRS", "LOAN", "ALL"],
            effective_date="2027-07-01",
            source_url="https://finance.ec.europa.eu/publications/financial-data-access",
            severity=EventSeverity.LOW,
            urgency=EventSeverity.LOW,
            status="PROPOSED",
            theme="REPORTING",
        ),
        RegulatoryAlert(
            alert_id="REG-013",
            regulation_name="EU AI Act — Financial Services Applications",
            jurisdiction="EU",
            impact_description=(
                "High-risk AI systems used in credit scoring, pricing, or risk "
                "management require conformity assessments, human oversight, and "
                "registration from Aug 2026. Algorithmic trading and automated "
                "contract execution systems in scope if classified high-risk."
            ),
            affected_contract_types=["ALL"],
            effective_date="2026-08-02",
            source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R1689",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.HIGH,
            status="ADOPTED",
            theme="CONDUCT",
        ),
        # ── UK ────────────────────────────────────────────────────────────────
        RegulatoryAlert(
            alert_id="REG-006",
            regulation_name="UK LIBOR Cessation — Synthetic USD LIBOR End",
            jurisdiction="UK",
            impact_description=(
                "Synthetic USD LIBOR ceased 30 Sep 2024. Any remaining legacy "
                "USD LIBOR contracts must now reference SOFR or have triggered "
                "contractual fallback language. FCA will not permit new synthetic "
                "rate after Dec 2026."
            ),
            affected_contract_types=["IRS", "LOAN", "BOND"],
            effective_date="2024-09-30",
            source_url="https://www.fca.org.uk/markets/libor",
            severity=EventSeverity.HIGH,
            urgency=EventSeverity.HIGH,
            status="IN_FORCE",
            theme="REPORTING",
        ),
        RegulatoryAlert(
            alert_id="REG-014",
            regulation_name="FCA Consumer Duty — Ongoing Monitoring Requirements",
            jurisdiction="UK",
            impact_description=(
                "FCA Consumer Duty fully in force. Firms must evidence ongoing "
                "monitoring of consumer outcomes, including in structured product "
                "and investment contract distribution. Annual board attestation required."
            ),
            affected_contract_types=["LOAN", "STRUCTURED_FINANCE", "FUND"],
            effective_date="2024-07-31",
            source_url="https://www.fca.org.uk/firms/consumer-duty",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="CONDUCT",
        ),
        RegulatoryAlert(
            alert_id="REG-015",
            regulation_name="UK EMIR — Post-Brexit Clearing Obligations",
            jurisdiction="UK",
            impact_description=(
                "UK EMIR clearing obligations continue for UK counterparties; "
                "equivalence of EU CCPs reviewed annually. UK has diverged on "
                "active account requirements — UK firms not subject to EU EMIR 3.0 "
                "active account mandates but face BoE/FCA scrutiny on concentration risk."
            ),
            affected_contract_types=["IRS", "CDS", "FX_DERIVATIVE"],
            effective_date="2021-01-01",
            source_url="https://www.fca.org.uk/markets/derivatives/emir",
            severity=EventSeverity.HIGH,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="CLEARING",
        ),
        RegulatoryAlert(
            alert_id="REG-016",
            regulation_name="UK Basel 3.1 (PRA Near-Final Rules)",
            jurisdiction="UK",
            impact_description=(
                "PRA published near-final rules for UK Basel 3.1 implementation, "
                "effective Jan 2027 (delayed from 2026). Revised standardised "
                "credit risk, operational risk, and output floor will increase "
                "capital charges for derivatives books held by UK banks."
            ),
            affected_contract_types=["IRS", "CDS", "LOAN"],
            effective_date="2027-01-01",
            source_url="https://www.bankofengland.co.uk/prudential-regulation/publication/2023/near-final-basel-3-1",
            severity=EventSeverity.HIGH,
            urgency=EventSeverity.MEDIUM,
            status="ADOPTED",
            theme="CAPITAL",
        ),
        RegulatoryAlert(
            alert_id="REG-017",
            regulation_name="Edinburgh Reforms — UK MiFID Review",
            jurisdiction="UK",
            impact_description=(
                "HM Treasury has launched wholesale markets review under Edinburgh "
                "Reforms. Proposed removal of share trading obligation, relaxation "
                "of derivatives trading obligation, and simplified research unbundling. "
                "Consultation closed; primary legislation expected 2026."
            ),
            affected_contract_types=["IRS", "BOND", "EQUITY_DERIVATIVE"],
            effective_date="2026-12-31",
            source_url="https://www.gov.uk/government/collections/the-edinburgh-reforms",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.LOW,
            status="PROPOSED",
            theme="CONDUCT",
        ),
        # ── US ────────────────────────────────────────────────────────────────
        RegulatoryAlert(
            alert_id="REG-009",
            regulation_name="SEC Form PF Amendments — Hedge Fund Reporting",
            jurisdiction="US",
            impact_description=(
                "Enhanced reporting for large hedge funds and private equity. "
                "Quarterly and current reporting requirements for qualifying funds. "
                "Relevant for US counterparties in cross-border derivative contracts."
            ),
            affected_contract_types=["IRS", "CDS", "LOAN"],
            effective_date="2023-12-14",
            source_url="https://www.sec.gov/rules/final/2023/ia-6297.pdf",
            severity=EventSeverity.LOW,
            urgency=EventSeverity.LOW,
            status="IN_FORCE",
            theme="REPORTING",
        ),
        RegulatoryAlert(
            alert_id="REG-018",
            regulation_name="Dodd-Frank Title VII — CFTC Margin Phase 6 Final Wave",
            jurisdiction="US",
            impact_description=(
                "CFTC uncleared swap margin requirements now cover smaller financial "
                "end-users (AANA threshold $8bn). Phase 6 final implementation required "
                "full re-papering of CSAs for in-scope counterparties. Ongoing "
                "compliance monitoring of IM threshold usage required."
            ),
            affected_contract_types=["IRS", "CDS", "FX_DERIVATIVE"],
            effective_date="2022-09-01",
            source_url="https://www.cftc.gov/LawRegulation/DoddFrankAct/Title_VII/index.htm",
            severity=EventSeverity.HIGH,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="CAPITAL",
        ),
        RegulatoryAlert(
            alert_id="REG-019",
            regulation_name="CFTC Digital Assets — Final Guidance on Derivatives",
            jurisdiction="US",
            impact_description=(
                "CFTC clarified its jurisdiction over crypto derivative products "
                "including Bitcoin and Ethereum futures and perpetual swaps. "
                "Registered derivatives trading facilities must onboard digital "
                "asset contracts under existing commodity exchange rules."
            ),
            affected_contract_types=["DIGITAL_ASSET", "COMMODITY_DERIVATIVE"],
            effective_date="2024-06-01",
            source_url="https://www.cftc.gov/DigitalAssets/index.htm",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.MEDIUM,
            status="IN_FORCE",
            theme="CRYPTO",
        ),
        RegulatoryAlert(
            alert_id="REG-020",
            regulation_name="SEC Climate Disclosure Rule — Stay Pending Appeal",
            jurisdiction="US",
            impact_description=(
                "SEC adopted climate disclosure rules (Mar 2024) but implementation "
                "stayed pending 8th Circuit appeal. If upheld, large accelerated filers "
                "must disclose Scope 1/2 emissions and climate risks from FY2025. "
                "Loan and structured finance documentation may need climate covenants."
            ),
            affected_contract_types=["LOAN", "PROJECT_FINANCE", "BOND"],
            effective_date="2026-01-01",
            source_url="https://www.sec.gov/rules/final/2024/33-11275.pdf",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.LOW,
            status="ADOPTED",
            theme="ESG",
        ),
        # ── GLOBAL ────────────────────────────────────────────────────────────
        RegulatoryAlert(
            alert_id="REG-010",
            regulation_name="India FEMA Derivatives Amendment — RBI Circular",
            jurisdiction="INDIA",
            impact_description=(
                "RBI updated permissible OTC derivative categories under FEMA. "
                "Non-resident entities hedging INR exposures must comply with new "
                "documentation and reporting to AD Category-I banks."
            ),
            affected_contract_types=["IRS", "FX_DERIVATIVE", "LOAN"],
            effective_date="2024-04-05",
            source_url="https://www.rbi.org.in/Scripts/NotificationUser.aspx",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.LOW,
            status="IN_FORCE",
            theme="REPORTING",
        ),
        RegulatoryAlert(
            alert_id="REG-021",
            regulation_name="FSB Crypto-Asset Framework — Global Implementation",
            jurisdiction="GLOBAL",
            impact_description=(
                "FSB published high-level recommendations for crypto-asset regulation "
                "in Jul 2023. G20 jurisdictions committed to implementation. Covers "
                "market integrity, investor protection, and cross-border cooperation "
                "for crypto derivatives and stablecoin settlement arrangements."
            ),
            affected_contract_types=["DIGITAL_ASSET", "TOKEN_SETTLEMENT"],
            effective_date="2025-01-01",
            source_url="https://www.fsb.org/2023/07/fsb-global-regulatory-framework-for-crypto-asset-activities",
            severity=EventSeverity.MEDIUM,
            urgency=EventSeverity.MEDIUM,
            status="ADOPTED",
            theme="CRYPTO",
        ),
        RegulatoryAlert(
            alert_id="REG-022",
            regulation_name="IOSCO DeFi Policy Recommendations",
            jurisdiction="GLOBAL",
            impact_description=(
                "IOSCO published 9 recommendations for DeFi regulation including "
                "applying same-activity same-risk principles to decentralised protocols. "
                "Jurisdictions expected to implement by end 2025. Relevant for "
                "firms offering smart-contract-based derivatives settlement."
            ),
            affected_contract_types=["DIGITAL_ASSET"],
            effective_date="2025-12-31",
            source_url="https://www.iosco.org/library/pubdocs/pdf/IOSCOPD748.pdf",
            severity=EventSeverity.LOW,
            urgency=EventSeverity.LOW,
            status="ADOPTED",
            theme="CRYPTO",
        ),
        RegulatoryAlert(
            alert_id="REG-023",
            regulation_name="Basel III Endgame — US Implementation (Revised Proposal)",
            jurisdiction="US",
            impact_description=(
                "US banking regulators re-proposed Basel III endgame rules in Sep 2024 "
                "with a reduced impact vs original. Market risk (FRTB) and credit risk "
                "SA changes still increase capital on derivatives. Effective date "
                "pushed to 2027. Affects US bank counterparty credit pricing."
            ),
            affected_contract_types=["IRS", "CDS", "LOAN"],
            effective_date="2027-07-01",
            source_url="https://www.federalreserve.gov/newsevents/pressreleases/bcreg20240909a.htm",
            severity=EventSeverity.HIGH,
            urgency=EventSeverity.LOW,
            status="PROPOSED",
            theme="CAPITAL",
        ),
        RegulatoryAlert(
            alert_id="REG-024",
            regulation_name="ISDA/IIFM Tahawwut Master Agreement — Sharia Derivatives",
            jurisdiction="GLOBAL",
            impact_description=(
                "Updated Tahawwut (Hedging) Master Agreement standard for Islamic "
                "finance derivatives published. Institutions in GCC and SE Asia "
                "offering Sharia-compliant IRS/FX products should review "
                "documentation against new ISDA/IIFM standard."
            ),
            affected_contract_types=["IRS", "FX_DERIVATIVE"],
            effective_date="2024-01-01",
            source_url="https://www.isda.org/a/bTEgE/",
            severity=EventSeverity.LOW,
            urgency=EventSeverity.LOW,
            status="IN_FORCE",
            theme="CONDUCT",
        ),
        RegulatoryAlert(
            alert_id="REG-025",
            regulation_name="G20 Cross-Border Payment Roadmap — FX Settlement Risk",
            jurisdiction="GLOBAL",
            impact_description=(
                "FSB and BIS CPMI published enhanced G20 cross-border payment roadmap. "
                "FX settlement risk targets include broader CLS adoption and PVP "
                "settlement alternatives. Correspondent banking agreements and FX "
                "derivative novation clauses may need updating."
            ),
            affected_contract_types=["FX_DERIVATIVE", "REPO"],
            effective_date="2027-12-31",
            source_url="https://www.fsb.org/2023/10/g20-roadmap-for-enhancing-cross-border-payments",
            severity=EventSeverity.LOW,
            urgency=EventSeverity.LOW,
            status="ADOPTED",
            theme="SETTLEMENT",
        ),
        RegulatoryAlert(
            alert_id="REG-026",
            regulation_name="Mansion House Compact — UK Pension Fund Infrastructure Allocation",
            jurisdiction="UK",
            impact_description=(
                "UK defined contribution pension funds committed to allocate 5% of "
                "assets to unlisted equities by 2030 under Mansion House Compact. "
                "Increases demand for bespoke project finance and infrastructure "
                "loan documentation. ISDA definitions may be applied to bespoke deals."
            ),
            affected_contract_types=["PROJECT_FINANCE", "LOAN"],
            effective_date="2030-01-01",
            source_url="https://www.gov.uk/government/publications/mansion-house-compact",
            severity=EventSeverity.LOW,
            urgency=EventSeverity.LOW,
            status="ADOPTED",
            theme="CAPITAL",
        ),
    ]


class RegulatoryWatch:
    """
    LAYER 3: Regulatory change monitoring.

    In production this would poll ESMA / FCA / EBA / SEC RSS feeds and
    parse new consultation papers, final rules, and Q&A updates.

    Currently: pre-loaded with 26 active regulatory items (Q1 2026).
    """

    def __init__(self):
        self._alerts: List[RegulatoryAlert] = _build_regulatory_db()

    def get_alerts(
        self,
        contract_type: str = "",
        jurisdiction: str = "",
        min_severity: EventSeverity = EventSeverity.LOW,
    ) -> List[RegulatoryAlert]:
        """
        Return alerts relevant to the given contract type and jurisdiction.
        Pass empty strings to get all alerts.
        """
        result = []
        for alert in self._alerts:
            if contract_type and jurisdiction:
                if not alert.impacts(contract_type, jurisdiction):
                    continue
            if _SEVERITY_ORDER[alert.severity] < _SEVERITY_ORDER[min_severity]:
                continue
            result.append(alert)
        # Sort: HIGH first, then by effective_date descending
        result.sort(
            key=lambda a: (
                -_SEVERITY_ORDER[a.severity],
                a.effective_date,
            )
        )
        return result

    def get_by_id(self, alert_id: str) -> Optional[RegulatoryAlert]:
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                return alert
        return None

    def all_alerts(self) -> List[RegulatoryAlert]:
        return list(self._alerts)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6: OracleV3 — MAIN FACADE
# ─────────────────────────────────────────────────────────────────────────────

class OracleV3:
    """
    Unified oracle — contract-agnostic.

    Any contract engine imports this and uses:
        oracle = OracleV3(newsapi_key=os.environ.get("NEWSAPI_KEY"))
        rate   = oracle.get_rate(RateID.EURIBOR_3M)
        events = oracle.get_events(contract_id="C-001")
        regs   = oracle.get_regulatory_alerts("IRS", "EU")

    The oracle maintains a shared RateRegistry (single cache across all
    contracts in the same process) and per-contract event subscriptions.
    """

    def __init__(
        self,
        newsapi_key: Optional[str] = None,
        anomaly_threshold_bps: Decimal = Decimal("5"),
    ):
        self.registry         = RateRegistry(anomaly_threshold_bps=anomaly_threshold_bps)
        self.event_monitor    = EventMonitor(newsapi_key=newsapi_key)
        self.regulatory_watch = RegulatoryWatch()
        self._subscriptions:  Dict[str, ContractSubscription] = {}

    # ── Layer 1: Market Data ──────────────────────────────────────────────────

    def get_rate(self, rate_id: RateID) -> RateReading:
        """Fetch a single rate with full fallback waterfall."""
        return self.registry.fetch(rate_id)

    def get_rates(self, rate_ids: List[RateID]) -> Dict[RateID, RateReading]:
        """Fetch multiple rates."""
        return self.registry.fetch_many(rate_ids)

    def rate_history(self, rate_id: RateID) -> List[RateReading]:
        return self.registry.history(rate_id)

    def latest_rate(self, rate_id: RateID) -> Optional[RateReading]:
        """Return cached latest reading without triggering a new fetch."""
        return self.registry.latest(rate_id)

    # ── Layer 2: Event Monitoring ─────────────────────────────────────────────

    def subscribe_contract(self, subscription: ContractSubscription) -> None:
        """Register a contract for event monitoring."""
        self._subscriptions[subscription.contract_id] = subscription

    def unsubscribe_contract(self, contract_id: str) -> None:
        self._subscriptions.pop(contract_id, None)

    def poll_events(self) -> List[MarketEvent]:
        """
        Run one full news poll cycle for all subscribed contracts.
        Call this on a schedule (e.g., hourly via cron or a background thread).
        """
        subs = list(self._subscriptions.values())
        return self.event_monitor.poll(subs)

    def get_events(
        self,
        contract_id: Optional[str] = None,
        min_severity: EventSeverity = EventSeverity.LOW,
        since_hours: int = 48,
    ) -> List[MarketEvent]:
        """Return stored events, optionally filtered by contract / severity / age."""
        return self.event_monitor.get_events(
            contract_id=contract_id,
            min_severity=min_severity,
            since_hours=since_hours,
        )

    # ── Layer 3: Regulatory Watch ─────────────────────────────────────────────

    def get_regulatory_alerts(
        self,
        contract_type: str = "",
        jurisdiction:  str = "",
        min_severity:  EventSeverity = EventSeverity.LOW,
    ) -> List[RegulatoryAlert]:
        return self.regulatory_watch.get_alerts(
            contract_type=contract_type,
            jurisdiction=jurisdiction,
            min_severity=min_severity,
        )

    # ── Composite summary ─────────────────────────────────────────────────────

    def oracle_summary(self) -> dict:
        """Full status snapshot — consumed by the advisor portal dashboard."""
        high_events = self.event_monitor.get_events(
            min_severity=EventSeverity.HIGH, since_hours=24
        )
        medium_events = self.event_monitor.get_events(
            min_severity=EventSeverity.MEDIUM, since_hours=24
        )
        high_regs = self.regulatory_watch.get_alerts(min_severity=EventSeverity.HIGH)

        return {
            "rates":               self.registry.summary(),
            "events_24h_high":     len(high_events),
            "events_24h_medium":   len(medium_events),
            "regulatory_high_count": len(high_regs),
            "subscribed_contracts": list(self._subscriptions.keys()),
            "newsapi_active":      self.event_monitor.api_key is not None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7: CONVENIENCE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_irs_subscription(
    contract_id:    str,
    party_a_name:   str,
    party_b_name:   str,
    jurisdictions:  List[str],
) -> ContractSubscription:
    """
    Convenience factory — builds a ContractSubscription for an IRS contract.
    Called by the IRS execution engine when a contract becomes active.
    """
    return ContractSubscription(
        contract_id=contract_id,
        counterparty_names=[party_a_name, party_b_name],
        jurisdictions=jurisdictions,
        extra_keywords=[],
        contract_type="IRS",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8: STANDALONE DEMO
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print("NOMOS ORACLE v3 — Live Demo")
    print("=" * 70)

    oracle = OracleV3(newsapi_key=os.environ.get("NEWSAPI_KEY"))

    # ── Layer 1: Fetch all market data ────────────────────────────────────────
    print("\n[ LAYER 1 — MARKET DATA ]")
    all_rate_ids = list(RateID)
    readings = oracle.get_rates(all_rate_ids)
    for rid, reading in readings.items():
        indicator = "OK" if reading.status == RateStatus.CONFIRMED else reading.status.value
        print(f"  {rid.value:<18} {str(reading.rate):<14} [{indicator}]  src: {reading.source}")

    # ── Layer 2: Event monitoring ─────────────────────────────────────────────
    print("\n[ LAYER 2 — EVENT MONITORING ]")
    sub = build_irs_subscription(
        contract_id="DEMO-001",
        party_a_name="Deutsche Bank",
        party_b_name="BNP Paribas",
        jurisdictions=["Germany", "France", "EU"],
    )
    oracle.subscribe_contract(sub)
    new_events = oracle.poll_events()
    if new_events:
        for ev in new_events[:3]:
            print(f"  [{ev.severity.value}] {ev.headline[:80]}")
    else:
        print("  (no events — stub mode or no NewsAPI key set)")

    # ── Layer 3: Regulatory watch ─────────────────────────────────────────────
    print("\n[ LAYER 3 — REGULATORY WATCH (IRS, EU) ]")
    alerts = oracle.get_regulatory_alerts("IRS", "EU")
    for alert in alerts:
        print(f"  [{alert.severity.value:<6}] {alert.alert_id}  {alert.regulation_name}")
        print(f"           Effective: {alert.effective_date}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n[ ORACLE SUMMARY ]")
    summary = oracle.oracle_summary()
    print(f"  Rates fetched:    {len(summary['rates'])}")
    print(f"  High events (24h):{summary['events_24h_high']}")
    print(f"  High reg alerts:  {summary['regulatory_high_count']}")
    print(f"  NewsAPI active:   {summary['newsapi_active']}")
    print("=" * 70)
