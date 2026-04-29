"""Editorial Streamlit dashboard for the Oracle.

Visual language follows the Nomos design system (Newsreader serif +
Geist sans + Geist Mono on a warm-paper palette, with a single ochre
accent and a sparing 'night' card for the trust-signal hero). The
dashboard is the only user-facing surface in this repo, so this file
also carries the platform's editorial aesthetic.

Four panels, narrative order rather than KPI grid:

1. **Chain integrity** — the trust signal, hero card.
2. **Latest attestations** — the chain narrative.
3. **Trigger events** — counts × severity, plus the recent feed.
4. **Collector health** — one editorial row per source.

The dashboard is read-only and queries the SQLite store directly. It
does not call any collector or write to the chain — running it against
production is safe.

Run::

    streamlit run oracle/dashboard/app.py -- --db-path /path/to/oracle.db
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st

from oracle.core.store import AttestationStore


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse ``streamlit run app.py -- --db-path ...`` arguments.

    Streamlit consumes its own flags before ours; everything after the
    bare ``--`` separator lands in ``sys.argv``.
    """

    parser = argparse.ArgumentParser(prog="oracle-dashboard")
    parser.add_argument("--db-path", type=Path, required=True)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Read queries (SQLite, read-only)
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_latest_attestations(
    conn: sqlite3.Connection, limit: int = 10
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT attestation_id, sequence_number, signed_at, current_hash,
               previous_hash, is_genesis, supersedes
        FROM attestations
        ORDER BY sequence_number DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_recent_triggers(
    conn: sqlite3.Connection, limit: int = 50
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT event_id, rule_id, severity, contract_id,
               evaluated_at, as_of, attestation_ref
        FROM trigger_events
        ORDER BY evaluated_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_trigger_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT rule_id, severity, COUNT(*) AS event_count
        FROM trigger_events
        GROUP BY rule_id, severity
        ORDER BY rule_id, severity
        """
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_collector_health(
    conn: sqlite3.Connection, now: datetime
) -> list[dict[str, Any]]:
    """One row per ``source_id`` ever seen in either table.

    Returns last_success time (max ``fetched_at`` from ``datapoints``),
    24h failure count (rows from ``source_failures`` since
    ``now - 24h``), and the latest attested value per source.
    """

    horizon = (now - timedelta(hours=24)).isoformat()

    cur = conn.execute(
        """
        WITH src AS (
            SELECT source_id FROM datapoints
            UNION
            SELECT source_id FROM source_failures
        ),
        last_ok AS (
            SELECT source_id,
                   MAX(fetched_at) AS last_fetched_at
            FROM datapoints
            GROUP BY source_id
        ),
        latest_value AS (
            SELECT d.source_id, d.metric, d.value, d.unit, d.as_of
            FROM datapoints d
            JOIN (
                SELECT source_id, MAX(fetched_at) AS mx
                FROM datapoints GROUP BY source_id
            ) m ON m.source_id = d.source_id AND m.mx = d.fetched_at
        ),
        fail24 AS (
            SELECT source_id, COUNT(*) AS failures_24h
            FROM source_failures
            WHERE attempted_at >= ?
            GROUP BY source_id
        )
        SELECT src.source_id,
               last_ok.last_fetched_at,
               COALESCE(fail24.failures_24h, 0) AS failures_24h,
               latest_value.metric  AS latest_metric,
               latest_value.value   AS latest_value,
               latest_value.unit    AS latest_unit,
               latest_value.as_of   AS latest_as_of
        FROM src
        LEFT JOIN last_ok       ON last_ok.source_id = src.source_id
        LEFT JOIN latest_value  ON latest_value.source_id = src.source_id
        LEFT JOIN fail24        ON fail24.source_id = src.source_id
        ORDER BY src.source_id
        """,
        (horizon,),
    )
    return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Editorial design system — Newsreader + Geist + warm paper palette
# ---------------------------------------------------------------------------


_THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Newsreader:ital,wght@0,400;0,500;0,600;1,400;1,500&family=Geist:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap');

:root {
  --paper: #F6F1E8;
  --paper-2: #EFE8DB;
  --card: #FBF7EE;
  --ink: #1C1A16;
  --ink-2: #2E2A22;
  --ink-3: #6B6456;
  --ink-4: #958D7C;
  --rule: #D9D1BE;
  --rule-2: #C7BEA8;
  --ochre: #B8853A;
  --ochre-2: #8F6423;
  --red: #A8442A;
  --green: #4E6A3A;
  --blue: #3F5A6B;
  --night: #1C1A16;
  --serif: 'Newsreader', Georgia, serif;
  --sans: 'Geist', ui-sans-serif, system-ui, sans-serif;
  --mono: 'Geist Mono', ui-monospace, monospace;
}

/* Strip Streamlit chrome so the page reads as a single editorial canvas. */
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer, [data-testid="stToolbar"] { visibility: hidden; }

/* Page surface. */
.stApp, .main, [data-testid="stAppViewContainer"] {
  background: var(--paper) !important;
  color: var(--ink) !important;
  font-family: var(--sans) !important;
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
}
.block-container {
  max-width: 1320px !important;
  padding-top: 56px !important;
  padding-bottom: 120px !important;
}

/* ---- Hero / page header ------------------------------------------------ */
.nomos-eyebrow {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--ink-3);
}
.nomos-title {
  font-family: var(--serif);
  font-weight: 400;
  font-size: 56px;
  line-height: 1.04;
  letter-spacing: -.02em;
  color: var(--ink);
  margin: 12px 0 16px;
}
.nomos-title em {
  font-style: italic;
  color: var(--ochre-2);
  font-weight: 500;
}
.nomos-lede {
  font-family: var(--serif);
  font-size: 18px;
  line-height: 1.5;
  color: var(--ink-2);
  max-width: 780px;
  margin: 0;
}
.nomos-meta {
  display: flex;
  gap: 28px;
  flex-wrap: wrap;
  margin-top: 32px;
  padding: 18px 0 6px;
  border-top: 1px dashed var(--rule-2);
  border-bottom: 1px solid var(--rule);
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--ink-2);
}
.nomos-meta .num { color: var(--ink-4); margin-right: 8px; }

/* ---- Section header ---------------------------------------------------- */
.nomos-section {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 32px;
  align-items: baseline;
  margin: 56px 0 22px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--rule);
}
.nomos-section .num {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--ink-4);
}
.nomos-section .title {
  font-family: var(--serif);
  font-weight: 400;
  font-size: 30px;
  letter-spacing: -.015em;
  margin: 0;
}
.nomos-section .sub {
  font-family: var(--serif);
  font-style: italic;
  color: var(--ink-3);
  font-size: 15px;
  margin-top: 4px;
}

/* ---- Hero: chain integrity (the trust signal) -------------------------- */
.integrity-hero {
  position: relative;
  margin-top: 16px;
  background: var(--night);
  color: #EEE6D2;
  border-radius: 10px;
  padding: 28px 32px;
  overflow: hidden;
}
.integrity-hero::before {
  content: '';
  position: absolute; inset: 0;
  background:
    radial-gradient(ellipse at 12% 18%, rgba(184,133,58,.16), transparent 45%),
    radial-gradient(ellipse at 92% 88%, rgba(78,106,58,.14), transparent 45%);
  pointer-events: none;
}
.integrity-hero.bad::before {
  background:
    radial-gradient(ellipse at 14% 18%, rgba(168,68,42,.22), transparent 45%),
    radial-gradient(ellipse at 92% 88%, rgba(184,133,58,.14), transparent 45%);
}
.integrity-hero .head {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 16px; position: relative; z-index: 1;
}
.integrity-hero .kicker {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: #B8B0A0;
}
.integrity-hero .ts {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: #B8B0A0;
}
.integrity-hero .pulse {
  display: inline-block;
  width: 7px; height: 7px; border-radius: 50%;
  background: #8AA877;
  margin-right: 8px;
  box-shadow: 0 0 0 0 rgba(138,168,119,.55);
  animation: nomosPulse 2.4s infinite;
  vertical-align: middle;
}
.integrity-hero.bad .pulse {
  background: #D88370;
  box-shadow: 0 0 0 0 rgba(216,131,112,.55);
}
@keyframes nomosPulse {
  0%   { box-shadow: 0 0 0 0 rgba(138,168,119,.55); }
  70%  { box-shadow: 0 0 0 9px rgba(138,168,119,0); }
  100% { box-shadow: 0 0 0 0 rgba(138,168,119,0); }
}
.integrity-hero .verdict {
  font-family: var(--serif);
  font-size: 30px;
  font-weight: 500;
  letter-spacing: -.015em;
  margin: 18px 0 6px;
  position: relative; z-index: 1;
}
.integrity-hero .verdict em { font-style: italic; color: #E2B978; }
.integrity-hero.bad .verdict em { color: #F0B197; }
.integrity-hero .body {
  font-family: var(--serif);
  font-size: 15px;
  line-height: 1.5;
  color: #C9C0AC;
  max-width: 760px;
  margin: 0;
  position: relative; z-index: 1;
}
.integrity-hero .err {
  font-family: var(--mono);
  font-size: 11.5px;
  color: #F0B197;
  margin-top: 14px;
  white-space: pre-wrap;
  position: relative; z-index: 1;
}

/* ---- Editorial cards (collector health, attestation rows) -------------- */
.health-grid { display: grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }
.health-row {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 8px;
  padding: 18px 22px;
  display: grid;
  grid-template-columns: 220px 1fr 200px;
  gap: 24px;
  align-items: center;
}
.health-row.warn { border-color: var(--ochre); background: #FBF2E0; }
.health-row.bad  { border-color: var(--red);   background: #F4E2DA; }
.health-row .src-name {
  font-family: var(--serif);
  font-weight: 500;
  font-size: 17px;
  letter-spacing: -.01em;
}
.health-row .src-id {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin-top: 2px;
}
.health-row .latest .lbl,
.health-row .stats .lbl {
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--ink-4);
  margin-bottom: 4px;
}
.health-row .latest .v {
  font-family: var(--serif);
  font-size: 15px;
  color: var(--ink-2);
}
.health-row .latest .v .metric {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin-right: 8px;
}
.health-row .latest .v .num {
  font-weight: 500;
  color: var(--ink);
}
.health-row .latest .when {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--ink-3);
  margin-top: 4px;
}
.health-row .stats .v {
  font-family: var(--serif);
  font-size: 15px;
  color: var(--ink-2);
}
.health-row .stats .v.bad   { color: var(--red); }
.health-row .stats .v.warn  { color: var(--ochre-2); }
.health-row .stats .v.ok    { color: var(--green); }

/* ---- Trigger summary chips -------------------------------------------- */
.trigger-counts {
  display: flex; flex-wrap: wrap; gap: 10px;
  margin: 12px 0 24px;
}
.trigger-counts .chip {
  display: inline-flex; align-items: baseline; gap: 10px;
  padding: 10px 14px;
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 6px;
}
.trigger-counts .chip .rule {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: .04em;
  color: var(--ink-2);
}
.trigger-counts .chip .sev {
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--ink-4);
}
.trigger-counts .chip .n {
  font-family: var(--serif);
  font-weight: 500;
  font-size: 18px;
  color: var(--ink);
  margin-left: 4px;
}
.trigger-counts .chip.sev-trigger { border-color: var(--red); background: #F4E2DA; }
.trigger-counts .chip.sev-trigger .sev { color: var(--red); }
.trigger-counts .chip.sev-warn,
.trigger-counts .chip.sev-watch { border-color: var(--ochre); background: #FBF2E0; }
.trigger-counts .chip.sev-warn .sev,
.trigger-counts .chip.sev-watch .sev { color: var(--ochre-2); }

/* ---- Empty-state notes ------------------------------------------------- */
.nomos-empty {
  font-family: var(--serif);
  font-style: italic;
  font-size: 15px;
  color: var(--ink-3);
  padding: 18px 0 8px;
}

/* ---- Streamlit dataframe / table tone-down ----------------------------- */
[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; }
[data-testid="stDataFrame"] * {
  font-family: var(--mono) !important;
  font-size: 11.5px !important;
  color: var(--ink-2) !important;
}
[data-testid="stDataFrame"] [role="columnheader"] {
  text-transform: uppercase;
  letter-spacing: .08em;
  font-size: 10px !important;
  color: var(--ink-3) !important;
  background: var(--paper-2) !important;
}
</style>
"""


def _render_theme() -> None:
    st.markdown(_THEME_CSS, unsafe_allow_html=True)


def _render_header(db_path: Path, now: datetime) -> None:
    st.markdown(
        f"""
        <div>
          <div class="nomos-eyebrow">Nomos · Oracle</div>
          <h1 class="nomos-title">Operational <em>dashboard</em></h1>
          <p class="nomos-lede">
            Signed, chained attestations of market and legal data
            for the DerivAI engine. The chain is read-only here — every
            row was sealed by the scheduler and verified on disk.
          </p>
          <div class="nomos-meta">
            <span><span class="num">DB</span>{html.escape(str(db_path))}</span>
            <span><span class="num">Loaded</span>{now.strftime("%Y-%m-%d %H:%M UTC")}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_section(num: str, title_html: str, sub: str) -> None:
    st.markdown(
        f"""
        <div class="nomos-section">
          <div class="num">{num}</div>
          <div>
            <div class="title">{title_html}</div>
            <div class="sub">{html.escape(sub)}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------


def _panel_chain_integrity(db_path: Path) -> None:
    """The hero. The trust signal lives here, so it gets the night card."""

    _render_section(
        "§ 01",
        "The chain, <em>verified</em>",
        "byte-level SHA-256 walk over every row · re-runs each load",
    )

    checked_at = datetime.now(timezone.utc)
    store = AttestationStore(db_path)
    ok, error = store.verify_integrity()

    if ok:
        verdict_html = "Chain <em>intact</em>."
        body = (
            "No tampering detected. Every payload re-hashed and every "
            "linked-list pointer resolved cleanly across the full ledger."
        )
        klass = ""
    else:
        verdict_html = "Chain <em>broken</em>."
        body = (
            "verify_integrity refused the ledger. Investigate before "
            "appending — do not delete the offending row."
        )
        klass = "bad"

    err_html = (
        f'<div class="err">{html.escape(str(error))}</div>' if error else ""
    )
    st.markdown(
        f"""
        <div class="integrity-hero {klass}">
          <div class="head">
            <span class="kicker"><span class="pulse"></span>Chain integrity</span>
            <span class="ts">Checked {checked_at.strftime("%Y-%m-%d %H:%M:%S UTC")}</span>
          </div>
          <div class="verdict">{verdict_html}</div>
          <p class="body">{html.escape(body)}</p>
          {err_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _panel_attestations(rows: list[dict[str, Any]]) -> None:
    _render_section(
        "§ 02",
        "Latest <em>attestations</em>",
        "ten most recent rows · ordered by sequence_number desc",
    )
    if not rows:
        st.markdown(
            '<p class="nomos-empty">No attestations recorded yet — '
            "run <code>make run-daily</code> to seal the genesis block.</p>",
            unsafe_allow_html=True,
        )
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _panel_triggers(
    summary: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> None:
    _render_section(
        "§ 03",
        "Rule <em>triggers</em>",
        "events the rule engine emitted while walking the chain",
    )

    if not summary and not rows:
        st.markdown(
            '<p class="nomos-empty">No trigger events yet. '
            "A quiet ledger is information too.</p>",
            unsafe_allow_html=True,
        )
        return

    if summary:
        chips = []
        for r in summary:
            sev = (r.get("severity") or "").lower()
            sev_class = ""
            if sev == "trigger":
                sev_class = "sev-trigger"
            elif sev in ("warn", "warning", "watch"):
                sev_class = "sev-warn"
            chips.append(
                '<span class="chip {cls}">'
                '<span class="rule">{rule}</span>'
                '<span class="sev">{sev}</span>'
                '<span class="n">{n}</span>'
                "</span>".format(
                    cls=sev_class,
                    rule=html.escape(str(r.get("rule_id") or "")),
                    sev=html.escape(str(r.get("severity") or "")),
                    n=int(r.get("event_count") or 0),
                )
            )
        st.markdown(
            f'<div class="trigger-counts">{"".join(chips)}</div>',
            unsafe_allow_html=True,
        )

    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)


def _panel_collector_health(
    rows: list[dict[str, Any]], now: datetime
) -> None:
    _render_section(
        "§ 04",
        "Collector <em>health</em>",
        "per-source pulse · last fetch, recent failures, latest attested value",
    )

    if not rows:
        st.markdown(
            '<p class="nomos-empty">No collector activity recorded yet.</p>',
            unsafe_allow_html=True,
        )
        return

    cards = []
    for r in rows:
        source_id = r.get("source_id") or ""
        last_fetched = r.get("last_fetched_at")
        failures = int(r.get("failures_24h") or 0)
        metric = r.get("latest_metric")
        value = r.get("latest_value")
        unit = r.get("latest_unit")
        as_of = r.get("latest_as_of")

        when_text = "—"
        klass = ""
        if last_fetched:
            try:
                ts = datetime.fromisoformat(str(last_fetched))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = now - ts
                when_text = _humanise_age(age) + " ago"
                if age > timedelta(hours=24):
                    klass = "warn"
            except ValueError:
                when_text = str(last_fetched)
        else:
            klass = "warn"

        if failures > 0 and not klass:
            klass = "warn"
        if failures > 5:
            klass = "bad"

        if value is not None and metric is not None:
            unit_suffix = ""
            if unit and "fraction" in str(unit).lower():
                unit_suffix = " <span class='dim'>·</span> rate"
            latest_html = (
                f'<span class="metric">{html.escape(str(metric))}</span>'
                f'<span class="num">{html.escape(str(value))}</span>'
                f"{unit_suffix}"
            )
            as_of_html = (
                f'<div class="when">as of {html.escape(str(as_of))} '
                f'· fetched {when_text}</div>'
                if as_of
                else f'<div class="when">fetched {when_text}</div>'
            )
        else:
            latest_html = '<span class="metric">no value yet</span>'
            as_of_html = f'<div class="when">{when_text}</div>'

        if failures == 0:
            stats_class = "ok"
            stats_text = "no failures (24h)"
        elif failures <= 5:
            stats_class = "warn"
            stats_text = f"{failures} failure{'s' if failures != 1 else ''} (24h)"
        else:
            stats_class = "bad"
            stats_text = f"{failures} failures (24h)"

        cards.append(
            f"""
            <div class="health-row {klass}">
              <div>
                <div class="src-name">{html.escape(_pretty_source(source_id))}</div>
                <div class="src-id">{html.escape(str(source_id))}</div>
              </div>
              <div class="latest">
                <div class="lbl">Latest attested</div>
                <div class="v">{latest_html}</div>
                {as_of_html}
              </div>
              <div class="stats">
                <div class="lbl">Last 24h</div>
                <div class="v {stats_class}">{html.escape(stats_text)}</div>
              </div>
            </div>
            """
        )

    st.markdown(
        f'<div class="health-grid">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pretty_source(source_id: str) -> str:
    """Display name for a source_id. Falls back to the id itself."""

    return {
        "ecb_sdw_v1": "ECB Statistical Data Warehouse",
        "fake_v1": "Fake collector (fixture)",
    }.get(source_id, source_id)


def _humanise_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60} min"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    args = _parse_args()
    if not args.db_path.exists():
        st.set_page_config(page_title="Nomos Oracle", layout="wide")
        _render_theme()
        st.markdown(
            f'<p class="nomos-empty">Database not found at '
            f"<code>{html.escape(str(args.db_path))}</code>.</p>",
            unsafe_allow_html=True,
        )
        sys.exit(1)

    st.set_page_config(page_title="Nomos Oracle", layout="wide")
    _render_theme()

    now = datetime.now(timezone.utc)
    _render_header(args.db_path, now)

    with _connect(args.db_path) as conn:
        attestations = _fetch_latest_attestations(conn)
        trigger_rows = _fetch_recent_triggers(conn)
        trigger_summary = _fetch_trigger_summary(conn)
        collector_rows = _fetch_collector_health(conn, now)

    _panel_chain_integrity(args.db_path)
    _panel_attestations(attestations)
    _panel_triggers(trigger_summary, trigger_rows)
    _panel_collector_health(collector_rows, now)


if __name__ == "__main__":
    main()
else:
    # Streamlit imports the module rather than calling it as ``__main__``;
    # invoke ``main()`` so the page renders.
    main()
